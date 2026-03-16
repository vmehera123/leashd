"""Tests for agent-agnostic permission types."""

import pytest
from pydantic import ValidationError

from leashd.agents.types import PermissionAllow, PermissionDeny


class TestPermissionAllow:
    def test_construction_and_behavior(self):
        result = PermissionAllow(updated_input={"command": "ls"})
        assert result.behavior == "allow"
        assert result.updated_input == {"command": "ls"}

    def test_empty_input(self):
        result = PermissionAllow(updated_input={})
        assert result.behavior == "allow"
        assert result.updated_input == {}

    def test_frozen_immutability(self):
        result = PermissionAllow(updated_input={"a": 1})
        with pytest.raises(ValidationError):
            result.updated_input = {"b": 2}  # type: ignore[misc]


class TestPermissionDeny:
    def test_construction_and_behavior(self):
        result = PermissionDeny(message="blocked by policy")
        assert result.behavior == "deny"
        assert result.message == "blocked by policy"

    def test_frozen_immutability(self):
        result = PermissionDeny(message="no")
        with pytest.raises(ValidationError):
            result.message = "yes"  # type: ignore[misc]
