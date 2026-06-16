import json
from pathlib import Path, PureWindowsPath
from types import SimpleNamespace

import pytest

import ai_chess.cli as cli_module
from ai_chess.analysis import AnalysisSettings
from ai_chess.errors import AppError, ErrorCode
from ai_chess.models import (
    AnalysisArtifact,
    ArchiveAggregate,
    EngineInfo,
    GameAnalysis,
    SourceFile,
    SourceManifest,
)
from ai_chess.stockfish import InstallPlan, PlatformTarget, ReleaseAsset


def _artifact(game_id: str = "game-1") -> AnalysisArtifact:
    return AnalysisArtifact(
        source_manifest=SourceManifest(
            source_kind="local",
            source_ref=game_id,
            files=[SourceFile(path=f"{game_id}.pgn", sha256="a" * 64, game_count=1)],
        ),
        engine=EngineInfo(
            name="FixtureFish",
            version="1.0",
            path="/tmp/fish",
            sha256="b" * 64,
        ),
        settings={"depth": 16},
        games=[
            GameAnalysis(
                game_id=game_id,
                headers={"White": "Alice", "Black": "Bob", "Result": "1-0"},
                result="1-0",
                plies=[],
                critical_positions=[],
            )
        ],
        aggregate=ArchiveAggregate(
            games_total=1,
            games_completed=1,
            critical_positions=0,
        ),
        complete=True,
    )


@pytest.mark.parametrize(
    ("argv", "assertions"),
    [
        (["doctor"], lambda args: args.command == "doctor"),
        (
            ["setup-engine", "--version", "latest", "--plan-only"],
            lambda args: args.command == "setup-engine"
            and args.version == "latest"
            and args.plan_only is True,
        ),
        (
            ["setup-engine", "--install-dir", "/tmp/engine", "--approve-download"],
            lambda args: args.install_dir == Path("/tmp/engine")
            and args.approve_download is True,
        ),
        (
            [
                "fetch",
                "chesscom",
                "Hikaru",
                "--month",
                "2026-01",
                "--output-dir",
                "/tmp/out",
                "--user-agent",
                "ai-chess/1.0 (contact: test@example.com)",
            ],
            lambda args: args.command == "fetch"
            and args.fetch_source == "chesscom"
            and args.username == "Hikaru"
            and args.month == "2026-01"
            and args.output_dir == Path("/tmp/out"),
        ),
        (
            [
                "fetch",
                "chesscom",
                "Hikaru",
                "--from",
                "2026-01",
                "--to",
                "2026-03",
                "--output-dir",
                "/tmp/out",
            ],
            lambda args: args.month_from == "2026-01" and args.month_to == "2026-03",
        ),
        (
            [
                "fetch",
                "lichess",
                "Topeklc",
                "--max",
                "5",
                "--output-dir",
                "/tmp/out",
            ],
            lambda args: args.command == "fetch"
            and args.fetch_source == "lichess"
            and args.username == "Topeklc"
            and args.max_games == 5
            and args.output_dir == Path("/tmp/out"),
        ),
        (
            ["import", "games.pgn", "--output-dir", "/tmp/out"],
            lambda args: args.command == "import" and args.path == Path("games.pgn"),
        ),
        (
            [
                "analyze",
                "game",
                "games.pgn",
                "--game",
                "2",
                "--depth",
                "18",
                "--deep-depth",
                "24",
                "--threads",
                "2",
                "--hash-mb",
                "256",
                "--timeout-seconds",
                "45",
            ],
            lambda args: args.command == "analyze"
            and args.analyze_target == "game"
            and args.path == Path("games.pgn")
            and args.game == 2
            and args.depth == 18
            and args.deep_depth == 24
            and args.threads == 2
            and args.hash_mb == 256
            and args.timeout_seconds == 45.0,
        ),
        (
            [
                "analyze",
                "archive",
                "games.pgn",
                "--output",
                "analysis.json",
            ],
            lambda args: args.analyze_target == "archive"
            and args.output == Path("analysis.json"),
        ),
        (
            [
                "report",
                "analysis.json",
                "--output",
                "report.html",
                "--insights",
                "notes.md",
            ],
            lambda args: args.command == "report"
            and args.analysis == Path("analysis.json")
            and args.output == Path("report.html")
            and args.insights == Path("notes.md"),
        ),
    ],
)
def test_build_parser_accepts_required_command_shapes(argv, assertions) -> None:
    args = cli_module.build_parser().parse_args(argv)

    assert assertions(args)


