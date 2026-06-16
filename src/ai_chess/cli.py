import argparse
import json
from datetime import datetime, timezone
import os
import platform
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

import httpx
from platformdirs import user_data_path

from ai_chess import __version__
from ai_chess.analysis import AnalysisSettings, analyze_archive, analyze_game
from ai_chess.chesscom import ChessComClient, iter_months
from ai_chess.errors import AppError, ErrorCode
from ai_chess.io import atomic_write_json
from ai_chess.lichess import LichessClient, parse_game_link as parse_lichess_game_link
from ai_chess.models import EngineInfo
from ai_chess.pgn import import_pgn, read_games, select_latest_index
from ai_chess.reports import write_report
from ai_chess.stockfish import (
    build_install_plan,
    detect_platform,
    discover_engine,
    install_engine,
)


@dataclass(frozen=True, slots=True)
class CommandResult:
    status: str
    payload: dict[str, object]


class _CliParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise AppError(
            ErrorCode.INVALID_INPUT,
            message,
            "Run `ai-chess --help` for valid command usage.",
        )


def _cache_root() -> Path:
    return Path(user_data_path("ai-chess"))


def _engine_cache_dir() -> Path:
    return _cache_root() / "engines"


def _chesscom_cache_dir() -> Path:
    return _cache_root() / "cache" / "chesscom"


def _engine_command() -> list[str]:
    engine = discover_engine(None, _engine_cache_dir())
    if engine is None:
        raise AppError(
            ErrorCode.ENGINE_MISSING,
            "No Stockfish executable was found.",
            "Run `ai-chess setup-engine --plan-only` to inspect the install plan.",
        )
    return [engine.path]


def _make_chesscom_client(user_agent: str) -> ChessComClient:
    return ChessComClient(httpx.Client(), user_agent, _chesscom_cache_dir())


def _make_lichess_client(user_agent: str) -> LichessClient:
    return LichessClient(httpx.Client(), user_agent)


def _json_dump(payload: object) -> str:
    return json.dumps(payload, ensure_ascii=True, separators=(",", ":"))


def _print_success(result: CommandResult) -> None:
    print(_json_dump({"ok": True, "result": result.payload}))
    if result.status:
        print(result.status, file=sys.stderr)


def _print_failure(error: AppError) -> None:
    print(_json_dump({"ok": False, "error": error.to_dict()}))
    print(error.message, file=sys.stderr)


def _json_path(path: Path) -> str:
    return path.as_posix()


def _analysis_settings(args: argparse.Namespace) -> AnalysisSettings:
    numeric_values = {
        "depth": args.depth,
        "deep_depth": args.deep_depth,
        "threads": args.threads,
        "hash_mb": args.hash_mb,
        "timeout_seconds": args.timeout_seconds,
    }
    for name, value in numeric_values.items():
        if value is None:
            continue
        if value <= 0:
            raise AppError(
                ErrorCode.INVALID_INPUT,
                f"{name} must be greater than zero.",
                "Use positive analysis limits and retry.",
            )
    return AnalysisSettings(
        depth=args.depth,
        deep_depth=args.deep_depth,
        threads=args.threads,
        hash_mb=args.hash_mb,
        timeout_seconds=args.timeout_seconds,
    )


def _validate_fetch_args(args: argparse.Namespace) -> None:
    has_month = args.month is not None
    has_range = args.month_from is not None or args.month_to is not None
    if has_month and has_range:
        raise AppError(
            ErrorCode.INVALID_INPUT,
            "Choose either --month or --from/--to for Chess.com fetches.",
            "Run `ai-chess --help` for valid command usage.",
        )
    if not has_month and not has_range:
        raise AppError(
            ErrorCode.INVALID_INPUT,
            "Provide either --month or both --from and --to.",
            "Run `ai-chess --help` for valid command usage.",
        )
    if args.month_from is None or args.month_to is None:
        if has_range:
            raise AppError(
                ErrorCode.INVALID_INPUT,
                "Both --from and --to are required for a month range.",
                "Run `ai-chess --help` for valid command usage.",
            )


