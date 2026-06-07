"""审计模块 CLI 端到端测试

覆盖：
  - 跨重启查询：dry-run/run 后重新实例化 AuditStorage，记录仍在
  - 过滤组合：audit list 按 command-type、operator、result-status、时间范围组合过滤
  - 导出冲突：导出 JSON/CSV 到已存在路径，CLI 错误非零退出
  - 权限失败：导出到只读目录，CLI 错误非零退出
  - 旧数据兼容：缺少列的旧表结构，audit list 正常显示
  - 审计关闭：audit.enabled=false，命令执行不写入审计
  - 失败 template import 留流水：导入不存在文件，audit list 查到 failed 记录
  - audit show 单条记录：关键字段完整显示
"""

from __future__ import annotations

import csv
import json
import os
import shutil
import sqlite3
import stat
import subprocess
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List

import pytest
import yaml


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def run(cmd, **kwargs):
    """封装 subprocess 调用，返回 CompletedProcess"""
    return subprocess.run(cmd, shell=True, capture_output=True, text=True, **kwargs)


def count_audit_rows(db_path: Path) -> int:
    conn = sqlite3.connect(str(db_path))
    try:
        try:
            row = conn.execute("SELECT COUNT(*) FROM audit_records").fetchone()
            return row[0] if row else 0
        except sqlite3.OperationalError:
            return 0
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def e2e_workdir(tmp_path):
    """提供可运行的工作目录 fixture"""
    def _make(name: str, audit_enabled: bool = True) -> Path:
        work = tmp_path / name
        if work.exists():
            shutil.rmtree(work)
        work.mkdir(parents=True)

        src = work / "sources"
        (src / "contracts").mkdir(parents=True)
        (src / "scans").mkdir(parents=True)
        (src / "contracts" / "main.pdf").write_text("MAIN CONTRACT v1", encoding="utf-8")
        (src / "contracts" / "supp.pdf").write_text("SUPPLEMENT v1", encoding="utf-8")
        (src / "scans" / "seal.jpg").write_text("SEAL IMAGE", encoding="utf-8")

        manifest = work / "manifest.csv"
        with open(manifest, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            w.writerow(["package", "category", "source_path", "target_name", "version", "description"])
            w.writerow(["甲方交付包", "main", "contracts/main.pdf", "主合同.pdf", "v1", ""])
            w.writerow(["甲方交付包", "supplement", "contracts/supp.pdf", "补充协议.pdf", "v1", ""])
            w.writerow(["甲方交付包", "seal", "scans/seal.jpg", "盖章扫描件.jpg", "", ""])

        cfg = {
            "operator": "e2e_tester",
            "manifest": "manifest.csv",
            "source_root": "./sources",
            "db_path": "./.contract_pack.db",
            "allow_overwrite": True,
            "audit": {
                "enabled": audit_enabled,
                "retention_days": 90,
            },
            "packages": [
                {
                    "name": "甲方交付包",
                    "output_dir": "./deliver/PartyA",
                    "zip_output": "./deliver/甲方交付包.zip",
                    "version": "v2024.06",
                    "file_mapping": {"main": "01_主合同", "supplement": "02_补充协议"},
                }
            ],
        }
        with open(work / "contract_pack.yaml", "w", encoding="utf-8") as f:
            yaml.safe_dump(cfg, f, allow_unicode=True, sort_keys=False)
        return work
    return _make


# ---------------------------------------------------------------------------
# 1. 跨重启查询
# ---------------------------------------------------------------------------

class TestCrossRestartQuery:
    def test_records_persist_across_restart(self, e2e_workdir):
        """跨重启查询：执行 dry-run 和 run，重新实例化 storage，确认记录仍在"""
        work = e2e_workdir("audit_cross_restart")
        db_path = work / ".contract_pack.db"

        r1 = run("contract-pack -c contract_pack.yaml dry-run", cwd=work)
        assert r1.returncode == 0, f"dry-run 执行失败: {r1.stdout} {r1.stderr}"

        r2 = run("contract-pack -c contract_pack.yaml run", cwd=work)
        assert r2.returncode == 0, f"run 执行失败: {r2.stdout} {r2.stderr}"

        assert db_path.exists()
        count_after = count_audit_rows(db_path)
        assert count_after >= 2, f"审计记录至少 2 条 (实际 {count_after})"

        from contract_pack.audit import AuditStorage
        storage2 = AuditStorage(db_path)
        records = storage2.query_records(limit=100)
        assert len(records) >= 2, f"跨重启后读取到至少 2 条 (实际 {len(records)})"

        command_types = {r.command_type for r in records}
        assert "dry-run" in command_types
        assert "run" in command_types

        for rec in records:
            assert rec.id and len(rec.id) > 0
            assert rec.operator == "e2e_tester"
            assert rec.started_at and len(rec.started_at) > 0
            assert rec.result_status in {"success", "failed", "partial", "skipped"}


# ---------------------------------------------------------------------------
# 2. 过滤组合
# ---------------------------------------------------------------------------

class TestFilterCombinations:
    def test_audit_list_various_filters(self, e2e_workdir):
        """过滤组合：执行多种命令后，audit list 按多维度组合过滤"""
        work = e2e_workdir("audit_filter_combinations")

        run("contract-pack -c contract_pack.yaml dry-run", cwd=work)
        run("contract-pack -c contract_pack.yaml run", cwd=work)
        missing_tpl = work / "does_not_exist.json"
        run(f'contract-pack -c contract_pack.yaml template import -f json -i "{missing_tpl}"', cwd=work)

        r_all = run("contract-pack -c contract_pack.yaml audit list -n 100", cwd=work)
        assert r_all.returncode == 0
        assert "审计记录" in r_all.stdout

        r_dryrun = run("contract-pack -c contract_pack.yaml audit list --command-type dry-run -n 100", cwd=work)
        assert r_dryrun.returncode == 0
        dryrun_count = r_dryrun.stdout.count("dry-run")
        assert dryrun_count >= 1
        assert "template-import" not in r_dryrun.stdout

        r_run = run("contract-pack -c contract_pack.yaml audit list --command-type run -n 100", cwd=work)
        assert r_run.returncode == 0
        assert "run" in r_run.stdout

        r_success = run("contract-pack -c contract_pack.yaml audit list --result-status success -n 100", cwd=work)
        assert r_success.returncode == 0

        r_failed = run("contract-pack -c contract_pack.yaml audit list --result-status failed -n 100", cwd=work)
        assert r_failed.returncode == 0

        r_op = run("contract-pack -c contract_pack.yaml audit list --operator e2e_tester -n 100", cwd=work)
        assert r_op.returncode == 0

        past = (datetime.now() - timedelta(days=365)).isoformat(timespec="seconds")
        future = (datetime.now() + timedelta(days=1)).isoformat(timespec="seconds")
        r_time = run(
            f'contract-pack -c contract_pack.yaml audit list --start-time "{past}" --end-time "{future}" -n 100',
            cwd=work,
        )
        assert r_time.returncode == 0

        r_combined = run(
            "contract-pack -c contract_pack.yaml audit list "
            "--command-type dry-run --operator e2e_tester --result-status success -n 100",
            cwd=work,
        )
        assert r_combined.returncode == 0


# ---------------------------------------------------------------------------
# 3. 导出冲突
# ---------------------------------------------------------------------------

class TestExportConflict:
    def test_export_to_existing_file_or_dir(self, e2e_workdir):
        """导出冲突：导出 JSON/CSV 到已存在路径，CLI 给出可读错误且退出非 0"""
        work = e2e_workdir("audit_export_conflict")

        run("contract-pack -c contract_pack.yaml dry-run", cwd=work)

        existing_json = work / "existing_audit.json"
        existing_json.write_text("I EXIST ALREADY", encoding="utf-8")
        rj = run(f'contract-pack -c contract_pack.yaml audit export -f json -o "{existing_json}"', cwd=work)
        assert rj.returncode != 0
        combined = (rj.stdout + rj.stderr)
        assert (
            ("已存在" in combined) or ("存在" in combined) or ("exist" in combined.lower()) or ("失败" in combined)
        )
        assert existing_json.read_text(encoding="utf-8") == "I EXIST ALREADY"

        existing_csv = work / "existing_audit.csv"
        existing_csv.write_text("OLD,CSV,CONTENT", encoding="utf-8")
        rc = run(f'contract-pack -c contract_pack.yaml audit export -f csv -o "{existing_csv}"', cwd=work)
        assert rc.returncode != 0
        combined_c = (rc.stdout + rc.stderr)
        assert (
            ("已存在" in combined_c) or ("存在" in combined_c) or ("exist" in combined_c.lower()) or ("失败" in combined_c)
        )
        assert existing_csv.read_text(encoding="utf-8") == "OLD,CSV,CONTENT"

        existing_dir = work / "existing_dir"
        existing_dir.mkdir()
        rd = run(f'contract-pack -c contract_pack.yaml audit export -f json -o "{existing_dir}"', cwd=work)
        assert rd.returncode != 0


# ---------------------------------------------------------------------------
# 4. 权限失败
# ---------------------------------------------------------------------------

class TestPermissionFailure:
    def test_export_to_bad_parent_path(self, e2e_workdir):
        """权限失败：导出到不可写路径，CLI 给出可读错误且退出非 0"""
        work = e2e_workdir("audit_permission_failure")

        run("contract-pack -c contract_pack.yaml dry-run", cwd=work)

        parent_as_file = work / "readonly_dir"
        parent_as_file.write_text("I am a file, not a directory", encoding="utf-8")

        out_json = parent_as_file / "audit.json"
        rj = run(f'contract-pack -c contract_pack.yaml audit export -f json -o "{out_json}"', cwd=work)
        assert rj.returncode != 0
        combined = (rj.stdout + rj.stderr)
        assert (
            ("失败" in combined) or ("父级" in combined) or ("目录" in combined) or ("无法" in combined)
        )
        assert not out_json.exists()

        out_csv = parent_as_file / "audit.csv"
        rc = run(f'contract-pack -c contract_pack.yaml audit export -f csv -o "{out_csv}"', cwd=work)
        assert rc.returncode != 0
        combined_c = (rc.stdout + rc.stderr)
        assert (
            ("失败" in combined_c) or ("父级" in combined_c) or ("目录" in combined_c) or ("无法" in combined_c)
        )
        assert not out_csv.exists()


# ---------------------------------------------------------------------------
# 5. 旧数据兼容
# ---------------------------------------------------------------------------

class TestLegacyDataCompatibility:
    def test_legacy_schema_still_works(self, e2e_workdir):
        """旧数据兼容：缺少列的旧 audit_records 表，audit list 正常显示"""
        work = e2e_workdir("audit_legacy_compat")
        db_path = work / ".contract_pack.db"

        conn = sqlite3.connect(str(db_path))
        try:
            conn.executescript(
                """
                DROP TABLE IF EXISTS audit_records;
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
            old_ts = "2024-01-15T10:30:00"
            conn.execute(
                """INSERT INTO audit_records
                   (id, command_type, operator, started_at, finished_at, result_status,
                    params_summary, config_summary, batch_id, package_names,
                    file_count, error_count, error_summary, detail_ref)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    "legacy-record-001",
                    "run",
                    "leguser",
                    old_ts,
                    old_ts,
                    "success",
                    "{}",
                    "{}",
                    None,
                    '["pkg1"]',
                    3,
                    0,
                    "",
                    "{}",
                ),
            )
            conn.commit()
        finally:
            conn.close()

        r_list = run("contract-pack -c contract_pack.yaml audit list -n 100", cwd=work)
        assert r_list.returncode == 0
        assert "审计记录" in r_list.stdout
        assert "leguser" in r_list.stdout
        assert "run" in r_list.stdout
        assert "success" in r_list.stdout.lower() or "SUCCESS" in r_list.stdout

        r_show = run("contract-pack -c contract_pack.yaml audit show legacy-record-001", cwd=work)
        assert r_show.returncode == 0
        assert "legacy-record-001" in r_show.stdout
        assert "leguser" in r_show.stdout
        assert "run" in r_show.stdout

        json_out = work / "legacy_export.json"
        r_exp = run(f'contract-pack -c contract_pack.yaml audit export -f json -o "{json_out}"', cwd=work)
        assert r_exp.returncode == 0
        assert json_out.exists()
        with open(json_out, "r", encoding="utf-8") as f:
            jdata = json.load(f)
        assert len(jdata) >= 1
        legacy = [x for x in jdata if x.get("id") == "legacy-record-001"]
        assert len(legacy) == 1
        assert legacy[0]["command_type"] == "run"
        assert legacy[0]["operator"] == "leguser"