def test_main_rejects_ambiguous_fetch_month_and_range(capsys) -> None:
    exit_code = cli_module.main(
        [
            "fetch",
            "chesscom",
            "Hikaru",
            "--month",
            "2026-01",
            "--from",
            "2026-01",
            "--to",
            "2026-02",
            "--output-dir",
            "out",
            "--user-agent",
            "ai-chess/1.0 (contact: test@example.com)",
        ]
    )

    captured = capsys.readouterr()
    assert exit_code != 0
    payload = json.loads(captured.out)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "invalid_input"
    assert "month" in payload["error"]["message"].lower()
    assert payload["error"]["remedy"] == "Run `ai-chess --help` for valid command usage."
    assert "choose either --month" in captured.err.lower()


@pytest.mark.parametrize(
    ("argv", "message_fragment"),
    [
        (["fetch"], "fetch source"),
        (["analyze"], "analysis target"),
        (["analyze", "game", "games.pgn", "--depth", "0"], "depth must be greater than zero"),
        (["analyze", "game", "games.pgn", "--game", "0"], "--game must be greater than zero"),
        (["analyze", "archive", "games.pgn", "--output", "analysis.json", "--threads", "-1"], "threads must be greater than zero"),
    ],
)
def test_main_rejects_incomplete_or_invalid_cli_shapes(
    argv: list[str],
    message_fragment: str,
    capsys,
) -> None:
    exit_code = cli_module.main(argv)

    captured = capsys.readouterr()
    assert exit_code != 0
    payload = json.loads(captured.out)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "invalid_input"
    assert message_fragment in payload["error"]["message"].lower()


def test_main_keeps_help_behavior_for_root_and_subcommands(capsys) -> None:
    with pytest.raises(SystemExit) as root_help:
        cli_module.main(["--help"])
    assert root_help.value.code == 0
    assert "usage: ai-chess" in capsys.readouterr().out

    with pytest.raises(SystemExit) as subcommand_help:
        cli_module.main(["fetch", "chesscom", "--help"])
    assert subcommand_help.value.code == 0
    assert "usage: ai-chess fetch chesscom" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("name", "argv", "handler_name", "status", "payload"),
    [
        ("doctor", ["doctor"], "_run_doctor", "doctor ok", {"python": "3.13"}),
        (
            "setup",
            ["setup-engine", "--plan-only"],
            "_run_setup_engine",
            "setup ok",
            {"version": "17.1"},
        ),
        (
            "fetch",
            [
                "fetch",
                "chesscom",
                "Hikaru",
                "--month",
                "2026-01",
                "--output-dir",
                "out",
                "--user-agent",
                "ai-chess/1.0 (contact: test@example.com)",
            ],
            "_run_fetch_chesscom",
            "fetch ok",
            {"manifest_path": "out/manifest.v1.json"},
        ),
        (
            "fetch-lichess",
            [
                "fetch",
                "lichess",
                "Topeklc",
                "--max",
                "5",
                "--output-dir",
                "out",
            ],
            "_run_fetch_lichess",
            "fetch ok",
            {"manifest_path": "out/manifest.v1.json"},
        ),
        (
            "import",
            ["import", "games.pgn", "--output-dir", "out"],
            "_run_import",
            "import ok",
            {"manifest_path": "out/manifest.v1.json"},
        ),
        (
            "analyze-game",
            ["analyze", "game", "games.pgn"],
            "_run_analyze_game",
            "analysis ok",
            {"artifact_path": "games.game-1.analysis.json"},
        ),
        (
            "analyze-archive",
            ["analyze", "archive", "games.pgn", "--output", "analysis.json"],
            "_run_analyze_archive",
            "archive ok",
            {"artifact_path": "analysis.json"},
        ),
        (
            "report",
            ["report", "analysis.json", "--output", "report.html"],
            "_run_report",
            "report ok",
            {"output_path": "report.html"},
        ),
    ],
)
def test_main_writes_compact_success_json_and_status_to_stderr(
    monkeypatch: pytest.MonkeyPatch,
    capsys,
    name: str,
    argv: list[str],
    handler_name: str,
    status: str,
    payload: dict[str, object],
) -> None:
    monkeypatch.setattr(
        cli_module,
        handler_name,
        lambda _args: cli_module.CommandResult(status=status, payload=payload),
    )

    assert cli_module.main(argv) == 0

    captured = capsys.readouterr()
    assert captured.out == json.dumps(
        {"ok": True, "result": payload},
        separators=(",", ":"),
    ) + "\n"
    assert status in captured.err


