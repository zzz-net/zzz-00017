"""交付方案模板 - 持久化存储与套用逻辑"""

from __future__ import annotations

import csv
import json
import logging
import sqlite3
import uuid
import yaml
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from .config import AppConfig, PackageConfig
from .manifest import ManifestEntry, group_by_package, load_manifest
from .precheck import PrecheckIssue, PrecheckResult, run_precheck

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


@dataclass
class Template:
    """交付方案模板"""

    name: str
    packages: List[PackageConfig]
    source_config_summary: Dict[str, Any]
    created_at: str
    id: str = field(default_factory=lambda: str(uuid.uuid4()))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "created_at": self.created_at,
            "source_config_summary": self.source_config_summary,
            "packages": [p.to_dict() for p in self.packages],
        }


class TemplateStorage:
    """模板 SQLite 存储 - 独立于批次存储，但共享相同的路径约定"""

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_schema(self):
        with self._conn() as c:
            c.executescript(
                """
                CREATE TABLE IF NOT EXISTS templates (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL,
                    source_config_summary TEXT NOT NULL DEFAULT '{}',
                    packages_data TEXT NOT NULL DEFAULT '[]'
                );
                CREATE INDEX IF NOT EXISTS idx_templates_name ON templates(name);
                CREATE INDEX IF NOT EXISTS idx_templates_created ON templates(created_at DESC);
                """
            )

    def _row_to_template(self, row: sqlite3.Row) -> Template:
        packages_data = json.loads(row["packages_data"] or "[]")
        packages = []
        for pd in packages_data:
            packages.append(
                PackageConfig(
                    name=pd["name"],
                    output_dir=Path(pd["output_dir"]),
                    zip_output=Path(pd["zip_output"]) if pd.get("zip_output") else None,
                    file_mapping=pd.get("file_mapping", {}),
                    version=pd.get("version"),
                )
            )
        return Template(
            id=row["id"],
            name=row["name"],
            created_at=row["created_at"],
            source_config_summary=json.loads(row["source_config_summary"] or "{}"),
            packages=packages,
        )

    def save_template(
        self,
        name: str,
        packages: List[PackageConfig],
        source_config_summary: Dict[str, Any],
        created_at: Optional[str] = None,
        id: Optional[str] = None,
    ) -> Template:
        """保存新模板。如果模板名已存在，抛出 TemplateNameExistsError。

        created_at 和 id 可选，用于导入场景保留原始审计数据；
        不传入则按新建模板处理（当前时间 + 新 UUID）。
        """
        if not name or not name.strip():
            raise ValueError("模板名不能为空")

        packages_data = [p.to_dict() for p in packages]

        with self._conn() as c:
            existing = c.execute("SELECT id FROM templates WHERE name=?", (name,)).fetchone()
            if existing:
                raise TemplateNameExistsError(f"模板名已存在: {name}")

            tpl = Template(
                name=name,
                packages=packages,
                source_config_summary=source_config_summary,
                created_at=created_at or _now_iso(),
                id=id or str(uuid.uuid4()),
            )
            c.execute(
                """INSERT INTO templates (id, name, created_at, source_config_summary, packages_data)
                   VALUES (?, ?, ?, ?, ?)""",
                (
                    tpl.id,
                    tpl.name,
                    tpl.created_at,
                    json.dumps(source_config_summary, ensure_ascii=False),
                    json.dumps(packages_data, ensure_ascii=False),
                ),
            )
            logger.info("模板已保存: %s (id=%s, created_at=%s)", tpl.name, tpl.id, tpl.created_at)
            return tpl

    def list_templates(self) -> List[Template]:
        with self._conn() as c:
            rows = c.execute("SELECT * FROM templates ORDER BY created_at DESC").fetchall()
        return [self._row_to_template(r) for r in rows]

    def get_template(self, name: str) -> Optional[Template]:
        with self._conn() as c:
            row = c.execute("SELECT * FROM templates WHERE name=?", (name,)).fetchone()
            if not row:
                return None
            return self._row_to_template(row)

    def delete_template(self, name: str) -> bool:
        with self._conn() as c:
            cur = c.execute("DELETE FROM templates WHERE name=?", (name,))
            deleted = cur.rowcount > 0
            if deleted:
                logger.info("模板已删除: %s", name)
            return deleted