def _require_command(args: argparse.Namespace) -> None:
    if args.show_version:
        return
    if getattr(args, "command", None) is None:
        raise AppError(
            ErrorCode.INVALID_INPUT,
            "A subcommand is required.",
            "Run `ai-chess --help` for valid command usage.",
        )
    if args.command == "fetch" and getattr(args, "fetch_source", None) is None:
        raise AppError(
            ErrorCode.INVALID_INPUT,
            "A fetch source is required.",
            "Run `ai-chess fetch --help` for valid command usage.",
        )
    if args.command == "analyze" and getattr(args, "analyze_target", None) is None:
        raise AppError(
            ErrorCode.INVALID_INPUT,
            "An analysis target is required.",
            "Run `ai-chess analyze --help` for valid command usage.",
        )
    if args.command == "review" and getattr(args, "review_source", None) is None:
        raise AppError(
            ErrorCode.INVALID_INPUT,
            "A review source is required.",
            "Run `ai-chess review --help` for valid command usage.",
        )
    if args.command == "fetch" and args.fetch_source == "chesscom":
        _validate_fetch_args(args)


def _run_doctor(_args: argparse.Namespace) -> CommandResult:
    cache_path = _cache_root() / "cache"
    cache_path.mkdir(parents=True, exist_ok=True)
    platform_payload: dict[str, object]
    try:
        target = detect_platform()
    except AppError as error:
        platform_payload = {
            "supported": False,
            "error": error.to_dict(),
        }
    else:
        platform_payload = {
            "supported": True,
            "os": target.os,
            "arch": target.arch,
            "cpu_flags": sorted(target.cpu_flags),
        }

    engine = discover_engine(None, _engine_cache_dir())
    stockfish_payload = (
        None
        if engine is None
        else {
            "path": engine.path,
            "version": engine.version,
        }
    )
    return CommandResult(
        status="doctor ok",
        payload={
            "python_version": platform.python_version(),
            "package_version": __version__,
            "cache_path": str(cache_path),
            "platform": platform_payload,
            "stockfish": stockfish_payload,
        },
    )


def _plan_payload(plan) -> dict[str, object]:
    return {
        "version": plan.version,
        "target": {
            "os": plan.target.os,
            "arch": plan.target.arch,
            "cpu_flags": sorted(plan.target.cpu_flags),
        },
        "asset": {
            "name": plan.asset.name,
            "url": plan.asset.url,
            "digest": plan.asset.digest,
            "size": plan.asset.size,
        },
        "install_dir": _json_path(plan.install_dir),
        "executable_path": _json_path(plan.executable_path),
    }


def _run_setup_engine(args: argparse.Namespace) -> CommandResult:
    target = detect_platform()
    plan = build_install_plan(args.version, target, args.install_dir)
    if args.plan_only:
        return CommandResult(status="setup plan ready", payload=_plan_payload(plan))
    if not args.approve_download:
        raise AppError(
            ErrorCode.APPROVAL_REQUIRED,
            "Stockfish installation requires explicit approval.",
            "Pass --approve-download after reviewing the plan.",
        )
    engine = install_engine(plan, approved=True)
    return CommandResult(
        status="engine installed",
        payload={
            "name": engine.name,
            "version": engine.version,
            "path": engine.path,
        },
    )


def _user_agent(args: argparse.Namespace) -> str:
    value = args.user_agent or os.environ.get("AI_CHESS_USER_AGENT")
    if value is None:
        raise AppError(
            ErrorCode.INVALID_INPUT,
            "Chess.com fetches require an identifiable User-Agent.",
            "Pass --user-agent or set AI_CHESS_USER_AGENT.",
        )
    return value


def _lichess_user_agent(args: argparse.Namespace) -> str:
    value = args.user_agent or os.environ.get("AI_CHESS_USER_AGENT")
    if value is None or not value.strip():
        return f"ai-chess-skills/{__version__}"
    return value.strip()


def _run_fetch_chesscom(args: argparse.Namespace) -> CommandResult:
    months = [args.month] if args.month is not None else iter_months(args.month_from, args.month_to)
    client = _make_chesscom_client(_user_agent(args))
    try:
        result = client.fetch_months(args.username, months, args.output_dir)
    finally:
        http_client = getattr(client, "client", None)
        if http_client is not None:
            http_client.close()
    return CommandResult(
        status="fetch ok",
        payload={
            "manifest_path": str(result.manifest_path),
            "pgn_paths": [str(path) for path in result.pgn_paths],
            "months": months,
        },
    )