# ---------------------------------------------------------------------------
# 6. 审计关闭
# ---------------------------------------------------------------------------

class TestAuditDisabled:
    def test_disabled_audit_writes_nothing(self, e2e_workdir):
        """审计关闭：配置 audit.enabled=false，执行命令不写入审计"""
        work = e2e_workdir("audit_disabled", audit_enabled=False)
        db_path = work / ".contract_pack.db"

        run("contract-pack -c contract_pack.yaml dry-run", cwd=work)
        run("contract-pack -c contract_pack.yaml run", cwd=work)

        missing = work / "no_file.json"
        run(f'contract-pack -c contract_pack.yaml template import -f json -i "{missing}"', cwd=work)

        count = count_audit_rows(db_path)
        assert count == 0, f"审计关闭时 audit_records 无记录 (实际 {count})"

        r_list = run("contract-pack -c contract_pack.yaml audit list -n 100", cwd=work)
        assert r_list.returncode == 0
        assert (
            "审计功能已关闭" in r_list.stdout
            or "暂无" in r_list.stdout
            or "不可用" in r_list.stdout
        )

        r_show = run("contract-pack -c contract_pack.yaml audit show any-id", cwd=work)
        assert r_show.returncode != 0 or "关闭" in r_show.stdout or "不可用" in r_show.stdout

        out = work / "should_not_exist.json"
        r_exp = run(f'contract-pack -c contract_pack.yaml audit export -f json -o "{out}"', cwd=work)
        assert r_exp.returncode != 0 or "关闭" in r_exp.stdout or "不可用" in r_exp.stdout


