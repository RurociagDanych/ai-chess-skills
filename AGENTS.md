# AGENTS.md

## Purpose

Guide for AI coding agents working on ai-chess-skills: agent skills (`SKILL.md` contracts) for Claude Code and Codex, backed by the agent-neutral `ai_chess` Python package and its `ai-chess` CLI. Skills are thin wrappers; all executable behavior lives in the package.

## Layout

- `.claude-plugin/`: Claude Code plugin (`plugin.json`) and marketplace (`marketplace.json`) manifests.
- `.github/`: `workflows/ci.yml` (tests with coverage and skill validation on push and pull request) and `workflows/stockfish-smoke.yml` (weekly Stockfish setup smoke test).
- `resources/`: static API reference data (`chess_com_api.json`).
- `scripts/`: `validate_skill.py`, which checks each `SKILL.md`, its `agents/openai.yaml` and the plugin manifests.
- `skills/`: one folder per skill with `SKILL.md` (read by both agents) and `agents/openai.yaml` (Codex interface metadata).
- `src/`: the `ai_chess` package (CLI, Chess.com and Lichess clients, PGN handling, Stockfish integration, analysis, HTML report assets).
- `tests/`: `unit/`, `integration/` and `fixtures/`.

## Commands

- Install: `uv sync --extra dev` (or `uv pip install -e ".[dev]"`, or `python -m pip install -e ".[dev]"`); check with `ai-chess doctor`.
- Lint: no linter is configured; `python scripts/validate_skill.py` validates the skill contracts and manifests.
- Test: `pytest --cov=ai_chess --cov-report=term-missing`.
- Run: `ai-chess <subcommand>`; see the CLI contract in [README.md](README.md#cli-contract).
- Deploy dry run: not applicable (nothing is deployed). For Claude Code packaging, `claude plugin validate` against the repository root.

## Conventions

- Language: English for code, docs and commit messages. Python 3.13.
- Public modules, classes and functions have docstrings. Comments explain why, not what. Tool directives such as # noqa are allowed.
- Formatters: none configured; follow the surrounding style.
- Skills: do not change the `SKILL.md` frontmatter format or the `agents/openai.yaml` structure; `scripts/validate_skill.py` and CI depend on them. Keep each skill's `allowed-tools` limited to the `ai-chess` subcommands it needs.
- Commit style: Conventional Commits (`feat:`, `fix:`, `docs:`, `chore:`).
- Branch naming: prefixed branches such as `feat/...`, `fix/...`, `docs/...`, `chore/...`.
- Test locations: unit tests in `tests/unit`, end-to-end CLI and engine tests in `tests/integration`, test data and the fake UCI engine in `tests/fixtures`.

## Guardrails

- Secrets: never commit credentials or tokens. No `.env` is used; the optional `AI_CHESS_USER_AGENT` comes from the environment.
- Git: no force-push and no history rewrite.
- Deletes: do not delete files or skills without owner approval.
- Generated paths: `.venv/`, `.ai-chess/`, `*.analysis.json`, `*.report.html`, `artifacts/`, `dist/`, `build/` and coverage output stay untracked.
- Lock file: `uv.lock` is the authoritative Python lock; change dependencies in `pyproject.toml` and run `uv lock`.
