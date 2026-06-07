"""按历史批次重跑 - 单元测试

覆盖：
  - rebuild_config_from_batch: 正常重建、缺字段报错、output_root 替换路径
  - rebuild_manifest_from_batch: 从 COPY 动作重建清单
  - rerun_batch: 正常重跑、预检冲突阻止、completed/partial 才能重跑
  - 跨重启持久化: parent_batch_id / rerun_params 持久化后可读取
  - 导出 JSON/CSV: 包含 parent_batch_id 和 rerun_params
  - rollback 只影响重跑批次，不碰原批次产物
"""

from __future__ import annotations

import csv
import json
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List

from contract_pack.config import AppConfig, PackageConfig
from contract_pack.engine import (
    Engine,
    RerunError,
    rebuild_config_from_batch,
    rebuild_manifest_from_batch,
    rerun_batch,
)
from contract_pack.manifest import load_manifest
from contract_pack.precheck import run_precheck
from contract_pack.report import export_csv, export_json, batch_to_dict
from contract_pack.storage import (
    BATCH_STATUS,
    FILE_ACTION,
    FILE_STATUS,
    Batch,
    BatchStorage,
    FileAction,
)


TESTS_PASS = 0
TESTS_FAIL = 0


def assert_eq(cond, msg):
    global TESTS_PASS, TESTS_FAIL
    if cond:
        TESTS_PASS += 1
        print(f"  ✓ {msg}")
    else:
        TESTS_FAIL += 1
        print(f"  ✗ {msg}")
        raise AssertionError(msg)


def _make_completed_batch(tmpdir: Path) -> tuple[Batch, Path, Path]:
    """创建一个完整的工作环境并执行一次批次，返回 (批次, work目录, manifest路径)。"""
    work = tmpdir / "scenario"
    work.mkdir()

    src = work / "sources"
    (src / "contracts").mkdir(parents=True)
    (src / "scans").mkdir(parents=True)
    (src / "contracts" / "main.pdf").write_text("MAIN CONTRACT", encoding="utf-8")
    (src / "scans" / "seal.jpg").write_text("SEAL IMAGE", encoding="utf-8")

    manifest = work / "manifest.csv"
    with open(manifest, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["package", "category", "source_path", "target_name", "version", "description"])
        w.writerow(["甲方交付包", "main", "contracts/main.pdf", "主合同.pdf", "v1", ""])
        w.writerow(["甲方交付包", "seal", "scans/seal.jpg", "盖章扫描件.jpg", "", ""])

    db_path = work / ".contract_pack.db"
    cfg = AppConfig(
        manifest_path=manifest,
        source_root=src,
        packages=[
            PackageConfig(
                name="甲方交付包",
                output_dir=work / "deliver" / "PartyA",
                zip_output=work / "deliver" / "甲方交付包.zip",
                file_mapping={"main": "01_主合同"},
                version="v2024.06",
            )
        ],
        operator="tester",
        db_path=db_path,
        allow_overwrite=True,
    )

    entries = load_manifest(manifest)
    storage = BatchStorage(db_path)
    precheck = run_precheck(cfg, entries, storage=storage, last_batch_id=None)
    assert_eq(precheck.ok, "首次预检通过")

    engine = Engine(cfg)
    result = engine.run(entries, precheck, make_zip=True)
    assert_eq(result.status == BATCH_STATUS["COMPLETED"], f"首次批次执行 completed (got {result.status})")

    batch = storage.get_batch(result.batch_id)
    assert_eq(batch is not None, "批次已存入数据库")
    assert_eq(len(batch.file_actions) >= 2, "批次有至少 2 个文件动作 (copy+copy+zip 或更多)")
    return batch, work, manifest


# ---------------------------------------------------------------------------
# rebuild_config_from_batch tests
# ---------------------------------------------------------------------------

