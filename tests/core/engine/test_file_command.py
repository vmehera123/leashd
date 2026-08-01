"""Engine tests — /file command and agent-emitted file delivery markers."""

import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from leashd.agents.base import AgentResponse, BaseAgent
from leashd.core.engine import Engine
from leashd.core.session import SessionManager


class MarkerAgent(BaseAgent):
    """Agent whose reply asks leashd to deliver files."""

    def __init__(self, content):
        self._content = content

    async def execute(self, prompt, session, **kwargs):
        return AgentResponse(content=self._content, session_id="s-1", cost=0.0)

    async def cancel(self, session_id):
        pass

    async def shutdown(self):
        pass

    def update_config(self, config):
        pass


@pytest.fixture
def engine_with_connector(config, audit_logger, policy_engine, mock_connector):
    def _build(agent):
        return Engine(
            connector=mock_connector,
            agent=agent,
            config=config,
            session_manager=SessionManager(),
            policy_engine=policy_engine,
            audit=audit_logger,
        )

    return _build


class TestFileCommand:
    async def test_sends_file_silently(
        self, engine_with_connector, mock_connector, tmp_path
    ):
        report = tmp_path / "report.txt"
        report.write_text("all good")
        eng = engine_with_connector(MarkerAgent("noop"))

        result = await eng.handle_command("user1", "file", "report.txt", "chat1")

        assert result == ""
        assert mock_connector.sent_files == [
            {
                "chat_id": "chat1",
                "file_path": str(report.resolve()),
                "caption": "report.txt",
            }
        ]

    async def test_no_args_returns_usage(self, engine_with_connector):
        eng = engine_with_connector(MarkerAgent("noop"))

        result = await eng.handle_command("user1", "file", "", "chat1")

        assert "Usage: /file" in result

    async def test_missing_file_reports_error(
        self, engine_with_connector, mock_connector
    ):
        eng = engine_with_connector(MarkerAgent("noop"))

        result = await eng.handle_command("user1", "file", "nope.txt", "chat1")

        assert "not found" in result
        assert mock_connector.sent_files == []

    async def test_path_outside_sandbox_refused(
        self, engine_with_connector, mock_connector, tmp_path
    ):
        outside = tmp_path.parent / "outside-secret.txt"
        outside.write_text("nope")
        eng = engine_with_connector(MarkerAgent("noop"))

        result = await eng.handle_command("user1", "file", str(outside), "chat1")

        assert "outside the approved directories" in result
        assert mock_connector.sent_files == []

    async def test_multiple_paths_and_quotes(
        self, engine_with_connector, mock_connector, tmp_path
    ):
        (tmp_path / "a.txt").write_text("a")
        (tmp_path / "my report.txt").write_text("b")
        eng = engine_with_connector(MarkerAgent("noop"))

        result = await eng.handle_command(
            "user1", "file", 'a.txt "my report.txt"', "chat1"
        )

        assert result == ""
        assert [f["caption"] for f in mock_connector.sent_files] == [
            "a.txt",
            "my report.txt",
        ]

    async def test_upload_failure_surfaces(
        self, engine_with_connector, mock_connector, tmp_path
    ):
        (tmp_path / "report.txt").write_text("all good")
        mock_connector.send_file_result = False
        eng = engine_with_connector(MarkerAgent("noop"))

        result = await eng.handle_command("user1", "file", "report.txt", "chat1")

        assert "upload failed" in result

    async def test_partial_failure_reports_both(
        self, engine_with_connector, mock_connector, tmp_path
    ):
        (tmp_path / "a.txt").write_text("a")
        eng = engine_with_connector(MarkerAgent("noop"))

        result = await eng.handle_command("user1", "file", "a.txt missing.txt", "chat1")

        assert "Sent 1 file(s)." in result
        assert "not found" in result
        assert len(mock_connector.sent_files) == 1

    async def test_unbalanced_quote_falls_back_to_whitespace_split(
        self, engine_with_connector, mock_connector, tmp_path
    ):
        """``shlex`` refuses an unclosed quote; a typo must not raise."""
        (tmp_path / "a.txt").write_text("a")
        eng = engine_with_connector(MarkerAgent("noop"))

        result = await eng.handle_command("user1", "file", 'a.txt "b.txt', "chat1")

        assert "not found" in result
        assert [f["caption"] for f in mock_connector.sent_files] == ["a.txt"]

    async def test_glob_delivers_every_match(
        self, engine_with_connector, mock_connector, tmp_path
    ):
        logs = tmp_path / ".leashd" / "logs"
        logs.mkdir(parents=True)
        (logs / "app.log").write_text("a")
        (logs / "hook.log").write_text("b")
        eng = engine_with_connector(MarkerAgent("noop"))

        result = await eng.handle_command(
            "user1", "file", ".leashd/logs/*.log", "chat1"
        )

        assert result == ""
        assert [f["caption"] for f in mock_connector.sent_files] == [
            ".leashd/logs/app.log",
            ".leashd/logs/hook.log",
        ]

    async def test_no_connector_reports_failure_without_raising(
        self, config, audit_logger, policy_engine, tmp_path
    ):
        (tmp_path / "report.txt").write_text("hi")
        eng = Engine(
            connector=None,
            agent=MarkerAgent("noop"),
            config=config,
            session_manager=SessionManager(),
            policy_engine=policy_engine,
            audit=audit_logger,
        )

        result = await eng.handle_command("user1", "file", "report.txt", "chat1")

        assert "upload failed" in result


