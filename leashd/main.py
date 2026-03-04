"""CLI entry point for leashd."""

import asyncio
import signal
import sys

import structlog

from leashd.app import build_engine
from leashd.config_store import inject_global_config_as_env
from leashd.core.config import LeashdConfig
from leashd.exceptions import ConfigError, ConnectorError, LeashdError

logger = structlog.get_logger()


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

    try:
        await stop_event.wait()
    finally:
        logger.info("telegram_shutting_down")
        await connector.stop()
        await engine.shutdown()
        print("\nShutdown complete.")


async def _main() -> None:
    from leashd.daemon import cleanup as daemon_cleanup

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

    try:
        if config.telegram_bot_token:
            await _run_telegram(config)
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
