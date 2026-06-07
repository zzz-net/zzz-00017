"""审计模块 - 单元测试

覆盖：
  - AuditStorage 基本 CRUD: start_record + finish_record、record_operation
  - 查询过滤组合：时间范围、操作者、命令类型、多命令类型、批次 id、包名、结果状态、limit
  - 同操作一分钟内去重：同参数再次写入抛 AuditDuplicateError
  - JSON/CSV 导出：字段稳定、文件可解析、导出路径已存在/父目录不可写时报错
  - 旧数据兼容：缺少 dedupe_key/warning_count 列的旧表自动迁移
  - 配置验证：AuditConfig.from_dict 各种错误场景
  - 审计关闭后写入不报错
  - cleanup_old_records: 清理过期记录
  - 失败操作也能留流水
"""

from __future__ import annotations

import csv
import json
import os
import shutil
import sqlite3
import tempfile
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List

from contract_pack.audit import (
    AUDIT_COMMAND_TYPES,
    AUDIT_EXPORT_FIELDNAMES,
    AUDIT_RESULT_STATUS,
    AuditConfigError,
    AuditDatabaseError,
    AuditDuplicateError,
    AuditError,
    AuditExportError,
    AuditRecord,
    AuditStorage,
    export_audit_csv,
    export_audit_json,
)
from contract_pack.cli import _get_audit_storage, _try_audit_record
from contract_pack.config import AppConfig, AuditConfig, PackageConfig


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


# ---------------------------------------------------------------------------
# 1. AuditStorage 基本 CRUD
# ---------------------------------------------------------------------------

def test_audit_crud_start_finish():
    """start_record + finish_record 完整流程"""
    print("\n=== test_audit_crud_start_finish ===")
    tmpdir = Path(tempfile.mkdtemp(prefix="audit_unit_crud_sf_"))
    try:
        db_path = tmpdir / "audit.db"
        storage = AuditStorage(db_path)
        assert_eq(db_path.exists(), "审计数据库文件已创建")

        rec = storage.start_record(
            command_type=AUDIT_COMMAND_TYPES["RUN"],
            operator="tester",
            params_summary={"zip": True},
            config_summary={"operator": "tester"},
            package_names=["甲方交付包"],
        )
        assert_eq(isinstance(rec.id, str) and len(rec.id) > 0, f"start_record 返回有效 id: {rec.id[:8]}...")
        assert_eq(rec.command_type == AUDIT_COMMAND_TYPES["RUN"], "command_type 正确")
        assert_eq(rec.operator == "tester", "operator 正确")
        assert_eq(rec.result_status == AUDIT_RESULT_STATUS["FAILED"], "初始 result_status 为 failed")
        assert_eq(rec.package_names == ["甲方交付包"], "package_names 正确")

        got = storage.get_record(rec.id)
        assert_eq(got is not None, "get_record 能查询到刚写入的记录")
        assert_eq(got.id == rec.id, "get_record 返回 id 一致")

        storage.finish_record(
            record_id=rec.id,
            result_status=AUDIT_RESULT_STATUS["SUCCESS"],
            file_count=5,
            error_count=0,
            warning_count=2,
            error_summary="",
            detail_ref={"report": "path/to/report"},
            batch_id="batch-001",
        )
        got2 = storage.get_record(rec.id)
        assert_eq(got2.result_status == AUDIT_RESULT_STATUS["SUCCESS"], "finish 后 result_status=success")
        assert_eq(got2.file_count == 5, f"file_count=5 (got {got2.file_count})")
        assert_eq(got2.warning_count == 2, f"warning_count=2 (got {got2.warning_count})")
        assert_eq(got2.batch_id == "batch-001", "batch_id 已更新")
        assert_eq(got2.finished_at is not None and len(got2.finished_at) > 0, "finished_at 已填充")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_audit_crud_record_operation():
    """record_operation 一次性写入完整记录"""
    print("\n=== test_audit_crud_record_operation ===")
    tmpdir = Path(tempfile.mkdtemp(prefix="audit_unit_crud_ro_"))
    try:
        db_path = tmpdir / "audit.db"
        storage = AuditStorage(db_path)

        rec = storage.record_operation(
            command_type=AUDIT_COMMAND_TYPES["DIFF"],
            operator="alice",
            result_status=AUDIT_RESULT_STATUS["PARTIAL"],
            params_summary={"baseline": "batch-001"},
            config_summary={"packages": 2},
            batch_id="batch-002",
            package_names=["甲方交付包", "乙方交付包"],
            file_count=10,
            error_count=1,
            warning_count=3,
            error_summary="missing a file",
            detail_ref={"diff_id": "abc"},
        )
        assert_eq(rec.command_type == AUDIT_COMMAND_TYPES["DIFF"], "command_type=diff")
        assert_eq(rec.result_status == AUDIT_RESULT_STATUS["PARTIAL"], "result_status=partial")
        assert_eq(rec.started_at == rec.finished_at, "started_at == finished_at (一次性写入)")
        assert_eq(rec.file_count == 10, f"file_count=10 (got {rec.file_count})")
        assert_eq(rec.error_count == 1, f"error_count=1 (got {rec.error_count})")
        assert_eq(rec.package_names == ["甲方交付包", "乙方交付包"], "package_names 正确")

        d = rec.to_dict()
        assert_eq("duration_seconds" in d, "to_dict 含 duration_seconds")
        assert_eq(d["command_type"] == AUDIT_COMMAND_TYPES["DIFF"], "to_dict command_type 正确")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# 2. 查询过滤组合
