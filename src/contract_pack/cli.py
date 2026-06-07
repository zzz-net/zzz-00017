"""主 CLI 入口"""

from __future__ import annotations

from pathlib import Path

import click
from rich.console import Console
from rich.table import Table

from . import __version__
from .config import AppConfig, PackageConfig
from .engine import Engine
from .manifest import load_manifest
from .precheck import PrecheckIssue, run_precheck
from .report import export_csv, export_json
from .storage import BATCH_STATUS, BatchStorage
from .template import (
    TemplateApplyError,
    TemplateImportError,
    TemplateNameExistsError,
    TemplateNotFoundError,
    TemplateStorage,
    apply_template,
    export_template_csv,
    export_template_json,
    import_template_csv,
    import_template_json,
)


console = Console()


def _load_config(ctx: click.Context, config_path: str) -> AppConfig:
    try:
        return AppConfig.load(config_path)
    except Exception as e:
        console.print(f"[red]加载配置失败:[/red] {e}")
        ctx.exit(1)


def _get_cfg(ctx: click.Context, config_path: str | None) -> AppConfig:
    if config_path:
        return _load_config(ctx, config_path)
    parent_cfg = ctx.obj.get("config") if ctx.obj else None
    if parent_cfg:
        return parent_cfg
    return _load_config(ctx, "contract_pack.yaml")


_config_option = click.option(
    "--config", "-c", "config_path",
    default=None,
    help="YAML 配置文件路径 (默认: contract_pack.yaml)",
)


@click.group(
    context_settings={"help_option_names": ["-h", "--help"]},
    invoke_without_command=False,
)
@click.version_option(version=__version__, prog_name="contract-pack")
@click.option("--config", "-c", "config_path", default=None, help="YAML 配置文件路径 (默认: contract_pack.yaml)")
@click.pass_context
def main(ctx: click.Context, config_path: str | None):
    """本地合同附件打包交付 CLI"""
    ctx.ensure_object(dict)
    if config_path:
        try:
            ctx.obj["config"] = AppConfig.load(config_path)
        except Exception as e:
            console.print(f"[red]加载配置失败:[/red] {e}")
            ctx.exit(1)


@main.command()
@_config_option
@click.pass_context
def dry_run(ctx: click.Context, config_path: str | None):
    """预检: 检查缺失、重复、版本倒退、清单外文件和包名冲突 (不改文件)"""
    cfg = _get_cfg(ctx, config_path)
    try:
        entries = load_manifest(cfg.manifest_path)
    except Exception as e:
        console.print(f"[red]加载清单失败:[/red] {e}")
        ctx.exit(1)

    storage = BatchStorage(cfg.db_path)
    last_batches = storage.list_batches(limit=1)
    last_id = last_batches[0].id if last_batches else None

    result = run_precheck(cfg, entries, storage=storage, last_batch_id=last_id)

    console.print(f"[bold]预检结果[/bold]: {'[green]通过[/green]' if result.ok else '[red]失败[/red]'}")
    console.print(f"  条目数: {len(entries)}  计划文件: {sum(len(v) for v in result.plan.values())}")

    if result.errors:
        table = Table(title="错误", show_lines=False)
        table.add_column("级别", style="red")
        table.add_column("类型")
        table.add_column("包")
        table.add_column("消息")
        table.add_column("详情")
        for i in result.errors:
            table.add_row(i.level, i.kind, i.package, i.message, i.detail)
        console.print(table)

    if result.warnings:
        table = Table(title="警告", show_lines=False)
        table.add_column("级别", style="yellow")
        table.add_column("类型")
        table.add_column("包")
        table.add_column("消息")
        table.add_column("详情")
        for i in result.warnings:
            table.add_row(i.level, i.kind, i.package, i.message, i.detail)
        console.print(table)

    if not result.ok:
        ctx.exit(2)


