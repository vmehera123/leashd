"""Agent-agnostic permission types for the safety pipeline."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict


class PermissionAllow(BaseModel):
    model_config = ConfigDict(frozen=True)

    updated_input: dict[str, Any]

    @property
    def behavior(self) -> Literal["allow"]:
        return "allow"


class PermissionDeny(BaseModel):
    model_config = ConfigDict(frozen=True)

    message: str

    @property
    def behavior(self) -> Literal["deny"]:
        return "deny"


PermissionResult = PermissionAllow | PermissionDeny
