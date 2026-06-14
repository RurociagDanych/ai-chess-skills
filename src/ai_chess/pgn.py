import errno
import hashlib
import json
import os
import re
import secrets
import stat
from dataclasses import dataclass
from pathlib import Path

import chess.pgn

from ai_chess.errors import AppError, ErrorCode
from ai_chess.io import atomic_write_json, sha256_file
from ai_chess.models import SourceFile, SourceManifest


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


class _TrackedGameBuilder(chess.pgn.GameBuilder):
    def begin_game(self) -> None:
        super().begin_game()
        self.header_result: str | None = None
        self.movetext_result: str | None = None

    def visit_header(self, tagname: str, tagvalue: str) -> None:
        super().visit_header(tagname, tagvalue)
        if tagname == "Result":
            self.header_result = tagvalue

    def visit_result(self, result: str) -> None:
        super().visit_result(result)
        self.movetext_result = result

    def result(self) -> tuple[chess.pgn.Game, str | None, str | None]:
        return self.game, self.header_result, self.movetext_result


def _invalid_pgn(message: str, remedy: str) -> AppError:
    return AppError(code=ErrorCode.INVALID_PGN, message=message, remedy=remedy)


def _game_id(game: chess.pgn.Game) -> str:
    identity = {
        "headers": dict(sorted(game.headers.items())),
        "moves": [move.uci() for move in game.mainline_moves()],
    }
    normalized = json.dumps(
        identity,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(normalized).hexdigest()


def read_games(path: Path) -> list[ImportedGame]:
    games: list[ImportedGame] = []
    try:
        with path.open(encoding="utf-8") as pgn_file:
            while parsed := chess.pgn.read_game(
                pgn_file,
                Visitor=_TrackedGameBuilder,
            ):
                game, header_result, movetext_result = parsed
                if game.errors:
                    details = "; ".join(str(error) for error in game.errors)
                    raise _invalid_pgn(
                        f"Could not parse PGN game: {details}",
                        "Correct the illegal or malformed PGN moves and try again.",
                    )
                if game.next() is None or header_result is None:
                    raise _invalid_pgn(
                        "Could not parse a complete PGN game.",
                        "Ensure every game has moves and an explicit Result header.",
                    )
                if movetext_result is None or movetext_result != header_result:
                    raise _invalid_pgn(
                        "PGN header and movetext results are missing or inconsistent.",
                        "End each game with a result matching its Result header.",
                    )
                games.append(ImportedGame(game_id=_game_id(game), game=game))
    except AppError:
        raise
    except (OSError, UnicodeError, ValueError) as error:
        raise _invalid_pgn(
            f"Could not read or parse PGN file {path}: {error}",
            "Check that the file is readable UTF-8 PGN and try again.",
        ) from error

    if not games:
        raise _invalid_pgn(
            f"PGN file {path} contains no games.",
            "Provide a non-empty PGN file containing at least one legal game.",
        )
    return games


def select_latest_index(games: list[ImportedGame]) -> int:
    """Return the 1-based index of the most recently played game.

    Ranking key: dated games rank above undated; among dated games the latest
    (UTCDate, UTCTime) wins, falling back to Date. Ties (including the
    no-dates-anywhere case) resolve to the last game in file order.
    """

    def _clean(value: str) -> str:
        return "" if not value or "?" in value else value

    def _key(item: tuple[int, ImportedGame]) -> tuple[bool, str, str, int]:
        index, imported = item
        headers = imported.game.headers
        primary = _clean(headers.get("UTCDate", "")) or _clean(headers.get("Date", ""))
        utc_time = _clean(headers.get("UTCTime", ""))
        return (bool(primary), primary, utc_time, index)

    best_index, _ = max(enumerate(games), key=_key)
    return best_index + 1


def _fsync_directory(path: Path) -> None:
    if os.name != "posix":
        return

    try:
        directory_fd = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
        )
    except OSError as error:
        if error.errno in {errno.EINVAL, errno.ENOTSUP, errno.EOPNOTSUPP}:
            return
        raise
    try:
        os.fsync(directory_fd)
    except OSError as error:
        if error.errno not in {errno.EINVAL, errno.ENOTSUP, errno.EOPNOTSUPP}:
            raise
    finally:
        os.close(directory_fd)


