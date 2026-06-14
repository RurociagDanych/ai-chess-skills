import json
from pathlib import Path

import pytest

from ai_chess.analysis import AnalysisSettings, analyze_archive
from ai_chess.errors import AppError, ErrorCode
from ai_chess.models import (
    AnalysisArtifact,
    ArchiveAggregate,
    EngineInfo,
    GameAnalysis,
    SourceFile,
    SourceManifest,
)
from ai_chess.pgn import ImportedGame, read_games


FIXTURES = Path(__file__).parents[1] / "fixtures"


def _game_artifact(game: ImportedGame) -> AnalysisArtifact:
    game_analysis = GameAnalysis(
        game_id=game.game_id,
        headers=dict(game.game.headers),
        result=game.game.headers["Result"],
        plies=[],
        critical_positions=[],
    )
    return AnalysisArtifact(
        source_manifest=SourceManifest(
            source_kind="local",
            source_ref=game.game_id,
            files=[SourceFile(path=f"{game.game_id}.pgn", sha256=game.game_id, game_count=1)],
        ),
        engine=EngineInfo(
            name="FixtureFish",
            version="1.0",
            path="/tmp/fake-engine",
            sha256="e" * 64,
        ),
        settings={"depth": 16},
        games=[game_analysis],
        aggregate=ArchiveAggregate(
            games_total=1,
            games_completed=1,
            critical_positions=0,
        ),
        complete=True,
    )


def test_analyze_archive_persists_partial_progress_and_resumes(tmp_path: Path) -> None:
    games = read_games(FIXTURES / "archive.pgn")
    output_path = tmp_path / "archive.analysis.json"
    calls: dict[str, int] = {game.game_id: 0 for game in games}
    failures = {games[1].game_id}

    def fake_analyze_game(
        game: ImportedGame,
        engine_command: list[str],
        settings: AnalysisSettings,
    ) -> AnalysisArtifact:
        calls[game.game_id] += 1
        if game.game_id in failures:
            raise RuntimeError("boom")
        return _game_artifact(game)

    first = analyze_archive(
        games,
        ["fake-engine"],
        AnalysisSettings(),
        output_path,
        _analyze_game=fake_analyze_game,
    )

    persisted = json.loads(output_path.read_text(encoding="utf-8"))

    assert first.complete is False
    assert first.aggregate.games_completed == 1
    assert len(first.games) == 1
    assert len(first.errors) == 1
    assert persisted["complete"] is False
    assert [game["game_id"] for game in persisted["games"]] == [games[0].game_id]
    assert calls == {games[0].game_id: 1, games[1].game_id: 1}

    failures.clear()

    second = analyze_archive(
        games,
        ["fake-engine"],
        AnalysisSettings(),
        output_path,
        _analyze_game=fake_analyze_game,
    )

    assert second.complete is True
    assert second.aggregate.games_completed == 2
    assert [game.game_id for game in second.games] == [games[0].game_id, games[1].game_id]
    assert second.errors == []
    assert calls == {games[0].game_id: 1, games[1].game_id: 2}


def test_analyze_archive_rejects_invalid_existing_artifact(tmp_path: Path) -> None:
    output_path = tmp_path / "archive.analysis.json"
    output_path.write_text("{not-json", encoding="utf-8")
    games = read_games(FIXTURES / "archive.pgn")

    with pytest.raises(AppError) as caught:
        analyze_archive(
            games,
            ["fake-engine"],
            AnalysisSettings(),
            output_path,
            _analyze_game=lambda *_args: _game_artifact(games[0]),
        )

    assert caught.value.code is ErrorCode.INVALID_INPUT
