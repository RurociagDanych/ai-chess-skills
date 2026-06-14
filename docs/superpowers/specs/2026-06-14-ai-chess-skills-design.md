# AI Chess Skills Design

## Objective

Build a Codex-first repository of portable AI chess workflows that can:

- retrieve public Chess.com games;
- import arbitrary local PGN files;
- install or locate Stockfish with explicit user approval;
- analyze one game or a multi-game archive;
- produce deterministic, versioned JSON evidence;
- render a self-contained interactive HTML report; and
- guide an AI agent in turning engine evidence into traceable coaching insights.

Keep chess, network, engine, and reporting behavior in an agent-neutral Python
package. Keep Codex skills thin so Claude, MCP, and other agent integrations can
be added later without rewriting the core.

## Scope

### Included In V1

- Codex skill packaging and metadata.
- Python 3.13 support.
- `uv`-preferred setup with standard `venv` and `pip` fallback.
- Chess.com PubAPI retrieval for player archives and monthly PGNs.
- Import of arbitrary local PGN files.
- Linux and Windows Stockfish setup on x86-64 and ARM64 where an
  official compatible binary exists.
- Explicit approval immediately before a Stockfish download.
- Safe fallback instructions when no compatible official binary exists.
- Single-game and archive analysis.
- Fast analysis at depth 16 by default.
- Optional deeper analysis of selected critical positions, default depth 22.
- Versioned JSON manifests and analysis artifacts.
- Self-contained, offline interactive HTML reports.
- Deterministic aggregate archive statistics.
- Agent-authored insights grounded in JSON evidence.

### Deferred

- Lichess retrieval, while preserving a source-adapter boundary for it.
- An MCP server.
- Other agent-specific packaging beyond Codex and Claude Code. Claude Code is
  supported via a `.claude-plugin/` plugin and marketplace over the same skills.
- macOS Stockfish setup and CI coverage.
- Remote or hosted analysis services.
- Authentication to private Chess.com data.
- Advanced longitudinal coaching, repertoire modeling, or player embeddings.
- A general-purpose chess GUI or interactive engine-playing interface.

## Design Principles

1. Keep deterministic operations outside prompts.
2. Preserve original source PGNs and complete analysis provenance.
3. Make every generated insight traceable to positions and engine output.
4. Require user approval for downloads and avoid system-wide modifications.
5. Prefer narrow skills and stable CLI/file contracts over a monolithic skill.
6. Keep the first release usable without MCP.
7. Serialize Chess.com requests and cache responses responsibly.

## Architecture

```text
Codex skills
    |
    v
Python CLI and application functions
    |-- Chess.com HTTP client and cache
    |-- PGN import and normalization
    |-- Stockfish discovery and installer
    |-- UCI analysis pipeline
    |-- versioned JSON artifacts
    `-- self-contained HTML report generator
```

The CLI and future MCP adapter must call the same application functions. CLI
argument parsing, Codex instructions, and protocol adapters must not contain
chess analysis logic.

No MCP server is included in v1. MCP would add installation, permissions,
protocol lifecycle, and testing work without improving the initial local
workflows. A future stdio MCP server can wrap the stable application functions.

## Repository Layout

```text
ai-chess-skills/
├── LICENSE
├── pyproject.toml
├── resources/
│   └── chess_com_api.json
├── src/
│   └── ai_chess/
│       ├── __init__.py
│       ├── cli.py
│       ├── models.py
│       ├── chesscom.py
│       ├── pgn.py
│       ├── stockfish.py
│       ├── analysis.py
│       └── reports.py
├── skills/
│   ├── fetch-chess-games/
│   ├── setup-chess-engine/
│   ├── analyze-chess-games/
│   └── visualize-chess-analysis/
├── tests/
│   ├── fixtures/
│   ├── unit/
│   └── integration/
└── docs/
    └── superpowers/
        ├── specs/
        └── plans/
