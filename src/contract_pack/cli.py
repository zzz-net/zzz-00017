"""主 CLI 入口"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import click
from rich.console import Console
from rich.table import Table

from . import __version__
from .audit import (
    AUDIT_COMMAND_TYPES,
    AUDIT_RESULT_STATUS,
    AuditService,
    AuditStorage,
    AuditError,
    AuditDuplicateError,
    AuditExportError,
    AuditConfigError,
    export_audit_csv,
    export_audit_json,
)
from .config import AppConfig, PackageConfig
from .diff_core import (
    DIFF_CHANGE_LABELS,
    DiffChangeType,
    DiffError,
    DiffResult,
    diff_against_batch,
    diff_against_directory,
)
from .engine import Engine, RerunError, rerun_batch
from .manifest import load_manifest
from .precheck import PrecheckIssue, run_precheck
from .report import export_csv, export_diff_csv, export_diff_json, export_json
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


def _get_audit_service(cfg: AppConfig) -> AuditService:
    """根据配置获取审计服务实例（审计关闭或存储不可用时返回 disabled 服务）"""
    svc = AuditService.from_config(cfg)
    if cfg.audit.enabled and not svc.enabled:
        console.print("[yellow]警告: 审计功能已启用但存储不可用，操作将继续但不会记录审计[/yellow]")
    return svc


def _get_audit_storage(cfg: AppConfig) -> Optional[AuditStorage]:
    """兼容旧调用：根据配置获取审计存储实例，审计关闭时返回 None"""
    svc = _get_audit_service(cfg)
    return svc.storage if svc.enabled else None


def _try_audit_record(
    audit: Optional[AuditStorage],
    command_type: str,
    operator: str,
    result_status: str,
    params_summary: Optional[Dict[str, Any]] = None,
    config_summary: Optional[Dict[str, Any]] = None,
    batch_id: Optional[str] = None,
    package_names: Optional[List[str]] = None,
    file_count: int = 0,
    error_count: int = 0,
    warning_count: int = 0,
    error_summary: str = "",
    detail_ref: Optional[Dict[str, Any]] = None,
) -> None:
    """兼容旧调用：尝试写入审计记录，失败时仅打印警告，不影响主流程"""
    if audit is None:
        return
    try:
        audit.record_operation(
            command_type=command_type,
            operator=operator,
            result_status=result_status,
            params_summary=params_summary,
            config_summary=config_summary,
            batch_id=batch_id,
            package_names=package_names,
            file_count=file_count,
            error_count=error_count,
            warning_count=warning_count,
            error_summary=error_summary,
            detail_ref=detail_ref,
        )
    except AuditDuplicateError:
        pass
    except AuditError as e:
        console.print(f"[yellow]警告: 审计记录写入失败 ({e})[/yellow]")


def _package_names_from_cfg(cfg: AppConfig) -> List[str]:
    return [p.name for p in cfg.packages]


def _load_config(ctx: click.Context, config_path: str) -> AppConfig:
    try:
        return AppConfig.load(config_path)
    except AuditConfigError as e:
        console.print(f"[red]审计配置错误:[/red] {e}")
        ctx.exit(1)
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
    audit = _get_audit_storage(cfg)
    pkg_names = _package_names_from_cfg(cfg)
    error_summary = ""
    error_count = 0
    warning_count = 0
    file_count = 0
    result_status = AUDIT_RESULT_STATUS["SUCCESS"]

    try:
        entries = load_manifest(cfg.manifest_path)
    except Exception as e:
        error_summary = f"加载清单失败: {e}"
        _try_audit_record(
            audit, AUDIT_COMMAND_TYPES["DRY_RUN"], cfg.operator,
            AUDIT_RESULT_STATUS["FAILED"],
            params_summary={"manifest": str(cfg.manifest_path)},
            config_summary=cfg.summary(),
            package_names=pkg_names,
            error_count=1,
            error_summary=error_summary,
        )
        console.print(f"[red]加载清单失败:[/red] {e}")
        ctx.exit(1)

    storage = BatchStorage(cfg.db_path)
    last_batches = storage.list_batches(limit=1)
    last_id = last_batches[0].id if last_batches else None

    result = run_precheck(cfg, entries, storage=storage, last_batch_id=last_id)
    error_count = len(result.errors)
    warning_count = len(result.warnings)
    file_count = sum(len(v) for v in result.plan.values())

    if not result.ok:
        result_status = AUDIT_RESULT_STATUS["FAILED"]
        error_msgs = [f"[{e.kind}] {e.package}: {e.message}" for e in result.errors]
        error_summary = "；".join(error_msgs[:5])

    _try_audit_record(
        audit, AUDIT_COMMAND_TYPES["DRY_RUN"], cfg.operator,
        result_status,
        params_summary={"manifest": str(cfg.manifest_path), "last_batch_id": last_id},
        config_summary=cfg.summary(),
        package_names=pkg_names,
        file_count=file_count,
        error_count=error_count,
        warning_count=warning_count,
        error_summary=error_summary,
    )

    console.print(f"[bold]预检结果[/bold]: {'[green]通过[/green]' if result.ok else '[red]失败[/red]'}")
    console.print(f"  条目数: {len(entries)}  计划文件: {file_count}")

    if result.errors:
        table = Table(title="错误", show_lines=False)
        table.add_column("级别", style="red")
        table.add_column("类型")
        table.add_column("包")
        table.add_column("消息")
        table.add_column("详情")
        for i in result.errors:
            table.add_row(i.level, i.kind, i.package, i.message, i.detail or "")
        console.print(table)

    if result.warnings:
        table = Table(title="警告", show_lines=False)
        table.add_column("级别", style="yellow")
        table.add_column("类型")
        table.add_column("包")
        table.add_column("消息")
        table.add_column("详情")
        for i in result.warnings:
            table.add_row(i.level, i.kind, i.package, i.message, i.detail or "")
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
    audit = _get_audit_storage(cfg)
    pkg_names = _package_names_from_cfg(cfg)
    params = {"zip": zip, "force": force}
    error_summary = ""
    error_count = 0
    warning_count = 0
    file_count = 0
    result_status = AUDIT_RESULT_STATUS["SUCCESS"]
    batch_id_out: Optional[str] = None

    try:
        entries = load_manifest(cfg.manifest_path)
    except Exception as e:
        error_summary = f"加载清单失败: {e}"
        _try_audit_record(
            audit, AUDIT_COMMAND_TYPES["RUN"], cfg.operator,
            AUDIT_RESULT_STATUS["FAILED"],
            params_summary={**params, "manifest": str(cfg.manifest_path)},
            config_summary=cfg.summary(),
            package_names=pkg_names,
            error_count=1,
            error_summary=error_summary,
        )
        console.print(f"[red]加载清单失败:[/red] {e}")
        ctx.exit(1)

    storage = BatchStorage(cfg.db_path)
    last_batches = storage.list_batches(limit=1)
    last_id = last_batches[0].id if last_batches else None
    precheck = run_precheck(cfg, entries, storage=storage, last_batch_id=last_id)
    error_count = len(precheck.errors)
    warning_count = len(precheck.warnings)

    if not precheck.ok and not force:
        result_status = AUDIT_RESULT_STATUS["FAILED"]
        error_msgs = [f"[{e.kind}] {e.package}: {e.message}" for e in precheck.errors]
        error_summary = "；".join(error_msgs[:5])
        _try_audit_record(
            audit, AUDIT_COMMAND_TYPES["RUN"], cfg.operator,
            result_status,
            params_summary={**params, "manifest": str(cfg.manifest_path), "last_batch_id": last_id},
            config_summary=cfg.summary(),
            package_names=pkg_names,
            file_count=sum(len(v) for v in precheck.plan.values()),
            error_count=error_count,
            warning_count=warning_count,
            error_summary=error_summary,
        )
        console.print(f"[red]预检失败，共 {len(precheck.errors)} 个错误。请先运行 dry-run 查看详情，或使用 --force 强制执行。[/red]")
        ctx.exit(2)

    engine = Engine(cfg)
    exec_result = engine.run(entries, precheck, make_zip=zip)
    batch_id_out = exec_result.batch_id
    file_count = exec_result.copied_files + exec_result.zipped_files

    if exec_result.status == BATCH_STATUS["COMPLETED"]:
        result_status = AUDIT_RESULT_STATUS["SUCCESS"]
    elif exec_result.status == BATCH_STATUS["PARTIAL"]:
        result_status = AUDIT_RESULT_STATUS["PARTIAL"]
        error_summary = f"部分失败: {exec_result.failed_files} 个文件失败"
        error_count = exec_result.failed_files
    else:
        result_status = AUDIT_RESULT_STATUS["FAILED"]
        error_summary = exec_result.error or "执行失败"
        error_count = max(error_count, exec_result.failed_files, 1)

    _try_audit_record(
        audit, AUDIT_COMMAND_TYPES["RUN"], cfg.operator,
        result_status,
        params_summary={**params, "manifest": str(cfg.manifest_path), "last_batch_id": last_id},
        config_summary=cfg.summary(),
        batch_id=batch_id_out,
        package_names=pkg_names,
        file_count=file_count,
        error_count=error_count,
        warning_count=warning_count,
        error_summary=error_summary,
        detail_ref={"copied": exec_result.copied_files, "zipped": exec_result.zipped_files, "failed": exec_result.failed_files},
    )

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
    table.add_column("父批次", overflow="fold")
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
            b.parent_batch_id or "-",
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
    if batch.parent_batch_id:
        console.print(f"[bold]父批次[/bold]: {batch.parent_batch_id}")
    if batch.rerun_params:
        console.print(f"[bold]重跑参数[/bold]: {batch.rerun_params}")
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
    audit = _get_audit_storage(cfg)
    pkg_names = _package_names_from_cfg(cfg)

    engine = Engine(cfg)
    ok, msg = engine.rollback(batch_id)

    result_status = AUDIT_RESULT_STATUS["SUCCESS"] if ok else AUDIT_RESULT_STATUS["FAILED"]
    _try_audit_record(
        audit, AUDIT_COMMAND_TYPES["ROLLBACK"], cfg.operator,
        result_status,
        params_summary={"batch_id": batch_id},
        config_summary=cfg.summary(),
        batch_id=batch_id,
        package_names=pkg_names,
        error_count=0 if ok else 1,
        error_summary="" if ok else msg,
    )

    if ok:
        console.print(f"[green]{msg}[/green]")
    else:
        console.print(f"[red]{msg}[/red]")
        ctx.exit(4)


@main.command()
@_config_option
@click.argument("batch_id")
@click.option("--manifest", "-m", "manifest_path", default=None, help="替换原 manifest 的 CSV 文件路径（默认沿用原批次）")
@click.option("--source-root", "source_root", default=None, help="替换原 source_root（默认沿用原批次）")
@click.option("--output-root", "output_root", default=None, help="新的输出根目录，所有包的 output_dir 和 zip_output 重定向到此目录下")
@click.option("--zip/--no-zip", default=False, help="是否生成 zip 压缩包")
@click.option("--force", is_flag=True, help="跳过预检错误直接重跑（不推荐）")
@click.pass_context
def rerun(
    ctx: click.Context,
    config_path: str | None,
    batch_id: str,
    manifest_path: str | None,
    source_root: str | None,
    output_root: str | None,
    zip: bool,
    force: bool,
):
    """按历史批次重跑（基于 completed/partial 批次生成新计划并执行）

    默认禁止覆盖已有交付文件和 zip。重跑前会执行 dry-run 级别预检。
    """
    cfg = _get_cfg(ctx, config_path)
    audit = _get_audit_storage(cfg)
    pkg_names = _package_names_from_cfg(cfg)
    params = {
        "parent_batch_id": batch_id,
        "manifest_path": manifest_path,
        "source_root": source_root,
        "output_root": output_root,
        "zip": zip,
        "force": force,
    }
    error_summary = ""
    error_count = 0
    warning_count = 0
    result_status = AUDIT_RESULT_STATUS["SUCCESS"]
    new_batch_id: Optional[str] = None
    file_count = 0

    storage = BatchStorage(cfg.db_path)
    base_dir = Path.cwd()

    try:
        result = rerun_batch(
            parent_batch_id=batch_id,
            storage=storage,
            base_dir=base_dir,
            manifest_path=Path(manifest_path) if manifest_path else None,
            source_root=Path(source_root) if source_root else None,
            output_root=Path(output_root) if output_root else None,
            make_zip=zip,
            force=force,
        )
    except RerunError as e:
        _try_audit_record(
            audit, AUDIT_COMMAND_TYPES["RERUN"], cfg.operator,
            AUDIT_RESULT_STATUS["FAILED"],
            params_summary=params,
            config_summary=cfg.summary(),
            batch_id=batch_id,
            package_names=pkg_names,
            error_count=1,
            error_summary=str(e),
        )
        console.print(f"[red]重跑失败: {e}[/red]")
        ctx.exit(8)

    new_batch_id = result.batch_id
    file_count = result.copied_files + result.zipped_files
    if result.precheck:
        error_count = len(result.precheck.errors)
        warning_count = len(result.precheck.warnings)

    if result.status == BATCH_STATUS["COMPLETED"]:
        result_status = AUDIT_RESULT_STATUS["SUCCESS"]
    elif result.status == BATCH_STATUS["PARTIAL"]:
        result_status = AUDIT_RESULT_STATUS["PARTIAL"]
        error_summary = f"部分失败: {result.failed_files} 个文件失败"
        error_count = max(error_count, result.failed_files)
    else:
        result_status = AUDIT_RESULT_STATUS["FAILED"]
        error_summary = result.error or "重跑执行失败"
        error_count = max(error_count, result.failed_files, 1)

    _try_audit_record(
        audit, AUDIT_COMMAND_TYPES["RERUN"], cfg.operator,
        result_status,
        params_summary=params,
        config_summary=cfg.summary(),
        batch_id=new_batch_id,
        package_names=pkg_names,
        file_count=file_count,
        error_count=error_count,
        warning_count=warning_count,
        error_summary=error_summary,
        detail_ref={"parent_batch_id": batch_id, "copied": result.copied_files, "zipped": result.zipped_files, "failed": result.failed_files},
    )

    status_color = {
        BATCH_STATUS["COMPLETED"]: "green",
        BATCH_STATUS["PARTIAL"]: "yellow",
        BATCH_STATUS["FAILED"]: "red",
    }.get(result.status, "white")

    console.print(
        f"重跑批次 [bold]{result.batch_id}[/bold] (父批次 {result.parent_batch_id}) 执行完毕，"
        f"状态: [bold {status_color}]{result.status}[/bold {status_color}]"
    )
    console.print(
        f"  复制成功: {result.copied_files}  zip: {result.zipped_files}  失败: {result.failed_files}"
    )
    if result.error:
        console.print(f"[red]错误: {result.error}[/red]")

    if result.precheck:
        if result.precheck.errors:
            _print_issues(result.precheck.errors)
        if result.precheck.warnings:
            _print_issues(result.precheck.warnings)

    if result.status == BATCH_STATUS["FAILED"]:
        ctx.exit(3)


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
    audit = _get_audit_storage(cfg)
    params = {"format": fmt, "output": output_path, "batch_id": batch_id, "limit": limit}
    storage = BatchStorage(cfg.db_path)
    batch_count = 0

    if batch_id:
        b = storage.get_batch(batch_id)
        if not b:
            _try_audit_record(
                audit, AUDIT_COMMAND_TYPES["EXPORT"], cfg.operator,
                AUDIT_RESULT_STATUS["FAILED"],
                params_summary=params,
                config_summary=cfg.summary(),
                error_count=1,
                error_summary=f"批次不存在: {batch_id}",
            )
            console.print(f"[red]批次不存在: {batch_id}[/red]")
            ctx.exit(1)
        batches = [b]
    else:
        batches = storage.list_batches(limit=limit)

    out = Path(output_path)
    try:
        if fmt == "json":
            export_json(batches, out)
        else:
            export_csv(batches, out)
        batch_count = len(batches)
    except Exception as e:
        _try_audit_record(
            audit, AUDIT_COMMAND_TYPES["EXPORT"], cfg.operator,
            AUDIT_RESULT_STATUS["FAILED"],
            params_summary=params,
            config_summary=cfg.summary(),
            error_count=1,
            error_summary=str(e),
            detail_ref={"batch_count": len(batches)},
        )
        console.print(f"[red]导出失败: {e}[/red]")
        ctx.exit(1)

    _try_audit_record(
        audit, AUDIT_COMMAND_TYPES["EXPORT"], cfg.operator,
        AUDIT_RESULT_STATUS["SUCCESS"],
        params_summary=params,
        config_summary=cfg.summary(),
        file_count=batch_count,
        detail_ref={"batch_count": batch_count},
    )
    console.print(f"[green]已导出 {len(batches)} 个批次到 {out}[/green]")


@main.command()
@_config_option
@click.option("--batch-id", "batch_id", default=None, help="以指定历史批次为基准进行对比")
@click.option("--dir", "dir_path", default=None, type=click.Path(path_type=Path), help="以指定目录为基准进行对比")
@click.option("--format", "-f", "fmt", type=click.Choice(["json", "csv"]), default=None, help="导出格式（不指定则仅打印到终端）")
@click.option("--output", "-o", "output_path", default=None, help="导出文件路径（指定 --format 时必填）")
@click.option("--show-unchanged/--hide-unchanged", default=False, show_default=True, help="是否显示无变化的条目")
@click.pass_context
def diff(
    ctx: click.Context,
    config_path: str | None,
    batch_id: str | None,
    dir_path: Path | None,
    fmt: str | None,
    output_path: str | None,
    show_unchanged: bool,
):
    """对比当前配置+CSV清单与历史批次或指定目录的交付结果差异

    输出：新增、缺失、文件名变化、版本变化、包归属变化、zip状态差异
    """
    if not batch_id and not dir_path:
        console.print("[red]错误: 必须指定 --batch-id 或 --dir 其中之一作为基准[/red]")
        ctx.exit(1)
    if batch_id and dir_path:
        console.print("[red]错误: --batch-id 和 --dir 不能同时指定[/red]")
        ctx.exit(1)
    if fmt and not output_path:
        console.print("[red]错误: 指定 --format 时必须同时指定 --output[/red]")
        ctx.exit(1)

    cfg = _get_cfg(ctx, config_path)
    audit = _get_audit_storage(cfg)
    baseline_type = "batch" if batch_id else "dir"
    baseline_ref = str(batch_id) if batch_id else str(dir_path)
    params = {
        "baseline_type": baseline_type,
        "baseline_ref": baseline_ref,
        "show_unchanged": show_unchanged,
        "format": fmt,
        "output": output_path,
    }
    total_diff_items = 0
    error_count = 0
    warning_count = 0
    error_summary = ""

    try:
        entries = load_manifest(cfg.manifest_path)
    except Exception as e:
        _try_audit_record(
            audit, AUDIT_COMMAND_TYPES["DIFF"], cfg.operator,
            AUDIT_RESULT_STATUS["FAILED"],
            params_summary=params,
            config_summary=cfg.summary(),
            error_count=1,
            error_summary=f"加载清单失败: {e}",
        )
        console.print(f"[red]加载清单失败:[/red] {e}")
        ctx.exit(1)

    storage = BatchStorage(cfg.db_path)

    try:
        if batch_id:
            result = diff_against_batch(cfg, entries, storage, batch_id)
        else:
            result = diff_against_directory(cfg, entries, dir_path)
    except DiffError as e:
        _try_audit_record(
            audit, AUDIT_COMMAND_TYPES["DIFF"], cfg.operator,
            AUDIT_RESULT_STATUS["FAILED"],
            params_summary=params,
            config_summary=cfg.summary(),
            error_count=1,
            error_summary=str(e),
        )
        console.print(f"[red]对比失败: {e}[/red]")
        ctx.exit(1)

    total_diff_items = len(result.items)
    error_count = sum(1 for e in result.errors if e.level == "error")
    warning_count = sum(1 for e in result.errors if e.level == "warning")

    _print_diff_result(result, show_unchanged=show_unchanged)

    if result.errors:
        has_errors = any(e.level == "error" for e in result.errors)
        if has_errors:
            console.print(f"[yellow]对比过程中有 {sum(1 for e in result.errors if e.level == 'error')} 个错误，以上结果可能不准确。[/yellow]")
            error_msgs = [f"[{e.kind}] {e.message}" for e in result.errors if e.level == "error"]
            error_summary = "；".join(error_msgs[:5])

    export_success = True
    if fmt and output_path:
        out = Path(output_path)
        try:
            if fmt == "json":
                export_diff_json(result, out)
            else:
                export_diff_csv(result, out)
            console.print(f"[green]差异报告已导出到 {out}[/green]")
        except PermissionError as e:
            export_success = False
            error_summary = f"导出失败: 权限不足 - {e}"
            error_count += 1
            console.print(f"[red]导出失败: 权限不足 - {e}[/red]")
        except OSError as e:
            export_success = False
            error_summary = f"导出失败: {e}"
            error_count += 1
            console.print(f"[red]导出失败: {e}[/red]")

    has_diff_errors = any(e.level == "error" for e in result.errors)
    if has_diff_errors or not export_success:
        result_status = AUDIT_RESULT_STATUS["FAILED"]
    else:
        result_status = AUDIT_RESULT_STATUS["SUCCESS"]

    _try_audit_record(
        audit, AUDIT_COMMAND_TYPES["DIFF"], cfg.operator,
        result_status,
        params_summary=params,
        config_summary=cfg.summary(),
        batch_id=batch_id,
        file_count=total_diff_items,
        error_count=error_count,
        warning_count=warning_count,
        error_summary=error_summary,
        detail_ref={
            "baseline_type": baseline_type,
            "baseline_ref": baseline_ref,
            "total_diff_items": total_diff_items,
        },
    )

    if not export_success:
        ctx.exit(1)
    if has_diff_errors:
        ctx.exit(10)


def _print_diff_result(result: DiffResult, show_unchanged: bool = False):
    """打印差异对比结果到终端"""
    baseline_desc = (
        f"批次 {result.baseline_ref}" if result.baseline_kind == "batch" else f"目录 {result.baseline_ref}"
    )
    console.print(f"[bold]交付包差异对比[/bold] (基准: {baseline_desc})")
    console.print(f"  生成时间: {result.generated_at}")
    console.print(f"  预期交付项: {result.total_expected}  基准交付项: {result.total_baseline}")

    summary_colors = {
        DiffChangeType.ADDED: "green",
        DiffChangeType.MISSING: "red",
        DiffChangeType.RENAMED: "cyan",
        DiffChangeType.VERSION_CHANGED: "yellow",
        DiffChangeType.PACKAGE_CHANGED: "magenta",
        DiffChangeType.ZIP_STATUS_CHANGED: "blue",
        DiffChangeType.CONTENT_CHANGED: "yellow",
        DiffChangeType.UNCHANGED: "white",
    }

    summary_parts = []
    for ct in [
        DiffChangeType.ADDED,
        DiffChangeType.MISSING,
        DiffChangeType.RENAMED,
        DiffChangeType.VERSION_CHANGED,
        DiffChangeType.PACKAGE_CHANGED,
        DiffChangeType.ZIP_STATUS_CHANGED,
        DiffChangeType.CONTENT_CHANGED,
    ]:
        count = len([i for i in result.items if i.change_type == ct])
        if count > 0:
            color = summary_colors.get(ct, "white")
            summary_parts.append(f"[{color}]{DIFF_CHANGE_LABELS[ct]}: {count}[/{color}]")
    if summary_parts:
        console.print("  差异汇总: " + "  ".join(summary_parts))
    else:
        console.print("  [green]差异汇总: 无差异[/green]")

    if result.errors:
        error_table = Table(title="对比错误/警告")
        error_table.add_column("级别", style="red")
        error_table.add_column("类型")
        error_table.add_column("消息")
        error_table.add_column("详情", overflow="fold")
        for e in result.errors:
            style = "red" if e.level == "error" else "yellow"
            error_table.add_row(f"[{style}]{e.level}[/{style}]", e.kind, e.message, e.detail or "")
        console.print(error_table)

    display_items = result.items
    if not show_unchanged:
        display_items = [i for i in result.items if i.change_type != DiffChangeType.UNCHANGED]

    if not display_items:
        if show_unchanged:
            console.print("[green]所有交付项一致[/green]")
        return

    diff_table = Table(title=f"差异详情 ({len(display_items)} 条)")
    diff_table.add_column("类型", min_width=10)
    diff_table.add_column("包")
    diff_table.add_column("分类")
    diff_table.add_column("当前目标名", overflow="fold")
    diff_table.add_column("基准目标名", overflow="fold")
    diff_table.add_column("当前版本")
    diff_table.add_column("基准版本")
    diff_table.add_column("详情", overflow="fold", max_width=40)

    for item in display_items:
        ct = item.change_type
        color = summary_colors.get(ct, "white")
        label = DIFF_CHANGE_LABELS.get(ct, ct.value)
        diff_table.add_row(
            f"[{color}]{label}[/{color}]",
            item.package,
            item.category if item.category != "__zip__" else "[zip]",
            item.target_name,
            item.baseline_target_name or "",
            item.version or "",
            item.baseline_version or "",
            item.detail or "",
        )

    console.print(diff_table)


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
    audit = _get_audit_storage(cfg)
    package_count = len(cfg.packages)
    params = {"template_name": name, "package_count": package_count}
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
        _try_audit_record(
            audit, AUDIT_COMMAND_TYPES["TEMPLATE_SAVE"], cfg.operator,
            AUDIT_RESULT_STATUS["FAILED"],
            params_summary=params,
            config_summary=cfg.summary(),
            file_count=package_count,
            error_count=1,
            error_summary=str(e),
        )
        console.print(f"[red]保存失败: {e}[/red]")
        console.print("[yellow]提示: 使用 'contract-pack template list' 查看已有模板，或使用其他名称。[/yellow]")
        ctx.exit(5)
    except ValueError as e:
        _try_audit_record(
            audit, AUDIT_COMMAND_TYPES["TEMPLATE_SAVE"], cfg.operator,
            AUDIT_RESULT_STATUS["FAILED"],
            params_summary=params,
            config_summary=cfg.summary(),
            file_count=package_count,
            error_count=1,
            error_summary=str(e),
        )
        console.print(f"[red]保存失败: {e}[/red]")
        ctx.exit(5)

    _try_audit_record(
        audit, AUDIT_COMMAND_TYPES["TEMPLATE_SAVE"], cfg.operator,
        AUDIT_RESULT_STATUS["SUCCESS"],
        params_summary=params,
        config_summary=cfg.summary(),
        file_count=len(tpl.packages),
    )
    console.print(f"[green]模板已保存:[/green] {tpl.name} (创建于 {tpl.created_at})")
    console.print(f"  包含 {len(tpl.packages)} 个包: {', '.join(p.name for p in tpl.packages)}")


@template_group.command("list")
@_config_option
@click.pass_context
def template_list_cmd(ctx: click.Context, config_path: str | None):
    """列出所有已保存的交付方案模板"""
    cfg = _get_cfg(ctx, config_path)
    audit = _get_audit_storage(cfg)
    storage = TemplateStorage(cfg.db_path)
    templates = storage.list_templates()

    _try_audit_record(
        audit, AUDIT_COMMAND_TYPES["TEMPLATE_LIST"], cfg.operator,
        AUDIT_RESULT_STATUS["SUCCESS"],
        params_summary={"template_count": len(templates)},
        config_summary=cfg.summary(),
        file_count=len(templates),
    )

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
    audit = _get_audit_storage(cfg)
    params = {"template_name": name}
    storage = TemplateStorage(cfg.db_path)
    tpl = storage.get_template(name)
    if not tpl:
        _try_audit_record(
            audit, AUDIT_COMMAND_TYPES["TEMPLATE_SHOW"], cfg.operator,
            AUDIT_RESULT_STATUS["FAILED"],
            params_summary=params,
            config_summary=cfg.summary(),
            error_count=1,
            error_summary=f"模板不存在: {name}",
        )
        console.print(f"[red]模板不存在: {name}[/red]")
        console.print("[yellow]提示: 使用 'contract-pack template list' 查看已有模板。[/yellow]")
        ctx.exit(1)

    _try_audit_record(
        audit, AUDIT_COMMAND_TYPES["TEMPLATE_SHOW"], cfg.operator,
        AUDIT_RESULT_STATUS["SUCCESS"],
        params_summary={**params, "package_count": len(tpl.packages)},
        config_summary=cfg.summary(),
        file_count=len(tpl.packages),
    )

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
    audit = _get_audit_storage(cfg)
    params = {"template_name": name, "force": force}
    storage = TemplateStorage(cfg.db_path)
    tpl = storage.get_template(name)
    if not tpl:
        _try_audit_record(
            audit, AUDIT_COMMAND_TYPES["TEMPLATE_DELETE"], cfg.operator,
            AUDIT_RESULT_STATUS["FAILED"],
            params_summary={**params, "success": False},
            config_summary=cfg.summary(),
            error_count=1,
            error_summary=f"模板不存在: {name}",
        )
        console.print(f"[red]模板不存在: {name}[/red]")
        ctx.exit(1)
    if not force:
        console.print(f"[yellow]即将删除模板: {name} (包含 {len(tpl.packages)} 个包)[/yellow]")
        confirmed = click.confirm("确认删除？", default=False)
        if not confirmed:
            _try_audit_record(
                audit, AUDIT_COMMAND_TYPES["TEMPLATE_DELETE"], cfg.operator,
                AUDIT_RESULT_STATUS["SKIPPED"],
                params_summary={**params, "success": False},
                config_summary=cfg.summary(),
                error_summary="用户取消删除",
            )
            console.print("已取消")
            return
    deleted = storage.delete_template(name)
    if deleted:
        _try_audit_record(
            audit, AUDIT_COMMAND_TYPES["TEMPLATE_DELETE"], cfg.operator,
            AUDIT_RESULT_STATUS["SUCCESS"],
            params_summary={**params, "success": True, "package_count": len(tpl.packages)},
            config_summary=cfg.summary(),
            file_count=len(tpl.packages),
        )
        console.print(f"[green]模板已删除: {name}[/green]")
    else:
        _try_audit_record(
            audit, AUDIT_COMMAND_TYPES["TEMPLATE_DELETE"], cfg.operator,
            AUDIT_RESULT_STATUS["FAILED"],
            params_summary={**params, "success": False},
            config_summary=cfg.summary(),
            error_count=1,
            error_summary=f"删除失败: {name}",
        )
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
    audit = _get_audit_storage(cfg)
    params = {"format": fmt, "output": output_path, "template_name": name}
    storage = TemplateStorage(cfg.db_path)
    if name:
        tpl = storage.get_template(name)
        if not tpl:
            _try_audit_record(
                audit, AUDIT_COMMAND_TYPES["TEMPLATE_EXPORT"], cfg.operator,
                AUDIT_RESULT_STATUS["FAILED"],
                params_summary=params,
                config_summary=cfg.summary(),
                error_count=1,
                error_summary=f"模板不存在: {name}",
            )
            console.print(f"[red]模板不存在: {name}[/red]")
            ctx.exit(1)
        templates = [tpl]
    else:
        templates = storage.list_templates()
    if not templates:
        _try_audit_record(
            audit, AUDIT_COMMAND_TYPES["TEMPLATE_EXPORT"], cfg.operator,
            AUDIT_RESULT_STATUS["FAILED"],
            params_summary=params,
            config_summary=cfg.summary(),
            error_count=1,
            error_summary="没有可导出的模板",
        )
        console.print("[yellow]没有可导出的模板[/yellow]")
        ctx.exit(1)
    out = Path(output_path)
    template_count = len(templates)
    template_names = [t.name for t in templates]
    try:
        if fmt == "json":
            export_template_json(templates, out)
        else:
            export_template_csv(templates, out)
    except (PermissionError, OSError) as e:
        _try_audit_record(
            audit, AUDIT_COMMAND_TYPES["TEMPLATE_EXPORT"], cfg.operator,
            AUDIT_RESULT_STATUS["FAILED"],
            params_summary={**params, "template_count": template_count, "template_names": template_names},
            config_summary=cfg.summary(),
            file_count=template_count,
            error_count=1,
            error_summary=str(e),
        )
        console.print(f"[red]导出失败: {e}（可能是只读目录或权限不足）[/red]")
        ctx.exit(1)

    _try_audit_record(
        audit, AUDIT_COMMAND_TYPES["TEMPLATE_EXPORT"], cfg.operator,
        AUDIT_RESULT_STATUS["SUCCESS"],
        params_summary={**params, "template_count": template_count, "template_names": template_names},
        config_summary=cfg.summary(),
        file_count=template_count,
    )
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
    audit = _get_audit_storage(cfg)
    pkg_names = _package_names_from_cfg(cfg)
    params = {"format": fmt, "input_path": input_path, "force": force}
    storage = TemplateStorage(cfg.db_path)
    in_file = Path(input_path)

    try:
        if fmt == "json":
            imported = import_template_json(in_file)
        else:
            imported = import_template_csv(in_file)
    except TemplateImportError as e:
        _try_audit_record(
            audit, AUDIT_COMMAND_TYPES["TEMPLATE_IMPORT"], cfg.operator,
            AUDIT_RESULT_STATUS["FAILED"],
            params_summary=params,
            config_summary=cfg.summary(),
            package_names=pkg_names,
            error_count=1,
            error_summary=str(e),
        )
        console.print(f"[red]导入失败: {e}[/red]")
        ctx.exit(7)

    if not imported:
        _try_audit_record(
            audit, AUDIT_COMMAND_TYPES["TEMPLATE_IMPORT"], cfg.operator,
            AUDIT_RESULT_STATUS["FAILED"],
            params_summary=params,
            config_summary=cfg.summary(),
            package_names=pkg_names,
            error_count=1,
            error_summary="文件中没有可导入的模板",
        )
        console.print("[yellow]文件中没有可导入的模板[/yellow]")
        ctx.exit(1)

    saved = 0
    skipped = 0
    error_count = 0
    error_summary = ""
    imported_template_names = [t.name for t in imported]
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
            error_count += 1
            error_summary = f"保存模板 '{tpl.name}' 失败: {e}"
            _try_audit_record(
                audit, AUDIT_COMMAND_TYPES["TEMPLATE_IMPORT"], cfg.operator,
                AUDIT_RESULT_STATUS["FAILED"],
                params_summary={**params, "saved": saved, "skipped": skipped, "imported_template_names": imported_template_names},
                config_summary=cfg.summary(),
                package_names=pkg_names,
                file_count=len(imported),
                warning_count=skipped,
                error_count=error_count,
                error_summary=error_summary,
            )
            console.print(f"[red]保存模板 '{tpl.name}' 失败: {e}[/red]")
            ctx.exit(7)

    if error_count > 0 or saved == 0 and skipped > 0:
        result_status = AUDIT_RESULT_STATUS["PARTIAL"] if saved > 0 else AUDIT_RESULT_STATUS["FAILED"]
    elif skipped > 0:
        result_status = AUDIT_RESULT_STATUS["PARTIAL"]
    else:
        result_status = AUDIT_RESULT_STATUS["SUCCESS"]
    _try_audit_record(
        audit, AUDIT_COMMAND_TYPES["TEMPLATE_IMPORT"], cfg.operator,
        result_status,
        params_summary={**params, "saved": saved, "skipped": skipped, "imported_template_names": imported_template_names},
        config_summary=cfg.summary(),
        package_names=pkg_names,
        file_count=len(imported),
        warning_count=skipped,
        error_count=error_count,
        error_summary=error_summary,
    )
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
    audit = _get_audit_storage(cfg)
    params = {
        "template_name": template_name,
        "manifest": manifest_path,
        "output": output_path,
        "skip_dry_run": skip_dry_run,
        "source_root": source_root,
        "operator_override": operator,
        "db_path_override": db_path,
    }
    storage = TemplateStorage(cfg.db_path)
    tpl = storage.get_template(template_name)
    if not tpl:
        _try_audit_record(
            audit, AUDIT_COMMAND_TYPES["TEMPLATE_APPLY"], cfg.operator,
            AUDIT_RESULT_STATUS["FAILED"],
            params_summary=params,
            config_summary=cfg.summary(),
            error_count=1,
            error_summary=f"模板不存在: {template_name}",
        )
        console.print(f"[red]模板不存在: {template_name}[/red]")
        console.print("[yellow]提示: 使用 'contract-pack template list' 查看已有模板。[/yellow]")
        ctx.exit(1)

    console.print(f"套用模板 [bold]{tpl.name}[/bold] 生成配置草稿...")
    console.print(f"  清单: {manifest_path}")
    console.print(f"  输出: {output_path}")

    error_count = 0
    warning_count = 0
    file_count = 0
    error_summary = ""
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
        if precheck_result:
            error_count = len(precheck_result.errors)
            warning_count = len(precheck_result.warnings)
            file_count = sum(len(v) for v in precheck_result.plan.values())
            if not precheck_result.ok:
                error_msgs = [f"[{e.kind}] {e.package}: {e.message}" for e in precheck_result.errors]
                error_summary = "；".join(error_msgs[:5])
    except TemplateApplyError as e:
        error_count = len(e.issues) if e.issues else 1
        warning_count = sum(1 for i in e.issues if i.level == "warning") if e.issues else 0
        _try_audit_record(
            audit, AUDIT_COMMAND_TYPES["TEMPLATE_APPLY"], cfg.operator,
            AUDIT_RESULT_STATUS["FAILED"],
            params_summary={**params, "package_count": len(tpl.packages)},
            config_summary=cfg.summary(),
            file_count=len(tpl.packages),
            error_count=error_count,
            warning_count=warning_count,
            error_summary=str(e),
        )
        console.print(f"[red]模板套用失败: {e}[/red]")
        if e.issues:
            _print_issues(e.issues)
        console.print("[yellow]提示: 数据库状态未被修改，可修复问题后重试。[/yellow]")
        ctx.exit(6)

    result_status = AUDIT_RESULT_STATUS["SUCCESS"] if error_count == 0 else AUDIT_RESULT_STATUS["PARTIAL"]
    _try_audit_record(
        audit, AUDIT_COMMAND_TYPES["TEMPLATE_APPLY"], cfg.operator,
        result_status,
        params_summary={**params, "package_count": len(tpl.packages)},
        config_summary=cfg.summary(),
        file_count=file_count if file_count > 0 else len(tpl.packages),
        error_count=error_count,
        warning_count=warning_count,
        error_summary=error_summary,
    )

    console.print(f"[green]配置草稿已生成:[/green] {generated}")
    if not skip_dry_run:
        console.print(f"  dry-run 通过，条目数: {sum(len(v) for v in precheck_result.plan.values())}")
        if precheck_result.warnings:
            _print_issues(precheck_result.warnings)
    console.print("[yellow]提示: 请检查生成的配置，确认后使用 'contract-pack dry-run' 再验证，然后 'contract-pack run' 执行。[/yellow]")


@main.group("audit")
@click.pass_context
def audit_group(ctx: click.Context):
    """审计记录查询与导出"""
    pass


@audit_group.command("list")
@_config_option
@click.option("--command-type", "command_type", default=None, help="按命令类型过滤")
@click.option("--operator", "operator", default=None, help="按操作者过滤")
@click.option("--result-status", "result_status", default=None, help="按结果状态过滤 (success/failed/partial/skipped)")
@click.option("--start-time", "start_time", default=None, help="起始时间 (ISO 格式, 如 2024-01-01T00:00:00)")
@click.option("--end-time", "end_time", default=None, help="结束时间 (ISO 格式)")
@click.option("--batch-id", "batch_id", default=None, help="按批次 ID 过滤")
@click.option("--package", "package", default=None, help="按交付包名过滤")
@click.option("--limit", "-n", default=50, show_default=True, help="显示最近多少条")
@click.pass_context
def audit_list(
    ctx: click.Context,
    config_path: str | None,
    command_type: str | None,
    operator: str | None,
    result_status: str | None,
    start_time: str | None,
    end_time: str | None,
    batch_id: str | None,
    package: str | None,
    limit: int,
):
    """列出审计记录，支持多维度过滤"""
    cfg = _get_cfg(ctx, config_path)
    svc = _get_audit_service(cfg)
    params = {
        "start_time": start_time,
        "end_time": end_time,
        "operator": operator,
        "command_type": command_type,
        "batch_id": batch_id,
        "package": package,
        "result_status": result_status,
        "limit": limit,
    }

    if not svc.enabled:
        console.print("[yellow]审计功能已关闭或不可用[/yellow]")
        return

    try:
        records = svc.query(
            start_time=start_time,
            end_time=end_time,
            operator=operator,
            command_type=command_type,
            batch_id=batch_id,
            package_name=package,
            result_status=result_status,
            limit=limit,
        )
    except AuditError as e:
        svc.try_record(
            AUDIT_COMMAND_TYPES["AUDIT_QUERY"], cfg.operator,
            AUDIT_RESULT_STATUS["FAILED"],
            params_summary=params,
            config_summary=cfg.summary(),
            error_count=1,
            error_summary=str(e),
        )
        console.print(f"[red]查询失败:[/red] {e}")
        ctx.exit(1)

    svc.try_record(
        AUDIT_COMMAND_TYPES["AUDIT_QUERY"], cfg.operator,
        AUDIT_RESULT_STATUS["SUCCESS"],
        params_summary={**params, "record_count": len(records)},
        config_summary=cfg.summary(),
        file_count=len(records),
    )

    if not records:
        console.print("[yellow]暂无审计记录[/yellow]")
        return

    table = Table(title=f"审计记录 (最近 {len(records)} 条)")
    table.add_column("ID", overflow="fold", max_width=20)
    table.add_column("命令类型", min_width=14)
    table.add_column("操作者")
    table.add_column("开始时间")
    table.add_column("状态")
    table.add_column("批次", overflow="fold", max_width=16)
    table.add_column("文件/错误/警告")
    table.add_column("错误摘要", overflow="fold", max_width=30)

    status_colors = {
        AUDIT_RESULT_STATUS["SUCCESS"]: "green",
        AUDIT_RESULT_STATUS["FAILED"]: "red",
        AUDIT_RESULT_STATUS["PARTIAL"]: "yellow",
        AUDIT_RESULT_STATUS["SKIPPED"]: "white",
    }

    for r in records:
        sc = status_colors.get(r.result_status, "white")
        batch_display = r.batch_id[:16] + "…" if r.batch_id and len(r.batch_id) > 16 else (r.batch_id or "-")
        stats = f"{r.file_count}/{r.error_count}/{r.warning_count}"
        table.add_row(
            r.id,
            r.command_type,
            r.operator,
            r.started_at,
            f"[{sc}]{r.result_status}[/{sc}]",
            batch_display,
            stats,
            r.error_summary or "",
        )
    console.print(table)


@audit_group.command("show")
@_config_option
@click.argument("record_id")
@click.pass_context
def audit_show(ctx: click.Context, config_path: str | None, record_id: str):
    """查看单条审计记录详情"""
    cfg = _get_cfg(ctx, config_path)
    svc = _get_audit_service(cfg)
    params = {"record_id": record_id}

    if not svc.enabled:
        console.print("[yellow]审计功能已关闭或不可用[/yellow]")
        ctx.exit(1)

    try:
        record = svc.get(record_id)
    except AuditError as e:
        svc.try_record(
            AUDIT_COMMAND_TYPES["AUDIT_QUERY"], cfg.operator,
            AUDIT_RESULT_STATUS["FAILED"],
            params_summary=params,
            config_summary=cfg.summary(),
            error_count=1,
            error_summary=str(e),
        )
        console.print(f"[red]查询失败:[/red] {e}")
        ctx.exit(1)

    if not record:
        svc.try_record(
            AUDIT_COMMAND_TYPES["AUDIT_QUERY"], cfg.operator,
            AUDIT_RESULT_STATUS["FAILED"],
            params_summary=params,
            config_summary=cfg.summary(),
            error_count=1,
            error_summary=f"审计记录不存在: {record_id}",
        )
        console.print(f"[red]审计记录不存在: {record_id}[/red]")
        ctx.exit(1)

    svc.try_record(
        AUDIT_COMMAND_TYPES["AUDIT_QUERY"], cfg.operator,
        AUDIT_RESULT_STATUS["SUCCESS"],
        params_summary={**params, "target_command_type": record.command_type, "target_operator": record.operator},
        config_summary=cfg.summary(),
    )

    d = record.to_dict()
    console.print(f"[bold]审计记录 ID[/bold]: {d['id']}")
    console.print(f"[bold]命令类型[/bold]: {d['command_type']}")
    console.print(f"[bold]操作者[/bold]: {d['operator']}")
    console.print(f"[bold]开始时间[/bold]: {d['started_at']}")
    console.print(f"[bold]结束时间[/bold]: {d['finished_at'] or '-'}")
    console.print(f"[bold]耗时(秒)[/bold]: {d['duration_seconds'] if d['duration_seconds'] is not None else '-'}")
    status_color = {"success": "green", "failed": "red", "partial": "yellow", "skipped": "white"}.get(d['result_status'], "white")
    console.print(f"[bold]结果状态[/bold]: [{status_color}]{d['result_status']}[/{status_color}]")
    console.print(f"[bold]批次 ID[/bold]: {d['batch_id'] or '-'}")
    console.print(f"[bold]交付包[/bold]: {', '.join(d['package_names']) if d['package_names'] else '-'}")
    console.print(f"[bold]文件数[/bold]: {d['file_count']}  [bold]错误数[/bold]: {d['error_count']}  [bold]警告数[/bold]: {d['warning_count']}")
    if d['error_summary']:
        console.print(f"[bold]错误摘要[/bold]: [red]{d['error_summary']}[/red]")
    if d['params_summary']:
        console.print(f"[bold]参数摘要[/bold]: {json.dumps(d['params_summary'], ensure_ascii=False)}")
    if d['detail_ref']:
        console.print(f"[bold]详情引用[/bold]: {json.dumps(d['detail_ref'], ensure_ascii=False)}")


@audit_group.command("export")
@_config_option
@click.option("--format", "-f", "fmt", type=click.Choice(["json", "csv"]), default="json", show_default=True, help="导出格式")
@click.option("--output", "-o", "output_path", default=None, help="输出文件路径 (未指定时使用配置中的默认导出目录)")
@click.option("--command-type", "command_type", default=None, help="按命令类型过滤")
@click.option("--operator", "operator", default=None, help="按操作者过滤")
@click.option("--result-status", "result_status", default=None, help="按结果状态过滤")
@click.option("--start-time", "start_time", default=None, help="起始时间")
@click.option("--end-time", "end_time", default=None, help="结束时间")
@click.option("--batch-id", "batch_id", default=None, help="按批次 ID 过滤")
@click.option("--package", "package", default=None, help="按交付包名过滤")
@click.option("--limit", "-n", default=500, show_default=True, help="导出最近多少条")
@click.pass_context
def audit_export_cmd(
    ctx: click.Context,
    config_path: str | None,
    fmt: str,
    output_path: str | None,
    command_type: str | None,
    operator: str | None,
    result_status: str | None,
    start_time: str | None,
    end_time: str | None,
    batch_id: str | None,
    package: str | None,
    limit: int,
):
    """导出审计记录为 JSON 或 CSV"""
    cfg = _get_cfg(ctx, config_path)
    svc = _get_audit_service(cfg)
    params = {
        "format": fmt,
        "start_time": start_time,
        "end_time": end_time,
        "operator": operator,
        "command_type": command_type,
        "batch_id": batch_id,
        "package": package,
        "result_status": result_status,
        "limit": limit,
    }

    if not svc.enabled:
        console.print("[yellow]审计功能已关闭或不可用[/yellow]")
        ctx.exit(1)

    resolved = svc.resolve_export_path(output_path, fmt)
    if resolved is None:
        svc.try_record(
            AUDIT_COMMAND_TYPES["AUDIT_EXPORT"], cfg.operator,
            AUDIT_RESULT_STATUS["FAILED"],
            params_summary=params,
            config_summary=cfg.summary(),
            error_count=1,
            error_summary="未指定输出路径且未配置默认导出目录",
        )
        console.print("[red]错误: 未指定 --output 且配置中未设置 audit.export_default_dir[/red]")
        ctx.exit(1)

    if not output_path:
        console.print(f"[yellow]未指定输出路径，使用默认导出目录: {resolved}[/yellow]")

    params["output"] = str(resolved)

    try:
        records = svc.query(
            start_time=start_time,
            end_time=end_time,
            operator=operator,
            command_type=command_type,
            batch_id=batch_id,
            package_name=package,
            result_status=result_status,
            limit=limit,
        )
    except AuditError as e:
        svc.try_record(
            AUDIT_COMMAND_TYPES["AUDIT_EXPORT"], cfg.operator,
            AUDIT_RESULT_STATUS["FAILED"],
            params_summary=params,
            config_summary=cfg.summary(),
            error_count=1,
            error_summary=f"查询失败: {e}",
        )
        console.print(f"[red]查询失败:[/red] {e}")
        ctx.exit(1)

    try:
        if fmt == "json":
            svc.export_json(records, resolved)
        else:
            svc.export_csv(records, resolved)
    except (AuditExportError, PermissionError, OSError) as e:
        svc.try_record(
            AUDIT_COMMAND_TYPES["AUDIT_EXPORT"], cfg.operator,
            AUDIT_RESULT_STATUS["FAILED"],
            params_summary={**params, "record_count": len(records)},
            config_summary=cfg.summary(),
            file_count=len(records),
            error_count=1,
            error_summary=str(e),
        )
        console.print(f"[red]导出失败:[/red] {e}")
        ctx.exit(1)

    svc.try_record(
        AUDIT_COMMAND_TYPES["AUDIT_EXPORT"], cfg.operator,
        AUDIT_RESULT_STATUS["SUCCESS"],
        params_summary={**params, "record_count": len(records)},
        config_summary=cfg.summary(),
        file_count=len(records),
    )

    console.print(f"[green]已导出 {len(records)} 条审计记录到 {resolved}[/green]")


if __name__ == "__main__":
    main()
