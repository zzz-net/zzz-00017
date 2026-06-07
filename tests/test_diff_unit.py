"""交付包差异对比 - 单元测试

覆盖：
  - collect_expected_deliverables: 正常、重复目标名、file_mapping 分类子目录
  - collect_from_batch: 正常、批次不存在、批次不完整
  - collect_from_directory: 正常、目录不存在、权限不足、同名文件冲突
  - diff_against_batch: 新增/缺失/重命名/版本变化/包归属变化/zip 状态差异
  - diff_against_directory: 目录基准差异对比
  - export_diff_json / export_diff_csv: 字段稳定性、可解析
  - 跨重启持久化: 批次入库后重新打开仍可对比
"""

from __future__ import annotations

import csv
import json
import shutil
import tempfile
import time
from pathlib import Path
from typing import List

from contract_pack.config import AppConfig, PackageConfig
from contract_pack.diff_core import (
    DIFF_CHANGE_LABELS,
    DeliverableItem,
    DiffChangeType,
    DiffError,
    diff_against_batch,
    diff_against_directory,
    collect_expected_deliverables,
    collect_from_batch,
    collect_from_directory,
)
from contract_pack.engine import Engine
from contract_pack.manifest import ManifestEntry, load_manifest
from contract_pack.precheck import run_precheck
from contract_pack.report import diff_to_dict, export_diff_csv, export_diff_json
from contract_pack.storage import (
    BATCH_STATUS,
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


def _make_base_workdir(tmpdir: Path, name: str) -> Path:
    """创建基础工作目录，含 sources 子目录和示例文件"""
    work = tmpdir / name
    work.mkdir(parents=True, exist_ok=True)
    src = work / "sources"
    (src / "contracts").mkdir(parents=True)
    (src / "scans").mkdir(parents=True)
    (src / "contracts" / "main.pdf").write_text("MAIN CONTRACT", encoding="utf-8")
    (src / "contracts" / "supp.pdf").write_text("SUPPLEMENT", encoding="utf-8")
    (src / "scans" / "seal.jpg").write_text("SEAL IMAGE", encoding="utf-8")
    return work


def _write_manifest(work: Path, rows: List[List]) -> Path:
    """写 manifest.csv"""
    manifest = work / "manifest.csv"
    with open(manifest, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["package", "category", "source_path", "target_name", "version", "description"])
        for row in rows:
            w.writerow(row)
    return manifest


def _make_completed_batch(work: Path, manifest_rows: List[List], with_zip: bool = True) -> tuple[Batch, AppConfig]:
    """执行一次 run，返回 (批次对象, 配置)"""
    manifest = _write_manifest(work, manifest_rows)
    db_path = work / ".contract_pack.db"
    cfg = AppConfig(
        manifest_path=manifest,
        source_root=work / "sources",
        packages=[
            PackageConfig(
                name="甲方交付包",
                output_dir=work / "deliver" / "PartyA",
                zip_output=work / "deliver" / "甲方交付包.zip" if with_zip else None,
                file_mapping={"main": "01_主合同", "supplement": "02_补充协议"},
                version="v2024.06",
            ),
            PackageConfig(
                name="乙方交付包",
                output_dir=work / "deliver" / "PartyB",
                zip_output=None,
                version="v2024.06",
            ),
        ],
        operator="tester",
        db_path=db_path,
        allow_overwrite=True,
    )
    entries = load_manifest(manifest)
    storage = BatchStorage(db_path)
    precheck = run_precheck(cfg, entries, storage=storage, last_batch_id=None)
    engine = Engine(cfg)
    result = engine.run(entries, precheck, make_zip=with_zip)
    batch = storage.get_batch(result.batch_id)
    return batch, cfg


# ---------------------------------------------------------------------------
# collect_expected_deliverables tests
# ---------------------------------------------------------------------------

def test_collect_expected_normal():
    """正常收集预期交付项：文件 + zip"""
    print("\n=== test_collect_expected_normal ===")
    tmpdir = Path(tempfile.mkdtemp(prefix="diff_unit_ce_n_"))
    try:
        work = _make_base_workdir(tmpdir, "scenario")
        manifest = _write_manifest(work, [
            ["甲方交付包", "main", "contracts/main.pdf", "主合同.pdf", "v1", ""],
            ["甲方交付包", "supplement", "contracts/supp.pdf", "补充协议.pdf", "v1", ""],
        ])
        cfg = AppConfig(
            manifest_path=manifest,
            source_root=work / "sources",
            packages=[
                PackageConfig(
                    name="甲方交付包",
                    output_dir=work / "deliver" / "PartyA",
                    zip_output=work / "deliver" / "甲方交付包.zip",
                    file_mapping={"main": "01_主合同"},
                    version="v1",
                )
            ],
            operator="tester",
            db_path=work / ".db",
            allow_overwrite=True,
        )
        entries = load_manifest(manifest)
        items, errors = collect_expected_deliverables(cfg, entries)
        assert_eq(len(items) == 3, f"预期 3 个交付项 (2 文件 + 1 zip)，实际 {len(items)}")
        files = [i for i in items if not i.is_zip]
        zips = [i for i in items if i.is_zip]
        assert_eq(len(files) == 2, f"其中 2 个文件 (got {len(files)})")
        assert_eq(len(zips) == 1, f"其中 1 个 zip (got {len(zips)})")
        target_names = {i.target_name for i in items}
        assert_eq("主合同.pdf" in target_names, "含 主合同.pdf")
        assert_eq("甲方交付包.zip" in target_names, "含 甲方交付包.zip")
        assert_eq(not any(e.level == "error" for e in errors), "无 error 级错误")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_collect_expected_duplicate_target():
    """同一包内目标文件名重复 -> 返回 error"""
    print("\n=== test_collect_expected_duplicate_target ===")
    tmpdir = Path(tempfile.mkdtemp(prefix="diff_unit_ce_dup_"))
    try:
        work = _make_base_workdir(tmpdir, "scenario")
        manifest = _write_manifest(work, [
            ["甲方交付包", "main", "contracts/main.pdf", "主合同.pdf", "v1", ""],
            ["甲方交付包", "supplement", "contracts/supp.pdf", "主合同.pdf", "v1", ""],
        ])
        cfg = AppConfig(
            manifest_path=manifest,
            source_root=work / "sources",
            packages=[PackageConfig(
                name="甲方交付包", output_dir=work / "out",
            )],
            operator="tester", db_path=work / ".db", allow_overwrite=True,
        )
        entries = load_manifest(manifest)
        items, errors = collect_expected_deliverables(cfg, entries)
        has_dup = any(e.kind == "duplicate_target" for e in errors)
        assert_eq(has_dup, "检测到 duplicate_target 错误")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_collect_expected_file_mapping():
    """file_mapping 分类子目录 -> target_path 包含子目录"""
    print("\n=== test_collect_expected_file_mapping ===")
    tmpdir = Path(tempfile.mkdtemp(prefix="diff_unit_ce_fm_"))
    try:
        work = _make_base_workdir(tmpdir, "scenario")
        manifest = _write_manifest(work, [
            ["甲方交付包", "main", "contracts/main.pdf", "主合同.pdf", "v1", ""],
        ])
        cfg = AppConfig(
            manifest_path=manifest,
            source_root=work / "sources",
            packages=[PackageConfig(
                name="甲方交付包",
                output_dir=work / "out",
                file_mapping={"main": "01_主合同"},
            )],
            operator="tester", db_path=work / ".db", allow_overwrite=True,
        )
        entries = load_manifest(manifest)
        items, errors = collect_expected_deliverables(cfg, entries)
        main_item = next(i for i in items if i.target_name == "主合同.pdf")
        assert_eq("01_主合同" in main_item.target_path, f"target_path 含分类子目录: {main_item.target_path}")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# collect_from_batch tests
# ---------------------------------------------------------------------------

def test_collect_from_batch_normal():
    """正常从 completed 批次收集"""
    print("\n=== test_collect_from_batch_normal ===")
    tmpdir = Path(tempfile.mkdtemp(prefix="diff_unit_cb_n_"))
    try:
        work = _make_base_workdir(tmpdir, "scenario")
        batch, cfg = _make_completed_batch(work, [
            ["甲方交付包", "main", "contracts/main.pdf", "主合同.pdf", "v1", ""],
            ["甲方交付包", "supplement", "contracts/supp.pdf", "补充协议.pdf", "v1", ""],
        ])
        storage = BatchStorage(work / ".contract_pack.db")
        items, errors = collect_from_batch(storage, batch.id)
        file_items = [i for i in items if not i.is_zip]
        assert_eq(len(file_items) >= 2, f"至少 2 个文件 (got {len(file_items)})")
        assert_eq(not any(e.level == "error" for e in errors), "无 error 级错误")
        names = {i.target_name for i in items}
        assert_eq("主合同.pdf" in names, "批次中含 主合同.pdf")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_collect_from_batch_not_found():
    """批次不存在 -> 返回 error"""
    print("\n=== test_collect_from_batch_not_found ===")
    tmpdir = Path(tempfile.mkdtemp(prefix="diff_unit_cb_nf_"))
    try:
        work = _make_base_workdir(tmpdir, "scenario")
        storage = BatchStorage(work / ".db")
        items, errors = collect_from_batch(storage, "non-existent-batch-id")
        assert_eq(len(items) == 0, "无交付项")
        has_err = any(e.kind == "batch_not_found" and e.level == "error" for e in errors)
        assert_eq(has_err, "返回 batch_not_found error")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# collect_from_directory tests
# ---------------------------------------------------------------------------

def test_collect_from_directory_normal():
    """正常从目录扫描"""
    print("\n=== test_collect_from_directory_normal ===")
    tmpdir = Path(tempfile.mkdtemp(prefix="diff_unit_cd_n_"))
    try:
        work = _make_base_workdir(tmpdir, "scenario")
        manifest = _write_manifest(work, [
            ["甲方交付包", "main", "contracts/main.pdf", "主合同.pdf", "v1", ""],
        ])
        cfg = AppConfig(
            manifest_path=manifest,
            source_root=work / "sources",
            packages=[PackageConfig(
                name="甲方交付包",
                output_dir=work / "deliver" / "PartyA",
                zip_output=work / "deliver" / "甲方交付包.zip",
                file_mapping={"main": "01_主合同"},
            )],
            operator="tester", db_path=work / ".db", allow_overwrite=True,
        )
        out_dir = work / "deliver" / "PartyA" / "01_主合同"
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "主合同.pdf").write_text("main contract", encoding="utf-8")
        (work / "deliver" / "甲方交付包.zip").write_text("fake zip", encoding="utf-8")

        items, errors = collect_from_directory(cfg, work / "deliver")
        assert_eq(len(items) >= 2, f"至少找到 2 项 (got {len(items)})")
        names = {i.target_name for i in items}
        assert_eq("主合同.pdf" in names, "找到 主合同.pdf")
        assert_eq("甲方交付包.zip" in names, "找到 zip 包")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_collect_from_directory_not_found():
    """目录不存在 -> 返回 error"""
    print("\n=== test_collect_from_directory_not_found ===")
    tmpdir = Path(tempfile.mkdtemp(prefix="diff_unit_cd_nf_"))
    try:
        work = _make_base_workdir(tmpdir, "scenario")
        manifest = _write_manifest(work, [])
        cfg = AppConfig(
            manifest_path=manifest, source_root=work / "sources",
            packages=[], operator="t", db_path=work / ".db",
        )
        items, errors = collect_from_directory(cfg, work / "non_existent_dir")
        assert_eq(len(items) == 0, "无交付项")
        has_err = any(e.kind == "directory_not_found" and e.level == "error" for e in errors)
        assert_eq(has_err, "返回 directory_not_found error")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# diff_against_batch tests - 各种差异类型
