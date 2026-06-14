import json
import hashlib
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from pathlib import Path

import httpx
import pytest

import ai_chess.chesscom as chesscom_module
from ai_chess.chesscom import ChessComClient
from ai_chess.errors import AppError, ErrorCode

FIXTURES = Path(__file__).parents[1] / "fixtures"
USER_AGENT = "ai-chess-skills/0.1 (contact: maintainer@example.com)"
JSON_BODY = b'{"games":[{"url":"https://www.chess.com/game/live/1"}]}'
PGN_BODY = (FIXTURES / "single.pgn").read_bytes()
MALFORMED_PGN_BODY = (FIXTURES / "malformed.pgn").read_bytes()


def response_for(
    request: httpx.Request,
    status_code: int = 200,
    *,
    cache_control: str | None = None,
    date: str | None = None,
    age: str | None = None,
) -> httpx.Response:
    cache_headers = (
        {"Cache-Control": cache_control} if cache_control is not None else {}
    )
    if date is not None:
        cache_headers["Date"] = date
    if age is not None:
        cache_headers["Age"] = age
    if request.url.path.endswith("/pgn"):
        return httpx.Response(
            status_code,
            headers={
                "Content-Type": "application/x-chess-pgn",
                "ETag": '"pgn-v1"',
                "Last-Modified": "Sun, 14 Jun 2026 10:00:00 GMT",
                **cache_headers,
            },
            content=PGN_BODY,
        )
    return httpx.Response(
        status_code,
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "ETag": '"json-v1"',
            "Last-Modified": "Sun, 14 Jun 2026 09:00:00 GMT",
            **cache_headers,
        },
        content=JSON_BODY,
    )


def make_client(
    tmp_path: Path,
    handler,
    *,
    sleep=lambda _seconds: None,
    clock=lambda: datetime.now(UTC),
) -> tuple[ChessComClient, httpx.Client]:
    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    return (
        ChessComClient(
            http_client,
            USER_AGENT,
            tmp_path / "cache",
            sleep=sleep,
            clock=clock,
        ),
        http_client,
    )


