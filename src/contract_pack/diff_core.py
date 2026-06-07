"""交付包差异对比模块

将当前配置和 CSV 清单预期的交付结果，与历史批次或指定目录进行对比。
支持输出：新增、缺失、文件名变化、版本变化、包归属变化、zip 状态差异。
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .config import AppConfig, PackageConfig
from .manifest import ManifestEntry, load_manifest
from .storage import BATCH_STATUS, BatchStorage, FileAction


class DiffChangeType(str, Enum):
    """差异类型"""

    ADDED = "added"
    MISSING = "missing"
    RENAMED = "renamed"
    VERSION_CHANGED = "version_changed"
    PACKAGE_CHANGED = "package_changed"
    ZIP_STATUS_CHANGED = "zip_status_changed"
    UNCHANGED = "unchanged"
    CONTENT_CHANGED = "content_changed"


DIFF_CHANGE_LABELS = {
    DiffChangeType.ADDED: "新增",
    DiffChangeType.MISSING: "缺失",
    DiffChangeType.RENAMED: "文件名变化",
    DiffChangeType.VERSION_CHANGED: "版本变化",
    DiffChangeType.PACKAGE_CHANGED: "包归属变化",
    DiffChangeType.ZIP_STATUS_CHANGED: "zip 状态差异",
    DiffChangeType.UNCHANGED: "无变化",
    DiffChangeType.CONTENT_CHANGED: "内容变化",
}


@dataclass
class DeliverableItem:
    """单个交付项（文件或 zip 包）"""

    package: str
    category: str
    target_name: str
    target_path: str
    version: Optional[str] = None
    is_zip: bool = False
    file_hash: Optional[str] = None
    file_size: Optional[int] = None
    source_path: Optional[str] = None

    @property
    def identity_key(self) -> Tuple[str, str, str]:
        """用于匹配的主键 (package, category, normalized_target_name_stem)"""
        stem = Path(self.target_name).stem
        return (self.package, self.category, stem.lower())

    @property
    def full_key(self) -> Tuple[str, str, str]:
        """精确匹配键 (package, category, target_name)"""
        return (self.package, self.category, self.target_name)


@dataclass
class DiffItem:
    """单个差异条目"""

    change_type: DiffChangeType
    package: str
    category: str
    target_name: str
    baseline_target_name: Optional[str] = None
    version: Optional[str] = None
    baseline_version: Optional[str] = None
    baseline_package: Optional[str] = None
    target_path: Optional[str] = None
    baseline_path: Optional[str] = None
    is_zip: Optional[bool] = None
    baseline_is_zip: Optional[bool] = None
    file_hash: Optional[str] = None
    baseline_hash: Optional[str] = None
    file_size: Optional[int] = None
    baseline_size: Optional[int] = None
    detail: str = ""


@dataclass
class DiffError:
    """对比过程中的错误信息"""

    level: str
    kind: str
    message: str
    detail: str = ""


@dataclass
class DiffResult:
    """差异对比结果"""

    baseline_kind: str
    baseline_ref: str
    generated_at: str
    total_expected: int = 0
    total_baseline: int = 0
    items: List[DiffItem] = field(default_factory=list)
    errors: List[DiffError] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not any(e.level == "error" for e in self.errors)

    @property
    def added(self) -> List[DiffItem]:
        return [i for i in self.items if i.change_type == DiffChangeType.ADDED]

    @property
    def missing(self) -> List[DiffItem]:
        return [i for i in self.items if i.change_type == DiffChangeType.MISSING]

    @property
    def renamed(self) -> List[DiffItem]:
        return [i for i in self.items if i.change_type == DiffChangeType.RENAMED]

    @property
    def version_changed(self) -> List[DiffItem]:
        return [i for i in self.items if i.change_type == DiffChangeType.VERSION_CHANGED]

    @property
    def package_changed(self) -> List[DiffItem]:
        return [i for i in self.items if i.change_type == DiffChangeType.PACKAGE_CHANGED]

    @property
    def zip_status_changed(self) -> List[DiffItem]:
        return [i for i in self.items if i.change_type == DiffChangeType.ZIP_STATUS_CHANGED]

    @property
    def content_changed(self) -> List[DiffItem]:
        return [i for i in self.items if i.change_type == DiffChangeType.CONTENT_CHANGED]

    @property
    def unchanged(self) -> List[DiffItem]:
        return [i for i in self.items if i.change_type == DiffChangeType.UNCHANGED]


class DiffErrorException(Exception):
    """对比过程中的可报告异常"""

    def __init__(self, message: str, kind: str = "diff_error", errors: Optional[List[DiffError]] = None):
        super().__init__(message)
        self.kind = kind
        self.errors = errors or []


def _compute_file_hash(path: Path) -> Tuple[str, int]:
    """计算文件 SHA1 和大小"""
    size = path.stat().st_size
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest(), size


def _resolve_mapped_path(pkg: PackageConfig, category: str, target_name: str) -> Path:
    """根据 file_mapping 解析分类到子目录，返回完整目标路径"""
    output_dir = pkg.output_dir
    subdir = pkg.file_mapping.get(category, "")
    if subdir:
        return output_dir / subdir / target_name
    return output_dir / target_name


def collect_expected_deliverables(cfg: AppConfig, entries: List[ManifestEntry]) -> Tuple[List[DeliverableItem], List[DiffError]]:
    """根据当前配置和 CSV 清单，收集预期的交付项列表。

    包含：
    - 所有清单中的文件（考虑 file_mapping 分类子目录）
    - 所有配置了 zip_output 的包的 zip 包
    """
    items: List[DeliverableItem] = []
    errors: List[DiffError] = []

    pkg_configs = {p.name: p for p in cfg.packages}

    seen_targets: Dict[Tuple[str, str], ManifestEntry] = {}

    for entry in entries:
        pkg_cfg = pkg_configs.get(entry.package)
        if not pkg_cfg:
            errors.append(DiffError(
                level="warning",
                kind="unknown_package",
                message=f"清单中的包 '{entry.package}' 在配置中未定义，跳过",
                detail=f"category={entry.category}, target={entry.target_name}",
            ))
            continue

        target_path = _resolve_mapped_path(pkg_cfg, entry.category, entry.target_name)

        dup_key = (entry.package, entry.target_name)
        if dup_key in seen_targets:
            errors.append(DiffError(
                level="error",
                kind="duplicate_target",
                message=f"包 '{entry.package}' 中目标文件名重复: {entry.target_name}",
                detail=f"冲突于: {seen_targets[dup_key].source_path} vs {entry.source_path}",
            ))
        seen_targets[dup_key] = entry

        items.append(DeliverableItem(
            package=entry.package,
            category=entry.category,
            target_name=entry.target_name,
            target_path=str(target_path),
            version=entry.version,
            is_zip=False,
            source_path=entry.source_path,
        ))

    for pkg in cfg.packages:
        if pkg.zip_output:
            items.append(DeliverableItem(
                package=pkg.name,
                category="__zip__",
                target_name=pkg.zip_output.name,
                target_path=str(pkg.zip_output),
                version=pkg.version,
                is_zip=True,
            ))

    return items, errors


def collect_from_batch(storage: BatchStorage, batch_id: str) -> Tuple[List[DeliverableItem], List[DiffError]]:
    """从历史批次收集交付项。

    仅提取状态为 success 的 COPY 和 ZIP 动作。
    """
    items: List[DeliverableItem] = []
    errors: List[DiffError] = []

    batch = storage.get_batch(batch_id)
    if not batch:
        errors.append(DiffError(
            level="error",
            kind="batch_not_found",
            message=f"历史批次不存在: {batch_id}",
        ))
        return items, errors

    allowed_statuses = {BATCH_STATUS["COMPLETED"], BATCH_STATUS["PARTIAL"]}
    if batch.status not in allowed_statuses and batch.status != BATCH_STATUS["FAILED"]:
        errors.append(DiffError(
            level="warning",
            kind="batch_incomplete",
            message=f"历史批次状态为 '{batch.status}'，不是 completed/partial，结果可能不完整",
            detail=f"batch_id={batch_id}",
        ))

    for fa in batch.file_actions:
        if fa.status != "success":
            continue
        is_zip = fa.action == "zip" or fa.category == "__zip__"
        items.append(DeliverableItem(
            package=fa.package,
            category=fa.category,
            target_name=Path(fa.target_path).name,
            target_path=fa.target_path,
            version=getattr(fa, "version", None),
            is_zip=is_zip,
            file_hash=fa.file_hash,
            file_size=fa.file_size,
            source_path=fa.source_path,
        ))

    if not items and batch.status != BATCH_STATUS["FAILED"]:
        errors.append(DiffError(
            level="warning",
            kind="batch_empty",
            message=f"历史批次 '{batch_id}' 没有成功的文件动作",
        ))

    return items, errors


def collect_from_directory(cfg: AppConfig, dir_path: Path) -> Tuple[List[DeliverableItem], List[DiffError]]:
    """从指定目录收集交付项。

    扫描目录下的所有文件（含子目录），按配置的包结构识别归属。
    zip 文件根据配置的 zip_output 名识别。
    """
    items: List[DeliverableItem] = []
    errors: List[DiffError] = []

    if not dir_path.exists():
        errors.append(DiffError(
            level="error",
            kind="directory_not_found",
            message=f"基准目录不存在: {dir_path}",
        ))
        return items, errors

    if not dir_path.is_dir():
        errors.append(DiffError(
            level="error",
            kind="not_a_directory",
            message=f"基准路径不是目录: {dir_path}",
        ))
        return items, errors

    try:
        all_files = list(dir_path.rglob("*"))
    except PermissionError as e:
        errors.append(DiffError(
            level="error",
            kind="permission_denied",
            message=f"扫描基准目录时权限不足: {dir_path}",
            detail=str(e),
        ))
        return items, errors
    except OSError as e:
        errors.append(DiffError(
            level="error",
            kind="io_error",
            message=f"扫描基准目录时出错: {dir_path}",
            detail=str(e),
        ))
        return items, errors

    dir_path_abs = dir_path.resolve()

    pkg_outputs: Dict[Path, PackageConfig] = {}
    pkg_out_names: Dict[str, PackageConfig] = {}
    zip_names: Dict[str, PackageConfig] = {}
    for pkg in cfg.packages:
        try:
            pkg_out_abs = pkg.output_dir.resolve()
            pkg_outputs[pkg_out_abs] = pkg
            pkg_out_names[pkg.output_dir.name] = pkg
        except Exception:
            pass
        if pkg.zip_output:
            zip_names[pkg.zip_output.name] = pkg

    name_conflicts: Dict[str, int] = {}

    for f in all_files:
        if not f.is_file():
            continue
        try:
            f_abs = f.resolve()
        except Exception:
            continue

        target_name = f.name

        matched_pkg: Optional[PackageConfig] = None
        category = ""
        rel_in_pkg: Optional[Path] = None

        for pkg_out, pkg in pkg_outputs.items():
            try:
                if str(f_abs).startswith(str(pkg_out)):
                    rel_in_pkg = f_abs.relative_to(pkg_out)
                    matched_pkg = pkg
                    break
            except ValueError:
                continue

        if matched_pkg is None:
            try:
                rel_to_base = f_abs.relative_to(dir_path_abs)
                if len(rel_to_base.parts) >= 2:
                    first_dir = rel_to_base.parts[0]
                    if first_dir in pkg_out_names:
                        matched_pkg = pkg_out_names[first_dir]
                        rel_in_pkg = Path(*rel_to_base.parts[1:])
            except ValueError:
                pass

        if matched_pkg is None:
            if target_name in zip_names:
                matched_pkg = zip_names[target_name]
                category = "__zip__"
            else:
                errors.append(DiffError(
                    level="warning",
                    kind="unclassified_file",
                    message=f"目录中的文件无法归类到任何包: {f}",
                ))
                continue

        if category != "__zip__":
            reverse_mapping = {v: k for k, v in matched_pkg.file_mapping.items()}
            if rel_in_pkg is not None and len(rel_in_pkg.parts) > 1:
                first_part = rel_in_pkg.parts[0]
                if first_part in reverse_mapping:
                    category = reverse_mapping[first_part]
                else:
                    category = first_part
            else:
                category = ""

        file_hash = None
        file_size = None
        try:
            file_hash, file_size = _compute_file_hash(f_abs)
        except PermissionError as e:
            errors.append(DiffError(
                level="error",
                kind="permission_denied",
                message=f"读取文件权限不足: {f}",
                detail=str(e),
            ))
        except OSError as e:
            errors.append(DiffError(
                level="warning",
                kind="io_error",
                message=f"读取文件出错: {f}",
                detail=str(e),
            ))

        dup_key = (matched_pkg.name, target_name)
        if dup_key in name_conflicts:
            name_conflicts[dup_key] += 1
            errors.append(DiffError(
                level="error",
                kind="duplicate_in_directory",
                message=f"目录中存在同名文件在同一个包: {matched_pkg.name}/{target_name}",
                detail=f"出现次数: {name_conflicts[dup_key]}",
            ))
        else:
            name_conflicts[dup_key] = 1

        is_zip = category == "__zip__" or target_name.lower().endswith(".zip")
        items.append(DeliverableItem(
            package=matched_pkg.name,
            category=category,
            target_name=target_name,
            target_path=str(f_abs),
            version=None,
            is_zip=is_zip,
            file_hash=file_hash,
            file_size=file_size,
        ))

    return items, errors


def _match_and_diff(
    expected: List[DeliverableItem],
    baseline: List[DeliverableItem],
) -> List[DiffItem]:
    """匹配预期与基准交付项并计算差异"""
    diff_items: List[DiffItem] = []

    expected_by_full: Dict[Tuple[str, str, str], DeliverableItem] = {}
    baseline_by_full: Dict[Tuple[str, str, str], DeliverableItem] = {}
    expected_by_identity: Dict[Tuple[str, str, str], List[DeliverableItem]] = {}
    baseline_by_identity: Dict[Tuple[str, str, str], List[DeliverableItem]] = {}
    expected_by_stem: Dict[Tuple[str, str], List[DeliverableItem]] = {}
    baseline_by_stem: Dict[Tuple[str, str], List[DeliverableItem]] = {}

    for item in expected:
        expected_by_full[item.full_key] = item
        expected_by_identity.setdefault(item.identity_key, []).append(item)
        stem_key = (item.package, Path(item.target_name).stem.lower())
        expected_by_stem.setdefault(stem_key, []).append(item)

    for item in baseline:
        baseline_by_full[item.full_key] = item
        baseline_by_identity.setdefault(item.identity_key, []).append(item)
        stem_key = (item.package, Path(item.target_name).stem.lower())
        baseline_by_stem.setdefault(stem_key, []).append(item)

    matched_expected: set = set()
    matched_baseline: set = set()

    for full_key, exp in expected_by_full.items():
        if full_key in baseline_by_full:
            base = baseline_by_full[full_key]
            matched_expected.add(id(exp))
            matched_baseline.add(id(base))
            _append_comparison(diff_items, exp, base)

    for full_key, exp in expected_by_full.items():
        if id(exp) in matched_expected:
            continue
        identity = exp.identity_key
        candidates = baseline_by_identity.get(identity, [])
        candidates = [c for c in candidates if id(c) not in matched_baseline]

        if not candidates:
            stem_key = (exp.package, Path(exp.target_name).stem.lower())
            stem_candidates = baseline_by_stem.get(stem_key, [])
            candidates = [c for c in stem_candidates if id(c) not in matched_baseline]

        if candidates:
            base = candidates[0]
            matched_expected.add(id(exp))
            matched_baseline.add(id(base))

            if exp.package != base.package:
                diff_items.append(DiffItem(
                    change_type=DiffChangeType.PACKAGE_CHANGED,
                    package=exp.package,
                    category=exp.category,
                    target_name=exp.target_name,
                    baseline_target_name=base.target_name,
                    baseline_package=base.package,
                    version=exp.version,
                    baseline_version=base.version,
                    target_path=exp.target_path,
                    baseline_path=base.target_path,
                    detail=f"包归属变化: {base.package} -> {exp.package}",
                ))
            elif exp.category != base.category and base.category and exp.category:
                diff_items.append(DiffItem(
                    change_type=DiffChangeType.RENAMED,
                    package=exp.package,
                    category=exp.category,
                    target_name=exp.target_name,
                    baseline_target_name=base.target_name,
                    version=exp.version,
                    baseline_version=base.version,
                    target_path=exp.target_path,
                    baseline_path=base.target_path,
                    detail=f"分类或文件名变化: {base.category}/{base.target_name} -> {exp.category}/{exp.target_name}",
                ))
            elif exp.target_name != base.target_name:
                diff_items.append(DiffItem(
                    change_type=DiffChangeType.RENAMED,
                    package=exp.package,
                    category=exp.category,
                    target_name=exp.target_name,
                    baseline_target_name=base.target_name,
                    version=exp.version,
                    baseline_version=base.version,
                    target_path=exp.target_path,
                    baseline_path=base.target_path,
                    detail=f"文件名变化: {base.target_name} -> {exp.target_name}",
                ))
            else:
                _append_comparison(diff_items, exp, base)

    for full_key, exp in expected_by_full.items():
        if id(exp) not in matched_expected:
            diff_items.append(DiffItem(
                change_type=DiffChangeType.ADDED,
                package=exp.package,
                category=exp.category,
                target_name=exp.target_name,
                version=exp.version,
                target_path=exp.target_path,
                is_zip=exp.is_zip,
                detail="预期中存在但基准中无匹配",
            ))

    for full_key, base in baseline_by_full.items():
        if id(base) not in matched_baseline:
            diff_items.append(DiffItem(
                change_type=DiffChangeType.MISSING,
                package=base.package,
                category=base.category,
                target_name="",
                baseline_target_name=base.target_name,
                baseline_version=base.version,
                baseline_path=base.target_path,
                baseline_is_zip=base.is_zip,
                detail="基准中存在但预期中无匹配",
            ))

    return diff_items


def _append_comparison(
    diff_items: List[DiffItem],
    exp: DeliverableItem,
    base: DeliverableItem,
) -> None:
    """比较两个匹配的交付项，追加对应的差异条目"""
    if exp.is_zip != base.is_zip:
        diff_items.append(DiffItem(
            change_type=DiffChangeType.ZIP_STATUS_CHANGED,
            package=exp.package,
            category=exp.category,
            target_name=exp.target_name,
            baseline_target_name=base.target_name,
            version=exp.version,
            baseline_version=base.version,
            target_path=exp.target_path,
            baseline_path=base.target_path,
            is_zip=exp.is_zip,
            baseline_is_zip=base.is_zip,
            detail=f"预期 is_zip={exp.is_zip}, 基准 is_zip={base.is_zip}",
        ))
    elif exp.version != base.version:
        diff_items.append(DiffItem(
            change_type=DiffChangeType.VERSION_CHANGED,
            package=exp.package,
            category=exp.category,
            target_name=exp.target_name,
            baseline_target_name=base.target_name,
            version=exp.version,
            baseline_version=base.version,
            target_path=exp.target_path,
            baseline_path=base.target_path,
            detail=f"版本变化: {base.version or 'None'} -> {exp.version or 'None'}",
        ))
    elif (
        exp.file_hash is not None
        and base.file_hash is not None
        and exp.file_hash != base.file_hash
    ):
        diff_items.append(DiffItem(
            change_type=DiffChangeType.CONTENT_CHANGED,
            package=exp.package,
            category=exp.category,
            target_name=exp.target_name,
            baseline_target_name=base.target_name,
            version=exp.version,
            baseline_version=base.version,
            target_path=exp.target_path,
            baseline_path=base.target_path,
            file_hash=exp.file_hash,
            baseline_hash=base.file_hash,
            file_size=exp.file_size,
            baseline_size=base.file_size,
            detail=f"内容哈希不同",
        ))
    else:
        diff_items.append(DiffItem(
            change_type=DiffChangeType.UNCHANGED,
            package=exp.package,
            category=exp.category,
            target_name=exp.target_name,
            baseline_target_name=base.target_name,
            version=exp.version,
            baseline_version=base.version,
            target_path=exp.target_path,
            baseline_path=base.target_path,
            is_zip=exp.is_zip,
            baseline_is_zip=base.is_zip,
        ))


def diff_against_batch(
    cfg: AppConfig,
    entries: List[ManifestEntry],
    storage: BatchStorage,
    batch_id: str,
) -> DiffResult:
    """对比当前预期与指定历史批次"""
    errors: List[DiffError] = []

    expected, exp_errors = collect_expected_deliverables(cfg, entries)
    errors.extend(exp_errors)

    baseline, batch_errors = collect_from_batch(storage, batch_id)
    errors.extend(batch_errors)

    diff_items = _match_and_diff(expected, baseline)

    return DiffResult(
        baseline_kind="batch",
        baseline_ref=batch_id,
        generated_at=datetime.now().isoformat(timespec="seconds"),
        total_expected=len(expected),
        total_baseline=len(baseline),
        items=diff_items,
        errors=errors,
    )


def diff_against_directory(
    cfg: AppConfig,
    entries: List[ManifestEntry],
    dir_path: Path,
) -> DiffResult:
    """对比当前预期与指定目录"""
    errors: List[DiffError] = []

    expected, exp_errors = collect_expected_deliverables(cfg, entries)
    errors.extend(exp_errors)

    baseline, dir_errors = collect_from_directory(cfg, dir_path)
    errors.extend(dir_errors)

    diff_items = _match_and_diff(expected, baseline)

    return DiffResult(
        baseline_kind="directory",
        baseline_ref=str(dir_path),
        generated_at=datetime.now().isoformat(timespec="seconds"),
        total_expected=len(expected),
        total_baseline=len(baseline),
        items=diff_items,
        errors=errors,
    )