# ---------------------------------------------------------------------------

def test_diff_batch_added_and_missing():
    """对比批次：新增 + 缺失"""
    print("\n=== test_diff_batch_added_and_missing ===")
    tmpdir = Path(tempfile.mkdtemp(prefix="diff_unit_db_am_"))
    try:
        work = _make_base_workdir(tmpdir, "scenario")
        batch, cfg = _make_completed_batch(work, [
            ["甲方交付包", "main", "contracts/main.pdf", "主合同.pdf", "v1", ""],
            ["甲方交付包", "supplement", "contracts/supp.pdf", "补充协议.pdf", "v1", ""],
        ])

        new_manifest = _write_manifest(work, [
            ["甲方交付包", "main", "contracts/main.pdf", "主合同.pdf", "v1", ""],
            ["甲方交付包", "seal", "scans/seal.jpg", "盖章扫描件.jpg", "", ""],
        ])
        new_cfg = AppConfig(
            manifest_path=new_manifest,
            source_root=work / "sources",
            packages=cfg.packages,
            operator="tester",
            db_path=work / ".contract_pack.db",
            allow_overwrite=True,
        )
        entries = load_manifest(new_manifest)
        storage = BatchStorage(work / ".contract_pack.db")
        result = diff_against_batch(new_cfg, entries, storage, batch.id)

        assert_eq(len(result.added) >= 1, f"检测到至少 1 个新增 (got {len(result.added)})")
        assert_eq(len(result.missing) >= 1, f"检测到至少 1 个缺失 (got {len(result.missing)})")
        added_names = {i.target_name for i in result.added}
        missing_names = {i.baseline_target_name for i in result.missing}
        assert_eq("盖章扫描件.jpg" in added_names, "新增包含 盖章扫描件.jpg")
        assert_eq("补充协议.pdf" in missing_names, "缺失包含 补充协议.pdf")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_diff_batch_version_changed():
    """对比批次：版本变化"""
    print("\n=== test_diff_batch_version_changed ===")
    tmpdir = Path(tempfile.mkdtemp(prefix="diff_unit_db_vc_"))
    try:
        work = _make_base_workdir(tmpdir, "scenario")
        batch, cfg = _make_completed_batch(work, [
            ["甲方交付包", "main", "contracts/main.pdf", "主合同.pdf", "v1", ""],
        ], with_zip=False)

        new_manifest = _write_manifest(work, [
            ["甲方交付包", "main", "contracts/main.pdf", "主合同.pdf", "v2", ""],
        ])
        new_cfg = AppConfig(
            manifest_path=new_manifest,
            source_root=work / "sources",
            packages=[PackageConfig(
                name="甲方交付包",
                output_dir=work / "deliver" / "PartyA",
                zip_output=None,
            )],
            operator="tester", db_path=work / ".contract_pack.db", allow_overwrite=True,
        )
        entries = load_manifest(new_manifest)
        storage = BatchStorage(work / ".contract_pack.db")
        result = diff_against_batch(new_cfg, entries, storage, batch.id)

        assert_eq(len(result.version_changed) >= 1,
                  f"检测到至少 1 个版本变化 (got {len(result.version_changed)})")
        vc = result.version_changed[0]
        assert_eq(vc.baseline_version == "v1" and vc.version == "v2",
                  f"版本从 v1 变到 v2 (基准={vc.baseline_version}, 当前={vc.version})")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_diff_batch_renamed():
    """对比批次：文件名变化"""
    print("\n=== test_diff_batch_renamed ===")
    tmpdir = Path(tempfile.mkdtemp(prefix="diff_unit_db_rn_"))
    try:
        work = _make_base_workdir(tmpdir, "scenario")
        batch, cfg = _make_completed_batch(work, [
            ["甲方交付包", "main", "contracts/main.pdf", "主合同.pdf", "v1", ""],
        ], with_zip=False)

        new_manifest = _write_manifest(work, [
            ["甲方交付包", "main", "contracts/main.pdf", "主合同_v2.pdf", "v1", ""],
        ])
        new_cfg = AppConfig(
            manifest_path=new_manifest,
            source_root=work / "sources",
            packages=[PackageConfig(
                name="甲方交付包",
                output_dir=work / "deliver" / "PartyA",
                zip_output=None,
            )],
            operator="tester", db_path=work / ".contract_pack.db", allow_overwrite=True,
        )
        entries = load_manifest(new_manifest)
        storage = BatchStorage(work / ".contract_pack.db")
        result = diff_against_batch(new_cfg, entries, storage, batch.id)

        renamed_or_related = result.renamed + result.added + result.missing
        assert_eq(len(renamed_or_related) >= 1, "检测到文件名相关差异")
        target_names = {i.target_name for i in result.items if i.target_name}
        assert_eq("主合同_v2.pdf" in target_names, "当前预期含 主合同_v2.pdf")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_diff_batch_package_changed():
    """对比批次：包归属变化"""
    print("\n=== test_diff_batch_package_changed ===")
    tmpdir = Path(tempfile.mkdtemp(prefix="diff_unit_db_pc_"))
    try:
        work = _make_base_workdir(tmpdir, "scenario")
        (work / "sources" / "contracts").mkdir(parents=True, exist_ok=True)
        (work / "sources" / "contracts" / "shared.pdf").write_text("SHARED", encoding="utf-8")

        manifest1 = _write_manifest(work, [
            ["甲方交付包", "main", "contracts/shared.pdf", "shared.pdf", "v1", ""],
        ])
        db_path = work / ".contract_pack.db"
        cfg1 = AppConfig(
            manifest_path=manifest1,
            source_root=work / "sources",
            packages=[
                PackageConfig(name="甲方交付包", output_dir=work / "deliver" / "PartyA"),
                PackageConfig(name="乙方交付包", output_dir=work / "deliver" / "PartyB"),
            ],
            operator="t", db_path=db_path, allow_overwrite=True,
        )
        entries1 = load_manifest(manifest1)
        storage = BatchStorage(db_path)
        precheck1 = run_precheck(cfg1, entries1, storage=storage)
        engine1 = Engine(cfg1)
        r1 = engine1.run(entries1, precheck1, make_zip=False)
        batch = storage.get_batch(r1.batch_id)

        manifest2 = _write_manifest(work, [
            ["乙方交付包", "main", "contracts/shared.pdf", "shared.pdf", "v1", ""],
        ])
        cfg2 = AppConfig(
            manifest_path=manifest2,
            source_root=work / "sources",
            packages=cfg1.packages,
            operator="t", db_path=db_path, allow_overwrite=True,
        )
        entries2 = load_manifest(manifest2)
        result = diff_against_batch(cfg2, entries2, storage, batch.id)

        assert_eq(len(result.package_changed) + len(result.added) + len(result.missing) >= 1,
                  "检测到包归属变化或相关差异")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_diff_batch_zip_status():
    """对比批次：zip 状态差异"""
    print("\n=== test_diff_batch_zip_status ===")
    tmpdir = Path(tempfile.mkdtemp(prefix="diff_unit_db_zs_"))
    try:
        work = _make_base_workdir(tmpdir, "scenario")
        batch, _ = _make_completed_batch(work, [
            ["甲方交付包", "main", "contracts/main.pdf", "主合同.pdf", "v1", ""],
        ], with_zip=True)

        new_manifest = _write_manifest(work, [
            ["甲方交付包", "main", "contracts/main.pdf", "主合同.pdf", "v1", ""],
        ])
        new_cfg = AppConfig(
            manifest_path=new_manifest,
            source_root=work / "sources",
            packages=[PackageConfig(
                name="甲方交付包",
                output_dir=work / "deliver" / "PartyA",
                zip_output=None,
            )],
            operator="tester", db_path=work / ".contract_pack.db", allow_overwrite=True,
        )
        entries = load_manifest(new_manifest)
        storage = BatchStorage(work / ".contract_pack.db")
        result = diff_against_batch(new_cfg, entries, storage, batch.id)

        zip_related = result.zip_status_changed + result.missing
        assert_eq(len(zip_related) >= 1, "检测到 zip 相关差异（基准有 zip，预期无）")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# diff_against_directory tests
