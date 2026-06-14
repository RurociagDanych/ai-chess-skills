# AI Chess Skills Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a Codex-first, Python 3.13 toolkit that fetches or imports PGNs, installs official Stockfish builds on Linux and Windows after approval, analyzes individual games and archives, and generates offline interactive HTML reports.

**Architecture:** Keep all deterministic behavior in an installable `ai_chess` Python package with typed application functions and a JSON-speaking CLI. Keep four Codex skills as thin orchestration layers over that CLI, leaving stable boundaries for future Lichess and MCP adapters.

**Tech Stack:** Python 3.13, `chess`/python-chess, `httpx`, `platformdirs`, `pytest`, stdlib `argparse`, HTML/CSS/JavaScript, GitHub Actions.

---

## File Map

```text
pyproject.toml                         Package metadata, dependencies, CLI entry point, pytest config
LICENSE                               GPL-3.0-or-later repository license
.gitignore                            Python, analysis, cache, and tool artifacts
src/ai_chess/__init__.py              Package version
src/ai_chess/errors.py                Stable application error codes
src/ai_chess/models.py                Versioned manifest and analysis dataclasses
src/ai_chess/io.py                    Atomic JSON/file writes and hashes
src/ai_chess/pgn.py                   PGN import, validation, and game identity
src/ai_chess/chesscom.py              Serial PubAPI retrieval and HTTP caching
src/ai_chess/stockfish.py             Platform detection, artifact selection, safe install, UCI probe
src/ai_chess/analysis.py              Fast/deep engine analysis and archive aggregation
src/ai_chess/reports.py               Offline interactive HTML generation
src/ai_chess/cli.py                   Stable CLI and JSON stdout contract
src/ai_chess/assets/report.html       Vendored report template
skills/*/SKILL.md                     Codex orchestration instructions
skills/*/agents/openai.yaml           Codex UI metadata
tests/fixtures/*.pgn                  Legal, multi-game, and malformed PGN fixtures
tests/fixtures/fake_uci.py             Deterministic UCI test process
tests/unit/*                           Pure behavior tests
tests/integration/*                    HTTP, UCI, CLI, resume, and report tests
.github/workflows/ci.yml              Linux/Windows Python 3.13 validation
.github/workflows/stockfish-smoke.yml Scheduled official metadata smoke test
```

## Task 1: Initialize The Package

**Files:**
- Create: `.gitignore`
- Create: `LICENSE`
- Create: `pyproject.toml`
- Create: `src/ai_chess/__init__.py`
- Create: `src/ai_chess/cli.py`
- Create: `tests/unit/test_cli.py`

- [ ] **Step 1: Initialize Git**

Run:

```bash
git init
```

Expected: an empty Git repository in `/home/topek/repos/ai-chess-skills`.

- [ ] **Step 2: Write the failing CLI version test**

```python
# tests/unit/test_cli.py
from ai_chess.cli import main


def test_version_prints_json(capsys):
    assert main(["--version"]) == 0
    assert capsys.readouterr().out == '{"version":"0.1.0"}\n'
```

- [ ] **Step 3: Run the test to verify it fails**

Run:

```bash
python3.13 -m pytest tests/unit/test_cli.py -v
```

Expected: FAIL because the package is not configured.

- [ ] **Step 4: Add minimal package configuration**

```toml
# pyproject.toml
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "ai-chess-skills"
version = "0.1.0"
description = "Deterministic chess analysis tools for agent skills"
requires-python = ">=3.13,<3.14"
license = { text = "GPL-3.0-or-later" }
dependencies = [
  "chess>=1.11,<2",
  "httpx>=0.28,<1",
  "platformdirs>=4.3,<5",
]

[project.optional-dependencies]
dev = ["pytest>=8.3,<9", "pytest-cov>=6,<7"]

[project.scripts]
ai-chess = "ai_chess.cli:entrypoint"

[tool.pytest.ini_options]
addopts = "-q"
testpaths = ["tests"]
```

```python
# src/ai_chess/__init__.py
__version__ = "0.1.0"
```

```python
# src/ai_chess/cli.py
import argparse
import json
from collections.abc import Sequence

from ai_chess import __version__


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ai-chess")
    parser.add_argument("--version", action="store_true")
    args = parser.parse_args(argv)
    if args.version:
        print(json.dumps({"version": __version__}, separators=(",", ":")))
    return 0


def entrypoint() -> None:
    raise SystemExit(main())
```

Create `.gitignore` with `.venv/`, `__pycache__/`, `.pytest_cache/`, `.coverage`,
`dist/`, `build/`, `*.egg-info/`, `.ai-chess/`, and generated `*.analysis.json`
and `*.report.html` files. Copy the canonical GPL-3.0-or-later text into
`LICENSE`.

- [ ] **Step 5: Install the development environment**

Run:

