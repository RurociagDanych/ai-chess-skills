# One-Shot Chess Review Orchestration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an `ai-chess review` command and a `review-chess-game` skill so one request runs engine-ensure → fetch/import → select latest game → analyze → report, delivering analysis plus a visual report together.

**Architecture:** A deterministic CLI pipeline (`ai-chess review chesscom|pgn`) sequences the existing fetch/import, analyze-game, and report code paths and auto-installs Stockfish if missing. A new orchestrator skill runs that command, then writes coaching-voice insights and regenerates the report with `--insights`. No acquisition/analysis/render logic is duplicated.

**Tech Stack:** Python 3.13, argparse CLI, `python-chess`, pytest. Engine: official Stockfish. Report: existing self-contained offline HTML template.

---

## File Structure

- `src/ai_chess/pgn.py` — add pure `select_latest_index(games)` helper (PGN-domain logic lives here).
- `src/ai_chess/cli.py` — add `review` subcommands, `_ensure_engine`, `_select_game_number`, `_finish_review`, the two handlers, argparse wiring, and `_require_command` validation; refactor analysis-flag wiring for reuse.
- `tests/unit/test_review_selection.py` — unit tests for `select_latest_index`.
- `tests/integration/test_cli_workflows.py` — add an offline `review pgn` end-to-end test.
- `skills/review-chess-game/SKILL.md` — new orchestrator skill.

---

## Task 1: Latest-game selection helper

**Files:**
- Modify: `src/ai_chess/pgn.py`
- Test: `tests/unit/test_review_selection.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_review_selection.py`:

```python
import chess.pgn

from ai_chess.pgn import ImportedGame, select_latest_index


def _game(headers: dict[str, str]) -> ImportedGame:
    game = chess.pgn.Game()
    for key, value in headers.items():
        game.headers[key] = value
    return ImportedGame(game_id="x", game=game)


def test_selects_latest_by_utc_date_then_time() -> None:
    games = [
        _game({"UTCDate": "2026.06.10", "UTCTime": "10:00:00"}),
        _game({"UTCDate": "2026.06.15", "UTCTime": "08:00:00"}),
        _game({"UTCDate": "2026.06.15", "UTCTime": "11:51:44"}),
    ]
    assert select_latest_index(games) == 3


def test_falls_back_to_date_header_when_utc_absent() -> None:
    games = [
        _game({"Date": "2026.06.01"}),
        _game({"Date": "2026.06.14"}),
    ]
    assert select_latest_index(games) == 2


def test_dated_games_rank_above_undated() -> None:
    games = [
        _game({"UTCDate": "2026.06.14", "UTCTime": "09:00:00"}),
        _game({"Date": "????.??.??"}),
    ]
    assert select_latest_index(games) == 1


def test_no_dates_anywhere_returns_last_game() -> None:
    games = [_game({}), _game({}), _game({})]
    assert select_latest_index(games) == 3


def test_single_game_returns_one() -> None:
    assert select_latest_index([_game({"UTCDate": "2026.06.15"})]) == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_review_selection.py -q`
Expected: FAIL with `ImportError: cannot import name 'select_latest_index'`.

- [ ] **Step 3: Implement the helper**

In `src/ai_chess/pgn.py`, after `read_games` (around line 108), add:

```python
def select_latest_index(games: list[ImportedGame]) -> int:
    """Return the 1-based index of the most recently played game.

    Ranking key: dated games rank above undated; among dated games the latest
    (UTCDate, UTCTime) wins, falling back to Date. Ties (including the
    no-dates-anywhere case) resolve to the last game in file order.
    """

    def _clean(value: str) -> str:
        return "" if not value or "?" in value else value

    def _key(item: tuple[int, ImportedGame]) -> tuple[bool, str, str, int]:
        index, imported = item
        headers = imported.game.headers
        primary = _clean(headers.get("UTCDate", "")) or _clean(headers.get("Date", ""))
        utc_time = _clean(headers.get("UTCTime", ""))
        return (bool(primary), primary, utc_time, index)

    best_index, _ = max(enumerate(games), key=_key)
    return best_index + 1
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_review_selection.py -q`
Expected: PASS (5 passed).

- [ ] **Step 5: Commit**

```bash
git add src/ai_chess/pgn.py tests/unit/test_review_selection.py
git commit -m "feat: add latest-game selection helper

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 2: Failing integration test for `review pgn`

**Files:**
- Test: `tests/integration/test_cli_workflows.py` (append)

- [ ] **Step 1: Write the failing test**

Append to `tests/integration/test_cli_workflows.py` (the file already imports `json`, `SimpleNamespace`, `pytest`, `cli_module`, and defines `_artifact`):

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/integration/test_cli_workflows.py::test_review_pgn_flow_selects_latest_and_writes_report -q`
Expected: FAIL — argparse raises `AppError` ("A subcommand is required" / invalid choice 'review'), exit code 1, so the `main(...) == 0` assertion fails.

- [ ] **Step 3: Commit the failing test**

