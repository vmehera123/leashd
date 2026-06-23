"""Abstract session and message store protocols."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from leashd.core.session import Session


@runtime_checkable
class SessionStore(Protocol):
    async def save(self, session: Session) -> None: ...

    async def load(self, user_id: str, chat_id: str) -> Session | None: ...

    async def delete(self, user_id: str, chat_id: str) -> None: ...

    async def setup(self) -> None: ...

    async def teardown(self) -> None: ...


@runtime_checkable
class MessageStore(Protocol):
    async def save_message(
        self,
        *,
        user_id: str,
        chat_id: str,
        role: str,
        content: str,
        cost: float | None = ...,
        duration_ms: int | None = ...,
        session_id: str | None = ...,
    ) -> None: ...

    async def get_messages(
        self,
        user_id: str,
        chat_id: str,
        *,
        limit: int = ...,
        offset: int = ...,
    ) -> list[dict[str, Any]]: ...

    async def switch_db(self, new_path: Path | str) -> None: ...

    async def setup(self) -> None: ...

    async def teardown(self) -> None: ...
