"""Tests for middleware chain, auth, and rate limiting."""

from __future__ import annotations

import pytest

from leashd.middleware.auth import AuthMiddleware
from leashd.middleware.base import MessageContext, MiddlewareChain
from leashd.middleware.rate_limit import RateLimitMiddleware, TokenBucket


def _make_ctx(user_id: str = "user1", text: str = "hello") -> MessageContext:
    return MessageContext(user_id=user_id, chat_id="chat1", text=text)


async def _echo_handler(ctx: MessageContext) -> str:
    return f"Echo: {ctx.text}"


class TestMiddlewareChain:
    async def test_empty_chain_calls_handler(self):
        chain = MiddlewareChain()
        result = await chain.run(_make_ctx(), _echo_handler)
        assert result == "Echo: hello"

    async def test_single_middleware_passthrough(self):
        class PassThrough:
            async def process(self, ctx, call_next):
                ctx.metadata["touched"] = True
                return await call_next(ctx)

        chain = MiddlewareChain()
        chain.add(PassThrough())
        ctx = _make_ctx()
        result = await chain.run(ctx, _echo_handler)
        assert result == "Echo: hello"
        assert ctx.metadata["touched"] is True

    async def test_middleware_can_short_circuit(self):
        class Blocker:
            async def process(self, ctx, call_next):
                return "blocked"

        chain = MiddlewareChain()
        chain.add(Blocker())
        result = await chain.run(_make_ctx(), _echo_handler)
        assert result == "blocked"

    async def test_middleware_order(self):
        calls = []

        class First:
            async def process(self, ctx, call_next):
                calls.append("first_before")
                result = await call_next(ctx)
                calls.append("first_after")
                return result

        class Second:
            async def process(self, ctx, call_next):
                calls.append("second_before")
                result = await call_next(ctx)
                calls.append("second_after")
                return result

        chain = MiddlewareChain()
        chain.add(First())
        chain.add(Second())
        await chain.run(_make_ctx(), _echo_handler)

        assert calls == ["first_before", "second_before", "second_after", "first_after"]


class TestAuthMiddleware:
    async def test_allowed_user_passes(self):
        mw = AuthMiddleware({"user1"})
        result = await mw.process(_make_ctx(user_id="user1"), _echo_handler)
        assert result == "Echo: hello"

    async def test_unauthorized_user_rejected(self):
        mw = AuthMiddleware({"user1"})
        result = await mw.process(_make_ctx(user_id="stranger"), _echo_handler)
        assert "Unauthorized" in result

    async def test_allow_all_bypasses_check(self):
        mw = AuthMiddleware(set(), allow_all=True)
        result = await mw.process(_make_ctx(user_id="anyone"), _echo_handler)
        assert result == "Echo: hello"

    async def test_empty_whitelist_rejects_all(self):
        mw = AuthMiddleware(set())
        result = await mw.process(_make_ctx(user_id="anyone"), _echo_handler)
        assert "Unauthorized" in result


class TestTokenBucket:
    def test_consume_within_burst(self):
        bucket = TokenBucket(rate=1.0, burst=3)
        assert bucket.consume()
        assert bucket.consume()
        assert bucket.consume()
        assert not bucket.consume()

    def test_refill_over_time(self):
        bucket = TokenBucket(rate=10.0, burst=1)
        bucket.consume()
        assert not bucket.consume()
        # Simulate time passing
        bucket._last_refill -= 1.0
        assert bucket.consume()


class TestRateLimitMiddleware:
    async def test_within_limit_passes(self):
        mw = RateLimitMiddleware(requests_per_minute=60, burst=5)
        result = await mw.process(_make_ctx(), _echo_handler)
        assert result == "Echo: hello"

    async def test_exceeds_burst_rejected(self):
        mw = RateLimitMiddleware(requests_per_minute=60, burst=2)
        await mw.process(_make_ctx(), _echo_handler)
        await mw.process(_make_ctx(), _echo_handler)
        result = await mw.process(_make_ctx(), _echo_handler)
        assert "Rate limited" in result

    async def test_per_user_buckets(self):
        mw = RateLimitMiddleware(requests_per_minute=60, burst=1)
        await mw.process(_make_ctx(user_id="u1"), _echo_handler)
        # u1 is now rate-limited, but u2 should still pass
        result = await mw.process(_make_ctx(user_id="u2"), _echo_handler)
        assert result == "Echo: hello"