@main.command()
@_config_option
@click.option("--zip/--no-zip", default=False, help="是否生成 zip 压缩包")
@click.option("--force", is_flag=True, help="跳过预检错误直接执行 (不推荐)")
@click.pass_context
def run(ctx: click.Context, config_path: str | None, zip: bool, force: bool):
    """生成批次并执行复制/压包"""
    cfg = _get_cfg(ctx, config_path)
    try:
        entries = load_manifest(cfg.manifest_path)
    except Exception as e:
        console.print(f"[red]加载清单失败:[/red] {e}")
        ctx.exit(1)

    storage = BatchStorage(cfg.db_path)
    last_batches = storage.list_batches(limit=1)
    last_id = last_batches[0].id if last_batches else None
    result = run_precheck(cfg, entries, storage=storage, last_batch_id=last_id)

    if not result.ok and not force:
        console.print(f"[red]预检失败，共 {len(result.errors)} 个错误。请先运行 dry-run 查看详情，或使用 --force 强制执行。[/red]")
        ctx.exit(2)

    engine = Engine(cfg)
    exec_result = engine.run(entries, result, make_zip=zip)

    status_color = {
        BATCH_STATUS["COMPLETED"]: "green",
        BATCH_STATUS["PARTIAL"]: "yellow",
        BATCH_STATUS["FAILED"]: "red",
    }.get(exec_result.status, "white")

    console.print(
        f"批次 [bold]{exec_result.batch_id}[/bold] 执行完毕，状态: [bold {status_color}]{exec_result.status}[/bold {status_color}]"
    )
    console.print(f"  复制成功: {exec_result.copied_files}  zip: {exec_result.zipped_files}  失败: {exec_result.failed_files}")
    if exec_result.error:
        console.print(f"[red]错误: {exec_result.error}[/red]")

    if exec_result.status == BATCH_STATUS["FAILED"]:
        ctx.exit(3)


@main.command("list")
@_config_option
@click.option("--limit", "-n", default=10, show_default=True, help="显示最近多少批次")
@click.pass_context
def list_batches(ctx: click.Context, config_path: str | None, limit: int):
    """查看批次记录"""
    cfg = _get_cfg(ctx, config_path)
    storage = BatchStorage(cfg.db_path)
    batches = storage.list_batches(limit=limit)

    if not batches:
        console.print("[yellow]暂无批次记录[/yellow]")
        return

    table = Table(title=f"最近 {len(batches)} 个批次")
    table.add_column("批次 ID", overflow="fold")
    table.add_column("状态", min_width=15, no_wrap=True)
    table.add_column("操作者")
    table.add_column("开始时间")
    table.add_column("结束时间")
    table.add_column("文件动作")
    table.add_column("错误", overflow="fold", max_width=40)
    for b in batches:
        sc = {
            BATCH_STATUS["COMPLETED"]: "green",
            BATCH_STATUS["PARTIAL"]: "yellow",
            BATCH_STATUS["FAILED"]: "red",
            BATCH_STATUS["ROLLED_BACK"]: "cyan",
            BATCH_STATUS["ROLLBACK_FAILED"]: "bold red",
            BATCH_STATUS["RUNNING"]: "blue",
            BATCH_STATUS["PENDING"]: "white",
        }.get(b.status, "white")
        file_summary = f"{sum(1 for f in b.file_actions if f.status == 'success')}/{len(b.file_actions)}"
        table.add_row(
            b.id,
            f"[{sc}]{b.status}[/{sc}]",
            b.operator,
            b.started_at,
            b.finished_at or "-",
            file_summary,
            b.error or "",
        )
    console.print(table)


