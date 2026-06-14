import errno
import hashlib
import json
import os
import re
import secrets
import stat
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Literal, TypeAlias
from urllib.parse import urljoin, urlparse

import httpx

from ai_chess.errors import AppError, ErrorCode
from ai_chess.io import atomic_write_json, sha256_file
from ai_chess.models import SourceFile, SourceManifest
from ai_chess.pgn import read_games

_USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9_-]{3,25}$")
_MONTH_PATTERN = re.compile(r"^(\d{4})-(0[1-9]|1[0-2])$")
# Matches a Chess.com game link, e.g.
#   https://www.chess.com/game/170242602084
#   https://www.chess.com/game/live/170242602084
#   https://www.chess.com/game/daily/170242602084
# capturing the optional kind (live/daily) and the numeric game id.
_GAME_URL_PATTERN = re.compile(
    r"chess\.com/(?:[a-z]+/)?game/(?:(live|daily)/)?(\d+)", re.IGNORECASE
)
_USER_AGENT_PATTERN = re.compile(
    r"^(?P<project>[A-Za-z0-9._-]+)/(?P<version>[A-Za-z0-9._-]+) "
    r"\((?P<contact>[^()]+)\)$"
)
_EMAIL_PATTERN = re.compile(
    r"^[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+"
    r"(?:\.[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+)*@"
    r"(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+"
    r"[A-Za-z]{2,63}$"
)
_GENERIC_USER_AGENT_PROJECTS = {
    "chrome",
    "curl",
    "mozilla",
    "python-httpx",
    "safari",
    "wget",
}
_MAX_RESPONSE_BYTES = 64 * 1024 * 1024
_MAX_ATTEMPTS = 3
_MAX_REDIRECTS = 5
_PGN_CONTENT_TYPES = {
    "application/x-chess-pgn",
    "application/pgn",
    "application/vnd.chess-pgn",
    "text/plain",
}

ResourceKind: TypeAlias = Literal["json", "pgn"]
CacheStatus: TypeAlias = Literal["network", "not_modified", "fresh_cache"]


@dataclass(frozen=True, slots=True)
class FetchResult:
    manifest: SourceManifest
    manifest_path: Path
    pgn_paths: list[Path]
    metadata_paths: list[Path]
    raw_json_paths: list[Path]


@dataclass(frozen=True, slots=True)
class _FetchedBody:
    body: bytes
    etag: str | None
    last_modified: str | None
    cache_control: str | None
    cache_status: CacheStatus


@dataclass(frozen=True, slots=True)
class GameReference:
    """A single Chess.com game resolved from its public link."""

    game_id: str
    kind: Literal["live", "daily"]
    white: str
    black: str
    month: str


def _invalid_input(message: str, remedy: str) -> AppError:
    return AppError(
        code=ErrorCode.INVALID_INPUT,
        message=message,
        remedy=remedy,
    )


def _parse_month(month: str) -> tuple[int, int]:
    match = _MONTH_PATTERN.fullmatch(month)
    if match is None:
        raise _invalid_input(
            f"Invalid month: {month}",
            "Use a month in YYYY-MM format.",
        )
    return int(match.group(1)), int(match.group(2))


def _validate_username(username: str) -> str:
    if _USERNAME_PATTERN.fullmatch(username) is None:
        raise _invalid_input(
            f"Invalid Chess.com username: {username}",
            "Use 3 to 25 letters, digits, underscores, or hyphens.",
        )
    return username.lower()


def _validate_user_agent(user_agent: str) -> str:
    normalized = user_agent.strip()
    match = _USER_AGENT_PATTERN.fullmatch(normalized)
    contact = match.group("contact").strip() if match is not None else ""
    contact_value = re.sub(r"^contact:\s*", "", contact, flags=re.IGNORECASE)
    parsed_contact = urlparse(contact_value)
    has_contact = (
        _EMAIL_PATTERN.fullmatch(contact_value) is not None
        or (
            parsed_contact.scheme in {"http", "https"}
            and parsed_contact.hostname is not None
        )
    )
    if (
        match is None
        or match.group("project").lower() in _GENERIC_USER_AGENT_PROJECTS
        or not has_contact
    ):
        raise _invalid_input(
            "Chess.com requests require an identifiable User-Agent.",
            "Use project/version (contact), with an email address or "
            "http:// or https:// contact URL.",
        )
    return normalized


def iter_months(start: str, end: str) -> list[str]:
    start_year, start_month = _parse_month(start)
    end_year, end_month = _parse_month(end)
    start_index = start_year * 12 + start_month - 1
    end_index = end_year * 12 + end_month - 1
    if start_index > end_index:
        raise _invalid_input(
            f"Month range descends from {start} to {end}.",
            "Use YYYY-MM values in ascending order.",
        )
    return [
        f"{index // 12:04d}-{index % 12 + 1:02d}"
        for index in range(start_index, end_index + 1)
    ]