```bash
if command -v uv >/dev/null; then uv sync --extra dev; else python3.13 -m venv .venv && .venv/bin/python -m pip install -e '.[dev]'; fi
```

Expected: dependencies install into `.venv`.

- [ ] **Step 6: Run the test**

Run:

```bash
.venv/bin/python -m pytest tests/unit/test_cli.py -v
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add .gitignore LICENSE pyproject.toml src tests
git commit -m "chore: initialize ai chess package"
```

## Task 2: Add Stable Errors, Atomic IO, And Versioned Models

**Files:**
- Create: `src/ai_chess/errors.py`
- Create: `src/ai_chess/io.py`
- Create: `src/ai_chess/models.py`
- Create: `tests/unit/test_io.py`
- Create: `tests/unit/test_models.py`

- [ ] **Step 1: Write failing model and IO tests**

```python
# tests/unit/test_io.py
import json

from ai_chess.io import atomic_write_json, sha256_file


def test_atomic_write_json_and_hash(tmp_path):
    path = tmp_path / "artifact.json"
    atomic_write_json(path, {"schema_version": "manifest.v1"})
    assert json.loads(path.read_text()) == {"schema_version": "manifest.v1"}
    assert len(sha256_file(path)) == 64
    assert not list(tmp_path.glob("*.tmp"))
```

```python
# tests/unit/test_models.py
from ai_chess.models import SourceFile, SourceManifest


def test_manifest_serializes_stable_schema():
    manifest = SourceManifest(
        source_kind="local",
        source_ref="/tmp/game.pgn",
        files=[SourceFile(path="game.pgn", sha256="a" * 64, game_count=1)],
    )
    payload = manifest.to_dict()
    assert payload["schema_version"] == "manifest.v1"
    assert payload["files"][0]["game_count"] == 1
```

- [ ] **Step 2: Verify failure**

Run:

```bash
.venv/bin/python -m pytest tests/unit/test_io.py tests/unit/test_models.py -v
```

Expected: FAIL because the modules do not exist.

- [ ] **Step 3: Implement errors and atomic IO**

```python
# src/ai_chess/errors.py
from dataclasses import dataclass
from enum import StrEnum


class ErrorCode(StrEnum):
    INVALID_INPUT = "invalid_input"
    HTTP_ERROR = "http_error"
    RATE_LIMITED = "rate_limited"
    INVALID_PGN = "invalid_pgn"
    ENGINE_MISSING = "engine_missing"
    UNSUPPORTED_PLATFORM = "unsupported_platform"
    APPROVAL_REQUIRED = "approval_required"
    DOWNLOAD_FAILED = "download_failed"
    UNSAFE_ARCHIVE = "unsafe_archive"
    ENGINE_FAILED = "engine_failed"
    PARTIAL_ANALYSIS = "partial_analysis"


@dataclass(slots=True)
class AppError(Exception):
    code: ErrorCode
    message: str
    remedy: str

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message, "remedy": self.remedy}
```

```python
# src/ai_chess/io.py
import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=True, separators=(",", ":"))
            handle.write("\n")
        os.replace(temp_name, path)
    except BaseException:
        Path(temp_name).unlink(missing_ok=True)
        raise
```

- [ ] **Step 4: Implement the artifact models**

Define frozen, slotted dataclasses in `models.py`:

```python
@dataclass(frozen=True, slots=True)
class SourceFile:
    path: str
    sha256: str
    game_count: int
    source_url: str | None = None
    etag: str | None = None
    last_modified: str | None = None


@dataclass(frozen=True, slots=True)
class SourceManifest:
    source_kind: Literal["local", "chesscom"]
    source_ref: str
    files: list[SourceFile]
    schema_version: str = "manifest.v1"
    created_at: str = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, object]:
        return asdict(self)
```

Also define `EngineInfo`, `Evaluation`, `PlyAnalysis`, `CriticalPosition`,
`GameAnalysis`, `ArchiveAggregate`, and `AnalysisArtifact`. Use
`schema_version="analysis.v1"`, store mate separately from centipawns, and give
every class a `to_dict()` method based on `dataclasses.asdict`.

- [ ] **Step 5: Run tests**

Run:

```bash
.venv/bin/python -m pytest tests/unit/test_io.py tests/unit/test_models.py -v
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/ai_chess tests/unit
git commit -m "feat: add versioned artifact models"
```

## Task 3: Import And Normalize PGNs

**Files:**
- Create: `src/ai_chess/pgn.py`
- Create: `tests/fixtures/single.pgn`
- Create: `tests/fixtures/archive.pgn`
- Create: `tests/fixtures/malformed.pgn`
- Create: `tests/unit/test_pgn.py`

- [ ] **Step 1: Add focused PGN fixtures**

Use a legal Scholar's Mate for `single.pgn`, two short legal games for
`archive.pgn`, and a game containing an illegal SAN token for `malformed.pgn`.
Keep fixtures under 30 plies.

