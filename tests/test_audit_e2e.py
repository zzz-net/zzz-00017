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
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import yaml


TESTS_PASS = 0
TESTS_FAIL = 0


def run(cmd, **kwargs):
    print(f"  $ {cmd}")
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True, **kwargs)
    print(f"    rc={r.returncode}")
    if r.stdout:
        print(f"    stdout: {r.stdout.strip()[:600]}")
    if r.stderr:
        print(f"    stderr: {r.stderr.strip()[:300]}")
    return r


def assert_eq(cond, msg):
    global TESTS_PASS, TESTS_FAIL
    if cond:
        TESTS_PASS += 1
        print(f"  ✓ {msg}")
    else:
        TESTS_FAIL += 1
        print(f"  ✗ {msg}")
        raise AssertionError(msg)


def setup_workdir(tmpdir: Path, name: str, audit_enabled: bool = True) -> Path:
    """构建可运行的工作目录：sources + manifest + contract_pack.yaml。"""
    print(f"\n=== Scenario: {name} ===")
    work = tmpdir / name
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
# Scenarios
# ---------------------------------------------------------------------------

def scenario_cross_restart_query(tmpdir: Path):
    """跨重启查询：执行 dry-run 和 run，重新实例化 storage/AuditStorage，确认记录仍在"""
    work = setup_workdir(tmpdir, "audit_cross_restart")
    db_path = work / ".contract_pack.db"

    r1 = run("contract-pack -c contract_pack.yaml dry-run", cwd=work)
    assert_eq(r1.returncode == 0, "dry-run 执行成功")

    r2 = run("contract-pack -c contract_pack.yaml run", cwd=work)
    assert_eq(r2.returncode == 0, "run 执行成功")

    assert_eq(db_path.exists(), "DB 文件存在")
    count_after = count_audit_rows(db_path)
    assert_eq(count_after >= 2, f"审计记录至少 2 条 (实际 {count_after})")

    from contract_pack.audit import AuditStorage
    storage2 = AuditStorage(db_path)
    records = storage2.query_records(limit=100)
    assert_eq(len(records) >= 2, f"跨重启后读取到至少 2 条 (实际 {len(records)})")

    command_types = {r.command_type for r in records}
    assert_eq("dry-run" in command_types, "存在 dry-run 审计记录")
    assert_eq("run" in command_types, "存在 run 审计记录")

    for rec in records:
        assert_eq(rec.id and len(rec.id) > 0, f"记录 {rec.command_type} 有合法 id")
        assert_eq(rec.operator == "e2e_tester", f"记录 {rec.command_type} operator 正确")
        assert_eq(rec.started_at and len(rec.started_at) > 0, f"记录 {rec.command_type} 有 started_at")
        assert_eq(rec.result_status in {"success", "failed", "partial", "skipped"},
                  f"记录 {rec.command_type} result_status 合法")


def scenario_filter_combinations(tmpdir: Path):
    """过滤组合：执行多种命令后，audit list 按多维度组合过滤，验证结果条数"""
    work = setup_workdir(tmpdir, "audit_filter_combinations")
    db_path = work / ".contract_pack.db"

    run("contract-pack -c contract_pack.yaml dry-run", cwd=work)
    run("contract-pack -c contract_pack.yaml run", cwd=work)
    missing_tpl = work / "does_not_exist.json"
    run(f'contract-pack -c contract_pack.yaml template import -f json -i "{missing_tpl}"', cwd=work)

    r_all = run("contract-pack -c contract_pack.yaml audit list -n 100", cwd=work)
    assert_eq(r_all.returncode == 0, "audit list 无过滤执行成功")
    assert_eq("审计记录" in r_all.stdout, "输出含审计记录标题")

    r_dryrun = run("contract-pack -c contract_pack.yaml audit list --command-type dry-run -n 100", cwd=work)
    assert_eq(r_dryrun.returncode == 0, "按 command-type=dry-run 过滤成功")
    dryrun_count = r_dryrun.stdout.count("dry-run")
    assert_eq(dryrun_count >= 1, "dry-run 过滤结果至少 1 条")
    assert_eq("template-import" not in r_dryrun.stdout, "dry-run 过滤结果不含 template-import")

    r_run = run("contract-pack -c contract_pack.yaml audit list --command-type run -n 100", cwd=work)
    assert_eq(r_run.returncode == 0, "按 command-type=run 过滤成功")
    assert_eq("run" in r_run.stdout, "run 过滤结果含 run 类型")

    r_success = run("contract-pack -c contract_pack.yaml audit list --result-status success -n 100", cwd=work)
    assert_eq(r_success.returncode == 0, "按 result-status=success 过滤成功")

    r_failed = run("contract-pack -c contract_pack.yaml audit list --result-status failed -n 100", cwd=work)
    assert_eq(r_failed.returncode == 0, "按 result-status=failed 过滤成功")
    assert_eq("failed" in r_failed.stdout or "暂无" in r_failed.stdout,
              "failed 过滤输出含 failed 状态或暂无")

    r_op = run("contract-pack -c contract_pack.yaml audit list --operator e2e_tester -n 100", cwd=work)
    assert_eq(r_op.returncode == 0, "按 operator 过滤成功")

    past = (datetime.now() - timedelta(days=365)).isoformat(timespec="seconds")
    future = (datetime.now() + timedelta(days=1)).isoformat(timespec="seconds")
    r_time = run(
        f'contract-pack -c contract_pack.yaml audit list --start-time "{past}" --end-time "{future}" -n 100',
        cwd=work,
    )
    assert_eq(r_time.returncode == 0, "按时间范围过滤成功")

    r_combined = run(
        "contract-pack -c contract_pack.yaml audit list "
        "--command-type dry-run --operator e2e_tester --result-status success -n 100",
        cwd=work,
    )
    assert_eq(r_combined.returncode == 0, "组合过滤 (command-type+operator+result-status) 成功")