# ---------------------------------------------------------------------------
# 7. 失败操作也留流水
# ---------------------------------------------------------------------------

class TestFailedOperationTrail:
    def test_failed_template_import_leaves_audit(self, e2e_workdir):
        """失败的 template import 留流水：导入不存在的文件，audit list 查到 failed 状态"""
        work = e2e_workdir("audit_tpl_import_failed")
        db_path = work / ".contract_pack.db"

        missing_file = work / "definitely_missing.json"
        assert not missing_file.exists()

        r_imp = run(f'contract-pack -c contract_pack.yaml template import -f json -i "{missing_file}"', cwd=work)
        assert r_imp.returncode != 0

        count = count_audit_rows(db_path)
        assert count >= 1, f"失败的 template import 留下至少 1 条审计记录 (实际 {count})"

        conn = sqlite3.connect(str(db_path))
        try:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM audit_records WHERE command_type = ? ORDER BY started_at DESC",
                ("template-import",),
            ).fetchall()
        finally:
            conn.close()
        assert len(rows) >= 1
        rec = rows[0]
        assert rec["result_status"] == "failed", f"记录 result_status=failed (实际 {rec['result_status']})"
        assert rec["operator"] == "e2e_tester"
        assert rec["error_count"] >= 1
        err_summary = rec["error_summary"] or ""
        assert len(err_summary) > 0

        r_list = run("contract-pack -c contract_pack.yaml audit list --result-status failed -n 100", cwd=work)
        assert r_list.returncode == 0