# ---------------------------------------------------------------------------

def _seed_query_data(storage: AuditStorage) -> Dict[str, Any]:
    """为查询测试写入一批多样化数据"""
    t0 = datetime.now() - timedelta(hours=2)
    t1 = datetime.now() - timedelta(hours=1)
    t2 = datetime.now()

    def _iso(dt: datetime) -> str:
        return dt.isoformat(timespec="seconds")

    rec_a = storage.record_operation(
        command_type=AUDIT_COMMAND_TYPES["RUN"],
        operator="alice",
        result_status=AUDIT_RESULT_STATUS["SUCCESS"],
        batch_id="batch-A",
        package_names=["甲方交付包"],
        file_count=3,
    )
    rec_b = storage.record_operation(
        command_type=AUDIT_COMMAND_TYPES["DRY_RUN"],
        operator="bob",
        result_status=AUDIT_RESULT_STATUS["FAILED"],
        batch_id="batch-B",
        package_names=["乙方交付包"],
        error_count=2,
        error_summary="bad manifest",
    )
    rec_c = storage.record_operation(
        command_type=AUDIT_COMMAND_TYPES["DIFF"],
        operator="alice",
        result_status=AUDIT_RESULT_STATUS["SUCCESS"],
        batch_id="batch-C",
        package_names=["甲方交付包", "乙方交付包"],
    )
    return {"a": rec_a, "b": rec_b, "c": rec_c, "t0": _iso(t0), "t1": _iso(t1), "t2": _iso(t2)}


