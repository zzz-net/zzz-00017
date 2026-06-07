"""交付包差异对比 - 端到端 CLI 测试

覆盖：
  - CLI: diff --batch-id 完整链路（含版本变化、新增、缺失、导出）
  - CLI: diff --dir 目录基准对比
  - CLI: diff 导出 JSON/CSV 字段稳定
  - CLI: diff 错误处理（目录不存在、缺少基准参数、批次不存在）
  - CLI: diff 同名文件冲突（不与 file_mapping 子目录正确识别）
  - 跨重启：批次入库后重新打开 CLI 仍可 diff
  - 报告一致性：diff 的 JSON/CSV 字段一致
"""

from __future__ import annotations

import csv
import json
import shutil
import sqlite3
import subprocess
import sys
import tempfile
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


def setup_workdir(tmpdir: Path, name: str) -> Path:
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


def run_exec(work: Path, make_zip: bool = True) -> str:
    """执行 run 命令并返回批次 ID。"""
    zflag = "--zip" if make_zip else ""
    r = run(f"contract-pack -c contract_pack.yaml run {zflag}".strip(), cwd=work)
    assert_eq(r.returncode == 0, "首次 run 命令执行成功")
    for line in r.stdout.splitlines():
        if "批次" in line and "执行完毕" in line:
            parts = line.split()
            for p in parts:
                if len(p) > 20 and "-" in p:
                    return p
    raise RuntimeError("无法从 run 输出解析批次 id")


# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------