# ---------------------------------------------------------------------------
# 8. audit show 单条记录
# ---------------------------------------------------------------------------

class TestAuditShowSingle:
    def test_show_displays_key_fields(self, e2e_workdir):
        """audit show 单条记录显示：写入一条记录，show 输出包含关键字段"""
        work = e2e_workdir("audit_show_single")
        db_path = work / ".contract_pack.db"

        run("contract-pack -c contract_pack.yaml dry-run", cwd=work)

        conn = sqlite3.connect(str(db_path))
        try:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT id, command_type, operator, started_at, result_status FROM audit_records ORDER BY started_at DESC LIMIT 1"
            ).fetchone()
            assert row is not None
            record_id = row["id"]
            cmd_type = row["command_type"]
            op = row["operator"]
            status = row["result_status"]
        finally:
            conn.close()

        r_show = run(f"contract-pack -c contract_pack.yaml audit show {record_id}", cwd=work)
        assert r_show.returncode == 0
        assert record_id in r_show.stdout
        assert cmd_type in r_show.stdout
        assert op in r_show.stdout
        assert "开始时间" in r_show.stdout
        assert "结果状态" in r_show.stdout
        assert status in r_show.stdout.lower() or status.upper() in r_show.stdout
        assert "文件数" in r_show.stdout
        assert "错误数" in r_show.stdout

        r_bad = run("contract-pack -c contract_pack.yaml audit show non-existent-id-xyz", cwd=work)
        assert r_bad.returncode != 0
        combined = r_bad.stdout + r_bad.stderr
        assert "不存在" in combined or "找不到" in combined or "失败" in combined