@main.command()
@_config_option
@click.argument("batch_id")
@click.pass_context
def show(ctx: click.Context, config_path: str | None, batch_id: str):
    """查看单个批次的详细信息"""
    cfg = _get_cfg(ctx, config_path)
    storage = BatchStorage(cfg.db_path)
    batch = storage.get_batch(batch_id)
    if not batch:
        console.print(f"[red]批次不存在: {batch_id}[/red]")
        ctx.exit(1)

    console.print(f"[bold]批次 ID[/bold]: {batch.id}")
    console.print(f"[bold]状态[/bold]: {batch.status}")
    console.print(f"[bold]操作者[/bold]: {batch.operator}")
    console.print(f"[bold]开始[/bold]: {batch.started_at}")
    console.print(f"[bold]结束[/bold]: {batch.finished_at or '-'}")
    if batch.error:
        console.print(f"[bold]错误[/bold]: [red]{batch.error}[/red]")

    if batch.file_actions:
        table = Table(title="文件动作")
        table.add_column("包")
        table.add_column("动作")
        table.add_column("分类")
        table.add_column("源路径", overflow="fold")
        table.add_column("目标路径", overflow="fold")
        table.add_column("状态")
        table.add_column("错误", overflow="fold", max_width=30)
        table.add_column("大小", max_width=12)
        table.add_column("指纹 (sha1)", max_width=16)
        for fa in batch.file_actions:
            sc = "green" if fa.status == "success" else ("red" if fa.status == "failed" else "white")
            hash_display = (fa.file_hash[:12] + "…") if fa.file_hash else "-"
            size_display = str(fa.file_size) if fa.file_size is not None else "-"
            table.add_row(
                fa.package,
                fa.action,
                fa.category,
                fa.source_path,
                fa.target_path,
                f"[{sc}]{fa.status}[/{sc}]",
                fa.error or "",
                size_display,
                hash_display,
            )
        console.print(table)


@main.command()
@_config_option
@click.argument("batch_id")
@click.pass_context
def rollback(ctx: click.Context, config_path: str | None, batch_id: str):
    """回滚指定批次"""
    cfg = _get_cfg(ctx, config_path)
    engine = Engine(cfg)
    ok, msg = engine.rollback(batch_id)
    if ok:
        console.print(f"[green]{msg}[/green]")
    else:
        console.print(f"[red]{msg}[/red]")
        ctx.exit(4)


@main.command()
@_config_option
@click.option("--format", "-f", "fmt", type=click.Choice(["json", "csv"]), default="json", show_default=True, help="导出格式")
@click.option("--output", "-o", "output_path", required=True, help="输出文件路径")
@click.option("--batch-id", "batch_id", default=None, help="仅导出指定批次")
@click.option("--limit", "-n", default=50, show_default=True, help="导出最近多少批次")
@click.pass_context
def export(ctx: click.Context, config_path: str | None, fmt: str, output_path: str, batch_id: str | None, limit: int):
    """导出批次报告为 JSON 或 CSV"""
    cfg = _get_cfg(ctx, config_path)
    storage = BatchStorage(cfg.db_path)

    if batch_id:
        b = storage.get_batch(batch_id)
        if not b:
            console.print(f"[red]批次不存在: {batch_id}[/red]")
            ctx.exit(1)
        batches = [b]
    else:
        batches = storage.list_batches(limit=limit)

    out = Path(output_path)
    if fmt == "json":
        export_json(batches, out)
    else:
        export_csv(batches, out)
    console.print(f"[green]已导出 {len(batches)} 个批次到 {out}[/green]")


def _print_issues(issues: list[PrecheckIssue]):
    errors = [i for i in issues if i.level == "error"]
    warnings = [i for i in issues if i.level == "warning"]
    if errors:
        table = Table(title="错误", show_lines=False)
        table.add_column("级别", style="red")
        table.add_column("类型")
        table.add_column("包")
        table.add_column("消息")
        table.add_column("详情")
        for i in errors:
            table.add_row(i.level, i.kind, i.package, i.message, i.detail or "")
        console.print(table)
    if warnings:
        table = Table(title="警告", show_lines=False)
        table.add_column("级别", style="yellow")
        table.add_column("类型")
        table.add_column("包")
        table.add_column("消息")
        table.add_column("详情")
        for i in warnings:
            table.add_row(i.level, i.kind, i.package, i.message, i.detail or "")
        console.print(table)


@main.group("template")
@click.pass_context
def template_group(ctx: click.Context):
    """交付方案模板管理 (保存/列出/查看/套用/导出/删除)"""
    pass


