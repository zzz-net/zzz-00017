"""审计模块 - 单元测试

覆盖：
  - AuditStorage 基本 CRUD: start_record + finish_record、record_operation
  - 查询过滤组合：时间范围、操作者、命令类型、多命令类型、批次 id、包名、结果状态、limit
  - 同操作一分钟内去重：同参数再次写入抛 AuditDuplicateError
  - JSON/CSV 导出：字段稳定、文件可解析、导出路径已存在/父目录不可写时报错
  - 旧数据兼容：缺少 dedupe_key/warning_count 列的旧表自动迁移
  - 配置验证：AuditConfig.from_dict 各种错误场景
  - 审计关闭后写入不报错（AuditService）
  - cleanup_old_records: 清理过期记录
  - 失败操作也能留流水
  - AuditService 业务逻辑：try_record/query/export/resolve_export_path
"""

from __future__ import annotations

import csv
import json
import os
import shutil
import sqlite3
import tempfile
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List

import pytest

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
    AuditService,
    AuditStorage,
    export_audit_csv,
    export_audit_json,
)
from contract_pack.cli import _get_audit_service, _get_audit_storage, _try_audit_record
from contract_pack.config import AppConfig, AuditConfig, PackageConfig


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def tmpdir_path(tmp_path):
    """提供临时目录 Path 对象"""
    return tmp_path


@pytest.fixture()
def empty_audit_storage(tmpdir_path):
    """提供一个干净的 AuditStorage 实例"""
    db_path = tmpdir_path / "audit.db"
    return AuditStorage(db_path)


# ---------------------------------------------------------------------------
# 1. AuditStorage 基本 CRUD
# ---------------------------------------------------------------------------

class TestAuditStorageCRUD:
    def test_start_record_and_finish(self, tmpdir_path):
        """start_record + finish_record 完整流程"""
        db_path = tmpdir_path / "audit.db"
        storage = AuditStorage(db_path)
        assert db_path.exists(), "审计数据库文件已创建"

        rec = storage.start_record(
            command_type=AUDIT_COMMAND_TYPES["RUN"],
            operator="tester",
            params_summary={"zip": True},
            config_summary={"operator": "tester"},
            package_names=["甲方交付包"],
        )
        assert isinstance(rec.id, str) and len(rec.id) > 0
        assert rec.command_type == AUDIT_COMMAND_TYPES["RUN"]
        assert rec.operator == "tester"
        assert rec.result_status == AUDIT_RESULT_STATUS["FAILED"]
        assert rec.package_names == ["甲方交付包"]

        got = storage.get_record(rec.id)
        assert got is not None
        assert got.id == rec.id

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
        assert got2.result_status == AUDIT_RESULT_STATUS["SUCCESS"]
        assert got2.file_count == 5
        assert got2.warning_count == 2
        assert got2.batch_id == "batch-001"
        assert got2.finished_at is not None and len(got2.finished_at) > 0

    def test_record_operation(self, tmpdir_path):
        """record_operation 一次性写入完整记录"""
        db_path = tmpdir_path / "audit.db"
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
        assert rec.command_type == AUDIT_COMMAND_TYPES["DIFF"]
        assert rec.result_status == AUDIT_RESULT_STATUS["PARTIAL"]
        assert rec.started_at == rec.finished_at
        assert rec.file_count == 10
        assert rec.error_count == 1
        assert rec.package_names == ["甲方交付包", "乙方交付包"]

        d = rec.to_dict()
        assert "duration_seconds" in d
        assert d["command_type"] == AUDIT_COMMAND_TYPES["DIFF"]


# ---------------------------------------------------------------------------
# 2. 查询过滤组合
# ---------------------------------------------------------------------------

def _seed_query_data(storage: AuditStorage) -> Dict[str, Any]:
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
    t0 = (datetime.now() - timedelta(hours=2)).isoformat(timespec="seconds")
    t2 = datetime.now().isoformat(timespec="seconds")
    return {"a": rec_a, "b": rec_b, "c": rec_c, "t0": t0, "t2": t2}


