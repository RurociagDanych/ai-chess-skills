---
name: fetch-chess-games
description: Fetch Chess.com or Lichess games, or import local PGNs into a reusable source archive. Use when the user needs raw game acquisition, manifest/provenance paths, or a local PGN staged for later analysis without running engine analysis yet.
allowed-tools:
  - Bash(ai-chess fetch *)
  - Bash(ai-chess import *)
  - Read
---

# Fetch Chess Games

Confirm the source first: local PGN import, Chess.com retrieval, or Lichess retrieval. This skill stops after acquisition and returns artifact paths. Do not run `ai-chess analyze ...` or `ai-chess report ...` here.

## Local PGN

For a local file, import it into a destination directory:

```bash
ai-chess import PATH_TO_FILE.pgn --output-dir OUTPUT_DIR
```

Return the copied PGN path and `manifest.v1.json`. The manifest is the handoff for later analysis.

## Chess.com

Use serial month retrieval only. Do not parallelize months or interleave other network fetches for the same request.

Use an identifiable user agent in this format:

```text
project/version (contact: email@example.com)
```

Fetch one month:

```bash
ai-chess fetch chesscom USERNAME --month YYYY-MM --output-dir OUTPUT_DIR --user-agent 'project/version (contact: email@example.com)'
```

Fetch a month range:

```bash
ai-chess fetch chesscom USERNAME --from YYYY-MM --to YYYY-MM --output-dir OUTPUT_DIR --user-agent 'project/version (contact: email@example.com)'
```

Return the output paths for:

- `manifest.v1.json`
- `USERNAME-YYYY-MM.pgn`
- `USERNAME-YYYY-MM.json`
- `USERNAME-YYYY-MM.provenance.json`

Preserve provenance and manifest paths exactly as written so later steps can cite source identity, hashes, source URLs, ETags, and retrieval metadata.

## Lichess

Fetch public Lichess games as a PGN archive:

```bash
ai-chess fetch lichess USERNAME --output-dir OUTPUT_DIR
```

Limit the number of games:

```bash
ai-chess fetch lichess USERNAME --max 25 --output-dir OUTPUT_DIR
```

Return the output paths for:

- `manifest.v1.json`
- `USERNAME.lichess.pgn`
- `USERNAME.lichess.provenance.json`

Use `--user-agent` or `AI_CHESS_USER_AGENT` when the caller has an identifiable
agent string; otherwise the CLI uses its package user agent.

## Output Handling

Report what was fetched or imported, the destination directory, and the manifest path. If the user wants analysis next, hand off to the `analyze-chess-games` skill instead of doing it here.

## One-shot alternative

If the user wants the entire flow (fetch → analyze → visual report) from a single request,
use the `review-chess-game` skill instead of running these steps individually.
