"""批次持久化存储 - 使用 SQLite"""

from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional


BATCH_STATUS = {
    "PENDING": "pending",
    "RUNNING": "running",
    "COMPLETED": "completed",
    "PARTIAL": "partial",
    "FAILED": "failed",
    "ROLLED_BACK": "rolled_back",
    "ROLLBACK_FAILED": "rollback_failed",
}

FILE_ACTION = {
    "COPY": "copy",
    "ZIP": "zip",
    "SKIP": "skip",
    "DELETE": "delete",
}

FILE_STATUS = {
    "PENDING": "pending",
    "SUCCESS": "success",
    "FAILED": "failed",
    "SKIPPED": "skipped",
    "ROLLED_BACK": "rolled_back",
}


@dataclass
class FileAction:
    id: str
    batch_id: str
    package: str
    action: str
    source_path: str
    target_path: str
    category: str
    status: str
    error: str = ""
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    file_hash: Optional[str] = None
    file_size: Optional[int] = None


@dataclass
class Batch:
    id: str
    status: str
    operator: str
    started_at: str
    finished_at: Optional[str] = None
    error: str = ""
    config_summary: Dict[str, Any] = field(default_factory=dict)
    file_actions: List[FileAction] = field(default_factory=list)
    parent_batch_id: Optional[str] = None
    rerun_params: Dict[str, Any] = field(default_factory=dict)


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