def parse_game_link(url: str) -> tuple[str, str]:
    """Extract (kind, game_id) from a Chess.com game link.

    ``kind`` is "live" or "daily"; when the link omits it (e.g.
    ``.../game/170242602084``) we assume "live", which is the common case.
    """
    match = _GAME_URL_PATTERN.search(url.strip())
    if match is None:
        raise _invalid_input(
            f"Could not find a Chess.com game id in {url!r}.",
            "Paste a link like https://www.chess.com/game/live/12345 "
            "or https://www.chess.com/game/12345.",
        )
    kind = (match.group(1) or "live").lower()
    return kind, match.group(2)


def live_game_callback_url(kind: str, game_id: str) -> str:
    return f"https://www.chess.com/callback/{kind}/game/{game_id}"


def _game_headers_from_callback(url: str, body: bytes) -> dict[str, str]:
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as error:
        raise _http_error(
            f"Chess.com returned malformed JSON for {url}: {error}",
            "Retry later; if it persists the link may be invalid.",
        ) from error
    game = payload.get("game") if isinstance(payload, dict) else None
    headers = game.get("pgnHeaders") if isinstance(game, dict) else None
    if not isinstance(headers, dict):
        raise _http_error(
            f"Chess.com callback for {url} did not contain game headers.",
            "Confirm the link points to a finished, public game.",
        )
    return {str(key): str(value) for key, value in headers.items()}


def monthly_games_url(username: str, month: str) -> str:
    normalized_username = _validate_username(username)
    year, month_number = _parse_month(month)
    return (
        "https://api.chess.com/pub/player/"
        f"{normalized_username}/games/{year:04d}/{month_number:02d}"
    )


def monthly_pgn_url(username: str, month: str) -> str:
    return f"{monthly_games_url(username, month)}/pgn"


def _http_error(message: str, remedy: str) -> AppError:
    return AppError(
        code=ErrorCode.HTTP_ERROR,
        message=message,
        remedy=remedy,
    )


def _rate_limited(url: str) -> AppError:
    return AppError(
        code=ErrorCode.RATE_LIMITED,
        message=f"Chess.com rate limit persisted for {url}.",
        remedy="Wait before retrying the serial Chess.com request.",
    )