@template_group.command("save")
@_config_option
@click.argument("name")
@click.pass_context
def template_save(ctx: click.Context, config_path: str | None, name: str):
    """将当前 YAML 配置的 packages / file_mapping / zip 规则保存为命名模板"""
    cfg = _get_cfg(ctx, config_path)
    base_dir = Path(cfg.db_path).parent

    rel_packages = []
    for pkg in cfg.packages:
        try:
            rel_out = pkg.output_dir.relative_to(base_dir)
        except ValueError:
            rel_out = Path(pkg.output_dir.name)
        rel_zip = None
        if pkg.zip_output:
            try:
                rel_zip = pkg.zip_output.relative_to(base_dir)
            except ValueError:
                rel_zip = Path(pkg.zip_output.name)
        rel_packages.append(
            PackageConfig(
                name=pkg.name,
                output_dir=rel_out,
                zip_output=rel_zip,
                file_mapping=dict(pkg.file_mapping),
                version=pkg.version,
            )
        )

    storage = TemplateStorage(cfg.db_path)
    try:
        tpl = storage.save_template(
            name=name,
            packages=rel_packages,
            source_config_summary=cfg.summary(),
        )
    except TemplateNameExistsError as e:
        console.print(f"[red]保存失败: {e}[/red]")
        console.print("[yellow]提示: 使用 'contract-pack template list' 查看已有模板，或使用其他名称。[/yellow]")
        ctx.exit(5)
    except ValueError as e:
        console.print(f"[red]保存失败: {e}[/red]")
        ctx.exit(5)
    console.print(f"[green]模板已保存:[/green] {tpl.name} (创建于 {tpl.created_at})")
    console.print(f"  包含 {len(tpl.packages)} 个包: {', '.join(p.name for p in tpl.packages)}")


@template_group.command("list")
@_config_option
@click.pass_context
def template_list_cmd(ctx: click.Context, config_path: str | None):
    """列出所有已保存的交付方案模板"""
    cfg = _get_cfg(ctx, config_path)
    storage = TemplateStorage(cfg.db_path)
    templates = storage.list_templates()
    if not templates:
        console.print("[yellow]暂无保存的模板[/yellow]")
        console.print("[yellow]提示: 使用 'contract-pack template save <名称>' 保存当前配置为模板。[/yellow]")
        return
    table = Table(title=f"已保存的模板 ({len(templates)} 个)")
    table.add_column("模板名")
    table.add_column("创建时间")
    table.add_column("包数量")
    table.add_column("包列表", overflow="fold")
    for tpl in templates:
        table.add_row(
            tpl.name,
            tpl.created_at,
            str(len(tpl.packages)),
            ", ".join(p.name for p in tpl.packages),
        )
    console.print(table)


@template_group.command("show")
@_config_option
@click.argument("name")
@click.pass_context
def template_show(ctx: click.Context, config_path: str | None, name: str):
    """查看指定模板的详细信息"""
    cfg = _get_cfg(ctx, config_path)
    storage = TemplateStorage(cfg.db_path)
    tpl = storage.get_template(name)
    if not tpl:
        console.print(f"[red]模板不存在: {name}[/red]")
        console.print("[yellow]提示: 使用 'contract-pack template list' 查看已有模板。[/yellow]")
        ctx.exit(1)
    console.print(f"[bold]模板名[/bold]: {tpl.name}")
    console.print(f"[bold]ID[/bold]: {tpl.id}")
    console.print(f"[bold]创建时间[/bold]: {tpl.created_at}")
    console.print(f"[bold]来源配置摘要[/bold]:")
    for k, v in tpl.source_config_summary.items():
        if k != "packages":
            console.print(f"  {k}: {v}")
    if tpl.packages:
        table = Table(title="包配置")
        table.add_column("包名")
        table.add_column("输出目录", overflow="fold")
        table.add_column("zip 输出", overflow="fold")
        table.add_column("版本")
        table.add_column("file_mapping", overflow="fold", max_width=50)
        for pkg in tpl.packages:
            table.add_row(
                pkg.name,
                str(pkg.output_dir),
                str(pkg.zip_output) if pkg.zip_output else "-",
                pkg.version or "-",
                str(pkg.file_mapping) if pkg.file_mapping else "-",
            )
        console.print(table)


