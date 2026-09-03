"""Tests for FarcasterScanner.publish() — Neynar cast endpoint."""

from __future__ import annotations

import json
from collections.abc import Callable

import httpx
import pytest
from httpx import MockTransport, Request, Response

import scout.platforms.farcaster as farcaster_client
from scout.platforms.farcaster import FarcasterScanner
from scout.result import Err, Ok
from scout.scanning.schemas import FARCASTER_POST_MAX_BYTES


def _make_client(handler: Callable[[Request], Response]) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=MockTransport(handler))


def _route(
    routes: list[tuple[str, Callable[[Request], Response]]],
) -> Callable[[Request], Response]:
    def handler(request: Request) -> Response:
        for needle, factory in routes:
            if needle in str(request.url):
                return factory(request)
        return Response(404, json={"error": f"unrouted: {request.url}"})

    return handler


@pytest.fixture(autouse=True)
def _set_creds(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default to populated creds; individual tests can override or clear."""
    monkeypatch.setattr(farcaster_client, "NEYNAR_API_URL", "https://api.neynar.test")
    monkeypatch.setattr(farcaster_client, "FARCASTER_SIGNER_UUID", "signer-uuid-123")


def _ok_cast(_request: Request) -> Response:
    return Response(
        200,
        json={
            "success": True,
            "cast": {
                "hash": "0xabcdef0123456789",
                "author": {"username": "scout", "fid": 12345},
                "text": "ignored",
            },
        },
    )


class TestPublishHappyPath:
    async def test_returns_ok_with_cast_hash_and_warpcast_url(self) -> None:
        client = _make_client(_route([("cast", _ok_cast)]))
        async with client:
            scanner = FarcasterScanner(api_key="test-key")
            result = await scanner._publish_with_client(client, "agents need audit trails.")

        match result:
            case Ok(post):
                assert post.platform == "farcaster"
                assert post.platform_post_id == "0xabcdef0123456789"
                assert post.url == "https://warpcast.com/scout/0xabcdef01"
            case Err(err):
                pytest.fail(f"expected Ok, got Err: {err}")

    async def test_request_carries_api_key_and_signer_uuid(self) -> None:
        captured_key: str | None = None
        captured_body: bytes = b""

        def capture(request: Request) -> Response:
            nonlocal captured_key, captured_body
            captured_key = request.headers.get("x-api-key")
            captured_body = request.read()
            return _ok_cast(request)

        client = _make_client(_route([("cast", capture)]))
        async with client:
            scanner = FarcasterScanner(api_key="test-key")
            await scanner._publish_with_client(client, "hello farcaster")

        assert captured_key == "test-key"
        body_text = captured_body.decode()
        assert "signer-uuid-123" in body_text
        assert "hello farcaster" in body_text

    async def test_handles_response_without_author(self) -> None:
        # Neynar may return cast.author = null in some edge cases; URL should be empty.
        def cast_no_author(_r: Request) -> Response:
            return Response(
                200,
                json={"success": True, "cast": {"hash": "0xnoauthor", "author": None}},
            )

        client = _make_client(_route([("cast", cast_no_author)]))
        async with client:
            scanner = FarcasterScanner(api_key="test-key")
            result = await scanner._publish_with_client(client, "x")

        match result:
            case Ok(post):
                assert post.platform_post_id == "0xnoauthor"
                assert post.url == ""
            case _:
                pytest.fail("expected Ok")


class TestPublishErrorPaths:
    async def test_missing_signer_uuid_returns_network_error_without_http_call(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(farcaster_client, "FARCASTER_SIGNER_UUID", "")

        called = {"n": 0}

        def handler(request: Request) -> Response:
            called["n"] += 1
            return Response(200)

        client = _make_client(handler)
        async with client:
            scanner = FarcasterScanner(api_key="test-key")
            result = await scanner._publish_with_client(client, "x")

        match result:
            case Err(err):
                assert "FARCASTER_SIGNER_UUID" in err.detail
                assert err.url.endswith("/cast")
            case _:
                pytest.fail("expected Err on missing signer_uuid")
        assert called["n"] == 0

    async def test_403_preserves_status_and_body(self) -> None:
        client = _make_client(
            _route(
                [
                    (
                        "cast",
                        lambda _r: Response(403, json={"message": "signer not approved"}),
                    )
                ]
            )
        )
        async with client:
            scanner = FarcasterScanner(api_key="test-key")
            result = await scanner._publish_with_client(client, "x")

        match result:
            case Err(err):
                assert err.status == 403
                assert "signer not approved" in err.detail
            case _:
                pytest.fail("expected Err on 403")

    async def test_response_missing_cast_hash_returns_error(self) -> None:
        client = _make_client(
            _route([("cast", lambda _r: Response(200, json={"success": True, "cast": {}}))])
        )
        async with client:
            scanner = FarcasterScanner(api_key="test-key")
            result = await scanner._publish_with_client(client, "x")

        match result:
            case Err(err):
                assert "cast.hash" in err.detail
            case _:
                pytest.fail("expected Err on missing cast.hash")

    async def test_non_json_response_includes_body(self) -> None:
        client = _make_client(
            _route(
                [
                    (
                        "cast",
                        lambda _r: Response(
                            200,
                            content=b"<html>maintenance</html>",
                            headers={"content-type": "text/html"},
                        ),
                    )
                ]
            )
        )
        async with client:
            scanner = FarcasterScanner(api_key="test-key")
            result = await scanner._publish_with_client(client, "x")

        match result:
            case Err(err):
                assert err.status == 200
                assert "non-JSON" in err.detail
                assert "maintenance" in err.detail
            case _:
                pytest.fail("expected Err on non-JSON 2xx")


class TestPublishFarcasterByteLengthPreflight:
    """Byte-length validation runs before any HTTP call is made."""

    async def test_exactly_320_ascii_bytes_is_sent_to_neynar(self) -> None:
        called = {"n": 0}

        def handler(request: Request) -> Response:
            called["n"] += 1
            return _ok_cast(request)

        text = "a" * FARCASTER_POST_MAX_BYTES
        client = _make_client(handler)
        async with client:
            scanner = FarcasterScanner(api_key="test-key")
            result = await scanner._publish_with_client(client, text)

        assert called["n"] == 1
        assert isinstance(result, Ok)

    async def test_idempotency_key_is_sent_as_idem(self) -> None:
        captured_body: bytes = b""

        def handler(request: Request) -> Response:
            nonlocal captured_body
            captured_body = request.read()
            return _ok_cast(request)

        client = _make_client(handler)
        async with client:
            scanner = FarcasterScanner(api_key="test-key")
            result = await scanner._publish_with_client(
                client,
                "hello",
                idempotency_key="scout:1:farcaster",
            )

        assert isinstance(result, Ok)
        assert json.loads(captured_body)["idem"] == "scout:1:farcaster"

    async def test_321_ascii_bytes_fails_before_http_call(self) -> None:
        called = {"n": 0}

        def handler(request: Request) -> Response:
            called["n"] += 1
            return _ok_cast(request)

        text = "a" * (FARCASTER_POST_MAX_BYTES + 1)
        client = _make_client(handler)
        async with client:
            scanner = FarcasterScanner(api_key="test-key")
            result = await scanner._publish_with_client(client, text)

        assert called["n"] == 0
        match result:
            case Err(err):
                assert "321" in err.detail
                assert "UTF-8" in err.detail
            case _:
                pytest.fail("expected Err on oversized text")

    async def test_em_dash_text_at_byte_limit_passes(self) -> None:
        # 105 em dashes (3 bytes each) + 5 ASCII = 320 bytes; 110 code points
        text = "—" * 105 + "aaaaa"
        assert len(text.encode("utf-8")) == 320

        client = _make_client(_route([("cast", _ok_cast)]))
        async with client:
            scanner = FarcasterScanner(api_key="test-key")
            result = await scanner._publish_with_client(client, text)

        assert isinstance(result, Ok)

    async def test_em_dash_text_over_byte_limit_fails(self) -> None:
        # 107 em dashes = 321 bytes but only 107 code points
        text = "—" * 107
        assert len(text) < FARCASTER_POST_MAX_BYTES  # char count under limit
        assert len(text.encode("utf-8")) > FARCASTER_POST_MAX_BYTES  # byte count over

        client = _make_client(_route([("cast", _ok_cast)]))
        async with client:
            scanner = FarcasterScanner(api_key="test-key")
            result = await scanner._publish_with_client(client, text)

        match result:
            case Err(err):
                assert "UTF-8" in err.detail
            case _:
                pytest.fail("expected Err for over-byte-limit em dash text")

    async def test_emoji_at_byte_limit_passes(self) -> None:
        # 80 emoji (4 bytes each) = 320 bytes; 80 code points
        text = "\U0001f600" * 80
        assert len(text.encode("utf-8")) == 320

        client = _make_client(_route([("cast", _ok_cast)]))
        async with client:
            scanner = FarcasterScanner(api_key="test-key")
            result = await scanner._publish_with_client(client, text)

        assert isinstance(result, Ok)

    async def test_mixed_unicode_over_byte_limit_fails(self) -> None:
        # 200 ASCII + 41 em dashes = 323 bytes; 241 code points (well under 320 chars)
        text = "a" * 200 + "—" * 41
        assert len(text) < FARCASTER_POST_MAX_BYTES
        assert len(text.encode("utf-8")) > FARCASTER_POST_MAX_BYTES

        client = _make_client(_route([("cast", _ok_cast)]))
        async with client:
            scanner = FarcasterScanner(api_key="test-key")
            result = await scanner._publish_with_client(client, text)

        match result:
            case Err(err):
                assert "UTF-8" in err.detail
            case _:
                pytest.fail("expected Err for mixed-unicode over-byte-limit text")