def _stage_source(source: Path, staged_path: Path, staged_fd: int) -> Path:
    source_mode = stat.S_IMODE(source.stat().st_mode) if os.name == "posix" else None

    try:
        if not stat.S_ISREG(os.fstat(staged_fd).st_mode):
            raise OSError("PGN stage is not a regular file.")
        with source.open("rb") as source_file:
            with os.fdopen(staged_fd, "wb") as temporary_file:
                staged_fd = -1
                while chunk := source_file.read(1024 * 1024):
                    temporary_file.write(chunk)
                temporary_file.flush()
                os.fsync(temporary_file.fileno())
                if source_mode is not None:
                    os.fchmod(temporary_file.fileno(), source_mode)
        return staged_path
    except BaseException:
        if staged_fd >= 0:
            os.close(staged_fd)
        staged_path.unlink(missing_ok=True)
        raise


def _path_exists(path: Path) -> bool:
    return os.path.lexists(path)


def _unlink(path: Path) -> None:
    path.unlink()


def _unlink_if_exists(path: Path) -> bool:
    if not _path_exists(path):
        return False
    _unlink(path)
    return True


def _set_install_mode(staged: Path, destination: Path) -> None:
    if (
        os.name == "posix"
        and _path_exists(destination)
        and not destination.is_symlink()
    ):
        staged.chmod(stat.S_IMODE(destination.lstat().st_mode))


def _replace_and_sync(source: Path, destination: Path) -> None:
    os.replace(source, destination)
    _fsync_directory(destination.parent)


def _fixed_transaction_paths(
    output_dir: Path,
    pgn_name: str,
    manifest_name: str,
) -> dict[str, Path]:
    return {
        "pgn": output_dir / pgn_name,
        "manifest": output_dir / manifest_name,
        "marker": output_dir / f".{manifest_name}.importing",
    }


def _transaction_paths(
    output_dir: Path,
    pgn_name: str,
    manifest_name: str,
    transaction_id: str,
) -> dict[str, Path]:
    paths = _fixed_transaction_paths(output_dir, pgn_name, manifest_name)
    paths.update(
        {
            "pgn_backup": (
                output_dir / f".{pgn_name}.backup.{transaction_id}.tmp"
            ),
            "manifest_backup": (
                output_dir / f".{manifest_name}.backup.{transaction_id}.tmp"
            ),
            "pgn_stage": (
                output_dir / f".{pgn_name}.stage.{transaction_id}.tmp"
            ),
            "manifest_stage": (
                output_dir / f".{manifest_name}.stage.{transaction_id}.tmp"
            ),
        }
    )
    return paths


def _validate_marker(
    data: object,
    output_dir: Path,
    pgn_name: str,
    manifest_name: str,
) -> tuple[dict[str, object], dict[str, Path]]:
    expected_keys = {
        "pgn",
        "manifest",
        "pgn_backup",
        "manifest_backup",
        "pgn_existed",
        "manifest_existed",
        "pgn_stage",
        "manifest_stage",
        "transaction_id",
    }
    if not isinstance(data, dict) or set(data) != expected_keys:
        raise ValueError("Invalid PGN import transaction marker.")

    if data["pgn"] != pgn_name or data["manifest"] != manifest_name:
        raise ValueError("Unsafe PGN import transaction marker path.")
    transaction_id = data["transaction_id"]
    if not isinstance(transaction_id, str) or re.fullmatch(
        r"[0-9a-f]{32}",
        transaction_id,
    ) is None:
        raise ValueError("Invalid PGN import transaction id.")
    paths = _transaction_paths(
        output_dir,
        pgn_name,
        manifest_name,
        transaction_id,
    )
    for name in (
        "pgn_backup",
        "manifest_backup",
        "pgn_stage",
        "manifest_stage",
    ):
        if data[name] != paths[name].name:
            raise ValueError("Unsafe PGN import transaction marker path.")
    if type(data["pgn_existed"]) is not bool:
        raise ValueError("Invalid PGN import transaction marker state.")
    if type(data["manifest_existed"]) is not bool:
        raise ValueError("Invalid PGN import transaction marker state.")
    return data, paths


