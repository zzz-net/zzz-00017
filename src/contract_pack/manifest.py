"""CSV 清单解析模块"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional


@dataclass
class ManifestEntry:
    """CSV 清单中的单条记录"""

    package: str
    category: str
    source_path: str
    target_name: str
    version: Optional[str] = None
    description: str = ""
    raw: dict = field(default_factory=dict)

    @property
    def full_source_path(self) -> str:
        return self.source_path


def load_manifest(manifest_path: Path) -> List[ManifestEntry]:
    """加载 CSV 清单文件

    CSV 列:
        package: 所属交付包名
        category: 文件分类 (main/supplement/seal 等)
        source_path: 源文件相对路径 (相对于 source_root)
        target_name: 目标文件名
        version: 版本号 (可选)
        description: 描述 (可选)
    """
    if not manifest_path.exists():
        raise FileNotFoundError(f"清单文件不存在: {manifest_path}")

    entries: List[ManifestEntry] = []
    with open(manifest_path, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        required = {"package", "category", "source_path", "target_name"}
        headers = set(reader.fieldnames or [])
        missing = required - headers
        if missing:
            raise ValueError(f"CSV 清单缺少必要列: {', '.join(sorted(missing))}")

        for row in reader:
            entry = ManifestEntry(
                package=(row.get("package") or "").strip(),
                category=(row.get("category") or "").strip(),
                source_path=(row.get("source_path") or "").strip(),
                target_name=(row.get("target_name") or "").strip(),
                version=(row.get("version") or None) if (row.get("version") or "").strip() else None,
                description=(row.get("description") or "").strip(),
                raw=dict(row),
            )
            entries.append(entry)

    return entries


def group_by_package(entries: List[ManifestEntry]) -> dict:
    """按 package 分组条目"""
    groups: dict = {}
    for entry in entries:
        groups.setdefault(entry.package, []).append(entry)
    return groups
