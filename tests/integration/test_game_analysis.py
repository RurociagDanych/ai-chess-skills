import sys
from pathlib import Path

from ai_chess.analysis import AnalysisSettings, analyze_game
from ai_chess.pgn import read_games


FIXTURES = Path(__file__).parents[1] / "fixtures"
FAKE_UCI = FIXTURES / "fake_uci.py"


def test_analyze_game_produces_single_game_artifact_with_deepened_critical_moves() -> None:
    imported = read_games(FIXTURES / "single.pgn")[0]

    artifact = analyze_game(
        imported,
        [sys.executable, str(FAKE_UCI), "analysis"],
        AnalysisSettings(),
    )

    assert artifact.schema_version == "analysis.v1"
    assert artifact.complete is True
    assert artifact.engine.name == "FixtureFish 1.0"
    assert len(artifact.games) == 1
    assert artifact.aggregate.games_total == 1
    assert artifact.aggregate.games_completed == 1

    game = artifact.games[0]
    assert game.game_id == imported.game_id
    assert game.result == "1-0"
    assert len(game.plies) == 7
    assert game.plies[0].ply == 1
    assert game.plies[0].move_san == "e4"
    assert game.plies[0].move_uci == "e2e4"
    assert game.plies[0].depth == 16
    assert game.plies[0].phase == "opening"
    assert game.plies[0].pv

    assert game.critical_positions
    assert any(position.deep_evaluation is not None for position in game.critical_positions)
    assert {entry["depth"] for entry in artifact.aggregate.evidence} == {16, 22}