class BatchStorage:
    """批次 SQLite 存储"""

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
        finally:
            conn.close()

    def _init_schema(self):
        with self._conn() as c:
            c.executescript(
                """
                CREATE TABLE IF NOT EXISTS batches (
                    id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    operator TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    error TEXT DEFAULT '',
                    config_summary TEXT DEFAULT '{}',
                    parent_batch_id TEXT,
                    rerun_params TEXT DEFAULT '{}'
                );

                CREATE TABLE IF NOT EXISTS file_actions (
                    id TEXT PRIMARY KEY,
                    batch_id TEXT NOT NULL,
                    package TEXT NOT NULL,
                    action TEXT NOT NULL,
                    source_path TEXT NOT NULL,
                    target_path TEXT NOT NULL,
                    category TEXT NOT NULL,
                    status TEXT NOT NULL,
                    error TEXT DEFAULT '',
                    started_at TEXT,
                    finished_at TEXT,
                    FOREIGN KEY (batch_id) REFERENCES batches(id)
                );

                CREATE INDEX IF NOT EXISTS idx_file_actions_batch
                    ON file_actions(batch_id);
                CREATE INDEX IF NOT EXISTS idx_batches_started
                    ON batches(started_at DESC);
                CREATE INDEX IF NOT EXISTS idx_batches_parent
                    ON batches(parent_batch_id);
                """
            )
            self._migrate_schema(c)

    def _migrate_schema(self, c: sqlite3.Connection):
        batch_cols = {r[1] for r in c.execute("PRAGMA table_info(batches)").fetchall()}
        if "parent_batch_id" not in batch_cols:
            c.execute("ALTER TABLE batches ADD COLUMN parent_batch_id TEXT")
        if "rerun_params" not in batch_cols:
            c.execute("ALTER TABLE batches ADD COLUMN rerun_params TEXT DEFAULT '{}'")
        try:
            c.execute("CREATE INDEX IF NOT EXISTS idx_batches_parent ON batches(parent_batch_id)")
        except sqlite3.OperationalError:
            pass

        cols = {r[1] for r in c.execute("PRAGMA table_info(file_actions)").fetchall()}
        if "file_hash" not in cols:
            c.execute("ALTER TABLE file_actions ADD COLUMN file_hash TEXT")
        if "file_size" not in cols:
            c.execute("ALTER TABLE file_actions ADD COLUMN file_size INTEGER")

    def create_batch(
        self,
        operator: str,
        config_summary: Dict[str, Any],
        parent_batch_id: Optional[str] = None,
        rerun_params: Optional[Dict[str, Any]] = None,
    ) -> Batch:
        batch = Batch(
            id=str(uuid.uuid4()),
            status=BATCH_STATUS["PENDING"],
            operator=operator,
            started_at=_now_iso(),
            config_summary=config_summary,
            parent_batch_id=parent_batch_id,
            rerun_params=rerun_params or {},
        )
        with self._conn() as c:
            c.execute(
                "INSERT INTO batches (id, status, operator, started_at, config_summary, parent_batch_id, rerun_params) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    batch.id,
                    batch.status,
                    batch.operator,
                    batch.started_at,
                    json.dumps(config_summary, ensure_ascii=False),
                    batch.parent_batch_id,
                    json.dumps(batch.rerun_params, ensure_ascii=False),
                ),
            )
        return batch

    def update_batch_status(self, batch_id: str, status: str, error: str = "", finished: bool = False):
        with self._conn() as c:
            if finished:
                c.execute(
                    "UPDATE batches SET status=?, error=?, finished_at=? WHERE id=?",
                    (status, error, _now_iso(), batch_id),
                )
            else:
                c.execute(
                    "UPDATE batches SET status=?, error=? WHERE id=?",
                    (status, error, batch_id),
                )

    def add_file_action(
        self,
        batch_id: str,
        package: str,
        action: str,
        source_path: str,
        target_path: str,
        category: str,
        status: str = FILE_STATUS["PENDING"],
        file_hash: Optional[str] = None,
        file_size: Optional[int] = None,
    ) -> str:
        fa_id = str(uuid.uuid4())
        with self._conn() as c:
            c.execute(
                """INSERT INTO file_actions
                   (id, batch_id, package, action, source_path, target_path, category, status, started_at, file_hash, file_size)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (fa_id, batch_id, package, action, source_path, target_path, category, status, _now_iso(), file_hash, file_size),
            )
        return fa_id

    def update_file_action(self, fa_id: str, status: str, error: str = "", file_hash: Optional[str] = None, file_size: Optional[int] = None):
        with self._conn() as c:
            if file_hash is not None or file_size is not None:
                c.execute(
                    "UPDATE file_actions SET status=?, error=?, finished_at=?, file_hash=COALESCE(?, file_hash), file_size=COALESCE(?, file_size) WHERE id=?",
                    (status, error, _now_iso(), file_hash, file_size, fa_id),
                )
            else:
                c.execute(
                    "UPDATE file_actions SET status=?, error=?, finished_at=? WHERE id=?",
                    (status, error, _now_iso(), fa_id),
                )

    def get_batch(self, batch_id: str) -> Optional[Batch]:
        with self._conn() as c:
            row = c.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()
            if not row:
                return None
            batch = Batch(
                id=row["id"],
                status=row["status"],
                operator=row["operator"],
                started_at=row["started_at"],
                finished_at=row["finished_at"],
                error=row["error"],
                config_summary=json.loads(row["config_summary"] or "{}"),
                parent_batch_id=row["parent_batch_id"],
                rerun_params=json.loads(row["rerun_params"] or "{}"),
            )
            fa_rows = c.execute(
                "SELECT * FROM file_actions WHERE batch_id=? ORDER BY started_at",
                (batch_id,),
            ).fetchall()
            batch.file_actions = []
            for r in fa_rows:
                rd = dict(r)
                batch.file_actions.append(
                    FileAction(
                        id=rd["id"],
                        batch_id=rd["batch_id"],
                        package=rd["package"],
                        action=rd["action"],
                        source_path=rd["source_path"],
                        target_path=rd["target_path"],
                        category=rd["category"],
                        status=rd["status"],
                        error=rd["error"],
                        started_at=rd.get("started_at"),
                        finished_at=rd.get("finished_at"),
                        file_hash=rd.get("file_hash"),
                        file_size=rd.get("file_size"),
                    )
                )
            return batch

    def list_batches(self, limit: int = 20) -> List[Batch]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT id FROM batches ORDER BY started_at DESC LIMIT ?", (limit,)
            ).fetchall()
        result: List[Batch] = []
        for r in rows:
            b = self.get_batch(r["id"])
            if b:
                result.append(b)
        return result