class TestMarkerDelivery:
    async def test_marker_delivers_file_and_is_stripped(
        self, engine_with_connector, mock_connector, tmp_path
    ):
        report = tmp_path / "coverage.html"
        report.write_text("<html>coverage</html>")
        eng = engine_with_connector(
            MarkerAgent("Coverage is 91%.\n\n[[leashd:file coverage.html]]")
        )

        result = await eng.handle_message("user1", "run coverage", "chat1")

        assert result == "Coverage is 91%."
        texts = [m.get("text", "") for m in mock_connector.sent_messages]
        assert all("leashd:file" not in t for t in texts)
        assert mock_connector.sent_files[0]["file_path"] == str(report.resolve())
        assert mock_connector.sent_files[0]["caption"] == "coverage.html"

    async def test_marker_for_credential_file_refused_and_reported(
        self, engine_with_connector, mock_connector, tmp_path
    ):
        (tmp_path / ".env").write_text("TOKEN=secret")
        eng = engine_with_connector(MarkerAgent("Here it is.\n[[leashd:file .env]]"))

        await eng.handle_message("user1", "send me the env", "chat1")

        assert mock_connector.sent_files == []
        texts = [m.get("text", "") for m in mock_connector.sent_messages]
        assert any("credential" in t for t in texts)

    async def test_response_without_marker_delivers_nothing(
        self, engine_with_connector, mock_connector
    ):
        eng = engine_with_connector(MarkerAgent("Just a plain answer."))

        result = await eng.handle_message("user1", "hi", "chat1")

        assert result == "Just a plain answer."
        assert mock_connector.sent_files == []

    async def test_multiple_markers_deliver_in_order(
        self, engine_with_connector, mock_connector, tmp_path
    ):
        (tmp_path / "coverage.html").write_text("<html>cov</html>")
        shots = tmp_path / "shots"
        shots.mkdir()
        (shots / "dashboard.png").write_bytes(b"\x89PNG\r\n\x1a\nfake")
        eng = engine_with_connector(
            MarkerAgent(
                "Run is green.\n\n"
                "[[leashd:file coverage.html]]\n"
                "[[leashd:file shots/dashboard.png]]"
            )
        )

        result = await eng.handle_message("user1", "run the suite", "chat1")

        assert result == "Run is green."
        assert [f["caption"] for f in mock_connector.sent_files] == [
            "coverage.html",
            "shots/dashboard.png",
        ]

    async def test_marker_is_stripped_from_the_persisted_reply(
        self, engine_with_connector, mock_connector, tmp_path
    ):
        (tmp_path / "report.txt").write_text("hi")
        eng = engine_with_connector(
            MarkerAgent("Report attached.\n\n[[leashd:file report.txt]]")
        )
        logged = AsyncMock()
        eng._message_logger.log = logged

        await eng.handle_message("user1", "make a report", "chat1")

        assistant = [
            call.kwargs
            for call in logged.await_args_list
            if call.kwargs.get("role") == "assistant"
        ]
        assert [c["content"] for c in assistant] == ["Report attached."]

    async def test_marker_pointing_outside_the_sandbox_is_reported_to_the_chat(
        self, engine_with_connector, mock_connector, tmp_path
    ):
        outside = tmp_path.parent / "outside-marker.txt"
        outside.write_text("nope")
        eng = engine_with_connector(
            MarkerAgent(f"Here you go.\n[[leashd:file {outside}]]")
        )

        await eng.handle_message("user1", "send it", "chat1")

        assert mock_connector.sent_files == []
        texts = [m.get("text", "") for m in mock_connector.sent_messages]
        assert any("outside the approved directories" in t for t in texts)

    async def test_file_removed_after_upload_audits_without_a_size(
        self, engine_with_connector, mock_connector, audit_logger, tmp_path
    ):
        """The audit write happens after the upload; a build artefact cleaned
        up in between must not break the record."""
        report = tmp_path / "report.txt"
        report.write_text("all good")
        inner = mock_connector.send_file

        async def send_then_clean(chat_id, file_path, *, caption=""):
            result = await inner(chat_id, file_path, caption=caption)
            Path(file_path).unlink()
            return result

        mock_connector.send_file = send_then_clean
        eng = engine_with_connector(MarkerAgent("noop"))

        await eng.handle_command("user1", "file", "report.txt", "chat1")

        entries = [
            json.loads(line)
            for line in audit_logger._path.read_text().splitlines()
            if line.strip()
        ]
        delivery = next(e for e in entries if e["event"] == "file_delivery")
        assert delivery["delivered"] is True
        assert "size" not in delivery


