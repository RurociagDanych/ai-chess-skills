import hashlib
import json
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Callable, Literal, Sequence

import chess
import chess.engine
import chess.pgn

from ai_chess.errors import AppError, ErrorCode
from ai_chess.io import atomic_write_json
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
from ai_chess.pgn import ImportedGame, _game_id
from ai_chess.stockfish import probe_engine


@dataclass(frozen=True, slots=True)
class AnalysisSettings:
    depth: int = 16
    deep_depth: int = 22
    threads: int = 1
    hash_mb: int = 128
    timeout_seconds: float = 30.0
    critical_threshold_cp: int = 150
    max_critical_positions: int = 8


@dataclass(frozen=True, slots=True)
class PlyEvidence:
    game_id: str
    ply: int
    phase: Literal["opening", "middlegame", "endgame"]
    before: dict[str, int | None]
    after: dict[str, int | None]


def normalize_score(score: chess.engine.PovScore | None) -> dict[str, int | None]:
    if score is None:
        return {"centipawns": None, "mate": None}

    white_score = score.white()
    if white_score.is_mate():
        return {"centipawns": None, "mate": white_score.mate()}

    return {"centipawns": white_score.score(), "mate": None}


def _phase_for_ply(ply: int) -> Literal["opening", "middlegame", "endgame"]:
    index = ply - 1
    if index <= 19:
        return "opening"
    if index <= 59:
        return "middlegame"
    return "endgame"


def _evaluation_from_dict(score: dict[str, int | None]) -> Evaluation:
    return Evaluation(
        centipawns=score["centipawns"],
        mate=score["mate"],
    )


def _mover_loss_centipawns(
    ply: int,
    before: dict[str, int | None],
    after: dict[str, int | None],
) -> int | None:
    before_cp = before["centipawns"]
    after_cp = after["centipawns"]
    if before_cp is None or after_cp is None:
        return None
    if ply % 2 == 1:
        return before_cp - after_cp
    return after_cp - before_cp


def _mate_category_for_mover(ply: int, mate: int | None) -> int:
    if mate is None:
        return 0
    if ply % 2 == 1:
        return 1 if mate > 0 else -1
    return 1 if mate < 0 else -1


def _is_mate_transition_for_mover(
    ply: int,
    before: dict[str, int | None],
    after: dict[str, int | None],
) -> bool:
    return _mate_category_for_mover(ply, after["mate"]) < _mate_category_for_mover(
        ply,
        before["mate"],
    )


def select_critical_positions(
    plies: list[PlyEvidence],
    threshold: int,
    limit: int,
) -> list[CriticalPosition]:
    critical: list[tuple[int, int, CriticalPosition]] = []

    for ply in plies:
        mate_transition = _is_mate_transition_for_mover(
            ply.ply,
            ply.before,
            ply.after,
        )
        loss_centipawns = _mover_loss_centipawns(ply.ply, ply.before, ply.after)
        if not mate_transition and (
            loss_centipawns is None or loss_centipawns < threshold
        ):
            continue

        if mate_transition:
            position = CriticalPosition(
                game_id=ply.game_id,
                ply=ply.ply,
                loss_centipawns=None,
                label="mate_transition",
                reason="The move changed the mate outlook against the mover.",
            )
            critical.append((1, threshold + 1, position))
            continue

        position = CriticalPosition(
            game_id=ply.game_id,
            ply=ply.ply,
            loss_centipawns=loss_centipawns,
            label="mistake",
            reason=f"The move lost {loss_centipawns} centipawns for the mover.",
        )
        critical.append((0, loss_centipawns, position))

    critical.sort(key=lambda item: (-item[0], -item[1], item[2].ply))
    return [position for _, _, position in critical[:limit]]


def _analysis_info(
    engine: chess.engine.SimpleEngine,
    board: chess.Board,
    depth: int,
) -> dict[str, object]:
    return engine.analyse(
        board,
        chess.engine.Limit(depth=depth),
        info=chess.engine.INFO_SCORE | chess.engine.INFO_PV,
    )


def _pv_to_uci(info: dict[str, object]) -> list[str]:
    pv = info.get("pv")
    if not isinstance(pv, list):
        return []
    return [move.uci() for move in pv]


