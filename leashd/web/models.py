"""WebSocket message models for the WebUI protocol."""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

# --- Client → Server ---

ClientMessageType = Literal[
    "auth",
    "message",
    "approval_response",
    "interaction_response",
    "interrupt_response",
    "ping",
]


class ClientMessage(BaseModel):
    model_config = ConfigDict(frozen=True)

    type: ClientMessageType
    payload: dict[str, Any] = {}


# --- Server → Client ---

ServerMessageType = Literal[
    "auth_ok",
    "auth_error",
    "history",
    "message",
    "stream_token",
    "tool_start",
    "tool_end",
    "approval_request",
    "approval_resolved",
    "message_complete",
    "message_delete",
    "question",
    "plan_review",
    "interrupt_prompt",
    "pending_state",
    "task_update",
    "error",
    "pong",
    "status",
    "reload",
    "config_updated",
]


class ServerMessage(BaseModel):
    model_config = ConfigDict(frozen=True)

    type: ServerMessageType
    payload: dict[str, Any] = {}


# --- Tab API models ---


class TabInfo(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    path: str


class WorkspaceTabInfo(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    description: str = ""
    directories: list[str] = []
