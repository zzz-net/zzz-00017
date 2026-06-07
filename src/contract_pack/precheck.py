"""预检 (dry-run) 模块 - 检测缺失附件、重复目标名、版本倒退、清单外文件和包名冲突"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from .config import AppConfig
from .manifest import ManifestEntry, group_by_package


def _parse_version(v: Optional[str]) -> Tuple[int, ...]:
    """解析版本号为可比较的元组，例如 'v1.2.3' -> (1,2,3)，'R3' -> (3,)，None -> (0,)"""
    if not v:
        return (0,)
    digits = re.findall(r"\d+", v)
    if not digits:
        return (0,)
    return tuple(int(d) for d in digits)


@dataclass
class PrecheckIssue:
    level: str
    kind: str
    package: str
    message: str
    detail: str = ""


@dataclass
class PrecheckResult:
    issues: List[PrecheckIssue] = field(default_factory=list)
    plan: Dict[str, List[Tuple[ManifestEntry, Path, Path]]] = field(default_factory=dict)

    @property
    def errors(self) -> List[PrecheckIssue]:
        return [i for i in self.issues if i.level == "error"]

    @property
    def warnings(self) -> List[PrecheckIssue]:
        return [i for i in self.issues if i.level == "warning"]

    @property
    def ok(self) -> bool:
        return not self.errors


def run_precheck(config: AppConfig, entries: List[ManifestEntry], storage=None, last_batch_id: Optional[str] = None) -> PrecheckResult:
    """执行预检，返回检测结果和执行计划"""
    result = PrecheckResult()

    source_root = config.source_root
    pkg_configs = {p.name: p for p in config.packages}
    grouped = group_by_package(entries)

    for pkg_name in grouped:
        if pkg_name not in pkg_configs:
            result.issues.append(
                PrecheckIssue(
                    level="error",
                    kind="unknown_package",
                    package=pkg_name,
                    message=f"清单中的包 '{pkg_name}' 在配置文件中未定义",
                )
            )

    for pkg in config.packages:
        if pkg.name not in grouped:
            result.issues.append(
                PrecheckIssue(
                    level="warning",
                    kind="empty_package",
                    package=pkg.name,
                    message=f"配置的包 '{pkg.name}' 在清单中没有任何文件",
                )
            )

    for pkg in config.packages:
        pkg_entries = grouped.get(pkg.name, [])
        output_dir = pkg.output_dir
        plan_for_pkg: List[Tuple[ManifestEntry, Path, Path]] = []

        target_names: Dict[str, ManifestEntry] = {}
        source_to_target: Dict[str, str] = {}

        for entry in pkg_entries:
            src = (source_root / entry.source_path).resolve() if not Path(entry.source_path).is_absolute() else Path(entry.source_path).resolve()
            if not entry.source_path:
                result.issues.append(
                    PrecheckIssue(
                        level="error",
                        kind="empty_source",
                        package=pkg.name,
                        message=f"文件分类 '{entry.category}' 的 source_path 为空",
                    )
                )
                continue

            if not src.exists():
                result.issues.append(
                    PrecheckIssue(
                        level="error",
                        kind="missing_attachment",
                        package=pkg.name,
                        message=f"源文件不存在: {entry.source_path}",
                        detail=f"期望路径: {src}",
                    )
                )
                continue

            if not src.is_file():
                result.issues.append(
                    PrecheckIssue(
                        level="error",
                        kind="not_a_file",
                        package=pkg.name,
                        message=f"源路径不是文件: {entry.source_path}",
                        detail=str(src),
                    )
                )
                continue

            target_name = entry.target_name or Path(entry.source_path).name
            if target_name in target_names:
                prev = target_names[target_name]
                result.issues.append(
                    PrecheckIssue(
                        level="error",
                        kind="duplicate_target",
                        package=pkg.name,
                        message=f"目标文件名重复: {target_name}",
                        detail=f"冲突于: {prev.source_path} vs {entry.source_path}",
                    )
                )
                continue
            target_names[target_name] = entry

            target_path = output_dir / target_name

            if entry.version and target_path.exists():
                if storage and last_batch_id:
                    last = storage.get_batch(last_batch_id)
                    if last:
                        for fa in last.file_actions:
                            if fa.package == pkg.name and Path(fa.target_path).name == target_name:
                                last_entry = None
                                for le in grouped.get(pkg.name, []):
                                    if Path(le.target_name or le.source_path).name == target_name:
                                        last_entry = le
                                        break
                                if last_entry and last_entry.version:
                                    if _parse_version(entry.version) < _parse_version(last_entry.version):
                                        result.issues.append(
                                            PrecheckIssue(
                                                level="error",
                                                kind="version_rollback",
                                                package=pkg.name,
                                                message=f"版本倒退: {target_name}",
                                                detail=f"上次版本 {last_entry.version} >= 当前版本 {entry.version}",
                                            )
                                        )

            if not config.allow_overwrite and target_path.exists():
                result.issues.append(
                    PrecheckIssue(
                        level="warning",
                        kind="target_exists",
                        package=pkg.name,
                        message=f"目标文件已存在: {target_name}",
                        detail=f"路径: {target_path}",
                    )
                )

            rel_src = entry.source_path
            source_to_target[rel_src] = target_name
            plan_for_pkg.append((entry, src, target_path))

        result.plan[pkg.name] = plan_for_pkg

        if output_dir.exists():
            manifest_targets = {e.target_name or Path(e.source_path).name for e in pkg_entries if e.target_name or e.source_path}
            for existing in output_dir.iterdir():
                if existing.is_file() and existing.name not in manifest_targets:
                    result.issues.append(
                        PrecheckIssue(
                            level="warning",
                            kind="outside_manifest",
                            package=pkg.name,
                            message=f"输出目录存在清单外文件: {existing.name}",
                            detail=str(existing),
                        )
                    )

    package_names: Set[str] = set()
    for pkg in config.packages:
        if pkg.name in package_names:
            result.issues.append(
                PrecheckIssue(
                    level="error",
                    kind="package_name_conflict",
                    package=pkg.name,
                    message=f"包名冲突: '{pkg.name}' 在配置中重复定义",
                )
            )
        package_names.add(pkg.name)

    zip_paths: Dict[str, str] = {}
    for pkg in config.packages:
        if pkg.zip_output:
            zp = str(pkg.zip_output)
            if zp in zip_paths:
                result.issues.append(
                    PrecheckIssue(
                        level="error",
                        kind="zip_name_conflict",
                        package=pkg.name,
                        message=f"zip 输出路径冲突",
                        detail=f"{pkg.name} 与 {zip_paths[zp]} 共享路径: {zp}",
                    )
                )
            zip_paths[zp] = pkg.name
            if pkg.zip_output.exists() and not config.allow_overwrite:
                result.issues.append(
                    PrecheckIssue(
                        level="warning",
                        kind="zip_exists",
                        package=pkg.name,
                        message=f"zip 文件已存在",
                        detail=str(pkg.zip_output),
                    )
                )

    return result