def test_fetch_writes_original_sources_and_manifest_in_serial_order(
    tmp_path: Path,
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return response_for(request)

    chesscom, http_client = make_client(tmp_path, handler)
    with http_client:
        result = chesscom.fetch_months(
            "Hikaru",
            ["2026-01", "2026-02"],
            tmp_path / "output",
        )

    assert [request.url.path for request in requests] == [
        "/pub/player/hikaru/games/2026/01",
        "/pub/player/hikaru/games/2026/01/pgn",
        "/pub/player/hikaru/games/2026/02",
        "/pub/player/hikaru/games/2026/02/pgn",
    ]
    assert all(request.headers["user-agent"] == USER_AGENT for request in requests)
    assert [path.name for path in result.metadata_paths] == [
        "hikaru-2026-01.provenance.json",
        "hikaru-2026-02.provenance.json",
    ]
    assert [path.name for path in result.raw_json_paths] == [
        "hikaru-2026-01.json",
        "hikaru-2026-02.json",
    ]
    assert [path.name for path in result.pgn_paths] == [
        "hikaru-2026-01.pgn",
        "hikaru-2026-02.pgn",
    ]
    assert result.raw_json_paths[0].read_bytes() == JSON_BODY
    assert result.pgn_paths[0].read_bytes() == PGN_BODY
    assert result.manifest_path.name == "manifest.v1.json"
    assert json.loads(result.manifest_path.read_text()) == result.manifest.to_dict()
    assert result.manifest.source_kind == "chesscom"
    assert result.manifest.source_ref == "hikaru"
    assert [source.game_count for source in result.manifest.files] == [1, 1]
    assert result.manifest.files[0].source_url == (
        "https://api.chess.com/pub/player/hikaru/games/2026/01/pgn"
    )
    assert result.manifest.files[0].etag == '"pgn-v1"'


def test_fetch_rejects_empty_months_before_output_or_network(
    tmp_path: Path,
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return response_for(request)

    output_dir = tmp_path / "output"
    chesscom, http_client = make_client(tmp_path, handler)
    with http_client:
        with pytest.raises(AppError) as caught:
            chesscom.fetch_months("Hikaru", [], output_dir)

    assert caught.value.code == ErrorCode.INVALID_INPUT
    assert "month" in caught.value.remedy.lower()
    assert not output_dir.exists()
    assert requests == []


def test_fetch_revalidates_etag_and_reuses_cached_bodies(
    tmp_path: Path,
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.headers.get("if-none-match"):
            return httpx.Response(304)
        return response_for(request)

    chesscom, http_client = make_client(tmp_path, handler)
    with http_client:
        first = chesscom.fetch_months("Hikaru", ["2026-01"], tmp_path / "output")
        first_json = first.raw_json_paths[0].read_bytes()
        first_pgn = first.pgn_paths[0].read_bytes()
        second = chesscom.fetch_months("Hikaru", ["2026-01"], tmp_path / "output")

    assert len(requests) == 4
    assert requests[2].headers["if-none-match"] == '"json-v1"'
    assert requests[2].headers["if-modified-since"] == (
        "Sun, 14 Jun 2026 09:00:00 GMT"
    )
    assert requests[3].headers["if-none-match"] == '"pgn-v1"'
    assert requests[3].headers["if-modified-since"] == (
        "Sun, 14 Jun 2026 10:00:00 GMT"
    )
    assert second.metadata_paths == first.metadata_paths
    assert second.raw_json_paths == first.raw_json_paths
    assert second.pgn_paths == first.pgn_paths
    assert second.raw_json_paths[0].read_bytes() == first_json
    assert second.pgn_paths[0].read_bytes() == first_pgn


def test_cache_metadata_records_body_integrity_and_kind(tmp_path: Path) -> None:
    chesscom, http_client = make_client(tmp_path, response_for)
    with http_client:
        chesscom.fetch_months("Hikaru", ["2026-01"], tmp_path / "output")

    metadata_items = [
        json.loads(path.read_text())
        for path in (tmp_path / "cache").glob("*.json")
    ]
    assert {item["kind"] for item in metadata_items} == {"json", "pgn"}
    for item in metadata_items:
        body = (tmp_path / "cache" / item["body"]).read_bytes()
        assert item["byte_size"] == len(body)
        assert item["sha256"] == hashlib.sha256(body).hexdigest()


def test_modified_cached_pgn_is_evicted_and_refetched(tmp_path: Path) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.headers.get("if-none-match"):
            return httpx.Response(304)
        return response_for(request)

    chesscom, http_client = make_client(tmp_path, handler)
    with http_client:
        chesscom.fetch_months("Hikaru", ["2026-01"], tmp_path / "output")
        pgn_metadata = next(
            json.loads(path.read_text())
            for path in (tmp_path / "cache").glob("*.json")
            if json.loads(path.read_text())["kind"] == "pgn"
        )
        pgn_body = tmp_path / "cache" / pgn_metadata["body"]
        modified = pgn_body.read_bytes().replace(b"Scholar", b"Changed")
        pgn_body.write_bytes(modified)
        result = chesscom.fetch_months(
            "Hikaru",
            ["2026-01"],
            tmp_path / "output",
        )

    pgn_requests = [request for request in requests if request.url.path.endswith("/pgn")]
    assert len(pgn_requests) == 3
    assert pgn_requests[1].headers["if-none-match"] == '"pgn-v1"'
    assert "if-none-match" not in pgn_requests[2].headers
    assert result.pgn_paths[0].read_bytes() == PGN_BODY


def test_malformed_cache_metadata_is_evicted_and_refetched(tmp_path: Path) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return response_for(request)

    chesscom, http_client = make_client(tmp_path, handler)
    with http_client:
        chesscom.fetch_months("Hikaru", ["2026-01"], tmp_path / "output")
        metadata_path = next((tmp_path / "cache").glob("*.json"))
        metadata_path.write_text("{not-json", encoding="utf-8")
        chesscom.fetch_months("Hikaru", ["2026-01"], tmp_path / "output")

    assert len(requests) == 4


def test_304_with_unusable_cache_retries_unconditionally(
    tmp_path: Path,
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.headers.get("if-none-match"):
            return httpx.Response(304)
        return response_for(request)

    chesscom, http_client = make_client(tmp_path, handler)
    with http_client:
        chesscom.fetch_months("Hikaru", ["2026-01"], tmp_path / "output")
        pgn_metadata = next(
            json.loads(path.read_text())
            for path in (tmp_path / "cache").glob("*.json")
            if json.loads(path.read_text())["url"].endswith("/pgn")
        )
        (tmp_path / "cache" / pgn_metadata["body"]).unlink()
        result = chesscom.fetch_months(
            "Hikaru",
            ["2026-01"],
            tmp_path / "output",
        )

    pgn_requests = [request for request in requests if request.url.path.endswith("/pgn")]
    assert len(pgn_requests) == 3
    assert pgn_requests[1].headers["if-none-match"] == '"pgn-v1"'
    assert "if-none-match" not in pgn_requests[2].headers
    assert result.pgn_paths[0].read_bytes() == PGN_BODY


def test_fetch_writes_json_and_pgn_provenance_for_200_then_304(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 6, 14, 12, 0, tzinfo=UTC)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.headers.get("if-none-match"):
            return httpx.Response(304)
        return response_for(request, cache_control="no-cache")

    chesscom, http_client = make_client(
        tmp_path,
        handler,
        clock=lambda: now,
    )
    with http_client:
        first = chesscom.fetch_months(
            "Hikaru",
            ["2026-01"],
            tmp_path / "output",
        )
        first_provenance = json.loads(first.metadata_paths[0].read_text())
        now += timedelta(minutes=5)
        second = chesscom.fetch_months(
            "Hikaru",
            ["2026-01"],
            tmp_path / "output",
        )
        second_provenance = json.loads(second.metadata_paths[0].read_text())

    assert first_provenance["retrieved_at"] == "2026-06-14T12:00:00+00:00"
    assert second_provenance["retrieved_at"] == "2026-06-14T12:05:00+00:00"
    assert first_provenance["resources"] == {
        "json": {
            "url": "https://api.chess.com/pub/player/hikaru/games/2026/01",
            "output_path": "hikaru-2026-01.json",
            "etag": '"json-v1"',
            "last_modified": "Sun, 14 Jun 2026 09:00:00 GMT",
            "cache_control": "no-cache",
            "cache_status": "network",
        },
        "pgn": {
            "url": "https://api.chess.com/pub/player/hikaru/games/2026/01/pgn",
            "output_path": "hikaru-2026-01.pgn",
            "etag": '"pgn-v1"',
            "last_modified": "Sun, 14 Jun 2026 10:00:00 GMT",
            "cache_control": "no-cache",
            "cache_status": "network",
        },
    }
    assert {
        resource["cache_status"]
        for resource in second_provenance["resources"].values()
    } == {"not_modified"}
    assert second_provenance["resources"]["json"]["etag"] == '"json-v1"'
    assert second_provenance["resources"]["pgn"]["etag"] == '"pgn-v1"'
    assert {
        resource["cache_control"]
        for resource in second_provenance["resources"].values()
    } == {"no-cache"}


def test_fetch_uses_fresh_max_age_cache_without_network(
    tmp_path: Path,
) -> None:
    requests: list[httpx.Request] = []
    now = datetime(2026, 6, 14, 12, 0, tzinfo=UTC)

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return response_for(request, cache_control="public, max-age=120")

    chesscom, http_client = make_client(
        tmp_path,
        handler,
        clock=lambda: now,
    )
    with http_client:
        chesscom.fetch_months("Hikaru", ["2026-01"], tmp_path / "output")
        now += timedelta(seconds=60)
        second = chesscom.fetch_months(
            "Hikaru",
            ["2026-01"],
            tmp_path / "output",
        )

    assert len(requests) == 2
    provenance = json.loads(second.metadata_paths[0].read_text())
    assert {
        resource["cache_status"]
        for resource in provenance["resources"].values()
    } == {"fresh_cache"}


def test_fetch_no_cache_persists_and_conditionally_revalidates(
    tmp_path: Path,
) -> None:
    requests: list[httpx.Request] = []
    now = datetime(2026, 6, 14, 12, 0, tzinfo=UTC)

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.headers.get("if-none-match"):
            return httpx.Response(304)
        return response_for(request, cache_control="no-cache")

    chesscom, http_client = make_client(
        tmp_path,
        handler,
        clock=lambda: now,
    )
    with http_client:
        chesscom.fetch_months("Hikaru", ["2026-01"], tmp_path / "output")
        now += timedelta(seconds=1)
        chesscom.fetch_months("Hikaru", ["2026-01"], tmp_path / "output")

    cache_metadata = [
        json.loads(path.read_text())
        for path in (tmp_path / "cache").glob("*.json")
    ]
    assert len(requests) == 4
    assert requests[2].headers["if-none-match"] == '"json-v1"'
    assert requests[3].headers["if-none-match"] == '"pgn-v1"'
    assert {item["cache_control"] for item in cache_metadata} == {"no-cache"}


def test_fetch_parameterized_no_cache_conditionally_revalidates(
    tmp_path: Path,
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.headers.get("if-none-match"):
            return httpx.Response(304)
        return response_for(
            request,
            cache_control='public, No-Cache="Set-Cookie", max-age=3600',
        )

    chesscom, http_client = make_client(tmp_path, handler)
    with http_client:
        chesscom.fetch_months("Hikaru", ["2026-01"], tmp_path / "output")
        chesscom.fetch_months("Hikaru", ["2026-01"], tmp_path / "output")

    assert len(requests) == 4
    assert requests[2].headers["if-none-match"] == '"json-v1"'
    assert requests[3].headers["if-none-match"] == '"pgn-v1"'


def test_fetch_age_header_can_exhaust_max_age(tmp_path: Path) -> None:
    now = datetime(2026, 6, 14, 12, 0, tzinfo=UTC)
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return response_for(
            request,
            cache_control="max-age=120",
            date=format_datetime(now, usegmt=True),
            age="120",
        )

    chesscom, http_client = make_client(tmp_path, handler, clock=lambda: now)
    with http_client:
        chesscom.fetch_months("Hikaru", ["2026-01"], tmp_path / "output")
        chesscom.fetch_months("Hikaru", ["2026-01"], tmp_path / "output")

    assert len(requests) == 4


def test_fetch_old_date_can_exhaust_max_age(tmp_path: Path) -> None:
    now = datetime(2026, 6, 14, 12, 0, tzinfo=UTC)
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return response_for(
            request,
            cache_control="max-age=120",
            date=format_datetime(now - timedelta(seconds=121), usegmt=True),
        )

    chesscom, http_client = make_client(tmp_path, handler, clock=lambda: now)
    with http_client:
        chesscom.fetch_months("Hikaru", ["2026-01"], tmp_path / "output")
        chesscom.fetch_months("Hikaru", ["2026-01"], tmp_path / "output")

    assert len(requests) == 4


def test_fetch_future_cached_at_is_stale(tmp_path: Path) -> None:
    now = datetime(2026, 6, 14, 12, 0, tzinfo=UTC)
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return response_for(
            request,
            cache_control="max-age=3600",
            date=format_datetime(now, usegmt=True),
        )

    chesscom, http_client = make_client(tmp_path, handler, clock=lambda: now)
    with http_client:
        chesscom.fetch_months("Hikaru", ["2026-01"], tmp_path / "output")
        for metadata_path in (tmp_path / "cache").glob("*.json"):
            metadata = json.loads(metadata_path.read_text())
            metadata["cached_at"] = (now + timedelta(minutes=5)).isoformat()
            metadata_path.write_text(json.dumps(metadata))
        chesscom.fetch_months("Hikaru", ["2026-01"], tmp_path / "output")

    assert len(requests) == 4


def test_fetch_no_store_writes_outputs_without_reusable_cache(
    tmp_path: Path,
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return response_for(request, cache_control="private, no-store")

    chesscom, http_client = make_client(tmp_path, handler)
    with http_client:
        first = chesscom.fetch_months(
            "Hikaru",
            ["2026-01"],
            tmp_path / "output",
        )
        second = chesscom.fetch_months(
            "Hikaru",
            ["2026-01"],
            tmp_path / "output",
        )

    assert len(requests) == 4
    assert all("if-none-match" not in request.headers for request in requests)
    assert all(
        "if-modified-since" not in request.headers for request in requests
    )
    assert first.raw_json_paths[0].read_bytes() == JSON_BODY
    assert first.pgn_paths[0].read_bytes() == PGN_BODY
    assert second.raw_json_paths[0].read_bytes() == JSON_BODY
    assert second.pgn_paths[0].read_bytes() == PGN_BODY
    assert not list((tmp_path / "cache").glob("*"))
    provenance = json.loads(second.metadata_paths[0].read_text())
    assert {
        resource["cache_status"]
        for resource in provenance["resources"].values()
    } == {"network"}
    assert {
        resource["cache_control"]
        for resource in provenance["resources"].values()
    } == {"private, no-store"}


def test_fetch_no_store_deletes_existing_reusable_cache(
    tmp_path: Path,
) -> None:
    use_no_store = False

    def handler(request: httpx.Request) -> httpx.Response:
        cache_control = "no-store" if use_no_store else "no-cache"
        return response_for(request, cache_control=cache_control)

    chesscom, http_client = make_client(tmp_path, handler)
    with http_client:
        chesscom.fetch_months("Hikaru", ["2026-01"], tmp_path / "output")
        assert len(list((tmp_path / "cache").glob("*"))) == 4
        use_no_store = True
        result = chesscom.fetch_months(
            "Hikaru",
            ["2026-01"],
            tmp_path / "output",
        )

    assert not list((tmp_path / "cache").glob("*"))
    provenance = json.loads(result.metadata_paths[0].read_text())
    assert {
        resource["cache_status"]
        for resource in provenance["resources"].values()
    } == {"network"}
    assert {
        resource["cache_control"]
        for resource in provenance["resources"].values()
    } == {"no-store"}


def test_fetch_retries_429_once_and_honors_retry_after(
    tmp_path: Path,
) -> None:
    attempts: dict[str, int] = {}
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        attempts[path] = attempts.get(path, 0) + 1
        if not path.endswith("/pgn") and attempts[path] == 1:
            return httpx.Response(429, headers={"Retry-After": "0"})
        return response_for(request)

    chesscom, http_client = make_client(tmp_path, handler, sleep=sleeps.append)
    with http_client:
        chesscom.fetch_months("Hikaru", ["2026-01"], tmp_path / "output")

    json_path = "/pub/player/hikaru/games/2026/01"
    assert attempts[json_path] == 2
    assert sum(attempts.values()) == 3
    assert sleeps == [0.0]


def test_fetch_honors_retry_after_delta_without_silent_cap(
    tmp_path: Path,
) -> None:
    attempts = 0
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        if not request.url.path.endswith("/pgn"):
            attempts += 1
            if attempts == 1:
                return httpx.Response(429, headers={"Retry-After": "120"})
        return response_for(request)

    chesscom, http_client = make_client(tmp_path, handler, sleep=sleeps.append)
    with http_client:
        chesscom.fetch_months("Hikaru", ["2026-01"], tmp_path / "output")

    assert sleeps == [120.0]


def test_fetch_honors_retry_after_http_date(tmp_path: Path) -> None:
    now = datetime(2026, 6, 14, 12, 0, tzinfo=UTC)
    retry_at = format_datetime(now + timedelta(seconds=45), usegmt=True)
    attempts = 0
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        if not request.url.path.endswith("/pgn"):
            attempts += 1
            if attempts == 1:
                return httpx.Response(429, headers={"Retry-After": retry_at})
        return response_for(request)

    chesscom, http_client = make_client(
        tmp_path,
        handler,
        sleep=sleeps.append,
        clock=lambda: now,
    )
    with http_client:
        chesscom.fetch_months("Hikaru", ["2026-01"], tmp_path / "output")

    assert sleeps == [45.0]


def test_fetch_invalid_retry_after_uses_exponential_delay(
    tmp_path: Path,
) -> None:
    attempts = 0
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        if not request.url.path.endswith("/pgn"):
            attempts += 1
            if attempts == 1:
                return httpx.Response(
                    429,
                    headers={"Retry-After": "not-a-delay"},
                )
        return response_for(request)

    chesscom, http_client = make_client(tmp_path, handler, sleep=sleeps.append)
    with http_client:
        chesscom.fetch_months("Hikaru", ["2026-01"], tmp_path / "output")

    assert sleeps == [1.0]


def test_fetch_stops_after_three_transient_attempts(tmp_path: Path) -> None:
    requests: list[httpx.Request] = []
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(503, headers={"Content-Type": "text/plain"})

    chesscom, http_client = make_client(tmp_path, handler, sleep=sleeps.append)
    with http_client:
        with pytest.raises(AppError) as caught:
            chesscom.fetch_months("Hikaru", ["2026-01"], tmp_path / "output")

    assert caught.value.code == ErrorCode.HTTP_ERROR
    assert len(requests) == 3
    assert sleeps == [1.0, 2.0]


def test_fetch_retries_network_errors_at_most_three_times(
    tmp_path: Path,
) -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        raise httpx.ConnectError("fixture unavailable", request=request)

    chesscom, http_client = make_client(tmp_path, handler)
    with http_client:
        with pytest.raises(AppError) as caught:
            chesscom.fetch_months("Hikaru", ["2026-01"], tmp_path / "output")

    assert caught.value.code == ErrorCode.HTTP_ERROR
    assert attempts == 3


@pytest.mark.parametrize(
    ("bad_path_suffix", "content_type"),
    [
        ("", "text/html"),
        ("/pgn", "application/json"),
    ],
)
def test_fetch_rejects_incompatible_content_types(
    tmp_path: Path,
    bad_path_suffix: str,
    content_type: str,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(bad_path_suffix):
            return httpx.Response(
                200,
                headers={"Content-Type": content_type},
                content=b"not the requested representation",
            )
        return response_for(request)

    chesscom, http_client = make_client(tmp_path, handler)
    with http_client:
        with pytest.raises(AppError) as caught:
            chesscom.fetch_months("Hikaru", ["2026-01"], tmp_path / "output")

    assert caught.value.code == ErrorCode.HTTP_ERROR


def test_fetch_rejects_json_that_is_not_an_object(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"Content-Type": "application/json"},
            content=b"[]",
        )

    chesscom, http_client = make_client(tmp_path, handler)
    with http_client:
        with pytest.raises(AppError) as caught:
            chesscom.fetch_months("Hikaru", ["2026-01"], tmp_path / "output")

    assert caught.value.code == ErrorCode.HTTP_ERROR


def test_invalid_refreshed_pgn_preserves_previous_month_artifacts(
    tmp_path: Path,
) -> None:
    invalid = False

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/pgn") and invalid:
            return httpx.Response(
                200,
                headers={
                    "Content-Type": "application/x-chess-pgn",
                    "Cache-Control": "no-store",
                },
                content=MALFORMED_PGN_BODY,
            )
        return response_for(request, cache_control="no-store")

    output_dir = tmp_path / "output"
    chesscom, http_client = make_client(tmp_path, handler)
    with http_client:
        result = chesscom.fetch_months("Hikaru", ["2026-01"], output_dir)
        tracked = [
            result.raw_json_paths[0],
            result.pgn_paths[0],
            result.metadata_paths[0],
            result.manifest_path,
        ]
        before = {path: path.read_bytes() for path in tracked}
        invalid = True
        with pytest.raises(AppError) as caught:
            chesscom.fetch_months("Hikaru", ["2026-01"], output_dir)

    assert caught.value.code == ErrorCode.INVALID_PGN
    assert {path: path.read_bytes() for path in tracked} == before


def test_month_publish_failure_rolls_back_all_visible_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_dir = tmp_path / "output"
    chesscom, http_client = make_client(
        tmp_path,
        lambda request: response_for(request, cache_control="no-store"),
    )
    with http_client:
        result = chesscom.fetch_months("Hikaru", ["2026-01"], output_dir)
        tracked = [
            result.raw_json_paths[0],
            result.pgn_paths[0],
            result.metadata_paths[0],
            result.manifest_path,
        ]
        before = {path: path.read_bytes() for path in tracked}
        original_replace = chesscom_module._replace_path
        failed = False

        def fail_once(source: Path, destination: Path) -> None:
            nonlocal failed
            if destination.name == "hikaru-2026-01.pgn" and not failed:
                failed = True
                raise OSError("fixture install failure")
            original_replace(source, destination)

        monkeypatch.setattr(chesscom_module, "_replace_path", fail_once)
        with pytest.raises(OSError, match="fixture install failure"):
            chesscom.fetch_months("Hikaru", ["2026-01"], output_dir)

    assert {path: path.read_bytes() for path in tracked} == before
    assert not list(output_dir.glob(".*.transaction.json"))


def test_transaction_marker_is_synced_before_first_visible_replace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    original_atomic_write_json = chesscom_module.atomic_write_json
    original_fsync_directory = chesscom_module._fsync_directory
    original_replace = chesscom_module._replace_path

    def track_atomic_write_json(path: Path, payload: object) -> None:
        original_atomic_write_json(path, payload)
        if path.name.endswith(".transaction.json"):
            events.append("marker")

    def track_fsync_directory(path: Path) -> None:
        events.append("fsync")
        original_fsync_directory(path)

    def track_replace(source: Path, destination: Path) -> None:
        events.append("replace")
        original_replace(source, destination)

    monkeypatch.setattr(
        chesscom_module,
        "atomic_write_json",
        track_atomic_write_json,
    )
    monkeypatch.setattr(
        chesscom_module,
        "_fsync_directory",
        track_fsync_directory,
    )
    monkeypatch.setattr(chesscom_module, "_replace_path", track_replace)
    chesscom, http_client = make_client(tmp_path, response_for)
    with http_client:
        chesscom.fetch_months("Hikaru", ["2026-01"], tmp_path / "output")

    marker_index = events.index("marker")
    assert events[marker_index : marker_index + 3] == [
        "marker",
        "fsync",
        "replace",
    ]


def test_state_install_failure_rolls_back_month_and_retry_skips_committed_month(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return response_for(request, cache_control="no-store")

    output_dir = tmp_path / "output"
    chesscom, http_client = make_client(tmp_path, handler)
    original_replace = chesscom_module._replace_path
    manifest_installs = 0
    failed = False
    observed_marker: dict[str, object] | None = None

    def fail_second_state_install(source: Path, destination: Path) -> None:
        nonlocal failed, manifest_installs, observed_marker
        if destination.name == "manifest.v1.json":
            manifest_installs += 1
        if (
            destination.name == "hikaru.fetching.json"
            and manifest_installs == 2
            and not failed
        ):
            failed = True
            marker_path = output_dir / ".hikaru-2026-02.transaction.json"
            observed_marker = json.loads(marker_path.read_text())
            raise OSError("fixture state install failure")
        original_replace(source, destination)

    monkeypatch.setattr(
        chesscom_module,
        "_replace_path",
        fail_second_state_install,
    )
    with http_client:
        with pytest.raises(OSError, match="fixture state install failure"):
            chesscom.fetch_months(
                "Hikaru",
                ["2026-01", "2026-02"],
                output_dir,
            )

        marker = json.loads(
            (output_dir / "hikaru.fetching.json").read_text(),
        )
        manifest = json.loads((output_dir / "manifest.v1.json").read_text())
        assert marker["completed_months"] == ["2026-01"]
        assert [item["path"] for item in manifest["files"]] == [
            "hikaru-2026-01.pgn"
        ]
        assert (output_dir / "hikaru-2026-01.json").is_file()
        assert (output_dir / "hikaru-2026-01.pgn").is_file()
        assert (output_dir / "hikaru-2026-01.provenance.json").is_file()
        assert not list(output_dir.glob("hikaru-2026-02.*"))
        assert not list(output_dir.glob(".*.transaction.json"))

        assert observed_marker is not None
        transaction_id = observed_marker["transaction_id"]
        assert isinstance(transaction_id, str)
        entries = observed_marker["entries"]
        assert isinstance(entries, list)
        expected_destinations = {
            "hikaru-2026-02.json",
            "hikaru-2026-02.pgn",
            "hikaru-2026-02.provenance.json",
            "manifest.v1.json",
            "hikaru.fetching.json",
        }
        assert {entry["destination"] for entry in entries} == (
            expected_destinations
        )
        for entry in entries:
            destination = entry["destination"]
            assert entry["stage"] == (
                f".{destination}.{transaction_id}.stage"
            )
            assert entry["backup"] == (
                f".{destination}.{transaction_id}.backup"
            )

        requests_before_retry = len(requests)
        result = chesscom.fetch_months(
            "Hikaru",
            ["2026-01", "2026-02"],
            output_dir,
        )

    retry_requests = requests[requests_before_retry:]
    assert all("/2026/01" not in request.url.path for request in retry_requests)
    assert [item.path for item in result.manifest.files] == [
        "hikaru-2026-01.pgn",
        "hikaru-2026-02.pgn",
    ]
    assert not (output_dir / "hikaru.fetching.json").exists()


def test_initial_invalid_month_publishes_no_month_artifacts(
    tmp_path: Path,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/pgn"):
            return httpx.Response(
                200,
                headers={"Content-Type": "application/x-chess-pgn"},
                content=MALFORMED_PGN_BODY,
            )
        return response_for(request)

    output_dir = tmp_path / "output"
    chesscom, http_client = make_client(tmp_path, handler)
    with http_client:
        with pytest.raises(AppError):
            chesscom.fetch_months("Hikaru", ["2026-01"], output_dir)

    assert not list(output_dir.glob("hikaru-2026-01.*"))
    assert not (output_dir / "manifest.v1.json").exists()
    assert (output_dir / "hikaru.fetching.json").is_file()


def test_later_month_failure_records_partial_state_and_retry_completes(
    tmp_path: Path,
) -> None:
    fail_second = True
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/2026/02/pgn") and fail_second:
            return httpx.Response(
                200,
                headers={
                    "Content-Type": "application/x-chess-pgn",
                    "Cache-Control": "no-store",
                },
                content=MALFORMED_PGN_BODY,
            )
        return response_for(request, cache_control="no-store")

    output_dir = tmp_path / "output"
    chesscom, http_client = make_client(tmp_path, handler)
    with http_client:
        with pytest.raises(AppError):
            chesscom.fetch_months(
                "Hikaru",
                ["2026-01", "2026-02"],
                output_dir,
            )

        marker_path = output_dir / "hikaru.fetching.json"
        marker = json.loads(marker_path.read_text())
        manifest = json.loads((output_dir / "manifest.v1.json").read_text())
        assert marker["requested_months"] == ["2026-01", "2026-02"]
        assert marker["completed_months"] == ["2026-01"]
        assert [item["path"] for item in manifest["files"]] == [
            "hikaru-2026-01.pgn"
        ]
        assert (output_dir / "hikaru-2026-01.json").is_file()
        assert (output_dir / "hikaru-2026-01.pgn").is_file()
        assert (output_dir / "hikaru-2026-01.provenance.json").is_file()
        assert not list(output_dir.glob("hikaru-2026-02.*"))

        requests_before_retry = len(requests)
        fail_second = False
        result = chesscom.fetch_months(
            "Hikaru",
            ["2026-01", "2026-02"],
            output_dir,
        )

    retry_requests = requests[requests_before_retry:]
    assert all("/2026/01" not in request.url.path for request in retry_requests)
    assert not marker_path.exists()
    assert [item.path for item in result.manifest.files] == [
        "hikaru-2026-01.pgn",
        "hikaru-2026-02.pgn",
    ]


class OversizedStream(httpx.SyncByteStream):
    def __iter__(self) -> Iterator[bytes]:
        chunk = b"x" * (1024 * 1024)
        yield from (chunk for _ in range(64))
        yield b"x"


class FailedReadStream(httpx.SyncByteStream):
    def __init__(self, request: httpx.Request) -> None:
        self.request = request

    def __iter__(self) -> Iterator[bytes]:
        raise httpx.ReadError("fixture read failed", request=self.request)
        yield b""


def test_fetch_retries_network_error_while_streaming(tmp_path: Path) -> None:
    json_attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal json_attempts
        if not request.url.path.endswith("/pgn"):
            json_attempts += 1
            if json_attempts == 1:
                return httpx.Response(
                    200,
                    headers={"Content-Type": "application/json"},
                    stream=FailedReadStream(request),
                )
        return response_for(request)

    chesscom, http_client = make_client(tmp_path, handler)
    with http_client:
        chesscom.fetch_months("Hikaru", ["2026-01"], tmp_path / "output")

    assert json_attempts == 2


def test_fetch_rejects_response_larger_than_64_mib(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"Content-Type": "application/json"},
            stream=OversizedStream(),
        )

    chesscom, http_client = make_client(tmp_path, handler)
    with http_client:
        with pytest.raises(AppError) as caught:
            chesscom.fetch_months("Hikaru", ["2026-01"], tmp_path / "output")

    assert caught.value.code == ErrorCode.HTTP_ERROR


def test_fetch_fails_when_304_cache_body_is_missing(tmp_path: Path) -> None:
    revalidate = False

    def handler(request: httpx.Request) -> httpx.Response:
        if revalidate:
            return httpx.Response(304)
        return response_for(request)

    chesscom, http_client = make_client(tmp_path, handler)
    with http_client:
        chesscom.fetch_months("Hikaru", ["2026-01"], tmp_path / "output")
        cache_metadata = [
            json.loads(path.read_text())
            for path in (tmp_path / "cache").glob("*.json")
        ]
        pgn_metadata = next(
            item for item in cache_metadata if item["url"].endswith("/pgn")
        )
        (tmp_path / "cache" / pgn_metadata["body"]).unlink()
        revalidate = True

        with pytest.raises(AppError, match="cached body"):
            chesscom.fetch_months("Hikaru", ["2026-01"], tmp_path / "output")


def test_fetch_follows_only_same_origin_https_redirects(tmp_path: Path) -> None:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path.endswith("/2026/01"):
            return httpx.Response(
                302,
                headers={
                    "Location": (
                        "https://api.chess.com/pub/player/hikaru/games/2026/01.json"
                    )
                },
            )
        if request.url.path.endswith(".json"):
            return response_for(request)
        return response_for(request)

    chesscom, http_client = make_client(tmp_path, handler)
    with http_client:
        chesscom.fetch_months("Hikaru", ["2026-01"], tmp_path / "output")

    assert paths[:2] == [
        "/pub/player/hikaru/games/2026/01",
        "/pub/player/hikaru/games/2026/01.json",
    ]


def test_fetch_allows_three_same_origin_redirects_before_success(
    tmp_path: Path,
) -> None:
    json_requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal json_requests
        if request.url.path.endswith("/pgn"):
            return response_for(request)
        json_requests += 1
        if json_requests <= 3:
            return httpx.Response(
                302,
                headers={
                    "Location": (
                        "https://api.chess.com"
                        f"/pub/player/hikaru/games/2026/01.r{json_requests}"
                    )
                },
            )
        return response_for(request)

    chesscom, http_client = make_client(tmp_path, handler)
    with http_client:
        chesscom.fetch_months("Hikaru", ["2026-01"], tmp_path / "output")

    assert json_requests == 4


def test_fetch_rejects_sixth_same_origin_redirect(tmp_path: Path) -> None:
    requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(
            302,
            headers={
                "Location": (
                    "https://api.chess.com"
                    f"/pub/player/hikaru/games/2026/01.r{requests}"
                )
            },
        )

    chesscom, http_client = make_client(tmp_path, handler)
    with http_client:
        with pytest.raises(AppError, match="redirect"):
            chesscom.fetch_months("Hikaru", ["2026-01"], tmp_path / "output")

    assert requests == 6


def test_fetch_rejects_cross_origin_redirect(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            302,
            headers={"Location": "https://example.com/archive.json"},
        )

    chesscom, http_client = make_client(tmp_path, handler)
    with http_client:
        with pytest.raises(AppError) as caught:
            chesscom.fetch_months("Hikaru", ["2026-01"], tmp_path / "output")

    assert caught.value.code == ErrorCode.HTTP_ERROR
