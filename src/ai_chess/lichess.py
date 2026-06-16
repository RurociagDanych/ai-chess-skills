import hashlib
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlencode, urlparse

import httpx

from ai_chess.errors import AppError, ErrorCode
from ai_chess.io import atomic_write_json
from ai_chess.models import SourceFile, SourceManifest
from ai_chess.pgn import read_games

_USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9_-]{2,30}$")
_GAME_ID_PATTERN = re.compile(r"^[A-Za-z0-9]{8}$")
_MAX_RESPONSE_BYTES = 64 * 1024 * 1024
_PGN_CONTENT_TYPES = {
    "application/x-chess-pgn",
    "application/pgn",
    "application/vnd.chess-pgn",
    "text/plain",
}


@dataclass(frozen=True, slots=True)
class LichessFetchResult:
    manifest: SourceManifest
    manifest_path: Path
    pgn_path: Path
    provenance_path: Path


@dataclass(frozen=True, slots=True)
class _FetchedPgn:
    body: bytes
    etag: str | None
    last_modified: str | None
    cache_control: str | None


def _invalid_input(message: str, remedy: str) -> AppError:
    return AppError(
        code=ErrorCode.INVALID_INPUT,
        message=message,
        remedy=remedy,
    )


def _http_error(message: str, remedy: str) -> AppError:
    return AppError(
        code=ErrorCode.HTTP_ERROR,
        message=message,
        remedy=remedy,
    )


def _validate_username(username: str) -> str:
    if _USERNAME_PATTERN.fullmatch(username) is None:
        raise _invalid_input(
            f"Invalid Lichess username: {username}",
            "Use 2 to 30 letters, digits, underscores, or hyphens.",
        )
    return username


def _validate_game_id(game_id: str) -> str:
    if _GAME_ID_PATTERN.fullmatch(game_id) is None:
        raise _invalid_input(
            f"Invalid Lichess game id: {game_id}",
            "Use the 8-character game id from a standard Lichess game link.",
        )
    return game_id


def user_games_url(username: str, *, max_games: int | None = None) -> str:
    normalized_username = _validate_username(username)
    if max_games is not None and max_games <= 0:
        raise _invalid_input(
            "--max must be greater than zero.",
            "Use a positive maximum game count.",
        )
    query = "" if max_games is None else f"?{urlencode({'max': max_games})}"
    return f"https://lichess.org/api/games/user/{normalized_username}{query}"


def game_export_url(game_id: str) -> str:
    return f"https://lichess.org/game/export/{_validate_game_id(game_id)}"


def parse_game_link(url: str) -> str:
    candidate = url.strip()
    if _GAME_ID_PATTERN.fullmatch(candidate):
        return candidate
    parsed = urlparse(candidate)
    if parsed.scheme not in {"http", "https"} or parsed.netloc not in {
        "lichess.org",
        "www.lichess.org",
    }:
        raise _invalid_input(
            f"Could not find a Lichess game id in {url!r}.",
            "Paste a standard game link like https://lichess.org/abcdefgh.",
        )
    path_parts = [part for part in parsed.path.split("/") if part]
    if (
        not path_parts
        or len(path_parts) > 2
        or path_parts[0] in {"analysis", "broadcast", "study"}
        or (len(path_parts) == 2 and path_parts[1] not in {"white", "black"})
    ):
        raise _invalid_input(
            f"Unsupported Lichess game link: {url!r}.",
            "Paste a standard game link like https://lichess.org/abcdefgh.",
        )
    return _validate_game_id(path_parts[0])


def _content_type(headers: httpx.Headers) -> str:
    return headers.get("content-type", "").partition(";")[0].strip().lower()


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


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