def _run_fetch_lichess(args: argparse.Namespace) -> CommandResult:
    client = _make_lichess_client(_lichess_user_agent(args))
    try:
        result = client.fetch_user_games(
            args.username,
            args.output_dir,
            max_games=args.max_games,
        )
    finally:
        http_client = getattr(client, "client", None)
        if http_client is not None:
            http_client.close()
    return CommandResult(
        status="fetch ok",
        payload={
            "manifest_path": str(result.manifest_path),
            "pgn_path": str(result.pgn_path),
            "provenance_path": str(result.provenance_path),
        },
    )


def _run_import(args: argparse.Namespace) -> CommandResult:
    result = import_pgn(args.path, args.output_dir)
    return CommandResult(
        status="import ok",
        payload={
            "manifest_path": str(result.manifest_path),
            "pgn_path": str(result.pgn_path),
            "game_count": len(result.games),
        },
    )


def _run_analyze_game(args: argparse.Namespace) -> CommandResult:
    settings = _analysis_settings(args)
    if args.game <= 0:
        raise AppError(
            ErrorCode.INVALID_INPUT,
            "--game must be greater than zero.",
            "Choose a 1-based game index.",
        )
    with tempfile.TemporaryDirectory(prefix="ai-chess-cli-import-") as temporary:
        imported = import_pgn(args.path, Path(temporary))
    index = args.game - 1
    if index < 0 or index >= len(imported.games):
        raise AppError(
            ErrorCode.INVALID_INPUT,
            f"Game index {args.game} is out of range for {args.path}.",
            "Choose a 1-based game index within the imported archive.",
        )
    artifact = analyze_game(
        imported.games[index],
        _engine_command(),
        settings,
    )
    artifact_path = args.path.with_suffix(f".game-{args.game}.analysis.json")
    atomic_write_json(artifact_path, artifact.to_dict())
    game = artifact.games[0]
    return CommandResult(
        status="analysis ok",
        payload={
            "artifact_path": str(artifact_path),
            "game_id": game.game_id,
            "critical_positions": len(game.critical_positions),
            "complete": artifact.complete,
        },
    )


def _run_analyze_archive(args: argparse.Namespace) -> CommandResult:
    settings = _analysis_settings(args)
    artifact = analyze_archive(
        read_games(args.path),
        _engine_command(),
        settings,
        args.output,
    )
    return CommandResult(
        status="archive analysis ok",
        payload={
            "artifact_path": str(args.output),
            "games_total": artifact.aggregate.games_total,
            "games_completed": artifact.aggregate.games_completed,
            "complete": artifact.complete,
        },
    )


def _ensure_engine() -> tuple[EngineInfo, bool]:
    engine = discover_engine(None, _engine_cache_dir())
    if engine is not None:
        return engine, False
    target = detect_platform()
    plan = build_install_plan("latest", target, None)
    installed = install_engine(plan, approved=True)
    return installed, True


def _finish_review(
    args: argparse.Namespace,
    engine: object,
    installed: bool,
    pgn_path: Path,
    manifest_path: Path,
    games: list,
    game_number: int | None = None,
) -> CommandResult:
    # game_number is pre-selected for URL reviews; otherwise resolve it from the
    # --latest / --game selection flags.
    if game_number is None:
        if args.game is not None and args.latest:
            raise AppError(
                ErrorCode.INVALID_INPUT,
                "Choose either --latest or --game, not both.",
                "Pass only one game-selection option.",
            )
        if args.game is not None:
            if args.game <= 0 or args.game > len(games):
                raise AppError(
                    ErrorCode.INVALID_INPUT,
                    f"Game index {args.game} is out of range for the archive.",
                    "Choose a 1-based game index within the archive.",
                )
            game_number = args.game
        else:
            game_number = select_latest_index(games)

    settings = _analysis_settings(args)
    artifact = analyze_game(games[game_number - 1], [engine.path], settings)
    analysis_path = pgn_path.with_suffix(f".game-{game_number}.analysis.json")
    atomic_write_json(analysis_path, artifact.to_dict())
    report_path = args.report or pgn_path.with_suffix(f".game-{game_number}.report.html")
    write_report(analysis_path, report_path, None)

    headers = games[game_number - 1].game.headers
    selected_headers = {
        key: value
        for key in ("White", "Black", "Date", "UTCDate", "UTCTime", "Result")
        if (value := headers.get(key)) is not None
    }
    return CommandResult(
        status="review ok",
        payload={
            "engine": {
                "path": engine.path,
                "version": engine.version,
                "installed": installed,
            },
            "source": {
                "pgn_path": str(pgn_path),
                "manifest_path": str(manifest_path),
            },
            "selected_game": {"index": game_number, "headers": selected_headers},
            "analysis_path": str(analysis_path),
            "report_path": str(report_path),
        },
    )