- [ ] **Step 2: Write failing PGN tests**

```python
from pathlib import Path

import pytest

from ai_chess.errors import AppError, ErrorCode
from ai_chess.pgn import import_pgn

FIXTURES = Path(__file__).parents[1] / "fixtures"


def test_import_pgn_builds_manifest(tmp_path):
    result = import_pgn(FIXTURES / "archive.pgn", tmp_path)
    assert result.manifest.files[0].game_count == 2
    assert len(result.games) == 2
    assert result.games[0].game_id


def test_import_rejects_malformed_game(tmp_path):
    with pytest.raises(AppError) as error:
        import_pgn(FIXTURES / "malformed.pgn", tmp_path)
    assert error.value.code == ErrorCode.INVALID_PGN
```

- [ ] **Step 3: Verify failure**

Run:

```bash
.venv/bin/python -m pytest tests/unit/test_pgn.py -v
```

Expected: FAIL because `import_pgn` does not exist.

- [ ] **Step 4: Implement PGN normalization**

In `pgn.py`, define:

```python
@dataclass(slots=True)
class ImportedGame:
    game_id: str
    game: chess.pgn.Game


@dataclass(slots=True)
class ImportResult:
    manifest: SourceManifest
    manifest_path: Path
    pgn_path: Path
    games: list[ImportedGame]
```

Implement `read_games(path)` with `chess.pgn.read_game`, reject parser errors
from `game.errors`, reject empty files, and derive `game_id` as SHA-256 of
normalized headers plus mainline UCI moves. Implement `import_pgn(source,
output_dir)` by copying bytes atomically, parsing the copied file, and writing
`manifest.v1.json`.

- [ ] **Step 5: Run tests**

Run:

```bash
.venv/bin/python -m pytest tests/unit/test_pgn.py -v
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/ai_chess/pgn.py tests/fixtures tests/unit/test_pgn.py
git commit -m "feat: import and normalize pgn files"
```

## Task 4: Fetch Chess.com Archives With Cache Semantics

**Files:**
- Create: `src/ai_chess/chesscom.py`
- Create: `tests/unit/test_chesscom.py`
- Create: `tests/integration/test_chesscom_fetch.py`

- [ ] **Step 1: Write failing URL and validation tests**

```python
import pytest

from ai_chess.chesscom import iter_months, monthly_games_url, monthly_pgn_url
from ai_chess.errors import AppError


def test_month_range_is_inclusive():
    assert iter_months("2026-01", "2026-03") == [
        "2026-01", "2026-02", "2026-03"
    ]


def test_monthly_url_normalizes_username():
    assert monthly_pgn_url("Hikaru", "2026-01") == (
        "https://api.chess.com/pub/player/hikaru/games/2026/01/pgn"
    )
    assert monthly_games_url("Hikaru", "2026-01") == (
        "https://api.chess.com/pub/player/hikaru/games/2026/01"
    )


def test_invalid_username_is_rejected():
    with pytest.raises(AppError):
        monthly_pgn_url("../user", "2026-01")
```

- [ ] **Step 2: Write the failing cached-fetch integration test**

Use `httpx.MockTransport` to return `200` with `ETag` on the first request and
`304` when `If-None-Match` is present on the second. Assert:

```python
assert first.manifest.files[0].game_count == 1
assert second.pgn_paths == first.pgn_paths
assert requests[1].headers["if-none-match"] == '"fixture-v1"'
assert all(requests[index].timestamp <= requests[index + 1].timestamp
           for index in range(len(requests) - 1))
```

Add a second transport sequence returning `429` with `Retry-After: 0`, then
`200`, and assert exactly two serial requests.

- [ ] **Step 3: Verify failure**

Run:

```bash
.venv/bin/python -m pytest tests/unit/test_chesscom.py tests/integration/test_chesscom_fetch.py -v
```

Expected: FAIL because the Chess.com client does not exist.

- [ ] **Step 4: Implement the Chess.com client**

Implement `iter_months(start: str, end: str) -> list[str]`,
`monthly_games_url(username: str, month: str) -> str`,
`monthly_pgn_url(username: str, month: str) -> str`, and a
`ChessComClient(client, user_agent, cache_dir, sleep=time.sleep)` class with
`fetch_months(username, months, output_dir) -> FetchResult`. Retrieve monthly
JSON first to preserve public game metadata, then retrieve the original PGN.

Validate usernames with `^[A-Za-z0-9_-]{3,25}$`, accept only
`api.chess.com`, disable automatic cross-host redirects, cap responses at
64 MiB, require PGN-compatible content, persist URL/ETag/Last-Modified cache
metadata, issue requests one at a time, honor `Retry-After`, and retry transient
errors at most three times. Store monthly JSON beside the PGN, include both
source URLs in provenance, and parse all downloaded PGNs through `read_games`.

