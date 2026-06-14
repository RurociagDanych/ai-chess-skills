import pickle

import pytest

from ai_chess.errors import AppError, ErrorCode
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


def test_app_error_to_dict_uses_stable_string_code():
    error = AppError(
        code=ErrorCode.INVALID_PGN,
        message="The PGN could not be parsed.",
        remedy="Check the PGN syntax and try again.",
    )

    assert error.to_dict() == {
        "code": "invalid_pgn",
        "message": "The PGN could not be parsed.",
        "remedy": "Check the PGN syntax and try again.",
    }


def test_app_error_pickle_roundtrip_preserves_fields():
    error = AppError(
        code=ErrorCode.INVALID_PGN,
        message="The PGN could not be parsed.",
        remedy="Check the PGN syntax and try again.",
    )

    restored = pickle.loads(pickle.dumps(error))

    assert restored.code is ErrorCode.INVALID_PGN
    assert restored.message == error.message
    assert restored.remedy == error.remedy


def test_source_manifest_to_dict_includes_version_and_nested_file():
    manifest = SourceManifest(
        source_kind="local",
        source_ref="/tmp/game.pgn",
        files=[
            SourceFile(
                path="game.pgn",
                sha256="a" * 64,
                game_count=1,
            )
        ],
    )

    serialized = manifest.to_dict()

    assert serialized["schema_version"] == "manifest.v1"
    assert serialized["files"][0]["game_count"] == 1


def test_source_manifest_schema_version_cannot_be_overridden():
    with pytest.raises(TypeError):
        SourceManifest(
            source_kind="local",
            source_ref="/tmp/game.pgn",
            files=[],
            schema_version="manifest.v2",
        )

    manifest = SourceManifest(
        source_kind="local",
        source_ref="/tmp/game.pgn",
        files=[],
    )
    assert manifest.to_dict()["schema_version"] == "manifest.v1"


def test_evaluation_allows_unknown_but_rejects_centipawns_and_mate_together():
    assert Evaluation(centipawns=None, mate=None).to_dict() == {
        "centipawns": None,
        "mate": None,
    }

    with pytest.raises(ValueError, match="centipawns and mate"):
        Evaluation(centipawns=25, mate=3)


def test_analysis_artifact_serializes_nested_dataclasses_and_mate_separately():
    manifest = SourceManifest(
        source_kind="local",
        source_ref="/tmp/game.pgn",
        files=[SourceFile(path="game.pgn", sha256="a" * 64, game_count=1)],
    )
    artifact = AnalysisArtifact(
        source_manifest=manifest,
        engine=EngineInfo(
            name="Stockfish",
            version="17",
            path="/usr/bin/stockfish",
            sha256="b" * 64,
        ),
        settings={"depth": 18},
        games=[
            GameAnalysis(
                game_id="game-1",
                headers={"White": "Alice", "Black": "Bob"},
                result="1-0",
                plies=[
                    PlyAnalysis(
                        ply=1,
                        fen="fen",
                        move_san="e4",
                        move_uci="e2e4",
                        evaluation=Evaluation(centipawns=None, mate=3),
                        depth=18,
                        phase="opening",
                    )
                ],
                critical_positions=[
                    CriticalPosition(
                        game_id="game-1",
                        ply=1,
                        loss_centipawns=None,
                        label="forced_mate",
                        reason="A forced mate is available.",
                        deep_evaluation=Evaluation(centipawns=None, mate=3),
                    )
                ],
            )
        ],
        aggregate=ArchiveAggregate(
            games_total=1,
            games_completed=1,
            critical_positions=1,
        ),
        complete=True,
    )

    serialized = artifact.to_dict()

    assert serialized["schema_version"] == "analysis.v1"
    assert serialized["games"][0]["plies"][0]["evaluation"] == {
        "centipawns": None,
        "mate": 3,
    }
    assert serialized["games"][0]["critical_positions"][0]["deep_evaluation"] == {
        "centipawns": None,
        "mate": 3,
    }
    assert serialized["aggregate"]["by_phase"] == {
        "opening": 0,
        "middlegame": 0,
        "endgame": 0,
    }


def test_analysis_artifact_schema_version_cannot_be_overridden():
    manifest = SourceManifest(
        source_kind="local",
        source_ref="/tmp/game.pgn",
        files=[],
    )
    engine = EngineInfo(
        name="Stockfish",
        version="17",
        path="/usr/bin/stockfish",
        sha256="b" * 64,
    )

    with pytest.raises(TypeError):
        AnalysisArtifact(
            source_manifest=manifest,
            engine=engine,
            settings={},
            games=[],
            aggregate=ArchiveAggregate(),
            complete=True,
            schema_version="analysis.v2",
        )

    artifact = AnalysisArtifact(
        source_manifest=manifest,
        engine=engine,
        settings={},
        games=[],
        aggregate=ArchiveAggregate(),
        complete=True,
    )
    assert artifact.to_dict()["schema_version"] == "analysis.v1"
