# AI Chess Skills

Deterministic chess workflows packaged as agent skills: fetch Chess.com games or
import local PGNs, set up an official Stockfish engine, analyze games, and render
self-contained offline HTML reports. All executable behavior lives in the
agent-neutral `ai_chess` Python package; the skills are thin wrappers over its
stable CLI.

The skills ship for both **Claude Code** and **Codex** from one source tree:

- `skills/<name>/SKILL.md` — the instructions (read by both agents).
- `skills/<name>/agents/openai.yaml` — Codex interface metadata.
- `.claude-plugin/` — Claude Code plugin + marketplace manifests.

## Install the Python package

```bash
uv pip install -e ".[dev]"   # or: python -m pip install -e ".[dev]"
ai-chess doctor              # verify the CLI is on PATH
```

## Use with Claude Code

The repository root is a Claude Code plugin; the four skills are auto-discovered
from `skills/`. Install it from the bundled marketplace:

```text
/plugin marketplace add /path/to/ai-chess-skills
/plugin install ai-chess-skills@ai-chess-skills
```

Skills then appear namespaced, e.g. `/ai-chess-skills:fetch-chess-games`, and
Claude can also invoke them automatically when a request matches their
description. Each skill pre-approves only the `ai-chess` subcommands it needs via
`allowed-tools`, so the CLI runs without extra permission prompts once you trust
the workspace.

When working inside this repo directly (rather than installing the plugin), the
same skills are also available because the plugin is discovered in place.

## Use with Codex

Each skill keeps its `agents/openai.yaml` interface block. Reference a skill in a
prompt with `$fetch-chess-games`, `$setup-chess-engine`, `$analyze-chess-games`,
or `$visualize-chess-analysis`.

## Skills

| Skill | Purpose |
| --- | --- |
| `fetch-chess-games` | Retrieve Chess.com games or import local PGNs into a source archive. |
| `setup-chess-engine` | Inspect and install an official Stockfish build in a user-writable location. |
| `analyze-chess-games` | Analyze a game or archive with Stockfish into schema-valid JSON. |
| `visualize-chess-analysis` | Build a self-contained offline interactive HTML report. |

## CLI contract

```text
ai-chess setup-engine [--plan-only | --approve-download] [--install-dir PATH]
ai-chess fetch chesscom USER --month YYYY-MM [--output-dir DIR] [--user-agent UA]
ai-chess fetch chesscom USER --from YYYY-MM --to YYYY-MM [--output-dir DIR]
ai-chess import PATH.pgn [--output-dir DIR]
ai-chess analyze game PATH.pgn [--game N] [--depth 16] [--deep-depth 22]
ai-chess analyze archive PATH.pgn --output ANALYSIS.json
ai-chess report ANALYSIS.json --output report.html [--insights NOTES.txt]
ai-chess doctor
```

See `docs/superpowers/specs/` for the full design.

## Validate the skills

```bash
python scripts/validate_skill.py
```

This checks each `SKILL.md` (Claude frontmatter + Codex `openai.yaml`) and the
`.claude-plugin/` manifests. To validate Claude Code packaging end-to-end, also
run `claude plugin validate` against the repo root.

## Development

```bash
pytest --cov=ai_chess --cov-report=term-missing
```
