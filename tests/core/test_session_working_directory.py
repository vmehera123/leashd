"""SessionManager.working_directory_of, which names the project on a card."""

from leashd.core.session import SessionManager


async def test_a_live_conversation_reports_its_working_directory():
    sessions = SessionManager()
    await sessions.get_or_create("u1", "284184690:s2", "/w/protostar")

    assert sessions.working_directory_of("284184690:s2") == "/w/protostar"
    assert sessions.working_directory_of("284184690") is None


async def test_a_deactivated_conversation_reports_none():
    sessions = SessionManager()
    await sessions.get_or_create("u1", "c1", "/w/a")
    await sessions.deactivate("u1", "c1")

    assert sessions.working_directory_of("c1") is None