- [ ] **Step 5: Run tests**

Run:

```bash
.venv/bin/python -m pytest tests/unit/test_chesscom.py tests/integration/test_chesscom_fetch.py -v
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/ai_chess/chesscom.py tests/unit/test_chesscom.py tests/integration/test_chesscom_fetch.py
git commit -m "feat: fetch cached chess.com pgn archives"
```

## Task 5: Resolve And Safely Install Official Stockfish

**Files:**
- Create: `src/ai_chess/stockfish.py`
- Create: `tests/fixtures/stockfish_release.json`
- Create: `tests/fixtures/fake_uci.py`
- Create: `tests/unit/test_stockfish.py`
- Create: `tests/integration/test_stockfish_probe.py`

- [ ] **Step 1: Write failing platform-selection tests**

```python
from ai_chess.stockfish import PlatformTarget, ReleaseAsset, select_asset


def test_selects_windows_arm64_asset():
    assets = [
        ReleaseAsset("stockfish-windows-x64-avx2.zip", "https://example/x64", None),
        ReleaseAsset("stockfish-windows-arm64.zip", "https://example/arm64", None),
    ]
    selected = select_asset(assets, PlatformTarget("windows", "arm64", frozenset()))
    assert selected.name == "stockfish-windows-arm64.zip"


def test_selects_conservative_linux_x64_asset_without_cpu_flags():
    assets = [
        ReleaseAsset("stockfish-linux-x64-avx2.tar.gz", "https://example/avx2", None),
        ReleaseAsset("stockfish-linux-x64.tar.gz", "https://example/base", None),
    ]
    selected = select_asset(assets, PlatformTarget("linux", "x86_64", frozenset()))
    assert selected.name == "stockfish-linux-x64.tar.gz"
```

Also test unsupported macOS and missing official Linux ARM64 assets return
`UNSUPPORTED_PLATFORM`.

- [ ] **Step 2: Write failing safe-extraction tests**

Create in-memory ZIP and TAR archives with `../../escape` entries and assert
`extract_official_archive()` raises `AppError(ErrorCode.UNSAFE_ARCHIVE)`.
Create a valid archive containing one `stockfish` executable and assert it is
selected.

- [ ] **Step 3: Write a fake UCI engine and probe test**

`tests/fixtures/fake_uci.py` must respond:

```python
for line in sys.stdin:
    command = line.strip()
    if command == "uci":
        print("id name FixtureFish 1.0", flush=True)
        print("uciok", flush=True)
    elif command == "isready":
        print("readyok", flush=True)
    elif command == "quit":
        break
```

Assert `probe_engine([sys.executable, fake_path]).name == "FixtureFish 1.0"`.

- [ ] **Step 4: Verify failure**

Run:

```bash
.venv/bin/python -m pytest tests/unit/test_stockfish.py tests/integration/test_stockfish_probe.py -v
```

Expected: FAIL because Stockfish support does not exist.

- [ ] **Step 5: Implement platform and release resolution**

Define:

```python
@dataclass(frozen=True, slots=True)
class PlatformTarget:
    os: Literal["linux", "windows"]
    arch: Literal["x86_64", "arm64"]
    cpu_flags: frozenset[str]


@dataclass(frozen=True, slots=True)
class ReleaseAsset:
    name: str
    url: str
    digest: str | None
```

Implement OS/architecture normalization, Linux `/proc/cpuinfo` capability
reading, Windows processor capability fallback, GitHub
`official-stockfish/Stockfish/releases/latest` metadata parsing, and deterministic
asset selection. Prefer architecture compatibility first, then AVX2 only when
confirmed, otherwise use the universal/base asset. Reject macOS in v1.

- [ ] **Step 6: Implement discovery, approval boundary, and installation**

Implement these exact interfaces: `discover_engine(explicit, cache_dir)`,
`build_install_plan(version, target, install_dir)`, `install_engine(plan,
approved)`, and `probe_engine(command, timeout=5.0)`. Return `EngineInfo | None`,
`InstallPlan`, `EngineInfo`, and `EngineInfo`, respectively.

`install_engine` must fail with `APPROVAL_REQUIRED` unless `approved=True`.
Allow downloads only from `stockfishchess.org` and
`github.com/official-stockfish` release hosts, verify a published SHA-256 when
present, always record computed archive/binary hashes, reject traversal and
symlinks, extract to a temporary directory, and atomically move the executable
into the user cache returned by `platformdirs.user_data_path("ai-chess")`.

- [ ] **Step 7: Run tests**

Run:

```bash
.venv/bin/python -m pytest tests/unit/test_stockfish.py tests/integration/test_stockfish_probe.py -v
```

Expected: PASS without downloading a real binary.

- [ ] **Step 8: Commit**