def test_audit_query_filters():
    """多维度查询过滤组合"""
    print("\n=== test_audit_query_filters ===")
    tmpdir = Path(tempfile.mkdtemp(prefix="audit_unit_qry_"))
    try:
        db_path = tmpdir / "audit.db"
        storage = AuditStorage(db_path)
        seed = _seed_query_data(storage)

        all_recs = storage.query_records()
        assert_eq(len(all_recs) == 3, f"无过滤查询返回 3 条 (got {len(all_recs)})")

        by_op = storage.query_records(operator="alice")
        assert_eq(len(by_op) == 2, f"按 operator=alice 查询返回 2 条 (got {len(by_op)})")
        assert_eq(all(r.operator == "alice" for r in by_op), "结果 operator 均为 alice")

        by_cmd = storage.query_records(command_type=AUDIT_COMMAND_TYPES["DRY_RUN"])
        assert_eq(len(by_cmd) == 1, f"按 command_type=dry-run 查询返回 1 条")
        assert_eq(by_cmd[0].command_type == AUDIT_COMMAND_TYPES["DRY_RUN"], "command_type 匹配")

        by_cmds = storage.query_records(
            command_types=[AUDIT_COMMAND_TYPES["RUN"], AUDIT_COMMAND_TYPES["DIFF"]]
        )
        assert_eq(len(by_cmds) == 2, f"多 command_types 查询返回 2 条 (got {len(by_cmds)})")
        types = {r.command_type for r in by_cmds}
        assert_eq(types == {AUDIT_COMMAND_TYPES["RUN"], AUDIT_COMMAND_TYPES["DIFF"]},
                  f"类型集合正确: {types}")

        by_batch = storage.query_records(batch_id="batch-B")
        assert_eq(len(by_batch) == 1, f"按 batch_id 查询返回 1 条")
        assert_eq(by_batch[0].batch_id == "batch-B", "batch_id 匹配")

        by_pkg = storage.query_records(package_name="乙方交付包")
        assert_eq(len(by_pkg) == 2, f"按 package_name=乙方交付包 返回 2 条 (got {len(by_pkg)})")

        by_status = storage.query_records(result_status=AUDIT_RESULT_STATUS["FAILED"])
        assert_eq(len(by_status) == 1, f"按 result_status=failed 查询返回 1 条")

        by_limit = storage.query_records(limit=1)
        assert_eq(len(by_limit) == 1, f"limit=1 返回 1 条")

        by_time_range = storage.query_records(
            start_time=seed["t0"], end_time=seed["t2"]
        )
        assert_eq(len(by_time_range) >= 1, "时间范围过滤返回至少 1 条")

        combined = storage.query_records(
            operator="alice",
            result_status=AUDIT_RESULT_STATUS["SUCCESS"],
            package_name="甲方交付包",
        )
        assert_eq(len(combined) >= 1, "多条件组合过滤有结果")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# 3. 同操作一分钟内去重
# ---------------------------------------------------------------------------

def test_audit_dedupe_within_minute():
    """同参数再次写入抛 AuditDuplicateError"""
    print("\n=== test_audit_dedupe_within_minute ===")
    tmpdir = Path(tempfile.mkdtemp(prefix="audit_unit_dedup_"))
    try:
        db_path = tmpdir / "audit.db"
        storage = AuditStorage(db_path)

        params = {"zip": True, "force": False}
        storage.record_operation(
            command_type=AUDIT_COMMAND_TYPES["RUN"],
            operator="tester",
            result_status=AUDIT_RESULT_STATUS["SUCCESS"],
            params_summary=params,
        )

        dup_raised = False
        try:
            storage.record_operation(
                command_type=AUDIT_COMMAND_TYPES["RUN"],
                operator="tester",
                result_status=AUDIT_RESULT_STATUS["SUCCESS"],
                params_summary=params,
            )
        except AuditDuplicateError:
            dup_raised = True
        assert_eq(dup_raised, "同参数同分钟再次写入抛 AuditDuplicateError")

        storage.record_operation(
            command_type=AUDIT_COMMAND_TYPES["RUN"],
            operator="other",
            result_status=AUDIT_RESULT_STATUS["SUCCESS"],
            params_summary=params,
        )
        assert_eq(True, "不同 operator 不触发去重")

        storage.record_operation(
            command_type=AUDIT_COMMAND_TYPES["RUN"],
            operator="tester",
            result_status=AUDIT_RESULT_STATUS["SUCCESS"],
            params_summary={"zip": False},
        )
        assert_eq(True, "不同 params 不触发去重")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# 4. JSON/CSV 导出
# ---------------------------------------------------------------------------