```

Each skill contains `SKILL.md`, `agents/openai.yaml`, and only the references or
scripts that directly support that skill. The Python package is the source of
truth for executable behavior.

## Skill Responsibilities

### `fetch-chess-games`

- Select Chess.com retrieval or local PGN import.
- Obtain a username and month or inclusive month range for Chess.com.
- Invoke the relevant CLI command.
- Return original PGN paths and a manifest path.
- Avoid interpreting games or invoking Stockfish.

### `setup-chess-engine`

- Run engine discovery and diagnostics.
- Explain the chosen official release, target, and destination.
- Request approval immediately before a download.
- Invoke installation and verify UCI startup.
- Return the configured engine path and version.

### `analyze-chess-games`

- Ensure a usable engine is configured.
- Select one-game or archive mode.
- Choose bounded analysis settings.
- Invoke deterministic analysis and inspect schema-valid JSON.
- Produce coaching prose only from recorded evidence.

### `visualize-chess-analysis`

- Validate analysis input.
- Generate a self-contained HTML report.
- Open it only when the host permits and the user requests viewing.
- Avoid requiring a web server or external assets.

## CLI Contract

```text
ai-chess setup-engine [--version latest]
ai-chess fetch chesscom USER --month YYYY-MM
ai-chess fetch chesscom USER --from YYYY-MM --to YYYY-MM
ai-chess import PATH.pgn
ai-chess analyze game PATH.pgn [--game N] [--depth 16]
ai-chess analyze archive PATH.pgn [--depth 16] [--deep-depth 22]
ai-chess report ANALYSIS.json [--output report.html]
ai-chess doctor
```

Commands write machine-readable JSON to stdout and concise status information
to stderr. Failures use nonzero exit codes and a stable JSON error object.

Application functions return typed Python models and do not depend on terminal
formatting or Codex-specific behavior.

## Artifact Contracts

### `manifest.v1.json`

Include:

- schema version;
- source kind;
- Chess.com username and source URLs or local source path;
- retrieval/import timestamps;
- HTTP `ETag`, `Last-Modified`, and cache status when applicable;
- SHA-256 of each original PGN;
- game count and stable per-game identity;
- output paths.

### `analysis.v1.json`

Include:

- schema version;
- source manifest identity;
- analysis timestamp;
- platform and package version;
- Stockfish path, version, binary digest, and UCI settings;
- depth, threads, hash size, timeouts, and deep-analysis policy;
- game headers and result;
- per-ply FEN, move, normalized evaluation, principal variation, and depth;
- identified critical positions and the documented reason for selection;
- derived presentation labels and thresholds;
- archive aggregates and incomplete/resume state.

Normalize evaluations from the side-to-move engine representation to a clearly
documented point of view. Represent mate scores separately from centipawns.

### `report.html`

Generate only from validated PGN and `analysis.v1.json`. Embed report data as
escaped inert JSON. Bundle CSS, JavaScript, piece assets, and board code so the
file works without a server or network connection.

## Chess.com Retrieval

Use `resources/chess_com_api.json` as a repository reference, not as runtime
configuration. Implement documented endpoint templates explicitly and test
them.

For v1, use:

- player archive listing;
- monthly game JSON where metadata is useful; and
- monthly PGN download.

Requirements:

- send a configurable identifiable `User-Agent` with project and contact data;
- issue requests serially;
- honor `Cache-Control`, `ETag`, and `Last-Modified`;
- use conditional requests;
- handle `304` from cache;
- honor `429 Retry-After`;
- use bounded exponential backoff for transient failures;
- validate usernames and month ranges;
- bound response sizes and accepted content types;
- write downloads atomically; and
- preserve original responses needed for provenance.

Do not access private data or attempt authenticated Chess.com operations.

## Stockfish Setup

### Discovery

Check, in order:

1. an explicit configured path;
2. the repository tool cache;
3. `stockfish` on `PATH`;
4. documented common package-manager locations.

Verify candidates by starting the process, performing the UCI handshake, and
recording the reported engine identity.

### Installation

Detect:

- operating system;
- machine architecture;
- CPU capabilities needed to choose a compatible build.

Prefer a conservative compatible official binary over the fastest uncertain
binary. Before downloading, display:

- Stockfish version;
- official source URL;
- selected platform/architecture variant;
- destination;
- expected archive size when known; and
- digest verification status.

Request explicit approval immediately before network or filesystem installation
work. Install into a user-owned cache. Never invoke `sudo`, modify a system
`PATH`, or write to protected directories.

Download only from official Stockfish sites or the official GitHub repository.
Verify an official published digest when available. Always compute and record
the downloaded archive and installed binary SHA-256 values.

Prevent archive traversal, symlink escapes, and unexpected file extraction.
Extract into a temporary directory, select the expected executable, set only
the necessary executable permission, move it atomically, and run a UCI smoke
test.

Official binaries cover the primary Windows x86-64/ARM64 and Linux x86-64
targets. Linux ARM64 support depends on the availability of a compatible
official binary. If no compatible official binary is available, stop automatic
installation and provide official package-manager or source-build instructions.
Do not substitute an unofficial binary.

## Analysis Pipeline

Use one parser and normalized game model for Chess.com and local PGNs.

### Fast Pass

- Default to depth 16.
- Analyze every mainline position.
- Keep engine threads and hash explicit.
- Apply a per-position timeout.
- Persist completed game results incrementally.
- Record invalid or unsupported games without aborting unrelated games.

### Critical Position Selection

Select positions using documented deterministic signals, including:

- evaluation swing relative to the mover;
- transition to or from a forced mate;
- materially better engine alternative;
- tactical principal variation;
- configurable top-N cap per game.

Presentation labels such as inaccuracy, mistake, or blunder are derived from
repository-owned thresholds. Do not represent them as Chess.com labels.

### Deep Pass

Optionally re-analyze selected critical positions at depth 22 by default.
Replace or supplement fast-pass evidence without discarding original settings.

### Archive Aggregation

Support multi-game PGNs as a first-class mode. Produce deterministic aggregates
such as:

- games and positions analyzed;
- incomplete or failed games;
- average evaluation loss by color and phase;
- critical-position counts by threshold and phase;
- repeated opening positions or ECO values when present;
- common tactical evidence flags; and
- links from aggregates to supporting games and plies.

Do not attempt advanced longitudinal coaching or causal claims in v1.

## Agent Insight Contract

The AI agent reads analysis JSON and generates human-facing insights. Every
material claim must identify its evidence, such as game identity, move number,
played move, engine alternative, evaluation change, or principal variation.

The agent must:

- separate engine facts from interpretation;
- avoid claiming a single engine line is the only practical choice;
- account for analysis depth and incomplete results;
- prioritize a bounded number of useful observations;
- distinguish recurring archive evidence from isolated examples; and
- avoid reproducing Chess.com proprietary move-classification presentation.

The Python core must not call an LLM.

## Interactive Report

The report contains:

- interactive board with previous/next and direct move navigation;
- synchronized move list;
- orientation selection;
- last-move and critical-position highlighting;
- evaluation graph;
- principal variation and numeric/mate evaluation;
- game and engine provenance;
- critical-position index;
- archive summary with links to supporting positions; and
- optional agent-authored insight text supplied as escaped input.

Use a vendored, license-compatible board implementation or a small
repository-owned renderer. Do not depend on a CDN. Reuse Chess.com data, but do
not copy Chess.com piece sets, color palettes, sounds, glyphs, or branding.

## Security And Resource Controls

- Treat API data, PGNs, engine output, filenames, and tags as untrusted.
- Never execute PGN or API content.
- Escape all report text and safely serialize embedded JSON.
- Restrict retrieval and installation commands to documented official hosts.
- Validate redirect destinations before following them.
- Bound file sizes, game counts, retries, engine time, threads, and memory.
- Terminate engine process groups on timeout, cancellation, or failure.
- Use atomic writes and explicit partial-state markers.
- Make archive analysis resumable at completed-game boundaries.
- Avoid shell interpolation when launching Stockfish.
- Record enough provenance to reproduce an analysis.

## Error Model

Use stable error codes for at least:

- unsupported Python version;
- malformed arguments;
- invalid username or month range;
- Chess.com unavailable, rate limited, or malformed response;
- malformed or empty PGN;
- illegal or unsupported game;
- engine missing;
- unsupported Stockfish platform;
- approval declined;
- download or digest failure;
- unsafe archive;
- UCI startup or analysis failure;
- resource limit reached; and
- partial archive completion.

Errors must include an actionable remedy without exposing secrets or raw HTML.

## Testing Strategy

### Unit Tests

- platform, architecture, and CPU detection;
- official artifact selection;
- digest verification and safe extraction;
- Chess.com URL construction, caching, retries, and rate limiting;
- local and downloaded PGN normalization;
- evaluation point-of-view normalization;
- mate-score handling;
- critical-position thresholds;
- archive aggregates;
- schema serialization and validation;
- report escaping and inert JSON embedding.

### Integration Tests

- fake Chess.com HTTP server with `200`, `304`, `429`, and transient errors;
- fake UCI process for deterministic handshake and analysis;
- single-game fast and deep analysis;
- multi-game archive with partial failure and resume;
- end-to-end offline HTML generation.

### Compatibility Tests

Run CI on:

- Linux and Windows;
- Python 3.13.

Keep installer-selection tests fixture-driven. Use a limited scheduled smoke
test to detect official Stockfish metadata changes without making ordinary test
runs depend on external services.

### Skill Validation

- Run the skill creator `quick_validate.py` against all four skill folders.
- Forward-test realistic prompts in clean agent contexts.
- Verify that the agent invokes deterministic commands, requests installation
  approval, and grounds insights in artifacts.

## Acceptance Criteria

1. Fetch one Chess.com month serially and reuse cached content on a repeat run.
2. Import an arbitrary local PGN through the same normalized pipeline.
3. Detect or install a compatible official Stockfish binary after approval.
4. Analyze a selected game at depth 16 and deepen selected critical positions.
5. Analyze a multi-game archive and resume after an interrupted run.
6. Produce schema-valid JSON with complete engine and source provenance.
7. Produce a self-contained interactive HTML report that works offline.
8. Generate agent insights traceable to concrete game and engine evidence.
9. Pass unit, integration, platform, schema, and skill validation checks.
10. Preserve boundaries suitable for a future Lichess adapter and thin MCP
    wrapper.

## Licensing

License the repository under GPL-3.0-or-later or another explicitly
GPL-compatible license selected before distribution. Track third-party notices
for Stockfish, `python-chess`, and any vendored report assets. Do not bundle a
Stockfish binary in the repository.

## External References

- Chess.com PubAPI guidance:
  https://support.chess.com/en/articles/9650547-what-is-the-pubapi-and-how-do-i-use-it
- Stockfish downloads:
  https://stockfishchess.org/download/
- Official Stockfish repository:
  https://github.com/official-stockfish/Stockfish
- Stockfish download and usage documentation:
  https://official-stockfish.github.io/docs/stockfish-wiki/Download-and-usage.html
- Python version status:
  https://devguide.python.org/versions/
- MCP tools and resources:
  https://modelcontextprotocol.io/specification/2025-06-18/server/tools
  and
  https://modelcontextprotocol.io/specification/2025-06-18/server/resources
