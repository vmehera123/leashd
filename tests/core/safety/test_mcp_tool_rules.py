"""Custom policies for MCP servers and deferred tools.

A research agent run under a deny-by-default policy could use none of the
tools that policy allowed. Claude Code 2.1.274 defers ``WebFetch``,
``WebSearch`` and every MCP tool until ``ToolSearch`` loads their schemas, and
the policy did not name ToolSearch. Once it did, every leadline read was still
refused: the policy named ``mcp__leadline__get_opportunity``, the name Claude
Code uses, and leashd only ever compared the bare ``get_opportunity``.
"""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from leashd.agents.runtimes.tmux_session import native_ask_rules
from leashd.core.events import EventBus
from leashd.core.safety.gatekeeper import ToolGatekeeper
from leashd.core.safety.policy import PolicyDecision, PolicyEngine

POLICIES = Path(__file__).parent.parent.parent.parent / "leashd" / "policies"

RESEARCH_POLICY = """
version: "1.0"
name: research
settings:
  default_action: deny
rules:
  - name: web-read
    tools: [WebSearch, WebFetch]
    action: allow
  - name: leadline-write
    tools: [mcp__leadline__create_opportunity]
    action: require_approval
  - name: leadline-read
    tools: [mcp__leadline__get_opportunity, mcp__leadline__list_prospects]
    action: allow
"""


def write_policy(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "policy.yaml"
    path.write_text(text)
    return path


@pytest.fixture
def research(tmp_path):
    return PolicyEngine([write_policy(tmp_path, RESEARCH_POLICY)])


def verdict(engine: PolicyEngine, tool: str, tool_input=None) -> PolicyDecision:
    return engine.evaluate(engine.classify_compound(tool, tool_input or {}))


class TestFullMcpNames:
    @pytest.mark.parametrize(
        "tool", ["mcp__leadline__get_opportunity", "mcp__leadline__list_prospects"]
    )
    def test_full_name_rule_matches_its_tool(self, research, tool):
        assert verdict(research, tool) == PolicyDecision.ALLOW

    def test_full_name_rule_is_scoped_to_its_server(self, research):
        assert verdict(research, "mcp__other__get_opportunity") == PolicyDecision.DENY
        assert verdict(research, "get_opportunity") == PolicyDecision.DENY

    def test_unlisted_tool_on_the_same_server_falls_to_default(self, research):
        assert (
            verdict(research, "mcp__leadline__decide_suggestion") == PolicyDecision.DENY
        )

    def test_full_name_gate_rule_applies(self, research):
        classification = research.classify("mcp__leadline__create_opportunity", {})
        assert classification.matched_rule.name == "leadline-write"
        assert research.evaluate(classification) == PolicyDecision.REQUIRE_APPROVAL

    def test_bare_name_rule_still_matches_any_server(self, tmp_path):
        engine = PolicyEngine(
            [
                write_policy(
                    tmp_path,
                    """
rules:
  - name: graph-read
    tools: [search_graph]
    action: allow
settings:
  default_action: deny
""",
                )
            ]
        )
        assert (
            verdict(engine, "mcp__codebase-memory-mcp__search_graph")
            == PolicyDecision.ALLOW
        )
        assert verdict(engine, "mcp__other__search_graph") == PolicyDecision.ALLOW

    def test_shipped_credential_floor_still_covers_mcp_file_tools(self):
        engine = PolicyEngine([POLICIES / "default.yaml"])
        assert (
            verdict(engine, "mcp__fs__Read", {"file_path": "/repo/.env"})
            == PolicyDecision.DENY
        )

    def test_mcp_tool_named_bash_is_not_a_shell_command(self):
        engine = PolicyEngine([POLICIES / "default.yaml"])
        classification = engine.classify_compound(
            "mcp__remote__Bash", {"command": "ls"}
        )
        assert classification.matched_rule is None

    def test_native_ask_mirrors_the_full_name(self, research):
        rules, names = native_ask_rules(research.rules)
        assert "mcp__leadline__create_opportunity" in rules
        assert names == ["leadline-write"]


class TestToolSearch:
    def test_allowed_when_no_rule_names_it(self, research):
        classification = research.classify("ToolSearch", {"query": "select:WebFetch"})
        assert research.evaluate(classification) == PolicyDecision.ALLOW
        assert classification.matched_rule.name == "tool-search"

    def test_allowed_with_no_policy_rules(self):
        assert verdict(PolicyEngine(), "ToolSearch") == PolicyDecision.ALLOW

    def test_a_rule_naming_it_wins(self, tmp_path):
        engine = PolicyEngine(
            [
                write_policy(
                    tmp_path,
                    """
rules:
  - name: no-deferred-tools
    tools: [ToolSearch]
    action: deny
""",
                )
            ]
        )
        assert verdict(engine, "ToolSearch") == PolicyDecision.DENY

    @pytest.mark.parametrize(
        "policy", ["default.yaml", "strict.yaml", "permissive.yaml", "autonomous.yaml"]
    )
    def test_shipped_policies_keep_their_own_rule(self, policy):
        engine = PolicyEngine([POLICIES / policy])
        classification = engine.classify("ToolSearch", {"query": "select:Read"})
        assert classification.matched_rule.name != "tool-search"
        assert engine.evaluate(classification) == PolicyDecision.ALLOW

    def test_not_mirrored_into_native_ask(self, research):
        rules, _ = native_ask_rules(research.rules)
        assert "ToolSearch" not in rules


class TestGatekeeper:
    @pytest.fixture
    def gatekeeper(self, tmp_path, research):
        from leashd.core.safety.sandbox import SandboxEnforcer

        return ToolGatekeeper(
            sandbox=SandboxEnforcer([tmp_path]),
            audit=MagicMock(),
            event_bus=EventBus(),
            policy_engine=research,
        )

    async def test_research_run_reaches_its_tools(self, gatekeeper):
        for tool, tool_input in [
            ("ToolSearch", {"query": "select:WebFetch,mcp__leadline__get_opportunity"}),
            ("WebFetch", {"url": "https://example.com"}),
            ("mcp__leadline__get_opportunity", {"opportunity": "farming"}),
        ]:
            result = await gatekeeper.check(tool, tool_input, "s1", "c1")
            assert result.behavior == "allow", tool

    async def test_other_server_is_refused(self, gatekeeper):
        result = await gatekeeper.check("mcp__other__get_opportunity", {}, "s1", "c1")
        assert result.behavior == "deny"

    async def test_audit_records_the_matched_full_name_rule(self, gatekeeper):
        await gatekeeper.check("mcp__leadline__list_prospects", {}, "s1", "c1")
        classification = gatekeeper._audit.log_tool_attempt.call_args[0][3]
        assert classification.matched_rule.name == "leadline-read"