```bash
git add src/ai_chess/stockfish.py tests/fixtures tests/unit/test_stockfish.py tests/integration/test_stockfish_probe.py
git commit -m "feat: install and verify official stockfish"
```

## Task 6: Analyze One Game With Deterministic UCI Evidence

**Files:**
- Create: `src/ai_chess/analysis.py`
- Extend: `tests/fixtures/fake_uci.py`
- Create: `tests/unit/test_analysis.py`
- Create: `tests/integration/test_game_analysis.py`

- [ ] **Step 1: Write failing evaluation-normalization tests**

```python
import chess

from ai_chess.analysis import normalize_score


def test_normalizes_centipawns_to_white_point_of_view():
    score = chess.engine.PovScore(chess.engine.Cp(80), chess.BLACK)
    assert normalize_score(score) == {"centipawns": -80, "mate": None}


def test_preserves_mate_separately():
    score = chess.engine.PovScore(chess.engine.Mate(3), chess.WHITE)
    assert normalize_score(score) == {"centipawns": None, "mate": 3}
```

- [ ] **Step 2: Write failing critical-position tests**

Create a list named `plies` of synthetic positions where White changes from
`+50` before its move to `-250` after its move. Assert
`select_critical_positions(plies, threshold=200, limit=8)` returns that move
with a 300 cp loss and label `"mistake"`. Add mate transition and top-N cap
cases.

- [ ] **Step 3: Extend the fake UCI engine**

Handle `position`, `setoption`, and `go depth N`. Emit deterministic lines:

```text
info depth 16 score cp 42 pv e2e4 e7e5
bestmove e2e4
```

Change the score based on a small mapping from received FEN or move count so the
integration fixture contains one known critical move.

- [ ] **Step 4: Write the failing game-analysis integration test**

```python
artifact = analyze_game(
    game=read_games(FIXTURES / "single.pgn")[0],
    engine_command=[sys.executable, str(FIXTURES / "fake_uci.py")],
    settings=AnalysisSettings(depth=16, deep_depth=22, threads=1, hash_mb=16),
)
assert artifact.schema_version == "analysis.v1"
assert artifact.games[0].plies
assert artifact.engine.name == "FixtureFish 1.0"
assert all(ply.depth in {16, 22} for ply in artifact.games[0].plies)
```

- [ ] **Step 5: Verify failure**

Run:

```bash
.venv/bin/python -m pytest tests/unit/test_analysis.py tests/integration/test_game_analysis.py -v
```

Expected: FAIL because analysis functions do not exist.

- [ ] **Step 6: Implement analysis**

Define `AnalysisSettings` with defaults:

```python
depth: int = 16
deep_depth: int | None = 22
threads: int = 1
hash_mb: int = 128
timeout_seconds: float = 30.0
critical_threshold_cp: int = 150
max_critical_positions: int = 8
```

Use `chess.engine.SimpleEngine.popen_uci`, configure threads/hash, analyze the
initial position and every mainline position, capture one PV, normalize all
scores to White's point of view, calculate loss relative to the mover, select
critical positions, and deepen only selected positions. Ensure `engine.quit()`
runs in `finally`.

- [ ] **Step 7: Run tests**

Run:

```bash
.venv/bin/python -m pytest tests/unit/test_analysis.py tests/integration/test_game_analysis.py -v
```

Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add src/ai_chess/analysis.py tests/fixtures/fake_uci.py tests/unit/test_analysis.py tests/integration/test_game_analysis.py
git commit -m "feat: analyze chess games with stockfish"
```

## Task 7: Analyze Archives, Persist Progress, And Resume

**Files:**
- Modify: `src/ai_chess/analysis.py`
- Create: `tests/unit/test_aggregates.py`
- Create: `tests/integration/test_archive_resume.py`

- [ ] **Step 1: Write failing aggregate tests**

Construct two `GameAnalysis` objects and assert:

```python
aggregate = aggregate_games(games)
assert aggregate.games_total == 2
assert aggregate.critical_positions == 3
assert aggregate.by_phase == {"opening": 1, "middlegame": 2, "endgame": 0}
assert aggregate.evidence[0].game_id
```

Use deterministic phase boundaries: opening plies `0..19`, middlegame `20..59`,
endgame `60+`.

- [ ] **Step 2: Write the failing resume integration test**

Run archive analysis with an injected analyzer that succeeds for game one and
raises on game two. Assert the partial JSON contains game one and
`complete=false`. Run again with a successful analyzer and assert game one is
not re-analyzed, both games are present, and `complete=true`.

- [ ] **Step 3: Verify failure**

Run:

```bash
.venv/bin/python -m pytest tests/unit/test_aggregates.py tests/integration/test_archive_resume.py -v
```

Expected: FAIL because archive functions do not exist.

- [ ] **Step 4: Implement archive analysis**

Implement `aggregate_games(games: Sequence[GameAnalysis]) ->
ArchiveAggregate` and `analyze_archive(games: Sequence[ImportedGame],
engine_command: Sequence[str], settings: AnalysisSettings, output_path: Path)
-> AnalysisArtifact`.

Load a matching partial artifact if present, key completed work by stable
`game_id`, append one completed game at a time, atomically rewrite JSON after
each game, collect per-game errors, set `complete` only after all games have
been attempted, and produce evidence links as `{game_id, ply}` records.

- [ ] **Step 5: Run tests**

Run:

```bash
.venv/bin/python -m pytest tests/unit/test_aggregates.py tests/integration/test_archive_resume.py -v
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/ai_chess/analysis.py tests/unit/test_aggregates.py tests/integration/test_archive_resume.py
git commit -m "feat: add resumable archive analysis"
```

## Task 8: Generate A Self-Contained Interactive Report

**Files:**
- Create: `src/ai_chess/assets/report.html`
- Create: `src/ai_chess/reports.py`
- Create: `tests/unit/test_reports.py`
- Create: `tests/integration/test_report_generation.py`

- [ ] **Step 1: Write failing escaping and offline tests**

```python
from ai_chess.reports import render_report