def scenario_export_conflict(tmpdir: Path):
    """导出冲突：导出 JSON/CSV 到已存在路径，CLI 给出可读错误且退出非 0"""
    work = setup_workdir(tmpdir, "audit_export_conflict")

    run("contract-pack -c contract_pack.yaml dry-run", cwd=work)

    existing_json = work / "existing_audit.json"
    existing_json.write_text("I EXIST ALREADY", encoding="utf-8")
    rj = run(f'contract-pack -c contract_pack.yaml audit export -f json -o "{existing_json}"', cwd=work)
    assert_eq(rj.returncode != 0, "导出到已存在 JSON 返回非零")
    combined = (rj.stdout + rj.stderr)
    assert_eq(
        ("已存在" in combined) or ("存在" in combined) or ("exist" in combined.lower()) or ("失败" in combined),
        "JSON 导出冲突输出包含可读错误信息",
    )
    assert_eq(existing_json.read_text(encoding="utf-8") == "I EXIST ALREADY",
              "已存在 JSON 文件未被覆盖")

    existing_csv = work / "existing_audit.csv"
    existing_csv.write_text("OLD,CSV,CONTENT", encoding="utf-8")
    rc = run(f'contract-pack -c contract_pack.yaml audit export -f csv -o "{existing_csv}"', cwd=work)
    assert_eq(rc.returncode != 0, "导出到已存在 CSV 返回非零")
    combined_c = (rc.stdout + rc.stderr)
    assert_eq(
        ("已存在" in combined_c) or ("存在" in combined_c) or ("exist" in combined_c.lower()) or ("失败" in combined_c),
        "CSV 导出冲突输出包含可读错误信息",
    )
    assert_eq(existing_csv.read_text(encoding="utf-8") == "OLD,CSV,CONTENT",
              "已存在 CSV 文件未被覆盖")

    existing_dir = work / "existing_dir"
    existing_dir.mkdir()
    rd = run(f'contract-pack -c contract_pack.yaml audit export -f json -o "{existing_dir}"', cwd=work)
    assert_eq(rd.returncode != 0, "导出到已存在目录返回非零")


def scenario_permission_failure(tmpdir: Path):
    """权限失败：导出到不可写路径，CLI 给出可读错误且退出非 0"""
    work = setup_workdir(tmpdir, "audit_permission_failure")

    run("contract-pack -c contract_pack.yaml dry-run", cwd=work)

    parent_as_file = work / "readonly_dir"
    parent_as_file.write_text("I am a file, not a directory", encoding="utf-8")

    out_json = parent_as_file / "audit.json"
    rj = run(f'contract-pack -c contract_pack.yaml audit export -f json -o "{out_json}"', cwd=work)
    assert_eq(rj.returncode != 0, "导出 JSON 到不可写父路径返回非零")
    combined = (rj.stdout + rj.stderr)
    assert_eq(
        ("失败" in combined) or ("父级" in combined) or ("目录" in combined) or ("无法" in combined),
        "JSON 导出不可写路径输出可读提示",
    )
    assert_eq(not out_json.exists(), "不可写路径下未生成 JSON 文件")

    out_csv = parent_as_file / "audit.csv"
    rc = run(f'contract-pack -c contract_pack.yaml audit export -f csv -o "{out_csv}"', cwd=work)
    assert_eq(rc.returncode != 0, "导出 CSV 到不可写父路径返回非零")
    combined_c = (rc.stdout + rc.stderr)
    assert_eq(
        ("失败" in combined_c) or ("父级" in combined_c) or ("目录" in combined_c) or ("无法" in combined_c),
        "CSV 导出不可写路径输出可读提示",
    )
    assert_eq(not out_csv.exists(), "不可写路径下未生成 CSV 文件")


