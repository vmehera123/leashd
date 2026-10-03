import inspect
import re
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from telegram.error import NetworkError

from leashd.connectors.telegram import TelegramConnector
from leashd.connectors.telegram_commands import (
    ENGINE_MENU_COMMANDS,
    NATIVE_MENU_COMMANDS,
    menu_commands,
)
from leashd.core.engine import Engine

_BOT_API_COMMAND_NAME = re.compile(r"[a-z0-9_]{1,32}")
_BOT_API_MAX_COMMANDS = 100
_BOT_API_MAX_DESCRIPTION = 256


def _mock_builder(app):
    builder = MagicMock()
    builder.token.return_value = builder
    builder.concurrent_updates.return_value = builder
    builder.build.return_value = app
    return builder


def _mock_app():
    app = AsyncMock()
    app.bot = AsyncMock()
    app.updater = AsyncMock()
    app.add_handler = MagicMock()
    app.add_error_handler = MagicMock()
    return app


class TestMenuCommands:
    def test_names_fit_bot_api_rules(self):
        for command in menu_commands():
            assert _BOT_API_COMMAND_NAME.fullmatch(command.command), command.command

    def test_descriptions_fit_bot_api_rules(self):
        for command in menu_commands():
            assert 1 <= len(command.description) <= _BOT_API_MAX_DESCRIPTION

    def test_names_are_unique_and_within_limit(self):
        names = [command.command for command in menu_commands()]
        assert len(names) == len(set(names))
        assert len(names) <= _BOT_API_MAX_COMMANDS

    def test_engine_commands_are_dispatched_by_the_engine(self):
        dispatch = inspect.getsource(Engine._dispatch_command)
        for name, _ in ENGINE_MENU_COMMANDS:
            assert f'"{name}"' in dispatch, name

    def test_native_commands_are_left_to_claude(self):
        dispatch = inspect.getsource(Engine._dispatch_command)
        for name, _ in NATIVE_MENU_COMMANDS:
            assert f'"{name}"' not in dispatch, name


class TestCommandMenuRegistration:
    @pytest.fixture
    def connector(self):
        return TelegramConnector("fake:token")

    async def test_start_registers_the_menu(self, connector):
        app = _mock_app()
        with patch(
            "leashd.connectors.telegram.Application.builder",
            return_value=_mock_builder(app),
        ):
            await connector.start()

        app.bot.set_my_commands.assert_awaited_once()
        sent = app.bot.set_my_commands.await_args.args[0]
        assert [c.command for c in sent] == [c.command for c in menu_commands()]

    async def test_start_survives_a_failed_registration(self, connector):
        app = _mock_app()
        app.bot.set_my_commands.side_effect = NetworkError("down")
        with patch(
            "leashd.connectors.telegram.Application.builder",
            return_value=_mock_builder(app),
        ):
            await connector.start()

        app.updater.start_polling.assert_awaited_once()
