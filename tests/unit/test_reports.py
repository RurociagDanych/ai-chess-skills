from pathlib import Path

from ai_chess.models import (
    AnalysisArtifact,
    ArchiveAggregate,
    CriticalPosition,
    EngineInfo,
    Evaluation,
    GameAnalysis,
    PlyAnalysis,
    SourceFile,
    SourceManifest,
)
from ai_chess.reports import inert_json, render_report


def _artifact(headers: dict[str, str] | None = None) -> AnalysisArtifact:
    return AnalysisArtifact(
        source_manifest=SourceManifest(
            source_kind="local",
            source_ref="fixture",
            files=[SourceFile(path="fixture.pgn", sha256="a" * 64, game_count=1)],
        ),
        engine=EngineInfo(
            name="FixtureFish",
            version="1.0",
            path="/tmp/fake-engine",
            sha256="b" * 64,
        ),
        settings={"depth": 16},
        games=[
            GameAnalysis(
                game_id="game-1",
                headers=headers or {"White": "Alice", "Black": "Bob", "Event": "Fixture"},
                result="1-0",
                plies=[
                    PlyAnalysis(
                        ply=1,
                        fen="rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1",
                        move_san="e4",
                        move_uci="e2e4",
                        evaluation=Evaluation(centipawns=32, mate=None),
                        depth=16,
                        phase="opening",
                        pv=["e7e5", "g1f3"],
                    ),
                    PlyAnalysis(
                        ply=2,
                        fen="rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKBNR w KQkq - 0 2",
                        move_san="...e5",
                        move_uci="e7e5",
                        evaluation=Evaluation(centipawns=15, mate=None),
                        depth=16,
                        phase="opening",
                        pv=["g1f3", "b8c6"],
                    ),
                ],
                critical_positions=[
                    CriticalPosition(
                        game_id="game-1",
                        ply=2,
                        loss_centipawns=180,
                        label="mistake",
                        reason="The move lost 180 centipawns for the mover.",
                        deep_evaluation=Evaluation(centipawns=15, mate=None),
                    )
                ],
            )
        ],
        aggregate=ArchiveAggregate(
            games_total=1,
            games_completed=1,
            critical_positions=1,
            by_phase={"opening": 1, "middlegame": 0, "endgame": 0},
            evidence=[
                {
                    "game_id": "game-1",
                    "ply": 2,
                    "phase": "opening",
                    "before": {"centipawns": 32, "mate": None},
                    "after": {"centipawns": 15, "mate": None},
                    "depth": 16,
                }
            ],
        ),
        complete=True,
    )


def test_inert_json_escapes_script_breakout_sequences() -> None:
    payload = {"header": "</script><script>alert(1)</script>", "amp": "&"}

    rendered = inert_json(payload)

    assert "</script><script>alert(1)</script>" not in rendered
    assert "\\u003c/script\\u003e\\u003cscript\\u003ealert(1)\\u003c/script\\u003e" in rendered
    assert "\\u0026" in rendered


def test_render_report_is_offline_and_contains_required_structure() -> None:
    html = render_report(_artifact(), insights="Plain <b>text</b> & more")

    assert "http://" not in html
    assert "https://" not in html
    assert "<script src=" not in html
    assert "<link href=" not in html
    for element_id in (
        "board",
        "move-list",
        "evaluation-graph",
        "critical-list",
        "previous-move",
        "next-move",
        "orientation",
    ):
        assert f'id="{element_id}"' in html
    assert '<script id="analysis-data" type="application/json">' in html
    assert "fetch(" not in html
    assert 'type="module"' not in html
    assert "&lt;b&gt;text&lt;/b&gt; &amp; more" in html


def test_render_report_escapes_analysis_payload_inside_script_tag() -> None:
    html = render_report(
        _artifact(headers={"Event": "</script><script>alert(1)</script>", "White": "A", "Black": "B"}),
    )

    assert "</script><script>alert(1)</script>" not in html
    assert "\\u003c/script\\u003e\\u003cscript\\u003ealert(1)\\u003c/script\\u003e" in html