def _atomic_write_bytes(path: Path, body: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)
            temporary_file.write(body)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        os.replace(temporary_path, path)
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _json_bytes(payload: object) -> bytes:
    return (
        json.dumps(
            payload,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def _transaction_member_name(
    destination_name: str,
    transaction_id: str,
    kind: Literal["stage", "backup"],
) -> str:
    return f".{destination_name}.{transaction_id}.{kind}"


def _stage_bytes(
    output_dir: Path,
    destination_name: str,
    transaction_id: str,
    body: bytes,
) -> Path:
    staged_path = output_dir / _transaction_member_name(
        destination_name,
        transaction_id,
        "stage",
    )
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(staged_path, flags, 0o600)
    with os.fdopen(descriptor, "wb") as staged_file:
        staged_file.write(body)
        staged_file.flush()
        os.fsync(staged_file.fileno())
    return staged_path


def _transaction_marker(
    output_dir: Path,
    username: str,
    month: str,
) -> Path:
    return output_dir / f".{username}-{month}.transaction.json"


def _replace_path(source: Path, destination: Path) -> None:
    os.replace(source, destination)


def _expected_transaction_destinations(
    username: str,
    month: str,
) -> set[str]:
    return {
        f"{username}-{month}.json",
        f"{username}-{month}.pgn",
        f"{username}-{month}.provenance.json",
        "manifest.v1.json",
        f"{username}.fetching.json",
    }


def _lstat_regular_or_absent(path: Path) -> bool:
    try:
        path_stat = path.lstat()
    except FileNotFoundError:
        return False
    if not stat.S_ISREG(path_stat.st_mode):
        raise ValueError("Unsafe Chess.com month transaction marker.")
    return True


def _read_transaction_marker(marker_path: Path) -> object:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(marker_path, flags)
    except FileNotFoundError:
        return None
    except OSError as error:
        raise ValueError(
            "Invalid Chess.com month transaction marker.",
        ) from error
    try:
        with os.fdopen(descriptor, encoding="utf-8") as marker_file:
            if not stat.S_ISREG(os.fstat(marker_file.fileno()).st_mode):
                raise ValueError(
                    "Invalid Chess.com month transaction marker.",
                )
            return json.load(marker_file)
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError(
            "Invalid Chess.com month transaction marker.",
        ) from error


def _fsync_directory(path: Path) -> None:
    if os.name != "posix":
        return

    unsupported_errors = {
        errno.EINVAL,
        errno.ENOSYS,
        errno.ENOTSUP,
        errno.EOPNOTSUPP,
    }
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
        )
    except OSError as error:
        if error.errno in unsupported_errors:
            return
        raise
    try:
        try:
            os.fsync(descriptor)
        except OSError as error:
            if error.errno not in unsupported_errors:
                raise
    finally:
        os.close(descriptor)


def _recover_month_transaction(
    output_dir: Path,
    username: str,
    month: str,
) -> None:
    marker_path = _transaction_marker(output_dir, username, month)
    data = _read_transaction_marker(marker_path)
    if data is None:
        return
    if (
        not isinstance(data, dict)
        or set(data) != {"transaction_id", "entries"}
        or not isinstance(data["transaction_id"], str)
        or re.fullmatch(r"[0-9a-f]{32}", data["transaction_id"]) is None
        or not isinstance(data["entries"], list)
    ):
        raise ValueError("Invalid Chess.com month transaction marker.")
    transaction_id = data["transaction_id"]
    entries = data["entries"]
    destinations: set[str] = set()
    for entry in entries:
        if (
            not isinstance(entry, dict)
            or set(entry) != {"destination", "stage", "backup", "existed"}
            or type(entry["destination"]) is not str
            or type(entry["stage"]) is not str
            or type(entry["backup"]) is not str
            or type(entry["existed"]) is not bool
            or Path(entry["destination"]).name != entry["destination"]
            or entry["destination"]
            not in _expected_transaction_destinations(username, month)
            or entry["stage"]
            != _transaction_member_name(
                entry["destination"],
                transaction_id,
                "stage",
            )
            or entry["backup"]
            != _transaction_member_name(
                entry["destination"],
                transaction_id,
                "backup",
            )
        ):
            raise ValueError("Unsafe Chess.com month transaction marker.")
        destinations.add(entry["destination"])
    if (
        len(entries) != len(destinations)
        or destinations
        != _expected_transaction_destinations(username, month)
    ):
        raise ValueError("Incomplete Chess.com month transaction marker.")

    for entry in entries:
        _lstat_regular_or_absent(output_dir / entry["destination"])
        _lstat_regular_or_absent(output_dir / entry["stage"])
        _lstat_regular_or_absent(output_dir / entry["backup"])

    for entry in entries:
        destination = output_dir / entry["destination"]
        backup = output_dir / entry["backup"]
        if entry["existed"] and _lstat_regular_or_absent(backup):
            destination.unlink(missing_ok=True)
            _replace_path(backup, destination)
        elif not entry["existed"]:
            destination.unlink(missing_ok=True)
    for entry in entries:
        (output_dir / entry["stage"]).unlink(missing_ok=True)
        (output_dir / entry["backup"]).unlink(missing_ok=True)
    marker_path.unlink()
    _fsync_directory(output_dir)


def _publish_month(
    output_dir: Path,
    username: str,
    month: str,
    transaction_id: str,
    staged_files: dict[str, Path],
) -> None:
    marker_path = _transaction_marker(output_dir, username, month)
    expected_destinations = _expected_transaction_destinations(username, month)
    if set(staged_files) != expected_destinations:
        raise ValueError("Incomplete Chess.com month transaction.")
    entries = []
    for destination_name, staged_path in staged_files.items():
        expected_stage_name = _transaction_member_name(
            destination_name,
            transaction_id,
            "stage",
        )
        if (
            staged_path.parent != output_dir
            or staged_path.name != expected_stage_name
            or not _lstat_regular_or_absent(staged_path)
        ):
            raise ValueError("Unsafe Chess.com month transaction stage.")
        backup_name = _transaction_member_name(
            destination_name,
            transaction_id,
            "backup",
        )
        entries.append(
            {
                "destination": destination_name,
                "stage": staged_path.name,
                "backup": backup_name,
                "existed": _lstat_regular_or_absent(
                    output_dir / destination_name,
                ),
            }
        )
    atomic_write_json(
        marker_path,
        {"transaction_id": transaction_id, "entries": entries},
    )
    try:
        _fsync_directory(output_dir)
        for entry in entries:
            destination = output_dir / entry["destination"]
            if entry["existed"]:
                _replace_path(destination, output_dir / entry["backup"])
        for entry in entries:
            _replace_path(
                output_dir / entry["stage"],
                output_dir / entry["destination"],
            )
        _fsync_directory(output_dir)
        marker_path.unlink()
        _fsync_directory(output_dir)
    except BaseException:
        _recover_month_transaction(output_dir, username, month)
        raise
    for entry in entries:
        (output_dir / entry["backup"]).unlink(missing_ok=True)
    _fsync_directory(output_dir)


def _content_type(headers: httpx.Headers) -> str:
    return headers.get("content-type", "").partition(";")[0].strip().lower()


def _validate_kind(kind: str) -> ResourceKind:
    if kind not in {"json", "pgn"}:
        raise _invalid_input(
            f"Invalid Chess.com resource kind: {kind}",
            "Use only the json or pgn resource kind.",
        )
    return kind


def _validate_content_type(
    url: str,
    kind: ResourceKind,
    content_type: str,
) -> None:
    if kind == "json":
        compatible = (
            content_type == "application/json"
            or content_type.endswith("+json")
        )
    else:
        compatible = content_type in _PGN_CONTENT_TYPES
    if not compatible:
        raise _http_error(
            f"Chess.com returned incompatible Content-Type "
            f"{content_type or '<missing>'} for {url}.",
            "Retry later or verify the documented Chess.com endpoint.",
        )


def _validate_json_object(url: str, body: bytes) -> None:
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise _http_error(
            f"Chess.com returned malformed JSON for {url}: {error}",
            "Retry later or verify the Chess.com response.",
        ) from error
    if not isinstance(payload, dict):
        raise _http_error(
            f"Chess.com returned non-object JSON for {url}.",
            "Retry later or verify the Chess.com response.",
        )


def _source_file_for_completed_month(
    output_dir: Path,
    username: str,
    month: str,
) -> SourceFile | None:
    json_path = output_dir / f"{username}-{month}.json"
    pgn_path = output_dir / f"{username}-{month}.pgn"
    provenance_path = output_dir / f"{username}-{month}.provenance.json"
    if not all(path.is_file() for path in (json_path, pgn_path, provenance_path)):
        return None
    try:
        _validate_json_object(str(json_path), json_path.read_bytes())
        games = read_games(pgn_path)
        provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
        pgn_provenance = provenance["resources"]["pgn"]
        if (
            not isinstance(pgn_provenance, dict)
            or pgn_provenance.get("url") != monthly_pgn_url(username, month)
        ):
            return None
        return SourceFile(
            path=pgn_path.name,
            sha256=sha256_file(pgn_path),
            game_count=len(games),
            source_url=pgn_provenance["url"],
            etag=pgn_provenance.get("etag"),
            last_modified=pgn_provenance.get("last_modified"),
        )
    except (AppError, OSError, UnicodeError, KeyError, TypeError, ValueError):
        return None


def _load_fetch_state(
    state_path: Path,
    username: str,
    months: list[str],
    output_dir: Path,
) -> tuple[list[str], list[SourceFile]]:
    completed: list[str] = []
    source_files: list[SourceFile] = []
    if state_path.is_file():
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            state = None
        if (
            isinstance(state, dict)
            and state.get("username") == username
            and state.get("requested_months") == months
            and isinstance(state.get("completed_months"), list)
        ):
            for month in state["completed_months"]:
                if month not in months or month != months[len(completed)]:
                    break
                source_file = _source_file_for_completed_month(
                    output_dir,
                    username,
                    month,
                )
                if source_file is None:
                    break
                completed.append(month)
                source_files.append(source_file)
    return completed, source_files


def _read_cache_metadata(
    path: Path,
    body_path: Path,
    url: str,
    kind: ResourceKind,
) -> dict[str, object] | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("metadata is not an object")
        required_types = {
            "url": str,
            "final_url": str,
            "body": str,
            "kind": str,
            "content_type": str,
            "age": int,
            "cached_at": str,
            "byte_size": int,
            "sha256": str,
        }
        if any(
            type(data.get(name)) is not expected
            for name, expected in required_types.items()
        ):
            raise ValueError("metadata fields have invalid types")
        for name in ("etag", "last_modified", "cache_control", "date"):
            if data.get(name) is not None and type(data[name]) is not str:
                raise ValueError("optional metadata fields have invalid types")
        if (
            data["url"] != url
            or data["kind"] != kind
            or data["body"] != body_path.name
            or data["age"] < 0
            or data["byte_size"] < 0
            or re.fullmatch(r"[0-9a-f]{64}", data["sha256"]) is None
        ):
            raise ValueError("metadata binding or integrity fields are invalid")
        datetime.fromisoformat(data["cached_at"])
        final_url = httpx.URL(data["final_url"])
        if (
            final_url.scheme != "https"
            or final_url.host != "api.chess.com"
            or final_url.port not in (None, 443)
        ):
            raise ValueError("metadata final URL is unsafe")
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
        _delete_cache_entry(path, body_path)
        return None
    return data


def _optional_header(data: dict[str, object] | None, name: str) -> str | None:
    if data is None:
        return None
    value = data.get(name)
    return value if isinstance(value, str) and value else None


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _cache_directives(cache_control: str | None) -> dict[str, str | None]:
    if cache_control is None:
        return {}
    directives: dict[str, str | None] = {}
    for directive in cache_control.split(","):
        name, separator, value = directive.strip().partition("=")
        directives[name.lower()] = value.strip().strip('"') if separator else None
    return directives


def _cache_forbids_storage(cache_control: str | None) -> bool:
    return "no-store" in _cache_directives(cache_control)


def _delete_cache_entry(metadata_path: Path, body_path: Path) -> None:
    metadata_path.unlink(missing_ok=True)
    body_path.unlink(missing_ok=True)


def _cache_is_fresh(
    metadata: dict[str, object] | None,
    body_path: Path,
    now: datetime,
) -> bool:
    if metadata is None or not body_path.is_file():
        return False
    cache_control = _optional_header(metadata, "cache_control")
    cached_at = _optional_header(metadata, "cached_at")
    if cache_control is None or cached_at is None:
        return False
    directives = _cache_directives(cache_control)
    if set(directives) & {"no-cache", "no-store", "must-revalidate"}:
        return False
    max_age = directives.get("max-age")
    if max_age is None:
        return False
    try:
        max_age_seconds = int(max_age)
        cached_time = _as_utc(datetime.fromisoformat(cached_at))
    except (TypeError, ValueError):
        return False
    current_time = _as_utc(now)
    if cached_time > current_time:
        return False
    response_date = _optional_header(metadata, "date")
    apparent_age = 0.0
    if response_date is not None:
        try:
            date_time = _as_utc(parsedate_to_datetime(response_date))
        except (TypeError, ValueError, OverflowError):
            return False
        apparent_age = max(0.0, (cached_time - date_time).total_seconds())
    age_value = metadata.get("age")
    if age_value is None:
        age_seconds = 0
    elif type(age_value) is int and age_value >= 0:
        age_seconds = age_value
    else:
        return False
    resident_time = max(0.0, (current_time - cached_time).total_seconds())
    current_age = max(apparent_age, float(age_seconds)) + resident_time
    return max_age_seconds >= 0 and current_age < max_age_seconds


def _read_cached_body(
    url: str,
    kind: ResourceKind,
    body_path: Path,
    metadata_path: Path,
    metadata: dict[str, object] | None,
) -> bytes | None:
    if metadata is None or not body_path.is_file():
        _delete_cache_entry(metadata_path, body_path)
        return None
    try:
        body = body_path.read_bytes()
    except OSError:
        _delete_cache_entry(metadata_path, body_path)
        return None
    if (
        len(body) > _MAX_RESPONSE_BYTES
        or len(body) != metadata["byte_size"]
        or hashlib.sha256(body).hexdigest() != metadata["sha256"]
    ):
        _delete_cache_entry(metadata_path, body_path)
        return None
    content_type = _optional_header(metadata, "content_type")
    try:
        _validate_content_type(url, kind, content_type or "")
        if kind == "json":
            _validate_json_object(url, body)
    except AppError:
        _delete_cache_entry(metadata_path, body_path)
        return None
    return body


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _retry_after_seconds(
    response: httpx.Response,
    attempt: int,
    now: datetime,
) -> float:
    value = response.headers.get("retry-after")
    if value is not None:
        try:
            seconds = max(0.0, float(value))
        except ValueError:
            try:
                retry_at = parsedate_to_datetime(value)
                if retry_at.tzinfo is None:
                    retry_at = retry_at.replace(tzinfo=UTC)
                seconds = max(
                    0.0,
                    (retry_at.astimezone(UTC) - now.astimezone(UTC)).total_seconds(),
                )
            except (TypeError, ValueError, OverflowError):
                pass
            else:
                return seconds
        else:
            return seconds
    return float(2**attempt)


def _validate_redirect(url: str, location: str) -> str:
    target = httpx.URL(urljoin(url, location))
    if (
        target.scheme != "https"
        or target.host != "api.chess.com"
        or target.port not in (None, 443)
    ):
        raise _http_error(
            f"Chess.com redirected outside https://api.chess.com: {target}",
            "Do not follow the redirect; retry the official endpoint later.",
        )
    return str(target)


class ChessComClient:
    def __init__(
        self,
        client: httpx.Client,
        user_agent: str,
        cache_dir: Path,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self.client = client
        self.user_agent = _validate_user_agent(user_agent)
        self.cache_dir = cache_dir
        self.sleep = sleep
        self.clock = clock

    def _fetch(self, url: str, kind: ResourceKind) -> _FetchedBody:
        kind = _validate_kind(kind)
        cache_key = hashlib.sha256(url.encode("utf-8")).hexdigest()
        metadata_path = self.cache_dir / f"{cache_key}.json"
        body_path = self.cache_dir / f"{cache_key}.body"
        metadata = _read_cache_metadata(
            metadata_path,
            body_path,
            url,
            kind,
        )
        headers = {"User-Agent": self.user_agent}
        etag = _optional_header(metadata, "etag")
        last_modified = _optional_header(metadata, "last_modified")
        cache_control = _optional_header(metadata, "cache_control")
        if _cache_is_fresh(metadata, body_path, self.clock()):
            body = _read_cached_body(
                url,
                kind,
                body_path,
                metadata_path,
                metadata,
            )
            if body is not None:
                return _FetchedBody(
                    body,
                    etag,
                    last_modified,
                    cache_control,
                    "fresh_cache",
                )
            metadata = None
            etag = None
            last_modified = None
            cache_control = None
        if etag is not None:
            headers["If-None-Match"] = etag
        if last_modified is not None:
            headers["If-Modified-Since"] = last_modified

        current_url = url
        last_network_error: httpx.RequestError | None = None
        transient_attempts = 0
        redirect_count = 0
        retried_unusable_cache = False
        while transient_attempts < _MAX_ATTEMPTS:
            request = self.client.build_request(
                "GET",
                current_url,
                headers=headers,
            )
            try:
                response = self.client.send(
                    request,
                    stream=True,
                    follow_redirects=False,
                )
            except httpx.RequestError as error:
                last_network_error = error
                transient_attempts += 1
                if transient_attempts < _MAX_ATTEMPTS:
                    self.sleep(float(2 ** (transient_attempts - 1)))
                    continue
                break

            try:
                attempt = transient_attempts
                if response.status_code == 304:
                    transient_attempts += 1
                    body = _read_cached_body(
                        url,
                        kind,
                        body_path,
                        metadata_path,
                        metadata,
                    )
                    if body is None:
                        if retried_unusable_cache:
                            raise _http_error(
                                f"Chess.com returned 304 but the cached body "
                                f"for {url} is unusable.",
                                "Retry later to obtain a complete response.",
                            )
                        retried_unusable_cache = True
                        metadata = None
                        etag = None
                        last_modified = None
                        cache_control = None
                        headers = {"User-Agent": self.user_agent}
                        current_url = url
                        continue
                    response_etag = response.headers.get("etag", etag)
                    response_last_modified = response.headers.get(
                        "last-modified",
                        last_modified,
                    )
                    response_cache_control = response.headers.get(
                        "cache-control",
                        cache_control,
                    )
                    response_date = response.headers.get(
                        "date",
                        _optional_header(metadata, "date"),
                    )
                    response_age_text = response.headers.get("age")
                    response_age = (
                        int(response_age_text)
                        if response_age_text is not None
                        and response_age_text.isdigit()
                        else metadata.get("age", 0) if metadata is not None else 0
                    )
                    if _cache_forbids_storage(response_cache_control):
                        _delete_cache_entry(metadata_path, body_path)
                    else:
                        atomic_write_json(
                            metadata_path,
                            {
                                "url": url,
                                "final_url": (
                                    metadata.get("final_url", url)
                                    if metadata is not None
                                    else url
                                ),
                                "body": body_path.name,
                                "kind": kind,
                                "content_type": _optional_header(
                                    metadata,
                                    "content_type",
                                ),
                                "etag": response_etag,
                                "last_modified": response_last_modified,
                                "cache_control": response_cache_control,
                                "date": response_date,
                                "age": response_age,
                                "cached_at": _as_utc(self.clock()).isoformat(),
                                "byte_size": len(body),
                                "sha256": hashlib.sha256(body).hexdigest(),
                            },
                        )
                    return _FetchedBody(
                        body,
                        response_etag,
                        response_last_modified,
                        response_cache_control,
                        "not_modified",
                    )

                if response.is_redirect:
                    location = response.headers.get("location")
                    if location is None:
                        raise _http_error(
                            f"Chess.com returned a redirect without Location for {url}.",
                            "Retry the official endpoint later.",
                        )
                    redirect_count += 1
                    if redirect_count > _MAX_REDIRECTS:
                        raise _http_error(
                            f"Chess.com exceeded five redirects for {url}.",
                            "Retry the official endpoint later.",
                        )
                    current_url = _validate_redirect(current_url, location)
                    continue

                transient_attempts += 1
                if response.status_code == 429:
                    if transient_attempts >= _MAX_ATTEMPTS:
                        raise _rate_limited(url)
                    self.sleep(
                        _retry_after_seconds(response, attempt, self.clock())
                    )
                    continue

                if 500 <= response.status_code <= 599:
                    if transient_attempts < _MAX_ATTEMPTS:
                        self.sleep(float(2**attempt))
                        continue
                    raise _http_error(
                        f"Chess.com returned HTTP {response.status_code} for {url}.",
                        "Retry later; the request failed after three attempts.",
                    )

                if response.status_code != 200:
                    raise _http_error(
                        f"Chess.com returned HTTP {response.status_code} for {url}.",
                        "Verify the username and month, then retry later.",
                    )

                content_type = _content_type(response.headers)
                _validate_content_type(url, kind, content_type)
                content_length = response.headers.get("content-length")
                if content_length is not None:
                    try:
                        declared_size = int(content_length)
                    except ValueError:
                        declared_size = 0
                    if declared_size > _MAX_RESPONSE_BYTES:
                        raise _http_error(
                            f"Chess.com response exceeds 64 MiB for {url}.",
                            "Request a smaller archive.",
                        )

                body_buffer = bytearray()
                for chunk in response.iter_bytes():
                    body_buffer.extend(chunk)
                    if len(body_buffer) > _MAX_RESPONSE_BYTES:
                        raise _http_error(
                            f"Chess.com response exceeds 64 MiB for {url}.",
                            "Request a smaller archive.",
                        )
                body = bytes(body_buffer)
                if kind == "json":
                    _validate_json_object(url, body)

                response_etag = response.headers.get("etag")
                response_last_modified = response.headers.get("last-modified")
                response_cache_control = response.headers.get("cache-control")
                response_date = response.headers.get("date")
                response_age_text = response.headers.get("age")
                response_age = (
                    int(response_age_text)
                    if response_age_text is not None
                    and response_age_text.isdigit()
                    else 0
                )
                if _cache_forbids_storage(response_cache_control):
                    _delete_cache_entry(metadata_path, body_path)
                else:
                    self.cache_dir.mkdir(parents=True, exist_ok=True)
                    _atomic_write_bytes(body_path, body)
                    atomic_write_json(
                        metadata_path,
                        {
                            "url": url,
                            "final_url": current_url,
                            "body": body_path.name,
                            "kind": kind,
                            "content_type": content_type,
                            "etag": response_etag,
                            "last_modified": response_last_modified,
                            "cache_control": response_cache_control,
                            "date": response_date,
                            "age": response_age,
                            "cached_at": _as_utc(self.clock()).isoformat(),
                            "byte_size": len(body),
                            "sha256": hashlib.sha256(body).hexdigest(),
                        },
                    )
                return _FetchedBody(
                    body,
                    response_etag,
                    response_last_modified,
                    response_cache_control,
                    "network",
                )
            except httpx.RequestError as error:
                last_network_error = error
                if transient_attempts < _MAX_ATTEMPTS:
                    self.sleep(float(2**attempt))
                    continue
                break
            finally:
                response.close()

        detail = f": {last_network_error}" if last_network_error else ""
        raise _http_error(
            f"Chess.com request failed after three attempts for {url}{detail}",
            "Check network access and retry later.",
        )

    def fetch_game_reference(self, url: str) -> GameReference:
        """Resolve a Chess.com game link to its players and month.

        Uses the public game callback endpoint, which returns the PGN headers
        (White, Black, Date) without needing the username up front. Falls back
        between the live/daily callbacks when the link omits the kind.
        """
        kind, game_id = parse_game_link(url)
        kinds = [kind] if kind == "daily" else ["live", "daily"]
        last_error: AppError | None = None
        for candidate in kinds:
            callback_url = live_game_callback_url(candidate, game_id)
            try:
                body = self._fetch(callback_url, "json").body
            except AppError as error:
                last_error = error
                continue
            headers = _game_headers_from_callback(callback_url, body)
            white = headers.get("White")
            black = headers.get("Black")
            date = headers.get("Date")
            if not white or not black or not date:
                raise _http_error(
                    f"Chess.com callback for {callback_url} was missing player "
                    "or date headers.",
                    "Confirm the link points to a finished, public game.",
                )
            return GameReference(
                game_id=game_id,
                kind=candidate,  # type: ignore[arg-type]
                white=white,
                black=black,
                month=date[:7].replace(".", "-"),
            )
        assert last_error is not None
        raise last_error

    def fetch_months(
        self,
        username: str,
        months: list[str],
        output_dir: Path,
    ) -> FetchResult:
        normalized_username = _validate_username(username)
        if not months:
            raise _invalid_input(
                "Chess.com fetch requires at least one month.",
                "Provide one or more months in YYYY-MM format.",
            )
        validated_months = [
            f"{year:04d}-{month_number:02d}"
            for year, month_number in (_parse_month(month) for month in months)
        ]
        output_dir.mkdir(parents=True, exist_ok=True)
        for month in validated_months:
            _recover_month_transaction(
                output_dir,
                normalized_username,
                month,
            )
        state_path = output_dir / f"{normalized_username}.fetching.json"
        completed_months, source_files = _load_fetch_state(
            state_path,
            normalized_username,
            validated_months,
            output_dir,
        )
        atomic_write_json(
            state_path,
            {
                "username": normalized_username,
                "requested_months": validated_months,
                "completed_months": completed_months,
            },
        )
        manifest: SourceManifest | None = None

        for month in validated_months[len(completed_months):]:
            games_url = monthly_games_url(normalized_username, month)
            pgn_url = monthly_pgn_url(normalized_username, month)
            metadata_response = self._fetch(games_url, "json")
            pgn_response = self._fetch(pgn_url, "pgn")

            json_name = f"{normalized_username}-{month}.json"
            pgn_name = f"{normalized_username}-{month}.pgn"
            provenance_name = (
                f"{normalized_username}-{month}.provenance.json"
            )
            transaction_id = secrets.token_hex(16)
            staged_paths: list[Path] = []
            try:
                staged_json = _stage_bytes(
                    output_dir,
                    json_name,
                    transaction_id,
                    metadata_response.body,
                )
                staged_paths.append(staged_json)
                staged_pgn = _stage_bytes(
                    output_dir,
                    pgn_name,
                    transaction_id,
                    pgn_response.body,
                )
                staged_paths.append(staged_pgn)
                _validate_json_object(games_url, staged_json.read_bytes())
                games = read_games(staged_pgn)
                provenance = {
                    "username": normalized_username,
                    "month": month,
                    "retrieved_at": _as_utc(self.clock()).isoformat(),
                    "resources": {
                        "json": {
                            "url": games_url,
                            "output_path": json_name,
                            "etag": metadata_response.etag,
                            "last_modified": metadata_response.last_modified,
                            "cache_control": metadata_response.cache_control,
                            "cache_status": metadata_response.cache_status,
                        },
                        "pgn": {
                            "url": pgn_url,
                            "output_path": pgn_name,
                            "etag": pgn_response.etag,
                            "last_modified": pgn_response.last_modified,
                            "cache_control": pgn_response.cache_control,
                            "cache_status": pgn_response.cache_status,
                        },
                    },
                }
                staged_provenance = _stage_bytes(
                    output_dir,
                    provenance_name,
                    transaction_id,
                    _json_bytes(provenance),
                )
                staged_paths.append(staged_provenance)
                next_source_files = [
                    *source_files,
                    SourceFile(
                        path=pgn_name,
                        sha256=hashlib.sha256(
                            pgn_response.body,
                        ).hexdigest(),
                        game_count=len(games),
                        source_url=pgn_url,
                        etag=pgn_response.etag,
                        last_modified=pgn_response.last_modified,
                    ),
                ]
                manifest = SourceManifest(
                    source_kind="chesscom",
                    source_ref=normalized_username,
                    files=next_source_files,
                )
                staged_manifest = _stage_bytes(
                    output_dir,
                    "manifest.v1.json",
                    transaction_id,
                    _json_bytes(manifest.to_dict()),
                )
                staged_paths.append(staged_manifest)
                next_completed_months = [*completed_months, month]
                staged_state = _stage_bytes(
                    output_dir,
                    state_path.name,
                    transaction_id,
                    _json_bytes(
                        {
                            "username": normalized_username,
                            "requested_months": validated_months,
                            "completed_months": next_completed_months,
                        }
                    ),
                )
                staged_paths.append(staged_state)
                _publish_month(
                    output_dir,
                    normalized_username,
                    month,
                    transaction_id,
                    {
                        json_name: staged_json,
                        pgn_name: staged_pgn,
                        provenance_name: staged_provenance,
                        "manifest.v1.json": staged_manifest,
                        state_path.name: staged_state,
                    },
                )
                staged_paths.clear()
                source_files = next_source_files
                completed_months = next_completed_months
            finally:
                for staged_path in staged_paths:
                    staged_path.unlink(missing_ok=True)

        if manifest is None:
            manifest = SourceManifest(
                source_kind="chesscom",
                source_ref=normalized_username,
                files=source_files,
            )
        state_path.unlink()
        _fsync_directory(output_dir)
        manifest_path = output_dir / "manifest.v1.json"
        pgn_paths = [
            output_dir / f"{normalized_username}-{month}.pgn"
            for month in validated_months
        ]
        metadata_paths = [
            output_dir
            / f"{normalized_username}-{month}.provenance.json"
            for month in validated_months
        ]
        raw_json_paths = [
            output_dir / f"{normalized_username}-{month}.json"
            for month in validated_months
        ]
        return FetchResult(
            manifest=manifest,
            manifest_path=manifest_path,
            pgn_paths=pgn_paths,
            metadata_paths=metadata_paths,
            raw_json_paths=raw_json_paths,
        )
