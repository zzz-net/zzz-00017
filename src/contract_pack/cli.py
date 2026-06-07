"""主 CLI 入口"""

from __future__ import annotations

from pathlib import Path

import click
from rich.console import Console
from rich.table import Table

from . import __version__
from .config import AppConfig
from .engine import Engine
from .manifest import load_manifest
from .precheck import run_precheck
from .report import export_csv, export_json
from .storage import BATCH_STATUS, BatchStorage


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


if __name__ == "__main__":
    main()
