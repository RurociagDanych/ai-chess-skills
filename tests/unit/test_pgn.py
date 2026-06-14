import hashlib
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from ai_chess import pgn
from ai_chess.errors import AppError, ErrorCode
from ai_chess.pgn import import_pgn, read_games


FIXTURES = Path(__file__).parents[1] / "fixtures"


def test_read_games_reads_archive():
    games = read_games(FIXTURES / "archive.pgn")

    assert len(games) == 2
    assert games[0].game.headers["Event"] == "Short Game One"
    assert all(len(list(game.game.mainline_moves())) < 30 for game in games)


def test_game_id_is_stable_for_reordered_headers_and_changes_for_moves(tmp_path):
    first = tmp_path / "first.pgn"
    reordered = tmp_path / "reordered.pgn"
    changed = tmp_path / "changed.pgn"
    first.write_text(
        '[Event "Identity"]\n[Site "Warsaw"]\n[White "Alice"]\n'
        '[Black "Bob"]\n[Result "*"]\n\n1. e4 e5 2. Nf3 *\n',
        encoding="ascii",
    )
    reordered.write_text(
        '[Black "Bob"]\n[Result "*"]\n[White "Alice"]\n'
        '[Site "Warsaw"]\n[Event "Identity"]\n\n1. e4 e5 2. Nf3 *\n',
        encoding="ascii",
    )
    changed.write_text(
        '[Event "Identity"]\n[Site "Warsaw"]\n[White "Alice"]\n'
        '[Black "Bob"]\n[Result "*"]\n\n1. e4 e5 2. Nc3 *\n',
        encoding="ascii",
    )

    first_game = read_games(first)[0]
    reordered_game = read_games(reordered)[0]
    changed_game = read_games(changed)[0]

    assert first_game.game_id == reordered_game.game_id
    assert first_game.game_id != changed_game.game_id
    assert len(first_game.game_id) == 64
    assert first_game.game_id == first_game.game_id.lower()
    assert first_game.game.headers["Event"] == "Identity"


@pytest.mark.parametrize(
    ("path", "expected_message"),
    [
        (FIXTURES / "malformed.pgn", "parse"),
    ],
)
def test_read_games_rejects_malformed_pgn(path, expected_message):
    with pytest.raises(AppError) as caught:
        read_games(path)

    assert caught.value.code is ErrorCode.INVALID_PGN
    assert expected_message in caught.value.message.lower()
    assert caught.value.remedy


def test_read_games_rejects_empty_input(tmp_path):
    path = tmp_path / "empty.pgn"
    path.write_bytes(b"")

    with pytest.raises(AppError) as caught:
        read_games(path)

    assert caught.value.code is ErrorCode.INVALID_PGN
    assert "no games" in caught.value.message.lower()
    assert caught.value.remedy


def test_read_games_rejects_junk_as_phantom_game(tmp_path):
    path = tmp_path / "junk.pgn"
    path.write_text("not a pgn\n", encoding="ascii")

    with pytest.raises(AppError) as caught:
        read_games(path)

    assert caught.value.code is ErrorCode.INVALID_PGN


def test_read_games_rejects_valid_game_followed_by_junk(tmp_path):
    path = tmp_path / "trailing-junk.pgn"
    path.write_bytes((FIXTURES / "single.pgn").read_bytes() + b"\nnot a pgn\n")

    with pytest.raises(AppError) as caught:
        read_games(path)

    assert caught.value.code is ErrorCode.INVALID_PGN


def test_read_games_requires_explicit_result_header(tmp_path):
    path = tmp_path / "missing-result.pgn"
    path.write_text(
        '[Event "Missing Result"]\n\n1. e4 *\n',
        encoding="ascii",
    )

    with pytest.raises(AppError) as caught:
        read_games(path)

    assert caught.value.code is ErrorCode.INVALID_PGN


