"""配置加载模块 - 加载并验证 YAML 配置文件"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from .audit import AuditConfigError


@dataclass
class AuditConfig:
    """审计模块配置"""

    enabled: bool = True
    retention_days: int = 90
    export_default_dir: Optional[Path] = None

    @classmethod
    def from_dict(cls, data: Dict[str, Any], base_dir: Path) -> "AuditConfig":
        enabled = data.get("enabled", True)
        if not isinstance(enabled, bool):
            raise AuditConfigError(f"audit.enabled 必须是布尔值，实际类型: {type(enabled).__name__}")

        retention_days = data.get("retention_days", 90)
        if not isinstance(retention_days, int) or isinstance(retention_days, bool):
            raise AuditConfigError(f"audit.retention_days 必须是整数，实际: {retention_days!r}")
        if retention_days < 0:
            raise AuditConfigError(f"audit.retention_days 不能为负数，实际: {retention_days}")

        export_default_dir = data.get("export_default_dir")
        resolved_export_dir: Optional[Path] = None
        if export_default_dir is not None:
            if not isinstance(export_default_dir, (str, os.PathLike)):
                raise AuditConfigError(
                    f"audit.export_default_dir 必须是字符串路径，实际类型: {type(export_default_dir).__name__}"
                )
            p = Path(export_default_dir)
            if not p.is_absolute():
                p = (base_dir / p).resolve()
            resolved_export_dir = p

        return cls(
            enabled=enabled,
            retention_days=retention_days,
            export_default_dir=resolved_export_dir,
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "enabled": self.enabled,
            "retention_days": self.retention_days,
            "export_default_dir": str(self.export_default_dir) if self.export_default_dir else None,
        }


@dataclass
class PackageConfig:
    """单个交付包的配置"""

    name: str
    output_dir: Path
    zip_output: Optional[Path] = None
    file_mapping: Dict[str, str] = field(default_factory=dict)
    version: Optional[str] = None

    @classmethod
    def from_dict(cls, data: Dict[str, Any], base_dir: Path) -> "PackageConfig":
        name = data.get("name")
        if not name:
            raise ValueError("package 配置缺少 'name' 字段")

        output_dir = Path(data.get("output_dir", ""))
        if not output_dir.is_absolute():
            output_dir = (base_dir / output_dir).resolve()

        zip_output = data.get("zip_output")
        if zip_output:
            zip_output = Path(zip_output)
            if not zip_output.is_absolute():
                zip_output = (base_dir / zip_output).resolve()

        return cls(
            name=name,
            output_dir=output_dir,
            zip_output=zip_output,
            file_mapping=data.get("file_mapping", {}),
            version=data.get("version"),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "output_dir": str(self.output_dir),
            "zip_output": str(self.zip_output) if self.zip_output else None,
            "file_mapping": self.file_mapping,
            "version": self.version,
        }


@dataclass
class AppConfig:
    """应用总配置"""

    manifest_path: Path
    source_root: Path
    packages: List[PackageConfig]
    operator: str
    db_path: Path
    allow_overwrite: bool = False
    audit: AuditConfig = field(default_factory=AuditConfig)

    @classmethod
    def load(cls, config_path: str | os.PathLike) -> "AppConfig":
        path = Path(config_path).resolve()
        if not path.exists():
            raise FileNotFoundError(f"配置文件不存在: {path}")

        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}

        base_dir = path.parent

        manifest_path = data.get("manifest")
        if not manifest_path:
            raise ValueError("配置缺少 'manifest' 字段")
        manifest_path = Path(manifest_path)
        if not manifest_path.is_absolute():
            manifest_path = (base_dir / manifest_path).resolve()

        source_root = data.get("source_root", ".")
        source_root = Path(source_root)
        if not source_root.is_absolute():
            source_root = (base_dir / source_root).resolve()

        packages_data = data.get("packages", [])
        if not packages_data:
            raise ValueError("配置缺少 'packages' 列表")
        packages = [PackageConfig.from_dict(p, base_dir) for p in packages_data]

        operator = data.get("operator", os.environ.get("USER", os.environ.get("USERNAME", "unknown")))

        db_path = data.get("db_path", ".contract_pack.db")
        db_path = Path(db_path)
        if not db_path.is_absolute():
            db_path = (base_dir / db_path).resolve()

        audit_data = data.get("audit", {})
        if audit_data is None:
            audit_data = {}
        if not isinstance(audit_data, dict):
            raise AuditConfigError(
                f"audit 配置必须是字典，实际类型: {type(audit_data).__name__}"
            )
        try:
            audit_cfg = AuditConfig.from_dict(audit_data, base_dir)
        except AuditConfigError:
            raise
        except Exception as e:
            raise AuditConfigError(f"audit 配置解析失败: {e}")

        return cls(
            manifest_path=manifest_path,
            source_root=source_root,
            packages=packages,
            operator=operator,
            db_path=db_path,
            allow_overwrite=data.get("allow_overwrite", False),
            audit=audit_cfg,
        )

    def summary(self) -> Dict[str, Any]:
        return {
            "manifest_path": str(self.manifest_path),
            "source_root": str(self.source_root),
            "operator": self.operator,
            "packages": [p.to_dict() for p in self.packages],
            "allow_overwrite": self.allow_overwrite,
            "audit": self.audit.to_dict(),
        }
