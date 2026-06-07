"""报告导出模块 - JSON / CSV"""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import List

from .storage import Batch


def batch_to_dict(batch: Batch) -> dict:
    return {
        "id": batch.id,
        "status": batch.status,
        "operator": batch.operator,
        "started_at": batch.started_at,
        "finished_at": batch.finished_at,
        "error": batch.error,
        "config_summary": batch.config_summary,
        "parent_batch_id": batch.parent_batch_id,
        "rerun_params": batch.rerun_params,
        "file_actions": [
            {
                "id": fa.id,
                "package": fa.package,
                "action": fa.action,
                "source_path": fa.source_path,
                "target_path": fa.target_path,
                "category": fa.category,
                "status": fa.status,
                "error": fa.error,
                "started_at": fa.started_at,
                "finished_at": fa.finished_at,
                "file_hash": fa.file_hash,
                "file_size": fa.file_size,
                "version": getattr(fa, "version", None),
            }
            for fa in batch.file_actions
        ],
    }


def export_json(batches: List[Batch], out_path: Path) -> None:
    data = [batch_to_dict(b) for b in batches]
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def export_csv(batches: List[Batch], out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    import json as _json
    fieldnames = [
        "batch_id",
        "batch_status",
        "batch_error",
        "operator",
        "started_at",
        "finished_at",
        "parent_batch_id",
        "rerun_params",
        "package",
        "action",
        "category",
        "source_path",
        "target_path",
        "file_status",
        "file_error",
        "file_hash",
        "file_size",
        "file_version",
    ]
    with open(out_path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for b in batches:
            rerun_params_str = _json.dumps(b.rerun_params, ensure_ascii=False) if b.rerun_params else ""
            base_row = {
                "batch_id": b.id,
                "batch_status": b.status,
                "batch_error": b.error,
                "operator": b.operator,
                "started_at": b.started_at,
                "finished_at": b.finished_at or "",
                "parent_batch_id": b.parent_batch_id or "",
                "rerun_params": rerun_params_str,
            }
            if not b.file_actions:
                writer.writerow({**base_row, **{
                    "package": "",
                    "action": "",
                    "category": "",
                    "source_path": "",
                    "target_path": "",
                    "file_status": "",
                    "file_error": "",
                    "file_hash": "",
                    "file_size": "",
                    "file_version": "",
                }})
            for fa in b.file_actions:
                writer.writerow({**base_row, **{
                    "package": fa.package,
                    "action": fa.action,
                    "category": fa.category,
                    "source_path": fa.source_path,
                    "target_path": fa.target_path,
                    "file_status": fa.status,
                    "file_error": fa.error,
                    "file_hash": fa.file_hash or "",
                    "file_size": fa.file_size if fa.file_size is not None else "",
                    "file_version": getattr(fa, "version", None) or "",
                }})


# ---------------------------------------------------------------------------
# Diff Result Export
# ---------------------------------------------------------------------------

from .diff_core import DiffResult, DiffItem, DiffError, DiffChangeType, DIFF_CHANGE_LABELS


def diff_to_dict(result: DiffResult) -> dict:
    """将 DiffResult 转换为可序列化字典（字段稳定，适合审计留档）"""
    return {
        "baseline_kind": result.baseline_kind,
        "baseline_ref": result.baseline_ref,
        "generated_at": result.generated_at,
        "total_expected": result.total_expected,
        "total_baseline": result.total_baseline,
        "summary": {
            "added": len(result.added),
            "missing": len(result.missing),
            "renamed": len(result.renamed),
            "version_changed": len(result.version_changed),
            "package_changed": len(result.package_changed),
            "zip_status_changed": len(result.zip_status_changed),
            "content_changed": len(result.content_changed),
            "unchanged": len(result.unchanged),
        },
        "errors": [
            {
                "level": e.level,
                "kind": e.kind,
                "message": e.message,
                "detail": e.detail,
            }
            for e in result.errors
        ],
        "items": [
            {
                "change_type": item.change_type.value,
                "change_label": DIFF_CHANGE_LABELS.get(item.change_type, item.change_type.value),
                "package": item.package,
                "category": item.category,
                "target_name": item.target_name,
                "baseline_target_name": item.baseline_target_name or "",
                "version": item.version or "",
                "baseline_version": item.baseline_version or "",
                "baseline_package": item.baseline_package or "",
                "target_path": item.target_path or "",
                "baseline_path": item.baseline_path or "",
                "is_zip": item.is_zip if item.is_zip is not None else "",
                "baseline_is_zip": item.baseline_is_zip if item.baseline_is_zip is not None else "",
                "file_hash": item.file_hash or "",
                "baseline_hash": item.baseline_hash or "",
                "file_size": item.file_size if item.file_size is not None else "",
                "baseline_size": item.baseline_size if item.baseline_size is not None else "",
                "detail": item.detail or "",
            }
            for item in result.items
        ],
    }


def export_diff_json(result: DiffResult, out_path: Path) -> None:
    """导出差异对比结果为 JSON（字段稳定，适合审计留档）"""
    data = diff_to_dict(result)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


DIFF_CSV_FIELDNAMES = [
    "baseline_kind",
    "baseline_ref",
    "generated_at",
    "change_type",
    "change_label",
    "package",
    "baseline_package",
    "category",
    "target_name",
    "baseline_target_name",
    "version",
    "baseline_version",
    "target_path",
    "baseline_path",
    "is_zip",
    "baseline_is_zip",
    "file_hash",
    "baseline_hash",
    "file_size",
    "baseline_size",
    "detail",
]


def export_diff_csv(result: DiffResult, out_path: Path) -> None:
    """导出差异对比结果为 CSV（字段稳定，适合审计留档）

    每行一个差异条目，头部包含基准信息，便于审计归档和表格软件处理。
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    base_info = {
        "baseline_kind": result.baseline_kind,
        "baseline_ref": result.baseline_ref,
        "generated_at": result.generated_at,
    }
    with open(out_path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=DIFF_CSV_FIELDNAMES)
        writer.writeheader()
        if not result.items:
            writer.writerow({**base_info, **{k: "" for k in DIFF_CSV_FIELDNAMES[3:]}})
        for item in result.items:
            row = {
                **base_info,
                "change_type": item.change_type.value,
                "change_label": DIFF_CHANGE_LABELS.get(item.change_type, item.change_type.value),
                "package": item.package,
                "baseline_package": item.baseline_package or "",
                "category": item.category,
                "target_name": item.target_name,
                "baseline_target_name": item.baseline_target_name or "",
                "version": item.version or "",
                "baseline_version": item.baseline_version or "",
                "target_path": item.target_path or "",
                "baseline_path": item.baseline_path or "",
                "is_zip": str(item.is_zip) if item.is_zip is not None else "",
                "baseline_is_zip": str(item.baseline_is_zip) if item.baseline_is_zip is not None else "",
                "file_hash": item.file_hash or "",
                "baseline_hash": item.baseline_hash or "",
                "file_size": str(item.file_size) if item.file_size is not None else "",
                "baseline_size": str(item.baseline_size) if item.baseline_size is not None else "",
                "detail": item.detail or "",
            }
            writer.writerow(row)