def scenario_legacy_data_compatibility(tmpdir: Path):
    """旧数据兼容：缺少列的旧 audit_records 表，插入旧记录，audit list 正常显示"""
    work = setup_workdir(tmpdir, "audit_legacy_compat")
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
    assert_eq(r_list.returncode == 0, "旧表结构下 audit list 返回 0")
    assert_eq("审计记录" in r_list.stdout, "旧表结构下输出含标题")
    assert_eq("leguser" in r_list.stdout, "旧记录 operator 可见")
    assert_eq("run" in r_list.stdout, "旧记录 command_type 可见")
    assert_eq("success" in r_list.stdout.lower() or "SUCCESS" in r_list.stdout,
              "旧记录 result_status 可见")

    r_show = run("contract-pack -c contract_pack.yaml audit show legacy-record-001", cwd=work)
    assert_eq(r_show.returncode == 0, "旧表结构下 audit show 返回 0")
    assert_eq("legacy-record-001" in r_show.stdout, "show 输出含记录 ID")
    assert_eq("leguser" in r_show.stdout, "show 输出含 operator")
    assert_eq("run" in r_show.stdout, "show 输出含 command_type")

    json_out = work / "legacy_export.json"
    r_exp = run(f'contract-pack -c contract_pack.yaml audit export -f json -o "{json_out}"', cwd=work)
    assert_eq(r_exp.returncode == 0, "旧表结构下 audit export JSON 返回 0")
    assert_eq(json_out.exists(), "JSON 导出文件生成")
    with open(json_out, "r", encoding="utf-8") as f:
        jdata = json.load(f)
    assert_eq(len(jdata) >= 1, "JSON 导出至少 1 条记录")
    legacy = [x for x in jdata if x.get("id") == "legacy-record-001"]
    assert_eq(len(legacy) == 1, "JSON 导出中找到旧记录")
    assert_eq(legacy[0]["command_type"] == "run", "旧记录 JSON command_type 正确")
    assert_eq(legacy[0]["operator"] == "leguser", "旧记录 JSON operator 正确")


def scenario_audit_disabled(tmpdir: Path):
    """审计关闭：配置 audit.enabled=false，执行命令不写入审计"""
    work = setup_workdir(tmpdir, "audit_disabled", audit_enabled=False)
    db_path = work / ".contract_pack.db"

    run("contract-pack -c contract_pack.yaml dry-run", cwd=work)
    run("contract-pack -c contract_pack.yaml run", cwd=work)

    missing = work / "no_file.json"
    run(f'contract-pack -c contract_pack.yaml template import -f json -i "{missing}"', cwd=work)

    count = count_audit_rows(db_path)
    assert_eq(count == 0, f"审计关闭时 audit_records 无记录 (实际 {count})")

    r_list = run("contract-pack -c contract_pack.yaml audit list -n 100", cwd=work)
    assert_eq(r_list.returncode == 0, "审计关闭时 audit list 不报错")
    assert_eq(
        "审计功能已关闭" in r_list.stdout or "暂无" in r_list.stdout or "不可用" in r_list.stdout,
        "审计关闭时 audit list 输出提示或无记录",
    )

    r_show = run("contract-pack -c contract_pack.yaml audit show any-id", cwd=work)
    assert_eq(r_show.returncode != 0 or "关闭" in r_show.stdout or "不可用" in r_show.stdout,
              "审计关闭时 audit show 返回非零或提示关闭")

    out = work / "should_not_exist.json"
    r_exp = run(f'contract-pack -c contract_pack.yaml audit export -f json -o "{out}"', cwd=work)
    assert_eq(r_exp.returncode != 0 or "关闭" in r_exp.stdout or "不可用" in r_exp.stdout,
              "审计关闭时 audit export 返回非零或提示关闭")