def _run_review_chesscom(args: argparse.Namespace) -> CommandResult:
    engine, installed = _ensure_engine()
    month = args.month or datetime.now(timezone.utc).strftime("%Y-%m")
    client = _make_chesscom_client(_user_agent(args))
    try:
        result = client.fetch_months(args.username, [month], args.output_dir)
    finally:
        http_client = getattr(client, "client", None)
        if http_client is not None:
            http_client.close()
    if not result.pgn_paths:
        # Defensive: a month with no games normally raises AppError during
        # fetch, but guard here in case fetch_months returns unexpectedly empty.
        raise AppError(
            ErrorCode.INVALID_INPUT,
            f"No games were fetched for {args.username} in {month}.",
            "Choose a --month in which games were played.",
        )
    pgn_path = result.pgn_paths[0]
    games = read_games(pgn_path)
    return _finish_review(args, engine, installed, pgn_path, result.manifest_path, games)


def _game_index_for_id(games: list, game_id: str) -> int:
    """1-based index of the game whose Link/Site header ends with game_id."""
    for index, game in enumerate(games, start=1):
        headers = game.game.headers
        for key in ("Link", "Site"):
            link = headers.get(key, "") or ""
            if link.rstrip("/").rsplit("/", 1)[-1] == game_id:
                return index
    raise AppError(
        ErrorCode.INVALID_INPUT,
        f"Game {game_id} was not found in the fetched archive.",
        "Confirm the link is correct and the game is finished and public.",
    )


def _run_review_url(args: argparse.Namespace) -> CommandResult:
    engine, installed = _ensure_engine()
    try:
        lichess_game_id = parse_lichess_game_link(args.url)
    except AppError:
        lichess_game_id = None
    if lichess_game_id is not None:
        client = _make_lichess_client(_lichess_user_agent(args))
        try:
            result = client.fetch_game(lichess_game_id, args.output_dir)
        finally:
            http_client = getattr(client, "client", None)
            if http_client is not None:
                http_client.close()
        games = read_games(result.pgn_path)
        return _finish_review(
            args,
            engine,
            installed,
            result.pgn_path,
            result.manifest_path,
            games,
            game_number=1,
        )

    client = _make_chesscom_client(_user_agent(args))
    try:
        reference = client.fetch_game_reference(args.url)
        result = client.fetch_months(reference.white, [reference.month], args.output_dir)
    finally:
        http_client = getattr(client, "client", None)
        if http_client is not None:
            http_client.close()
    if not result.pgn_paths:
        raise AppError(
            ErrorCode.INVALID_INPUT,
            f"No games were fetched for {reference.white} in {reference.month}.",
            "Confirm the link points to a finished, public game.",
        )
    pgn_path = result.pgn_paths[0]
    games = read_games(pgn_path)
    game_number = _game_index_for_id(games, reference.game_id)
    return _finish_review(
        args,
        engine,
        installed,
        pgn_path,
        result.manifest_path,
        games,
        game_number=game_number,
    )


def _run_review_pgn(args: argparse.Namespace) -> CommandResult:
    engine, installed = _ensure_engine()
    output_dir = args.output_dir or args.path.parent
    imported = import_pgn(args.path, output_dir)
    return _finish_review(
        args,
        engine,
        installed,
        imported.pgn_path,
        imported.manifest_path,
        imported.games,
    )


def _run_report(args: argparse.Namespace) -> CommandResult:
    output = write_report(args.analysis, args.output, args.insights)
    return CommandResult(
        status="report ok",
        payload={"output_path": str(output)},
    )