class TemplateNameExistsError(Exception):
    """模板名重复异常"""
    pass


class TemplateNotFoundError(Exception):
    """模板不存在异常"""
    pass


class TemplateApplyError(Exception):
    """模板套用失败异常"""

    def __init__(self, message: str, issues: Optional[List[PrecheckIssue]] = None):
        super().__init__(message)
        self.issues = issues or []


def _validate_package_match(
    template_packages: List[PackageConfig],
    manifest_entries: List[ManifestEntry],
) -> Tuple[bool, List[PrecheckIssue]]:
    """校验模板包集合与清单包集合是否匹配。"""
    issues: List[PrecheckIssue] = []
    tpl_names = {p.name for p in template_packages}
    grouped = group_by_package(manifest_entries)
    manifest_names = set(grouped.keys())

    missing_in_manifest = tpl_names - manifest_names
    extra_in_manifest = manifest_names - tpl_names

    for pkg in missing_in_manifest:
        issues.append(
            PrecheckIssue(
                level="warning",
                kind="template_package_not_in_manifest",
                package=pkg,
                message=f"模板中的包 '{pkg}' 在清单中不存在，将被忽略",
            )
        )
    for pkg in extra_in_manifest:
        issues.append(
            PrecheckIssue(
                level="error",
                kind="manifest_package_not_in_template",
                package=pkg,
                message=f"清单中的包 '{pkg}' 在模板中未定义",
            )
        )

    has_errors = any(i.level == "error" for i in issues)
    return has_errors, issues


def _is_parent_writable(p: Path) -> bool:
    """检查路径的父目录是否可写。若父目录不存在，检查最近存在的祖先。"""
    try:
        parent = p.parent
        while not parent.exists():
            if parent == parent.parent:
                break
            parent = parent.parent
        if not parent.exists():
            return True
        test_file = parent / f"._writable_test_{uuid.uuid4().hex}"
        try:
            test_file.write_text("", encoding="utf-8")
            test_file.unlink()
            return True
        except (PermissionError, OSError):
            return False
    except Exception:
        return False


def _check_output_path_conflicts(
    packages: List[PackageConfig],
    base_dir: Path,
) -> List[PrecheckIssue]:
    """检查输出目录和 zip 路径冲突、已有交付包覆盖风险、父路径可写性。"""
    issues: List[PrecheckIssue] = []
    output_dirs: Dict[str, str] = {}
    zip_paths: Dict[str, str] = {}

    for pkg in packages:
        out_dir = pkg.output_dir
        if not out_dir.is_absolute():
            out_dir = (base_dir / out_dir).resolve()

        out_key = str(out_dir)
        if out_key in output_dirs:
            issues.append(
                PrecheckIssue(
                    level="error",
                    kind="output_dir_conflict",
                    package=pkg.name,
                    message=f"输出目录冲突",
                    detail=f"{pkg.name} 与 {output_dirs[out_key]} 共享目录: {out_dir}",
                )
            )
        output_dirs[out_key] = pkg.name

        if out_dir.exists() and out_dir.is_dir():
            try:
                has_contents = any(out_dir.iterdir())
            except (PermissionError, OSError):
                has_contents = True
            if has_contents:
                issues.append(
                    PrecheckIssue(
                        level="error",
                        kind="output_dir_has_content",
                        package=pkg.name,
                        message=f"输出目录已存在且非空（可能覆盖既有交付包）",
                        detail=str(out_dir),
                    )
                )

        if not _is_parent_writable(out_dir):
            issues.append(
                PrecheckIssue(
                    level="error",
                    kind="output_dir_parent_not_writable",
                    package=pkg.name,
                    message=f"输出目录父路径不可写",
                    detail=str(out_dir.parent),
                )
            )

        if pkg.zip_output:
            zp = pkg.zip_output
            if not zp.is_absolute():
                zp = (base_dir / zp).resolve()
            zp_key = str(zp)
            if zp_key in zip_paths:
                issues.append(
                    PrecheckIssue(
                        level="error",
                        kind="zip_path_conflict",
                        package=pkg.name,
                        message=f"zip 输出路径冲突",
                        detail=f"{pkg.name} 与 {zip_paths[zp_key]} 共享路径: {zp}",
                    )
                )
            zip_paths[zp_key] = pkg.name

            if zp.exists() and zp.is_file():
                issues.append(
                    PrecheckIssue(
                        level="error",
                        kind="zip_already_exists",
                        package=pkg.name,
                        message=f"zip 文件已存在（会覆盖既有交付包）",
                        detail=str(zp),
                    )
                )
            parent = zp.parent
            if parent.exists() and not parent.is_dir():
                issues.append(
                    PrecheckIssue(
                        level="error",
                        kind="zip_parent_not_dir",
                        package=pkg.name,
                        message=f"zip 父路径不是目录",
                        detail=str(parent),
                    )
                )

            if not _is_parent_writable(zp):
                issues.append(
                    PrecheckIssue(
                        level="error",
                        kind="zip_parent_not_writable",
                        package=pkg.name,
                        message=f"zip 输出父路径不可写",
                        detail=str(zp.parent),
                    )
                )

    return issues