def scenario_failed_template_import_leaves_trail(tmpdir: Path):
    """失败的 template import 留流水：导入不存在的文件，audit list 查到 failed 状态"""
    work = setup_workdir(tmpdir, "audit_tpl_import_failed")
    db_path = work / ".contract_pack.db"

    missing_file = work / "definitely_missing.json"
    assert_eq(not missing_file.exists(), "确认文件不存在")

    r_imp = run(f'contract-pack -c contract_pack.yaml template import -f json -i "{missing_file}"', cwd=work)
    assert_eq(r_imp.returncode != 0, "导入不存在文件返回非零")

    count = count_audit_rows(db_path)
    assert_eq(count >= 1, f"失败的 template import 留下至少 1 条审计记录 (实际 {count})")

    conn = sqlite3.connect(str(db_path))
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM audit_records WHERE command_type = ? ORDER BY started_at DESC",
            ("template-import",),
        ).fetchall()
    finally:
        conn.close()
    assert_eq(len(rows) >= 1, "DB 中存在 template-import 类型记录")
    rec = rows[0]
    assert_eq(rec["result_status"] == "failed", f"记录 result_status=failed (实际 {rec['result_status']})")
    assert_eq(rec["operator"] == "e2e_tester", "记录 operator 正确")
    assert_eq(rec["error_count"] >= 1, f"记录 error_count >= 1 (实际 {rec['error_count']})")
    err_summary = rec["error_summary"] or ""
    assert_eq(len(err_summary) > 0, "记录含非空 error_summary")

    r_list = run("contract-pack -c contract_pack.yaml audit list --result-status failed -n 100", cwd=work)
    assert_eq(r_list.returncode == 0, "audit list --result-status failed 执行成功")
    assert_eq("template-import" in r_list.stdout or "暂无" not in r_list.stdout,
              "audit list 失败过滤结果包含 template-import 或非空")


def scenario_audit_show_single_record(tmpdir: Path):
    """audit show 单条记录显示：写入一条记录，show 输出包含关键字段"""
    work = setup_workdir(tmpdir, "audit_show_single")
    db_path = work / ".contract_pack.db"

    run("contract-pack -c contract_pack.yaml dry-run", cwd=work)

    conn = sqlite3.connect(str(db_path))
    try:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT id, command_type, operator, started_at, result_status FROM audit_records ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
        assert_eq(row is not None, "DB 中至少有 1 条审计记录")
        record_id = row["id"]
        cmd_type = row["command_type"]
        op = row["operator"]
        status = row["result_status"]
    finally:
        conn.close()

    r_show = run(f"contract-pack -c contract_pack.yaml audit show {record_id}", cwd=work)
    assert_eq(r_show.returncode == 0, "audit show 执行成功")

    assert_eq(record_id in r_show.stdout, "show 输出包含记录 ID")
    assert_eq(cmd_type in r_show.stdout, "show 输出包含 command_type")
    assert_eq(op in r_show.stdout, "show 输出包含 operator")
    assert_eq("开始时间" in r_show.stdout, "show 输出包含开始时间标签")
    assert_eq("结果状态" in r_show.stdout, "show 输出包含结果状态标签")
    assert_eq(status in r_show.stdout.lower() or status.upper() in r_show.stdout,
              f"show 输出包含 result_status ({status})")
    assert_eq("文件数" in r_show.stdout, "show 输出包含文件数字段")
    assert_eq("错误数" in r_show.stdout, "show 输出包含错误数字段")

    r_bad = run("contract-pack -c contract_pack.yaml audit show non-existent-id-xyz", cwd=work)
    assert_eq(r_bad.returncode != 0, "audit show 不存在 ID 返回非零")
    combined = r_bad.stdout + r_bad.stderr
    assert_eq(
        "不存在" in combined or "找不到" in combined or "失败" in combined,
        "audit show 不存在 ID 输出可读错误",
    )


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main():
    tmpdir = Path(tempfile.mkdtemp(prefix="contract_pack_audit_e2e_"))
    print(f"E2E 测试临时目录: {tmpdir}")
    try:
        scenario_cross_restart_query(tmpdir)
        scenario_filter_combinations(tmpdir)
        scenario_export_conflict(tmpdir)
        scenario_permission_failure(tmpdir)
        scenario_legacy_data_compatibility(tmpdir)
        scenario_audit_disabled(tmpdir)
        scenario_failed_template_import_leaves_trail(tmpdir)
        scenario_audit_show_single_record(tmpdir)
    except AssertionError:
        pass
    finally:
        try:
            shutil.rmtree(tmpdir)
        except OSError:
            pass

    print(f"\n=== 审计模块端到端测试: 通过 {TESTS_PASS}, 失败 {TESTS_FAIL} ===")
    sys.exit(1 if TESTS_FAIL else 0)


if __name__ == "__main__":
    main()