def test_read_games_rejects_truncated_movetext_without_result_token(tmp_path):
    path = tmp_path / "truncated.pgn"
    path.write_text(
        '[Event "Truncated"]\n[Result "*"]\n\n1. e4 e5 2. Nf3\n',
        encoding="ascii",
    )

    with pytest.raises(AppError) as caught:
        read_games(path)

    assert caught.value.code is ErrorCode.INVALID_PGN


def test_read_games_rejects_mismatched_header_and_movetext_results(tmp_path):
    path = tmp_path / "mismatch.pgn"
    path.write_text(
        '[Event "Mismatch"]\n[Result "1-0"]\n\n1. f3 e5 2. g4 Qh4# 0-1\n',
        encoding="ascii",
    )

    with pytest.raises(AppError) as caught:
        read_games(path)

    assert caught.value.code is ErrorCode.INVALID_PGN


def test_import_pgn_copies_bytes_and_writes_valid_manifest(tmp_path):
    source = FIXTURES / "archive.pgn"
    output_dir = tmp_path / "imported"
    expected_bytes = source.read_bytes()
    expected_hash = hashlib.sha256(expected_bytes).hexdigest()
    output_dir.mkdir()
    (output_dir / source.name).write_bytes(b"old contents")

    result = import_pgn(source, output_dir)

    assert len(result.games) == 2
    assert result.pgn_path == output_dir / source.name
    assert result.pgn_path.read_bytes() == expected_bytes
    assert result.manifest_path == output_dir / "manifest.v1.json"
    manifest_json = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    assert manifest_json == result.manifest.to_dict()
    assert result.manifest.source_kind == "local"
    assert result.manifest.source_ref == str(source.resolve())
    assert len(result.manifest.files) == 1
    assert result.manifest.files[0].path == source.name
    assert result.manifest.files[0].sha256 == expected_hash
    assert result.manifest.files[0].game_count == 2


def test_import_pgn_is_safe_when_source_is_destination(tmp_path):
    source = tmp_path / "single.pgn"
    source.write_bytes((FIXTURES / "single.pgn").read_bytes())
    expected_bytes = source.read_bytes()

    result = import_pgn(source, tmp_path)

    assert result.pgn_path == source
    assert source.read_bytes() == expected_bytes
    assert source.is_file()
    assert not source.is_symlink()
    assert len(result.games) == 1
    assert result.manifest_path.is_file()
    assert list(tmp_path.glob("*.tmp")) == []


def test_import_pgn_uses_immutable_staged_snapshot(tmp_path, monkeypatch):
    source = tmp_path / "source" / "game.pgn"
    source.parent.mkdir()
    original_bytes = (FIXTURES / "single.pgn").read_bytes()
    source.write_bytes(original_bytes)
    replacement_bytes = (FIXTURES / "archive.pgn").read_bytes()
    output_dir = tmp_path / "output"
    original_stage_source = pgn._stage_source

    def stage_then_mutate(source_path, staged_path, staged_fd):
        staged_path = original_stage_source(
            source_path,
            staged_path,
            staged_fd,
        )
        source_path.write_bytes(replacement_bytes)
        return staged_path

    monkeypatch.setattr(pgn, "_stage_source", stage_then_mutate)

    result = import_pgn(source, output_dir)

    assert result.pgn_path.read_bytes() == original_bytes
    assert result.manifest.files[0].sha256 == hashlib.sha256(
        original_bytes
    ).hexdigest()
    assert result.manifest.files[0].game_count == 1
    assert len(result.games) == 1


@pytest.mark.skipif(os.name != "posix", reason="POSIX symlinks required")
def test_import_pgn_replaces_destination_symlink_with_regular_file(tmp_path):
    source = tmp_path / "source" / "game.pgn"
    source.parent.mkdir()
    source.write_bytes((FIXTURES / "single.pgn").read_bytes())
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    destination = output_dir / source.name
    destination.symlink_to(source)

    result = import_pgn(source, output_dir)

    assert result.pgn_path == destination
    assert destination.is_file()
    assert not destination.is_symlink()
    assert destination.read_bytes() == source.read_bytes()


