import json
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
from ai_chess.reports import write_report


def _artifact() -> AnalysisArtifact:
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
                headers={"White": "Alice", "Black": "Bob", "Event": "Fixture"},
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
                    )
                ],
                critical_positions=[
                    CriticalPosition(
                        game_id="game-1",
                        ply=1,
                        loss_centipawns=180,
                        label="mistake",
                        reason="The move lost 180 centipawns for the mover.",
                        deep_evaluation=Evaluation(centipawns=32, mate=None),
                    )
                ],
            )
        ],
        aggregate=ArchiveAggregate(
            games_total=1,
            games_completed=1,
            critical_positions=1,
            by_phase={"opening": 1, "middlegame": 0, "endgame": 0},
            evidence=[],
        ),
        complete=True,
    )


def test_write_report_writes_self_contained_html(tmp_path: Path) -> None:
    artifact_path = tmp_path / "analysis.json"
    artifact_path.write_text(json.dumps(_artifact().to_dict()), encoding="utf-8")
    insights_path = tmp_path / "insights.md"
    insights_path.write_text("Line 1\n<script>alert(1)</script>", encoding="utf-8")
    output_path = tmp_path / "report.html"

    result = write_report(artifact_path, output_path, insights_path)

    assert result == output_path
    assert output_path.is_file()
    html = output_path.read_text(encoding="utf-8")
    assert "FixtureFish" in html
    assert "Line 1" in html
    assert "<script>alert(1)</script>" not in html
    assert 'id="board"' in html
