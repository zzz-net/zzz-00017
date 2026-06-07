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

## 审计时间线

所有 CLI 操作（dry-run、run、rerun、rollback、diff、template 导入/导出/套用）都会自动写入可查询的审计流水，持久化到 SQLite，重启后仍可查完整记录。

### 审计配置 (contract_pack.yaml)

```yaml
audit:
  enabled: true                    # 是否开启审计（默认 true）
  retention_days: 90               # 审计记录保留天数（默认 90，0 或负数值非法）
  export_default_dir: "./audit"    # audit export 的默认输出目录（可选）
```

非法配置加载时会给出明确错误信息，例如：
- `audit.enabled 必须是布尔值`
- `audit.retention_days 不能为负数`
- `audit.export_default_dir 必须是字符串路径`

### 审计查询

```bash
# 查看最近 20 条审计记录
contract-pack -c config.yaml audit list -n 20

# 按操作者过滤
contract-pack -c config.yaml audit list --operator zhangsan

# 按命令类型过滤（dry-run/run/rerun/rollback/diff/template-import 等）
contract-pack -c config.yaml audit list --command-type run

# 按结果状态过滤（success/failed/partial/skipped）
contract-pack -c config.yaml audit list --result-status failed

# 按关联批次 ID 过滤
contract-pack -c config.yaml audit list --batch-id <BATCH_ID>

# 按包名过滤
contract-pack -c config.yaml audit list --package 甲方交付包

# 按时间范围过滤（ISO 格式）
contract-pack -c config.yaml audit list \
  --start-time 2024-06-01T00:00:00 \
  --end-time   2024-06-30T23:59:59

# 多条件组合过滤
contract-pack -c config.yaml audit list \
  --operator zhangsan \
  --command-type run \
  --result-status failed \
  -n 50

# 查看单条审计记录详情
contract-pack -c config.yaml audit show <AUDIT_RECORD_ID>
```

每条审计记录包含：
- **参数摘要** (`params_summary`)：命令行参数的精简 JSON
- **配置摘要** (`config_summary`)：配置文件关键信息快照
- **关联批次** (`batch_id`)：run/rerun/rollback 等操作关联的批次 ID
- **包名列表** (`package_names`)：涉及的交付包名
- **文件数量** (`file_count`)：成功处理的文件数
- **错误/警告摘要** (`error_count` / `warning_count` / `error_summary`)
- **详情引用** (`detail_ref`)：如复制数、压缩数、失败数等结构化细节

### 审计导出

支持 JSON 和 CSV 两种格式，字段稳定，便于后续分析归档。

```bash
# 导出 JSON（字段稳定，包含完整嵌套结构）
contract-pack -c config.yaml audit export -f json -o audit.json

# 导出 CSV（复杂字段序列化为 JSON 字符串，适合 Excel）
contract-pack -c config.yaml audit export -f csv -o audit.csv

# 导出指定过滤范围的记录
contract-pack -c config.yaml audit export \
  -f json -o audit_june.json \
  --start-time 2024-06-01T00:00:00 \
  --end-time   2024-06-30T23:59:59 \
  --operator zhangsan
```

导出时的错误处理（绝不"假成功"）：
- 导出路径已存在 → 明确报错 `导出文件已存在: xxx（请先删除或指定其他路径）`
- 导出路径是目录 → 明确报错 `导出路径已存在且是目录: xxx`
- 导出目录只读/权限不足 → 明确报错 `导出目录无写入权限: xxx`
- 数据库缺表或损坏 → 报错 `查询审计记录失败: ...`
- 旧记录字段缺失 → 自动降级为默认值，不会崩溃

### 审计去重与完整性

- **同一次操作不重复写**：同一分钟内相同 operator + 相同 command_type + 相同参数的调用会被去重拒绝（`AuditDuplicateError`），避免重复流水
- **失败操作也留流水**：即使模板导入失败、重跑失败、dry-run 预检不通过，也会写入 `failed` 状态的审计记录，保留参数、错误摘要和详情引用
- **关闭审计**：设置 `audit.enabled: false` 后，所有命令正常执行但不写入审计记录，`audit list/export` 查询不到数据

## 失败路径保障

- `dry-run` 只读，不会修改任何文件
- 源路径不存在的文件不会进入批次动作列表，批次不会被标记为 completed
- 回滚时若目标路径已被其他文件/目录占用，会立即停止并提示，不会强制删除
- 审计导出绝不"假成功"：路径冲突、权限不足、数据库损坏都会给出可读错误并非零退出
- 失败的模板导入/重跑/预检等操作仍然写入审计流水，事后可追溯

## 命令速查

```
contract-pack -c config.yaml dry-run              # 预检
contract-pack -c config.yaml run                  # 执行批次 (仅复制)
contract-pack -c config.yaml run --zip            # 执行批次 + 生成 zip
contract-pack -c config.yaml run --zip --force    # 跳过预检错误强制执行
contract-pack -c config.yaml list -n 10           # 查看最近 10 个批次
contract-pack -c config.yaml show <BATCH_ID>      # 查看批次详情
contract-pack -c config.yaml rollback <BATCH_ID>  # 回滚批次
contract-pack -c config.yaml export -f json -o report.json   # 导出批次 JSON
contract-pack -c config.yaml export -f csv  -o report.csv    # 导出批次 CSV

# --- 审计时间线 ---
contract-pack -c config.yaml audit list -n 20                         # 列出审计记录
contract-pack -c config.yaml audit list --operator zhangsan           # 按操作者过滤
contract-pack -c config.yaml audit list --command-type run            # 按命令类型过滤
contract-pack -c config.yaml audit list --result-status failed        # 按结果状态过滤
contract-pack -c config.yaml audit list --batch-id <BATCH_ID>         # 按批次过滤
contract-pack -c config.yaml audit list --package 甲方交付包            # 按包名过滤
contract-pack -c config.yaml audit list --start-time 2024-06-01T00:00:00  # 按时间范围
contract-pack -c config.yaml audit show <AUDIT_ID>                    # 单条详情
contract-pack -c config.yaml audit export -f json -o audit.json       # 导出审计 JSON
contract-pack -c config.yaml audit export -f csv  -o audit.csv        # 导出审计 CSV
```