class LichessClient:
    def __init__(self, client: httpx.Client, user_agent: str) -> None:
        self.client = client
        self.user_agent = user_agent.strip()

    def _fetch_pgn(self, url: str) -> _FetchedPgn:
        request = self.client.build_request(
            "GET",
            url,
            headers={
                "Accept": "application/x-chess-pgn",
                "User-Agent": self.user_agent,
            },
        )
        try:
            response = self.client.send(
                request,
                stream=True,
                follow_redirects=True,
            )
        except httpx.RequestError as error:
            raise _http_error(
                f"Lichess request failed for {url}: {error}",
                "Check network access and retry later.",
            ) from error

        try:
            if response.status_code == 429:
                raise _http_error(
                    f"Lichess rate limited the request for {url}.",
                    "Wait before retrying the Lichess request.",
                )
            if response.status_code != 200:
                raise _http_error(
                    f"Lichess returned HTTP {response.status_code} for {url}.",
                    "Verify the username or game link, then retry later.",
                )
            content_type = _content_type(response.headers)
            if content_type not in _PGN_CONTENT_TYPES:
                raise _http_error(
                    f"Lichess returned incompatible Content-Type "
                    f"{content_type or '<missing>'} for {url}.",
                    "Retry later or verify the documented Lichess export endpoint.",
                )
            content_length = response.headers.get("content-length")
            if content_length is not None:
                try:
                    declared_size = int(content_length)
                except ValueError:
                    declared_size = 0
                if declared_size > _MAX_RESPONSE_BYTES:
                    raise _http_error(
                        f"Lichess response exceeds 64 MiB for {url}.",
                        "Use --max or a narrower export.",
                    )

            body_buffer = bytearray()
            for chunk in response.iter_bytes():
                body_buffer.extend(chunk)
                if len(body_buffer) > _MAX_RESPONSE_BYTES:
                    raise _http_error(
                        f"Lichess response exceeds 64 MiB for {url}.",
                        "Use --max or a narrower export.",
                    )
            return _FetchedPgn(
                bytes(body_buffer),
                response.headers.get("etag"),
                response.headers.get("last-modified"),
                response.headers.get("cache-control"),
            )
        finally:
            response.close()

    def _write_archive(
        self,
        output_dir: Path,
        source_ref: str,
        pgn_name: str,
        provenance_name: str,
        source_url: str,
        kind: str,
        fetched: _FetchedPgn,
    ) -> LichessFetchResult:
        output_dir.mkdir(parents=True, exist_ok=True)
        pgn_path = output_dir / pgn_name
        provenance_path = output_dir / provenance_name
        manifest_path = output_dir / "manifest.v1.json"
        _atomic_write_bytes(pgn_path, fetched.body)
        games = read_games(pgn_path)
        provenance = {
            "source": "lichess",
            "kind": kind,
            "source_ref": source_ref,
            "retrieved_at": _utc_now(),
            "resources": {
                "pgn": {
                    "url": source_url,
                    "output_path": pgn_name,
                    "etag": fetched.etag,
                    "last_modified": fetched.last_modified,
                    "cache_control": fetched.cache_control,
                }
            },
        }
        atomic_write_json(provenance_path, provenance)
        manifest = SourceManifest(
            source_kind="lichess",
            source_ref=source_ref,
            files=[
                SourceFile(
                    path=pgn_name,
                    sha256=hashlib.sha256(fetched.body).hexdigest(),
                    game_count=len(games),
                    source_url=source_url,
                    etag=fetched.etag,
                    last_modified=fetched.last_modified,
                )
            ],
        )
        atomic_write_json(manifest_path, manifest.to_dict())
        return LichessFetchResult(
            manifest=manifest,
            manifest_path=manifest_path,
            pgn_path=pgn_path,
            provenance_path=provenance_path,
        )

    def fetch_user_games(
        self,
        username: str,
        output_dir: Path,
        *,
        max_games: int | None = None,
    ) -> LichessFetchResult:
        source_url = user_games_url(username, max_games=max_games)
        fetched = self._fetch_pgn(source_url)
        normalized_username = _validate_username(username)
        return self._write_archive(
            output_dir,
            normalized_username,
            f"{normalized_username}.lichess.pgn",
            f"{normalized_username}.lichess.provenance.json",
            source_url,
            "user_games",
            fetched,
        )

    def fetch_game(
        self,
        game_id: str,
        output_dir: Path,
    ) -> LichessFetchResult:
        normalized_game_id = _validate_game_id(game_id)
        source_url = game_export_url(normalized_game_id)
        fetched = self._fetch_pgn(source_url)
        return self._write_archive(
            output_dir,
            normalized_game_id,
            f"{normalized_game_id}.lichess.pgn",
            f"{normalized_game_id}.lichess.provenance.json",
            source_url,
            "game",
            fetched,
        )