def test_main_writes_exact_failure_json_for_app_errors(
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:
    def fail(_args):
        raise AppError(
            ErrorCode.APPROVAL_REQUIRED,
            "approval missing",
            "pass --approve-download",
        )

    monkeypatch.setattr(cli_module, "_run_setup_engine", fail)

    assert cli_module.main(["setup-engine"]) == 1

    captured = capsys.readouterr()
    assert captured.out == (
        '{"ok":false,"error":{"code":"approval_required",'
        '"message":"approval missing","remedy":"pass --approve-download"}}\n'
    )
    assert "approval missing" in captured.err


def test_doctor_flow_reports_platform_cache_and_discovered_engine(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys,
) -> None:
    cache_root = tmp_path / "data"
    monkeypatch.setattr(cli_module, "_cache_root", lambda: cache_root)
    monkeypatch.setattr(
        cli_module,
        "detect_platform",
        lambda: PlatformTarget("linux", "x86_64", frozenset({"avx2"})),
    )
    monkeypatch.setattr(
        cli_module,
        "discover_engine",
        lambda explicit, cache_dir: SimpleNamespace(
            path=str(Path(cache_dir) / "stockfish"),
            version="17.1",
        ),
    )

    assert cli_module.main(["doctor"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["result"]["cache_path"] == str(cache_root / "cache")
    assert payload["result"]["stockfish"]["version"] == "17.1"
    assert payload["result"]["platform"]["os"] == "linux"


def test_setup_engine_plan_only_uses_resolved_plan_without_download(
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:
    target = PlatformTarget("linux", "x86_64", frozenset())
    plan = InstallPlan(
        "17.1",
        target,
        ReleaseAsset(
            "stockfish-linux-x64.tar",
            "https://github.com/official-stockfish/Stockfish/releases/download/sf_17.1/stockfish-linux-x64.tar",
            None,
        ),
        Path("/tmp/engines/17.1"),
        Path("/tmp/engines/17.1/stockfish"),
    )
    calls: list[tuple[str, PlatformTarget, Path | None]] = []

    monkeypatch.setattr(cli_module, "detect_platform", lambda: target)

    def fake_build(version: str, selected_target: PlatformTarget, install_dir=None):
        calls.append((version, selected_target, install_dir))
        return plan

    monkeypatch.setattr(cli_module, "build_install_plan", fake_build)

    assert (
        cli_module.main(
            [
                "setup-engine",
                "--plan-only",
                "--install-dir",
                "/tmp/engines/17.1",
            ]
        )
        == 0
    )

    payload = json.loads(capsys.readouterr().out)
    assert calls == [("latest", target, Path("/tmp/engines/17.1"))]
    assert payload["result"]["asset"]["name"] == "stockfish-linux-x64.tar"
    assert payload["result"]["executable_path"] == "/tmp/engines/17.1/stockfish"


def test_setup_engine_plan_payload_uses_forward_slashes_for_json_paths() -> None:
    target = PlatformTarget("linux", "x86_64", frozenset())
    plan = InstallPlan(
        "17.1",
        target,
        ReleaseAsset(
            "stockfish-linux-x64.tar",
            "https://github.com/official-stockfish/Stockfish/releases/download/sf_17.1/stockfish-linux-x64.tar",
            None,
        ),
        PureWindowsPath("/tmp/engines/17.1"),
        PureWindowsPath("/tmp/engines/17.1/stockfish"),
    )

    payload = cli_module._plan_payload(plan)

    assert payload["install_dir"] == "/tmp/engines/17.1"
    assert payload["executable_path"] == "/tmp/engines/17.1/stockfish"


def test_fetch_chesscom_flow_uses_env_user_agent_and_month_range(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys,
) -> None:
    calls: list[tuple[str, list[str], Path]] = []

    class FakeClient:
        def fetch_months(self, username: str, months: list[str], output_dir: Path):
            calls.append((username, months, output_dir))
            return SimpleNamespace(
                manifest_path=output_dir / "manifest.v1.json",
                pgn_paths=[output_dir / "hikaru-2026-01.pgn", output_dir / "hikaru-2026-02.pgn"],
                raw_json_paths=[],
                metadata_paths=[],
            )

    monkeypatch.setenv(
        "AI_CHESS_USER_AGENT",
        "ai-chess/1.0 (contact: test@example.com)",
    )
    monkeypatch.setattr(cli_module, "_make_chesscom_client", lambda user_agent: FakeClient())

    assert (
        cli_module.main(
            [
                "fetch",
                "chesscom",
                "Hikaru",
                "--from",
                "2026-01",
                "--to",
                "2026-02",
                "--output-dir",
                str(tmp_path / "out"),
            ]
        )
        == 0
    )

    payload = json.loads(capsys.readouterr().out)
    assert calls == [("Hikaru", ["2026-01", "2026-02"], tmp_path / "out")]
    assert payload["result"]["manifest_path"] == str(tmp_path / "out" / "manifest.v1.json")


def test_fetch_lichess_flow_returns_manifest_and_pgn(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys,
) -> None:
    calls: list[tuple[str, Path, int | None]] = []

    class FakeClient:
        def fetch_user_games(
            self,
            username: str,
            output_dir: Path,
            *,
            max_games: int | None,
        ):
            calls.append((username, output_dir, max_games))
            return SimpleNamespace(
                manifest_path=output_dir / "manifest.v1.json",
                pgn_path=output_dir / "Topeklc.lichess.pgn",
                provenance_path=output_dir / "Topeklc.lichess.provenance.json",
            )

    monkeypatch.setattr(cli_module, "_make_lichess_client", lambda user_agent: FakeClient())

    assert (
        cli_module.main(
            [
                "fetch",
                "lichess",
                "Topeklc",
                "--max",
                "5",
                "--output-dir",
                str(tmp_path / "out"),
            ]
        )
        == 0
    )

    payload = json.loads(capsys.readouterr().out)
    assert calls == [("Topeklc", tmp_path / "out", 5)]
    assert payload["result"]["manifest_path"] == str(tmp_path / "out" / "manifest.v1.json")
    assert payload["result"]["pgn_path"] == str(tmp_path / "out" / "Topeklc.lichess.pgn")


def test_import_flow_returns_manifest_and_game_count(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys,
) -> None:
    output_dir = tmp_path / "out"
    monkeypatch.setattr(
        cli_module,
        "import_pgn",
        lambda source, destination: SimpleNamespace(
            manifest_path=destination / "manifest.v1.json",
            pgn_path=destination / source.name,
            games=[object(), object()],
        ),
    )

    assert cli_module.main(["import", str(tmp_path / "games.pgn"), "--output-dir", str(output_dir)]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["result"]["manifest_path"] == str(output_dir / "manifest.v1.json")
    assert payload["result"]["game_count"] == 2


def test_analyze_game_flow_writes_deterministic_artifact_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys,
) -> None:
    source = tmp_path / "games.pgn"
    source.write_text("fixture", encoding="utf-8")

    monkeypatch.setattr(
        cli_module,
        "import_pgn",
        lambda _source, _destination: SimpleNamespace(games=[SimpleNamespace(game_id="g1"), SimpleNamespace(game_id="g2")]),
    )
    monkeypatch.setattr(cli_module, "analyze_game", lambda game, engine_command, settings: _artifact("g2"))
    monkeypatch.setattr(cli_module, "_engine_command", lambda: ["stockfish"])

    assert cli_module.main(["analyze", "game", str(source), "--game", "2"]) == 0

    payload = json.loads(capsys.readouterr().out)
    artifact_path = source.with_suffix(".game-2.analysis.json")
    assert artifact_path.is_file()
    assert payload["result"]["artifact_path"] == str(artifact_path)
    assert payload["result"]["game_id"] == "g2"


def test_analyze_archive_flow_uses_explicit_output_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys,
) -> None:
    output_path = tmp_path / "archive.analysis.json"

    monkeypatch.setattr(
        cli_module,
        "read_games",
        lambda _path: [SimpleNamespace(game_id="g1"), SimpleNamespace(game_id="g2")],
    )

    def fake_analyze_archive(games, engine_command, settings, output):
        output.write_text('{"artifact":"ok"}\n', encoding="utf-8")
        return _artifact("g1")

    monkeypatch.setattr(cli_module, "analyze_archive", fake_analyze_archive)
    monkeypatch.setattr(cli_module, "_engine_command", lambda: ["stockfish"])

    assert (
        cli_module.main(
            [
                "analyze",
                "archive",
                str(tmp_path / "games.pgn"),
                "--output",
                str(output_path),
            ]
        )
        == 0
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["result"]["artifact_path"] == str(output_path)
    assert payload["result"]["games_total"] == 1


def test_report_flow_returns_written_output_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys,
) -> None:
    output_path = tmp_path / "report.html"

    def fake_write_report(artifact_path: Path, destination: Path, insights_path=None):
        destination.write_text("<html></html>", encoding="utf-8")
        return destination

    monkeypatch.setattr(cli_module, "write_report", fake_write_report)

    assert (
        cli_module.main(
            [
                "report",
                str(tmp_path / "analysis.json"),
                "--output",
                str(output_path),
            ]
        )
        == 0
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["result"]["output_path"] == str(output_path)


def test_review_pgn_flow_selects_latest_and_writes_report(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys,
) -> None:
    source = tmp_path / "games.pgn"
    source.write_text("fixture", encoding="utf-8")
    output_dir = tmp_path / "out"
    output_dir.mkdir()
    pgn_path = output_dir / "games.pgn"

    older = SimpleNamespace(game=SimpleNamespace(headers={"UTCDate": "2026.06.10", "UTCTime": "10:00:00", "White": "A", "Black": "B", "Result": "1-0"}))
    newer = SimpleNamespace(game=SimpleNamespace(headers={"UTCDate": "2026.06.15", "UTCTime": "11:51:44", "White": "C", "Black": "D", "Result": "0-1"}))

    monkeypatch.setattr(
        cli_module,
        "import_pgn",
        lambda _source, _destination: SimpleNamespace(
            pgn_path=pgn_path,
            manifest_path=output_dir / "manifest.v1.json",
            games=[older, newer],
        ),
    )
    monkeypatch.setattr(cli_module, "analyze_game", lambda game, engine_command, settings: _artifact("g-latest"))
    monkeypatch.setattr(
        cli_module,
        "discover_engine",
        lambda _version, _cache_dir: SimpleNamespace(path="stockfish", version="18"),
    )

    assert cli_module.main(["review", "pgn", str(source), "--output-dir", str(output_dir)]) == 0

    payload = json.loads(capsys.readouterr().out)["result"]
    assert payload["selected_game"]["index"] == 2
    assert payload["selected_game"]["headers"]["Black"] == "D"
    assert payload["engine"]["installed"] is False
    analysis_path = pgn_path.with_suffix(".game-2.analysis.json")
    report_path = pgn_path.with_suffix(".game-2.report.html")
    assert payload["analysis_path"] == str(analysis_path)
    assert payload["report_path"] == str(report_path)
    assert analysis_path.is_file()
    assert report_path.is_file()
    assert "<!doctype html>" in report_path.read_text(encoding="utf-8")


def test_review_url_routes_lichess_links_to_single_game_export(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys,
) -> None:
    output_dir = tmp_path / "out"
    pgn_path = output_dir / "abcdefgh.lichess.pgn"
    games = [
        SimpleNamespace(
            game=SimpleNamespace(
                headers={"White": "A", "Black": "B", "Result": "1-0"},
            )
        )
    ]
    calls: list[tuple[str, Path]] = []

    class FakeLichessClient:
        def fetch_game(self, game_id: str, destination: Path):
            calls.append((game_id, destination))
            return SimpleNamespace(
                pgn_path=pgn_path,
                manifest_path=destination / "manifest.v1.json",
            )

    monkeypatch.setattr(
        cli_module,
        "_ensure_engine",
        lambda: (SimpleNamespace(path="stockfish", version="18"), False),
    )
    monkeypatch.setattr(cli_module, "_make_lichess_client", lambda user_agent: FakeLichessClient())
    monkeypatch.setattr(cli_module, "read_games", lambda path: games)

    def finish(
        args,
        engine,
        installed,
        selected_pgn_path,
        manifest_path,
        selected_games,
        game_number=None,
    ):
        assert selected_pgn_path == pgn_path
        assert manifest_path == output_dir / "manifest.v1.json"
        assert selected_games == games
        assert game_number == 1
        return cli_module.CommandResult(
            status="review ok",
            payload={"provider": "lichess", "game_id": "abcdefgh"},
        )

    monkeypatch.setattr(cli_module, "_finish_review", finish)

    assert (
        cli_module.main(
            [
                "review",
                "url",
                "https://lichess.org/abcdefgh/black",
                "--output-dir",
                str(output_dir),
            ]
        )
        == 0
    )

    payload = json.loads(capsys.readouterr().out)["result"]
    assert calls == [("abcdefgh", output_dir)]
    assert payload == {"provider": "lichess", "game_id": "abcdefgh"}