def _resolve_relative_paths(
    packages: List[PackageConfig],
    base_dir: Path,
) -> List[PackageConfig]:
    """将包配置中的相对路径解析为绝对路径（基于 base_dir）。"""
    resolved = []
    for pkg in packages:
        out_dir = pkg.output_dir
        if not out_dir.is_absolute():
            out_dir = (base_dir / out_dir).resolve()
        zip_out = pkg.zip_output
        if zip_out and not zip_out.is_absolute():
            zip_out = (base_dir / zip_out).resolve()
        resolved.append(
            PackageConfig(
                name=pkg.name,
                output_dir=out_dir,
                zip_output=zip_out,
                file_mapping=dict(pkg.file_mapping),
                version=pkg.version,
            )
        )
    return resolved


def apply_template(
    template: Template,
    manifest_path: Path,
    output_config_path: Path,
    source_root: Optional[Path] = None,
    operator: Optional[str] = None,
    db_path: Optional[Path] = None,
    run_dry_run: bool = True,
) -> Tuple[Path, PrecheckResult]:
    """
    套用模板生成新的 YAML 配置草稿，并可选地执行 dry-run 预检。

    返回 (生成的配置文件路径, 预检结果)。
    失败时抛出 TemplateApplyError，保证不会生成半截配置文件。
    """
    manifest_path = Path(manifest_path).resolve()
    output_config_path = Path(output_config_path).resolve()
    base_dir = output_config_path.parent

    if not manifest_path.exists():
        raise TemplateApplyError(f"清单文件不存在: {manifest_path}")

    out_parent = output_config_path.parent
    try:
        out_parent.mkdir(parents=True, exist_ok=True)
    except (PermissionError, OSError) as e:
        raise TemplateApplyError(
            f"无法创建输出目录 {out_parent}: {e}（可能是只读目录或权限不足）"
        )

    if output_config_path.exists():
        raise TemplateApplyError(
            f"配置输出路径已存在: {output_config_path}，请先删除或指定其他路径"
        )

    try:
        entries = load_manifest(manifest_path)
    except Exception as e:
        raise TemplateApplyError(f"加载清单失败: {e}")

    has_errors, issues = _validate_package_match(template.packages, entries)
    conflict_issues = _check_output_path_conflicts(template.packages, base_dir)
    all_issues = issues + conflict_issues
    has_errors = has_errors or any(i.level == "error" for i in conflict_issues)

    resolved_packages = _resolve_relative_paths(template.packages, base_dir)

    used_package_names: Set[str] = set()
    grouped = group_by_package(entries)
    for pkg in resolved_packages:
        if pkg.name in grouped:
            used_package_names.add(pkg.name)
    active_packages = [p for p in resolved_packages if p.name in used_package_names]

    rel_source_root = source_root or Path("./sources")
    if not rel_source_root.is_absolute():
        rel_source_root_out = rel_source_root
    else:
        try:
            rel_source_root_out = rel_source_root.relative_to(base_dir)
        except ValueError:
            rel_source_root_out = rel_source_root

    rel_manifest = manifest_path
    try:
        rel_manifest = manifest_path.relative_to(base_dir)
    except ValueError:
        rel_manifest = manifest_path

    import os as _os
    effective_operator = operator or _os.environ.get("USER", _os.environ.get("USERNAME", "unknown"))
    effective_db = db_path or Path("./.contract_pack.db")

    def _relativize(p: Path, base: Path) -> str:
        try:
            return str(p.relative_to(base))
        except ValueError:
            return str(p)

    config_dict: Dict[str, Any] = {
        "operator": effective_operator,
        "manifest": str(rel_manifest),
        "source_root": str(rel_source_root_out),
        "db_path": str(effective_db),
        "allow_overwrite": False,
        "packages": [],
    }

    for pkg in resolved_packages:
        if pkg.name in used_package_names:
            rel_zip = None
            if pkg.zip_output:
                try:
                    rel_zip = str(pkg.zip_output.relative_to(base_dir))
                except ValueError:
                    rel_zip = str(pkg.zip_output)
            try:
                rel_out = str(pkg.output_dir.relative_to(base_dir))
            except ValueError:
                rel_out = str(pkg.output_dir)
            config_dict["packages"].append(
                {
                    "name": pkg.name,
                    "output_dir": rel_out,
                    "zip_output": rel_zip,
                    "file_mapping": dict(pkg.file_mapping),
                    "version": pkg.version,
                }
            )

    precheck_result = PrecheckResult(issues=list(all_issues))

    if has_errors:
        logger.warning("模板套用存在错误，不写入配置文件")
        raise TemplateApplyError(
            "模板套用预检失败，请检查包匹配和路径冲突",
            issues=all_issues,
        )

    try:
        with open(output_config_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(config_dict, f, allow_unicode=True, sort_keys=False)
        logger.info("配置草稿已写入: %s", output_config_path)
    except (PermissionError, OSError) as e:
        if output_config_path.exists():
            try:
                output_config_path.unlink()
            except OSError:
                pass
        raise TemplateApplyError(
            f"写入配置文件失败 {output_config_path}: {e}（可能是只读目录或权限不足）"
        )

    if run_dry_run:
        try:
            cfg_for_check = AppConfig.load(str(output_config_path))
        except Exception as e:
            try:
                output_config_path.unlink()
            except OSError:
                pass
            raise TemplateApplyError(f"生成的配置无法加载: {e}")

        precheck_result = run_precheck(cfg_for_check, entries)
        precheck_result.issues.extend(all_issues)

        if not precheck_result.ok:
            try:
                output_config_path.unlink()
                logger.info("dry-run 失败，已删除生成的配置草稿")
            except OSError:
                pass
            raise TemplateApplyError(
                "dry-run 验证失败，已删除配置草稿。请修复清单或调整模板后重试",
                issues=precheck_result.issues,
            )

    return output_config_path, precheck_result


def export_template_json(templates: List[Template], out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    data = []
    for tpl in templates:
        d = tpl.to_dict()
        d["template_name"] = tpl.name
        d["source_config_summary"] = tpl.source_config_summary
        d["created_at"] = tpl.created_at
        data.append(d)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def export_template_csv(templates: List[Template], out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "template_id",
        "template_name",
        "created_at",
        "package_name",
        "output_dir",
        "zip_output",
        "package_version",
        "file_mapping",
        "source_config_summary",
    ]
    with open(out_path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for tpl in templates:
            for pkg in tpl.packages:
                writer.writerow(
                    {
                        "template_id": tpl.id,
                        "template_name": tpl.name,
                        "created_at": tpl.created_at,
                        "package_name": pkg.name,
                        "output_dir": str(pkg.output_dir),
                        "zip_output": str(pkg.zip_output) if pkg.zip_output else "",
                        "package_version": pkg.version or "",
                        "file_mapping": json.dumps(pkg.file_mapping, ensure_ascii=False),
                        "source_config_summary": json.dumps(
                            tpl.source_config_summary, ensure_ascii=False
                        ),
                    }
                )


class TemplateImportError(Exception):
    """模板导入失败异常"""
    pass


def import_template_json(in_path: Path) -> List[Template]:
    """从 JSON 文件导入模板列表。"""
    in_path = Path(in_path)
    if not in_path.exists():
        raise TemplateImportError(f"导入文件不存在: {in_path}")
    try:
        with open(in_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        raise TemplateImportError(f"读取 JSON 失败: {e}")

    if not isinstance(data, list):
        raise TemplateImportError("JSON 根节点应为模板列表")

    templates: List[Template] = []
    for idx, rec in enumerate(data):
        try:
            name = rec.get("template_name") or rec.get("name")
            if not name:
                raise ValueError(f"第 {idx} 条记录缺少 template_name/name 字段")
            created_at = rec.get("created_at") or _now_iso()
            source_config_summary = rec.get("source_config_summary", {}) or {}
            packages_data = rec.get("packages", []) or []
            packages = []
            for pd in packages_data:
                packages.append(
                    PackageConfig(
                        name=pd["name"],
                        output_dir=Path(pd["output_dir"]),
                        zip_output=Path(pd["zip_output"]) if pd.get("zip_output") else None,
                        file_mapping=pd.get("file_mapping", {}),
                        version=pd.get("version"),
                    )
                )
            templates.append(
                Template(
                    name=name,
                    packages=packages,
                    source_config_summary=source_config_summary,
                    created_at=created_at,
                    id=rec.get("id") or str(uuid.uuid4()),
                )
            )
        except Exception as e:
            raise TemplateImportError(f"解析第 {idx} 条模板记录失败: {e}")
    return templates


def import_template_csv(in_path: Path) -> List[Template]:
    """从 CSV 文件导入模板列表。"""
    in_path = Path(in_path)
    if not in_path.exists():
        raise TemplateImportError(f"导入文件不存在: {in_path}")
    try:
        with open(in_path, "r", encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            rows = list(reader)
    except (OSError, csv.Error) as e:
        raise TemplateImportError(f"读取 CSV 失败: {e}")

    grouped: Dict[str, dict] = {}
    for idx, row in enumerate(rows):
        try:
            tpl_name = row.get("template_name") or row.get("name")
            if not tpl_name:
                raise ValueError(f"第 {idx} 行缺少 template_name 字段")

            if tpl_name not in grouped:
                scs_raw = row.get("source_config_summary", "{}") or "{}"
                try:
                    scs = json.loads(scs_raw) if scs_raw.strip() else {}
                except json.JSONDecodeError:
                    scs = {}
                grouped[tpl_name] = {
                    "template_id": row.get("template_id") or str(uuid.uuid4()),
                    "created_at": row.get("created_at") or _now_iso(),
                    "source_config_summary": scs,
                    "packages": [],
                }

            pkg_name = row.get("package_name")
            if not pkg_name:
                continue
            fm_raw = row.get("file_mapping", "{}") or "{}"
            try:
                fm = json.loads(fm_raw) if fm_raw.strip() else {}
            except json.JSONDecodeError:
                fm = {}
            zip_out = row.get("zip_output") or ""
            grouped[tpl_name]["packages"].append(
                PackageConfig(
                    name=pkg_name,
                    output_dir=Path(row.get("output_dir") or "./deliver/" + pkg_name),
                    zip_output=Path(zip_out) if zip_out else None,
                    file_mapping=fm,
                    version=row.get("package_version") or None,
                )
            )
        except Exception as e:
            raise TemplateImportError(f"解析第 {idx} 行失败: {e}")

    templates: List[Template] = []
    for name, info in grouped.items():
        templates.append(
            Template(
                name=name,
                packages=info["packages"],
                source_config_summary=info["source_config_summary"],
                created_at=info["created_at"],
                id=info["template_id"],
            )
        )
    return templates
