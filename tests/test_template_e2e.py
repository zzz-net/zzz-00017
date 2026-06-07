"""合同附件打包 CLI - 交付方案模板端到端测试

覆盖：
  - 跨重启持久化 (save -> 重新打开 CLI -> list/show 仍能读取)
  - 模板 import/export (JSON/CSV 含模板名、来源摘要、创建时间)
  - 冲突失败 (重复名、清单包不匹配、输出路径冲突)
  - 失败后数据库不被污染
  - apply -> dry-run -> run 完整链路
"""

from __future__ import annotations

import csv
import json
import shutil
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
        print(f"    stdout: {r.stdout.strip()[:500]}")
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


def setup_workdir(tmpdir: Path, name: str) -> Path:
    """构建一个可运行的工作目录：包含 sources、manifest.csv、contract_pack.yaml。"""
    print(f"\n=== Scenario: {name} ===")
    work = tmpdir / name
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)

    src = work / "sources"
    (src / "contracts").mkdir(parents=True)
    (src / "scans").mkdir(parents=True)
    (src / "contracts" / "main.pdf").write_text("MAIN CONTRACT", encoding="utf-8")
    (src / "contracts" / "supp.pdf").write_text("SUPPLEMENT", encoding="utf-8")
    (src / "scans" / "seal.jpg").write_text("SEAL IMAGE", encoding="utf-8")

    manifest = work / "manifest.csv"
    with open(manifest, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["package", "category", "source_path", "target_name", "version", "description"])
        w.writerow(["甲方交付包", "main", "contracts/main.pdf", "主合同.pdf", "v1", ""])
        w.writerow(["甲方交付包", "supplement", "contracts/supp.pdf", "补充协议.pdf", "v1", ""])
        w.writerow(["甲方交付包", "seal", "scans/seal.jpg", "盖章扫描件.jpg", "", ""])
        w.writerow(["乙方交付包", "main", "contracts/main.pdf", "主合同.pdf", "v1", ""])
        w.writerow(["乙方交付包", "seal", "scans/seal.jpg", "盖章扫描件.jpg", "", ""])

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
            },
            {
                "name": "乙方交付包",
                "output_dir": "./deliver/PartyB",
                "zip_output": "./deliver/乙方交付包.zip",
                "version": "v2024.06",
            },
        ],
    }
    with open(work / "contract_pack.yaml", "w", encoding="utf-8") as f:
        yaml.safe_dump(cfg, f, allow_unicode=True, sort_keys=False)
    return work


def scenario_cross_restart_persistence(tmpdir: Path):
    """跨重启持久化：save 后模拟进程退出，再打开仍能 list/show/get。"""
    work = setup_workdir(tmpdir, "cross_restart")

    r = run("contract-pack template save 标准方案V1", cwd=work)
    assert_eq(r.returncode == 0, "template save 成功")
    assert_eq("标准方案V1" in r.stdout, "stdout 包含模板名")

    # 记录 DB 路径，确保用的是同一个文件
    db_path = work / ".contract_pack.db"
    assert_eq(db_path.exists(), "SQLite DB 文件创建成功")

    # 模拟重启：删除任何进程内状态（这里通过再次启动独立子进程实现）
    r2 = run("contract-pack template list", cwd=work)
    assert_eq(r2.returncode == 0, "template list 成功")
    assert_eq("标准方案V1" in r2.stdout, "重启后 list 仍能看到模板")

    r3 = run("contract-pack template show 标准方案V1", cwd=work)
    assert_eq(r3.returncode == 0, "template show 成功")
    assert_eq("标准方案V1" in r3.stdout, "重启后 show 能看到模板名")
    assert_eq("甲方交付包" in r3.stdout, "重启后 show 能看到包配置")
    assert_eq("v2024.06" in r3.stdout, "重启后 show 能看到版本号")

    # 删除再验证
    r4 = run("contract-pack template delete --force 标准方案V1", cwd=work)
    assert_eq(r4.returncode == 0, "template delete 成功")
    r5 = run("contract-pack template list", cwd=work)
    assert_eq("暂无保存的模板" in r5.stdout, "删除后 list 为空")


