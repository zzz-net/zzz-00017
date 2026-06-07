"""模板功能单元测试

覆盖：
  - TemplateStorage: save/list/get/delete, 重复名, 跨重启持久化
  - apply_template: 正常流程, 包不匹配, 路径冲突, 清单不存在, 输出已存在
  - 导出 JSON/CSV: 包含模板名、来源配置摘要、创建时间
  - 失败后数据库不被污染
"""

from __future__ import annotations

import csv
import json
import shutil
import sqlite3
import stat
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List

import yaml

from contract_pack.config import PackageConfig
from contract_pack.template import (
    Template,
    TemplateApplyError,
    TemplateNameExistsError,
    TemplateStorage,
    apply_template,
    export_template_csv,
    export_template_json,
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


def _make_packages() -> List[PackageConfig]:
    return [
        PackageConfig(
            name="甲方交付包",
            output_dir=Path("./deliver/PartyA"),
            zip_output=Path("./deliver/甲方交付包.zip"),
            file_mapping={"main": "01_主合同", "seal": "03_盖章扫描件"},
            version="v2024.06",
        ),
        PackageConfig(
            name="乙方交付包",
            output_dir=Path("./deliver/PartyB"),
            zip_output=Path("./deliver/乙方交付包.zip"),
            file_mapping={},
            version="v2024.06",
        ),
    ]


def _make_summary() -> Dict[str, Any]:
    return {
        "manifest_path": "/tmp/manifest.csv",
        "source_root": "/tmp/sources",
        "operator": "tester",
        "allow_overwrite": False,
    }


def _write_manifest(work: Path, package_names: List[str] | None = None) -> Path:
    """在 work 目录下创建 sources 子目录和 manifest.csv。"""
    if package_names is None:
        package_names = ["甲方交付包", "乙方交付包"]
    src = work / "sources"
    (src / "contracts").mkdir(parents=True)
    (src / "scans").mkdir(parents=True)
    (src / "contracts" / "main.pdf").write_text("MAIN", encoding="utf-8")
    (src / "scans" / "seal.jpg").write_text("SEAL", encoding="utf-8")

    manifest = work / "manifest.csv"
    with open(manifest, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["package", "category", "source_path", "target_name", "version", "description"])
        for pn in package_names:
            w.writerow([pn, "main", "contracts/main.pdf", f"{pn}_主合同.pdf", "v1", ""])
            w.writerow([pn, "seal", "scans/seal.jpg", f"{pn}_盖章.jpg", "", ""])
    return manifest


# ---------------------------------------------------------------------------
# Storage tests
# ---------------------------------------------------------------------------

def test_storage_basic_crud():
    """基本 CRUD: save -> list -> get -> delete。"""
    print("\n=== test_storage_basic_crud ===")
    tmpdir = Path(tempfile.mkdtemp(prefix="tpl_unit_"))
    try:
        db = tmpdir / "tpl.db"
        storage = TemplateStorage(db)
        assert_eq(len(storage.list_templates()) == 0, "初始为空")

        tpl = storage.save_template("方案A", _make_packages(), _make_summary())
        assert_eq(tpl.name == "方案A", "保存后模板名正确")
        assert_eq(tpl.created_at is not None and len(tpl.created_at) > 0, "created_at 非空")
        assert_eq(len(tpl.packages) == 2, "包含 2 个包")

        lst = storage.list_templates()
        assert_eq(len(lst) == 1, "list 返回 1 条")
        assert_eq(lst[0].name == "方案A", "list 中的模板名正确")
        assert_eq(lst[0].packages[0].name == "甲方交付包", "包信息被持久化")
        assert_eq(lst[0].source_config_summary["operator"] == "tester", "source_config_summary 被持久化")

        got = storage.get_template("方案A")
        assert_eq(got is not None, "get 存在")
        assert_eq(got.id == tpl.id, "id 匹配")

        missing = storage.get_template("不存在")
        assert_eq(missing is None, "get 不存在返回 None")

        deleted = storage.delete_template("方案A")
        assert_eq(deleted, "delete 返回 True")
        assert_eq(len(storage.list_templates()) == 0, "delete 后 list 为空")

        deleted2 = storage.delete_template("方案A")
        assert_eq(not deleted2, "delete 不存在返回 False")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_storage_duplicate_name():
    """保存重复模板名应抛出 TemplateNameExistsError。"""
    print("\n=== test_storage_duplicate_name ===")
    tmpdir = Path(tempfile.mkdtemp(prefix="tpl_dup_"))
    try:
        storage = TemplateStorage(tmpdir / "tpl.db")
        storage.save_template("方案A", _make_packages(), _make_summary())
        try:
            storage.save_template("方案A", _make_packages(), _make_summary())
            assert_eq(False, "重复名应抛异常")
        except TemplateNameExistsError as e:
            assert_eq("方案A" in str(e), "异常信息包含模板名")
        assert_eq(len(storage.list_templates()) == 1, "重复名失败后数据库仍只有 1 条")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_storage_persistence_across_reopen():
    """跨重启（重新打开 Storage / 重新创建 Storage）仍能读取。"""
    print("\n=== test_storage_persistence_across_reopen ===")
    tmpdir = Path(tempfile.mkdtemp(prefix="tpl_persist_"))
    try:
        db = tmpdir / "tpl.db"
        s1 = TemplateStorage(db)
        s1.save_template("持久化模板", _make_packages(), _make_summary())
        del s1
        time.sleep(0.05)

        s2 = TemplateStorage(db)
        lst = s2.list_templates()
        assert_eq(len(lst) == 1, "重新打开后能读取 1 条")
        assert_eq(lst[0].name == "持久化模板", "模板名一致")
        assert_eq(len(lst[0].packages) == 2, "包数量正确")
        assert_eq(lst[0].packages[0].file_mapping.get("main") == "01_主合同",
                  "file_mapping 字段正确持久化")

        # 直接用 sqlite3 打开，验证底层表存在
        conn = sqlite3.connect(str(db))
        try:
            rows = conn.execute("SELECT name, created_at FROM templates").fetchall()
            assert_eq(len(rows) == 1, "底层 SQLite 表存在且有数据")
        finally:
            conn.close()
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_storage_empty_name_rejected():
    """空模板名应被拒绝。"""
    print("\n=== test_storage_empty_name_rejected ===")
    tmpdir = Path(tempfile.mkdtemp(prefix="tpl_empty_"))
    try:
        storage = TemplateStorage(tmpdir / "tpl.db")
        try:
            storage.save_template("", _make_packages(), _make_summary())
            assert_eq(False, "空名应抛 ValueError")
        except ValueError:
            assert_eq(True, "空名抛出 ValueError")
        try:
            storage.save_template("   ", _make_packages(), _make_summary())
            assert_eq(False, "全空格名应抛 ValueError")
        except ValueError:
            assert_eq(True, "全空格名抛出 ValueError")
        assert_eq(len(storage.list_templates()) == 0, "拒绝后数据库仍为空")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# apply_template tests
# ---------------------------------------------------------------------------

def _setup_apply_scenario(tmpdir: Path) -> tuple[Template, Path, Path]:
    work = tmpdir / "scenario"
    work.mkdir()
    manifest = _write_manifest(work)
    storage = TemplateStorage(work / ".tpl.db")
    tpl = storage.save_template("方案A", _make_packages(), _make_summary())
    return tpl, manifest, work


def test_apply_normal_success():
    """apply_template 正常流程：生成配置草稿并 dry-run 通过。"""
    print("\n=== test_apply_normal_success ===")
    tmpdir = Path(tempfile.mkdtemp(prefix="tpl_apply_ok_"))
    try:
        tpl, manifest, work = _setup_apply_scenario(tmpdir)
        out_yaml = work / "generated.yaml"
        result_path, precheck = apply_template(
            template=tpl,
            manifest_path=manifest,
            output_config_path=out_yaml,
            source_root=Path("./sources"),
        )
        assert_eq(result_path.exists(), "生成的 yaml 文件存在")
        assert_eq(precheck.ok, "dry-run 通过")

        with open(result_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        assert_eq(data["operator"] != "", "operator 字段存在")
        assert_eq(data["manifest"] == "manifest.csv", "manifest 路径被相对化")
        assert_eq(data["source_root"] == "sources", "source_root 正确")
        assert_eq(len(data["packages"]) == 2, "生成了 2 个包配置")
        pkg_names = [p["name"] for p in data["packages"]]
        assert_eq("甲方交付包" in pkg_names and "乙方交付包" in pkg_names,
                  "两个包都出现在生成的配置中")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_apply_manifest_package_not_in_template():
    """清单中的包在模板中不存在 -> 错误，不生成配置。"""
    print("\n=== test_apply_manifest_package_not_in_template ===")
    tmpdir = Path(tempfile.mkdtemp(prefix="tpl_apply_pkg_err_"))
    try:
        work = tmpdir / "scenario"
        work.mkdir()
        manifest = _write_manifest(work, package_names=["甲方交付包", "神秘第三方包"])
        storage = TemplateStorage(work / ".tpl.db")
        # 模板只定义了甲方和乙方
        tpl = storage.save_template("方案A", _make_packages(), _make_summary())

        out_yaml = work / "generated.yaml"
        try:
            apply_template(tpl, manifest, out_yaml, source_root=Path("./sources"))
            assert_eq(False, "应抛出 TemplateApplyError")
        except TemplateApplyError as e:
            assert_eq(not out_yaml.exists(), "失败时不生成半截配置")
            kinds = {i.kind for i in e.issues}
            assert_eq("manifest_package_not_in_template" in kinds,
                      "错误类型包含 manifest_package_not_in_template")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_apply_template_package_not_in_manifest():
    """模板中的包在清单中不存在 -> 只警告，不报错，配置只包含有清单的包。"""
    print("\n=== test_apply_template_package_not_in_manifest ===")
    tmpdir = Path(tempfile.mkdtemp(prefix="tpl_apply_tpl_warn_"))
    try:
        work = tmpdir / "scenario"
        work.mkdir()
        manifest = _write_manifest(work, package_names=["甲方交付包"])  # 只有甲方
        storage = TemplateStorage(work / ".tpl.db")
        tpl = storage.save_template("方案A", _make_packages(), _make_summary())

        out_yaml = work / "generated.yaml"
        result_path, precheck = apply_template(
            tpl, manifest, out_yaml, source_root=Path("./sources")
        )
        assert_eq(result_path.exists(), "生成 yaml 文件")
        warn_kinds = {i.kind for i in precheck.warnings}
        assert_eq("template_package_not_in_manifest" in warn_kinds,
                  "发出 template_package_not_in_manifest 警告")

        with open(result_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        pkg_names = [p["name"] for p in data["packages"]]
        assert_eq(pkg_names == ["甲方交付包"],
                  "只有在清单中出现的甲方包才写入生成的配置")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_apply_manifest_missing():
    """清单文件不存在 -> 抛出异常，不写文件。"""
    print("\n=== test_apply_manifest_missing ===")
    tmpdir = Path(tempfile.mkdtemp(prefix="tpl_apply_mf_miss_"))
    try:
        work = tmpdir / "scenario"
        work.mkdir()
        storage = TemplateStorage(work / ".tpl.db")
        tpl = storage.save_template("方案A", _make_packages(), _make_summary())
        out_yaml = work / "generated.yaml"
        try:
            apply_template(tpl, work / "nope.csv", out_yaml)
            assert_eq(False, "应抛异常")
        except TemplateApplyError as e:
            assert_eq("不存在" in str(e), "异常信息提示不存在")
            assert_eq(not out_yaml.exists(), "不生成半截配置")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_apply_output_already_exists():
    """输出路径已存在 -> 抛出异常，不覆盖。"""
    print("\n=== test_apply_output_already_exists ===")
    tmpdir = Path(tempfile.mkdtemp(prefix="tpl_apply_out_exist_"))
    try:
        tpl, manifest, work = _setup_apply_scenario(tmpdir)
        out_yaml = work / "generated.yaml"
        out_yaml.write_text("existing content", encoding="utf-8")
        orig_content = out_yaml.read_text(encoding="utf-8")
        try:
            apply_template(tpl, manifest, out_yaml, source_root=Path("./sources"))
            assert_eq(False, "应抛异常")
        except TemplateApplyError as e:
            assert_eq("已存在" in str(e), "异常信息提示已存在")
            assert_eq(out_yaml.read_text(encoding="utf-8") == orig_content,
                      "原有文件未被覆盖")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_apply_output_path_conflict_same_zip():
    """两个包共享同一个 zip 输出路径 -> 错误，不写文件。"""
    print("\n=== test_apply_output_path_conflict_same_zip ===")
    tmpdir = Path(tempfile.mkdtemp(prefix="tpl_apply_conflict_"))
    try:
        work = tmpdir / "scenario"
        work.mkdir()
        manifest = _write_manifest(work)
        storage = TemplateStorage(work / ".tpl.db")
        conflict_pkgs = [
            PackageConfig(
                name="甲方交付包",
                output_dir=Path("./deliver/A"),
                zip_output=Path("./deliver/same.zip"),
            ),
            PackageConfig(
                name="乙方交付包",
                output_dir=Path("./deliver/B"),
                zip_output=Path("./deliver/same.zip"),
            ),
        ]
        tpl = storage.save_template("冲突方案", conflict_pkgs, _make_summary())
        out_yaml = work / "generated.yaml"
        try:
            apply_template(tpl, manifest, out_yaml, source_root=Path("./sources"))
            assert_eq(False, "应抛异常")
        except TemplateApplyError as e:
            assert_eq(not out_yaml.exists(), "失败时不生成配置")
            kinds = {i.kind for i in e.issues}
            assert_eq("zip_path_conflict" in kinds, "错误类型包含 zip_path_conflict")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_apply_readonly_output_dir():
    """输出目录只读 -> 保存失败，不生成半截配置，数据库不被污染。"""
    print("\n=== test_apply_readonly_output_dir ===")
    tmpdir = Path(tempfile.mkdtemp(prefix="tpl_apply_ro_"))
    try:
        tpl, manifest, work = _setup_apply_scenario(tmpdir)
        out_dir = work / "readonly"
        out_dir.mkdir()
        try:
            out_dir.chmod(stat.S_IRUSR | stat.S_IXUSR)
            out_yaml = out_dir / "generated.yaml"
            tpl_count_before = len(TemplateStorage(work / ".tpl.db").list_templates())
            try:
                apply_template(tpl, manifest, out_yaml, source_root=Path("./sources"))
                assert_eq(False, "应抛异常")
            except TemplateApplyError as e:
                assert_eq(not out_yaml.exists(), "只读目录下不生成文件")
                tpl_count_after = len(TemplateStorage(work / ".tpl.db").list_templates())
                assert_eq(tpl_count_before == tpl_count_after,
                          "失败后数据库模板数量不变（不被污染）")
        finally:
            out_dir.chmod(stat.S_IRWXU)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_apply_failure_db_not_polluted():
    """apply_template 失败后，模板表数量保持不变（不插入新行也不删除）。"""
    print("\n=== test_apply_failure_db_not_polluted ===")
    tmpdir = Path(tempfile.mkdtemp(prefix="tpl_apply_dbclean_"))
    try:
        tpl, manifest, work = _setup_apply_scenario(tmpdir)
        db = work / ".tpl.db"
        storage_before = TemplateStorage(db)
        count_before = len(storage_before.list_templates())

        # 制造一个包不匹配错误
        bad_manifest = work / "bad_manifest.csv"
        bad_manifest.write_text(
            "package,category,source_path,target_name\n未知包,main,c/a.pdf,x.pdf\n",
            encoding="utf-8-sig",
        )
        out_yaml = work / "generated.yaml"
        try:
            apply_template(tpl, bad_manifest, out_yaml, source_root=Path("./sources"))
        except TemplateApplyError:
            pass

        storage_after = TemplateStorage(db)
        count_after = len(storage_after.list_templates())
        assert_eq(count_before == count_after,
                  f"失败后模板数不变 ({count_before} -> {count_after})")
        assert_eq(storage_after.get_template("方案A") is not None,
                  "原有模板未被删除或修改")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_apply_skip_dry_run():
    """--skip-dry-run 场景：不做预检，直接写入配置。"""
    print("\n=== test_apply_skip_dry_run ===")
    tmpdir = Path(tempfile.mkdtemp(prefix="tpl_apply_skip_"))
    try:
        work = tmpdir / "scenario"
        work.mkdir()
        # 故意不放 sources 目录 -> dry-run 会失败
        manifest = work / "manifest.csv"
        manifest.write_text(
            "package,category,source_path,target_name\n甲方交付包,main,contracts/missing.pdf,x.pdf\n",
            encoding="utf-8-sig",
        )
        storage = TemplateStorage(work / ".tpl.db")
        tpl = storage.save_template("方案A", _make_packages(), _make_summary())
        out_yaml = work / "generated.yaml"

        # 不跳过：应失败
        try:
            apply_template(tpl, manifest, out_yaml, source_root=Path("./sources"))
            assert_eq(False, "默认 dry-run 下源文件缺失应失败")
        except TemplateApplyError:
            pass
        assert_eq(not out_yaml.exists(), "失败时不生成文件")

        # 跳过 dry-run：应成功写入
        apply_template(
            tpl, manifest, out_yaml, source_root=Path("./sources"), run_dry_run=False
        )
        assert_eq(out_yaml.exists(), "跳过 dry-run 后生成了文件")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Export tests
# ---------------------------------------------------------------------------

def test_export_json_and_csv_fields():
    """导出 JSON 和 CSV 都包含模板名、来源配置摘要、创建时间。"""
    print("\n=== test_export_json_and_csv_fields ===")
    tmpdir = Path(tempfile.mkdtemp(prefix="tpl_export_"))
    try:
        storage = TemplateStorage(tmpdir / "tpl.db")
        tpl = storage.save_template("导出方案", _make_packages(), _make_summary())

        json_path = tmpdir / "out.json"
        export_template_json([tpl], json_path)
        with open(json_path, "r", encoding="utf-8") as f:
            j = json.load(f)
        assert_eq(len(j) == 1, "JSON 包含 1 条记录")
        rec = j[0]
        assert_eq(rec["template_name"] == "导出方案", "JSON 包含 template_name")
        assert_eq("source_config_summary" in rec, "JSON 包含 source_config_summary")
        assert_eq(rec["source_config_summary"]["operator"] == "tester",
                  "来源配置摘要内容正确")
        assert_eq("created_at" in rec and rec["created_at"] == tpl.created_at,
                  "JSON 包含 created_at 且与模板一致")

        csv_path = tmpdir / "out.csv"
        export_template_csv([tpl], csv_path)
        with open(csv_path, "r", encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            rows = list(reader)
        assert_eq(len(rows) == 2, f"CSV 有 2 行（每个包一行），实际 {len(rows)}")
        headers = reader.fieldnames or []
        for h in ("template_name", "created_at", "source_config_summary"):
            assert_eq(h in headers, f"CSV 表头包含 {h}")
        assert_eq(all(r["template_name"] == "导出方案" for r in rows),
                  "所有 CSV 行的 template_name 正确")
        assert_eq(all(r["created_at"] == tpl.created_at for r in rows),
                  "所有 CSV 行的 created_at 正确")
        # 解析第一条的 source_config_summary
        scs = json.loads(rows[0]["source_config_summary"])
        assert_eq(scs["operator"] == "tester", "CSV 中的 source_config_summary 可解析")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main():
    global TESTS_PASS, TESTS_FAIL
    try:
        test_storage_basic_crud()
        test_storage_duplicate_name()
        test_storage_persistence_across_reopen()
        test_storage_empty_name_rejected()
        test_apply_normal_success()
        test_apply_manifest_package_not_in_template()
        test_apply_template_package_not_in_manifest()
        test_apply_manifest_missing()
        test_apply_output_already_exists()
        test_apply_output_path_conflict_same_zip()
        test_apply_readonly_output_dir()
        test_apply_failure_db_not_polluted()
        test_apply_skip_dry_run()
        test_export_json_and_csv_fields()
    except AssertionError:
        pass

    print(f"\n=== 单元测试: 通过 {TESTS_PASS}, 失败 {TESTS_FAIL} ===")
    import sys
    sys.exit(1 if TESTS_FAIL else 0)


if __name__ == "__main__":
    main()