def build_parser() -> argparse.ArgumentParser:
    parser = _CliParser(prog="ai-chess")
    parser.add_argument("--version", action="store_true", dest="show_version")
    subparsers = parser.add_subparsers(dest="command")

    doctor = subparsers.add_parser("doctor")
    doctor.set_defaults(handler=_run_doctor)

    setup = subparsers.add_parser("setup-engine")
    setup.add_argument("--version", default="latest")
    setup.add_argument("--plan-only", action="store_true")
    setup.add_argument("--approve-download", action="store_true")
    setup.add_argument("--install-dir", type=Path)
    setup.set_defaults(handler=_run_setup_engine)

    fetch = subparsers.add_parser("fetch")
    fetch_subparsers = fetch.add_subparsers(dest="fetch_source")
    chesscom = fetch_subparsers.add_parser("chesscom")
    chesscom.add_argument("username")
    chesscom.add_argument("--month")
    chesscom.add_argument("--from", dest="month_from")
    chesscom.add_argument("--to", dest="month_to")
    chesscom.add_argument("--output-dir", type=Path, required=True)
    chesscom.add_argument("--user-agent")
    chesscom.set_defaults(handler=_run_fetch_chesscom)

    lichess = fetch_subparsers.add_parser("lichess")
    lichess.add_argument("username")
    lichess.add_argument("--max", dest="max_games", type=int)
    lichess.add_argument("--output-dir", type=Path, required=True)
    lichess.add_argument("--user-agent")
    lichess.set_defaults(handler=_run_fetch_lichess)

    imported = subparsers.add_parser("import")
    imported.add_argument("path", type=Path)
    imported.add_argument("--output-dir", type=Path, required=True)
    imported.set_defaults(handler=_run_import)

    analyze = subparsers.add_parser("analyze")
    analyze_subparsers = analyze.add_subparsers(dest="analyze_target")

    def add_analysis_flags(command: argparse.ArgumentParser) -> None:
        command.add_argument("--depth", type=int, default=16)
        command.add_argument("--deep-depth", type=int, default=22)
        command.add_argument("--threads", type=int, default=1)
        command.add_argument("--hash-mb", type=int, default=128)
        command.add_argument("--timeout-seconds", type=float, default=30.0)

    def add_analysis_options(command: argparse.ArgumentParser) -> None:
        command.add_argument("path", type=Path)
        add_analysis_flags(command)

    game = analyze_subparsers.add_parser("game")
    add_analysis_options(game)
    game.add_argument("--game", type=int, default=1)
    game.set_defaults(handler=_run_analyze_game)

    archive = analyze_subparsers.add_parser("archive")
    add_analysis_options(archive)
    archive.add_argument("--output", type=Path, required=True)
    archive.set_defaults(handler=_run_analyze_archive)

    review = subparsers.add_parser("review")
    review_subparsers = review.add_subparsers(dest="review_source")

    def add_review_selection(command: argparse.ArgumentParser) -> None:
        # --latest is the default when --game is absent; the flag is ergonomic and
        # exists primarily to guard against accidental --latest --game combinations.
        command.add_argument("--latest", action="store_true")
        command.add_argument("--game", type=int, default=None)
        command.add_argument("--report", type=Path)
        add_analysis_flags(command)

    review_chesscom = review_subparsers.add_parser("chesscom")
    review_chesscom.add_argument("username")
    review_chesscom.add_argument("--month")
    review_chesscom.add_argument("--output-dir", type=Path, required=True)
    review_chesscom.add_argument("--user-agent")
    add_review_selection(review_chesscom)
    review_chesscom.set_defaults(handler=_run_review_chesscom)

    review_pgn = review_subparsers.add_parser("pgn")
    review_pgn.add_argument("path", type=Path)
    review_pgn.add_argument("--output-dir", type=Path)
    add_review_selection(review_pgn)
    review_pgn.set_defaults(handler=_run_review_pgn)

    # `review url` takes a full Chess.com or Lichess game link and reviews that exact game;
    # the link itself selects the game, so no --latest / --game flags are needed.
    review_url = review_subparsers.add_parser("url")
    review_url.add_argument("url")
    review_url.add_argument("--output-dir", type=Path, required=True)
    review_url.add_argument("--user-agent")
    review_url.add_argument("--report", type=Path)
    add_analysis_flags(review_url)
    review_url.set_defaults(handler=_run_review_url)

    report = subparsers.add_parser("report")
    report.add_argument("analysis", type=Path)
    report.add_argument("--output", type=Path, required=True)
    report.add_argument("--insights", type=Path)
    report.set_defaults(handler=_run_report)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
        if args.show_version:
            print(_json_dump({"version": __version__}))
            return 0
        _require_command(args)
        result = args.handler(args)
    except AppError as error:
        _print_failure(error)
        return 1

    _print_success(result)
    return 0


def entrypoint() -> None:
    raise SystemExit(main())