def _create_regular_file(path: Path) -> int:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    if not stat.S_ISREG(os.fstat(descriptor).st_mode):
        os.close(descriptor)
        path.unlink(missing_ok=True)
        raise OSError("Reserved transaction file is not regular.")
    return descriptor


def _recover_import(
    output_dir: Path,
    pgn_name: str,
    manifest_name: str,
) -> None:
    fixed_paths = _fixed_transaction_paths(output_dir, pgn_name, manifest_name)
    marker_path = fixed_paths["marker"]
    if not marker_path.is_file():
        return

    data, paths = _validate_marker(
        json.loads(marker_path.read_text(encoding="utf-8")),
        output_dir,
        pgn_name,
        manifest_name,
    )
    for name in ("pgn", "manifest"):
        destination = paths[name]
        backup = paths[f"{name}_backup"]
        if data[f"{name}_existed"] and _path_exists(backup):
            _unlink_if_exists(destination)
            os.replace(backup, destination)
        elif not data[f"{name}_existed"]:
            _unlink_if_exists(destination)

    for name in ("pgn_stage", "manifest_stage"):
        _unlink_if_exists(paths[name])

    _unlink(marker_path)
    _fsync_directory(output_dir)


def _begin_import(
    output_dir: Path,
    pgn_name: str,
    manifest_name: str,
) -> dict[str, Path]:
    transaction_id = secrets.token_hex(16)
    paths = _transaction_paths(
        output_dir,
        pgn_name,
        manifest_name,
        transaction_id,
    )
    marker = {
        "pgn": paths["pgn"].name,
        "manifest": paths["manifest"].name,
        "pgn_backup": paths["pgn_backup"].name,
        "manifest_backup": paths["manifest_backup"].name,
        "pgn_existed": _path_exists(paths["pgn"]),
        "manifest_existed": _path_exists(paths["manifest"]),
        "pgn_stage": paths["pgn_stage"].name,
        "manifest_stage": paths["manifest_stage"].name,
        "transaction_id": transaction_id,
    }
    atomic_write_json(paths["marker"], marker)
    _fsync_directory(output_dir)
    return paths


def _create_stage_files(paths: dict[str, Path]) -> tuple[int, int]:
    pgn_fd = -1
    try:
        pgn_fd = _create_regular_file(paths["pgn_stage"])
        manifest_fd = _create_regular_file(paths["manifest_stage"])
        return pgn_fd, manifest_fd
    except BaseException:
        if pgn_fd >= 0:
            os.close(pgn_fd)
            paths["pgn_stage"].unlink(missing_ok=True)
        raise


def _write_manifest_stage(descriptor: int, data: object) -> None:
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as file:
        json.dump(
            data,
            file,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
        )
        file.write("\n")
        file.flush()
        os.fsync(file.fileno())


def _cleanup_backups(backups: tuple[Path | None, ...], directory: Path) -> None:
    deleted = False
    for backup in backups:
        if backup is None:
            continue
        try:
            deleted = _unlink_if_exists(backup) or deleted
        except OSError:
            pass
    if deleted:
        try:
            _fsync_directory(directory)
        except OSError:
            pass


def _install_pair(
    staged_pgn: Path,
    pgn_path: Path,
    staged_manifest: Path,
    manifest_path: Path,
) -> None:
    directory = pgn_path.parent
    marker_path = _fixed_transaction_paths(
        directory,
        pgn_path.name,
        manifest_path.name,
    )["marker"]
    marker, paths = _validate_marker(
        json.loads(marker_path.read_text(encoding="utf-8")),
        directory,
        pgn_path.name,
        manifest_path.name,
    )
    if marker["pgn_existed"]:
        _replace_and_sync(pgn_path, paths["pgn_backup"])
    if marker["manifest_existed"]:
        _replace_and_sync(manifest_path, paths["manifest_backup"])
    _replace_and_sync(staged_pgn, pgn_path)
    _replace_and_sync(staged_manifest, manifest_path)
    _unlink(paths["marker"])
    _fsync_directory(directory)
    _cleanup_backups(
        (paths["pgn_backup"], paths["manifest_backup"]),
        directory,
    )