class TestDeliveryAudit:
    """Files leaving the machine belong in the audit trail, not just app.log."""

    def _entries(self, audit_logger):
        path = audit_logger._path
        if not path.exists():
            return []
        return [
            json.loads(line) for line in path.read_text().splitlines() if line.strip()
        ]

    async def test_delivered_file_is_audited(
        self, engine_with_connector, audit_logger, tmp_path
    ):
        report = tmp_path / "report.txt"
        report.write_text("all good")
        eng = engine_with_connector(MarkerAgent("noop"))

        await eng.handle_command("user1", "file", "report.txt", "chat1")

        entries = [
            e for e in self._entries(audit_logger) if e["event"] == "file_delivery"
        ]
        assert len(entries) == 1
        assert entries[0]["delivered"] is True
        assert entries[0]["path"] == str(report.resolve())
        assert entries[0]["size"] == 8
        assert entries[0]["trigger"] == "command"

    async def test_refusal_is_audited_with_reason(
        self, engine_with_connector, audit_logger, tmp_path
    ):
        (tmp_path / ".env").write_text("TOKEN=abc")
        eng = engine_with_connector(MarkerAgent("noop"))

        await eng.handle_command("user1", "file", ".env", "chat1")

        entries = [
            e for e in self._entries(audit_logger) if e["event"] == "file_delivery"
        ]
        assert len(entries) == 1
        assert entries[0]["delivered"] is False
        assert "credential" in entries[0]["reason"]

    async def test_failed_upload_is_audited(
        self, engine_with_connector, mock_connector, audit_logger, tmp_path
    ):
        (tmp_path / "report.txt").write_text("all good")
        mock_connector.send_file_result = False
        eng = engine_with_connector(MarkerAgent("noop"))

        await eng.handle_command("user1", "file", "report.txt", "chat1")

        entries = [
            e for e in self._entries(audit_logger) if e["event"] == "file_delivery"
        ]
        assert entries[0]["delivered"] is False
        assert entries[0]["reason"] == "upload failed"

    async def test_marker_delivery_records_its_trigger(
        self, engine_with_connector, audit_logger, tmp_path
    ):
        (tmp_path / "coverage.html").write_text("<html>ok</html>")
        eng = engine_with_connector(
            MarkerAgent("Coverage is 91%.\n\n[[leashd:file coverage.html]]")
        )

        await eng.handle_message("user1", "run coverage", "chat1")

        entries = [
            e for e in self._entries(audit_logger) if e["event"] == "file_delivery"
        ]
        assert entries[0]["trigger"] == "marker"
        assert entries[0]["delivered"] is True