def test_report_escapes_untrusted_headers(sample_artifact):
    sample_artifact.games[0].headers["White"] = "</script><script>alert(1)</script>"
    html = render_report(sample_artifact)
    assert "</script><script>alert(1)</script>" not in html
    assert "\\u003c/script\\u003e" in html


def test_report_has_no_network_dependencies(sample_artifact):
    html = render_report(sample_artifact)
    assert "http://" not in html
    assert "https://" not in html
    assert "<script src=" not in html
    assert "<link href=" not in html
```

- [ ] **Step 2: Write the failing interaction-structure test**

Assert generated HTML contains:

```python
for element_id in (
    "board", "move-list", "evaluation-graph", "critical-list",
    "previous-move", "next-move", "orientation"
):
    assert f'id="{element_id}"' in html
```

- [ ] **Step 3: Verify failure**

Run:

```bash
.venv/bin/python -m pytest tests/unit/test_reports.py tests/integration/test_report_generation.py -v
```

Expected: FAIL because report generation does not exist.

- [ ] **Step 4: Implement the HTML template**

Build a single vendored template with:

- CSS Grid layout;
- a repository-owned 8x8 CSS board;
- Unicode chess-piece rendering;
- previous/next buttons and clickable move list;
- orientation selector;
- SVG evaluation polyline;
- critical-position markers;
- PV and provenance panels;
- archive aggregate section.

The JavaScript must consume only:

```html
<script id="analysis-data" type="application/json">__ANALYSIS_JSON__</script>
```

and must not call `fetch`, import modules, or access external assets.

- [ ] **Step 5: Implement safe rendering**

```python
def inert_json(payload: object) -> str:
    return (
        json.dumps(payload, ensure_ascii=True, separators=(",", ":"))
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
    )