@pytest.mark.skipif(os.name != "posix", reason="POSIX symlinks required")
def test_import_pgn_replaces_dangling_destination_symlink(tmp_path):
    source = tmp_path / "source" / "game.pgn"
    source.parent.mkdir()
    source.write_bytes((FIXTURES / "single.pgn").read_bytes())
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    destination = output_dir / source.name
    destination.symlink_to(tmp_path / "missing.pgn")

    result = import_pgn(source, output_dir)

    assert result.pgn_path == destination
    assert destination.is_file()
    assert not destination.is_symlink()
    assert destination.read_bytes() == source.read_bytes()


def test_import_pgn_recovers_crash_after_pgn_install(tmp_path):
    first_source = tmp_path / "first" / "game.pgn"
    second_source = tmp_path / "second" / "game.pgn"
    first_source.parent.mkdir()
    second_source.parent.mkdir()
    first_source.write_bytes((FIXTURES / "single.pgn").read_bytes())
    second_source.write_bytes((FIXTURES / "archive.pgn").read_bytes())
    output_dir = tmp_path / "output"
    original = import_pgn(first_source, output_dir)
    original_manifest = original.manifest_path.read_bytes()
    script = """
import os
import sys
from pathlib import Path
from ai_chess import pgn

source = Path(sys.argv[1])
output_dir = Path(sys.argv[2])
original_replace_and_sync = pgn._replace_and_sync

def crash_after_pgn(source_path, destination):
    original_replace_and_sync(source_path, destination)
    if destination == output_dir / source.name:
        os._exit(70)

pgn._replace_and_sync = crash_after_pgn
pgn.import_pgn(source, output_dir)
"""

    crashed = subprocess.run(
        [sys.executable, "-c", script, str(second_source), str(output_dir)],
        check=False,
    )

    marker = output_dir / ".manifest.v1.json.importing"
    assert crashed.returncode == 70
    assert marker.is_file()
    assert (output_dir / "game.pgn").read_bytes() == second_source.read_bytes()
    assert not (output_dir / "manifest.v1.json").exists()
    marker_json = json.loads(marker.read_text(encoding="utf-8"))
    manifest_backup = output_dir / marker_json["manifest_backup"]
    assert manifest_backup.read_bytes() == original_manifest

    recovered = import_pgn(second_source, output_dir)

    assert not marker.exists()
    assert recovered.pgn_path.read_bytes() == second_source.read_bytes()
    manifest_json = json.loads(recovered.manifest_path.read_text(encoding="utf-8"))
    assert manifest_json == recovered.manifest.to_dict()
    assert recovered.manifest.files[0].sha256 == hashlib.sha256(
        second_source.read_bytes()
    ).hexdigest()
    assert list(output_dir.glob(".*.backup.*")) == []
    assert list(output_dir.glob(".*.stage.*")) == []


def test_import_pgn_recovers_same_path_before_source_validation(tmp_path):
    source = tmp_path / "game.pgn"
    source.write_bytes((FIXTURES / "single.pgn").read_bytes())
    import_pgn(source, tmp_path)
    script = """
import os
import sys
from pathlib import Path
from ai_chess import pgn

source = Path(sys.argv[1])
original_replace_and_sync = pgn._replace_and_sync

def crash_after_source_backup(source_path, destination):
    original_replace_and_sync(source_path, destination)
    if source_path == source and ".backup." in destination.name:
        os._exit(70)

pgn._replace_and_sync = crash_after_source_backup
pgn.import_pgn(source, source.parent)
"""

    crashed = subprocess.run(
        [sys.executable, "-c", script, str(source)],
        check=False,
    )

    marker = tmp_path / ".manifest.v1.json.importing"
    assert crashed.returncode == 70
    assert marker.is_file()
    assert not source.exists()

    recovered = import_pgn(source, tmp_path)

    assert recovered.pgn_path == source
    assert source.read_bytes() == (FIXTURES / "single.pgn").read_bytes()
    assert not marker.exists()
    assert list(tmp_path.glob(".*.backup.*")) == []
    assert list(tmp_path.glob(".*.stage.*")) == []


