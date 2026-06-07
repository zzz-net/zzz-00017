"""审计时间线模块 - 记录所有 CLI 操作流水并支持查询和导出"""

from __future__ import annotations

import csv
import hashlib
import json
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional


AUDIT_COMMAND_TYPES = {
    "DRY_RUN": "dry-run",
    "RUN": "run",
    "RERUN": "rerun",
    "ROLLBACK": "rollback",
    "DIFF": "diff",
    "TEMPLATE_SAVE": "template-save",
    "TEMPLATE_LIST": "template-list",
    "TEMPLATE_SHOW": "template-show",
    "TEMPLATE_DELETE": "template-delete",
    "TEMPLATE_EXPORT": "template-export",
    "TEMPLATE_IMPORT": "template-import",
    "TEMPLATE_APPLY": "template-apply",
    "EXPORT": "export",
    "AUDIT_QUERY": "audit-query",
    "AUDIT_EXPORT": "audit-export",
}


AUDIT_RESULT_STATUS = {
    "SUCCESS": "success",
    "FAILED": "failed",
    "PARTIAL": "partial",
    "SKIPPED": "skipped",
}


AUDIT_EXPORT_FIELDNAMES = [
    "id",
    "command_type",
    "operator",
    "started_at",
    "finished_at",
    "duration_seconds",
    "result_status",
    "batch_id",
    "package_names",
    "file_count",
    "error_count",
    "warning_count",
    "params_summary",
    "config_summary",
    "error_summary",
    "detail_ref",
]


class AuditError(Exception):
    """审计模块错误基类"""
    pass


class AuditDatabaseError(AuditError):
    """审计数据库错误"""
    pass


class AuditDuplicateError(AuditError):
    """重复写入审计记录错误"""
    pass


class AuditExportError(AuditError):
    """审计导出错误"""
    pass


class AuditConfigError(AuditError):
    """审计配置错误"""
    pass