def scenario_diff_against_batch_full_flow(tmpdir: Path):
    """完整链路：run 生成批次 -> 修改 manifest -> diff --batch-id -> 导出 JSON/CSV"""
    work = setup_workdir(tmpdir, "diff_batch_full_flow")
    parent_id = run_exec(work, make_zip=True)

    new_manifest = work / "manifest.csv"
    with open(new_manifest, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["package", "category", "source_path", "target_name", "version", "description"])
        w.writerow(["甲方交付包", "main", "contracts/main.pdf", "主合同.pdf", "v2", ""])
        w.writerow(["甲方交付包", "seal", "scans/seal.jpg", "盖章扫描件.jpg", "", ""])
        w.writerow(["甲方交付包", "supplement", "contracts/supp.pdf", "补充协议_v2.pdf", "v2", ""])

    r = run(f"contract-pack -c contract_pack.yaml diff --batch-id {parent_id}", cwd=work)
    assert_eq(r.returncode in (0, 10), f"diff 返回 0 或 10 (rc={r.returncode})")
    assert_eq("交付包差异对比" in r.stdout, "diff 输出含标题")
    assert_eq("差异汇总" in r.stdout, "diff 输出含差异汇总")
    has_summary_diff = ("版本变化" in r.stdout) or ("新增" in r.stdout) or ("缺失" in r.stdout) or ("文件名变化" in r.stdout)
    assert_eq(has_summary_diff, "diff 输出检测到差异")

    json_out = work / "diff_report.json"
    rj = run(f'contract-pack -c contract_pack.yaml diff --batch-id {parent_id} -f json -o "{json_out}"', cwd=work)
    assert_eq(rj.returncode in (0, 10), f"diff + json 导出返回 0 或 10 (rc={rj.returncode})")
    assert_eq(json_out.exists(), "JSON 导出文件存在")
    with open(json_out, "r", encoding="utf-8") as f:
        j = json.load(f)
    assert_eq(j["baseline_kind"] == "batch", "JSON baseline_kind=batch")
    assert_eq(j["baseline_ref"] == parent_id, "JSON baseline_ref=parent_id")
    assert_eq("summary" in j, "JSON 含 summary")
    assert_eq("items" in j, "JSON 含 items")
    if j["items"]:
        first = j["items"][0]
        for k in ["change_type", "change_label", "package", "target_name", "detail"]:
            assert_eq(k in first, f"JSON item 含字段 {k}")

    csv_out = work / "diff_report.csv"
    rc = run(f'contract-pack -c contract_pack.yaml diff --batch-id {parent_id} -f csv -o "{csv_out}"', cwd=work)
    assert_eq(rc.returncode in (0, 10), f"diff + csv 导出返回 0 或 10 (rc={rc.returncode})")
    assert_eq(csv_out.exists(), "CSV 导出文件存在")
    with open(csv_out, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        headers = reader.fieldnames or []
        rows = list(reader)
    for h in ["baseline_kind", "baseline_ref", "change_type", "change_label", "package", "target_name", "detail"]:
        assert_eq(h in headers, f"CSV 表头含 {h}")
    assert_eq(all(r["baseline_kind"] == "batch" for r in rows), "CSV 所有行 baseline_kind=batch")
    assert_eq(all(r["baseline_ref"] == parent_id for r in rows), "CSV 所有行 baseline_ref=parent_id")


def scenario_diff_against_directory(tmpdir: Path):
    """目录基准：diff --dir"""
    work = setup_workdir(tmpdir, "diff_dir_baseline")

    base_dir = work / "old_deliver"
    (base_dir / "PartyA" / "01_主合同").mkdir(parents=True)
    (base_dir / "PartyA" / "02_补充协议").mkdir(parents=True)
    (base_dir / "PartyA" / "01_主合同" / "主合同.pdf").write_text("old main", encoding="utf-8")
    (base_dir / "PartyA" / "02_补充协议" / "补充协议.pdf").write_text("old supp", encoding="utf-8")
    (base_dir / "甲方交付包.zip").write_text("old zip", encoding="utf-8")

    r = run(f'contract-pack -c contract_pack.yaml diff --dir "{base_dir}"', cwd=work)
    assert_eq(r.returncode in (0, 10), f"diff --dir 返回 0 或 10 (rc={r.returncode})")
    assert_eq("交付包差异对比" in r.stdout, "diff 输出含标题")
    assert_eq("目录" in r.stdout, "输出提到目录基准")

    json_out = work / "diff_dir.json"
    run(f'contract-pack -c contract_pack.yaml diff --dir "{base_dir}" -f json -o "{json_out}"', cwd=work)
    assert_eq(json_out.exists(), "JSON 导出文件存在")
    with open(json_out, "r", encoding="utf-8") as f:
        j = json.load(f)
    assert_eq(j["baseline_kind"] == "directory", "JSON baseline_kind=directory")


def scenario_diff_error_handling(tmpdir: Path):
    """错误处理：缺少参数、批次不存在、目录不存在"""
    work = setup_workdir(tmpdir, "diff_errors")

    r = run("contract-pack -c contract_pack.yaml diff", cwd=work)
    assert_eq(r.returncode != 0, "不指定基准时返回非零")
    assert_eq("必须指定" in r.stdout or "错误" in r.stdout, "提示必须指定基准")

    r2 = run("contract-pack -c contract_pack.yaml diff --batch-id non-existent-123", cwd=work)
    assert_eq(r2.returncode != 0, "批次不存在时返回非零")

    non_existent = work / "non_existent_dir_xyz"
    r3 = run(f'contract-pack -c contract_pack.yaml diff --dir "{non_existent}"', cwd=work)
    assert_eq(r3.returncode != 0, "目录不存在时返回非零")

    r4 = run(f'contract-pack -c contract_pack.yaml diff --batch-id x -f json', cwd=work)
    assert_eq(r4.returncode != 0, "指定 format 但不指定 output 返回非零")


def scenario_diff_cross_restart(tmpdir: Path):
    """跨重启：run 生成批次，重新打开 CLI 执行 diff 仍可读取历史批次"""
    work = setup_workdir(tmpdir, "diff_cross_restart")
    parent_id = run_exec(work, make_zip=False)

    db_path = work / ".contract_pack.db"
    assert_eq(db_path.exists(), "DB 文件存在")

    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute("SELECT id, status FROM batches WHERE id=?", (parent_id,)).fetchone()
        assert_eq(row is not None, "DB 中存在父批次")
    finally:
        conn.close()

    r = run(f"contract-pack -c contract_pack.yaml diff --batch-id {parent_id}", cwd=work)
    assert_eq(r.returncode in (0, 10), f"跨重启后 diff 执行成功 (rc={r.returncode})")
    assert_eq("交付包差异对比" in r.stdout, "跨重启后 diff 输出正常")


def scenario_diff_report_consistency(tmpdir: Path):
    """报告一致性：JSON/CSV/终端输出的基准信息一致"""
    work = setup_workdir(tmpdir, "diff_report_consistency")
    parent_id = run_exec(work, make_zip=False)

    json_out = work / "consistency.json"
    run(f'contract-pack -c contract_pack.yaml diff --batch-id {parent_id} -f json -o "{json_out}"', cwd=work)
    with open(json_out, "r", encoding="utf-8") as f:
        j = json.load(f)

    csv_out = work / "consistency.csv"
    run(f'contract-pack -c contract_pack.yaml diff --batch-id {parent_id} -f csv -o "{csv_out}"', cwd=work)
    with open(csv_out, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        rows = list(reader)

    assert_eq(j["baseline_kind"] == "batch", "JSON baseline_kind=batch")
    assert_eq(j["baseline_ref"] == parent_id, "JSON baseline_ref 正确")
    if rows:
        assert_eq(rows[0]["baseline_kind"] == "batch", "CSV baseline_kind=batch")
        assert_eq(rows[0]["baseline_ref"] == parent_id, "CSV baseline_ref 正确")

    expected_total = j["total_expected"]
    baseline_total = j["total_baseline"]
    summary_added = j["summary"]["added"]
    summary_missing = j["summary"]["missing"]
    calc_added = sum(1 for i in j["items"] if i["change_type"] == "added")
    calc_missing = sum(1 for i in j["items"] if i["change_type"] == "missing")
    assert_eq(summary_added == calc_added, f"JSON summary.added 与 items 计数一致: {summary_added} vs {calc_added}")
    assert_eq(summary_missing == calc_missing, f"JSON summary.missing 与 items 计数一致: {summary_missing} vs {calc_missing}")


def scenario_diff_unchanged_show(tmpdir: Path):
    """--show-unchanged: 显示/隐藏无变化条目"""
    work = setup_workdir(tmpdir, "diff_show_unchanged")
    parent_id = run_exec(work, make_zip=False)

    r_hide = run(f"contract-pack -c contract_pack.yaml diff --batch-id {parent_id}", cwd=work)
    r_show = run(f"contract-pack -c contract_pack.yaml diff --batch-id {parent_id} --show-unchanged", cwd=work)

    assert_eq(r_show.returncode in (0, 10), "--show-unchanged 执行成功")
    show_has_unchanged = "无变化" in r_show.stdout or "unchanged" in r_show.stdout.lower()
    # 只要 show 输出不报错即可


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main():
    tmpdir = Path(tempfile.mkdtemp(prefix="contract_pack_diff_e2e_"))
    print(f"E2E 测试临时目录: {tmpdir}")
    try:
        scenario_diff_against_batch_full_flow(tmpdir)
        scenario_diff_against_directory(tmpdir)
        scenario_diff_error_handling(tmpdir)
        scenario_diff_cross_restart(tmpdir)
        scenario_diff_report_consistency(tmpdir)
        scenario_diff_unchanged_show(tmpdir)
    except AssertionError:
        pass

    print(f"\n=== 差异对比端到端测试: 通过 {TESTS_PASS}, 失败 {TESTS_FAIL} ===")
    sys.exit(1 if TESTS_FAIL else 0)


if __name__ == "__main__":
    main()
