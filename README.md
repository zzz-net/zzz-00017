# contract-pack - 本地合同附件打包交付 CLI

基于 CSV 清单和 YAML 配置，将主合同、补充协议、盖章扫描件整理成规范的交付目录或 zip 压缩包。

## 功能特性

- **dry-run 预检**：不改文件，提前发现缺失附件、重复目标名、版本倒退、清单外文件和包名冲突
- **批次执行**：复制到交付目录，可选生成 zip 压缩包
- **持久化记录**：批次状态、每个文件动作、操作者、起止时间、配置摘要均持久化到 SQLite
- **查看与回滚**：重启后仍可查询最近批次、执行回滚；回滚时若原路径被占用会停止并提示
- **报告导出**：支持导出 JSON / CSV 报告

## 安装

```bash
pip install -e .
```

或直接运行：

```bash
python -m contract_pack.cli --help
```

## 快速开始

示例文件在 `examples/` 目录下，可直接复现：

```bash
cd examples

# 1) 预检 (不改文件)
contract-pack -c contract_pack.yaml dry-run

# 2) 执行批次，复制文件并生成 zip
contract-pack -c contract_pack.yaml run --zip

# 3) 查看最近批次
contract-pack -c contract_pack.yaml list -n 5

# 4) 查看某批次详情 (替换为实际 batch id)
contract-pack -c contract_pack.yaml show <BATCH_ID>

# 5) 回滚批次 (删除本批次复制的文件)
contract-pack -c contract_pack.yaml rollback <BATCH_ID>

# 6) 导出报告
contract-pack -c contract_pack.yaml export -f json -o report.json
contract-pack -c contract_pack.yaml export -f csv  -o report.csv
```

## 配置文件 (contract_pack.yaml)

```yaml
operator: "zhangsan"            # 操作者，默认取当前系统用户
manifest: "manifest.csv"        # CSV 清单路径 (相对本配置文件或绝对路径)
source_root: "./sources"        # 源文件根目录
db_path: "./.contract_pack.db"  # 批次数据库路径
allow_overwrite: false          # 是否允许覆盖已存在的目标文件

packages:
  - name: "甲方交付包"
    output_dir: "./deliver/PartyA"   # 交付目录
    zip_output: "./deliver/甲方交付包.zip"  # 可选：zip 输出路径
    version: "v2024.06"              # 批次版本标签 (可选)
    file_mapping:                    # 分类到子目录的映射 (可选，预留)
      "main": "01_主合同"
      "supplement": "02_补充协议"
      "seal": "03_盖章扫描件"

  - name: "乙方交付包"
    output_dir: "./deliver/PartyB"
    zip_output: "./deliver/乙方交付包.zip"
```

## CSV 清单 (manifest.csv)

必需列：`package, category, source_path, target_name`  
可选列：`version, description`

| 列 | 说明 |
|---|---|
| package | 所属交付包名，需与配置中 `packages[].name` 对应 |
| category | 文件分类：`main` (主合同) / `supplement` (补充协议) / `seal` (盖章扫描件) 或自定义 |
| source_path | 源文件相对路径 (相对于 `source_root`) |
| target_name | 目标文件名 |
| version | 文件版本号，如 `v2.1`、`R3` (可选，用于版本倒退检测) |
| description | 描述 (可选) |

示例：

```csv
package,category,source_path,target_name,version,description
甲方交付包,main,contracts/主合同_2024.pdf,主合同_v2.pdf,v2.0,正式版主合同
甲方交付包,supplement,contracts/补充协议一.pdf,补充协议一_v1.pdf,v1.1,关于付款方式的补充
甲方交付包,seal,scans/主合同盖章页.jpg,主合同_盖章扫描件.jpg,,盖章扫描件
```

## 预检检测项 (dry-run)

| 类型 | 级别 | 说明 |
|---|---|---|
| missing_attachment | error | 源文件不存在 |
| duplicate_target | error | 同一包内目标文件名重复 |
| version_rollback | error | 版本号小于上一次同文件记录 |
| package_name_conflict | error | 配置中包名重复 |
| zip_name_conflict | error | 多个包共享同一 zip 输出路径 |
| unknown_package | error | 清单中的包未在配置中定义 |
| target_exists | warning | 目标文件已存在 (allow_overwrite=false) |
| outside_manifest | warning | 输出目录存在清单外文件 |
| empty_package | warning | 配置的包在清单中无文件 |
| zip_exists | warning | zip 文件已存在 |

## 失败路径保障

- `dry-run` 只读，不会修改任何文件
- 源路径不存在的文件不会进入批次动作列表，批次不会被标记为 completed
- 回滚时若目标路径已被其他文件/目录占用，会立即停止并提示，不会强制删除

## 命令速查

```
contract-pack -c config.yaml dry-run              # 预检
contract-pack -c config.yaml run                  # 执行批次 (仅复制)
contract-pack -c config.yaml run --zip            # 执行批次 + 生成 zip
contract-pack -c config.yaml run --zip --force    # 跳过预检错误强制执行
contract-pack -c config.yaml list -n 10           # 查看最近 10 个批次
contract-pack -c config.yaml show <BATCH_ID>      # 查看批次详情
contract-pack -c config.yaml rollback <BATCH_ID>  # 回滚批次
contract-pack -c config.yaml export -f json -o report.json   # 导出 JSON
contract-pack -c config.yaml export -f csv  -o report.csv    # 导出 CSV
```
