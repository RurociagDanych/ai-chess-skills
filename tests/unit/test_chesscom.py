import errno
import json
import os
from pathlib import Path

import httpx
import pytest

import ai_chess.chesscom as chesscom_module
from ai_chess.chesscom import (
    ChessComClient,
    GameReference,
    iter_months,
    monthly_games_url,
    monthly_pgn_url,
    parse_game_link,
)
from ai_chess.errors import AppError, ErrorCode


def test_month_range_is_inclusive() -> None:
    assert iter_months("2025-11", "2026-02") == [
        "2025-11",
        "2025-12",
        "2026-01",
        "2026-02",
    ]


@pytest.mark.parametrize(
    ("start", "end"),
    [
        ("2026-1", "2026-02"),
        ("2026-01", "2026-13"),
        ("not-a-month", "2026-02"),
        ("2026-03", "2026-02"),
    ],
)
def test_month_range_rejects_malformed_or_descending_values(
    start: str,
    end: str,
) -> None:
    with pytest.raises(AppError) as caught:
        iter_months(start, end)

    assert caught.value.code == ErrorCode.INVALID_INPUT


def test_monthly_urls_normalize_username() -> None:
    assert monthly_games_url("Hikaru_123", "2026-01") == (
        "https://api.chess.com/pub/player/hikaru_123/games/2026/01"
    )
    assert monthly_pgn_url("Hikaru_123", "2026-01") == (
        "https://api.chess.com/pub/player/hikaru_123/games/2026/01/pgn"
    )


@pytest.mark.parametrize(
    "username",
    ["ab", "a" * 26, "../user", "user.name", "user name"],
)
def test_monthly_urls_reject_invalid_usernames(username: str) -> None:
    with pytest.raises(AppError) as caught:
        monthly_pgn_url(username, "2026-01")

    assert caught.value.code == ErrorCode.INVALID_INPUT


def test_monthly_urls_reject_invalid_month() -> None:
    with pytest.raises(AppError) as caught:
        monthly_games_url("Hikaru", "2026-1")

    assert caught.value.code == ErrorCode.INVALID_INPUT


@pytest.mark.parametrize(
    "user_agent",
    [
        "",
        "   ",
        "x",
        "python-httpx/0.28",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        "ai-chess-skills/0.1 (maintainer)",
        "project/1 (@)",
        "project/1 (http://)",
        "project/1 (https://)",
        "project/1 (maintainer@)",
        "project/1 (@example.com)",
        "project/1 (maintainer@example)",
        "project/1 (maintainer..name@example.com)",
        "project/1 (https:///contact)",
    ],
)
def test_client_requires_identifiable_user_agent(
    tmp_path: Path,
    user_agent: str,
) -> None:
    with httpx.Client() as client:
        with pytest.raises(AppError) as caught:
            ChessComClient(client, user_agent, tmp_path)

    assert caught.value.code == ErrorCode.INVALID_INPUT
    assert "project/version" in caught.value.remedy
    assert "contact" in caught.value.remedy


@pytest.mark.parametrize(
    "user_agent",
    [
        "ai-chess-skills/0.1 (maintainer@example.com)",
        "project/1 (maintainer@example.com)",
        "project/1 (http://example.com/contact)",
        "ai-chess-skills/0.1 (https://example.com/contact)",
    ],
)
def test_client_accepts_project_and_contact_user_agent(
    tmp_path: Path,
    user_agent: str,
) -> None:
    with httpx.Client() as client:
        chesscom = ChessComClient(client, user_agent, tmp_path)

    assert chesscom.user_agent == user_agent


def test_client_rejects_impossible_resource_kind(tmp_path: Path) -> None:
    with httpx.Client() as client:
        chesscom = ChessComClient(
            client,
            "project/1 (maintainer@example.com)",
            tmp_path,
        )
        with pytest.raises(AppError) as caught:
            chesscom._fetch("https://api.chess.com/test", "html")  # type: ignore[arg-type]

    assert caught.value.code == ErrorCode.INVALID_INPUT


def _transaction_entries(
    username: str,
    month: str,
    transaction_id: str,
) -> list[dict[str, object]]:
    destinations = [
        f"{username}-{month}.json",
        f"{username}-{month}.pgn",
        f"{username}-{month}.provenance.json",
        "manifest.v1.json",
        f"{username}.fetching.json",
    ]
    return [
        {
            "destination": destination,
            "stage": f".{destination}.{transaction_id}.stage",
            "backup": f".{destination}.{transaction_id}.backup",
            "existed": False,
        }
        for destination in destinations
    ]


