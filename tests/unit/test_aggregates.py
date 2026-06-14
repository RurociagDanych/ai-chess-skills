from ai_chess.analysis import aggregate_games
from ai_chess.models import CriticalPosition, GameAnalysis, PlyAnalysis, Evaluation


def _ply(ply: int, phase: str) -> PlyAnalysis:
    return PlyAnalysis(
        ply=ply,
        fen=f"fen-{ply}",
        move_san=f"m{ply}",
        move_uci="e2e4",
        evaluation=Evaluation(centipawns=0, mate=None),
        depth=16,
        phase=phase,
    )


def test_aggregate_games_counts_critical_positions_by_phase_and_keeps_evidence() -> None:
    aggregate = aggregate_games(
        [
            GameAnalysis(
                game_id="game-1",
                headers={},
                result="1-0",
                plies=[_ply(1, "opening"), _ply(21, "middlegame")],
                critical_positions=[
                    CriticalPosition(
                        game_id="game-1",
                        ply=1,
                        loss_centipawns=180,
                        label="mistake",
                        reason="Lost material.",
                    ),
                    CriticalPosition(
                        game_id="game-1",
                        ply=21,
                        loss_centipawns=220,
                        label="mistake",
                        reason="Missed tactic.",
                    ),
                ],
            ),
            GameAnalysis(
                game_id="game-2",
                headers={},
                result="0-1",
                plies=[_ply(30, "middlegame")],
                critical_positions=[
                    CriticalPosition(
                        game_id="game-2",
                        ply=30,
                        loss_centipawns=None,
                        label="mate_transition",
                        reason="Lost mate defense.",
                    )
                ],
            ),
        ]
    )

    assert aggregate.games_total == 2
    assert aggregate.games_completed == 2
    assert aggregate.critical_positions == 3
    assert aggregate.by_phase == {"opening": 1, "middlegame": 2, "endgame": 0}
    assert aggregate.evidence[0]["game_id"] == "game-1"