```bash
git add tests/integration/test_cli_workflows.py
git commit -m "test: add failing review pgn integration test

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 3: Implement the `ai-chess review` command

**Files:**
- Modify: `src/ai_chess/cli.py`

- [ ] **Step 1: Add the datetime import and the selection import**

At the top of `src/ai_chess/cli.py`, add after `import argparse` (line 1) a new import line:

```python
from datetime import datetime, timezone
```

Change the pgn import (line 18) from:

```python
from ai_chess.pgn import import_pgn, read_games
```

to:

```python
from ai_chess.pgn import import_pgn, read_games, select_latest_index
```

- [ ] **Step 2: Refactor analysis-flag wiring for reuse**

In `build_parser`, replace the existing nested `add_analysis_options` definition (lines 380-386) with two helpers so review subcommands can add flags without a `path` positional:

```python
    def add_analysis_flags(command: argparse.ArgumentParser) -> None:
        command.add_argument("--depth", type=int, default=16)
        command.add_argument("--deep-depth", type=int, default=22)
        command.add_argument("--threads", type=int, default=1)
        command.add_argument("--hash-mb", type=int, default=128)
        command.add_argument("--timeout-seconds", type=float, default=30.0)

    def add_analysis_options(command: argparse.ArgumentParser) -> None:
        command.add_argument("path", type=Path)
        add_analysis_flags(command)
```

- [ ] **Step 3: Add review helpers**

In `src/ai_chess/cli.py`, immediately before `def _run_report` (line 338), add:

```python
def _ensure_engine() -> tuple[object, bool]:
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
) -> CommandResult:
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
        key: headers.get(key)
        for key in ("White", "Black", "Date", "UTCDate", "UTCTime", "Result")
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
        raise AppError(
            ErrorCode.INVALID_INPUT,
            f"No games were fetched for {args.username} in {month}.",
            "Choose a --month in which games were played.",
        )
    pgn_path = result.pgn_paths[0]
    games = read_games(pgn_path)
    return _finish_review(args, engine, installed, pgn_path, result.manifest_path, games)


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
```

This adds four functions: `_ensure_engine`, `_finish_review`, `_run_review_chesscom`, and `_run_review_pgn`. Game selection and the `--latest`/`--game` validation live inline in `_finish_review`.

- [ ] **Step 4: Register the review subparser**

In `build_parser`, immediately before `report = subparsers.add_parser("report")` (line 398), add:

```python
    review = subparsers.add_parser("review")
    review_subparsers = review.add_subparsers(dest="review_source")

    def add_review_selection(command: argparse.ArgumentParser) -> None:
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
```

- [ ] **Step 5: Validate the review subcommand in `_require_command`**

In `_require_command`, after the `analyze` target check block (lines 150-155), add:

```python
    if args.command == "review" and getattr(args, "review_source", None) is None:
        raise AppError(
            ErrorCode.INVALID_INPUT,
            "A review source is required.",
            "Run `ai-chess review --help` for valid command usage.",
        )
```

- [ ] **Step 6: Run the integration test to verify it passes**

Run: `uv run pytest tests/integration/test_cli_workflows.py::test_review_pgn_flow_selects_latest_and_writes_report -q`
Expected: PASS (1 passed).

- [ ] **Step 7: Run the full suite**

Run: `uv run pytest -q`
Expected: PASS (all tests green).

- [ ] **Step 8: Commit**

```bash
git add src/ai_chess/cli.py
git commit -m "feat: add ai-chess review one-shot pipeline command

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 4: Orchestrator skill `review-chess-game`

**Files:**
- Create: `skills/review-chess-game/SKILL.md`

- [ ] **Step 1: Write the skill**

Create `skills/review-chess-game/SKILL.md`:

```markdown
---
name: review-chess-game
description: Run a complete one-shot chess review — engine setup, game retrieval, Stockfish analysis, and a visual HTML report — from a single request. Use when the user wants their latest Chess.com game (or a local PGN) reviewed end to end with a coaching summary and report, not individual pipeline steps.
allowed-tools:
  - Bash(ai-chess review *)
  - Bash(ai-chess report *)
  - Read
  - Write
---

# Review Chess Game

Use this skill for a one-shot, end-to-end review. It runs the full pipeline through
`ai-chess review`, then adds a human-analyst narrative and a regenerated report. For
single isolated steps (only fetch, only analyze, only visualize), use the dedicated
skills instead.

## 1. Run the pipeline

For the user's latest Chess.com game (default scope is the single most recent game in the
current month; Stockfish is installed automatically if missing):

```bash
ai-chess review chesscom USERNAME --latest --output-dir OUTPUT_DIR \
  --user-agent 'project/version (contact: email@example.com)'