def test_rebuild_config_normal():
    """正常场景：从 completed 批次重建配置成功。"""
    print("\n=== test_rebuild_config_normal ===")
    tmpdir = Path(tempfile.mkdtemp(prefix="rerun_unit_rcn_"))
    try:
        batch, work, manifest = _make_completed_batch(tmpdir)
        new_cfg = rebuild_config_from_batch(batch, base_dir=work)
        assert_eq(len(new_cfg.packages) == 1, "重建配置含 1 个包")
        assert_eq(new_cfg.packages[0].name == "甲方交付包", "包名正确")
        assert_eq(new_cfg.operator == "tester", "operator 沿用原批次")
        assert_eq(new_cfg.allow_overwrite is False, "默认 allow_overwrite=False（禁止覆盖）")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_rebuild_config_missing_packages():
    """config_summary 缺少 packages 字段 -> 抛 RerunError。"""
    print("\n=== test_rebuild_config_missing_packages ===")
    tmpdir = Path(tempfile.mkdtemp(prefix="rerun_unit_missing_pkg_"))
    try:
        batch, work, _ = _make_completed_batch(tmpdir)
        bad_batch = Batch(
            id=batch.id, status=batch.status, operator=batch.operator,
            started_at=batch.started_at, config_summary={"manifest_path": "/x"},
        )
        try:
            rebuild_config_from_batch(bad_batch, base_dir=work)
            assert_eq(False, "缺 packages 应抛 RerunError")
        except RerunError as e:
            assert_eq("packages" in str(e), "错误信息提到 packages")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_rebuild_config_package_missing_output_dir():
    """包配置缺 output_dir -> 抛 RerunError。"""
    print("\n=== test_rebuild_config_package_missing_output_dir ===")
    tmpdir = Path(tempfile.mkdtemp(prefix="rerun_unit_missing_out_"))
    try:
        batch, work, _ = _make_completed_batch(tmpdir)
        bad_summary = {"packages": [{"name": "A包"}], "manifest_path": "/x", "source_root": "/y"}
        bad_batch = Batch(
            id=batch.id, status=batch.status, operator=batch.operator,
            started_at=batch.started_at, config_summary=bad_summary,
        )
        try:
            rebuild_config_from_batch(bad_batch, base_dir=work)
            assert_eq(False, "包缺 output_dir 应抛 RerunError")
        except RerunError as e:
            assert_eq("output_dir" in str(e), "错误信息提到 output_dir")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_rebuild_config_missing_manifest_path():
    """config_summary 缺 manifest_path 且未传新 manifest -> 抛错。"""
    print("\n=== test_rebuild_config_missing_manifest_path ===")
    tmpdir = Path(tempfile.mkdtemp(prefix="rerun_unit_missing_mf_"))
    try:
        batch, work, _ = _make_completed_batch(tmpdir)
        summary = dict(batch.config_summary)
        summary.pop("manifest_path", None)
        bad_batch = Batch(
            id=batch.id, status=batch.status, operator=batch.operator,
            started_at=batch.started_at, config_summary=summary,
        )
        try:
            rebuild_config_from_batch(bad_batch, base_dir=work)
            assert_eq(False, "缺 manifest_path 应抛错")
        except RerunError as e:
            assert_eq("manifest_path" in str(e) or "manifest" in str(e), "错误信息提到 manifest")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_rebuild_config_output_root_rewrites_paths():
    """指定 output_root -> 所有包的 output_dir 和 zip_output 被重写到新根下。"""
    print("\n=== test_rebuild_config_output_root_rewrites_paths ===")
    tmpdir = Path(tempfile.mkdtemp(prefix="rerun_unit_out_root_"))
    try:
        batch, work, _ = _make_completed_batch(tmpdir)
        new_root = work / "deliver_rerun"
        new_cfg = rebuild_config_from_batch(batch, base_dir=work, output_root=new_root)
        pkg = new_cfg.packages[0]
        assert_eq(str(pkg.output_dir).startswith(str(new_root)),
                  f"output_dir 在新根下: {pkg.output_dir} vs {new_root}")
        assert_eq(pkg.zip_output is not None and str(pkg.zip_output).startswith(str(new_root)),
                  f"zip_output 在新根下: {pkg.zip_output}")
        assert_eq(pkg.output_dir.name == "PartyA", "output_dir 保持 basename")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_rebuild_config_replace_manifest_and_source_root():
    """替换 manifest 和 source_root 参数，新值被使用。"""
    print("\n=== test_rebuild_config_replace_manifest_and_source_root ===")
    tmpdir = Path(tempfile.mkdtemp(prefix="rerun_unit_replace_"))
    try:
        batch, work, _ = _make_completed_batch(tmpdir)
        new_manifest = work / "new_manifest.csv"
        new_manifest.write_text(
            "package,category,source_path,target_name\n甲方交付包,main,c/a.pdf,x.pdf\n",
            encoding="utf-8-sig",
        )
        new_src = work / "new_sources"
        new_src.mkdir()
        new_cfg = rebuild_config_from_batch(
            batch, base_dir=work, manifest_path=new_manifest, source_root=new_src,
        )
        assert_eq(new_cfg.manifest_path.resolve() == new_manifest.resolve(),
                  "manifest_path 被替换")
        assert_eq(new_cfg.source_root.resolve() == new_src.resolve(),
                  "source_root 被替换")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# rebuild_manifest_from_batch tests
