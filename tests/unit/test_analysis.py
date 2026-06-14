import sys
from pathlib import Path

import chess
import chess.engine

from ai_chess.analysis import (
    AnalysisSettings,
    PlyEvidence,
    normalize_score,
    select_critical_positions,
)


FIXTURES = Path(__file__).parents[1] / "fixtures"


def test_normalize_score_returns_white_point_of_view_centipawns_and_mate() -> None:
    assert normalize_score(None) == {"centipawns": None, "mate": None}
    assert normalize_score(
        chess.engine.PovScore(chess.engine.Cp(34), chess.WHITE)
    ) == {"centipawns": 34, "mate": None}
    assert normalize_score(
        chess.engine.PovScore(chess.engine.Cp(34), chess.BLACK)
    ) == {"centipawns": -34, "mate": None}
    assert normalize_score(
        chess.engine.PovScore(chess.engine.Mate(-2), chess.BLACK)
    ) == {"centipawns": None, "mate": 2}


def test_select_critical_positions_marks_centipawn_loss_for_the_mover() -> None:
    critical = select_critical_positions(
        [
            PlyEvidence(
                game_id="game-1",
                ply=1,
                phase="opening",
                before={"centipawns": 50, "mate": None},
                after={"centipawns": -250, "mate": None},
            )
        ],
        threshold=150,
        limit=8,
    )

    assert len(critical) == 1
    assert critical[0].game_id == "game-1"
    assert critical[0].ply == 1
    assert critical[0].loss_centipawns == 300
    assert critical[0].label == "mistake"
    assert "300" in critical[0].reason


def test_select_critical_positions_keeps_mate_transitions_and_caps_top_n() -> None:
    critical = select_critical_positions(
        [
            PlyEvidence(
                game_id="game-1",
                ply=1,
                phase="opening",
                before={"centipawns": 0, "mate": None},
                after={"centipawns": None, "mate": -1},
            ),
            PlyEvidence(
                game_id="game-1",
                ply=2,
                phase="opening",
                before={"centipawns": -20, "mate": None},
                after={"centipawns": 280, "mate": None},
            ),
            PlyEvidence(
                game_id="game-1",
                ply=3,
                phase="opening",
                before={"centipawns": 10, "mate": None},
                after={"centipawns": -390, "mate": None},
            ),
        ],
        threshold=150,
        limit=2,
    )

    assert [position.ply for position in critical] == [1, 3]
    assert critical[0].loss_centipawns is None
    assert critical[0].label == "mate_transition"
    assert critical[1].loss_centipawns == 400
    assert all(position.deep_evaluation is None for position in critical)