def _source_manifest_for_game(game_id: str) -> SourceManifest:
    return SourceManifest(
        source_kind="local",
        source_ref=game_id,
        files=[
            SourceFile(
                path=f"{game_id}.pgn",
                sha256=game_id,
                game_count=1,
            )
        ],
    )


def _source_manifest_for_archive(games: Sequence[ImportedGame]) -> SourceManifest:
    digest = hashlib.sha256()
    for game_id in sorted(game.game_id for game in games):
        digest.update(game_id.encode("utf-8"))

    return SourceManifest(
        source_kind="local",
        source_ref="archive",
        files=[
            SourceFile(
                path="archive.pgn",
                sha256=digest.hexdigest(),
                game_count=len(games),
            )
        ],
    )


def _invalid_artifact(message: str, details: str) -> AppError:
    return AppError(
        code=ErrorCode.INVALID_INPUT,
        message=message,
        remedy=details,
    )


def _require_mapping(value: object, context: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise _invalid_artifact(
            f"Could not read analysis artifact at {context}.",
            "Ensure the file contains a valid analysis.v1 JSON object.",
        )
    return value


def _require_list(value: object, context: str) -> list[object]:
    if not isinstance(value, list):
        raise _invalid_artifact(
            f"Could not read analysis artifact at {context}.",
            "Ensure the file contains a valid analysis.v1 JSON object.",
        )
    return value


def _require_str(value: object, context: str) -> str:
    if not isinstance(value, str):
        raise _invalid_artifact(
            f"Could not read analysis artifact at {context}.",
            "Ensure the file contains a valid analysis.v1 JSON object.",
        )
    return value


def _require_int_or_none(value: object, context: str) -> int | None:
    if value is None or isinstance(value, int):
        return value
    raise _invalid_artifact(
        f"Could not read analysis artifact at {context}.",
        "Ensure the file contains a valid analysis.v1 JSON object.",
    )


def _require_int(value: object, context: str) -> int:
    if isinstance(value, int):
        return value
    raise _invalid_artifact(
        f"Could not read analysis artifact at {context}.",
        "Ensure the file contains a valid analysis.v1 JSON object.",
    )


def _require_bool(value: object, context: str) -> bool:
    if not isinstance(value, bool):
        raise _invalid_artifact(
            f"Could not read analysis artifact at {context}.",
            "Ensure the file contains a valid analysis.v1 JSON object.",
        )
    return value


def _parse_evaluation(data: object, context: str) -> Evaluation:
    payload = _require_mapping(data, context)
    return Evaluation(
        centipawns=_require_int_or_none(payload.get("centipawns"), f"{context}.centipawns"),
        mate=_require_int_or_none(payload.get("mate"), f"{context}.mate"),
    )


def _parse_ply(data: object, context: str) -> PlyAnalysis:
    payload = _require_mapping(data, context)
    pv = payload.get("pv", [])
    if not isinstance(pv, list) or not all(isinstance(move, str) for move in pv):
        raise _invalid_artifact(
            f"Could not read analysis artifact at {context}.pv.",
            "Ensure the file contains a valid analysis.v1 JSON object.",
        )
    phase = _require_str(payload.get("phase"), f"{context}.phase")
    return PlyAnalysis(
        ply=_require_int(payload.get("ply"), f"{context}.ply"),
        fen=_require_str(payload.get("fen"), f"{context}.fen"),
        move_san=_require_str(payload.get("move_san"), f"{context}.move_san"),
        move_uci=_require_str(payload.get("move_uci"), f"{context}.move_uci"),
        evaluation=_parse_evaluation(payload.get("evaluation"), f"{context}.evaluation"),
        depth=_require_int(payload.get("depth"), f"{context}.depth"),
        phase=phase,  # type: ignore[arg-type]
        pv=pv,
        evaluation_after=(
            _parse_evaluation(payload.get("evaluation_after"), f"{context}.evaluation_after")
            if payload.get("evaluation_after") is not None
            else None
        ),
    )


def _parse_critical_position(data: object, context: str) -> CriticalPosition:
    payload = _require_mapping(data, context)
    deep_evaluation = payload.get("deep_evaluation")
    return CriticalPosition(
        game_id=_require_str(payload.get("game_id"), f"{context}.game_id"),
        ply=_require_int(payload.get("ply"), f"{context}.ply"),
        loss_centipawns=_require_int_or_none(
            payload.get("loss_centipawns"),
            f"{context}.loss_centipawns",
        ),
        label=_require_str(payload.get("label"), f"{context}.label"),
        reason=_require_str(payload.get("reason"), f"{context}.reason"),
        deep_evaluation=(
            _parse_evaluation(deep_evaluation, f"{context}.deep_evaluation")
            if deep_evaluation is not None
            else None
        ),
    )


def _parse_game(data: object, context: str) -> GameAnalysis:
    payload = _require_mapping(data, context)
    headers = payload.get("headers", {})
    if not isinstance(headers, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in headers.items()
    ):
        raise _invalid_artifact(
            f"Could not read analysis artifact at {context}.headers.",
            "Ensure the file contains a valid analysis.v1 JSON object.",
        )
    error = payload.get("error")
    if error is not None and not isinstance(error, str):
        raise _invalid_artifact(
            f"Could not read analysis artifact at {context}.error.",
            "Ensure the file contains a valid analysis.v1 JSON object.",
        )
    return GameAnalysis(
        game_id=_require_str(payload.get("game_id"), f"{context}.game_id"),
        headers=headers,
        result=_require_str(payload.get("result"), f"{context}.result"),
        plies=[
            _parse_ply(item, f"{context}.plies[{index}]")
            for index, item in enumerate(_require_list(payload.get("plies", []), f"{context}.plies"))
        ],
        critical_positions=[
            _parse_critical_position(item, f"{context}.critical_positions[{index}]")
            for index, item in enumerate(
                _require_list(payload.get("critical_positions", []), f"{context}.critical_positions")
            )
        ],
        error=error,
    )


def _parse_engine_info(data: object, context: str) -> EngineInfo:
    payload = _require_mapping(data, context)
    options = payload.get("options", {})
    if not isinstance(options, dict):
        raise _invalid_artifact(
            f"Could not read analysis artifact at {context}.options.",
            "Ensure the file contains a valid analysis.v1 JSON object.",
        )
    return EngineInfo(
        name=_require_str(payload.get("name"), f"{context}.name"),
        version=_require_str(payload.get("version"), f"{context}.version"),
        path=_require_str(payload.get("path"), f"{context}.path"),
        sha256=_require_str(payload.get("sha256"), f"{context}.sha256"),
        options=options,
    )


def _load_existing_artifact(output_path: Path) -> AnalysisArtifact:
    try:
        payload = json.loads(output_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise _invalid_artifact(
            f"Could not read analysis artifact at {output_path}.",
            "Ensure the file contains valid analysis.v1 JSON before resuming.",
        ) from error

    data = _require_mapping(payload, str(output_path))
    if data.get("schema_version") != "analysis.v1":
        raise _invalid_artifact(
            f"Could not read analysis artifact at {output_path}.",
            "Ensure the file contains valid analysis.v1 JSON before resuming.",
        )

    source_manifest = _require_mapping(data.get("source_manifest"), "source_manifest")
    source_kind = _require_str(source_manifest.get("source_kind"), "source_manifest.source_kind")
    if source_kind not in {"local", "chesscom"}:
        raise _invalid_artifact(
            "Could not read analysis artifact at source_manifest.source_kind.",
            "Ensure the file contains a valid analysis.v1 JSON object.",
        )
    files = [
        SourceFile(
            path=_require_str(item.get("path"), f"source_manifest.files[{index}].path"),
            sha256=_require_str(item.get("sha256"), f"source_manifest.files[{index}].sha256"),
            game_count=_require_int(
                item.get("game_count"),
                f"source_manifest.files[{index}].game_count",
            ),
            source_url=(
                _require_str(item.get("source_url"), f"source_manifest.files[{index}].source_url")
                if item.get("source_url") is not None
                else None
            ),
            etag=(
                _require_str(item.get("etag"), f"source_manifest.files[{index}].etag")
                if item.get("etag") is not None
                else None
            ),
            last_modified=(
                _require_str(
                    item.get("last_modified"),
                    f"source_manifest.files[{index}].last_modified",
                )
                if item.get("last_modified") is not None
                else None
            ),
        )
        for index, item in enumerate(
            (
                _require_mapping(file_data, f"source_manifest.files[{index}]")
                for index, file_data in enumerate(
                    _require_list(source_manifest.get("files"), "source_manifest.files")
                )
            )
        )
    ]

    return AnalysisArtifact(
        source_manifest=SourceManifest(
            source_kind=source_kind,  # type: ignore[arg-type]
            source_ref=_require_str(source_manifest.get("source_ref"), "source_manifest.source_ref"),
            files=files,
        ),
        engine=_parse_engine_info(data.get("engine"), "engine"),
        settings=_require_mapping(data.get("settings"), "settings"),
        games=[
            _parse_game(item, f"games[{index}]")
            for index, item in enumerate(_require_list(data.get("games"), "games"))
        ],
        aggregate=ArchiveAggregate(),
        complete=_require_bool(data.get("complete"), "complete"),
        errors=[
            {
                "code": _require_str(item.get("code"), f"errors[{index}].code"),
                "message": _require_str(item.get("message"), f"errors[{index}].message"),
            }
            for index, item in enumerate(
                (
                    _require_mapping(error_data, f"errors[{index}]")
                    for index, error_data in enumerate(
                        _require_list(data.get("errors", []), "errors")
                    )
                )
            )
        ],
    )


def aggregate_games(games: Sequence[GameAnalysis]) -> ArchiveAggregate:
    by_phase = {"opening": 0, "middlegame": 0, "endgame": 0}
    evidence: list[dict[str, object]] = []

    for game in games:
        phases_by_ply = {ply.ply: ply.phase for ply in game.plies}
        for position in game.critical_positions:
            phase = phases_by_ply.get(position.ply, _phase_for_ply(position.ply))
            by_phase[phase] += 1
            evidence.append({"game_id": position.game_id, "ply": position.ply})

    return ArchiveAggregate(
        games_total=len(games),
        games_completed=len(games),
        critical_positions=sum(len(game.critical_positions) for game in games),
        by_phase=by_phase,
        evidence=evidence,
    )


def _persist_archive_artifact(
    output_path: Path,
    source_manifest: SourceManifest,
    engine: EngineInfo,
    settings: AnalysisSettings,
    completed_games: Sequence[GameAnalysis],
    games_total: int,
    errors: list[dict[str, str]],
) -> AnalysisArtifact:
    aggregate = aggregate_games(completed_games)
    aggregate = replace(
        aggregate,
        games_total=games_total,
        games_completed=len(completed_games),
    )
    artifact = AnalysisArtifact(
        source_manifest=source_manifest,
        engine=engine,
        settings=asdict(settings),
        games=list(completed_games),
        aggregate=aggregate,
        complete=len(completed_games) == games_total and not errors,
        errors=errors,
    )
    atomic_write_json(output_path, artifact.to_dict())
    return artifact


def analyze_game(
    game: ImportedGame | chess.pgn.Game,
    engine_command: Sequence[str],
    settings: AnalysisSettings,
) -> AnalysisArtifact:
    imported = game if isinstance(game, ImportedGame) else None
    raw_game = imported.game if imported is not None else game
    game_id = imported.game_id if imported is not None else _game_id(raw_game)

    engine_info = probe_engine(engine_command, timeout=settings.timeout_seconds)
    engine = chess.engine.SimpleEngine.popen_uci(list(engine_command))

    try:
        options: dict[str, int] = {}
        if "Threads" in engine.options:
            options["Threads"] = settings.threads
        if "Hash" in engine.options:
            options["Hash"] = settings.hash_mb
        if options:
            engine.configure(options)

        board = raw_game.board()
        plies: list[PlyAnalysis] = []
        evidence: list[PlyEvidence] = []
        prefixes: dict[int, list[chess.Move]] = {}

        for ply_number, move in enumerate(raw_game.mainline_moves(), start=1):
            prefixes[ply_number] = list(board.move_stack)
            before_info = _analysis_info(engine, board, settings.depth)
            before_score = normalize_score(before_info.get("score"))
            before_fen = board.fen()
            san = board.san(move)
            phase = _phase_for_ply(ply_number)

            pv = _pv_to_uci(before_info)
            depth = int(before_info.get("depth", settings.depth))

            board.push(move)
            after_score = normalize_score(
                _analysis_info(engine, board, settings.depth).get("score")
            )

            plies.append(
                PlyAnalysis(
                    ply=ply_number,
                    fen=before_fen,
                    move_san=san,
                    move_uci=move.uci(),
                    evaluation=_evaluation_from_dict(before_score),
                    depth=depth,
                    phase=phase,
                    pv=pv,
                    evaluation_after=_evaluation_from_dict(after_score),
                )
            )

            evidence.append(
                PlyEvidence(
                    game_id=game_id,
                    ply=ply_number,
                    phase=phase,
                    before=before_score,
                    after=after_score,
                )
            )

        critical_positions = select_critical_positions(
            evidence,
            threshold=settings.critical_threshold_cp,
            limit=settings.max_critical_positions,
        )

        aggregate_evidence: list[dict[str, object]] = []
        if settings.deep_depth is not None and settings.deep_depth > settings.depth:
            deepened: list[CriticalPosition] = []
            for position in critical_positions:
                aggregate_evidence.append(
                    {"game_id": position.game_id, "ply": position.ply, "depth": settings.depth}
                )
                replay = chess.Board()
                for move in prefixes[position.ply]:
                    replay.push(move)
                deepened.append(
                    replace(
                        position,
                        deep_evaluation=_evaluation_from_dict(
                            normalize_score(
                                _analysis_info(engine, replay, settings.deep_depth).get(
                                    "score"
                                )
                            )
                        ),
                    )
                )
                aggregate_evidence.append(
                    {
                        "game_id": position.game_id,
                        "ply": position.ply,
                        "depth": settings.deep_depth,
                    }
                )
            critical_positions = deepened

        by_phase = {"opening": 0, "middlegame": 0, "endgame": 0}
        for ply in plies:
            by_phase[ply.phase] += 1

        return AnalysisArtifact(
            source_manifest=_source_manifest_for_game(game_id),
            engine=engine_info,
            settings=asdict(settings),
            games=[
                GameAnalysis(
                    game_id=game_id,
                    headers=dict(raw_game.headers),
                    result=raw_game.headers.get("Result", "*"),
                    plies=plies,
                    critical_positions=critical_positions,
                )
            ],
            aggregate=ArchiveAggregate(
                games_total=1,
                games_completed=1,
                critical_positions=len(critical_positions),
                by_phase=by_phase,
                evidence=aggregate_evidence,
            ),
            complete=True,
        )
    finally:
        try:
            engine.quit()
        finally:
            engine.close()


def analyze_archive(
    games: Sequence[ImportedGame],
    engine_command: Sequence[str],
    settings: AnalysisSettings,
    output_path: Path,
    _analyze_game: Callable[
        [ImportedGame, Sequence[str], AnalysisSettings],
        AnalysisArtifact,
    ] = analyze_game,
) -> AnalysisArtifact:
    source_manifest = _source_manifest_for_archive(games)
    existing = _load_existing_artifact(output_path) if output_path.exists() else None

    completed_by_id: dict[str, GameAnalysis] = {}
    engine_info: EngineInfo | None = None
    if existing is not None:
        completed_by_id = {game.game_id: game for game in existing.games}
        engine_info = existing.engine

    errors: list[dict[str, str]] = []

    for game in games:
        if game.game_id in completed_by_id:
            continue

        try:
            artifact = _analyze_game(game, engine_command, settings)
        except Exception as error:
            errors.append(
                {
                    "code": ErrorCode.PARTIAL_ANALYSIS.value,
                    "message": f"Game {game.game_id} failed: {error}",
                }
            )
            if engine_info is not None:
                _persist_archive_artifact(
                    output_path,
                    source_manifest,
                    engine_info,
                    settings,
                    [completed_by_id[item.game_id] for item in games if item.game_id in completed_by_id],
                    len(games),
                    errors,
                )
            continue

        engine_info = artifact.engine
        if artifact.games:
            completed_by_id[artifact.games[0].game_id] = artifact.games[0]
        _persist_archive_artifact(
            output_path,
            source_manifest,
            engine_info,
            settings,
            [completed_by_id[item.game_id] for item in games if item.game_id in completed_by_id],
            len(games),
            errors,
        )

    ordered_games = [
        completed_by_id[item.game_id] for item in games if item.game_id in completed_by_id
    ]
    if engine_info is None:
        raise _invalid_artifact(
            f"Could not analyze archive into {output_path}.",
            "Ensure at least one game can be analyzed or resume from a valid partial artifact.",
        )

    return _persist_archive_artifact(
        output_path,
        source_manifest,
        engine_info,
        settings,
        ordered_games,
        len(games),
        errors,
    )