@template_group.command("delete")
@_config_option
@click.argument("name")
@click.option("--force", is_flag=True, help="不确认直接删除")
@click.pass_context
def template_delete(ctx: click.Context, config_path: str | None, name: str, force: bool):
    """删除指定模板"""
    cfg = _get_cfg(ctx, config_path)
    storage = TemplateStorage(cfg.db_path)
    tpl = storage.get_template(name)
    if not tpl:
        console.print(f"[red]模板不存在: {name}[/red]")
        ctx.exit(1)
    if not force:
        console.print(f"[yellow]即将删除模板: {name} (包含 {len(tpl.packages)} 个包)[/yellow]")
        confirmed = click.confirm("确认删除？", default=False)
        if not confirmed:
            console.print("已取消")
            return
    deleted = storage.delete_template(name)
    if deleted:
        console.print(f"[green]模板已删除: {name}[/green]")
    else:
        console.print(f"[red]删除失败: {name}[/red]")
        ctx.exit(1)


@template_group.command("export")
@_config_option
@click.option("--format", "-f", "fmt", type=click.Choice(["json", "csv"]), default="json", show_default=True, help="导出格式")
@click.option("--output", "-o", "output_path", required=True, help="输出文件路径")
@click.argument("name", required=False)
@click.pass_context
def template_export(ctx: click.Context, config_path: str | None, fmt: str, output_path: str, name: str | None):
    """导出模板为 JSON 或 CSV，包含模板名、来源配置摘要和创建时间"""
    cfg = _get_cfg(ctx, config_path)
    storage = TemplateStorage(cfg.db_path)
    if name:
        tpl = storage.get_template(name)
        if not tpl:
            console.print(f"[red]模板不存在: {name}[/red]")
            ctx.exit(1)
        templates = [tpl]
    else:
        templates = storage.list_templates()
    if not templates:
        console.print("[yellow]没有可导出的模板[/yellow]")
        ctx.exit(1)
    out = Path(output_path)
    try:
        if fmt == "json":
            export_template_json(templates, out)
        else:
            export_template_csv(templates, out)
    except (PermissionError, OSError) as e:
        console.print(f"[red]导出失败: {e}（可能是只读目录或权限不足）[/red]")
        ctx.exit(1)
    console.print(f"[green]已导出 {len(templates)} 个模板到 {out}[/green]")


@template_group.command("import")
@_config_option
@click.option("--format", "-f", "fmt", type=click.Choice(["json", "csv"]), default="json", show_default=True, help="导入格式")
@click.option("--input", "-i", "input_path", required=True, help="输入文件路径")
@click.option("--force", is_flag=True, help="模板名已存在时覆盖（默认拒绝）")
@click.pass_context
def template_import(ctx: click.Context, config_path: str | None, fmt: str, input_path: str, force: bool):
    """从 JSON 或 CSV 导入模板，保留模板名、来源配置摘要、创建时间和包级 zip 规则"""
    cfg = _get_cfg(ctx, config_path)
    storage = TemplateStorage(cfg.db_path)
    in_file = Path(input_path)

    try:
        if fmt == "json":
            imported = import_template_json(in_file)
        else:
            imported = import_template_csv(in_file)
    except TemplateImportError as e:
        console.print(f"[red]导入失败: {e}[/red]")
        ctx.exit(7)

    if not imported:
        console.print("[yellow]文件中没有可导入的模板[/yellow]")
        ctx.exit(1)

    saved = 0
    skipped = 0
    for tpl in imported:
        existing = storage.get_template(tpl.name)
        if existing and not force:
            skipped += 1
            console.print(f"[yellow]跳过: 模板名 '{tpl.name}' 已存在（使用 --force 覆盖）[/yellow]")
            continue
        if existing and force:
            storage.delete_template(tpl.name)
        try:
            storage.save_template(
                name=tpl.name,
                packages=tpl.packages,
                source_config_summary=tpl.source_config_summary,
                created_at=tpl.created_at,
                id=tpl.id,
            )
            saved += 1
        except (TemplateNameExistsError, ValueError) as e:
            console.print(f"[red]保存模板 '{tpl.name}' 失败: {e}[/red]")
            ctx.exit(7)

    console.print(f"[green]导入完成: 成功 {saved} 个, 跳过 {skipped} 个[/green]")
    for t in imported:
        scs = t.source_config_summary
        console.print(f"  [bold]{t.name}[/bold] (创建于 {t.created_at}) - {len(t.packages)} 个包")
        if scs:
            keys = [k for k in scs.keys() if k != "packages"]
            if keys:
                console.print(f"    来源摘要: {', '.join(f'{k}={scs[k]}' for k in keys)}")
        for pkg in t.packages:
            zip_note = f", zip={pkg.zip_output}" if pkg.zip_output else ""
            console.print(f"      - {pkg.name}: output={pkg.output_dir}{zip_note}")