# ---------------------------------------------------------------------------

def test_diff_directory_normal():
    """目录基准：检测新增/缺失"""
    print("\n=== test_diff_directory_normal ===")
    tmpdir = Path(tempfile.mkdtemp(prefix="diff_unit_dd_n_"))
    try:
        work = _make_base_workdir(tmpdir, "scenario")
        manifest = _write_manifest(work, [
            ["甲方交付包", "main", "contracts/main.pdf", "主合同.pdf", "v1", ""],
            ["甲方交付包", "seal", "scans/seal.jpg", "盖章扫描件.jpg", "", ""],
        ])
        cfg = AppConfig(
            manifest_path=manifest,
            source_root=work / "sources",
            packages=[PackageConfig(
                name="甲方交付包",
                output_dir=work / "deliver" / "PartyA",
                zip_output=work / "deliver" / "甲方交付包.zip",
            )],
            operator="tester", db_path=work / ".db", allow_overwrite=True,
        )

        base_dir = work / "baseline"
        (base_dir / "PartyA").mkdir(parents=True)
        (base_dir / "PartyA" / "主合同.pdf").write_text("main", encoding="utf-8")
        (base_dir / "PartyA" / "补充协议.pdf").write_text("supplement", encoding="utf-8")
        (base_dir / "甲方交付包.zip").write_text("zip", encoding="utf-8")

        result = diff_against_directory(cfg, entries=load_manifest(manifest), dir_path=base_dir)
        assert_eq(len(result.added) >= 1, f"检测到新增项（预期有但基准没有，got {len(result.added)}）")
        assert_eq(len(result.missing) >= 1, f"检测到缺失项（基准有但预期没有，got {len(result.missing)}）")
        added_names = {i.target_name for i in result.added}
        missing_names = {i.baseline_target_name for i in result.missing}
        assert_eq("盖章扫描件.jpg" in added_names, f"新增包含 盖章扫描件.jpg (added={added_names})")
        assert_eq("补充协议.pdf" in missing_names, f"缺失包含 补充协议.pdf (missing={missing_names})")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Export tests
