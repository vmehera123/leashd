"""CLI entry point for leashd."""

from __future__ import annotations

import asyncio
import os
import signal
import sys
from typing import TYPE_CHECKING

import structlog

from leashd.app import build_engine
from leashd.config_store import inject_global_config_as_env
from leashd.core.config import LeashdConfig
from leashd.exceptions import ConfigError, ConnectorError, LeashdError

if TYPE_CHECKING:
    from leashd.storage.base import MessageStore

logger = structlog.get_logger()


async def _create_message_store(config: LeashdConfig) -> MessageStore | None:
    """Create and initialize a shared message store for the WebUI REST router."""
    if config.storage_backend != "sqlite":
        return None
    from pathlib import Path

    from leashd.storage.sqlite import SqliteSessionStore

    global_dir = Path.home() / ".leashd"
    global_dir.mkdir(parents=True, exist_ok=True)
    store = SqliteSessionStore(global_dir / "messages.db")
    await store.setup()
    return store


async def _run_cli(config: LeashdConfig) -> None:
    engine = build_engine(config)
    await engine.startup()

    logger.info(
        "cli_starting",
        working_directories=[str(d) for d in config.approved_directories],
    )
    print(f"leashd ready — working in {config.approved_directories}")
    print("Enter a prompt (Ctrl+D to exit):\n")

    try:
        while True:
            try:
                prompt = input("> ")
            except EOFError:
                break

            if not prompt.strip():
                continue

            print("\nProcessing...\n")
            response = await engine.handle_message(
                user_id="cli",
                text=prompt,
                chat_id="cli",
            )
            print(f"\n{response}\n")
    except KeyboardInterrupt:
        pass
    finally:
        logger.info("cli_shutting_down")
        await engine.shutdown()
        print("\nShutdown complete.")


async def _run_telegram(config: LeashdConfig) -> None:
    from leashd.connectors.telegram import TelegramConnector

    connector = TelegramConnector(config.telegram_bot_token)  # type: ignore[arg-type]
    engine = build_engine(config, connector=connector)
    await engine.startup()
    try:
        await connector.start()
    except Exception:
        logger.error("telegram_startup_failed")
        await engine.shutdown()
        raise

    logger.info(
        "telegram_starting",
        working_directories=[str(d) for d in config.approved_directories],
    )
    print(f"leashd ready via Telegram — working in {config.approved_directories}")

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop_event.set)

    if hasattr(signal, "SIGHUP"):
        _reload_tasks: set[asyncio.Task[None]] = set()

        def _schedule_reload() -> None:
            task = asyncio.ensure_future(engine.reload_config())
            _reload_tasks.add(task)
            task.add_done_callback(_reload_tasks.discard)

        loop.add_signal_handler(signal.SIGHUP, _schedule_reload)

    try:
        await stop_event.wait()
    finally:
        logger.info("telegram_shutting_down")
        await connector.stop()
        await engine.shutdown()
        print("\nShutdown complete.")


async def _run_web(config: LeashdConfig) -> None:
    from leashd.connectors.web import WebConnector

    message_store = await _create_message_store(config)
    connector = WebConnector(config, message_store=message_store)
    engine = build_engine(config, connector=connector, message_store=message_store)
    await engine.startup()

    logger.info(
        "webui_starting",
        url=f"http://{config.web_host}:{config.web_port}",
        working_directories=[str(d) for d in config.approved_directories],
    )
    print(f"leashd ready via WebUI — http://{config.web_host}:{config.web_port}")

    try:
        await connector.start()
    except Exception:
        logger.error("webui_startup_failed")
        await engine.shutdown()
        raise

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop_event.set)

    if hasattr(signal, "SIGHUP"):
        _reload_tasks: set[asyncio.Task[None]] = set()

        def _schedule_reload() -> None:
            task = asyncio.ensure_future(engine.reload_config())
            _reload_tasks.add(task)
            task.add_done_callback(_reload_tasks.discard)

        loop.add_signal_handler(signal.SIGHUP, _schedule_reload)

    try:
        await stop_event.wait()
    finally:
        logger.info("webui_shutting_down")
        await connector.stop()
        await engine.shutdown()
        print("\nShutdown complete.")


async def _run_multi(config: LeashdConfig) -> None:
    from leashd.connectors.multi import MultiConnector
    from leashd.connectors.telegram import TelegramConnector
    from leashd.connectors.web import WebConnector

    message_store = await _create_message_store(config)
    telegram_connector = TelegramConnector(config.telegram_bot_token)  # type: ignore[arg-type]
    web_connector = WebConnector(config, message_store=message_store)

    multi = MultiConnector([telegram_connector, web_connector])

    # Auto-register web routes when WebSocket connections open/close
    web_connector._on_connect = lambda cid: multi.register_route(cid, web_connector)
    web_connector._on_disconnect = lambda cid: multi.unregister_route(cid)

    engine = build_engine(config, connector=multi, message_store=message_store)
    await engine.startup()

    logger.info(
        "multi_connector_starting",
        telegram=True,
        webui=f"http://{config.web_host}:{config.web_port}",
        working_directories=[str(d) for d in config.approved_directories],
    )
    print(
        f"leashd ready via Telegram + WebUI — "
        f"http://{config.web_host}:{config.web_port}"
    )

    try:
        await multi.start()
    except Exception:
        logger.error("multi_connector_startup_failed")
        await engine.shutdown()
        raise

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop_event.set)

    if hasattr(signal, "SIGHUP"):
        _reload_tasks: set[asyncio.Task[None]] = set()

        def _schedule_reload() -> None:
            task = asyncio.ensure_future(engine.reload_config())
            _reload_tasks.add(task)
            task.add_done_callback(_reload_tasks.discard)

        loop.add_signal_handler(signal.SIGHUP, _schedule_reload)

    try:
        await stop_event.wait()
    finally:
        logger.info("multi_connector_shutting_down")
        await multi.stop()
        await engine.shutdown()
        print("\nShutdown complete.")


async def _main() -> None:
    from leashd.daemon import cleanup as daemon_cleanup

    # leashd spawns Claude Code as subprocesses — it must never be
    # treated as a nested Claude Code session, even when started from
    # a Claude Code terminal.
    os.environ.pop("CLAUDECODE", None)

    inject_global_config_as_env()

    try:
        config = LeashdConfig()  # type: ignore[call-arg]  # pydantic-settings loads from env
    except (ConfigError, LeashdError, ValueError) as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        print(
            "Run 'leashd init' or set LEASHD_APPROVED_DIRECTORIES.",
            file=sys.stderr,
        )
        sys.exit(1)

    has_telegram = bool(config.telegram_bot_token)
    has_web = config.web_enabled

    try:
        if has_telegram and has_web:
            await _run_multi(config)
        elif has_telegram:
            await _run_telegram(config)
        elif has_web:
            await _run_web(config)
        else:
            await _run_cli(config)
    except ConnectorError as e:
        print(f"Connector failed: {e}", file=sys.stderr)
        sys.exit(1)
    finally:
        daemon_cleanup()


def start() -> None:
    """Start the engine — called by cli.py after smart-start checks."""
    asyncio.run(_main())


def run() -> None:
    """Entry point registered in pyproject.toml. Delegates to CLI router."""
    from leashd.cli import main as cli_main

    cli_main()
