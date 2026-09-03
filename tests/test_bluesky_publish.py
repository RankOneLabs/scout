"""Tests for BlueskyScanner.publish() — auth, createRecord, error mapping."""

from __future__ import annotations

import json
from collections.abc import Callable

import httpx
import pytest
from httpx import MockTransport, Request, Response

import scout.platforms.bluesky as bluesky_client
from scout.platforms.bluesky import BlueskyBatchPublisher, BlueskyScanner, build_facets
from scout.result import Err, Ok


def _make_client(handler: Callable[[Request], Response]) -> httpx.AsyncClient:
    """httpx client with a MockTransport that routes via the given handler."""
    return httpx.AsyncClient(transport=MockTransport(handler))


def _route(
    routes: list[tuple[str, Callable[[Request], Response]]],
) -> Callable[[Request], Response]:
    """Build a handler that matches the first route whose substring is in the URL."""

    def handler(request: Request) -> Response:
        for needle, factory in routes:
            if needle in str(request.url):
                return factory(request)
        return Response(404, json={"error": f"unrouted: {request.url}"})

    return handler


@pytest.fixture(autouse=True)
def _set_creds(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default to populated creds; individual tests can override or clear."""
    monkeypatch.setattr(bluesky_client, "BLUESKY_IDENTIFIER", "scout.test")
    monkeypatch.setattr(bluesky_client, "BLUESKY_APP_PASSWORD", "app-pw")
    monkeypatch.setattr(bluesky_client, "BLUESKY_API_URL", "https://bsky.test")


def _ok_session(_request: Request) -> Response:
    return Response(
        200,
        json={
            "accessJwt": "TOK",
            "did": "did:plc:abc123",
            "handle": "scout.bsky.social",
        },
    )


def _ok_create_record(_request: Request) -> Response:
    return Response(
        200,
        json={
            "uri": "at://did:plc:abc123/app.bsky.feed.post/3jxh4abcd",
            "cid": "bafyrei...",
        },
    )


class TestPublishHappyPath:
    async def test_returns_ok_with_at_uri_and_reconstructed_url(self) -> None:
        client = _make_client(
            _route(
                [
                    ("createSession", _ok_session),
                    ("createRecord", _ok_create_record),
                ]
            )
        )
        async with client:
            scanner = BlueskyScanner()
            result = await scanner._publish_with_client(client, "agents need audit trails.")

        match result:
            case Ok(post):
                assert post.platform == "bluesky"
                assert post.platform_post_id == "at://did:plc:abc123/app.bsky.feed.post/3jxh4abcd"
                assert post.url == "https://bsky.app/profile/scout.bsky.social/post/3jxh4abcd"
            case Err(err):
                pytest.fail(f"expected Ok, got Err: {err}")

    async def test_request_carries_bearer_token_and_repo_did(self) -> None:
        captured_auth: str | None = None
        captured_body: bytes = b""

        def capture_create_record(request: Request) -> Response:
            nonlocal captured_auth, captured_body
            captured_auth = request.headers.get("authorization")
            captured_body = request.read()
            return _ok_create_record(request)

        client = _make_client(
            _route(
                [
                    ("createSession", _ok_session),
                    ("createRecord", capture_create_record),
                ]
            )
        )
        async with client:
            scanner = BlueskyScanner()
            await scanner._publish_with_client(client, "hello bluesky")

        assert captured_auth == "Bearer TOK"
        body_text = captured_body.decode()
        assert "did:plc:abc123" in body_text
        assert "hello bluesky" in body_text
        assert "app.bsky.feed.post" in body_text


class TestPublishErrorPaths:
    async def test_missing_creds_returns_network_error_without_http_call(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(bluesky_client, "BLUESKY_IDENTIFIER", "")

        called = {"n": 0}

        def handler(request: Request) -> Response:
            called["n"] += 1
            return Response(200)

        client = _make_client(handler)
        async with client:
            scanner = BlueskyScanner()
            result = await scanner._publish_with_client(client, "x")

        match result:
            case Err(err):
                assert err.url.endswith("/com.atproto.server.createSession")
                assert "BLUESKY_IDENTIFIER" in err.detail
            case _:
                pytest.fail("expected Err on missing creds")
        assert called["n"] == 0  # never made an HTTP call

    async def test_auth_401_preserves_status(self) -> None:
        client = _make_client(
            _route([("createSession", lambda _r: Response(401, json={"error": "bad creds"}))])
        )
        async with client:
            scanner = BlueskyScanner()
            result = await scanner._publish_with_client(client, "x")

        match result:
            case Err(err):
                assert "createSession" in err.url
                assert err.status == 401  # preserved from HTTP response
                assert "bad creds" in err.detail
            case _:
                pytest.fail("expected Err on auth 401")

    async def test_publish_4xx_returns_network_error_with_status_and_body(self) -> None:
        client = _make_client(
            _route(
                [
                    ("createSession", _ok_session),
                    (
                        "createRecord",
                        lambda _r: Response(
                            400, json={"error": "InvalidRequest", "message": "text too long"}
                        ),
                    ),
                ]
            )
        )
        async with client:
            scanner = BlueskyScanner()
            result = await scanner._publish_with_client(client, "x" * 500)

        match result:
            case Err(err):
                assert err.status == 400
                assert "text too long" in err.detail
            case _:
                pytest.fail("expected Err on 400")

    async def test_non_json_response_includes_body_in_detail(self) -> None:
        # Bluesky returns 2xx HTML (e.g., maintenance page); resp.json() raises.
        client = _make_client(
            _route(
                [
                    ("createSession", _ok_session),
                    (
                        "createRecord",
                        lambda _r: Response(
                            200,
                            content=b"<html>maintenance</html>",
                            headers={"content-type": "text/html"},
                        ),
                    ),
                ]
            )
        )
        async with client:
            scanner = BlueskyScanner()
            result = await scanner._publish_with_client(client, "x")

        match result:
            case Err(err):
                assert err.status == 200
                assert "non-JSON" in err.detail
                assert "maintenance" in err.detail
            case _:
                pytest.fail("expected Err on non-JSON 2xx")

    async def test_response_missing_uri_returns_network_error(self) -> None:
        client = _make_client(
            _route(
                [
                    ("createSession", _ok_session),
                    ("createRecord", lambda _r: Response(200, json={"cid": "x"})),
                ]
            )
        )
        async with client:
            scanner = BlueskyScanner()
            result = await scanner._publish_with_client(client, "x")

        match result:
            case Err(err):
                assert "missing 'uri'" in err.detail
            case _:
                pytest.fail("expected Err on missing uri")


# ---- build_facets unit tests ----


class TestBuildFacets:
    def test_url_facet_correct_byte_offsets(self) -> None:
        text = "Check https://example.com out"
        facets = build_facets(text)
        assert len(facets) == 1
        f = facets[0]
        assert f["features"][0]["$type"] == "app.bsky.richtext.facet#link"
        assert f["features"][0]["uri"] == "https://example.com"
        expected_start = len(b"Check ")
        expected_end = len(b"Check https://example.com")
        assert f["index"]["byteStart"] == expected_start
        assert f["index"]["byteEnd"] == expected_end

    def test_unicode_before_url_correct_byte_offsets(self) -> None:
        # 'café' encodes to 5 bytes (UTF-8), not 4 code points
        text = "café https://example.com"
        facets = build_facets(text)
        assert len(facets) == 1
        f = facets[0]
        expected_start = len("café ".encode())  # 5 + 1 = 6
        expected_end = len("café https://example.com".encode())
        assert f["index"]["byteStart"] == expected_start
        assert f["index"]["byteEnd"] == expected_end
        assert f["features"][0]["uri"] == "https://example.com"

    def test_trailing_period_stripped_from_url(self) -> None:
        text = "See https://example.com."
        facets = build_facets(text)
        assert len(facets) == 1
        assert facets[0]["features"][0]["uri"] == "https://example.com"
        expected_end = len(b"See https://example.com")
        assert facets[0]["index"]["byteEnd"] == expected_end

    def test_trailing_closing_paren_stripped_from_url(self) -> None:
        text = "(see https://example.com)"
        facets = build_facets(text)
        assert len(facets) == 1
        assert facets[0]["features"][0]["uri"] == "https://example.com"

    def test_hashtag_facet(self) -> None:
        text = "Thoughts on #AI today"
        facets = build_facets(text)
        assert len(facets) == 1
        f = facets[0]
        assert f["features"][0]["$type"] == "app.bsky.richtext.facet#tag"
        assert f["features"][0]["tag"] == "AI"
        expected_start = len(b"Thoughts on ")
        expected_end = len(b"Thoughts on #AI")
        assert f["index"]["byteStart"] == expected_start
        assert f["index"]["byteEnd"] == expected_end

    def test_mention_facet_emitted_when_did_resolved(self) -> None:
        text = "Hello @alice.bsky.social how are you"
        did_map = {"alice.bsky.social": "did:plc:alicedid123"}
        facets = build_facets(text, did_map=did_map)
        mention_facets = [
            f for f in facets
            if f["features"][0]["$type"] == "app.bsky.richtext.facet#mention"
        ]
        assert len(mention_facets) == 1
        f = mention_facets[0]
        assert f["features"][0]["did"] == "did:plc:alicedid123"
        expected_start = len(b"Hello ")
        expected_end = len(b"Hello @alice.bsky.social")
        assert f["index"]["byteStart"] == expected_start
        assert f["index"]["byteEnd"] == expected_end

    def test_mention_without_dot_is_plain_text(self) -> None:
        text = "Hello @alice today"
        facets = build_facets(text, did_map={"alice": "did:plc:x"})
        assert facets == []

    def test_unresolved_mention_stays_plain_text(self) -> None:
        text = "Hello @alice.bsky.social today"
        facets = build_facets(text, did_map={})
        assert facets == []

    def test_hashtag_inside_url_suppressed_by_overlap(self) -> None:
        # The #section inside the URL must NOT become a separate hashtag facet.
        text = "See https://example.com/#section for details"
        facets = build_facets(text)
        tag_facets = [
            f for f in facets
            if f["features"][0]["$type"] == "app.bsky.richtext.facet#tag"
        ]
        assert tag_facets == []
        link_facets = [
            f for f in facets
            if f["features"][0]["$type"] == "app.bsky.richtext.facet#link"
        ]
        assert len(link_facets) == 1

    def test_multiple_facets_sorted_by_byte_start(self) -> None:
        text = "#one and #two and https://x.com"
        facets = build_facets(text)
        starts = [f["index"]["byteStart"] for f in facets]
        assert starts == sorted(starts)

    def test_no_facets_on_plain_text(self) -> None:
        assert build_facets("just a plain sentence") == []

    def test_http_only_no_ftps(self) -> None:
        text = "ftp://example.com is not a link"
        assert build_facets(text) == []


class TestPublishIncludesFacets:
    """createRecord payload includes facets when text contains rich-text tokens."""

    async def test_url_in_text_sends_facet_in_record(self) -> None:
        captured_body: bytes = b""

        def capture_create_record(request: Request) -> Response:
            nonlocal captured_body
            captured_body = request.read()
            return _ok_create_record(request)

        client = _make_client(
            _route([
                ("createSession", _ok_session),
                ("createRecord", capture_create_record),
            ])
        )
        async with client:
            scanner = BlueskyScanner()
            await scanner._publish_with_client(client, "See https://example.com today")

        body = json.loads(captured_body)
        record = body["record"]
        assert "facets" in record
        facets = record["facets"]
        assert len(facets) == 1
        assert facets[0]["features"][0]["$type"] == "app.bsky.richtext.facet#link"
        assert facets[0]["features"][0]["uri"] == "https://example.com"

    async def test_plain_text_sends_no_facets_key(self) -> None:
        captured_body: bytes = b""

        def capture(request: Request) -> Response:
            nonlocal captured_body
            captured_body = request.read()
            return _ok_create_record(request)

        client = _make_client(
            _route([("createSession", _ok_session), ("createRecord", capture)])
        )
        async with client:
            scanner = BlueskyScanner()
            await scanner._publish_with_client(client, "plain text no links")

        body = json.loads(captured_body)
        assert "facets" not in body["record"]

    async def test_idempotency_key_sets_deterministic_rkey(self) -> None:
        captured_body: bytes = b""

        def capture(request: Request) -> Response:
            nonlocal captured_body
            captured_body = request.read()
            return _ok_create_record(request)

        client = _make_client(
            _route([("createSession", _ok_session), ("createRecord", capture)])
        )
        async with client:
            scanner = BlueskyScanner()
            await scanner._publish_with_client(
                client,
                "plain text",
                idempotency_key="scout:1:bluesky",
            )

        body = json.loads(captured_body)
        assert body["rkey"] == bluesky_client._rkey_from_idempotency_key(
            "scout:1:bluesky"
        )

    async def test_configured_languages_are_sent_as_langs(self) -> None:
        captured_body: bytes = b""

        def capture(request: Request) -> Response:
            nonlocal captured_body
            captured_body = request.read()
            return _ok_create_record(request)

        client = _make_client(
            _route([("createSession", _ok_session), ("createRecord", capture)])
        )
        async with client:
            scanner = BlueskyScanner(languages=("en", "ES"))
            await scanner._publish_with_client(client, "plain text")

        body = json.loads(captured_body)
        assert body["record"]["langs"] == ["en", "es"]


# ---- BlueskyBatchPublisher session-reuse tests ----


def _batch_driver(
    pub: BlueskyBatchPublisher,
    scanner: BlueskyScanner,
    client: httpx.AsyncClient,
):
    """Return an async callable that drives BlueskyBatchPublisher logic via a test client.

    BlueskyBatchPublisher.publish() creates its own httpx.AsyncClient internally.
    For unit tests we replicate the same logic using the provided mock client so
    the mock transport intercepts all HTTP calls.
    """
    async def _publish(text: str, idempotency_key: str | None = None) -> object:
        if pub._session is None:
            match await scanner._create_session(client):
                case Ok(session):
                    pub._session = session
                case Err(net_err):
                    return Err(net_err)
        res = await scanner._publish_with_session(
            client,
            pub._session,
            text,
            idempotency_key=idempotency_key,
        )
        if isinstance(res, Err) and res.error.status in (401, 403):
            match await scanner._create_session(client):
                case Ok(fresh):
                    pub._session = fresh
                    res = await scanner._publish_with_session(
                        client,
                        fresh,
                        text,
                        idempotency_key=idempotency_key,
                    )
                case Err(net_err):
                    pub._session = None
                    return Err(net_err)
        return res
    return _publish


class TestBlueskyBatchPublisher:
    async def test_session_reused_across_multiple_publishes(self) -> None:
        """BlueskyBatchPublisher creates one session for multiple posts in a batch."""
        session_calls = {"n": 0}

        def counting_session(request: Request) -> Response:
            session_calls["n"] += 1
            return _ok_session(request)

        client = _make_client(
            _route([
                ("createSession", counting_session),
                ("createRecord", _ok_create_record),
            ])
        )

        scanner = BlueskyScanner()
        pub = BlueskyBatchPublisher(scanner)
        drive = _batch_driver(pub, scanner, client)

        async with client:
            r1 = await drive("first post")
            r2 = await drive("second post")
            r3 = await drive("third post")

        assert session_calls["n"] == 1   # only one createSession across the batch
        assert isinstance(r1, Ok)
        assert isinstance(r2, Ok)
        assert isinstance(r3, Ok)

    async def test_refreshes_session_on_401_from_create_record(self) -> None:
        """On 401 from createRecord, publisher refreshes session and retries once."""
        session_calls = {"n": 0}
        record_calls = {"n": 0}

        def ok_session(request: Request) -> Response:
            session_calls["n"] += 1
            return _ok_session(request)

        def first_401_then_ok(request: Request) -> Response:
            record_calls["n"] += 1
            if record_calls["n"] == 1:
                return Response(401, json={"error": "ExpiredToken"})
            return _ok_create_record(request)

        client = _make_client(
            _route([
                ("createSession", ok_session),
                ("createRecord", first_401_then_ok),
            ])
        )

        scanner = BlueskyScanner()
        pub = BlueskyBatchPublisher(scanner)
        drive = _batch_driver(pub, scanner, client)

        async with client:
            result = await drive("hello world")

        assert isinstance(result, Ok)
        assert session_calls["n"] == 2   # initial + refresh after 401
        assert record_calls["n"] == 2    # first 401 + successful retry

    async def test_persistent_auth_failure_returns_err(self) -> None:
        """When session refresh also fails, Err is returned without further retries."""
        session_calls = {"n": 0}
        record_calls = {"n": 0}

        def ok_session(request: Request) -> Response:
            session_calls["n"] += 1
            return _ok_session(request)

        def always_401(request: Request) -> Response:
            record_calls["n"] += 1
            return Response(401, json={"error": "ExpiredToken"})

        # First session succeeds, but all createRecord calls return 401.
        # The refresh session also succeeds (session endpoint is always up),
        # but createRecord keeps returning 401 — retry only once.
        client = _make_client(
            _route([
                ("createSession", ok_session),
                ("createRecord", always_401),
            ])
        )

        scanner = BlueskyScanner()
        pub = BlueskyBatchPublisher(scanner)
        drive = _batch_driver(pub, scanner, client)

        async with client:
            result = await drive("hello world")

        assert isinstance(result, Err)
        assert result.error.status == 401
        assert record_calls["n"] == 2    # original + one retry

    async def test_scanner_plain_publish_creates_session_each_call(self) -> None:
        """Baseline: plain BlueskyScanner._publish_with_client creates one session per call."""
        session_calls = {"n": 0}

        def counting_session(request: Request) -> Response:
            session_calls["n"] += 1
            return _ok_session(request)

        client = _make_client(
            _route([
                ("createSession", counting_session),
                ("createRecord", _ok_create_record),
            ])
        )
        async with client:
            scanner = BlueskyScanner()
            await scanner._publish_with_client(client, "post one")
            await scanner._publish_with_client(client, "post two")

        assert session_calls["n"] == 2   # one per _publish_with_client call
