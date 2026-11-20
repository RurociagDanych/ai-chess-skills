"""Dataclasses for analysis artifacts and their JSON form."""

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Literal


def utc_now() -> str:
    """Return the current UTC time as an ISO 8601 string."""
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True, slots=True)
class SourceFile:
    """A PGN file recorded in a manifest with its hash and game count."""
    path: str
    sha256: str
    game_count: int
    source_url: str | None = None
    etag: str | None = None
    last_modified: str | None = None

    def to_dict(self) -> dict[str, object]:
        """Return the record as a plain dict."""
        return asdict(self)


@dataclass(frozen=True, slots=True)
class SourceManifest:
    """Provenance record for the PGN files a run used."""
    source_kind: Literal["local", "chesscom", "lichess"]
    source_ref: str
    files: list[SourceFile]
    schema_version: str = field(default="manifest.v1", init=False)
    created_at: str = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, object]:
        """Return the manifest as a plain dict."""
        return asdict(self)


@dataclass(frozen=True, slots=True)
class EngineInfo:
    """Identity and options of the engine used for analysis."""
    name: str
    version: str
    path: str
    sha256: str
    options: dict[str, object] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        """Return the engine info as a plain dict."""
        return asdict(self)


@dataclass(frozen=True, slots=True)
class Evaluation:
    """Engine score as either centipawns or a mate distance, never both."""
    centipawns: int | None
    mate: int | None

    def __post_init__(self) -> None:
        if self.centipawns is not None and self.mate is not None:
            raise ValueError("centipawns and mate cannot both be set")

    def to_dict(self) -> dict[str, object]:
        """Return the evaluation as a plain dict."""
        return asdict(self)


@dataclass(frozen=True, slots=True)
class PlyAnalysis:
    """Engine analysis of the position after one ply."""
    ply: int
    fen: str
    move_san: str
    move_uci: str
    evaluation: Evaluation
    depth: int
    phase: Literal["opening", "middlegame", "endgame"]
    pv: list[str] = field(default_factory=list)
    # Eval of the resulting position (after the move is played), from White's
    # perspective. `evaluation` is the position BEFORE the move; this is AFTER,
    # which is what should be shown alongside the post-move board.
    evaluation_after: Evaluation | None = None

    def to_dict(self) -> dict[str, object]:
        """Return the ply analysis as a plain dict."""
        return asdict(self)


@dataclass(frozen=True, slots=True)
class CriticalPosition:
    """A ply flagged as a significant mistake."""
    game_id: str
    ply: int
    loss_centipawns: int | None
    label: str
    reason: str
    deep_evaluation: Evaluation | None = None

    def to_dict(self) -> dict[str, object]:
        """Return the critical position as a plain dict."""
        return asdict(self)


@dataclass(frozen=True, slots=True)
class GameAnalysis:
    """Analysis results for one game."""
    game_id: str
    headers: dict[str, str]
    result: str
    plies: list[PlyAnalysis]
    critical_positions: list[CriticalPosition]
    error: str | None = None

    def to_dict(self) -> dict[str, object]:
        """Return the game analysis as a plain dict."""
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ArchiveAggregate:
    """Counts summarizing analysis across an archive of games."""
    games_total: int = 0
    games_completed: int = 0
    critical_positions: int = 0
    by_phase: dict[str, int] = field(
        default_factory=lambda: {
            "opening": 0,
            "middlegame": 0,
            "endgame": 0,
        }
    )
    evidence: list[dict[str, object]] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        """Return the aggregate as a plain dict."""
        return asdict(self)


@dataclass(frozen=True, slots=True)
class AnalysisArtifact:
    """Complete analysis output: source, engine, settings, games and aggregate."""
    source_manifest: SourceManifest
    engine: EngineInfo
    settings: dict[str, object]
    games: list[GameAnalysis]
    aggregate: ArchiveAggregate
    complete: bool
    errors: list[dict[str, str]] = field(default_factory=list)
    schema_version: str = field(default="analysis.v1", init=False)
    created_at: str = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, object]:
        """Return the artifact as a plain dict."""
        return asdict(self)
