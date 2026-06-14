---
name: setup-chess-engine
description: Install and verify an official Stockfish engine in a user-writable location. Use when the user needs engine availability checked, an install plan reviewed, or an official Stockfish build installed without touching system-wide paths.
allowed-tools:
  - Bash(ai-chess doctor*)
  - Bash(ai-chess setup-engine *)
  - Read
---

# Set Up Chess Engine

Use this skill to inspect engine availability, show the official Stockfish install plan, and install only after explicit approval. Never install system-wide and never use `sudo`.

## Workflow

Start with environment status:

```bash
ai-chess doctor
```

Then inspect the install plan before any download:

```bash
ai-chess setup-engine --plan-only
```

Present the plan fields back to the user before asking for approval:

- official source URL from `asset.url`
- selected asset name and version
- target platform from `target.os`, `target.arch`, and CPU flags when relevant
- destination from `install_dir`
- executable path from `executable_path`

Ask for approval immediately before the download step. After approval, rerun with `--approve-download` and, if the user requested a custom writable destination, include `--install-dir PATH`:

```bash
ai-chess setup-engine --approve-download
```

or

```bash
ai-chess setup-engine --install-dir PATH --approve-download
```

## Constraints

- Keep installs in the user data directory or another user-provided writable directory.
- Do not install into `/usr`, `/usr/local`, or any shared system path.
- Treat `doctor` and `--plan-only` output as facts; do not guess the download source or destination.
- After installation, report the installed engine path and version returned by the CLI.