def test_audit_export_json_csv_stable():
    """JSON/CSV 导出：字段稳定、文件可解析"""
    print("\n=== test_audit_export_json_csv_stable ===")
    tmpdir = Path(tempfile.mkdtemp(prefix="audit_unit_exp_ok_"))
    try:
        db_path = tmpdir / "audit.db"
        storage = AuditStorage(db_path)
        storage.record_operation(
            command_type=AUDIT_COMMAND_TYPES["RUN"],
            operator="tester",
            result_status=AUDIT_RESULT_STATUS["SUCCESS"],
            batch_id="batch-001",
            package_names=["甲方交付包"],
            file_count=5,
            error_count=0,
            warning_count=1,
            error_summary="",
            detail_ref={"x": 1},
        )
        records = storage.query_records()

        json_out = tmpdir / "audit.json"
        export_audit_json(records, json_out)
        assert_eq(json_out.exists(), "JSON 导出文件已生成")
        with open(json_out, "r", encoding="utf-8") as f:
            json_data = json.load(f)
        assert_eq(isinstance(json_data, list) and len(json_data) == 1, "JSON 可解析为列表，长度 1")
        j = json_data[0]
        required_json = {"id", "command_type", "operator", "started_at", "finished_at",
                         "duration_seconds", "result_status", "batch_id", "package_names",
                         "file_count", "error_count", "warning_count", "params_summary",
                         "config_summary", "error_summary", "detail_ref"}
        assert_eq(required_json.issubset(j.keys()),
                  f"JSON 字段完整: {set(j.keys())}")

        csv_out = tmpdir / "audit.csv"
        export_audit_csv(records, csv_out)
        assert_eq(csv_out.exists(), "CSV 导出文件已生成")
        with open(csv_out, "r", encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            headers = reader.fieldnames or []
            rows = list(reader)
        assert_eq(set(headers) == set(AUDIT_EXPORT_FIELDNAMES),
                  f"CSV 表头与 AUDIT_EXPORT_FIELDNAMES 一致: {headers}")
        assert_eq(len(rows) == 1, f"CSV 有 1 行数据 (got {len(rows)})")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_audit_export_path_exists_error():
    """导出路径已存在时报 AuditExportError"""
    print("\n=== test_audit_export_path_exists_error ===")
    tmpdir = Path(tempfile.mkdtemp(prefix="audit_unit_exp_exist_"))
    try:
        existing_file = tmpdir / "exists.json"
        existing_file.write_text("{}", encoding="utf-8")

        err_raised = False
        try:
            export_audit_json([], existing_file)
        except AuditExportError:
            err_raised = True
        assert_eq(err_raised, "JSON 导出路径已存在抛 AuditExportError")

        csv_file = tmpdir / "exists.csv"
        csv_file.write_text("a,b", encoding="utf-8")
        csv_err = False
        try:
            export_audit_csv([], csv_file)
        except AuditExportError:
            csv_err = True
        assert_eq(csv_err, "CSV 导出路径已存在抛 AuditExportError")

        dir_path = tmpdir / "subdir"
        dir_path.mkdir()
        dir_err = False
        try:
            export_audit_json([], dir_path)
        except AuditExportError:
            dir_err = True
        assert_eq(dir_err, "导出路径为目录时抛 AuditExportError")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_audit_export_parent_not_writable():
    """父级非目录/无法创建时报 AuditExportError（跨平台）"""
    print("\n=== test_audit_export_parent_not_writable ===")
    tmpdir = Path(tempfile.mkdtemp(prefix="audit_unit_exp_ro_"))
    try:
        fake_parent = tmpdir / "not_a_dir"
        fake_parent.write_text("I am a file, not a dir", encoding="utf-8")

        file_as_parent_err = False
        try:
            export_audit_json([], fake_parent / "out.json")
        except AuditExportError:
            file_as_parent_err = True
        assert_eq(file_as_parent_err, "父级是文件（非目录）时报 AuditExportError")

        nested_parent = tmpdir / "level1" / "level2" / "level3"
        assert_eq(not nested_parent.exists(), "嵌套父目录尚未存在")
        csv_out = nested_parent / "out.csv"
        export_audit_csv([], csv_out)
        assert_eq(csv_out.exists(), "不存在的父目录会被自动创建，导出成功")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# 5. 旧数据兼容迁移
# ---------------------------------------------------------------------------

def test_audit_migrate_old_schema():
    """缺少 dedupe_key/warning_count 列的旧表自动迁移，旧记录仍可读取"""
    print("\n=== test_audit_migrate_old_schema ===")
    tmpdir = Path(tempfile.mkdtemp(prefix="audit_unit_mig_"))
    try:
        db_path = tmpdir / "audit.db"
        conn = sqlite3.connect(str(db_path))
        try:
            conn.executescript(
                """
                CREATE TABLE audit_records (
                    id TEXT PRIMARY KEY,
                    command_type TEXT NOT NULL,
                    operator TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    result_status TEXT NOT NULL DEFAULT 'failed',
                    params_summary TEXT NOT NULL DEFAULT '{}',
                    config_summary TEXT NOT NULL DEFAULT '{}',
                    batch_id TEXT,
                    package_names TEXT NOT NULL DEFAULT '[]',
                    file_count INTEGER NOT NULL DEFAULT 0,
                    error_count INTEGER NOT NULL DEFAULT 0,
                    error_summary TEXT NOT NULL DEFAULT '',
                    detail_ref TEXT NOT NULL DEFAULT '{}'
                );
                """
            )
            old_id = str(uuid.uuid4())
            old_start = (datetime.now() - timedelta(days=5)).isoformat(timespec="seconds")
            conn.execute(
                """INSERT INTO audit_records
                   (id, command_type, operator, started_at, result_status,
                    params_summary, config_summary, package_names,
                    file_count, error_count, error_summary, detail_ref)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    old_id,
                    AUDIT_COMMAND_TYPES["RUN"],
                    "legacy_user",
                    old_start,
                    AUDIT_RESULT_STATUS["SUCCESS"],
                    json.dumps({"legacy": True}, ensure_ascii=False),
                    "{}",
                    json.dumps(["旧包"]),
                    3,
                    0,
                    "",
                    "{}",
                ),
            )
            conn.commit()
        finally:
            conn.close()

        storage = AuditStorage(db_path)

        cols_conn = sqlite3.connect(str(db_path))
        try:
            cols = {r[1] for r in cols_conn.execute("PRAGMA table_info(audit_records)").fetchall()}
        finally:
            cols_conn.close()
        assert_eq("dedupe_key" in cols, "迁移后存在 dedupe_key 列")
        assert_eq("warning_count" in cols, "迁移后存在 warning_count 列")

        old_rec = storage.get_record(old_id)
        assert_eq(old_rec is not None, "旧记录仍可通过 get_record 读取")
        assert_eq(old_rec.id == old_id, "旧记录 id 正确")
        assert_eq(old_rec.operator == "legacy_user", f"旧记录 operator=legacy_user (got {old_rec.operator})")
        assert_eq(old_rec.warning_count == 0, f"旧记录 warning_count 默认 0 (got {old_rec.warning_count})")
        assert_eq(old_rec.params_summary == {"legacy": True}, "旧记录 params_summary 正确反序列化")
        assert_eq(old_rec.package_names == ["旧包"], "旧记录 package_names 正确反序列化")

        new_rec = storage.record_operation(
            command_type=AUDIT_COMMAND_TYPES["DIFF"],
            operator="new_user",
            result_status=AUDIT_RESULT_STATUS["SUCCESS"],
        )
        assert_eq(new_rec is not None and len(new_rec.id) > 0, "迁移后仍可写入新记录")
        all_recs = storage.query_records()
        assert_eq(len(all_recs) == 2, f"迁移后数据库共有 2 条记录 (got {len(all_recs)})")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# 6. 配置验证
# ---------------------------------------------------------------------------

def test_audit_config_validation():
    """AuditConfig.from_dict 各种错误场景"""
    print("\n=== test_audit_config_validation ===")
    tmpdir = Path(tempfile.mkdtemp(prefix="audit_unit_cfg_"))
    try:
        base = Path(tmpdir)

        err_enabled = False
        try:
            AuditConfig.from_dict({"enabled": "yes"}, base)
        except AuditConfigError:
            err_enabled = True
        assert_eq(err_enabled, "enabled 非布尔抛 AuditConfigError")

        err_ret_neg = False
        try:
            AuditConfig.from_dict({"retention_days": -1}, base)
        except AuditConfigError:
            err_ret_neg = True
        assert_eq(err_ret_neg, "retention_days 负数抛 AuditConfigError")

        err_ret_str = False
        try:
            AuditConfig.from_dict({"retention_days": "30"}, base)
        except AuditConfigError:
            err_ret_str = True
        assert_eq(err_ret_str, "retention_days 非整数（字符串）抛 AuditConfigError")

        err_ret_float = False
        try:
            AuditConfig.from_dict({"retention_days": 30.5}, base)
        except AuditConfigError:
            err_ret_float = True
        assert_eq(err_ret_float, "retention_days 非整数（浮点数）抛 AuditConfigError")

        err_export_type = False
        try:
            AuditConfig.from_dict({"export_default_dir": 123}, base)
        except AuditConfigError:
            err_export_type = True
        assert_eq(err_export_type, "export_default_dir 非字符串抛 AuditConfigError")

        cfg_ok = AuditConfig.from_dict(
            {"enabled": True, "retention_days": 60, "export_default_dir": "./exports"},
            base,
        )
        assert_eq(cfg_ok.enabled is True, "正常配置 enabled=True")
        assert_eq(cfg_ok.retention_days == 60, f"正常配置 retention_days=60 (got {cfg_ok.retention_days})")
        assert_eq(cfg_ok.export_default_dir is not None, "正常配置 export_default_dir 已解析")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# 7. 审计关闭后写入不报错
# ---------------------------------------------------------------------------

def test_audit_disabled_no_error():
    """AppConfig.audit.enabled=False 时 _get_audit_storage 返回 None，_try_audit_record 直接返回"""
    print("\n=== test_audit_disabled_no_error ===")
    tmpdir = Path(tempfile.mkdtemp(prefix="audit_unit_dis_"))
    try:
        cfg = AppConfig(
            manifest_path=tmpdir / "manifest.csv",
            source_root=tmpdir / "src",
            packages=[PackageConfig(name="test", output_dir=tmpdir / "out")],
            operator="tester",
            db_path=tmpdir / "app.db",
            audit=AuditConfig(enabled=False),
        )

        storage = _get_audit_storage(cfg)
        assert_eq(storage is None, "audit.enabled=False 时 _get_audit_storage 返回 None")

        try:
            _try_audit_record(
                storage,
                AUDIT_COMMAND_TYPES["RUN"],
                "tester",
                AUDIT_RESULT_STATUS["SUCCESS"],
                params_summary={"x": 1},
            )
            ok = True
        except Exception:
            ok = False
        assert_eq(ok, "_try_audit_record(audit=None) 直接返回不抛错")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# 8. cleanup_old_records
# ---------------------------------------------------------------------------

def test_audit_cleanup_old_records():
    """写入旧记录，调用 cleanup，确认被删除"""
    print("\n=== test_audit_cleanup_old_records ===")
    tmpdir = Path(tempfile.mkdtemp(prefix="audit_unit_cln_"))
    try:
        db_path = tmpdir / "audit.db"
        storage = AuditStorage(db_path)

        storage.record_operation(
            command_type=AUDIT_COMMAND_TYPES["RUN"],
            operator="recent",
            result_status=AUDIT_RESULT_STATUS["SUCCESS"],
        )

        conn = sqlite3.connect(str(db_path))
        try:
            old_time = (datetime.now() - timedelta(days=200)).isoformat(timespec="seconds")
            conn.execute(
                """INSERT INTO audit_records
                   (id, dedupe_key, command_type, operator, started_at, result_status,
                    params_summary, config_summary, package_names,
                    file_count, error_count, warning_count, error_summary, detail_ref)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    str(uuid.uuid4()),
                    "old-dedupe-key-1",
                    AUDIT_COMMAND_TYPES["DRY_RUN"],
                    "ancient",
                    old_time,
                    AUDIT_RESULT_STATUS["FAILED"],
                    "{}", "{}", "[]",
                    0, 1, 0, "old error", "{}",
                ),
            )
            conn.commit()
        finally:
            conn.close()

        before = storage.query_records(limit=100)
        assert_eq(len(before) == 2, f"清理前共 2 条 (got {len(before)})")

        deleted = storage.cleanup_old_records(retention_days=90)
        assert_eq(deleted == 1, f"cleanup 删除了 1 条旧记录 (got {deleted})")

        after = storage.query_records(limit=100)
        assert_eq(len(after) == 1, f"清理后剩 1 条 (got {len(after)})")
        assert_eq(after[0].operator == "recent", "保留了近期记录")

        zero = storage.cleanup_old_records(retention_days=0)
        assert_eq(zero == 0, "retention_days<=0 时返回 0")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# 9. 失败操作也能留流水