# ---------------------------------------------------------------------------

def test_rebuild_manifest_from_copy_actions():
    """从 completed 批次的 COPY 动作重建 ManifestEntry。"""
    print("\n=== test_rebuild_manifest_from_copy_actions ===")
    tmpdir = Path(tempfile.mkdtemp(prefix="rerun_unit_rb_mf_"))
    try:
        batch, work, _ = _make_completed_batch(tmpdir)
        entries = rebuild_manifest_from_batch(batch)
        copy_count = sum(1 for fa in batch.file_actions if fa.action == FILE_ACTION["COPY"] and fa.category != "__zip__")
        assert_eq(len(entries) == copy_count, f"重建了 {copy_count} 条 manifest 条目（实际 {len(entries)}）")
        pkgs = {e.package for e in entries}
        assert_eq("甲方交付包" in pkgs, "条目属于甲方交付包")
        targets = {e.target_name for e in entries}
        assert_eq("主合同.pdf" in targets, "目标名包含主合同.pdf")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# rerun_batch tests
# ---------------------------------------------------------------------------

def test_rerun_normal_success():
    """重跑成功：输出到新目录，生成新批次，记录 parent_batch_id。"""
    print("\n=== test_rerun_normal_success ===")
    tmpdir = Path(tempfile.mkdtemp(prefix="rerun_unit_ok_"))
    try:
        batch, work, _ = _make_completed_batch(tmpdir)
        new_root = work / "deliver_rerun"
        storage = BatchStorage(work / ".contract_pack.db")
        result = rerun_batch(
            parent_batch_id=batch.id,
            storage=storage,
            base_dir=work,
            output_root=new_root,
            make_zip=True,
        )
        assert_eq(result.status == BATCH_STATUS["COMPLETED"],
                  f"重跑 completed (got {result.status})")
        assert_eq(result.parent_batch_id == batch.id, "result.parent_batch_id 正确")

        rerun_b = storage.get_batch(result.batch_id)
        assert_eq(rerun_b is not None, "重跑批次已入库")
        assert_eq(rerun_b.parent_batch_id == batch.id, "DB 中 parent_batch_id 正确")
        assert_eq(rerun_b.rerun_params.get("make_zip") is True, "DB 中 rerun_params.make_zip=True")
        assert_eq(rerun_b.rerun_params.get("output_root") is not None, "DB 中 rerun_params 含 output_root")

        assert_eq((new_root / "PartyA" / "主合同.pdf").exists(), "重跑产物存在于新目录")
        orig_party_a = work / "deliver" / "PartyA" / "主合同.pdf"
        assert_eq(orig_party_a.exists(), "原批次产物仍然存在（未被覆盖或删除）")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_rerun_only_completed_or_partial():
    """只有 completed/partial 批次可重跑，其他状态拒绝。"""
    print("\n=== test_rerun_only_completed_or_partial ===")
    tmpdir = Path(tempfile.mkdtemp(prefix="rerun_unit_status_"))
    try:
        work = tmpdir / "scenario"
        work.mkdir()
        db_path = work / ".db"
        storage = BatchStorage(db_path)
        for status_name, status_val in [
            ("FAILED", BATCH_STATUS["FAILED"]),
            ("RUNNING", BATCH_STATUS["RUNNING"]),
            ("ROLLED_BACK", BATCH_STATUS["ROLLED_BACK"]),
        ]:
            b = storage.create_batch("tester", {"packages": [], "manifest_path": "/x", "source_root": "/y"})
            storage.update_batch_status(b.id, status_val, finished=True)
            try:
                rerun_batch(parent_batch_id=b.id, storage=storage, base_dir=work)
                assert_eq(False, f"{status_name} 状态应拒绝重跑")
            except RerunError as e:
                assert_eq("completed" in str(e).lower() or "partial" in str(e).lower(),
                          f"错误信息提到 completed/partial: {e}")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_rerun_missing_parent():
    """父批次不存在 -> RerunError。"""
    print("\n=== test_rerun_missing_parent ===")
    tmpdir = Path(tempfile.mkdtemp(prefix="rerun_unit_noparent_"))
    try:
        work = tmpdir / "s"
        work.mkdir()
        storage = BatchStorage(work / ".db")
        try:
            rerun_batch(parent_batch_id="non-existent-id", storage=storage, base_dir=work)
            assert_eq(False, "不存在的批次应抛错")
        except RerunError as e:
            assert_eq("不存在" in str(e), "错误信息提到不存在")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_rerun_target_exists_blocked_by_default():
    """默认 allow_overwrite=False：目标文件已存在 -> 预检失败，重跑被阻止。"""
    print("\n=== test_rerun_target_exists_blocked_by_default ===")
    tmpdir = Path(tempfile.mkdtemp(prefix="rerun_unit_block_"))
    try:
        batch, work, _ = _make_completed_batch(tmpdir)
        storage = BatchStorage(work / ".contract_pack.db")
        result = rerun_batch(
            parent_batch_id=batch.id,
            storage=storage,
            base_dir=work,
        )
        assert_eq(result.status == BATCH_STATUS["FAILED"],
                  f"目标存在时重跑预检失败 (got {result.status})")
        assert_eq(result.precheck is not None and len(result.precheck.errors) > 0,
                  "返回 precheck 且含 errors")
        kinds = {i.kind for i in result.precheck.errors}
        has_conflict = any(k in kinds for k in ("target_exists", "zip_exists", "output_dir_has_content", "zip_already_exists"))
        assert_eq(has_conflict, f"错误类型含冲突相关: {kinds}")

        rerun_b = storage.get_batch(result.batch_id)
        assert_eq(rerun_b is not None, "失败的重跑批次也被记录入库")
        assert_eq(rerun_b.parent_batch_id == batch.id, "失败批次也记录 parent_batch_id")
        assert_eq(rerun_b.status == BATCH_STATUS["FAILED"], "失败批次状态为 failed")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_rerun_force_bypasses_precheck():
    """--force 跳过预检失败，强制执行。"""
    print("\n=== test_rerun_force_bypasses_precheck ===")
    tmpdir = Path(tempfile.mkdtemp(prefix="rerun_unit_force_"))
    try:
        batch, work, _ = _make_completed_batch(tmpdir)
        storage = BatchStorage(work / ".contract_pack.db")

        new_cfg = rebuild_config_from_batch(batch, base_dir=work, allow_overwrite=True)
        new_cfg.allow_overwrite = True

        result = rerun_batch(
            parent_batch_id=batch.id,
            storage=storage,
            base_dir=work,
            force=True,
        )
        assert_eq(result.status in (BATCH_STATUS["COMPLETED"], BATCH_STATUS["PARTIAL"]),
                  f"--force 下重跑继续执行 (status={result.status})")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_rerun_partial_then_rerun_again():
    """第一次重跑部分失败 -> 再重跑（换 output_root）可以成功。"""
    print("\n=== test_rerun_partial_then_rerun_again ===")
    tmpdir = Path(tempfile.mkdtemp(prefix="rerun_unit_partial_"))
    try:
        batch, work, manifest = _make_completed_batch(tmpdir)

        bad_src = work / "bad_sources"
        (bad_src / "contracts").mkdir(parents=True)
        (bad_src / "scans").mkdir(parents=True)
        (bad_src / "scans" / "seal.jpg").write_text("SEAL", encoding="utf-8")

        bad_manifest = work / "bad.csv"
        with open(bad_manifest, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            w.writerow(["package", "category", "source_path", "target_name", "version", "description"])
            w.writerow(["甲方交付包", "main", "contracts/missing.pdf", "主合同_v2.pdf", "v2", ""])
            w.writerow(["甲方交付包", "seal", "scans/seal.jpg", "盖章_v2.jpg", "", ""])

        new_root1 = work / "deliver_r1"
        storage = BatchStorage(work / ".contract_pack.db")
        r1 = rerun_batch(
            parent_batch_id=batch.id,
            storage=storage,
            base_dir=work,
            manifest_path=bad_manifest,
            source_root=bad_src,
            output_root=new_root1,
        )
        assert_eq(r1.status == BATCH_STATUS["PARTIAL"] or r1.status == BATCH_STATUS["FAILED"],
                  f"源文件缺失，重跑 partial/failed: {r1.status}")

        new_root2 = work / "deliver_r2"
        r2 = rerun_batch(
            parent_batch_id=batch.id,
            storage=storage,
            base_dir=work,
            output_root=new_root2,
            make_zip=False,
        )
        assert_eq(r2.status == BATCH_STATUS["COMPLETED"],
                  f"第二次重跑（输出到新目录） completed: {r2.status}")
        b2 = storage.get_batch(r2.batch_id)
        assert_eq(b2.parent_batch_id == batch.id, "第二次重跑 parent_batch_id 仍指向原始批次")
        assert_eq((new_root2 / "PartyA" / "主合同.pdf").exists(), "第二次重跑产物存在")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Persistence across restart
# ---------------------------------------------------------------------------

def test_rerun_persistence_across_reopen():
    """跨重启：重跑批次的 parent_batch_id / rerun_params 持久化后仍可读取。"""
    print("\n=== test_rerun_persistence_across_reopen ===")
    tmpdir = Path(tempfile.mkdtemp(prefix="rerun_unit_persist_"))
    try:
        batch, work, _ = _make_completed_batch(tmpdir)
        new_root = work / "deliver_rerun"
        db_path = work / ".contract_pack.db"
        storage1 = BatchStorage(db_path)
        result = rerun_batch(
            parent_batch_id=batch.id,
            storage=storage1,
            base_dir=work,
            output_root=new_root,
        )
        rerun_id = result.batch_id
        del storage1
        time.sleep(0.05)

        storage2 = BatchStorage(db_path)
        rerun_b = storage2.get_batch(rerun_id)
        assert_eq(rerun_b is not None, "重新打开 DB 后仍能读取重跑批次")
        assert_eq(rerun_b.parent_batch_id == batch.id, "parent_batch_id 持久化正确")
        assert_eq(rerun_b.rerun_params.get("output_root") is not None, "rerun_params 持久化正确")
        assert_eq(rerun_b.rerun_params.get("make_zip") is False, "rerun_params.make_zip=False")

        lst = storage2.list_batches(limit=10)
        ids_in_list = {b.id for b in lst}
        assert_eq(batch.id in ids_in_list, "list_batches 包含原始批次")
        assert_eq(rerun_id in ids_in_list, "list_batches 包含重跑批次")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Export JSON/CSV
# ---------------------------------------------------------------------------

def test_rerun_export_json_has_parent_and_params():
    """导出 JSON: 含 parent_batch_id 和 rerun_params。"""
    print("\n=== test_rerun_export_json_has_parent_and_params ===")
    tmpdir = Path(tempfile.mkdtemp(prefix="rerun_unit_exp_json_"))
    try:
        batch, work, _ = _make_completed_batch(tmpdir)
        new_root = work / "deliver_rerun"
        storage = BatchStorage(work / ".contract_pack.db")
        result = rerun_batch(
            parent_batch_id=batch.id,
            storage=storage,
            base_dir=work,
            output_root=new_root,
        )
        rerun_b = storage.get_batch(result.batch_id)

        json_path = work / "report.json"
        export_json([rerun_b], json_path)
        with open(json_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        assert_eq(len(data) == 1, "JSON 有 1 条")
        rec = data[0]
        assert_eq(rec.get("parent_batch_id") == batch.id, "JSON 含 parent_batch_id 且正确")
        assert_eq("rerun_params" in rec, "JSON 含 rerun_params")
        assert_eq(rec["rerun_params"].get("make_zip") is False, "JSON 中 rerun_params.make_zip=False")

        d = batch_to_dict(rerun_b)
        assert_eq(d["parent_batch_id"] == batch.id, "batch_to_dict 含 parent_batch_id")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_rerun_export_csv_has_parent_and_params():
    """导出 CSV: 含 parent_batch_id 和 rerun_params 列。"""
    print("\n=== test_rerun_export_csv_has_parent_and_params ===")
    tmpdir = Path(tempfile.mkdtemp(prefix="rerun_unit_exp_csv_"))
    try:
        batch, work, _ = _make_completed_batch(tmpdir)
        new_root = work / "deliver_rerun"
        storage = BatchStorage(work / ".contract_pack.db")
        result = rerun_batch(
            parent_batch_id=batch.id,
            storage=storage,
            base_dir=work,
            output_root=new_root,
        )
        rerun_b = storage.get_batch(result.batch_id)

        csv_path = work / "report.csv"
        export_csv([rerun_b], csv_path)
        with open(csv_path, "r", encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            rows = list(reader)
        headers = reader.fieldnames or []
        assert_eq("parent_batch_id" in headers, "CSV 表头含 parent_batch_id")
        assert_eq("rerun_params" in headers, "CSV 表头含 rerun_params")
        assert_eq(all(r["parent_batch_id"] == batch.id for r in rows),
                  "所有 CSV 行的 parent_batch_id 正确")
        any_has_rp = any(json.loads(r["rerun_params"] or "{}").get("output_root") for r in rows)
        assert_eq(any_has_rp, "CSV 中 rerun_params 可解析且含 output_root")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Rollback isolation
# ---------------------------------------------------------------------------

def test_rerun_rollback_only_rerun_artifacts():
    """rollback 重跑批次 -> 只删除重跑产物，原批次产物保留。"""
    print("\n=== test_rerun_rollback_only_rerun_artifacts ===")
    tmpdir = Path(tempfile.mkdtemp(prefix="rerun_unit_rb_iso_"))
    try:
        batch, work, _ = _make_completed_batch(tmpdir)
        new_root = work / "deliver_rerun"
        storage = BatchStorage(work / ".contract_pack.db")
        result = rerun_batch(
            parent_batch_id=batch.id,
            storage=storage,
            base_dir=work,
            output_root=new_root,
            make_zip=True,
        )
        assert_eq(result.status == BATCH_STATUS["COMPLETED"], "重跑 completed")

        orig_main = work / "deliver" / "PartyA" / "主合同.pdf"
        orig_zip = work / "deliver" / "甲方交付包.zip"
        rerun_main = new_root / "PartyA" / "主合同.pdf"
        rerun_zip = new_root / "甲方交付包.zip"
        assert_eq(orig_main.exists() and orig_zip.exists(), "原批次产物都在")
        assert_eq(rerun_main.exists() and rerun_zip.exists(), "重跑产物都在")

        new_cfg = rebuild_config_from_batch(batch, base_dir=work, output_root=new_root)
        engine = Engine(new_cfg)
        ok, msg = engine.rollback(result.batch_id)
        assert_eq(ok, f"rollback 重跑批次成功: {msg}")

        assert_eq(not rerun_main.exists(), "rollback 后重跑主合同被删除")
        assert_eq(not rerun_zip.exists(), "rollback 后重跑 zip 被删除")
        assert_eq(orig_main.exists(), "原批次主合同未被删除")
        assert_eq(orig_zip.exists(), "原批次 zip 未被删除")

        rb = storage.get_batch(batch.id)
        assert_eq(rb.status != BATCH_STATUS["ROLLED_BACK"],
                  f"原批次状态不是 rolled_back: {rb.status}")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main():
    global TESTS_PASS, TESTS_FAIL
    try:
        test_rebuild_config_normal()
        test_rebuild_config_missing_packages()
        test_rebuild_config_package_missing_output_dir()
        test_rebuild_config_missing_manifest_path()
        test_rebuild_config_output_root_rewrites_paths()
        test_rebuild_config_replace_manifest_and_source_root()
        test_rebuild_manifest_from_copy_actions()
        test_rerun_normal_success()
        test_rerun_only_completed_or_partial()
        test_rerun_missing_parent()
        test_rerun_target_exists_blocked_by_default()
        test_rerun_force_bypasses_precheck()
        test_rerun_partial_then_rerun_again()
        test_rerun_persistence_across_reopen()
        test_rerun_export_json_has_parent_and_params()
        test_rerun_export_csv_has_parent_and_params()
        test_rerun_rollback_only_rerun_artifacts()
    except AssertionError:
        pass

    print(f"\n=== 重跑单元测试: 通过 {TESTS_PASS}, 失败 {TESTS_FAIL} ===")
    import sys
    sys.exit(1 if TESTS_FAIL else 0)


if __name__ == "__main__":
    main()