class TestAuditQueryFilters:
    def test_all_filters(self, empty_audit_storage):
        """多维度查询过滤组合"""
        seed = _seed_query_data(empty_audit_storage)
        storage = empty_audit_storage

        all_recs = storage.query_records()
        assert len(all_recs) == 3

        by_op = storage.query_records(operator="alice")
        assert len(by_op) == 2
        assert all(r.operator == "alice" for r in by_op)

        by_cmd = storage.query_records(command_type=AUDIT_COMMAND_TYPES["DRY_RUN"])
        assert len(by_cmd) == 1
        assert by_cmd[0].command_type == AUDIT_COMMAND_TYPES["DRY_RUN"]

        by_cmds = storage.query_records(
            command_types=[AUDIT_COMMAND_TYPES["RUN"], AUDIT_COMMAND_TYPES["DIFF"]]
        )
        assert len(by_cmds) == 2
        types = {r.command_type for r in by_cmds}
        assert types == {AUDIT_COMMAND_TYPES["RUN"], AUDIT_COMMAND_TYPES["DIFF"]}

        by_batch = storage.query_records(batch_id="batch-B")
        assert len(by_batch) == 1
        assert by_batch[0].batch_id == "batch-B"

        by_pkg = storage.query_records(package_name="乙方交付包")
        assert len(by_pkg) == 2

        by_status = storage.query_records(result_status=AUDIT_RESULT_STATUS["FAILED"])
        assert len(by_status) == 1

        by_limit = storage.query_records(limit=1)
        assert len(by_limit) == 1

        by_time_range = storage.query_records(
            start_time=seed["t0"], end_time=seed["t2"]
        )
        assert len(by_time_range) >= 1

        combined = storage.query_records(
            operator="alice",
            result_status=AUDIT_RESULT_STATUS["SUCCESS"],
            package_name="甲方交付包",
        )
        assert len(combined) >= 1


# ---------------------------------------------------------------------------
# 3. 同操作一分钟内去重
# ---------------------------------------------------------------------------

class TestAuditDeduplication:
    def test_same_params_within_minute_raises(self, tmpdir_path):
        """同参数再次写入抛 AuditDuplicateError"""
        db_path = tmpdir_path / "audit.db"
        storage = AuditStorage(db_path)

        params = {"zip": True, "force": False}
        storage.record_operation(
            command_type=AUDIT_COMMAND_TYPES["RUN"],
            operator="tester",
            result_status=AUDIT_RESULT_STATUS["SUCCESS"],
            params_summary=params,
        )

        with pytest.raises(AuditDuplicateError):
            storage.record_operation(
                command_type=AUDIT_COMMAND_TYPES["RUN"],
                operator="tester",
                result_status=AUDIT_RESULT_STATUS["SUCCESS"],
                params_summary=params,
            )

    def test_different_operator_not_duplicate(self, tmpdir_path):
        """不同 operator 不触发去重"""
        db_path = tmpdir_path / "audit.db"
        storage = AuditStorage(db_path)
        params = {"zip": True}
        storage.record_operation(
            command_type=AUDIT_COMMAND_TYPES["RUN"],
            operator="tester",
            result_status=AUDIT_RESULT_STATUS["SUCCESS"],
            params_summary=params,
        )
        storage.record_operation(
            command_type=AUDIT_COMMAND_TYPES["RUN"],
            operator="other",
            result_status=AUDIT_RESULT_STATUS["SUCCESS"],
            params_summary=params,
        )

    def test_different_params_not_duplicate(self, tmpdir_path):
        """不同 params 不触发去重"""
        db_path = tmpdir_path / "audit.db"
        storage = AuditStorage(db_path)
        storage.record_operation(
            command_type=AUDIT_COMMAND_TYPES["RUN"],
            operator="tester",
            result_status=AUDIT_RESULT_STATUS["SUCCESS"],
            params_summary={"zip": True},
        )
        storage.record_operation(
            command_type=AUDIT_COMMAND_TYPES["RUN"],
            operator="tester",
            result_status=AUDIT_RESULT_STATUS["SUCCESS"],
            params_summary={"zip": False},
        )


# ---------------------------------------------------------------------------
# 4. JSON/CSV 导出
# ---------------------------------------------------------------------------

