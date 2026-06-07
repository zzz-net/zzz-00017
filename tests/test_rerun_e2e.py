"""按历史批次重跑 - 端到端测试

覆盖：
  - CLI: run 之后 rerun --output-root 完整链路（含 --zip）
  - CLI: rerun 默认阻止覆盖已有交付文件和 zip
  - CLI: rerun --force 跳过预检
  - CLI: list/show 可见父批次和重跑参数
  - CLI: export JSON/CSV 含 parent_batch_id、rerun_params 和错误信息
  - CLI: rerun 预检失败时，失败批次入库且含 parent_batch_id/错误信息
  - 跨重启：重跑后重新打开 CLI，list/show/export 仍可见父批次关联
  - rollback: CLI rollback 重跑批次不碰原批次产物
  - 报告一致性：JSON/CSV/show 三者的 parent_batch_id、错误信息一致
"""

from __future__ import annotations

import csv
import json
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
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


def parse_rerun_batch_id(stdout: str) -> str:
    """从 rerun 命令 stdout 中解析批次 ID。"""
    for line in stdout.splitlines():
        if "重跑批次" in line:
            parts = line.split()
            for p in parts:
                if len(p) > 20 and "-" in p:
                    return p
    raise RuntimeError(f"无法从 rerun 输出解析批次 id:\n{stdout}")


# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------

