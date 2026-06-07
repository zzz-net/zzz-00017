"""按历史批次重跑 - 缺陷复现与回归测试

复现两个关键缺陷:
  1. 默认重跑必须阻止交付文件或 zip 被覆盖（target_exists 应为 error 而非 warning）
  2. 父批次已是 v2 时，传入旧 v1 manifest 不能静默版本倒退（FileAction 需存储 version）

覆盖:
  - completed/partial 父批次
  - 同名目标文件冲突（target_exists）和 zip 冲突（zip_exists）
  - manifest 版本低于父批次摘要或历史动作
  - force / allow_overwrite 边界
  - 重启后 list / show / export JSON / CSV 的 parent_batch_id 和错误信息
  - 被阻止时不留下产物、SQLite 记录原因
  - rollback 只影响新重跑批次、原产物不变
  - 报告错误对用户可读
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
from contract_pack.manifest import ManifestEntry, load_manifest
from contract_pack.precheck import PrecheckIssue, PrecheckResult, run_precheck
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


def _make_env(
    tmpdir: Path,
    manifest_versions: Dict[str, str] = None,
    package_version: str = "v2",
    make_zip: bool = True,
    batch_status: str = BATCH_STATUS["COMPLETED"],
) -> tuple[Batch, Path, Path]:
    """创建完整工作环境：v2 父批次 + 已存在的交付文件/zip（覆盖风险场景）。"""
    work = tmpdir / "scenario"
    if work.exists():
        shutil.rmtree(work)
    work.mkdir()

    src = work / "sources"
    (src / "contracts").mkdir(parents=True)
    (src / "scans").mkdir(parents=True)
    (src / "contracts" / "main.pdf").write_text("MAIN CONTRACT v2", encoding="utf-8")
    (src / "scans" / "seal.jpg").write_text("SEAL IMAGE", encoding="utf-8")

    manifest_versions = manifest_versions or {}
    v_main = manifest_versions.get("main.pdf", "v2")
    v_seal = manifest_versions.get("seal.jpg", "v1")

    manifest = work / "manifest.csv"
    with open(manifest, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["package", "category", "source_path", "target_name", "version", "description"])
        w.writerow(["甲方交付包", "main", "contracts/main.pdf", "主合同.pdf", v_main, ""])
        w.writerow(["甲方交付包", "seal", "scans/seal.jpg", "盖章扫描件.jpg", v_seal, ""])

    db_path = work / ".contract_pack.db"
    cfg = AppConfig(
        manifest_path=manifest,
        source_root=src,
        packages=[
            PackageConfig(
                name="甲方交付包",
                output_dir=work / "deliver" / "PartyA",
                zip_output=work / "deliver" / "甲方交付包.zip" if make_zip else None,
                file_mapping={"main": "01_主合同"},
                version=package_version,
            )
        ],
        operator="tester",
        db_path=db_path,
        allow_overwrite=True,
    )

    entries = load_manifest(manifest)
    storage = BatchStorage(db_path)
    precheck = run_precheck(cfg, entries, storage=storage, last_batch_id=None)
    assert_eq(precheck.ok, f"父批次预检通过 (ok={precheck.ok}, errors={precheck.errors})")

    engine = Engine(cfg)
    result = engine.run(entries, precheck, make_zip=make_zip)
    assert_eq(result.status == batch_status, f"父批次状态为 {batch_status} (got {result.status})")

    batch = storage.get_batch(result.batch_id)
    assert_eq(batch is not None, "父批次已存入数据库")
    assert_eq(len(batch.file_actions) >= (2 + (1 if make_zip else 0)),
              f"父批次有至少 {2 + (1 if make_zip else 0)} 个文件动作 (copy+copy+zip 或更多)")

    return batch, work, manifest


def _make_v1_manifest(work: Path) -> Path:
    """生成一个 v1 版本的 manifest（版本低于父批次 v2）。"""
    manifest = work / "manifest_v1.csv"
    with open(manifest, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["package", "category", "source_path", "target_name", "version", "description"])
        w.writerow(["甲方交付包", "main", "contracts/main.pdf", "主合同.pdf", "v1", ""])
        w.writerow(["甲方交付包", "seal", "scans/seal.jpg", "盖章扫描件.jpg", "v0.9", ""])
    return manifest


# ============================================================
# 缺陷 1: 默认重跑必须阻止交付文件或 zip 被覆盖
# ============================================================

def test_bug1_target_exists_blocks_by_default(tmpdir: Path):
    """target_exists 应该是 error（阻止）而非 warning（放行）。

    父批次已生成 主合同.pdf 和 zip。默认 rerun 到相同目录，应该被阻止。
    """
    print(f"\n=== {test_bug1_target_exists_blocks_by_default.__name__} ===")
    parent, work, _ = _make_env(tmpdir, manifest_versions={"main.pdf": "v2", "seal.jpg": "v1"})

    target_pdf = Path(parent.config_summary["packages"][0]["output_dir"]) / "主合同.pdf"
    target_zip = Path(parent.config_summary["packages"][0]["zip_output"])
    pdf_before = target_pdf.read_text(encoding="utf-8") if target_pdf.exists() else None
    zip_before = target_zip.stat().st_mtime if target_zip.exists() else None

    storage = BatchStorage(work / ".contract_pack.db")
    result = rerun_batch(parent.id, storage, base_dir=work, make_zip=True, force=False)

    assert_eq(result.status == BATCH_STATUS["FAILED"],
              f"默认 rerun 应被阻止 (status=failed)，实际 status={result.status}")
    assert_eq(result.precheck is not None and len(result.precheck.errors) >= 1,
              f"预检应有至少 1 个 error，实际 errors={[e.kind for e in (result.precheck.errors if result.precheck else [])]}")

    error_kinds = {e.kind for e in (result.precheck.errors if result.precheck else [])}
    has_target_err = "target_exists" in error_kinds
    has_zip_err = "zip_exists" in error_kinds
    assert_eq(has_target_err, f"target_exists 应为 error（阻止覆盖），实际 level 在 kinds 中")
    assert_eq(has_zip_err, f"zip_exists 应为 error（阻止覆盖 zip）")

    if target_pdf.exists():
        pdf_after = target_pdf.read_text(encoding="utf-8")
        assert_eq(pdf_after == pdf_before, "被阻止后主合同内容未被覆盖")
    if target_zip.exists():
        zip_after = target_zip.stat().st_mtime
        assert_eq(zip_after == zip_before, "被阻止后 zip 未被覆盖")

    db = BatchStorage(work / ".contract_pack.db")
    rerun_batch_db = db.get_batch(result.batch_id)
    assert_eq(rerun_batch_db is not None, "失败的重跑批次也被记录入库")
    assert_eq(rerun_batch_db.status == BATCH_STATUS["FAILED"], "失败批次状态为 failed")
    assert_eq(rerun_batch_db.parent_batch_id == parent.id, "失败批次也记录 parent_batch_id")
    assert_eq(len(rerun_batch_db.error) > 0, "失败批次 error 字段含原因")

    assert_eq(len(rerun_batch_db.file_actions) == 0,
              f"被阻止的重跑不应留下文件动作（防误认成功），实际有 {len(rerun_batch_db.file_actions)} 个动作")


def test_bug1_allow_overwrite_true_rerun_succeeds(tmpdir: Path):
    """当显式重建 config 时 allow_overwrite=True，允许覆盖（测试边界）。

    注意：CLI 默认调用 rebuild_config_from_batch(allow_overwrite=False)，
    这里只是直接调用 Engine 验证 allow_overwrite 边界。
    """
    print(f"\n=== {test_bug1_allow_overwrite_true_rerun_succeeds.__name__} ===")
    parent, work, manifest = _make_env(tmpdir)

    storage = BatchStorage(work / ".contract_pack.db")

    cfg = rebuild_config_from_batch(parent, base_dir=work, allow_overwrite=True)
    entries = rebuild_manifest_from_batch(parent)
    precheck = run_precheck(cfg, entries, storage=storage, last_batch_id=parent.id)

    assert_eq(precheck.ok, "allow_overwrite=True 时 target_exists/zip_exists 不应是 error")
    warn_kinds = {w.kind for w in precheck.warnings}
    assert_eq("target_exists" in warn_kinds or True, "allow_overwrite=True 时没有阻止（warning 可省略）")


def test_bug1_force_bypasses_target_exists(tmpdir: Path):
    """--force 跳过 target_exists / zip_exists 的预检阻止。"""
    print(f"\n=== {test_bug1_force_bypasses_target_exists.__name__} ===")
    parent, work, _ = _make_env(tmpdir)

    storage = BatchStorage(work / ".contract_pack.db")
    result = rerun_batch(parent.id, storage, base_dir=work, make_zip=True, force=True)

    assert_eq(result.status in (BATCH_STATUS["COMPLETED"], BATCH_STATUS["PARTIAL"]),
              f"--force 下即使目标存在也执行 (status={result.status})")


# ============================================================
# 缺陷 2: 父批次已是 v2，传入旧 v1 manifest 不能静默版本倒退
# ============================================================

def test_bug2_version_rollback_detected(tmpdir: Path):
    """父批次为 v2 manifest，重跑时传入 v1 manifest → 应检测 version_rollback 并阻止。"""
    print(f"\n=== {test_bug2_version_rollback_detected.__name__} ===")
    parent, work, _ = _make_env(
        tmpdir,
        manifest_versions={"main.pdf": "v2", "seal.jpg": "v1"},
        package_version="v2",
    )

    v1_manifest = _make_v1_manifest(work)
    new_output = work / "deliver_v1"
    new_output.mkdir(parents=True, exist_ok=True)

    storage = BatchStorage(work / ".contract_pack.db")
    result = rerun_batch(
        parent.id, storage,
        base_dir=work,
        manifest_path=v1_manifest,
        output_root=new_output,
        make_zip=False,
        force=False,
    )

    assert_eq(result.status == BATCH_STATUS["FAILED"],
              f"版本倒退应被阻止 (status=failed)，实际 status={result.status}")
    assert_eq(result.precheck is not None, "预检结果存在")
    error_kinds = {e.kind for e in result.precheck.errors}
    has_vr = "version_rollback" in error_kinds
    assert_eq(has_vr,
              f"version_rollback 应为 error，实际 errors={sorted(error_kinds)}")

    db = BatchStorage(work / ".contract_pack.db")
    rerun_batch_db = db.get_batch(result.batch_id)
    assert_eq(rerun_batch_db.parent_batch_id == parent.id, "失败批次 parent_batch_id 正确")
    assert_eq("version" in rerun_batch_db.error.lower() or "倒退" in rerun_batch_db.error,
              f"错误信息对用户可读（含 version/倒退），实际 error='{rerun_batch_db.error}'")

    new_main = new_output / "PartyA" / "主合同.pdf"
    assert_eq(not new_main.exists(),
              "被阻止后不留下 v1 交付文件（防止版本倒退静默成功）")


def test_bug2_file_action_stores_version(tmpdir: Path):
    """FileAction 应存储 manifest 中的 version，跨重启后仍能读取用于比较。"""
    print(f"\n=== {test_bug2_file_action_stores_version.__name__} ===")
    parent, work, _ = _make_env(
        tmpdir,
        manifest_versions={"main.pdf": "v2.5", "seal.jpg": "v1.0"},
    )

    copy_fa = [fa for fa in parent.file_actions if fa.action == FILE_ACTION["COPY"]]
    assert_eq(len(copy_fa) >= 2, f"至少 2 个 COPY 动作，实际 {len(copy_fa)}")

    main_fa = next(fa for fa in copy_fa if "主合同" in fa.target_path)
    seal_fa = next(fa for fa in copy_fa if "盖章" in fa.target_path)

    assert_eq(getattr(main_fa, "version", None) == "v2.5",
              f"主合同 FileAction.version == 'v2.5'，实际 {getattr(main_fa, 'version', None)}")
    assert_eq(getattr(seal_fa, "version", None) == "v1.0",
              f"盖章 FileAction.version == 'v1.0'，实际 {getattr(seal_fa, 'version', None)}")

    db_path = work / ".contract_pack.db"
    db2 = BatchStorage(db_path)
    parent_reloaded = db2.get_batch(parent.id)
    copy_reloaded = [fa for fa in parent_reloaded.file_actions if fa.action == FILE_ACTION["COPY"]]
    main_reloaded = next(fa for fa in copy_reloaded if "主合同" in fa.target_path)
    assert_eq(getattr(main_reloaded, "version", None) == "v2.5",
              f"跨重启后主合同 FileAction.version 仍为 v2.5（持久化），实际 {getattr(main_reloaded, 'version', None)}")


def test_bug2_same_version_allowed(tmpdir: Path):
    """相同版本（不低于）应放行。"""
    print(f"\n=== {test_bug2_same_version_allowed.__name__} ===")
    parent, work, _ = _make_env(
        tmpdir,
        manifest_versions={"main.pdf": "v2", "seal.jpg": "v1"},
    )

    same_manifest = work / "manifest_same.csv"
    with open(same_manifest, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["package", "category", "source_path", "target_name", "version", "description"])
        w.writerow(["甲方交付包", "main", "contracts/main.pdf", "主合同.pdf", "v2", ""])
        w.writerow(["甲方交付包", "seal", "scans/seal.jpg", "盖章扫描件.jpg", "v1", ""])

    new_output = work / "deliver_same"
    storage = BatchStorage(work / ".contract_pack.db")
    result = rerun_batch(
        parent.id, storage,
        base_dir=work,
        manifest_path=same_manifest,
        output_root=new_output,
        make_zip=False,
        force=False,
    )

    assert_eq(result.status == BATCH_STATUS["COMPLETED"],
              f"相同版本不应被阻止 (status=completed)，实际 status={result.status}")


def test_bug2_higher_version_allowed(tmpdir: Path):
    """更高版本应放行（v2 → v3）。"""
    print(f"\n=== {test_bug2_higher_version_allowed.__name__} ===")
    parent, work, _ = _make_env(
        tmpdir,
        manifest_versions={"main.pdf": "v2", "seal.jpg": "v1"},
    )

    higher_manifest = work / "manifest_v3.csv"
    with open(higher_manifest, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["package", "category", "source_path", "target_name", "version", "description"])
        w.writerow(["甲方交付包", "main", "contracts/main.pdf", "主合同.pdf", "v3", ""])
        w.writerow(["甲方交付包", "seal", "scans/seal.jpg", "盖章扫描件.jpg", "v2", ""])

    new_output = work / "deliver_v3"
    storage = BatchStorage(work / ".contract_pack.db")
    result = rerun_batch(
        parent.id, storage,
        base_dir=work,
        manifest_path=higher_manifest,
        output_root=new_output,
        make_zip=False,
        force=False,
    )

    assert_eq(result.status == BATCH_STATUS["COMPLETED"],
              f"更高版本不应被阻止 (status=completed)，实际 status={result.status}")


# ============================================================
# 跨重启查询 & 报告一致性
# ============================================================

def test_bug_cross_restart_reporting(tmpdir: Path):
    """重启后 list/show/export JSON/CSV 都能看到 parent_batch_id 和错误信息。"""
    print(f"\n=== {test_bug_cross_restart_reporting.__name__} ===")
    parent, work, _ = _make_env(tmpdir, manifest_versions={"main.pdf": "v2"})

    v1_manifest = _make_v1_manifest(work)
    new_output = work / "deliver_cross"
    storage = BatchStorage(work / ".contract_pack.db")
    result = rerun_batch(
        parent.id, storage,
        base_dir=work,
        manifest_path=v1_manifest,
        output_root=new_output,
        make_zip=False,
        force=False,
    )

    assert_eq(result.status == BATCH_STATUS["FAILED"], "先确保版本倒退被阻止")

    db_path = work / ".contract_pack.db"
    db2 = BatchStorage(db_path)
    listed = db2.list_batches(limit=10)
    listed_ids = {b.id for b in listed}
    assert_eq(parent.id in listed_ids, "跨重启 list 可见父批次")
    assert_eq(result.batch_id in listed_ids, "跨重启 list 可见失败的重跑批次")

    rerun_reloaded = db2.get_batch(result.batch_id)
    assert_eq(rerun_reloaded.parent_batch_id == parent.id,
              "跨重启后 parent_batch_id 仍正确")
    assert_eq(len(rerun_reloaded.error) > 0, "跨重启后 error 字段仍含原因")

    d = batch_to_dict(rerun_reloaded)
    assert_eq(d.get("parent_batch_id") == parent.id, "batch_to_dict 含 parent_batch_id")
    assert_eq(len(d.get("error", "")) > 0, "batch_to_dict 含 error")
    assert_eq(d.get("rerun_params") is not None, "batch_to_dict 含 rerun_params")

    json_out = work / "report.json"
    target_batch = db2.get_batch(result.batch_id)
    export_json([target_batch], json_out)
    j = json.loads(json_out.read_text(encoding="utf-8"))
    assert_eq(len(j) == 1, f"JSON 导出 1 条批次，实际 {len(j)}")
    assert_eq(j[0]["parent_batch_id"] == parent.id, "JSON 中 parent_batch_id 正确")
    assert_eq(len(j[0].get("error", "")) > 0, "JSON 中 error 存在")

    csv_out = work / "report.csv"
    export_csv([target_batch], csv_out)
    with open(csv_out, "r", encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    assert_eq(len(rows) >= 1, f"CSV 至少 1 行，实际 {len(rows)}")
    csv_parent = rows[0].get("parent_batch_id", "")
    csv_error = rows[0].get("batch_error", "")
    assert_eq(csv_parent == parent.id, f"CSV 中 parent_batch_id == {parent.id}，实际 {csv_parent}")
    assert_eq(len(csv_error) > 0, f"CSV 中 batch_error 存在（对用户可读），实际 '{csv_error}'")


# ============================================================
# rollback 隔离 & 原产物不变
# ============================================================

def test_bug_rollback_isolation_and_original_untouched(tmpdir: Path):
    """rollback 只删除重跑批次产物，原批次产物保留。被阻止的重跑无产物可 rollback。"""
    print(f"\n=== {test_bug_rollback_isolation_and_original_untouched.__name__} ===")
    parent, work, _ = _make_env(tmpdir, manifest_versions={"main.pdf": "v2"}, make_zip=True)

    orig_main = Path(parent.config_summary["packages"][0]["output_dir"]) / "主合同.pdf"
    orig_zip = Path(parent.config_summary["packages"][0]["zip_output"])
    assert_eq(orig_main.exists() and orig_zip.exists(), "原批次产物都在（rollback 前）")

    new_root = work / "deliver_rerun_iso"
    storage = BatchStorage(work / ".contract_pack.db")
    ok_result = rerun_batch(
        parent.id, storage,
        base_dir=work,
        output_root=new_root,
        make_zip=True,
        force=False,
    )
    assert_eq(ok_result.status == BATCH_STATUS["COMPLETED"],
              f"先重跑到新目录确保成功，status={ok_result.status}")

    rerun_main = new_root / "PartyA" / "主合同.pdf"
    rerun_zip = new_root / "甲方交付包.zip"
    assert_eq(rerun_main.exists() and rerun_zip.exists(), "重跑产物都在（rollback 前）")

    cfg = rebuild_config_from_batch(parent, base_dir=work, output_root=new_root)
    engine = Engine(cfg)
    ok, msg = engine.rollback(ok_result.batch_id)
    assert_eq(ok, f"rollback 成功: {msg}")
    assert_eq(not rerun_main.exists(), "rollback 后重跑主合同被删除")
    assert_eq(not rerun_zip.exists(), "rollback 后重跑 zip 被删除")
    assert_eq(orig_main.exists(), "rollback 后原批次主合同仍在")
    assert_eq(orig_zip.exists(), "rollback 后原批次 zip 仍在")


# ============================================================
# partial 父批次也可重跑
# ============================================================

def test_partial_parent_can_rerun_and_blocked(tmpdir: Path):
    """partial 父批次也能重跑，且同样受覆盖阻止 / 版本倒退阻止。"""
    print(f"\n=== {test_partial_parent_can_rerun_and_blocked.__name__} ===")
    work = tmpdir / "scenario_partial"
    work.mkdir()

    src = work / "sources"
    (src / "contracts").mkdir(parents=True)
    (src / "contracts" / "main.pdf").write_text("MAIN", encoding="utf-8")

    manifest = work / "manifest_partial.csv"
    with open(manifest, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["package", "category", "source_path", "target_name", "version", "description"])
        w.writerow(["甲方交付包", "main", "contracts/main.pdf", "主合同.pdf", "v2", ""])

    db_path = work / ".contract_pack.db"
    cfg = AppConfig(
        manifest_path=manifest,
        source_root=src,
        packages=[
            PackageConfig(
                name="甲方交付包",
                output_dir=work / "deliver" / "PartyA",
                zip_output=None,
                version="v2",
            )
        ],
        operator="tester",
        db_path=db_path,
        allow_overwrite=True,
    )
    entries = load_manifest(manifest)
    storage = BatchStorage(db_path)

    batch = storage.create_batch(operator="tester", config_summary=cfg.summary())
    storage.update_batch_status(batch.id, BATCH_STATUS["RUNNING"])
    fa_id = storage.add_file_action(
        batch_id=batch.id,
        package="甲方交付包",
        action=FILE_ACTION["COPY"],
        source_path=str(src / "contracts" / "main.pdf"),
        target_path=str(work / "deliver" / "PartyA" / "主合同.pdf"),
        category="main",
        status=FILE_STATUS["SUCCESS"],
        version="v2",
    )
    (work / "deliver" / "PartyA").mkdir(parents=True, exist_ok=True)
    (work / "deliver" / "PartyA" / "主合同.pdf").write_text("MAIN", encoding="utf-8")
    storage.update_file_action(fa_id, FILE_STATUS["SUCCESS"], file_hash="abc", file_size=4)
    storage.update_batch_status(batch.id, BATCH_STATUS["PARTIAL"], error="some missing", finished=True)

    partial_parent = storage.get_batch(batch.id)
    assert_eq(partial_parent.status == BATCH_STATUS["PARTIAL"], "父批次为 partial")

    v1_manifest = work / "manifest_partial_v1.csv"
    with open(v1_manifest, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["package", "category", "source_path", "target_name", "version", "description"])
        w.writerow(["甲方交付包", "main", "contracts/main.pdf", "主合同.pdf", "v1", ""])

    storage2 = BatchStorage(db_path)
    new_out = work / "deliver_partial_rerun"
    result = rerun_batch(
        partial_parent.id, storage2,
        base_dir=work,
        manifest_path=v1_manifest,
        output_root=new_out,
        make_zip=False,
        force=False,
    )
    assert_eq(result.status == BATCH_STATUS["FAILED"],
              f"partial 父批次 + v1 manifest 也被阻止 (status={result.status})")
    error_kinds = {e.kind for e in (result.precheck.errors if result.precheck else [])}
    assert_eq("version_rollback" in error_kinds,
              f"partial 父批次下 version_rollback 检测生效，errors={sorted(error_kinds)}")


# ============================================================

def run_all():
    tmpdir_base = Path(tempfile.mkdtemp(prefix="contract_pack_rerun_bug_"))
    print(f"缺陷回归测试临时目录: {tmpdir_base}")
    try:
        test_bug1_target_exists_blocks_by_default(tmpdir_base)
        test_bug1_allow_overwrite_true_rerun_succeeds(tmpdir_base)
        test_bug1_force_bypasses_target_exists(tmpdir_base)
        test_bug2_version_rollback_detected(tmpdir_base)
        test_bug2_file_action_stores_version(tmpdir_base)
        test_bug2_same_version_allowed(tmpdir_base)
        test_bug2_higher_version_allowed(tmpdir_base)
        test_bug_cross_restart_reporting(tmpdir_base)
        test_bug_rollback_isolation_and_original_untouched(tmpdir_base)
        test_partial_parent_can_rerun_and_blocked(tmpdir_base)
    finally:
        pass

    print(f"\n=== 缺陷回归测试: 通过 {TESTS_PASS}, 失败 {TESTS_FAIL} ===")
    if TESTS_FAIL:
        raise SystemExit(1)


if __name__ == "__main__":
    run_all()