# ---------------------------------------------------------------------------

def test_audit_failed_operation_still_recorded():
    """模拟失败的 template-import，确认仍写入 failed 状态记录"""
    print("\n=== test_audit_failed_operation_still_recorded ===")
    tmpdir = Path(tempfile.mkdtemp(prefix="audit_unit_fail_"))
    try:
        db_path = tmpdir / "audit.db"
        storage = AuditStorage(db_path)

        storage.record_operation(
            command_type=AUDIT_COMMAND_TYPES["TEMPLATE_IMPORT"],
            operator="importer",
            result_status=AUDIT_RESULT_STATUS["FAILED"],
            params_summary={"template_file": "/bad/path/tpl.yaml"},
            error_count=1,
            error_summary="模板文件不存在: /bad/path/tpl.yaml",
            detail_ref={"exception_type": "FileNotFoundError"},
        )

        recs = storage.query_records(
            command_type=AUDIT_COMMAND_TYPES["TEMPLATE_IMPORT"],
            result_status=AUDIT_RESULT_STATUS["FAILED"],
        )
        assert_eq(len(recs) == 1, f"查询到 1 条 failed 的 template-import 记录 (got {len(recs)})")
        r = recs[0]
        assert_eq(r.result_status == AUDIT_RESULT_STATUS["FAILED"], "result_status=failed")
        assert_eq(r.error_count == 1, f"error_count=1 (got {r.error_count})")
        assert_eq("模板文件不存在" in r.error_summary,
                  f"error_summary 含错误信息: {r.error_summary}")
        assert_eq(r.params_summary.get("template_file") == "/bad/path/tpl.yaml",
                  "params_summary 含失败时的参数")
        assert_eq(r.detail_ref.get("exception_type") == "FileNotFoundError",
                  "detail_ref 含异常类型")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main():
    global TESTS_PASS, TESTS_FAIL
    try:
        test_audit_crud_start_finish()
        test_audit_crud_record_operation()
        test_audit_query_filters()
        test_audit_dedupe_within_minute()
        test_audit_export_json_csv_stable()
        test_audit_export_path_exists_error()
        test_audit_export_parent_not_writable()
        test_audit_migrate_old_schema()
        test_audit_config_validation()
        test_audit_disabled_no_error()
        test_audit_cleanup_old_records()
        test_audit_failed_operation_still_recorded()
    except AssertionError:
        pass

    print(f"\n=== 审计模块单元测试: 通过 {TESTS_PASS}, 失败 {TESTS_FAIL} ===")
    import sys
    sys.exit(1 if TESTS_FAIL else 0)


if __name__ == "__main__":
    main()