# ---------------------------------------------------------------------------

def test_export_diff_json_stable_fields():
    """导出 JSON: 字段稳定、可解析"""
    print("\n=== test_export_diff_json_stable_fields ===")
    tmpdir = Path(tempfile.mkdtemp(prefix="diff_unit_exj_"))
    try:
        work = _make_base_workdir(tmpdir, "scenario")
        batch, cfg = _make_completed_batch(work, [
            ["甲方交付包", "main", "contracts/main.pdf", "主合同.pdf", "v1", ""],
        ], with_zip=False)
        storage = BatchStorage(work / ".contract_pack.db")
        entries = load_manifest(cfg.manifest_path)
        result = diff_against_batch(cfg, entries, storage, batch.id)

        out = work / "diff.json"
        export_diff_json(result, out)
        assert_eq(out.exists(), "JSON 文件已生成")

        with open(out, "r", encoding="utf-8") as f:
            data = json.load(f)

        required_top = {"baseline_kind", "baseline_ref", "generated_at",
                        "total_expected", "total_baseline", "summary", "errors", "items"}
        assert_eq(required_top.issubset(data.keys()),
                  f"JSON 顶层字段完整: {set(data.keys())}")

        required_summary = {"added", "missing", "renamed", "version_changed",
                            "package_changed", "zip_status_changed", "content_changed", "unchanged"}
        assert_eq(required_summary.issubset(data["summary"].keys()),
                  f"JSON summary 字段完整: {set(data['summary'].keys())}")

        if data["items"]:
            required_item = {"change_type", "change_label", "package", "category",
                             "target_name", "baseline_target_name", "version",
                             "baseline_version", "detail"}
            first_item = data["items"][0]
            assert_eq(required_item.issubset(first_item.keys()),
                      f"JSON item 字段完整: {set(first_item.keys())}")

        d = diff_to_dict(result)
        assert_eq(d["baseline_kind"] == "batch", "diff_to_dict baseline_kind=batch")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_export_diff_csv_stable_fields():
    """导出 CSV: 表头稳定、可解析"""
    print("\n=== test_export_diff_csv_stable_fields ===")
    tmpdir = Path(tempfile.mkdtemp(prefix="diff_unit_exc_"))
    try:
        work = _make_base_workdir(tmpdir, "scenario")
        batch, cfg = _make_completed_batch(work, [
            ["甲方交付包", "main", "contracts/main.pdf", "主合同.pdf", "v1", ""],
        ], with_zip=False)
        storage = BatchStorage(work / ".contract_pack.db")
        entries = load_manifest(cfg.manifest_path)
        result = diff_against_batch(cfg, entries, storage, batch.id)

        out = work / "diff.csv"
        export_diff_csv(result, out)
        assert_eq(out.exists(), "CSV 文件已生成")

        with open(out, "r", encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            headers = reader.fieldnames or []
            rows = list(reader)

        required_headers = {"baseline_kind", "baseline_ref", "change_type", "change_label",
                            "package", "target_name", "detail"}
        assert_eq(required_headers.issubset(set(headers)),
                  f"CSV 表头完整: {headers}")
        assert_eq(all(r["baseline_kind"] == "batch" for r in rows),
                  "所有行 baseline_kind=batch")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Cross-restart persistence
# ---------------------------------------------------------------------------

def test_diff_batch_persistence_across_reopen():
    """跨重启: 批次入库后重新打开 storage 仍可对比"""
    print("\n=== test_diff_batch_persistence_across_reopen ===")
    tmpdir = Path(tempfile.mkdtemp(prefix="diff_unit_persist_"))
    try:
        work = _make_base_workdir(tmpdir, "scenario")
        batch, cfg = _make_completed_batch(work, [
            ["甲方交付包", "main", "contracts/main.pdf", "主合同.pdf", "v1", ""],
        ], with_zip=False)
        batch_id = batch.id
        db_path = work / ".contract_pack.db"

        del batch
        del cfg
        time.sleep(0.05)

        storage2 = BatchStorage(db_path)
        b2 = storage2.get_batch(batch_id)
        assert_eq(b2 is not None, "重新打开 DB 后批次仍存在")

        new_manifest = _write_manifest(work, [
            ["甲方交付包", "main", "contracts/main.pdf", "主合同.pdf", "v2", ""],
            ["甲方交付包", "seal", "scans/seal.jpg", "盖章.jpg", "", ""],
        ])
        new_cfg = AppConfig(
            manifest_path=new_manifest,
            source_root=work / "sources",
            packages=[PackageConfig(name="甲方交付包", output_dir=work / "deliver" / "PartyA")],
            operator="tester", db_path=db_path, allow_overwrite=True,
        )
        entries = load_manifest(new_manifest)
        result = diff_against_batch(new_cfg, entries, storage2, batch_id)

        assert_eq(result.baseline_kind == "batch", "baseline_kind=batch")
        assert_eq(result.baseline_ref == batch_id, "baseline_ref 正确")
        assert_eq(len(result.items) > 0, "存在差异条目")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main():
    global TESTS_PASS, TESTS_FAIL
    try:
        test_collect_expected_normal()
        test_collect_expected_duplicate_target()
        test_collect_expected_file_mapping()
        test_collect_from_batch_normal()
        test_collect_from_batch_not_found()
        test_collect_from_directory_normal()
        test_collect_from_directory_not_found()
        test_diff_batch_added_and_missing()
        test_diff_batch_version_changed()
        test_diff_batch_renamed()
        test_diff_batch_package_changed()
        test_diff_batch_zip_status()
        test_diff_directory_normal()
        test_export_diff_json_stable_fields()
        test_export_diff_csv_stable_fields()
        test_diff_batch_persistence_across_reopen()
    except AssertionError:
        pass

    print(f"\n=== 差异对比单元测试: 通过 {TESTS_PASS}, 失败 {TESTS_FAIL} ===")
    import sys
    sys.exit(1 if TESTS_FAIL else 0)


if __name__ == "__main__":
    main()
