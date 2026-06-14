# One-Shot Chess Review Orchestration — Design

Date: 2026-06-15
Status: Approved (pending spec review)

## Problem

The skill set exposes four discrete steps — `setup-chess-engine`, `fetch-chess-games`,
`analyze-chess-games`, `visualize-chess-analysis` — each scoped to a single `ai-chess`
subcommand. A user who asks "analyze my last game" must currently drive every step by
hand, including manually extracting the most recent game from a month archive. The goal
is a single request that delivers everything together: engine readiness, game acquisition,
engine analysis, and a visual HTML report with a human-analyst narrative.

## Goals

- One request → full pipeline → analysis artifact + visual report, presented together.
- Deterministic, testable pipeline separate from the LLM-authored narrative.
- Reliable "most recent game" selection (no manual extraction).
- Auto-install Stockfish if missing, then continue.
- Reuse existing code paths; do not duplicate fetch/analyze/report logic.

## Non-Goals (YAGNI)

- No auto-opening of the report (host/GUI permission rules unchanged).
- No multi-game-in-one-report or aggregate dashboards.
- No new persistent configuration surface.

## Architecture

Two layers with a clear boundary:

1. **Deterministic pipeline — CLI (`ai-chess review`).** Engine-ensure → acquire →
   select latest game → analyze → base report. Pure, no LLM, fully testable.
2. **Narrative + presentation — skill (`review-chess-game`).** Runs `review`, reads the
   artifact, writes coaching-voice insights, regenerates the report with `--insights`,
   and presents the summary, cited key moments, and report path.

The existing one-skill-one-step skills are unchanged; the new skill composes them.

## Component: `ai-chess review` command

```
ai-chess review chesscom USERNAME [--month YYYY-MM] [--latest | --game N]
    [--output-dir DIR] [--report PATH] [--user-agent UA]
    [--depth N --deep-depth N --threads N --hash-mb N --timeout-seconds S]
ai-chess review pgn PATH [--latest | --game N] [--output-dir DIR] [--report PATH]
    [analysis flags]
```

Defaults:
- Source month: current calendar month.
- Selection: `--latest` (mutually exclusive with `--game N`).
- Analysis: bounded defaults `--depth 16 --deep-depth 22 --threads 1 --hash-mb 128
  --timeout-seconds 30`.
- Report: written next to the analysis artifact unless `--report` is given.

Behavior: reuses existing fetch/import, analyze-game, and report code paths. Internally it
does not re-implement any acquisition, analysis, or rendering logic; it sequences calls to
the existing functions.

Output: a single JSON envelope matching the existing CLI style:

```json
{"ok": true, "result": {
  "engine": {"path": "...", "version": "...", "installed": false},
  "source": {"pgn_path": "...", "manifest_path": "..."},
  "selected_game": {"index": 1, "headers": {"White": "...", "Black": "...", "Date": "..."}},
  "analysis_path": "...",
  "report_path": "..."
}}
```

`engine.installed` is `true` only when this invocation performed an install.

## Component: latest-game selection helper

A new pure function selects the most recent game from a parsed PGN:

- Sort key: `(UTCDate, UTCTime)`, falling back to `(Date)`, then original file order.
- Returns a 1-based index into file order, fed to the existing analyze-game path.
- `--game N` overrides selection entirely.
- Single-game PGN returns index 1. Missing date headers fall back to file order
  (last game in file treated as most recent only when no dates exist anywhere; otherwise
  dated games rank above undated ones).

This replaces the manual `awk` extraction used previously.

## Component: engine handling

`review` runs the doctor engine check first. If Stockfish is present, selection proceeds.
If missing, it runs the same setup routine `setup-engine` uses (official build,
user-writable location) and continues. Install failure stops the pipeline with a clear
remedy. The result envelope reports whether an install occurred.

## Component: `review-chess-game` skill

Frontmatter:
- `name: review-chess-game`
- `description`: one-shot full review triggered by "analyze my last game" / full game
  review requests.
- `allowed-tools`: `Bash(ai-chess review *)`, `Bash(ai-chess report *)`, `Read`, `Write`.

Flow:
1. Run `ai-chess review chesscom USERNAME --latest …` (or `review pgn PATH`).
2. Read the analysis artifact.
3. Write a coaching-voice `insights.txt` (human-analyst narrative, grounded in the
   artifact's evaluations, critical positions, and PV lines).
4. Run `ai-chess report ANALYSIS --output REPORT --insights insights.txt`.
5. Present: result summary, cited key moments (game/ply/eval/PV), and the report path.
   Offer to open the report only when the host permits GUI actions and the user asks.

Insights stay in the skill layer because they are LLM-authored and non-deterministic; the
CLI only produces the deterministic base report.

## Data Flow

```
review CLI:
  doctor → [setup-engine if missing] → fetch/import → select_latest → analyze game → report
skill:
  review CLI → read analysis → write insights → report --insights → present summary + path
```

## Error Handling

Reuse the existing `{ok:false, error:{code, message, remedy}}` envelope. Distinct cases:
- No games found in the month → remedy suggests a specific `--month` or `--from/--to`.
- Network failure during fetch → propagated from the existing fetch path.
- Engine install failure → remedy points to manual `setup-engine`.
- Empty or invalid PGN → remedy names the offending file.
- Both `--latest` and `--game` provided → input validation error.

## Testing

- Unit: latest-game selection — ordering by `(UTCDate, UTCTime)`, `Date` fallback,
  tie-breaks via file order, single game, missing/partial date headers.
- Integration: `review pgn FIXTURE` end-to-end (offline) producing analysis + report,
  reusing the existing engine-stub pattern from the current analyze tests.
- `review chesscom` selection is covered by the unit selection test plus the already-tested
  fetch path; no new network mocking is introduced.

## Optional follow-ups (out of scope here)

- README / skill cross-links pointing single-step skills at the orchestrator for the
  one-shot case.
