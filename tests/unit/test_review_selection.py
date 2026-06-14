import chess.pgn

from ai_chess.pgn import ImportedGame, select_latest_index


def _game(headers: dict[str, str]) -> ImportedGame:
    game = chess.pgn.Game()
    for key, value in headers.items():
        game.headers[key] = value
    return ImportedGame(game_id="x", game=game)


def test_selects_latest_by_utc_date_then_time() -> None:
    games = [
        _game({"UTCDate": "2026.06.10", "UTCTime": "10:00:00"}),
        _game({"UTCDate": "2026.06.15", "UTCTime": "08:00:00"}),
        _game({"UTCDate": "2026.06.15", "UTCTime": "11:51:44"}),
    ]
    assert select_latest_index(games) == 3


def test_falls_back_to_date_header_when_utc_absent() -> None:
    games = [
        _game({"Date": "2026.06.01"}),
        _game({"Date": "2026.06.14"}),
    ]
    assert select_latest_index(games) == 2


def test_dated_games_rank_above_undated() -> None:
    games = [
        _game({"UTCDate": "2026.06.14", "UTCTime": "09:00:00"}),
        _game({"Date": "????.??.??"}),
    ]
    assert select_latest_index(games) == 1


def test_no_dates_anywhere_returns_last_game() -> None:
    games = [_game({}), _game({}), _game({})]
    assert select_latest_index(games) == 3


def test_single_game_returns_one() -> None:
    assert select_latest_index([_game({"UTCDate": "2026.06.15"})]) == 1