def scenario_save_duplicate_and_db_clean(tmpdir: Path):
    """重复名保存失败，且失败后 DB 模板数量不变（不被污染）。"""
    work = setup_workdir(tmpdir, "duplicate_and_db_clean")

    run("contract-pack template save 方案X", cwd=work)
    r1 = run("contract-pack template list", cwd=work)
    count_before = r1.stdout.count("方案X")

    r2 = run("contract-pack template save 方案X", cwd=work)
    assert_eq(r2.returncode != 0, "重复名保存返回非零")
    assert_eq("已存在" in r2.stdout, "提示模板名已存在")

    r3 = run("contract-pack template list", cwd=work)
    count_after = r3.stdout.count("方案X")
    assert_eq(count_before == count_after, "失败后模板列表中方案X 数量不变")
    assert_eq("暂无保存的模板" not in r3.stdout, "原模板仍存在（未被误删）")


def scenario_apply_full_flow(tmpdir: Path):
    """完整 apply -> dry-run -> run 链路：
    1) save 模板
    2) 新建另一个带新 manifest 的目录
    3) template apply 套用模板生成配置
    4) dry-run 通过
    5) run 执行
    """
    # Step 1: 保存模板
    src_work = setup_workdir(tmpdir, "apply_flow_src")
    run("contract-pack template save 双发交付方案", cwd=src_work)

    # Step 2: 新目录，只放新的 CSV 和 sources，不放 YAML
    dst_work = tmpdir / "apply_flow_dst"
    dst_work.mkdir(parents=True)
    shutil.copytree(src_work / "sources", dst_work / "sources")

    new_manifest = dst_work / "manifest_new.csv"
    with open(new_manifest, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["package", "category", "source_path", "target_name", "version", "description"])
        w.writerow(["甲方交付包", "main", "contracts/main.pdf", "主合同_v2.pdf", "v2", ""])
        w.writerow(["甲方交付包", "seal", "scans/seal.jpg", "盖章扫描件_v2.jpg", "", ""])
        w.writerow(["乙方交付包", "main", "contracts/main.pdf", "合同_main.pdf", "v2", ""])

    # Step 3: 套用模板
    out_yaml = dst_work / "generated_config.yaml"
    r = run(
        f'contract-pack -c "{src_work / "contract_pack.yaml"}" template apply 双发交付方案 '
        f'--manifest "{new_manifest}" --output "{out_yaml}" '
        f'--source-root "./sources"',
        cwd=dst_work,
    )
    assert_eq(r.returncode == 0, "template apply 成功")
    assert_eq(out_yaml.exists(), "生成了配置草稿 YAML")
    with open(out_yaml, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    assert_eq(data["operator"] != "", "生成的配置含 operator")
    assert_eq(len(data["packages"]) >= 1, "生成的配置含 packages")

    # Step 4: dry-run 通过
    r4 = run(f'contract-pack -c "{out_yaml}" dry-run', cwd=dst_work)
    assert_eq(r4.returncode == 0, "dry-run 通过")
    assert_eq("通过" in r4.stdout, "dry-run 显示通过")

    # Step 5: run 执行
    r5 = run(f'contract-pack -c "{out_yaml}" run --zip', cwd=dst_work)
    assert_eq(r5.returncode == 0, "run 执行成功")
    assert_eq("执行完毕" in r5.stdout, "run 输出执行完毕")

    # 验证产物存在
    party_a = dst_work / "deliver" / "PartyA"
    zip_a = dst_work / "deliver" / "甲方交付包.zip"
    party_b = dst_work / "deliver" / "PartyB"
    zip_b = dst_work / "deliver" / "乙方交付包.zip"
    assert_eq((party_a / "主合同_v2.pdf").exists(), "甲方主合同已复制")
    assert_eq((party_a / "盖章扫描件_v2.jpg").exists(), "甲方盖章扫描件已复制")
    assert_eq((party_b / "合同_main.pdf").exists(), "乙方主合同已复制")
    assert_eq(zip_a.exists(), "甲方 zip 已生成")
    assert_eq(zip_b.exists(), "乙方 zip 已生成")


def scenario_apply_package_mismatch(tmpdir: Path):
    """套用模板时清单包不匹配 -> CLI 非零退出，不生成配置，DB 不被污染。"""
    src_work = setup_workdir(tmpdir, "apply_pkg_mismatch_src")
    run("contract-pack template save 双发方案", cwd=src_work)

    dst_work = tmpdir / "apply_pkg_mismatch_dst"
    dst_work.mkdir(parents=True)
    shutil.copytree(src_work / "sources", dst_work / "sources")

    # 清单中包含模板未定义的"丙方交付包"
    bad_manifest = dst_work / "bad_manifest.csv"
    with open(bad_manifest, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["package", "category", "source_path", "target_name"])
        w.writerow(["甲方交付包", "main", "contracts/main.pdf", "x.pdf"])
        w.writerow(["丙方交付包", "main", "contracts/main.pdf", "y.pdf"])

    db_path = src_work / ".contract_pack.db"
    import sqlite3
    conn = sqlite3.connect(str(db_path))
    try:
        count_before = conn.execute("SELECT COUNT(*) FROM templates").fetchone()[0]
    finally:
        conn.close()

    out_yaml = dst_work / "will_not_exist.yaml"
    r = run(
        f'contract-pack -c "{src_work / "contract_pack.yaml"}" template apply 双发方案 '
        f'--manifest "{bad_manifest}" --output "{out_yaml}"',
        cwd=dst_work,
    )
    assert_eq(r.returncode != 0, "包不匹配时 apply 返回非零")
    assert_eq("manifest_package_not_in_template" in r.stdout or "模板中未定义" in r.stdout or "失败" in r.stdout,
              "stdout 提示失败原因")
    assert_eq(not out_yaml.exists(), "不生成半截配置文件")

    conn = sqlite3.connect(str(db_path))
    try:
        count_after = conn.execute("SELECT COUNT(*) FROM templates").fetchone()[0]
    finally:
        conn.close()
    assert_eq(count_before == count_after, "失败后 DB 模板数量不变（未被污染）")


def scenario_apply_output_conflict(tmpdir: Path):
    """模板内部路径冲突（两个包共享 zip 路径）时 apply 失败，不生成文件。"""
    work = tmpdir / "apply_conflict"
    work.mkdir(parents=True)
    src = work / "sources"
    (src / "contracts").mkdir(parents=True)
    (src / "contracts" / "a.pdf").write_text("A", encoding="utf-8")
    (src / "contracts" / "b.pdf").write_text("B", encoding="utf-8")

    manifest = work / "manifest.csv"
    with open(manifest, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["package", "category", "source_path", "target_name"])
        w.writerow(["A包", "main", "contracts/a.pdf", "a.pdf"])
        w.writerow(["B包", "main", "contracts/b.pdf", "b.pdf"])

    # 配置中两个包共享同一个 zip 路径
    cfg = {
        "operator": "x",
        "manifest": "manifest.csv",
        "source_root": "./sources",
        "db_path": "./.contract_pack.db",
        "allow_overwrite": True,
        "packages": [
            {"name": "A包", "output_dir": "./deliver/A", "zip_output": "./deliver/same.zip"},
            {"name": "B包", "output_dir": "./deliver/B", "zip_output": "./deliver/same.zip"},
        ],
    }
    cfg_path = work / "contract_pack.yaml"
    with open(cfg_path, "w", encoding="utf-8") as f:
        yaml.safe_dump(cfg, f, allow_unicode=True, sort_keys=False)

    run("contract-pack template save 冲突方案", cwd=work)

    out_yaml = work / "gen.yaml"
    r = run(
        f'contract-pack template apply 冲突方案 --manifest "{manifest}" --output "{out_yaml}"',
        cwd=work,
    )
    assert_eq(r.returncode != 0, "路径冲突时 apply 返回非零")
    assert_eq(not out_yaml.exists(), "不生成半截配置")


def scenario_export_cli(tmpdir: Path):
    """CLI export 输出 JSON/CSV，包含模板名、来源配置摘要、创建时间。"""
    work = setup_workdir(tmpdir, "export_cli")
    run("contract-pack template save 导出测试方案", cwd=work)

    # show 一下确认创建时间
    r0 = run("contract-pack template show 导出测试方案", cwd=work)
    assert_eq(r0.returncode == 0, "show 成功")

    json_out = work / "tpl.json"
    csv_out = work / "tpl.csv"

    rj = run(f'contract-pack template export -f json -o "{json_out}" 导出测试方案', cwd=work)
    assert_eq(rj.returncode == 0, "export json 成功")
    assert_eq(json_out.exists(), "json 文件生成")

    with open(json_out, "r", encoding="utf-8") as f:
        jdata = json.load(f)
    assert_eq(len(jdata) == 1, "json 有 1 条记录")
    rec = jdata[0]
    assert_eq(rec.get("template_name") == "导出测试方案", "JSON 含 template_name")
    assert_eq("source_config_summary" in rec, "JSON 含 source_config_summary")
    assert_eq("created_at" in rec and rec["created_at"], "JSON 含 created_at")

    rc = run(f'contract-pack template export -f csv -o "{csv_out}" 导出测试方案', cwd=work)
    assert_eq(rc.returncode == 0, "export csv 成功")
    assert_eq(csv_out.exists(), "csv 文件生成")

    with open(csv_out, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
    headers = reader.fieldnames or []
    for h in ("template_name", "created_at", "source_config_summary"):
        assert_eq(h in headers, f"CSV 表头含 {h}")
    assert_eq(all(r["template_name"] == "导出测试方案" for r in rows), "CSV 行 template_name 正确")
    assert_eq(all(r["created_at"] for r in rows), "CSV 行 created_at 非空")
    scs = json.loads(rows[0]["source_config_summary"])
    assert_eq(scs.get("operator") == "e2e_tester", "CSV source_config_summary 内容正确")


def scenario_apply_output_exists(tmpdir: Path):
    """apply 输出路径已存在 -> 非零退出，不覆盖。"""
    src_work = setup_workdir(tmpdir, "apply_out_exist_src")
    run("contract-pack template save 方案", cwd=src_work)

    dst_work = tmpdir / "apply_out_exist_dst"
    dst_work.mkdir(parents=True)
    shutil.copytree(src_work / "sources", dst_work / "sources")
    shutil.copy(src_work / "manifest.csv", dst_work / "manifest.csv")

    out_yaml = dst_work / "gen.yaml"
    out_yaml.write_text("OLD_CONTENT", encoding="utf-8")

    r = run(
        f'contract-pack -c "{src_work / "contract_pack.yaml"}" template apply 方案 '
        f'--manifest "{dst_work / "manifest.csv"}" --output "{out_yaml}"',
        cwd=dst_work,
    )
    assert_eq(r.returncode != 0, "输出已存在时返回非零")
    assert_eq(out_yaml.read_text(encoding="utf-8") == "OLD_CONTENT", "原有文件未被覆盖")


def main():
    tmpdir = Path(tempfile.mkdtemp(prefix="contract_pack_tpl_e2e_"))
    print(f"E2E 测试临时目录: {tmpdir}")
    try:
        scenario_cross_restart_persistence(tmpdir)
        scenario_save_duplicate_and_db_clean(tmpdir)
        scenario_apply_full_flow(tmpdir)
        scenario_apply_package_mismatch(tmpdir)
        scenario_apply_output_conflict(tmpdir)
        scenario_export_cli(tmpdir)
        scenario_apply_output_exists(tmpdir)
    except AssertionError:
        pass

    print(f"\n=== 端到端测试总结: 通过 {TESTS_PASS}, 失败 {TESTS_FAIL} ===")
    sys.exit(1 if TESTS_FAIL else 0)


if __name__ == "__main__":
    main()