class TestTokenBucketEdgeCases:
    def test_token_bucket_zero_burst_always_denies(self):
        bucket = TokenBucket(rate=10.0, burst=0)
        assert not bucket.consume()
        assert not bucket.consume()

    def test_token_bucket_refill_caps_at_burst(self):
        bucket = TokenBucket(rate=100.0, burst=3)
        # Simulate a very long time passing
        bucket._last_refill -= 100.0
        bucket._refill()
        # Even after refill, tokens should be capped at burst
        assert bucket._tokens == 3.0


class TestAuthEdgeCases:
    async def test_auth_with_multiple_users(self):
        mw = AuthMiddleware({"alice", "bob", "charlie"})
        for user in ("alice", "bob", "charlie"):
            result = await mw.process(_make_ctx(user_id=user), _echo_handler)
            assert result == "Echo: hello"
        result = await mw.process(_make_ctx(user_id="mallory"), _echo_handler)
        assert "Unauthorized" in result


class TestMiddlewareChainEdgeCases:
    async def test_middleware_exception_propagates(self):
        class Exploder:
            async def process(self, ctx, call_next):
                raise ValueError("boom")

        chain = MiddlewareChain()
        chain.add(Exploder())
        with pytest.raises(ValueError, match="boom"):
            await chain.run(_make_ctx(), _echo_handler)


class TestRateLimitRecovery:
    async def test_rate_limit_recovery_after_wait(self):
        mw = RateLimitMiddleware(requests_per_minute=60, burst=1)
        await mw.process(_make_ctx(), _echo_handler)
        # Exhausted
        result = await mw.process(_make_ctx(), _echo_handler)
        assert "Rate limited" in result
        # Simulate time passing
        bucket = mw._get_bucket("user1")
        bucket._last_refill -= 2.0
        result = await mw.process(_make_ctx(), _echo_handler)
        assert result == "Echo: hello"


class TestAuthBypass:
    """Security bypass attempt vectors for auth middleware."""

    async def test_auth_empty_string_user_id(self):
        mw = AuthMiddleware({"user1"})
        result = await mw.process(_make_ctx(user_id=""), _echo_handler)
        assert "Unauthorized" in result

    async def test_auth_none_string_user_id(self):
        """Literal string 'None' should not be treated as special."""
        mw = AuthMiddleware({"user1"})
        result = await mw.process(_make_ctx(user_id="None"), _echo_handler)
        assert "Unauthorized" in result

    async def test_auth_special_chars_user_id(self):
        mw = AuthMiddleware({"user1"})
        result = await mw.process(_make_ctx(user_id="user;DROP TABLE"), _echo_handler)
        assert "Unauthorized" in result

    async def test_auth_numeric_string_user_id(self):
        """Telegram-style numeric IDs should work when allowlisted."""
        mw = AuthMiddleware({"12345"})
        result = await mw.process(_make_ctx(user_id="12345"), _echo_handler)
        assert result == "Echo: hello"


class TestRateLimitEdgeCases:
    """Edge cases for rate limiter."""

    async def test_rate_limit_zero_rpm(self):
        """rpm=0 → rate=0 tokens/sec; burst tokens work then blocks."""
        mw = RateLimitMiddleware(requests_per_minute=0, burst=1)
        # Burst allows first request
        result = await mw.process(_make_ctx(), _echo_handler)
        assert result == "Echo: hello"
        # No refill at rate=0 → blocked
        result = await mw.process(_make_ctx(), _echo_handler)
        assert "Rate limited" in result

    async def test_rate_limit_high_burst(self):
        """High burst allows many initial requests."""
        mw = RateLimitMiddleware(requests_per_minute=60, burst=100)
        results = []
        for _ in range(100):
            r = await mw.process(_make_ctx(), _echo_handler)
            results.append(r)
        assert all(r == "Echo: hello" for r in results)

    async def test_rate_limit_isolated_buckets(self):
        """3 distinct user_ids get independent buckets."""
        mw = RateLimitMiddleware(requests_per_minute=60, burst=1)
        for uid in ("u1", "u2", "u3"):
            result = await mw.process(_make_ctx(user_id=uid), _echo_handler)
            assert result == "Echo: hello"
        # All exhausted individually
        for uid in ("u1", "u2", "u3"):
            result = await mw.process(_make_ctx(user_id=uid), _echo_handler)
            assert "Rate limited" in result
