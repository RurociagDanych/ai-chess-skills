---
name: review-chess-game
description: Run a complete one-shot chess review — engine setup, game retrieval, Stockfish analysis, and a visual HTML report — from a single request. Use when the user wants their latest Chess.com game, a pasted Chess.com/Lichess game link, or a local PGN reviewed end to end with a coaching summary and report, not individual pipeline steps.
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

For a specific game the user pastes as a Chess.com or Lichess link (e.g.
`https://www.chess.com/game/170242602084`, `.../game/live/170242602084`, or
`https://lichess.org/abcdefgh`), use the `url` source — the link itself selects the
game, so no `--latest`/`--game` is needed:

```bash
ai-chess review url GAME_URL --output-dir OUTPUT_DIR \
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