def scenario_rerun_full_flow(tmpdir: Path):
    """完整链路：run -> rerun --output-root --zip，验证产物、list/show、导出。"""
    work = setup_workdir(tmpdir, "rerun_full_flow")
    parent_id = run_exec(work, make_zip=True)

    orig_main = work / "deliver" / "PartyA" / "主合同.pdf"
    orig_zip = work / "deliver" / "甲方交付包.zip"
    assert_eq(orig_main.exists(), "原批次主合同存在")
    assert_eq(orig_zip.exists(), "原批次 zip 存在")

    new_root = work / "deliver_rerun"
    r = run(
        f'contract-pack -c contract_pack.yaml rerun {parent_id} --output-root "{new_root}" --zip',
        cwd=work,
    )
    assert_eq(r.returncode == 0, "rerun 返回 0")
    rerun_id = parse_rerun_batch_id(r.stdout)
    assert_eq(rerun_id != parent_id, "重跑批次 ID 与父批次不同")
    assert_eq(parent_id in r.stdout, "rerun 输出提到父批次 ID")

    rerun_main = new_root / "PartyA" / "主合同.pdf"
    rerun_zip = new_root / "甲方交付包.zip"
    assert_eq(rerun_main.exists(), "重跑主合同存在于新目录")
    assert_eq(rerun_zip.exists(), "重跑 zip 存在于新目录")
    assert_eq(orig_main.exists(), "原批次主合同未被删除或覆盖")
    assert_eq(orig_zip.exists(), "原批次 zip 未被删除或覆盖")

    list_out_raw = run("contract-pack -c contract_pack.yaml list -n 20", cwd=work).stdout
    assert_eq("父批次" in list_out_raw, "list 表头含'父批次'列")

    db_path = work / ".contract_pack.db"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("SELECT id, parent_batch_id FROM batches ORDER BY started_at DESC LIMIT 10").fetchall()
        ids_in_db = {r["id"] for r in rows}
        parent_ids_in_db = {r["parent_batch_id"] for r in rows if r["parent_batch_id"]}
    finally:
        conn.close()
    assert_eq(parent_id in ids_in_db, "DB 中存在父批次")
    assert_eq(rerun_id in ids_in_db, "DB 中存在重跑批次")
    assert_eq(parent_id in parent_ids_in_db, "DB 中重跑批次正确关联了父批次")

    show_out = run(f"contract-pack -c contract_pack.yaml show {rerun_id}", cwd=work).stdout
    assert_eq(f"父批次: {parent_id}" in show_out, "show 输出含父批次 ID")
    assert_eq("重跑参数" in show_out, "show 输出含重跑参数")
    assert_eq("output_root" in show_out, "show 输出的重跑参数含 output_root")

    json_out = work / "report.json"
    run(f'contract-pack -c contract_pack.yaml export -f json -o "{json_out}" --batch-id {rerun_id}', cwd=work)
    j = json.loads(json_out.read_text(encoding="utf-8"))
    assert_eq(len(j) == 1, "JSON 有 1 条批次")
    assert_eq(j[0]["parent_batch_id"] == parent_id, "JSON 中 parent_batch_id 正确")
    assert_eq(j[0]["rerun_params"]["make_zip"] is True, "JSON 中 rerun_params.make_zip=True")

    csv_out = work / "report.csv"
    run(f'contract-pack -c contract_pack.yaml export -f csv -o "{csv_out}" --batch-id {rerun_id}', cwd=work)
    with open(csv_out, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
    headers = reader.fieldnames or []
    assert_eq("parent_batch_id" in headers, "CSV 表头含 parent_batch_id")
    assert_eq("rerun_params" in headers, "CSV 表头含 rerun_params")
    assert_eq(all(r["parent_batch_id"] == parent_id for r in rows), "CSV 所有行 parent_batch_id 正确")


def scenario_rerun_block_overwrite(tmpdir: Path):
    """默认阻止覆盖：不指定 --output-root 时，重跑预检失败。"""
    work = setup_workdir(tmpdir, "rerun_block_overwrite")
    parent_id = run_exec(work, make_zip=True)

    r = run(f"contract-pack -c contract_pack.yaml rerun {parent_id}", cwd=work)
    assert_eq(r.returncode != 0, "默认不允许覆盖时 rerun 返回非零")
    assert_eq("失败" in r.stdout or "预检" in r.stdout or "错误" in r.stdout,
              "输出提示失败/预检/错误")

    db_path = work / ".contract_pack.db"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("SELECT id, status FROM batches ORDER BY started_at DESC LIMIT 10").fetchall()
        ids_in_db = {r["id"] for r in rows}
    finally:
        conn.close()
    assert_eq(parent_id in ids_in_db, "DB 中仍可见父批次（未被影响）")


def scenario_rerun_force(tmpdir: Path):
    """--force 跳过预检失败继续执行。"""
    work = setup_workdir(tmpdir, "rerun_force")
    parent_id = run_exec(work, make_zip=True)

    r = run(f"contract-pack -c contract_pack.yaml rerun {parent_id} --force", cwd=work)
    assert_eq(r.returncode in (0, 3), f"--force 下 rerun 返回 0 或 3 (rc={r.returncode})")


def scenario_rerun_list_show_cross_restart(tmpdir: Path):
    """跨重启：重跑后再开 CLI，list/show 仍可见父批次关联。"""
    work = setup_workdir(tmpdir, "rerun_cross_restart")
    parent_id = run_exec(work, make_zip=False)

    new_root = work / "deliver_r2"
    r = run(
        f'contract-pack -c contract_pack.yaml rerun {parent_id} --output-root "{new_root}"',
        cwd=work,
    )
    rerun_id = parse_rerun_batch_id(r.stdout)

    db_path = work / ".contract_pack.db"
    assert_eq(db_path.exists(), "DB 文件存在")
    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute(
            "SELECT parent_batch_id, rerun_params FROM batches WHERE id=?", (rerun_id,)
        ).fetchone()
        assert_eq(row is not None, "DB 中有重跑批次行")
        assert_eq(row[0] == parent_id, "DB 中 parent_batch_id 正确")
        rp = json.loads(row[1] or "{}")
        assert_eq(rp.get("output_root") is not None, "DB 中 rerun_params 含 output_root")
    finally:
        conn.close()

    list_out_raw = run("contract-pack -c contract_pack.yaml list -n 20", cwd=work).stdout
    assert_eq("父批次" in list_out_raw, "list 表头含'父批次'列")

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("SELECT id, parent_batch_id FROM batches ORDER BY started_at DESC LIMIT 10").fetchall()
        ids_in_db = {r["id"] for r in rows}
        parent_ids_in_db = {r["parent_batch_id"] for r in rows if r["parent_batch_id"]}
    finally:
        conn.close()
    assert_eq(parent_id in ids_in_db and rerun_id in ids_in_db,
              "跨重启后 DB 可见两个批次")
    assert_eq(parent_id in parent_ids_in_db,
              "跨重启后 DB 中重跑批次仍关联父批次")

    show_out = run(f"contract-pack -c contract_pack.yaml show {rerun_id}", cwd=work).stdout
    assert_eq(f"父批次: {parent_id}" in show_out, "跨重启后 show 可见父批次")


def scenario_rerun_precheck_failure_stored(tmpdir: Path):
    """预检失败：失败批次入库且含 parent_batch_id 和错误信息。"""
    work = setup_workdir(tmpdir, "rerun_precheck_fail")
    parent_id = run_exec(work, make_zip=True)

    run(f"contract-pack -c contract_pack.yaml rerun {parent_id} --zip", cwd=work)

    db_path = work / ".contract_pack.db"
    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute(
            "SELECT id, status, parent_batch_id, error FROM batches WHERE parent_batch_id=?",
            (parent_id,),
        ).fetchall()
        assert_eq(len(rows) >= 1, "DB 中至少有一个子批次")
        failed = [r for r in rows if r[1] in ("failed",)]
        assert_eq(len(failed) >= 1, "DB 中有失败状态的子批次")
        assert_eq(failed[0][2] == parent_id, "失败子批次 parent_batch_id 正确")
        assert_eq(len(failed[0][3] or "") > 0, "失败子批次有错误信息")
    finally:
        conn.close()


def scenario_rerun_report_consistency(tmpdir: Path):
    """报告一致性：show/JSON/CSV 三者 parent_batch_id 与错误信息一致。"""
    work = setup_workdir(tmpdir, "rerun_report_consistency")
    parent_id = run_exec(work, make_zip=False)

    run(f"contract-pack -c contract_pack.yaml rerun {parent_id}", cwd=work)

    db_path = work / ".contract_pack.db"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT id, error FROM batches WHERE parent_batch_id=? ORDER BY started_at DESC LIMIT 1",
            (parent_id,),
        ).fetchone()
        rerun_id = row["id"]
        db_error = row["error"] or ""
    finally:
        conn.close()

    show_out = run(f"contract-pack -c contract_pack.yaml show {rerun_id}", cwd=work).stdout
    show_has_parent = f"父批次: {parent_id}" in show_out
    show_has_error = (db_error == "") or (db_error[:30] in show_out)

    json_out = work / "consistency.json"
    run(f'contract-pack -c contract_pack.yaml export -f json -o "{json_out}" --batch-id {rerun_id}', cwd=work)
    j = json.loads(json_out.read_text(encoding="utf-8"))
    json_parent = j[0]["parent_batch_id"]
    json_error = j[0]["error"] or ""

    csv_out = work / "consistency.csv"
    run(f'contract-pack -c contract_pack.yaml export -f csv -o "{csv_out}" --batch-id {rerun_id}', cwd=work)
    with open(csv_out, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
    csv_parent = rows[0]["parent_batch_id"] if rows else ""
    csv_error = (rows[0]["batch_error"] if rows else "") or ""

    assert_eq(show_has_parent, "show 输出含正确的 parent_batch_id")
    assert_eq(json_parent == parent_id, "JSON parent_batch_id 与 DB 一致")
    assert_eq(csv_parent == parent_id, "CSV parent_batch_id 与 DB 一致")
    assert_eq(json_error == db_error, f"JSON error 与 DB 一致: '{json_error}' vs '{db_error}'")
    assert_eq(csv_error == db_error, f"CSV error 与 DB 一致: '{csv_error}' vs '{db_error}'")
    if db_error:
        assert_eq(show_has_error, f"show 输出含错误信息: {db_error[:40]}")


def scenario_rerun_rollback_isolation(tmpdir: Path):
    """CLI rollback 重跑批次 -> 只删除重跑产物，原批次产物保留。"""
    work = setup_workdir(tmpdir, "rerun_rollback_iso")
    parent_id = run_exec(work, make_zip=True)

    new_root = work / "deliver_rerun"
    r = run(
        f'contract-pack -c contract_pack.yaml rerun {parent_id} --output-root "{new_root}" --zip',
        cwd=work,
    )
    rerun_id = parse_rerun_batch_id(r.stdout)

    orig_main = work / "deliver" / "PartyA" / "主合同.pdf"
    orig_zip = work / "deliver" / "甲方交付包.zip"
    rerun_main = new_root / "PartyA" / "主合同.pdf"
    rerun_zip = new_root / "甲方交付包.zip"
    assert_eq(orig_main.exists() and orig_zip.exists(), "rollback 前原批次产物都在")
    assert_eq(rerun_main.exists() and rerun_zip.exists(), "rollback 前重跑产物都在")

    rb = run(f"contract-pack -c contract_pack.yaml rollback {rerun_id}", cwd=work)
    assert_eq(rb.returncode == 0, "rollback 重跑批次返回 0")

    assert_eq(not rerun_main.exists(), "rollback 后重跑主合同被删除")
    assert_eq(not rerun_zip.exists(), "rollback 后重跑 zip 被删除")
    assert_eq(orig_main.exists(), "rollback 后原批次主合同仍在")
    assert_eq(orig_zip.exists(), "rollback 后原批次 zip 仍在")

    show_parent = run(f"contract-pack -c contract_pack.yaml show {parent_id}", cwd=work).stdout
    assert_eq("rolled_back" not in show_parent,
              f"原批次状态不是 rolled_back: {show_parent[:200]}")


def main():
    tmpdir = Path(tempfile.mkdtemp(prefix="contract_pack_rerun_e2e_"))
    print(f"E2E 测试临时目录: {tmpdir}")
    try:
        scenario_rerun_full_flow(tmpdir)
        scenario_rerun_block_overwrite(tmpdir)
        scenario_rerun_force(tmpdir)
        scenario_rerun_list_show_cross_restart(tmpdir)
        scenario_rerun_precheck_failure_stored(tmpdir)
        scenario_rerun_report_consistency(tmpdir)
        scenario_rerun_rollback_isolation(tmpdir)
    except AssertionError:
        pass

    print(f"\n=== 重跑端到端测试: 通过 {TESTS_PASS}, 失败 {TESTS_FAIL} ===")
    sys.exit(1 if TESTS_FAIL else 0)


if __name__ == "__main__":
    main()
