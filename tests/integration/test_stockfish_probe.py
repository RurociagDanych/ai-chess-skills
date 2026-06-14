import os
import sys
import time
from pathlib import Path

import pytest

from ai_chess.errors import AppError, ErrorCode
from ai_chess.stockfish import probe_engine


FAKE_UCI = Path(__file__).parents[1] / "fixtures" / "fake_uci.py"


def test_probe_engine_performs_uci_handshake() -> None:
    engine = probe_engine([sys.executable, str(FAKE_UCI)])

    assert engine.name == "FixtureFish 1.0"
    assert engine.version == "1.0"
    assert engine.path == sys.executable
    assert engine.options["Threads"]["default"] == 1
    assert engine.options["command"] == [sys.executable, str(FAKE_UCI)]


def test_probe_engine_parses_integer_stockfish_version() -> None:
    engine = probe_engine(
        [sys.executable, str(FAKE_UCI), "integer-version"],
    )

    assert engine.name == "Stockfish 17"
    assert engine.version == "17"


@pytest.mark.parametrize("mode", ["timeout", "malformed"])
def test_probe_engine_rejects_timeout_or_malformed_handshake(mode: str) -> None:
    with pytest.raises(AppError) as caught:
        probe_engine([sys.executable, str(FAKE_UCI), mode], timeout=0.1)

    assert caught.value.code == ErrorCode.ENGINE_FAILED


@pytest.mark.parametrize("mode", ["invalid-bytes", "flood", "malformed-option"])
def test_probe_engine_converts_invalid_or_excess_output_to_engine_error(
    mode: str,
) -> None:
    with pytest.raises(AppError) as caught:
        probe_engine([sys.executable, str(FAKE_UCI), mode], timeout=1.0)

    assert caught.value.code == ErrorCode.ENGINE_FAILED


@pytest.mark.skipif(os.name != "posix", reason="POSIX process groups required")
def test_probe_failure_terminates_spawned_child(tmp_path: Path) -> None:
    pid_path = tmp_path / "child.pid"

    with pytest.raises(AppError):
        probe_engine(
            [
                sys.executable,
                str(FAKE_UCI),
                "spawn-child-malformed",
                str(pid_path),
            ],
            timeout=1.0,
        )

    child_pid = int(pid_path.read_text(encoding="ascii"))
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.02)
    else:
        pytest.fail("probe left its child process running")