@dataclass
class AuditRecord:
    """单条审计记录"""
    id: str
    command_type: str
    operator: str
    started_at: str
    finished_at: Optional[str] = None
    result_status: str = AUDIT_RESULT_STATUS.get("RUNNING", AUDIT_RESULT_STATUS["FAILED"])
    params_summary: Dict[str, Any] = field(default_factory=dict)
    config_summary: Dict[str, Any] = field(default_factory=dict)
    batch_id: Optional[str] = None
    package_names: List[str] = field(default_factory=list)
    file_count: int = 0
    error_count: int = 0
    warning_count: int = 0
    error_summary: str = ""
    detail_ref: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        duration = None
        if self.started_at and self.finished_at:
            try:
                start = datetime.fromisoformat(self.started_at)
                end = datetime.fromisoformat(self.finished_at)
                duration = round((end - start).total_seconds(), 3)
            except (ValueError, TypeError):
                duration = None
        return {
            "id": self.id,
            "command_type": self.command_type,
            "operator": self.operator,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_seconds": duration,
            "result_status": self.result_status,
            "batch_id": self.batch_id or "",
            "package_names": self.package_names,
            "file_count": self.file_count,
            "error_count": self.error_count,
            "warning_count": self.warning_count,
            "params_summary": self.params_summary,
            "config_summary": self.config_summary,
            "error_summary": self.error_summary,
            "detail_ref": self.detail_ref,
        }


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _generate_dedupe_key(
    command_type: str,
    operator: str,
    params_summary: Dict[str, Any],
    started_at: str,
) -> str:
    """生成用于去重的哈希键"""
    payload = {
        "command_type": command_type,
        "operator": operator,
        "params_summary": params_summary,
        "started_at_minute": started_at[:16],
    }
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class AuditStorage:
    """审计记录 SQLite 存储"""

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        try:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            raise AuditDatabaseError(f"无法创建审计数据库目录 {self.db_path.parent}: {e}")
        self._init_schema()

    @contextmanager
    def _conn(self):
        try:
            conn = sqlite3.connect(str(self.db_path))
            conn.row_factory = sqlite3.Row
        except sqlite3.Error as e:
            raise AuditDatabaseError(f"无法连接审计数据库 {self.db_path}: {e}")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_schema(self):
        try:
            with self._conn() as c:
                c.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS audit_records (
                        id TEXT PRIMARY KEY,
                        dedupe_key TEXT NOT NULL UNIQUE,
                        command_type TEXT NOT NULL,
                        operator TEXT NOT NULL,
                        started_at TEXT NOT NULL,
                        finished_at TEXT,
                        result_status TEXT NOT NULL DEFAULT 'failed',
                        params_summary TEXT NOT NULL DEFAULT '{}',
                        config_summary TEXT NOT NULL DEFAULT '{}',
                        batch_id TEXT,
                        package_names TEXT NOT NULL DEFAULT '[]',
                        file_count INTEGER NOT NULL DEFAULT 0,
                        error_count INTEGER NOT NULL DEFAULT 0,
                        warning_count INTEGER NOT NULL DEFAULT 0,
                        error_summary TEXT NOT NULL DEFAULT '',
                        detail_ref TEXT NOT NULL DEFAULT '{}'
                    );
                    CREATE INDEX IF NOT EXISTS idx_audit_started
                        ON audit_records(started_at DESC);
                    CREATE INDEX IF NOT EXISTS idx_audit_command
                        ON audit_records(command_type);
                    CREATE INDEX IF NOT EXISTS idx_audit_operator
                        ON audit_records(operator);
                    CREATE INDEX IF NOT EXISTS idx_audit_batch
                        ON audit_records(batch_id);
                    CREATE INDEX IF NOT EXISTS idx_audit_status
                        ON audit_records(result_status);
                    """
                )
                self._migrate_schema(c)
        except sqlite3.Error as e:
            raise AuditDatabaseError(f"初始化审计数据库表失败: {e}")

    def _migrate_schema(self, c: sqlite3.Connection):
        try:
            cols = {r[1] for r in c.execute("PRAGMA table_info(audit_records)").fetchall()}
            if "dedupe_key" not in cols:
                c.execute("ALTER TABLE audit_records ADD COLUMN dedupe_key TEXT")
                try:
                    c.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_audit_dedupe ON audit_records(dedupe_key)")
                except sqlite3.OperationalError:
                    pass
            if "warning_count" not in cols:
                c.execute("ALTER TABLE audit_records ADD COLUMN warning_count INTEGER NOT NULL DEFAULT 0")
        except sqlite3.OperationalError:
            pass

    def start_record(
        self,
        command_type: str,
        operator: str,
        params_summary: Optional[Dict[str, Any]] = None,
        config_summary: Optional[Dict[str, Any]] = None,
        batch_id: Optional[str] = None,
        package_names: Optional[List[str]] = None,
    ) -> AuditRecord:
        """开始一条审计记录（创建 running 状态的记录），若同一分钟内同参数已存在则抛出重复错误"""
        if command_type not in set(AUDIT_COMMAND_TYPES.values()):
            raise AuditConfigError(f"未知的命令类型: {command_type}")

        params_summary = params_summary or {}
        config_summary = config_summary or {}
        package_names = package_names or []
        started_at = _now_iso()
        dedupe_key = _generate_dedupe_key(command_type, operator, params_summary, started_at)

        record = AuditRecord(
            id=str(uuid.uuid4()),
            command_type=command_type,
            operator=operator,
            started_at=started_at,
            result_status=AUDIT_RESULT_STATUS["FAILED"],
            params_summary=params_summary,
            config_summary=config_summary,
            batch_id=batch_id,
            package_names=package_names,
        )

        try:
            with self._conn() as c:
                existing = c.execute(
                    "SELECT id FROM audit_records WHERE dedupe_key = ?",
                    (dedupe_key,),
                ).fetchone()
                if existing:
                    raise AuditDuplicateError(
                        f"同一操作在一分钟内已记录过 (dedupe_key={dedupe_key[:12]}...)"
                    )
                c.execute(
                    """INSERT INTO audit_records
                       (id, dedupe_key, command_type, operator, started_at, result_status,
                        params_summary, config_summary, batch_id, package_names,
                        file_count, error_count, warning_count, error_summary, detail_ref)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        record.id,
                        dedupe_key,
                        record.command_type,
                        record.operator,
                        record.started_at,
                        record.result_status,
                        json.dumps(params_summary, ensure_ascii=False),
                        json.dumps(config_summary, ensure_ascii=False),
                        record.batch_id,
                        json.dumps(package_names, ensure_ascii=False),
                        0, 0, 0, "", "{}",
                    ),
                )
        except sqlite3.IntegrityError as e:
            raise AuditDuplicateError(f"审计记录去重冲突: {e}")
        except sqlite3.Error as e:
            raise AuditDatabaseError(f"写入审计记录失败: {e}")

        return record

    def finish_record(
        self,
        record_id: str,
        result_status: str,
        file_count: int = 0,
        error_count: int = 0,
        warning_count: int = 0,
        error_summary: str = "",
        detail_ref: Optional[Dict[str, Any]] = None,
        batch_id: Optional[str] = None,
        package_names: Optional[List[str]] = None,
    ) -> None:
        """完成一条审计记录，更新状态和统计信息"""
        if result_status not in set(AUDIT_RESULT_STATUS.values()):
            raise AuditConfigError(f"未知的结果状态: {result_status}")

        detail_ref = detail_ref or {}
        updates = {
            "result_status": result_status,
            "finished_at": _now_iso(),
            "file_count": file_count,
            "error_count": error_count,
            "warning_count": warning_count,
            "error_summary": error_summary,
            "detail_ref": json.dumps(detail_ref, ensure_ascii=False),
        }
        if batch_id is not None:
            updates["batch_id"] = batch_id
        if package_names is not None:
            updates["package_names"] = json.dumps(package_names, ensure_ascii=False)

        set_clause = ", ".join(f"{k} = ?" for k in updates.keys())
        params = list(updates.values()) + [record_id]

        try:
            with self._conn() as c:
                cur = c.execute(
                    f"UPDATE audit_records SET {set_clause} WHERE id = ?",
                    params,
                )
                if cur.rowcount == 0:
                    raise AuditDatabaseError(f"审计记录不存在: {record_id}")
        except sqlite3.Error as e:
            raise AuditDatabaseError(f"更新审计记录失败: {e}")

    def record_operation(
        self,
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
    ) -> AuditRecord:
        """一次性写入完整审计记录（适合快速操作），失败时也保证留痕"""
        params_summary = params_summary or {}
        config_summary = config_summary or {}
        package_names = package_names or []
        detail_ref = detail_ref or {}
        started_at = _now_iso()
        dedupe_key = _generate_dedupe_key(command_type, operator, params_summary, started_at)

        record = AuditRecord(
            id=str(uuid.uuid4()),
            command_type=command_type,
            operator=operator,
            started_at=started_at,
            finished_at=started_at,
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

        try:
            with self._conn() as c:
                existing = c.execute(
                    "SELECT id FROM audit_records WHERE dedupe_key = ?",
                    (dedupe_key,),
                ).fetchone()
                if existing:
                    raise AuditDuplicateError(
                        f"同一操作在一分钟内已记录过 (dedupe_key={dedupe_key[:12]}...)"
                    )
                c.execute(
                    """INSERT INTO audit_records
                       (id, dedupe_key, command_type, operator, started_at, finished_at,
                        result_status, params_summary, config_summary, batch_id,
                        package_names, file_count, error_count, warning_count,
                        error_summary, detail_ref)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        record.id,
                        dedupe_key,
                        record.command_type,
                        record.operator,
                        record.started_at,
                        record.finished_at,
                        record.result_status,
                        json.dumps(params_summary, ensure_ascii=False),
                        json.dumps(config_summary, ensure_ascii=False),
                        record.batch_id,
                        json.dumps(package_names, ensure_ascii=False),
                        file_count, error_count, warning_count,
                        error_summary,
                        json.dumps(detail_ref, ensure_ascii=False),
                    ),
                )
        except sqlite3.IntegrityError as e:
            raise AuditDuplicateError(f"审计记录去重冲突: {e}")
        except sqlite3.Error as e:
            raise AuditDatabaseError(f"写入审计记录失败: {e}")

        return record

    def _row_to_record(self, row: sqlite3.Row) -> AuditRecord:
        rd = dict(row)
        return AuditRecord(
            id=rd.get("id", ""),
            command_type=rd.get("command_type", ""),
            operator=rd.get("operator", ""),
            started_at=rd.get("started_at", ""),
            finished_at=rd.get("finished_at"),
            result_status=rd.get("result_status", AUDIT_RESULT_STATUS["FAILED"]),
            params_summary=self._safe_json_load(rd.get("params_summary"), {}),
            config_summary=self._safe_json_load(rd.get("config_summary"), {}),
            batch_id=rd.get("batch_id"),
            package_names=self._safe_json_load(rd.get("package_names"), []),
            file_count=rd.get("file_count", 0) or 0,
            error_count=rd.get("error_count", 0) or 0,
            warning_count=rd.get("warning_count", 0) or 0,
            error_summary=rd.get("error_summary", "") or "",
            detail_ref=self._safe_json_load(rd.get("detail_ref"), {}),
        )

    @staticmethod
    def _safe_json_load(raw: Optional[str], default: Any) -> Any:
        if not raw:
            return default
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return default

    def query_records(
        self,
        start_time: Optional[str] = None,
        end_time: Optional[str] = None,
        operator: Optional[str] = None,
        command_type: Optional[str] = None,
        command_types: Optional[List[str]] = None,
        batch_id: Optional[str] = None,
        package_name: Optional[str] = None,
        result_status: Optional[str] = None,
        limit: int = 100,
    ) -> List[AuditRecord]:
        """按条件查询审计记录，支持多维度过滤组合"""
        conditions: List[str] = []
        params: List[Any] = []

        if start_time:
            conditions.append("started_at >= ?")
            params.append(start_time)
        if end_time:
            conditions.append("started_at <= ?")
            params.append(end_time)
        if operator:
            conditions.append("operator = ?")
            params.append(operator)
        if command_type:
            conditions.append("command_type = ?")
            params.append(command_type)
        if command_types:
            placeholders = ", ".join("?" for _ in command_types)
            conditions.append(f"command_type IN ({placeholders})")
            params.extend(command_types)
        if batch_id:
            conditions.append("batch_id = ?")
            params.append(batch_id)
        if result_status:
            conditions.append("result_status = ?")
            params.append(result_status)
        if package_name:
            conditions.append("package_names LIKE ?")
            params.append(f'%"{package_name}"%')

        where_clause = (" WHERE " + " AND ".join(conditions)) if conditions else ""
        query = f"""
            SELECT * FROM audit_records
            {where_clause}
            ORDER BY started_at DESC
            LIMIT ?
        """
        params.append(limit)

        try:
            with self._conn() as c:
                rows = c.execute(query, params).fetchall()
            return [self._row_to_record(r) for r in rows]
        except sqlite3.Error as e:
            raise AuditDatabaseError(f"查询审计记录失败: {e}")

    def get_record(self, record_id: str) -> Optional[AuditRecord]:
        """获取单条审计记录"""
        try:
            with self._conn() as c:
                row = c.execute(
                    "SELECT * FROM audit_records WHERE id = ?",
                    (record_id,),
                ).fetchone()
            return self._row_to_record(row) if row else None
        except sqlite3.Error as e:
            raise AuditDatabaseError(f"获取审计记录失败: {e}")

    def cleanup_old_records(self, retention_days: int) -> int:
        """清理超过保留天数的旧记录，返回删除的条数"""
        if retention_days <= 0:
            return 0
        cutoff = (datetime.now() - timedelta(days=retention_days)).isoformat(timespec="seconds")
        try:
            with self._conn() as c:
                cur = c.execute(
                    "DELETE FROM audit_records WHERE started_at < ?",
                    (cutoff,),
                )
                return cur.rowcount
        except sqlite3.Error as e:
            raise AuditDatabaseError(f"清理旧审计记录失败: {e}")


def _validate_export_path(out_path: Path) -> None:
    """验证导出路径的可写性，路径已存在、只读目录时报可读错误"""
    if out_path.exists():
        if out_path.is_dir():
            raise AuditExportError(f"导出路径已存在且是目录: {out_path}")
        raise AuditExportError(f"导出文件已存在: {out_path}（请先删除或指定其他路径）")

    parent = out_path.parent
    if not parent.exists():
        try:
            parent.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            raise AuditExportError(f"无法创建导出目录 {parent}: {e}")

    if not parent.is_dir():
        raise AuditExportError(f"导出路径父级不是目录: {parent}")

    test_file = parent / f"._audit_writable_test_{uuid.uuid4().hex}"
    try:
        test_file.write_text("", encoding="utf-8")
        test_file.unlink()
    except PermissionError:
        raise AuditExportError(f"导出目录无写入权限: {parent}")
    except OSError as e:
        raise AuditExportError(f"导出目录不可写 {parent}: {e}")


def export_audit_json(records: List[AuditRecord], out_path: Path) -> None:
    """导出审计记录为 JSON（字段稳定）"""
    out_path = Path(out_path)
    _validate_export_path(out_path)
    data = [r.to_dict() for r in records]
    try:
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except PermissionError:
        raise AuditExportError(f"导出失败: 权限不足，无法写入 {out_path}")
    except OSError as e:
        raise AuditExportError(f"导出 JSON 失败: {e}")


def export_audit_csv(records: List[AuditRecord], out_path: Path) -> None:
    """导出审计记录为 CSV（字段稳定，复杂字段用 JSON 序列化）"""
    out_path = Path(out_path)
    _validate_export_path(out_path)
    try:
        with open(out_path, "w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=AUDIT_EXPORT_FIELDNAMES)
            writer.writeheader()
            for r in records:
                d = r.to_dict()
                writer.writerow({
                    "id": d["id"],
                    "command_type": d["command_type"],
                    "operator": d["operator"],
                    "started_at": d["started_at"],
                    "finished_at": d["finished_at"] or "",
                    "duration_seconds": d["duration_seconds"] if d["duration_seconds"] is not None else "",
                    "result_status": d["result_status"],
                    "batch_id": d["batch_id"],
                    "package_names": json.dumps(d["package_names"], ensure_ascii=False),
                    "file_count": d["file_count"],
                    "error_count": d["error_count"],
                    "warning_count": d["warning_count"],
                    "params_summary": json.dumps(d["params_summary"], ensure_ascii=False),
                    "config_summary": json.dumps(d["config_summary"], ensure_ascii=False),
                    "error_summary": d["error_summary"],
                    "detail_ref": json.dumps(d["detail_ref"], ensure_ascii=False),
                })
    except PermissionError:
        raise AuditExportError(f"导出失败: 权限不足，无法写入 {out_path}")
    except OSError as e:
        raise AuditExportError(f"导出 CSV 失败: {e}")
