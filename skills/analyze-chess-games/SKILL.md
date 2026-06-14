---
name: analyze-chess-games
description: Analyze a single game or a PGN archive with Stockfish and explain the result from the produced artifact. Use when the user wants engine-backed review, bounded analysis settings, or evidence cited from moves, evaluations, and principal variations.
allowed-tools:
  - Bash(ai-chess analyze *)
  - Read
---

# Analyze Chess Games

Choose the mode first: one game from a PGN or an archive-level run across all games. Use bounded defaults unless the user asks for something else: `--depth 16 --deep-depth 22 --threads 1 --hash-mb 128 --timeout-seconds 30`.

## Single Game

Use this for one selected game from a PGN. `--game` is 1-based and defaults to `1`.

```bash
ai-chess analyze game PATH_TO_FILE.pgn --game 1
```

This writes `PATH_TO_FILE.game-1.analysis.json`.

## Archive

Use this for a whole PGN archive and provide an explicit output path:

```bash
ai-chess analyze archive PATH_TO_FILE.pgn --output OUTPUT.analysis.json
```

## Interpretation Rules

Read the resulting `analysis.v1.json` before explaining anything.

- Treat `engine`, `settings`, `source_manifest`, `complete`, and `errors` as factual CLI output.
- Cite concrete evidence from `games[].plies[]`, `games[].critical_positions[]`, and `aggregate`.
- When summarizing a mistake or turning point, include the game, ply or move, evaluation change, and PV line when available.
- Distinguish fact from interpretation: the engine score and PV are facts; any coaching summary is your interpretation of those facts.
- If the artifact is partial or contains `errors`, say that directly instead of overstating confidence.

Do not invent missing variations. If a claim is not grounded in the artifact, leave it out.

## One-shot alternative

If the user wants the entire flow (fetch → analyze → visual report) from a single request,
use the `review-chess-game` skill instead of running these steps individually.
