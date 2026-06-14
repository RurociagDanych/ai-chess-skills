---
name: fetch-chess-games
description: Fetch Chess.com games or import local PGNs into a reusable source archive. Use when the user needs raw game acquisition, manifest/provenance paths, or a local PGN staged for later analysis without running engine analysis yet.
allowed-tools:
  - Bash(ai-chess fetch *)
  - Bash(ai-chess import *)
  - Read
---

# Fetch Chess Games

Confirm the source first: local PGN import or Chess.com retrieval. This skill stops after acquisition and returns artifact paths. Do not run `ai-chess analyze ...` or `ai-chess report ...` here.

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

## Output Handling

Report what was fetched or imported, the destination directory, and the manifest path. If the user wants analysis next, hand off to the `analyze-chess-games` skill instead of doing it here.

## One-shot alternative

If the user wants the entire flow (fetch → analyze → visual report) from a single request,
use the `review-chess-game` skill instead of running these steps individually.