def test_import_pgn_rejects_marker_path_traversal_without_touching_victim(
    tmp_path,
):
    source = tmp_path / "source" / "game.pgn"
    source.parent.mkdir()
    source.write_bytes((FIXTURES / "single.pgn").read_bytes())
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    victim = tmp_path / "victim.txt"
    victim.write_bytes(b"untouched")
    marker = output_dir / ".manifest.v1.json.importing"
    marker.write_text(
        json.dumps(
            {
                "pgn": "../victim.txt",
                "manifest": "manifest.v1.json",
                "pgn_backup": "../victim.txt",
                "manifest_backup": ".manifest.v1.json.backup.tmp",
                "pgn_existed": True,
                "manifest_existed": False,
                "pgn_stage": "../victim.txt",
                "manifest_stage": ".manifest.v1.json.stage.tmp",
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(AppError) as caught:
        import_pgn(source, output_dir)

    assert caught.value.code is ErrorCode.INVALID_PGN
    assert victim.read_bytes() == b"untouched"
    assert marker.is_file()


def test_import_pgn_recovers_crash_after_source_stage_creation(tmp_path):
    source = tmp_path / "source" / "game.pgn"
    source.parent.mkdir()
    source.write_bytes((FIXTURES / "single.pgn").read_bytes())
    output_dir = tmp_path / "output"
    script = """
import os
import sys
from pathlib import Path
from ai_chess import pgn

source = Path(sys.argv[1])
output_dir = Path(sys.argv[2])
original_stage_source = pgn._stage_source

def crash_after_stage(source_path, staged_path, staged_fd):
    staged = original_stage_source(source_path, staged_path, staged_fd)
    assert staged.is_file()
    os._exit(70)

pgn._stage_source = crash_after_stage
pgn.import_pgn(source, output_dir)
"""

    crashed = subprocess.run(
        [sys.executable, "-c", script, str(source), str(output_dir)],
        check=False,
    )

    marker = output_dir / ".manifest.v1.json.importing"
    assert crashed.returncode == 70
    assert marker.is_file()
    marker_json = json.loads(marker.read_text(encoding="utf-8"))
    staged = output_dir / marker_json["pgn_stage"]
    assert staged.is_file()

    recovered = import_pgn(source, output_dir)

    assert recovered.pgn_path.read_bytes() == source.read_bytes()
    assert not marker.exists()
    assert not staged.exists()
    assert list(output_dir.glob(".*.stage.*")) == []


def test_import_pgn_recovers_crash_after_marker_before_stage_creation(tmp_path):
    source = tmp_path / "source" / "game.pgn"
    source.parent.mkdir()
    source.write_bytes((FIXTURES / "single.pgn").read_bytes())
    output_dir = tmp_path / "output"
    script = """
import os
import sys
from pathlib import Path
from ai_chess import pgn

source = Path(sys.argv[1])
output_dir = Path(sys.argv[2])

def crash_before_stage_files(paths):
    assert paths["marker"].is_file()
    assert not paths["pgn_stage"].exists()
    assert not paths["manifest_stage"].exists()
    os._exit(70)

pgn._create_stage_files = crash_before_stage_files
pgn.import_pgn(source, output_dir)
"""

    crashed = subprocess.run(
        [sys.executable, "-c", script, str(source), str(output_dir)],
        check=False,
    )

    marker = output_dir / ".manifest.v1.json.importing"
    assert crashed.returncode == 70
    assert marker.is_file()
    marker_json = json.loads(marker.read_text(encoding="utf-8"))
    assert not (output_dir / marker_json["pgn_stage"]).exists()
    assert not (output_dir / marker_json["manifest_stage"]).exists()

    recovered = import_pgn(source, output_dir)

    assert recovered.pgn_path.read_bytes() == source.read_bytes()
    assert not marker.exists()
    assert list(output_dir.glob(".*.stage.*")) == []


@pytest.mark.skipif(os.name != "posix", reason="POSIX symlinks required")
def test_import_pgn_does_not_follow_preexisting_stage_symlink(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "source" / "game.pgn"
    source.parent.mkdir()
    source.write_bytes((FIXTURES / "single.pgn").read_bytes())
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    victim = tmp_path / "victim.txt"
    victim.write_bytes(b"untouched")
    transaction_ids = iter(["a" * 32, "b" * 32])
    monkeypatch.setattr(pgn.secrets, "token_hex", lambda size: next(transaction_ids))
    colliding_stage = output_dir / f".game.pgn.stage.{'a' * 32}.tmp"
    colliding_stage.symlink_to(victim)

    result = import_pgn(source, output_dir)

    assert victim.read_bytes() == b"untouched"
    assert colliding_stage.is_symlink()
    assert result.pgn_path.is_file()
    assert not result.pgn_path.is_symlink()
    assert result.pgn_path.read_bytes() == source.read_bytes()


def test_import_pgn_current_transaction_never_uses_stale_backups(
    tmp_path,
    monkeypatch,
):
    first_source = tmp_path / "first" / "game.pgn"
    second_source = tmp_path / "second" / "game.pgn"
    third_source = tmp_path / "third" / "game.pgn"
    first_source.parent.mkdir()
    second_source.parent.mkdir()
    third_source.parent.mkdir()
    first_source.write_bytes((FIXTURES / "single.pgn").read_bytes())
    second_source.write_bytes((FIXTURES / "archive.pgn").read_bytes())
    third_bytes = (
        b'[Event "Third"]\n[Result "*"]\n\n1. d4 d5 2. c4 *\n'
    )
    third_source.write_bytes(third_bytes)
    output_dir = tmp_path / "output"
    import_pgn(first_source, output_dir)
    original_unlink = pgn._unlink

    def fail_backup_unlink(path):
        if ".backup." in path.name:
            raise OSError("backup cleanup failed")
        original_unlink(path)

    monkeypatch.setattr(pgn, "_unlink", fail_backup_unlink)
    import_pgn(second_source, output_dir)
    stale_backups = set(output_dir.glob(".*.backup.*"))
    assert stale_backups

    monkeypatch.setattr(pgn, "_unlink", original_unlink)
    import_pgn(third_source, output_dir)
    assert all(path.exists() for path in stale_backups)
    assert (output_dir / "game.pgn").read_bytes() == third_bytes
    script = """
import os
import sys
from pathlib import Path
from ai_chess import pgn

source = Path(sys.argv[1])
original_replace_and_sync = pgn._replace_and_sync

def crash_before_current_backup(source_path, destination):
    os._exit(70)

pgn._replace_and_sync = crash_before_current_backup
pgn.import_pgn(source, source.parent)
"""

    crashed = subprocess.run(
        [sys.executable, "-c", script, str(output_dir / "game.pgn")],
        check=False,
    )

    assert crashed.returncode == 70
    assert (output_dir / ".manifest.v1.json.importing").is_file()
    recovered = import_pgn(output_dir / "game.pgn", output_dir)

    assert recovered.pgn_path.read_bytes() == third_bytes
    assert recovered.manifest.files[0].sha256 == hashlib.sha256(
        third_bytes
    ).hexdigest()
    assert json.loads(recovered.manifest_path.read_text(encoding="utf-8")) == (
        recovered.manifest.to_dict()
    )
    assert not (output_dir / ".manifest.v1.json.importing").exists()


def test_import_pgn_invalid_replacement_preserves_previous_import(tmp_path):
    valid_source_dir = tmp_path / "valid"
    invalid_source_dir = tmp_path / "invalid"
    output_dir = tmp_path / "output"
    valid_source_dir.mkdir()
    invalid_source_dir.mkdir()
    valid_source = valid_source_dir / "game.pgn"
    invalid_source = invalid_source_dir / "game.pgn"
    valid_source.write_bytes((FIXTURES / "single.pgn").read_bytes())
    invalid_source.write_bytes((FIXTURES / "malformed.pgn").read_bytes())
    imported = import_pgn(valid_source, output_dir)
    previous_pgn = imported.pgn_path.read_bytes()
    previous_manifest = imported.manifest_path.read_bytes()

    with pytest.raises(AppError) as caught:
        import_pgn(invalid_source, output_dir)

    assert caught.value.code is ErrorCode.INVALID_PGN
    assert imported.pgn_path.read_bytes() == previous_pgn
    assert imported.manifest_path.read_bytes() == previous_manifest
    assert list(output_dir.glob("*.tmp")) == []


def test_import_pgn_manifest_install_failure_restores_existing_pair(
    tmp_path,
    monkeypatch,
):
    first_source = tmp_path / "first" / "game.pgn"
    second_source = tmp_path / "second" / "game.pgn"
    first_source.parent.mkdir()
    second_source.parent.mkdir()
    first_source.write_bytes((FIXTURES / "single.pgn").read_bytes())
    second_source.write_bytes((FIXTURES / "archive.pgn").read_bytes())
    output_dir = tmp_path / "output"
    imported = import_pgn(first_source, output_dir)
    previous_pgn = imported.pgn_path.read_bytes()
    previous_manifest = imported.manifest_path.read_bytes()
    original_replace = pgn.os.replace

    def fail_manifest_install(source, destination):
        if (
            Path(destination) == imported.manifest_path
            and Path(source).name.startswith(".manifest.v1.json.stage.")
        ):
            raise OSError("manifest install failed")
        original_replace(source, destination)

    monkeypatch.setattr(pgn.os, "replace", fail_manifest_install)

    with pytest.raises(AppError, match="manifest install failed"):
        import_pgn(second_source, output_dir)

    assert imported.pgn_path.read_bytes() == previous_pgn
    assert imported.manifest_path.read_bytes() == previous_manifest
    assert list(output_dir.glob(".*.stage.*")) == []
    assert list(output_dir.glob(".*.backup.*")) == []


def test_import_pgn_manifest_install_failure_removes_initial_pair(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "source" / "game.pgn"
    source.parent.mkdir()
    source.write_bytes((FIXTURES / "single.pgn").read_bytes())
    output_dir = tmp_path / "output"
    manifest_path = output_dir / "manifest.v1.json"
    original_replace = pgn.os.replace

    def fail_manifest_install(staged, destination):
        if (
            Path(destination) == manifest_path
            and Path(staged).name.startswith(".manifest.v1.json.stage.")
        ):
            raise OSError("manifest install failed")
        original_replace(staged, destination)

    monkeypatch.setattr(pgn.os, "replace", fail_manifest_install)

    with pytest.raises(AppError, match="manifest install failed"):
        import_pgn(source, output_dir)

    assert not (output_dir / source.name).exists()
    assert not manifest_path.exists()
    assert list(output_dir.glob(".*.stage.*")) == []
    assert list(output_dir.glob(".*.backup.*")) == []


@pytest.mark.skipif(os.name != "posix", reason="POSIX directory fsync required")
def test_fsync_directory_propagates_ordinary_open_errors(tmp_path, monkeypatch):
    def fail_open(path, flags):
        raise OSError(5, "I/O error")

    monkeypatch.setattr(pgn.os, "open", fail_open)

    with pytest.raises(OSError, match="I/O error"):
        pgn._fsync_directory(tmp_path)


@pytest.mark.skipif(os.name != "posix", reason="POSIX directory fsync required")
def test_import_pgn_fsync_failure_still_restores_existing_pair(
    tmp_path,
    monkeypatch,
):
    first_source = tmp_path / "first" / "game.pgn"
    second_source = tmp_path / "second" / "game.pgn"
    first_source.parent.mkdir()
    second_source.parent.mkdir()
    first_source.write_bytes((FIXTURES / "single.pgn").read_bytes())
    second_source.write_bytes((FIXTURES / "archive.pgn").read_bytes())
    output_dir = tmp_path / "output"
    imported = import_pgn(first_source, output_dir)
    previous_pgn = imported.pgn_path.read_bytes()
    previous_manifest = imported.manifest_path.read_bytes()
    original_fsync_directory = pgn._fsync_directory
    calls = 0

    def fail_after_backups(path):
        nonlocal calls
        calls += 1
        if calls >= 3:
            raise OSError(5, "directory fsync failed")
        original_fsync_directory(path)

    monkeypatch.setattr(pgn, "_fsync_directory", fail_after_backups)

    with pytest.raises(AppError, match="directory fsync failed"):
        import_pgn(second_source, output_dir)

    assert imported.pgn_path.read_bytes() == previous_pgn
    assert imported.manifest_path.read_bytes() == previous_manifest
    assert list(output_dir.glob(".*.stage.*")) == []
    assert list(output_dir.glob(".*.backup.*")) == []


def test_import_pgn_backup_cleanup_failure_does_not_fail_commit(
    tmp_path,
    monkeypatch,
):
    first_source = tmp_path / "first" / "game.pgn"
    second_source = tmp_path / "second" / "game.pgn"
    first_source.parent.mkdir()
    second_source.parent.mkdir()
    first_source.write_bytes((FIXTURES / "single.pgn").read_bytes())
    second_source.write_bytes((FIXTURES / "archive.pgn").read_bytes())
    output_dir = tmp_path / "output"
    import_pgn(first_source, output_dir)
    original_unlink = pgn._unlink

    def fail_backup_unlink(path):
        if ".backup." in path.name:
            raise OSError("backup cleanup failed")
        original_unlink(path)

    monkeypatch.setattr(pgn, "_unlink", fail_backup_unlink)

    result = import_pgn(second_source, output_dir)

    assert result.pgn_path.read_bytes() == second_source.read_bytes()
    assert json.loads(result.manifest_path.read_text(encoding="utf-8")) == (
        result.manifest.to_dict()
    )
    assert not (output_dir / ".manifest.v1.json.importing").exists()
    stale_backups = list(output_dir.glob(".*.backup.*"))
    assert stale_backups

    monkeypatch.setattr(pgn, "_unlink", original_unlink)
    repeated = import_pgn(second_source, output_dir)

    assert repeated.pgn_path.read_bytes() == second_source.read_bytes()
    assert not (output_dir / ".manifest.v1.json.importing").exists()


@pytest.mark.skipif(os.name != "posix", reason="POSIX directory fsync required")
def test_import_pgn_backup_cleanup_fsync_failure_does_not_fail_commit(
    tmp_path,
    monkeypatch,
):
    first_source = tmp_path / "first" / "game.pgn"
    second_source = tmp_path / "second" / "game.pgn"
    first_source.parent.mkdir()
    second_source.parent.mkdir()
    first_source.write_bytes((FIXTURES / "single.pgn").read_bytes())
    second_source.write_bytes((FIXTURES / "archive.pgn").read_bytes())
    output_dir = tmp_path / "output"
    import_pgn(first_source, output_dir)
    original_fsync_directory = pgn._fsync_directory
    calls = 0

    def fail_cleanup_fsync(path):
        nonlocal calls
        calls += 1
        if calls == 7:
            raise OSError("backup cleanup fsync failed")
        original_fsync_directory(path)

    monkeypatch.setattr(pgn, "_fsync_directory", fail_cleanup_fsync)

    result = import_pgn(second_source, output_dir)

    assert result.pgn_path.read_bytes() == second_source.read_bytes()
    assert json.loads(result.manifest_path.read_text(encoding="utf-8")) == (
        result.manifest.to_dict()
    )
    assert not (output_dir / ".manifest.v1.json.importing").exists()


def test_import_pgn_initial_malformed_import_leaves_no_artifacts(tmp_path):
    source = tmp_path / "source" / "game.pgn"
    source.parent.mkdir()
    source.write_bytes((FIXTURES / "malformed.pgn").read_bytes())
    output_dir = tmp_path / "output"

    with pytest.raises(AppError) as caught:
        import_pgn(source, output_dir)

    assert caught.value.code is ErrorCode.INVALID_PGN
    assert not (output_dir / "game.pgn").exists()
    assert not (output_dir / "manifest.v1.json").exists()
    assert list(output_dir.glob("*.tmp")) == []


def test_import_pgn_closes_manifest_stage_before_error_recovery(tmp_path, monkeypatch):
    source = tmp_path / "source" / "game.pgn"
    source.parent.mkdir()
    source.write_bytes((FIXTURES / "malformed.pgn").read_bytes())
    output_dir = tmp_path / "output"
    original_create_stage_files = pgn._create_stage_files
    original_recover_import = pgn._recover_import
    manifest_fd = -1

    def track_manifest_fd(paths):
        nonlocal manifest_fd
        pgn_fd, manifest_fd = original_create_stage_files(paths)
        return pgn_fd, manifest_fd

    def require_closed_manifest_fd(output_dir, pgn_name, manifest_name):
        if manifest_fd >= 0:
            try:
                os.fstat(manifest_fd)
            except OSError:
                pass
            else:
                raise PermissionError("manifest stage is still open")
        original_recover_import(output_dir, pgn_name, manifest_name)

    monkeypatch.setattr(pgn, "_create_stage_files", track_manifest_fd)
    monkeypatch.setattr(pgn, "_recover_import", require_closed_manifest_fd)

    with pytest.raises(AppError) as caught:
        import_pgn(source, output_dir)

    assert caught.value.code is ErrorCode.INVALID_PGN
    assert list(output_dir.glob("*.tmp")) == []


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits required")
def test_import_pgn_preserves_existing_destination_permissions(tmp_path):
    source = tmp_path / "source" / "game.pgn"
    source.parent.mkdir()
    source.write_bytes((FIXTURES / "single.pgn").read_bytes())
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    destination = output_dir / source.name
    destination.write_bytes(b"old contents")
    destination.chmod(0o640)

    import_pgn(source, output_dir)

    assert stat.S_IMODE(destination.stat().st_mode) == 0o640


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits required")
def test_import_pgn_new_destination_inherits_source_permissions(tmp_path):
    source = tmp_path / "source" / "game.pgn"
    source.parent.mkdir()
    source.write_bytes((FIXTURES / "single.pgn").read_bytes())
    source.chmod(0o604)
    output_dir = tmp_path / "output"

    result = import_pgn(source, output_dir)

    assert stat.S_IMODE(result.pgn_path.stat().st_mode) == 0o604


@pytest.mark.skipif(os.name != "posix", reason="POSIX symlinks required")
def test_import_pgn_source_ref_uses_absolute_lexical_path(tmp_path):
    target = tmp_path / "target.pgn"
    target.write_bytes((FIXTURES / "single.pgn").read_bytes())
    source = tmp_path / "source-link.pgn"
    source.symlink_to(target)

    result = import_pgn(source, tmp_path / "output")

    assert result.manifest.source_ref == str(source.absolute())
    assert result.manifest.source_ref != str(source.resolve())


@pytest.mark.parametrize("name", ["game.txt", "game.PGN"])
def test_import_pgn_rejects_non_pgn_extension(tmp_path, name):
    source = tmp_path / name
    source.write_text("not a pgn", encoding="ascii")

    with pytest.raises(AppError) as caught:
        import_pgn(source, tmp_path / "output")

    assert caught.value.code is ErrorCode.INVALID_PGN
    assert ".pgn" in caught.value.remedy


def test_import_pgn_rejects_non_regular_file(tmp_path):
    source = tmp_path / "directory.pgn"
    source.mkdir()

    with pytest.raises(AppError) as caught:
        import_pgn(source, tmp_path / "output")

    assert caught.value.code is ErrorCode.INVALID_PGN
    assert "regular" in caught.value.remedy.lower()
