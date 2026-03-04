"""SQLite session store — sessions survive restarts."""

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiosqlite
import structlog

from leashd.core.session import Session
from leashd.exceptions import StorageError

logger = structlog.get_logger()

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS sessions (
    user_id TEXT NOT NULL,
    chat_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    working_directory TEXT NOT NULL,
    claude_session_id TEXT,
    created_at TEXT NOT NULL,
    last_used TEXT NOT NULL,
    total_cost REAL DEFAULT 0.0,
    message_count INTEGER DEFAULT 0,
    is_active INTEGER DEFAULT 1,
    PRIMARY KEY (user_id, chat_id)
)
"""

_CREATE_MESSAGES_TABLE = """
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    chat_id TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    content TEXT NOT NULL,
    cost REAL,
    duration_ms INTEGER,
    session_id TEXT,
    created_at TEXT NOT NULL
)
"""

_CREATE_MESSAGES_INDEX = """
CREATE INDEX IF NOT EXISTS idx_messages_chat
ON messages (user_id, chat_id, created_at)
"""


class SqliteSessionStore:
    def __init__(self, db_path: Path | str) -> None:
        self._db_path = str(db_path)
        self._db: aiosqlite.Connection | None = None

    async def setup(self) -> None:
        try:
            self._db = await aiosqlite.connect(self._db_path)
            self._db.row_factory = aiosqlite.Row
            await self._db.execute(_CREATE_TABLE)
            await self._db.execute(_CREATE_MESSAGES_TABLE)
            await self._db.execute(_CREATE_MESSAGES_INDEX)

            # Idempotent migrations for columns added after initial schema
            cursor = await self._db.execute("PRAGMA table_info(sessions)")
            existing = {row[1] for row in await cursor.fetchall()}
            if "workspace_name" not in existing:
                await self._db.execute(
                    "ALTER TABLE sessions ADD COLUMN workspace_name TEXT"
                )

            await self._db.commit()
            logger.info("sqlite_store_initialized", db_path=self._db_path)
        except Exception as e:
            logger.error(
                "sqlite_store_init_failed", db_path=self._db_path, error=str(e)
            )
            raise StorageError(f"Failed to initialize SQLite store: {e}") from e

    async def switch_db(self, new_path: Path | str) -> None:
        """Close current DB and open a new one (e.g. on /dir switch)."""
        new_str = str(new_path)
        if new_str == self._db_path:
            return
        if self._db:
            await self._db.close()
            self._db = None
        Path(new_str).parent.mkdir(parents=True, exist_ok=True)
        self._db_path = new_str
        await self.setup()

    async def teardown(self) -> None:
        if self._db:
            await self._db.close()
            self._db = None
            logger.info("sqlite_store_closed", db_path=self._db_path)

    async def save(self, session: Session) -> None:
        if not self._db:
            raise StorageError("Store not initialized — call setup() first")
        await self._db.execute(
            """INSERT OR REPLACE INTO sessions
               (user_id, chat_id, session_id, working_directory,
                claude_session_id, created_at, last_used,
                total_cost, message_count, is_active,
                workspace_name)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                session.user_id,
                session.chat_id,
                session.session_id,
                session.working_directory,
                session.claude_session_id,
                session.created_at.isoformat(),
                session.last_used.isoformat(),
                session.total_cost,
                session.message_count,
                int(session.is_active),
                session.workspace_name,
            ),
        )
        await self._db.commit()

    async def load(self, user_id: str, chat_id: str) -> Session | None:
        if not self._db:
            raise StorageError("Store not initialized — call setup() first")
        cursor = await self._db.execute(
            "SELECT * FROM sessions WHERE user_id = ? AND chat_id = ? AND is_active = 1",
            (user_id, chat_id),
        )
        row = await cursor.fetchone()
        if not row:
            logger.debug("sqlite_session_not_found", user_id=user_id, chat_id=chat_id)
            return None
        return self._row_to_session(row)

    async def delete(self, user_id: str, chat_id: str) -> None:
        if not self._db:
            raise StorageError("Store not initialized — call setup() first")
        await self._db.execute(
            "UPDATE sessions SET is_active = 0 WHERE user_id = ? AND chat_id = ?",
            (user_id, chat_id),
        )
        await self._db.commit()

    @staticmethod
    def _row_to_session(row: aiosqlite.Row) -> Session:
        return Session(
            session_id=row["session_id"],
            user_id=row["user_id"],
            chat_id=row["chat_id"],
            working_directory=row["working_directory"],
            claude_session_id=row["claude_session_id"],
            created_at=datetime.fromisoformat(row["created_at"]).replace(
                tzinfo=timezone.utc
            ),
            last_used=datetime.fromisoformat(row["last_used"]).replace(
                tzinfo=timezone.utc
            ),
            total_cost=row["total_cost"],
            message_count=row["message_count"],
            is_active=bool(row["is_active"]),
            workspace_name=row["workspace_name"],
        )

    async def save_message(
        self,
        *,
        user_id: str,
        chat_id: str,
        role: str,
        content: str,
        cost: float | None = None,
        duration_ms: int | None = None,
        session_id: str | None = None,
    ) -> None:
        if not self._db:
            raise StorageError("Store not initialized — call setup() first")
        await self._db.execute(
            """INSERT INTO messages
               (user_id, chat_id, role, content, cost, duration_ms, session_id, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                user_id,
                chat_id,
                role,
                content,
                cost,
                duration_ms,
                session_id,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        await self._db.commit()

    async def get_messages(
        self,
        user_id: str,
        chat_id: str,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        if not self._db:
            raise StorageError("Store not initialized — call setup() first")
        cursor = await self._db.execute(
            """SELECT * FROM messages
               WHERE user_id = ? AND chat_id = ?
               ORDER BY created_at ASC, id ASC
               LIMIT ? OFFSET ?""",
            (user_id, chat_id, limit, offset),
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]
