"""合同附件打包 CLI - rollback 回归测试
覆盖：正常回滚、文件缺失、文件被替换、目录占用、zip 被替换。
同时包含一次 CLI 验证。
"""

from __future__ import annotations

import csv
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

import yaml


TESTS_PASS = 0
TESTS_FAIL = 0


def run(cmd, **kwargs):
    print(f"  $ {cmd}")
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True, **kwargs)
    print(f"    rc={r.returncode}")
    if r.stdout:
        print(f"    stdout: {r.stdout.strip()[:400]}")
    if r.stderr:
        print(f"    stderr: {r.stderr.strip()[:200]}")
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


def setup_scenario(tmpdir: Path, scenario_name: str):
    """在临时目录下构建测试环境。"""
    print(f"\n=== Scenario: {scenario_name} ===")
    work = tmpdir / scenario_name
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
        "operator": "tester",
        "manifest": "manifest.csv",
        "source_root": "./sources",
        "db_path": "./.contract_pack.db",
        "allow_overwrite": True,
        "packages": [
            {
                "name": "甲方交付包",
                "output_dir": "./deliver/PartyA",
                "zip_output": "./deliver/甲方交付包.zip",
            }
        ],
    }
    with open(work / "contract_pack.yaml", "w", encoding="utf-8") as f:
        yaml.safe_dump(cfg, f, allow_unicode=True, sort_keys=False)

    return work


def run_exec(work: Path, make_zip=True):
    """执行批次并返回批次 id。"""
    zflag = "--zip" if make_zip else ""
    r = run(f"contract-pack -c contract_pack.yaml run {zflag}".strip(), cwd=work)
    assert_eq(r.returncode == 0, f"run 命令执行成功")
    for line in r.stdout.splitlines():
        if "批次" in line and "执行完毕" in line:
            bid = line.split()[1]
            return bid
    raise RuntimeError("无法从 run 输出中解析批次 id")


def scenario_normal_rollback(tmpdir: Path):
    """Scenario 1: 正常回滚——所有文件指纹匹配，应该全部删除并标记 rolled_back。"""
    work = setup_scenario(tmpdir, "normal_rollback")
    bid = run_exec(work)

    party_a = work / "deliver" / "PartyA"
    zip_path = work / "deliver" / "甲方交付包.zip"
    assert_eq((party_a / "主合同.pdf").exists(), "主合同存在")
    assert_eq(zip_path.exists(), "zip 存在")

    r = run(f"contract-pack -c contract_pack.yaml rollback {bid}", cwd=work)
    assert_eq(r.returncode == 0, "rollback 返回成功")
    assert_eq("回滚成功" in r.stdout, "输出包含'回滚成功'")
    assert_eq(not (party_a / "主合同.pdf").exists(), "主合同已删除")
    assert_eq(not zip_path.exists(), "zip 已删除")

    r = run(f"contract-pack -c contract_pack.yaml show {bid}", cwd=work)
    assert_eq(r.returncode == 0, "show 成功")
    assert_eq("rolled_back" in r.stdout, f"批次状态为 rolled_back")


def scenario_file_missing(tmpdir: Path):
    """Scenario 2: 文件缺失——目标文件被别人提前删除，回滚应能安全继续（不会误删、仍继续，不会误删、标记为成功"""
    work = setup_scenario(tmpdir, "file_missing")
    bid = run_exec(work)
    (work / "deliver" / "PartyA" / "主合同.pdf").unlink()

    r = run(f"contract-pack -c contract_pack.yaml rollback {bid}", cwd=work)
    assert_eq(r.returncode == 0, "rollback 成功（仅文件缺失不妨碍回滚")
    assert_eq("rolled_back" in run(f"contract-pack -c contract_pack.yaml show {bid}", cwd=work).stdout,
              "最终状态 rolled_back")


def scenario_file_replaced(tmpdir: Path):
    """Scenario 3: 文件被替换——目标内容被改写，回滚应立即停止并标记 rollback_failed"""
    work = setup_scenario(tmpdir, "file_replaced")
    bid = run_exec(work)
    main_pdf = work / "deliver" / "PartyA" / "主合同.pdf"
    main_pdf.write_text("完全无关内容 - 被替换", encoding="utf-8")

    r = run(f"contract-pack -c contract_pack.yaml rollback {bid}", cwd=work)
    assert_eq(r.returncode != 0, "rollback 返回非零表示失败")
    assert_eq("rollback_failed" in run(f"contract-pack -c contract_pack.yaml show {bid}", cwd=work).stdout,
              "批次状态 rollback_failed")
    assert_eq(main_pdf.exists(), "被替换的文件未被删除（没有误删）")
    show_out = run(f"contract-pack -c contract_pack.yaml show {bid}", cwd=work).stdout
    assert_eq("不匹配" in show_out or "替换" in show_out, "错误原因可见（文件大小/内容不匹配")