```

Add `render_report(artifact, insights=None) -> str` and
`write_report(artifact_path, output_path, insights_path=None) -> Path`. Load the
template with `importlib.resources.files`, insert only inert JSON, and write
atomically.

- [ ] **Step 6: Run tests**

Run:

```bash
.venv/bin/python -m pytest tests/unit/test_reports.py tests/integration/test_report_generation.py -v
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/ai_chess/assets src/ai_chess/reports.py tests/unit/test_reports.py tests/integration/test_report_generation.py
git commit -m "feat: generate offline interactive chess reports"
```

## Task 9: Complete The JSON CLI

**Files:**
- Modify: `src/ai_chess/cli.py`
- Create: `tests/integration/test_cli_workflows.py`

- [ ] **Step 1: Write failing parser tests for every command**

Parameterize these argument lists:

```python
[
    ["doctor"],
    ["setup-engine", "--version", "latest"],
    ["fetch", "chesscom", "hikaru", "--month", "2026-01"],
    ["fetch", "chesscom", "hikaru", "--from", "2026-01", "--to", "2026-03"],
    ["import", "tests/fixtures/single.pgn"],
    ["analyze", "game", "tests/fixtures/single.pgn", "--game", "1", "--depth", "16"],
    ["analyze", "archive", "tests/fixtures/archive.pgn", "--depth", "16", "--deep-depth", "22"],
    ["report", "analysis.v1.json", "--output", "report.html"],
]
```

Assert parser construction accepts each shape. Assert mutually exclusive month
and range arguments reject ambiguous input.

- [ ] **Step 2: Write failing stdout/stderr contract tests**

Monkeypatch each application function. Assert success writes one compact JSON
object to stdout, status messages only to stderr, and returns `0`. Raise
`AppError` and assert stdout contains:

```json
{"ok":false,"error":{"code":"invalid_input","message":"invalid month range","remedy":"use YYYY-MM values in ascending order"}}
```

with a nonzero exit.

- [ ] **Step 3: Verify failure**

Run:

```bash
.venv/bin/python -m pytest tests/integration/test_cli_workflows.py -v
```

Expected: FAIL because subcommands do not exist.

- [ ] **Step 4: Implement command parsing and dispatch**

Add `build_parser()` and one handler per command. Use dependency injection for
HTTP client, engine resolver, and analyzer in tests. Require:

- `--user-agent` or `AI_CHESS_USER_AGENT` for Chess.com;
- `--approve-download` for the actual setup-engine install step;
- `--plan-only` to resolve and print official Stockfish metadata without
  downloading;
- explicit output directories;
- positive bounded depth, threads, hash, game count, and timeout values;
- zero-based internals but one-based `--game` user input.

`doctor` must report Python version, platform support, writable cache path,
package version, and discovered Stockfish without making network requests.

- [ ] **Step 5: Run tests**

Run:

```bash
.venv/bin/python -m pytest tests/integration/test_cli_workflows.py -v
```

Expected: PASS.

- [ ] **Step 6: Run the complete Python suite**

Run:

```bash
.venv/bin/python -m pytest -v
```

Expected: all tests PASS.

- [ ] **Step 7: Commit**

```bash
git add src/ai_chess/cli.py tests/integration/test_cli_workflows.py
git commit -m "feat: expose chess workflows through json cli"
```

## Task 10: Create And Validate The Four Codex Skills

**Files:**
- Create: `skills/fetch-chess-games/SKILL.md`
- Create: `skills/fetch-chess-games/agents/openai.yaml`
- Create: `skills/setup-chess-engine/SKILL.md`
- Create: `skills/setup-chess-engine/agents/openai.yaml`
- Create: `skills/analyze-chess-games/SKILL.md`
- Create: `skills/analyze-chess-games/agents/openai.yaml`
- Create: `skills/visualize-chess-analysis/SKILL.md`
- Create: `skills/visualize-chess-analysis/agents/openai.yaml`

- [ ] **Step 1: Initialize each skill with the skill-creator script**

Run from `/home/topek/.codex/skills/.system/skill-creator`:

```bash
python3.13 scripts/init_skill.py fetch-chess-games --path /home/topek/repos/ai-chess-skills/skills --interface display_name="Fetch Chess Games" --interface short_description="Fetch Chess.com games or import local PGNs" --interface default_prompt="Use $fetch-chess-games to retrieve my Chess.com games or import a local PGN."
python3.13 scripts/init_skill.py setup-chess-engine --path /home/topek/repos/ai-chess-skills/skills --interface display_name="Set Up Chess Engine" --interface short_description="Install and verify an official Stockfish engine" --interface default_prompt="Use $setup-chess-engine to detect or install Stockfish after asking for approval."
python3.13 scripts/init_skill.py analyze-chess-games --path /home/topek/repos/ai-chess-skills/skills --interface display_name="Analyze Chess Games" --interface short_description="Analyze a game or archive with Stockfish" --interface default_prompt="Use $analyze-chess-games to analyze this PGN and explain evidence-backed improvements."
python3.13 scripts/init_skill.py visualize-chess-analysis --path /home/topek/repos/ai-chess-skills/skills --interface display_name="Visualize Chess Analysis" --interface short_description="Build an offline interactive chess report" --interface default_prompt="Use $visualize-chess-analysis to create an interactive HTML report from this analysis."
```

Expected: four valid skill directories with `SKILL.md` and
`agents/openai.yaml`.

- [ ] **Step 2: Replace generated skill bodies with concise workflows**

Each frontmatter must contain only `name` and `description`.

`fetch-chess-games` must instruct the agent to validate source choice, use
serial Chess.com retrieval with an identifying user agent, preserve manifest
paths, and never start analysis.

`setup-chess-engine` must instruct the agent to run `doctor`, display the
install plan, request approval immediately before download, and only then rerun
with `--approve-download`.

`analyze-chess-games` must instruct the agent to choose game/archive mode, use
bounded defaults, inspect `analysis.v1.json`, distinguish engine fact from
interpretation, and cite game/move/evaluation/PV evidence.

`visualize-chess-analysis` must instruct the agent to generate a report from
validated artifacts, avoid external servers/assets, and open the file only when
the user requests it and the host allows GUI actions.

- [ ] **Step 3: Validate all skills**

Run:

```bash
for skill in skills/*; do python3.13 /home/topek/.codex/skills/.system/skill-creator/scripts/quick_validate.py "$skill"; done
```

Expected: all four skills report successful validation.

- [ ] **Step 4: Smoke-test skill command references**

Run every command shown in the four skill files with `--help`, for example:

```bash
.venv/bin/ai-chess fetch chesscom --help
.venv/bin/ai-chess setup-engine --help
.venv/bin/ai-chess analyze game --help
.venv/bin/ai-chess report --help
```

Expected: exit code `0`; no stale command names.

- [ ] **Step 5: Commit**

```bash
git add skills
git commit -m "feat: add codex chess workflow skills"
```

## Task 11: Add Linux And Windows CI

**Files:**
- Create: `.github/workflows/ci.yml`
- Create: `.github/workflows/stockfish-smoke.yml`

- [ ] **Step 1: Add the main CI workflow**

```yaml
name: CI

on:
  push:
  pull_request:

jobs:
  test:
    strategy:
      fail-fast: false
      matrix:
        os: [ubuntu-latest, windows-latest]
    runs-on: ${{ matrix.os }}
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.13"
          cache: pip
      - run: python -m pip install -e ".[dev]"
      - run: python -m pytest --cov=ai_chess --cov-report=term-missing
      - name: Validate skills
        shell: bash
        run: |
          for skill in skills/*; do
            python scripts/validate_skill.py "$skill"
          done
```

Because CI cannot depend on the developer's global `skill-creator` path, add a
small repository `scripts/validate_skill.py` that checks folder-name equality,
frontmatter containing only `name` and `description`, required
`agents/openai.yaml` fields, and the `$skill-name` default prompt reference.
Keep local `quick_validate.py` validation in Task 10 as the authoritative
skill-creator check.

- [ ] **Step 2: Add scheduled official metadata smoke testing**

```yaml
name: Stockfish Metadata Smoke Test

on:
  schedule:
    - cron: "17 6 * * 1"
  workflow_dispatch:

jobs:
  resolve:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.13"
      - run: python -m pip install -e .
      - run: ai-chess setup-engine --version latest --plan-only
```

The smoke command must resolve metadata but must not download or install.

- [ ] **Step 3: Validate workflow syntax locally**

Run:

```bash
.venv/bin/python -c "import pathlib, yaml; [yaml.safe_load(p.read_text()) for p in pathlib.Path('.github/workflows').glob('*.yml')]"
```

Add `PyYAML>=6,<7` to the dev dependencies before running this command.

Expected: exit code `0`.

- [ ] **Step 4: Run the same checks CI will run**

Run:

```bash
.venv/bin/python -m pytest --cov=ai_chess --cov-report=term-missing
for skill in skills/*; do .venv/bin/python scripts/validate_skill.py "$skill"; done
.venv/bin/ai-chess setup-engine --version latest --plan-only
```

Expected: tests and validation pass; the final command prints an install plan
without downloading.

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml scripts .github
git commit -m "ci: test python package and codex skills"
```

## Task 12: Forward-Test The Product Workflows

**Files:**
- Modify only files implicated by failed forward tests.

- [ ] **Step 1: Forward-test local PGN analysis**

In a fresh agent context, use:

```text
Use $analyze-chess-games at /home/topek/repos/ai-chess-skills/skills/analyze-chess-games to analyze /home/topek/repos/ai-chess-skills/tests/fixtures/single.pgn at the default settings and explain the two most important moments with evidence.
```

Expected: the agent checks engine availability, uses the CLI, and cites concrete
moves, evaluations, and PVs.

- [ ] **Step 2: Forward-test the approval boundary**

In a fresh agent context, use:

```text
Use $setup-chess-engine at /home/topek/repos/ai-chess-skills/skills/setup-chess-engine to install Stockfish for this machine.
```

Expected: the agent displays the planned official source, target, destination,
and verification status, then stops for explicit approval before downloading.

- [ ] **Step 3: Forward-test Chess.com plus reporting**

In a fresh agent context, use:

```text
Use $fetch-chess-games at /home/topek/repos/ai-chess-skills/skills/fetch-chess-games to fetch one specified Chess.com month, then use $visualize-chess-analysis to explain how an offline report would be generated.
```

Use a mocked endpoint or a user-approved real request. Expected: serial
retrieval, manifest preservation, no analysis inside the fetch skill, and no
external report assets.

- [ ] **Step 4: Fix only observed workflow failures**

For each failure, add a regression test first, make the smallest implementation
or skill-instruction change, and rerun the affected forward test.

- [ ] **Step 5: Run final verification**

Run:

```bash
.venv/bin/python -m pytest --cov=ai_chess --cov-report=term-missing
for skill in skills/*; do python3.13 /home/topek/.codex/skills/.system/skill-creator/scripts/quick_validate.py "$skill"; done
.venv/bin/ai-chess doctor
git status --short
```

Expected: all tests and skill validations pass; `doctor` emits valid JSON; Git
shows only intentional tracked changes.

- [ ] **Step 6: Commit forward-test fixes**

```bash
git add src tests skills
git commit -m "test: harden end-to-end chess skill workflows"
```