def import_pgn(source: Path, output_dir: Path) -> ImportResult:
    if source.suffix != ".pgn":
        raise _invalid_pgn(
            f"Source file must use the .pgn extension: {source}",
            "Choose a regular file whose name ends in .pgn.",
        )

    source_ref = source.absolute()
    pgn_path = output_dir / source.name
    manifest_path = output_dir / "manifest.v1.json"
    marker_path = output_dir / ".manifest.v1.json.importing"
    transaction_started = False
    staged_pgn_fd = -1
    staged_manifest_fd = -1
    staged_pgn: Path | None = None
    staged_manifest: Path | None = None
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
        _recover_import(output_dir, source.name, manifest_path.name)
        if not source.is_file():
            raise _invalid_pgn(
                f"Source is not a regular PGN file: {source}",
                "Choose an existing regular .pgn file and try again.",
            )
        collision_error: FileExistsError | None = None
        for _attempt in range(3):
            paths = _begin_import(
                output_dir,
                source.name,
                manifest_path.name,
            )
            transaction_started = True
            try:
                staged_pgn_fd, staged_manifest_fd = _create_stage_files(paths)
                break
            except FileExistsError as error:
                collision_error = error
                _unlink(paths["marker"])
                _fsync_directory(output_dir)
                transaction_started = False
        else:
            raise _invalid_pgn(
                f"Could not create PGN transaction files: {collision_error}",
                "Remove conflicting import files and try again.",
            ) from collision_error
        staged_pgn = paths["pgn_stage"]
        pgn_descriptor = staged_pgn_fd
        staged_pgn_fd = -1
        staged_pgn = _stage_source(source, staged_pgn, pgn_descriptor)
        games = read_games(staged_pgn)
        manifest = SourceManifest(
            source_kind="local",
            source_ref=str(source_ref),
            files=[
                SourceFile(
                    path=source.name,
                    sha256=sha256_file(staged_pgn),
                    game_count=len(games),
                )
            ],
        )
        staged_manifest = paths["manifest_stage"]
        manifest_descriptor = staged_manifest_fd
        staged_manifest_fd = -1
        _write_manifest_stage(manifest_descriptor, manifest.to_dict())
        _set_install_mode(staged_pgn, pgn_path)
        _set_install_mode(staged_manifest, manifest_path)
        _install_pair(staged_pgn, pgn_path, staged_manifest, manifest_path)
        transaction_started = False
        staged_pgn = None
        staged_manifest = None
    except AppError:
        if staged_pgn_fd >= 0:
            os.close(staged_pgn_fd)
            staged_pgn_fd = -1
        if staged_manifest_fd >= 0:
            os.close(staged_manifest_fd)
            staged_manifest_fd = -1
        if transaction_started and marker_path.is_file():
            _recover_import(output_dir, source.name, manifest_path.name)
        raise
    except (OSError, UnicodeError, ValueError) as error:
        import_error: BaseException = error
        if staged_pgn_fd >= 0:
            os.close(staged_pgn_fd)
            staged_pgn_fd = -1
        if staged_manifest_fd >= 0:
            os.close(staged_manifest_fd)
            staged_manifest_fd = -1
        if transaction_started and marker_path.is_file():
            try:
                _recover_import(output_dir, source.name, manifest_path.name)
            except (OSError, UnicodeError, ValueError) as recovery_error:
                import_error = recovery_error
        raise _invalid_pgn(
            f"Could not import PGN file {source}: {import_error}",
            "Check source and output directory permissions, then try again.",
        ) from import_error
    finally:
        if staged_pgn_fd >= 0:
            os.close(staged_pgn_fd)
        if staged_manifest_fd >= 0:
            os.close(staged_manifest_fd)
        if staged_pgn is not None:
            staged_pgn.unlink(missing_ok=True)
        if staged_manifest is not None:
            staged_manifest.unlink(missing_ok=True)

    return ImportResult(
        manifest=manifest,
        manifest_path=manifest_path,
        pgn_path=pgn_path,
        games=games,
    )
