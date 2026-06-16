import json
from pathlib import Path

import httpx
import pytest

from ai_chess.errors import AppError, ErrorCode
from ai_chess.lichess import (
    LichessClient,
    game_export_url,
    parse_game_link,
    user_games_url,
)

FIXTURES = Path(__file__).parents[1] / "fixtures"
PGN_BODY = (FIXTURES / "single.pgn").read_bytes()


def test_user_games_url_builds_documented_export_url_with_max() -> None:
    assert user_games_url("Topeklc", max_games=25) == (
        "https://lichess.org/api/games/user/Topeklc?max=25"
    )


def test_user_games_url_rejects_invalid_username() -> None:
    with pytest.raises(AppError) as caught:
        user_games_url("../user", max_games=None)

    assert caught.value.code == ErrorCode.INVALID_INPUT


@pytest.mark.parametrize(
    ("url", "game_id"),
    [
        ("https://lichess.org/abcdefgh", "abcdefgh"),
        ("https://lichess.org/abcdefgh/black", "abcdefgh"),
        ("https://www.lichess.org/ABCD1234?tab=analysis", "ABCD1234"),
        ("abcdefgh", "abcdefgh"),
    ],
)
def test_parse_game_link_accepts_standard_lichess_game_links(
    url: str,
    game_id: str,
) -> None:
    assert parse_game_link(url) == game_id


@pytest.mark.parametrize(
    "url",
    [
        "https://lichess.org/broadcast/event/round/abcdefgh",
        "https://example.com/abcdefgh",
        "https://lichess.org/study/abcdefgh",
        "not a link",
    ],
)
def test_parse_game_link_rejects_unsupported_links(url: str) -> None:
    with pytest.raises(AppError) as caught:
        parse_game_link(url)

    assert caught.value.code == ErrorCode.INVALID_INPUT


def test_game_export_url_builds_documented_single_game_export_url() -> None:
    assert game_export_url("abcdefgh") == "https://lichess.org/game/export/abcdefgh"


def test_fetch_user_games_writes_pgn_provenance_and_manifest(tmp_path: Path) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.url.path == "/api/games/user/Topeklc"
        assert request.url.params["max"] == "1"
        return httpx.Response(
            200,
            headers={
                "Content-Type": "application/x-chess-pgn",
                "ETag": '"lichess-v1"',
                "Last-Modified": "Tue, 16 Jun 2026 10:00:00 GMT",
            },
            content=PGN_BODY,
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as http_client:
        result = LichessClient(http_client, "ai-chess/1").fetch_user_games(
            "Topeklc",
            tmp_path / "output",
            max_games=1,
        )

    assert requests[0].headers["accept"] == "application/x-chess-pgn"
    assert requests[0].headers["user-agent"] == "ai-chess/1"
    assert result.pgn_path.name == "Topeklc.lichess.pgn"
    assert result.pgn_path.read_bytes() == PGN_BODY
    assert result.manifest.source_kind == "lichess"
    assert result.manifest.source_ref == "Topeklc"
    assert result.manifest.files[0].game_count == 1
    assert result.manifest.files[0].source_url == (
        "https://lichess.org/api/games/user/Topeklc?max=1"
    )
    assert json.loads(result.manifest_path.read_text()) == result.manifest.to_dict()
    provenance = json.loads(result.provenance_path.read_text())
    assert provenance["source"] == "lichess"
    assert provenance["kind"] == "user_games"
    assert provenance["resources"]["pgn"]["etag"] == '"lichess-v1"'


def test_fetch_game_writes_single_game_archive(tmp_path: Path) -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(
            200,
            headers={"Content-Type": "application/x-chess-pgn"},
            content=PGN_BODY,
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as http_client:
        result = LichessClient(http_client, "ai-chess/1").fetch_game(
            "abcdefgh",
            tmp_path / "output",
        )

    assert seen == ["/game/export/abcdefgh"]
    assert result.pgn_path.name == "abcdefgh.lichess.pgn"
    assert result.manifest.source_kind == "lichess"
    assert result.manifest.source_ref == "abcdefgh"


def test_fetch_rejects_non_pgn_response(tmp_path: Path) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"Content-Type": "application/json"},
            json={"id": "abcdefgh"},
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as http_client:
        with pytest.raises(AppError) as caught:
            LichessClient(http_client, "ai-chess/1").fetch_game(
                "abcdefgh",
                tmp_path / "output",
            )

    assert caught.value.code == ErrorCode.HTTP_ERROR
