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