@template_group.command("apply")
@_config_option
@click.argument("template_name")
@click.option("--manifest", "-m", "manifest_path", required=True, help="新的 CSV 清单文件路径")
@click.option("--output", "-o", "output_path", required=True, help="生成的 YAML 配置草稿输出路径")
@click.option("--source-root", "source_root", default=None, help="新的 source_root (默认: ./sources)")
@click.option("--operator", "operator", default=None, help="操作者 (默认从环境变量读取)")
@click.option("--db-path", "db_path", default=None, help="新的 db_path (默认: ./.contract_pack.db)")
@click.option("--skip-dry-run", is_flag=True, help="跳过 dry-run 验证（不推荐）")
@click.pass_context
def template_apply(
    ctx: click.Context,
    config_path: str | None,
    template_name: str,
    manifest_path: str,
    output_path: str,
    source_root: str | None,
    operator: str | None,
    db_path: str | None,
    skip_dry_run: bool,
):
    """套用模板 + 新 CSV 清单生成配置草稿，默认自动 dry-run 验证"""
    cfg = _get_cfg(ctx, config_path)
    storage = TemplateStorage(cfg.db_path)
    tpl = storage.get_template(template_name)
    if not tpl:
        console.print(f"[red]模板不存在: {template_name}[/red]")
        console.print("[yellow]提示: 使用 'contract-pack template list' 查看已有模板。[/yellow]")
        ctx.exit(1)

    console.print(f"套用模板 [bold]{tpl.name}[/bold] 生成配置草稿...")
    console.print(f"  清单: {manifest_path}")
    console.print(f"  输出: {output_path}")

    try:
        generated, precheck_result = apply_template(
            template=tpl,
            manifest_path=Path(manifest_path),
            output_config_path=Path(output_path),
            source_root=Path(source_root) if source_root else None,
            operator=operator,
            db_path=Path(db_path) if db_path else None,
            run_dry_run=not skip_dry_run,
        )
    except TemplateApplyError as e:
        console.print(f"[red]模板套用失败: {e}[/red]")
        if e.issues:
            _print_issues(e.issues)
        console.print("[yellow]提示: 数据库状态未被修改，可修复问题后重试。[/yellow]")
        ctx.exit(6)

    console.print(f"[green]配置草稿已生成:[/green] {generated}")
    if not skip_dry_run:
        console.print(f"  dry-run 通过，条目数: {sum(len(v) for v in precheck_result.plan.values())}")
        if precheck_result.warnings:
            _print_issues(precheck_result.warnings)
    console.print("[yellow]提示: 请检查生成的配置，确认后使用 'contract-pack dry-run' 再验证，然后 'contract-pack run' 执行。[/yellow]")


if __name__ == "__main__":
    main()