```

For a local PGN:

```bash
ai-chess review pgn PATH_TO_FILE.pgn --latest --output-dir OUTPUT_DIR
```

Bounded analysis defaults apply (`--depth 16 --deep-depth 22 --threads 1 --hash-mb 128
--timeout-seconds 30`). To target a specific game, pass `--game N` instead of `--latest`.

The command prints a JSON envelope with `engine`, `source`, `selected_game`,
`analysis_path`, and `report_path`. Preserve these paths exactly.

## 2. Read the analysis artifact

Read the `analysis_path` JSON before writing anything. Treat `engine`, `settings`,
`source_manifest`, `complete`, and `errors` as factual. Cite concrete evidence from
`games[].plies[]`, `games[].critical_positions[]`, and `aggregate`.

## 3. Write coaching insights

Write a plain-text insights file (for example `OUTPUT_DIR/insights.txt`) in the voice of a
human trainer, not an engine log. Ground every claim in the artifact: when you name a
turning point, include the move, ply, evaluation change, and PV line. Distinguish fact
(engine score, PV) from interpretation (your coaching summary). Do not invent variations.

## 4. Regenerate the report with insights

```bash
ai-chess report ANALYSIS_PATH --output REPORT_PATH --insights OUTPUT_DIR/insights.txt
```

Use the same `REPORT_PATH` the pipeline reported so the final report supersedes the base one.

## 5. Present the result

Summarize the result, the decisive moments (cited with game/ply/eval/PV), and the report
path. Treat the report as offline and self-contained. Only open or view it when the host
permits GUI actions and the user asks; otherwise stop after generation and give the path.
If the artifact is partial or has `errors`, say so plainly instead of overstating confidence.
```

- [ ] **Step 2: Validate the skill**

Run: `uv run python scripts/validate_skill.py skills/review-chess-game/SKILL.md`
Expected: validation passes (no errors). If the script takes a directory or no argument, run `uv run python scripts/validate_skill.py` and confirm the new skill is reported valid.

- [ ] **Step 3: Run the full test suite**

Run: `uv run pytest -q`
Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add skills/review-chess-game/SKILL.md
git commit -m "feat: add review-chess-game orchestrator skill

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 5: Cross-link single-step skills (small docs touch)

**Files:**
- Modify: `skills/analyze-chess-games/SKILL.md`
- Modify: `skills/fetch-chess-games/SKILL.md`

- [ ] **Step 1: Add a pointer in each single-step skill**

At the end of `skills/analyze-chess-games/SKILL.md`, add:

```markdown

## One-shot alternative

If the user wants the entire flow (fetch → analyze → visual report) from a single request,
use the `review-chess-game` skill instead of running these steps individually.
```

At the end of `skills/fetch-chess-games/SKILL.md`, add the same section.

- [ ] **Step 2: Validate skills**

Run: `uv run python scripts/validate_skill.py skills/analyze-chess-games/SKILL.md skills/fetch-chess-games/SKILL.md`
Expected: validation passes. (Fall back to running the validator with no argument if it validates all skills at once.)

- [ ] **Step 3: Commit**

```bash
git add skills/analyze-chess-games/SKILL.md skills/fetch-chess-games/SKILL.md
git commit -m "docs: point single-step chess skills at the orchestrator

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 6: Manual smoke test

- [ ] **Step 1: Run the real pipeline on the previously fetched archive**

Run:
```bash
uv run ai-chess review pgn /tmp/ai-chess/topeklc/topeklc-2026-06.pgn --latest --output-dir /tmp/ai-chess/review-smoke
```
Expected: `{"ok":true,...}` envelope with `selected_game.index` pointing at the most recent game (2026-06-15, White `TBadi` / Black `Topeklc`), plus `analysis_path` and `report_path` that both exist. This exercises the real engine, analysis, and report end to end.

- [ ] **Step 2: Confirm the report is non-empty HTML**

Run: `head -c 30 /tmp/ai-chess/review-smoke/topeklc-2026-06.game-*.report.html`
Expected: `<!doctype html>`.

---

## Self-Review

**Spec coverage:**
- `ai-chess review chesscom|pgn` with month/latest/game/output-dir/report/analysis flags → Tasks 3.
- Latest-game selection (UTCDate/UTCTime, Date fallback, dated > undated, no-dates → last, single game) → Task 1.
- Engine auto-install when missing, continue, report `installed` flag → `_ensure_engine` + payload (Task 3).
- Result envelope (engine/source/selected_game/analysis_path/report_path) → `_finish_review` (Task 3).
- Error handling (out-of-range game, both --latest/--game, no games, missing review source) → Task 3 inline + `_require_command`; PGN/network/engine errors propagate from existing code paths.
- Orchestrator skill flow (run → read → insights → report --insights → present) → Task 4.
- Unit + integration testing → Tasks 1, 2, 3, 6.
- Optional cross-links → Task 5.

**Placeholder scan:** Task 3 Step 3 includes a NOTE explicitly removing the two placeholder symbols; all other steps contain complete code and exact commands.

**Type consistency:** `select_latest_index(games)` returns `int` (1-based) and is used identically in Task 1 and Task 3. `_finish_review` accesses `engine.path` / `engine.version` (provided by both `discover_engine` and `install_engine` results) and `games[i].game.headers.get(...)` (provided by `ImportedGame`). Report/analysis path suffixes `.game-{n}.analysis.json` and `.game-{n}.report.html` match between implementation (Task 3) and assertions (Task 2).