def scenario_dir_occupied(tmpdir: Path):
    """Scenario 4: 目录占用——目标路径变成目录，回滚应立即停止"""
    work = setup_scenario(tmpdir, "dir_occupied")
    bid = run_exec(work, make_zip=False)

    main_pdf = work / "deliver" / "PartyA" / "主合同.pdf"
    main_pdf.unlink()
    main_pdf.mkdir(parents=True)
    (main_pdf / "something.txt").write_text("x", encoding="utf-8")

    r = run(f"contract-pack -c contract_pack.yaml rollback {bid}", cwd=work)
    assert_eq(r.returncode != 0, "rollback 返回非零（目录占用）")

    show_out = run(f"contract-pack -c contract_pack.yaml show {bid}", cwd=work).stdout
    assert_eq("rollback_failed" in show_out, "批次状态 rollback_failed")
    assert_eq("目录" in show_out, "错误原因包含'目录'字样")


def scenario_zip_replaced(tmpdir: Path):
    """Scenario 5: zip 被替换——zip 被换成别的内容，回滚应立即停止"""
    work = setup_scenario(tmpdir, "zip_replaced")
    bid = run_exec(work)
    zip_path = work / "deliver" / "甲方交付包.zip"
    zip_path.unlink()
    zip_path.write_text("这不是一个合法的 zip，但被替换了", encoding="utf-8")

    r = run(f"contract-pack -c contract_pack.yaml rollback {bid}", cwd=work)
    assert_eq(r.returncode != 0, "rollback 返回非零（zip 被替换）")

    show_out = run(f"contract-pack -c contract_pack.yaml show {bid}", cwd=work).stdout
    assert_eq("rollback_failed" in show_out, "批次状态 rollback_failed")
    assert_eq(zip_path.exists(), "被替换的 zip 未被删除（防止误删其他文件）")


def scenario_show_list_export_visibility(tmpdir: Path):
    """验证 show / list / export 都能看到失败状态和错误原因"""
    work = setup_scenario(tmpdir, "visibility")
    bid = run_exec(work)
    (work / "deliver" / "PartyA" / "主合同.pdf").write_text("HACKED", encoding="utf-8")
    run(f"contract-pack -c contract_pack.yaml rollback {bid}", cwd=work)

    list_out = run(f"contract-pack -c contract_pack.yaml list", cwd=work).stdout
    assert_eq("rollback_failed" in list_out, "list 可看到 rollback_failed 状态")

    show_out = run(f"contract-pack -c contract_pack.yaml show {bid}", cwd=work).stdout
    assert_eq("rollback_failed" in show_out, "show 可看到 rollback_failed 状态")
    assert_eq("错误" in show_out, "show 展示错误列展示错误原因")

    run(f"contract-pack -c contract_pack.yaml export -f json -o report.json", cwd=work)
    report_json = (work / "report.json").read_text(encoding="utf-8")
    assert_eq("rollback_failed" in report_json, "JSON 报告包含 rollback_failed 状态")
    assert_eq("不匹配" in report_json or "替换" in report_json, "JSON 报告包含错误原因")

    run(f"contract-pack -c contract_pack.yaml export -f csv -o report.csv", cwd=work)
    report_csv = (work / "report.csv").read_text(encoding="utf-8-sig")
    assert_eq("rollback_failed" in report_csv, "CSV 报告包含 rollback_failed")


def main():
    tmpdir = Path(tempfile.mkdtemp(prefix="contract_pack_test_"))
    print(f"测试临时目录: {tmpdir}")
    try:
        scenario_normal_rollback(tmpdir)
        scenario_file_missing(tmpdir)
        scenario_file_replaced(tmpdir)
        scenario_dir_occupied(tmpdir)
        scenario_zip_replaced(tmpdir)
        scenario_show_list_export_visibility(tmpdir)
    except AssertionError:
        pass

    print(f"\n=== 总结: 通过 {TESTS_PASS}, 失败 {TESTS_FAIL}")
    sys.exit(1 if TESTS_FAIL else 0)


if __name__ == "__main__":
    main()
