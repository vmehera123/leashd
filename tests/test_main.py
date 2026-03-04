"""Tests for leashd.main — CLI entry point logic."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from leashd.exceptions import ConnectorError
from leashd.main import _main as main
from leashd.main import run


@pytest.fixture
def mock_engine():
    engine = AsyncMock()
    engine.startup = AsyncMock()
    engine.shutdown = AsyncMock()
    engine.handle_message = AsyncMock(return_value="response text")
    return engine


@pytest.fixture
def mock_config(tmp_path):
    cfg = MagicMock()
    cfg.approved_directories = [tmp_path]
    cfg.telegram_bot_token = None
    return cfg


class TestMain:
    @pytest.mark.asyncio
    async def test_config_error_exits(self):
        with (
            patch("leashd.main.inject_global_config_as_env"),
            patch("leashd.main.LeashdConfig", side_effect=ValueError("bad config")),
        ):
            with pytest.raises(SystemExit) as exc_info:
                await main()
            assert exc_info.value.code == 1

    @pytest.mark.asyncio
    async def test_successful_startup(self, mock_engine, mock_config):
        with (
            patch("leashd.main.inject_global_config_as_env"),
            patch("leashd.main.LeashdConfig", return_value=mock_config),
            patch("leashd.main.build_engine", return_value=mock_engine),
            patch("builtins.input", side_effect=EOFError),
        ):
            await main()

        mock_engine.startup.assert_called_once()

    @pytest.mark.asyncio
    async def test_eof_triggers_shutdown(self, mock_engine, mock_config):
        with (
            patch("leashd.main.inject_global_config_as_env"),
            patch("leashd.main.LeashdConfig", return_value=mock_config),
            patch("leashd.main.build_engine", return_value=mock_engine),
            patch("builtins.input", side_effect=EOFError),
        ):
            await main()

        mock_engine.shutdown.assert_called_once()

    @pytest.mark.asyncio
    async def test_keyboard_interrupt_shutdown(self, mock_engine, mock_config):
        with (
            patch("leashd.main.inject_global_config_as_env"),
            patch("leashd.main.LeashdConfig", return_value=mock_config),
            patch("leashd.main.build_engine", return_value=mock_engine),
            patch("builtins.input", side_effect=KeyboardInterrupt),
        ):
            await main()

        mock_engine.shutdown.assert_called_once()

    @pytest.mark.asyncio
    async def test_empty_input_skipped(self, mock_engine, mock_config):
        with (
            patch("leashd.main.inject_global_config_as_env"),
            patch("leashd.main.LeashdConfig", return_value=mock_config),
            patch("leashd.main.build_engine", return_value=mock_engine),
            patch("builtins.input", side_effect=["   ", EOFError]),
        ):
            await main()

        mock_engine.handle_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_valid_input_dispatched(self, mock_engine, mock_config):
        with (
            patch("leashd.main.inject_global_config_as_env"),
            patch("leashd.main.LeashdConfig", return_value=mock_config),
            patch("leashd.main.build_engine", return_value=mock_engine),
            patch("builtins.input", side_effect=["hello", EOFError]),
        ):
            await main()

        mock_engine.handle_message.assert_called_once_with(
            user_id="cli",
            text="hello",
            chat_id="cli",
        )

    @pytest.mark.asyncio
    async def test_response_printed(self, mock_engine, mock_config, capsys):
        with (
            patch("leashd.main.inject_global_config_as_env"),
            patch("leashd.main.LeashdConfig", return_value=mock_config),
            patch("leashd.main.build_engine", return_value=mock_engine),
            patch("builtins.input", side_effect=["hello", EOFError]),
        ):
            await main()

        captured = capsys.readouterr()
        assert "response text" in captured.out

    @pytest.mark.asyncio
    async def test_multiple_messages_dispatched(self, mock_engine, mock_config):
        with (
            patch("leashd.main.inject_global_config_as_env"),
            patch("leashd.main.LeashdConfig", return_value=mock_config),
            patch("leashd.main.build_engine", return_value=mock_engine),
            patch("builtins.input", side_effect=["first", "second", EOFError]),
        ):
            await main()

        assert mock_engine.handle_message.call_count == 2
        calls = mock_engine.handle_message.call_args_list
        assert calls[0].kwargs["text"] == "first"
        assert calls[1].kwargs["text"] == "second"

    @pytest.mark.asyncio
    async def test_config_error_catches_leashd_error(self):
        """LeashdError caught by narrowed except → exit code 1."""
        from leashd.exceptions import LeashdError

        with (
            patch("leashd.main.inject_global_config_as_env"),
            patch("leashd.main.LeashdConfig", side_effect=LeashdError("bad")),
            pytest.raises(SystemExit) as exc_info,
        ):
            await main()
        assert exc_info.value.code == 1

    @pytest.mark.asyncio
    async def test_config_error_catches_config_error(self, capsys):
        """ConfigError caught → exit code 1, 'Configuration error' in stderr."""
        from leashd.exceptions import ConfigError

        with (
            patch("leashd.main.inject_global_config_as_env"),
            patch("leashd.main.LeashdConfig", side_effect=ConfigError("missing key")),
            pytest.raises(SystemExit) as exc_info,
        ):
            await main()
        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert "Configuration error" in captured.err

    @pytest.mark.asyncio
    async def test_unexpected_exception_not_caught(self):
        """TypeError from LeashdConfig() NOT caught — propagates."""
        with (
            patch("leashd.main.inject_global_config_as_env"),
            patch("leashd.main.LeashdConfig", side_effect=TypeError("unexpected")),
            pytest.raises(TypeError, match="unexpected"),
        ):
            await main()

    def test_run_calls_cli_main(self):
        with patch("leashd.cli.main") as mock_cli_main:
            run()
            mock_cli_main.assert_called_once()


class TestDaemonCleanup:
    @pytest.mark.asyncio
    async def test_daemon_cleanup_called_on_success(self, mock_engine, mock_config):
        """daemon_cleanup() called in finally block on normal CLI exit."""
        with (
            patch("leashd.main.inject_global_config_as_env"),
            patch("leashd.main.LeashdConfig", return_value=mock_config),
            patch("leashd.main.build_engine", return_value=mock_engine),
            patch("builtins.input", side_effect=EOFError),
            patch("leashd.daemon.cleanup") as mock_cleanup,
        ):
            await main()

        mock_cleanup.assert_called_once()

    @pytest.mark.asyncio
    async def test_daemon_cleanup_called_on_connector_error(
        self, mock_engine, mock_config
    ):
        """daemon_cleanup() called even when ConnectorError triggers sys.exit(1)."""
        mock_config.telegram_bot_token = "fake:token"

        mock_connector = AsyncMock()
        mock_connector.start.side_effect = ConnectorError("network down")

        with (
            patch("leashd.main.inject_global_config_as_env"),
            patch("leashd.main.LeashdConfig", return_value=mock_config),
            patch("leashd.main.build_engine", return_value=mock_engine),
            patch(
                "leashd.connectors.telegram.TelegramConnector",
                return_value=mock_connector,
            ),
            patch("leashd.daemon.cleanup") as mock_cleanup,
            pytest.raises(SystemExit) as exc_info,
        ):
            await main()

        assert exc_info.value.code == 1
        mock_cleanup.assert_called_once()


class TestTelegramMode:
    @pytest.mark.asyncio
    async def test_telegram_mode_starts_connector(self, mock_engine):
        cfg = MagicMock()
        cfg.approved_directories = ["/tmp"]
        cfg.telegram_bot_token = "fake:token"

        mock_connector = AsyncMock()

        # Pre-set stop event so _run_telegram returns immediately
        stop_event = asyncio.Event()
        stop_event.set()

        with (
            patch("leashd.main.inject_global_config_as_env"),
            patch("leashd.main.LeashdConfig", return_value=cfg),
            patch("leashd.main.build_engine", return_value=mock_engine),
            patch(
                "leashd.connectors.telegram.TelegramConnector",
                return_value=mock_connector,
            ),
            patch("leashd.main.asyncio.Event", return_value=stop_event),
            patch("leashd.main.asyncio.get_running_loop") as mock_loop,
        ):
            mock_loop.return_value.add_signal_handler = MagicMock()
            await main()

        mock_connector.start.assert_awaited_once()
        mock_connector.stop.assert_awaited_once()
        mock_engine.startup.assert_awaited_once()
        mock_engine.shutdown.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_connector_start_failure_calls_engine_shutdown(self, mock_engine):
        cfg = MagicMock()
        cfg.approved_directories = ["/tmp"]
        cfg.telegram_bot_token = "fake:token"

        mock_connector = AsyncMock()
        mock_connector.start.side_effect = ConnectorError("network down")

        with (
            patch("leashd.main.inject_global_config_as_env"),
            patch("leashd.main.LeashdConfig", return_value=cfg),
            patch("leashd.main.build_engine", return_value=mock_engine),
            patch(
                "leashd.connectors.telegram.TelegramConnector",
                return_value=mock_connector,
            ),
        ):
            with pytest.raises(SystemExit) as exc_info:
                await main()
            assert exc_info.value.code == 1

        mock_engine.startup.assert_awaited_once()
        mock_engine.shutdown.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_connector_error_clean_exit(self, mock_engine, capsys):
        cfg = MagicMock()
        cfg.approved_directories = ["/tmp"]
        cfg.telegram_bot_token = "fake:token"

        mock_connector = AsyncMock()
        mock_connector.start.side_effect = ConnectorError(
            "initialize failed after 5 retries"
        )

        with (
            patch("leashd.main.inject_global_config_as_env"),
            patch("leashd.main.LeashdConfig", return_value=cfg),
            patch("leashd.main.build_engine", return_value=mock_engine),
            patch(
                "leashd.connectors.telegram.TelegramConnector",
                return_value=mock_connector,
            ),
        ):
            with pytest.raises(SystemExit) as exc_info:
                await main()
            assert exc_info.value.code == 1

        captured = capsys.readouterr()
        assert "Connector failed" in captured.err