class TestAuditExport:
    def test_export_json_csv_stable(self, tmpdir_path):
        """JSON/CSV 导出：字段稳定、文件可解析"""
        db_path = tmpdir_path / "audit.db"
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

        json_out = tmpdir_path / "audit.json"
        export_audit_json(records, json_out)
        assert json_out.exists()
        with open(json_out, "r", encoding="utf-8") as f:
            json_data = json.load(f)
        assert isinstance(json_data, list) and len(json_data) == 1
        j = json_data[0]
        required_json = {"id", "command_type", "operator", "started_at", "finished_at",
                         "duration_seconds", "result_status", "batch_id", "package_names",
                         "file_count", "error_count", "warning_count", "params_summary",
                         "config_summary", "error_summary", "detail_ref"}
        assert required_json.issubset(j.keys())

        csv_out = tmpdir_path / "audit.csv"
        export_audit_csv(records, csv_out)
        assert csv_out.exists()
        with open(csv_out, "r", encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            headers = reader.fieldnames or []
            rows = list(reader)
        assert set(headers) == set(AUDIT_EXPORT_FIELDNAMES)
        assert len(rows) == 1

    def test_export_path_exists_error(self, tmpdir_path):
        """导出路径已存在时报 AuditExportError"""
        existing_file = tmpdir_path / "exists.json"
        existing_file.write_text("{}", encoding="utf-8")
        with pytest.raises(AuditExportError):
            export_audit_json([], existing_file)

        csv_file = tmpdir_path / "exists.csv"
        csv_file.write_text("a,b", encoding="utf-8")
        with pytest.raises(AuditExportError):
            export_audit_csv([], csv_file)

        dir_path = tmpdir_path / "subdir"
        dir_path.mkdir()
        with pytest.raises(AuditExportError):
            export_audit_json([], dir_path)

    def test_export_parent_not_writable(self, tmpdir_path):
        """父级非目录/无法创建时报 AuditExportError（跨平台）"""
        fake_parent = tmpdir_path / "not_a_dir"
        fake_parent.write_text("I am a file, not a dir", encoding="utf-8")

        with pytest.raises(AuditExportError):
            export_audit_json([], fake_parent / "out.json")

        nested_parent = tmpdir_path / "level1" / "level2" / "level3"
        csv_out = nested_parent / "out.csv"
        export_audit_csv([], csv_out)
        assert csv_out.exists()


# ---------------------------------------------------------------------------
# 5. 旧数据兼容迁移
# ---------------------------------------------------------------------------

class TestAuditMigration:
    def test_migrate_old_schema(self, tmpdir_path):
        """缺少 dedupe_key/warning_count 列的旧表自动迁移，旧记录仍可读取"""
        db_path = tmpdir_path / "audit.db"
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
        assert "dedupe_key" in cols
        assert "warning_count" in cols

        old_rec = storage.get_record(old_id)
        assert old_rec is not None
        assert old_rec.id == old_id
        assert old_rec.operator == "legacy_user"
        assert old_rec.warning_count == 0
        assert old_rec.params_summary == {"legacy": True}
        assert old_rec.package_names == ["旧包"]

        new_rec = storage.record_operation(
            command_type=AUDIT_COMMAND_TYPES["DIFF"],
            operator="new_user",
            result_status=AUDIT_RESULT_STATUS["SUCCESS"],
        )
        assert new_rec is not None and len(new_rec.id) > 0
        all_recs = storage.query_records()
        assert len(all_recs) == 2


# ---------------------------------------------------------------------------
# 6. 配置验证
# ---------------------------------------------------------------------------

class TestAuditConfigValidation:
    def test_various_errors(self, tmpdir_path):
        """AuditConfig.from_dict 各种错误场景"""
        base = Path(tmpdir_path)

        with pytest.raises(AuditConfigError):
            AuditConfig.from_dict({"enabled": "yes"}, base)

        with pytest.raises(AuditConfigError):
            AuditConfig.from_dict({"retention_days": -1}, base)

        with pytest.raises(AuditConfigError):
            AuditConfig.from_dict({"retention_days": "30"}, base)

        with pytest.raises(AuditConfigError):
            AuditConfig.from_dict({"retention_days": 30.5}, base)

        with pytest.raises(AuditConfigError):
            AuditConfig.from_dict({"export_default_dir": 123}, base)

        cfg_ok = AuditConfig.from_dict(
            {"enabled": True, "retention_days": 60, "export_default_dir": "./exports"},
            base,
        )
        assert cfg_ok.enabled is True
        assert cfg_ok.retention_days == 60
        assert cfg_ok.export_default_dir is not None


# ---------------------------------------------------------------------------
# 7. AuditService 审计关闭后写入不报错
# ---------------------------------------------------------------------------

class TestAuditServiceDisabled:
    def test_disabled_service_noop(self, tmpdir_path):
        """AuditService disabled：try_record 不抛错，query/get 返回空"""
        cfg = AppConfig(
            manifest_path=tmpdir_path / "manifest.csv",
            source_root=tmpdir_path / "src",
            packages=[PackageConfig(name="test", output_dir=tmpdir_path / "out")],
            operator="tester",
            db_path=tmpdir_path / "app.db",
            audit=AuditConfig(enabled=False),
        )

        svc = AuditService.from_config(cfg)
        assert svc.enabled is False

        result = svc.try_record(
            AUDIT_COMMAND_TYPES["RUN"],
            "tester",
            AUDIT_RESULT_STATUS["SUCCESS"],
            params_summary={"x": 1},
        )
        assert result is None

        assert svc.query() == []
        assert svc.get("any-id") is None

    def test_cli_helpers_with_disabled(self, tmpdir_path):
        """CLI 辅助函数在审计关闭时行为正确"""
        cfg = AppConfig(
            manifest_path=tmpdir_path / "manifest.csv",
            source_root=tmpdir_path / "src",
            packages=[PackageConfig(name="test", output_dir=tmpdir_path / "out")],
            operator="tester",
            db_path=tmpdir_path / "app.db",
            audit=AuditConfig(enabled=False),
        )

        storage = _get_audit_storage(cfg)
        assert storage is None

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
        assert ok


# ---------------------------------------------------------------------------
# 8. cleanup_old_records
# ---------------------------------------------------------------------------

class TestAuditCleanup:
    def test_cleanup_old_records(self, tmpdir_path):
        """写入旧记录，调用 cleanup，确认被删除"""
        db_path = tmpdir_path / "audit.db"
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
        assert len(before) == 2

        deleted = storage.cleanup_old_records(retention_days=90)
        assert deleted == 1

        after = storage.query_records(limit=100)
        assert len(after) == 1
        assert after[0].operator == "recent"

        zero = storage.cleanup_old_records(retention_days=0)
        assert zero == 0


# ---------------------------------------------------------------------------
# 9. 失败操作也能留流水
# ---------------------------------------------------------------------------

class TestFailedOperationRecorded:
    def test_failed_template_import_recorded(self, tmpdir_path):
        """模拟失败的 template-import，确认仍写入 failed 状态记录"""
        db_path = tmpdir_path / "audit.db"
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
        assert len(recs) == 1
        r = recs[0]
        assert r.result_status == AUDIT_RESULT_STATUS["FAILED"]
        assert r.error_count == 1
        assert "模板文件不存在" in r.error_summary
        assert r.params_summary.get("template_file") == "/bad/path/tpl.yaml"
        assert r.detail_ref.get("exception_type") == "FileNotFoundError"


# ---------------------------------------------------------------------------
# 10. AuditService 业务逻辑
# ---------------------------------------------------------------------------

class TestAuditService:
    def test_try_record_and_query(self, tmpdir_path):
        """AuditService try_record 后 query 可查到"""
        cfg = AppConfig(
            manifest_path=tmpdir_path / "manifest.csv",
            source_root=tmpdir_path / "src",
            packages=[PackageConfig(name="test", output_dir=tmpdir_path / "out")],
            operator="svc_tester",
            db_path=tmpdir_path / "app.db",
            audit=AuditConfig(enabled=True, retention_days=90),
        )
        svc = AuditService.from_config(cfg)
        assert svc.enabled

        rec = svc.try_record(
            AUDIT_COMMAND_TYPES["RUN"],
            "svc_tester",
            AUDIT_RESULT_STATUS["SUCCESS"],
            package_names=["test-pkg"],
            file_count=3,
        )
        assert rec is not None

        all_recs = svc.query()
        assert len(all_recs) >= 1
        assert all_recs[0].operator == "svc_tester"

        got = svc.get(rec.id)
        assert got is not None
        assert got.id == rec.id

    def test_resolve_export_path(self, tmpdir_path):
        """AuditService.resolve_export_path 解析逻辑"""
        svc_with_default = AuditService(
            storage=None,
            enabled=False,
            export_default_dir=tmpdir_path / "exports",
        )
        assert svc_with_default.resolve_export_path(None, "json") is not None
        assert svc_with_default.resolve_export_path("/abs/path/out.json", "json") == Path("/abs/path/out.json")

        svc_no_default = AuditService(storage=None, enabled=False)
        assert svc_no_default.resolve_export_path(None, "json") is None
        assert svc_no_default.resolve_export_path("./out.csv", "csv") == Path("./out.csv")

    def test_service_export_json_csv(self, tmpdir_path):
        """AuditService export_json/export_csv"""
        svc = AuditService(storage=None, enabled=False)
        db_path = tmpdir_path / "audit.db"
        storage = AuditStorage(db_path)
        storage.record_operation(
            command_type=AUDIT_COMMAND_TYPES["RUN"],
            operator="t",
            result_status=AUDIT_RESULT_STATUS["SUCCESS"],
        )
        records = storage.query_records()

        jout = tmpdir_path / "svc_audit.json"
        svc.export_json(records, jout)
        assert jout.exists()
        with open(jout, "r", encoding="utf-8") as f:
            jd = json.load(f)
        assert len(jd) == 1

        cout = tmpdir_path / "svc_audit.csv"
        svc.export_csv(records, cout)
        assert cout.exists()
        with open(cout, "r", encoding="utf-8-sig", newline="") as f:
            rows = list(csv.DictReader(f))
        assert len(rows) == 1

    def test_service_validate_path_conflict(self, tmpdir_path):
        """AuditService 路径冲突抛错"""
        svc = AuditService(storage=None, enabled=False)
        existing = tmpdir_path / "exists.json"
        existing.write_text("{}")
        with pytest.raises(AuditExportError):
            svc.export_json([], existing)