@pytest.mark.parametrize(
    ("field", "malicious_name"),
    [
        ("stage", "victim.txt"),
        ("backup", "victim.txt"),
        ("stage", "/tmp/victim.txt"),
        ("backup", "/tmp/victim.txt"),
        ("stage", "../victim.txt"),
        ("backup", "nested/victim.txt"),
    ],
)
def test_recovery_rejects_unbound_transaction_paths_without_modifying_victim(
    tmp_path: Path,
    field: str,
    malicious_name: str,
) -> None:
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    victim = output_dir / "victim.txt"
    victim.write_text("do not modify", encoding="utf-8")
    transaction_id = "a" * 32
    entries = _transaction_entries("hikaru", "2026-01", transaction_id)
    entries[0][field] = malicious_name
    marker_path = output_dir / ".hikaru-2026-01.transaction.json"
    marker_path.write_text(
        json.dumps(
            {"transaction_id": transaction_id, "entries": entries},
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="transaction marker"):
        chesscom_module._recover_month_transaction(
            output_dir,
            "hikaru",
            "2026-01",
        )

    assert victim.read_text(encoding="utf-8") == "do not modify"
    assert marker_path.is_file()


@pytest.mark.parametrize("field", ["stage", "backup"])
def test_recovery_rejects_symlinked_transaction_members(
    tmp_path: Path,
    field: str,
) -> None:
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    victim = tmp_path / "victim.txt"
    victim.write_text("do not modify", encoding="utf-8")
    transaction_id = "b" * 32
    entries = _transaction_entries("hikaru", "2026-01", transaction_id)
    member_path = output_dir / str(entries[0][field])
    member_path.symlink_to(victim)
    marker_path = output_dir / ".hikaru-2026-01.transaction.json"
    marker_path.write_text(
        json.dumps(
            {"transaction_id": transaction_id, "entries": entries},
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="transaction marker"):
        chesscom_module._recover_month_transaction(
            output_dir,
            "hikaru",
            "2026-01",
        )

    assert victim.read_text(encoding="utf-8") == "do not modify"
    assert member_path.is_symlink()
    assert marker_path.is_file()


@pytest.mark.skipif(os.name != "posix", reason="POSIX directory fsync required")
@pytest.mark.parametrize(
    "error_number",
    [errno.EINVAL, errno.ENOTSUP, errno.ENOSYS],
)
def test_fsync_directory_ignores_unsupported_fsync_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error_number: int,
) -> None:
    monkeypatch.setattr(
        chesscom_module.os,
        "fsync",
        lambda _descriptor: (_ for _ in ()).throw(
            OSError(error_number, "unsupported directory fsync"),
        ),
    )

    chesscom_module._fsync_directory(tmp_path)


@pytest.mark.skipif(os.name != "posix", reason="POSIX directory fsync required")
def test_fsync_directory_propagates_io_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        chesscom_module.os,
        "fsync",
        lambda _descriptor: (_ for _ in ()).throw(
            OSError(errno.EIO, "directory fsync failed"),
        ),
    )

    with pytest.raises(OSError) as caught:
        chesscom_module._fsync_directory(tmp_path)

    assert caught.value.errno == errno.EIO


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://www.chess.com/game/170242602084", ("live", "170242602084")),
        ("https://www.chess.com/game/live/170242602084", ("live", "170242602084")),
        ("https://www.chess.com/game/daily/123", ("daily", "123")),
        ("chess.com/game/999", ("live", "999")),
        ("  https://www.chess.com/game/live/42?tab=review  ", ("live", "42")),
    ],
)
def test_parse_game_link_extracts_kind_and_id(
    url: str, expected: tuple[str, str]
) -> None:
    assert parse_game_link(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        "https://www.chess.com/member/topeklc",
        "https://example.com/game/123",
        "not a url",
    ],
)
def test_parse_game_link_rejects_non_game_urls(url: str) -> None:
    with pytest.raises(AppError) as caught:
        parse_game_link(url)
    assert caught.value.code == ErrorCode.INVALID_INPUT


def _callback_payload() -> dict[str, object]:
    return {
        "game": {
            "id": 170242602084,
            "pgnHeaders": {
                "White": "Topeklc",
                "Black": "AkmalEliteW",
                "Date": "2026.06.15",
                "Result": "1-0",
            },
        }
    }


def test_fetch_game_reference_resolves_players_and_month(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/callback/live/game/170242602084"
        return httpx.Response(
            200,
            json=_callback_payload(),
            headers={"Content-Type": "application/json"},
        )

    transport = httpx.MockTransport(handler)
    with httpx.Client(transport=transport) as client:
        chesscom = ChessComClient(
            client, "project/1 (maintainer@example.com)", tmp_path
        )
        reference = chesscom.fetch_game_reference(
            "https://www.chess.com/game/170242602084"
        )

    assert reference == GameReference(
        game_id="170242602084",
        kind="live",
        white="Topeklc",
        black="AkmalEliteW",
        month="2026-06",
    )


def test_fetch_game_reference_falls_back_from_live_to_daily(tmp_path: Path) -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if "live" in request.url.path:
            return httpx.Response(404, text="missing")
        return httpx.Response(
            200,
            json=_callback_payload(),
            headers={"Content-Type": "application/json"},
        )

    transport = httpx.MockTransport(handler)
    with httpx.Client(transport=transport) as client:
        chesscom = ChessComClient(
            client, "project/1 (maintainer@example.com)", tmp_path
        )
        reference = chesscom.fetch_game_reference(
            "https://www.chess.com/game/170242602084"
        )

    assert reference.kind == "daily"
    assert any("live" in path for path in seen)
    assert any("daily" in path for path in seen)
