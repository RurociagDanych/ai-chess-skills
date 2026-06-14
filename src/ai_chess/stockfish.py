import hashlib
import os
import platform
import re
import shutil
import signal
import stat
import subprocess
import tarfile
import tempfile
import threading
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Literal, Sequence
from urllib.parse import quote, urljoin, urlparse

import httpx
from platformdirs import user_data_path

from ai_chess.errors import AppError, ErrorCode
from ai_chess.models import EngineInfo

_LINUX_CPUINFO = Path("/proc/cpuinfo")
_GITHUB_API = "https://api.github.com/repos/official-stockfish/Stockfish"
_MAX_ARCHIVE_BYTES = 256 * 1024 * 1024
_MAX_ARCHIVE_MEMBERS = 256
_MAX_EXECUTABLE_BYTES = 256 * 1024 * 1024
_MAX_REDIRECTS = 5
_MAX_UCI_OUTPUT_BYTES = 1024 * 1024
_VERSION_PATTERN = re.compile(r"(?<!\w)(\d+(?:\.\d+)*(?:[-+._]\w+)?)")
_OPTIMIZATION_TOKENS = {
    "avx2",
    "avx512",
    "avx512icl",
    "bmi2",
    "largeboards",
    "modern",
    "popcnt",
    "sse",
    "sse2",
    "sse3",
    "sse4",
    "sse41",
    "sse42",
    "vnni",
    "vnni256",
    "vnni512",
}

_OPTIMIZATION_PRIORITY = {
    "avx512": 500,
    "avx512icl": 490,
    "vnni512": 480,
    "vnni256": 470,
    "vnni": 460,
    "avx2": 400,
    "bmi2": 300,
    "modern": 200,
    "largeboards": 190,
    "popcnt": 120,
    "sse42": 110,
    "sse41": 100,
    "sse4": 90,
    "sse3": 80,
    "sse2": 70,
    "sse": 60,
}


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
    size: int | None = None


@dataclass(frozen=True, slots=True)
class InstallPlan:
    version: str
    target: PlatformTarget
    asset: ReleaseAsset
    install_dir: Path
    executable_path: Path


def _error(code: ErrorCode, message: str, remedy: str) -> AppError:
    return AppError(code=code, message=message, remedy=remedy)


def _unsupported(message: str) -> AppError:
    return _error(
        ErrorCode.UNSUPPORTED_PLATFORM,
        message,
        "Use an official package or build Stockfish from official source.",
    )


def detect_platform() -> PlatformTarget:
    system = platform.system().lower()
    if system not in {"linux", "windows"}:
        raise _unsupported(f"Stockfish automatic installation does not support {system}.")

    machine = platform.machine().lower()
    if system == "windows":
        machine = (
            os.environ.get("PROCESSOR_ARCHITEW6432")
            or machine
            or os.environ.get("PROCESSOR_ARCHITECTURE")
            or platform.processor()
        ).lower()
    architecture_aliases = {
        "x86_64": "x86_64",
        "amd64": "x86_64",
        "arm64": "arm64",
        "aarch64": "arm64",
    }
    architecture = architecture_aliases.get(machine)
    if architecture is None:
        raise _unsupported(f"Unsupported Stockfish architecture: {machine or 'unknown'}.")

    flags: frozenset[str] = frozenset()
    if system == "linux":
        try:
            values: set[str] = set()
            for line in _LINUX_CPUINFO.read_text(
                encoding="utf-8",
                errors="replace",
            ).splitlines():
                key, separator, value = line.partition(":")
                if separator and key.strip().lower() in {"flags", "features"}:
                    values.update(value.lower().split())
            flags = frozenset(values)
        except OSError:
            flags = frozenset()
    elif re.search(
        r"(?<![a-z0-9])avx2(?![a-z0-9])",
        " ".join(
            (
                os.environ.get("PROCESSOR_IDENTIFIER", ""),
                platform.processor(),
            )
        ).lower(),
    ):
        flags = frozenset({"avx2"})
    return PlatformTarget(system, architecture, flags)


def parse_release(payload: object) -> tuple[str, list[ReleaseAsset]]:
    if not isinstance(payload, dict):
        raise _error(
            ErrorCode.DOWNLOAD_FAILED,
            "Invalid Stockfish release metadata.",
            "Retry using the official Stockfish GitHub release.",
        )
    tag = payload.get("tag_name")
    raw_assets = payload.get("assets")
    if not isinstance(tag, str) or not isinstance(raw_assets, list):
        raise _error(
            ErrorCode.DOWNLOAD_FAILED,
            "Incomplete Stockfish release metadata.",
            "Retry using the official Stockfish GitHub release.",
        )

    version = tag.removeprefix("sf_").removeprefix("SF_")
    assets: list[ReleaseAsset] = []
    for raw_asset in raw_assets:
        if not isinstance(raw_asset, dict):
            continue
        name = raw_asset.get("name")
        url = raw_asset.get("browser_download_url")
        if not isinstance(name, str) or not isinstance(url, str):
            continue
        raw_digest = raw_asset.get("digest")
        digest: str | None = None
        if isinstance(raw_digest, str):
            digest = raw_digest.removeprefix("sha256:").lower()
            if re.fullmatch(r"[0-9a-f]{64}", digest) is None:
                digest = None
        raw_size = raw_asset.get("size")
        size = raw_size if isinstance(raw_size, int) and raw_size >= 0 else None
        assets.append(ReleaseAsset(name, url, digest, size))
    if not assets:
        raise _error(
            ErrorCode.DOWNLOAD_FAILED,
            "Stockfish release contains no downloadable assets.",
            "Check the official Stockfish release metadata.",
        )
    return version, assets


def _asset_architecture(name: str) -> Literal["x86_64", "arm64"] | None:
    lowered = name.lower()
    if "arm64" in lowered or "aarch64" in lowered:
        return "arm64"
    if any(token in lowered for token in ("x64", "x86-64", "x86_64", "amd64")):
        return "x86_64"
    return None


def _normalized_cpu_flags(flags: frozenset[str]) -> set[str]:
    normalized = {
        re.sub(r"[^a-z0-9]", "", flag.lower())
        for flag in flags
    }
    if any(flag.startswith("avx512") for flag in normalized):
        normalized.add("avx512")
    return normalized


def _asset_optimization_tokens(name: str) -> set[str]:
    tokens = set(re.findall(r"[a-z0-9]+", name.lower()))
    return tokens.intersection(_OPTIMIZATION_TOKENS)


def _asset_preference(name: str) -> tuple[int, int]:
    tokens = _asset_optimization_tokens(name)
    if not tokens:
        return (0, 0)
    return (
        max(_OPTIMIZATION_PRIORITY.get(token, 0) for token in tokens),
        len(tokens),
    )


def select_asset(
    assets: Sequence[ReleaseAsset],
    target: PlatformTarget,
) -> ReleaseAsset:
    os_tokens = (
        {"windows", "win32", "win64"}
        if target.os == "windows"
        else {"linux", "ubuntu"}
    )
    compatible = [
        asset
        for asset in assets
        if _asset_architecture(asset.name) == target.arch
        and os_tokens.intersection(
            re.findall(r"[a-z0-9]+", asset.name.lower())
        )
    ]
    if not compatible:
        raise _unsupported(
            f"No official Stockfish asset is available for {target.os}/{target.arch}."
        )

    confirmed_flags = _normalized_cpu_flags(target.cpu_flags)
    supported = [
        asset
        for asset in compatible
        if _asset_optimization_tokens(asset.name).issubset(confirmed_flags)
    ]
    if supported:
        return sorted(
            supported,
            key=lambda asset: (
                -_asset_preference(asset.name)[0],
                -_asset_preference(asset.name)[1],
                asset.name.lower(),
            ),
        )[0]
    raise _unsupported(
        f"No conservative official Stockfish asset is available for "
        f"{target.os}/{target.arch}."
    )


def _is_official_asset_url(url: str, *, redirected: bool = False) -> bool:
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.username is not None:
        return False
    host = (parsed.hostname or "").lower()
    if host in {"stockfishchess.org", "www.stockfishchess.org"}:
        return True
    if host == "github.com":
        path = parsed.path.lower()
        return path.startswith("/official-stockfish/stockfish/releases/")
    return redirected and host == "objects.githubusercontent.com"


def _validate_install_destination(
    user_root: str | Path,
    install_dir: str | Path,
) -> tuple[Path, Path]:
    root = Path(user_root).expanduser().resolve(strict=False)
    destination = Path(install_dir).expanduser().resolve(strict=False)
    try:
        contained = Path(os.path.commonpath((root, destination))) == root
    except ValueError:
        contained = False
    if not contained:
        raise _error(
            ErrorCode.INVALID_INPUT,
            f"Stockfish install directory is outside the user-owned root: "
            f"{destination}.",
            f"Install into {root} or one of its descendants.",
        )
    return root, destination


def _trusted_engine_root() -> Path:
    root = user_data_path("ai-chess") / "engines"
    if root.exists() or root.is_symlink():
        _verify_secure_directory(root)
    return root.resolve(strict=False)


def _is_reparse_point(path_stat: os.stat_result) -> bool:
    attributes = getattr(path_stat, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return bool(attributes & reparse_flag)


def _verify_secure_directory(path: Path) -> os.stat_result:
    try:
        path_stat = path.lstat()
    except OSError as error:
        raise _error(
            ErrorCode.INVALID_INPUT,
            f"Could not inspect Stockfish install directory {path}: {error}.",
            "Use a user-owned directory without links or reparse points.",
        ) from error
    if (
        stat.S_ISLNK(path_stat.st_mode)
        or _is_reparse_point(path_stat)
        or not stat.S_ISDIR(path_stat.st_mode)
    ):
        raise _error(
            ErrorCode.INVALID_INPUT,
            f"Unsafe Stockfish install directory: {path}.",
            "Use a real user-owned directory without links or reparse points.",
        )
    if os.name == "posix":
        if path_stat.st_uid != os.getuid() or path_stat.st_mode & 0o022:
            raise _error(
                ErrorCode.INVALID_INPUT,
                f"Insecure ownership or permissions on {path}.",
                "Use a directory owned by the current user and not writable "
                "by group or others.",
            )
    return path_stat


def _prepare_trusted_root(root: Path) -> Path:
    root = root.expanduser()
    if root.exists() or root.is_symlink():
        _verify_secure_directory(root)
    else:
        try:
            root.mkdir(parents=True, mode=0o700)
        except OSError as error:
            raise _error(
                ErrorCode.INVALID_INPUT,
                f"Could not create trusted Stockfish root {root}: {error}.",
                "Use a user-owned path without links or reparse points.",
            ) from error
        if os.name == "posix":
            root.chmod(0o700)
    _verify_secure_directory(root)
    return root.resolve(strict=True)


def _prepare_install_directory(root: Path, install_dir: Path) -> Path:
    root, destination = _validate_install_destination(root, install_dir)
    relative = destination.relative_to(root)
    current = root
    for part in relative.parts:
        current /= part
        if current.exists() or current.is_symlink():
            _verify_secure_directory(current)
        else:
            try:
                current.mkdir(mode=0o700)
            except FileExistsError:
                _verify_secure_directory(current)
                continue
            except OSError as error:
                raise _error(
                    ErrorCode.INVALID_INPUT,
                    f"Could not create Stockfish install directory "
                    f"{current}: {error}.",
                    "Use a user-owned path without links or reparse points.",
                ) from error
            if os.name == "posix":
                current.chmod(0o700)
            _verify_secure_directory(current)
    return destination


def _before_final_replace(_install_dir: Path) -> None:
    pass


def _anchored_replace(source: Path, install_dir: Path, name: str) -> None:
    if os.name == "posix":
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(install_dir, flags)
        except OSError as error:
            raise _error(
                ErrorCode.INVALID_INPUT,
                f"Could not anchor Stockfish install directory: {error}.",
                "Retry with an unchanged user-owned install directory.",
            ) from error
        try:
            opened_stat = os.fstat(descriptor)
            _before_final_replace(install_dir)
            current_stat = _verify_secure_directory(install_dir)
            if (
                opened_stat.st_dev,
                opened_stat.st_ino,
            ) != (
                current_stat.st_dev,
                current_stat.st_ino,
            ):
                raise _error(
                    ErrorCode.INVALID_INPUT,
                    "Stockfish install directory changed during installation.",
                    "Retry with an unchanged user-owned install directory.",
                )
            os.replace(source, name, dst_dir_fd=descriptor)
            final_stat = os.fstat(descriptor)
            final_path_stat = _verify_secure_directory(install_dir)
            if (
                final_stat.st_dev,
                final_stat.st_ino,
            ) != (
                opened_stat.st_dev,
                opened_stat.st_ino,
            ) or (
                final_path_stat.st_dev,
                final_path_stat.st_ino,
            ) != (
                opened_stat.st_dev,
                opened_stat.st_ino,
            ):
                raise _error(
                    ErrorCode.INVALID_INPUT,
                    "Stockfish install directory changed during installation.",
                    "Inspect the user-owned engine directory before retrying.",
                )
        finally:
            os.close(descriptor)
        return

    _before_final_replace(install_dir)
    # Python's Windows stdlib has no directory-fd replace operation. Revalidate
    # the full path and reject reparse points immediately before replacement.
    _verify_secure_directory(install_dir)
    os.replace(source, install_dir / name)


def build_install_plan(
    version: str,
    target: PlatformTarget,
    install_dir: str | Path | None = None,
    *,
    client: httpx.Client | None = None,
    user_data_root: str | Path | None = None,
) -> InstallPlan:
    root = Path(
        user_data_root
        if user_data_root is not None
        else user_data_path("ai-chess") / "engines"
    )
    destination = Path(install_dir) if install_dir is not None else root / version
    root, destination = _validate_install_destination(root, destination)
    release_path = (
        "releases/latest"
        if version == "latest"
        else f"releases/tags/{quote(version, safe='')}"
    )
    owned_client = client is None
    http_client = client or httpx.Client()
    try:
        request = http_client.build_request(
            "GET",
            f"{_GITHUB_API}/{release_path}",
        )
        response = http_client.send(request, follow_redirects=False)
        response.raise_for_status()
        resolved_version, assets = parse_release(response.json())
    except (httpx.HTTPError, ValueError) as error:
        raise _error(
            ErrorCode.DOWNLOAD_FAILED,
            f"Could not resolve official Stockfish release: {error}.",
            "Check connectivity and retry the official GitHub release.",
        ) from error
    finally:
        if owned_client:
            http_client.close()

    asset = select_asset(assets, target)
    if not _is_official_asset_url(asset.url):
        raise _error(
            ErrorCode.DOWNLOAD_FAILED,
            f"Refusing unofficial Stockfish asset URL: {asset.url}.",
            "Use an asset from the official Stockfish site or GitHub repository.",
        )
    executable_name = "stockfish.exe" if target.os == "windows" else "stockfish"
    return InstallPlan(
        resolved_version,
        target,
        asset,
        destination,
        destination / executable_name,
    )


def _candidate_paths(
    explicit: str | Path | None,
    cache_dir: str | Path,
) -> list[Path]:
    candidates: list[Path] = []
    if explicit is not None:
        candidates.append(Path(explicit))

    cache = Path(cache_dir)
    for name in ("stockfish", "stockfish.exe"):
        candidates.append(cache / name)
    if cache.is_dir():
        candidates.extend(
            path
            for path in sorted(cache.rglob("stockfish*"))
            if path.name.lower() == "stockfish"
            or path.name.lower() == "stockfish.exe"
        )

    path_candidate = shutil.which("stockfish")
    if path_candidate is not None:
        candidates.append(Path(path_candidate))
    candidates.extend(
        Path(path)
        for path in (
            "/usr/bin/stockfish",
            "/usr/local/bin/stockfish",
            "/snap/bin/stockfish",
            r"C:\Program Files\Stockfish\stockfish.exe",
            r"C:\Program Files (x86)\Stockfish\stockfish.exe",
        )
    )

    unique: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = os.path.normcase(os.path.abspath(candidate))
        if key not in seen:
            seen.add(key)
            unique.append(candidate)
    return unique


def discover_engine(
    explicit: str | Path | None,
    cache_dir: str | Path,
) -> EngineInfo | None:
    for candidate in _candidate_paths(explicit, cache_dir):
        if not candidate.is_file():
            continue
        try:
            return probe_engine([str(candidate)])
        except (AppError, OSError):
            continue
    return None


def _unsafe_archive(message: str) -> AppError:
    return _error(
        ErrorCode.UNSAFE_ARCHIVE,
        message,
        "Use an unmodified official Stockfish archive.",
    )


def _safe_member_path(name: str) -> PurePosixPath:
    normalized = name.replace("\\", "/")
    path = PurePosixPath(normalized)
    if (
        not normalized
        or path.is_absolute()
        or any(part in {"", ".", ".."} for part in path.parts)
        or re.match(r"^[A-Za-z]:", normalized)
    ):
        raise _unsafe_archive(f"Unsafe archive member path: {name}.")
    return path


def _is_executable_candidate(path: PurePosixPath) -> bool:
    name = path.name.lower()
    return name.startswith("stockfish") and (
        name.endswith(".exe") or "." not in name
    )


def extract_official_archive(
    archive: str | Path,
    destination: str | Path,
) -> Path:
    archive_path = Path(archive)
    destination_path = Path(destination)
    members: list[tuple[PurePosixPath, object]] = []
    kind: Literal["zip", "tar"]

    try:
        if zipfile.is_zipfile(archive_path):
            kind = "zip"
            with zipfile.ZipFile(archive_path) as zip_archive:
                archive_members = zip_archive.infolist()
                if len(archive_members) > _MAX_ARCHIVE_MEMBERS:
                    raise _unsafe_archive("Stockfish archive has too many members.")
                for info in archive_members:
                    member_path = _safe_member_path(info.filename)
                    mode = info.external_attr >> 16
                    file_type = stat.S_IFMT(mode)
                    if file_type == stat.S_IFLNK:
                        raise _unsafe_archive(
                            f"Archive symlink is not allowed: {info.filename}."
                        )
                    if (
                        not info.is_dir()
                        and file_type not in {0, stat.S_IFREG}
                    ):
                        raise _unsafe_archive(
                            f"Non-regular archive member: {info.filename}."
                        )
                    if not info.is_dir() and info.file_size > _MAX_EXECUTABLE_BYTES:
                        raise _unsafe_archive(
                            f"Archive member is too large: {info.filename}."
                        )
                    members.append((member_path, info))
        elif tarfile.is_tarfile(archive_path):
            kind = "tar"
            with tarfile.open(archive_path) as tar_archive:
                archive_members = tar_archive.getmembers()
                if len(archive_members) > _MAX_ARCHIVE_MEMBERS:
                    raise _unsafe_archive("Stockfish archive has too many members.")
                for info in archive_members:
                    member_path = _safe_member_path(info.name)
                    if not (info.isdir() or info.isreg()):
                        raise _unsafe_archive(
                            f"Non-regular archive member: {info.name}."
                        )
                    if info.isreg() and info.size > _MAX_EXECUTABLE_BYTES:
                        raise _unsafe_archive(
                            f"Archive member is too large: {info.name}."
                        )
                    members.append((member_path, info))
        else:
            raise _unsafe_archive("Stockfish download is not a ZIP or TAR archive.")
    except (OSError, tarfile.TarError, zipfile.BadZipFile) as error:
        if isinstance(error, AppError):
            raise
        raise _unsafe_archive(f"Could not inspect Stockfish archive: {error}.") from error

    candidates = [
        member_path
        for member_path, info in members
        if _is_executable_candidate(member_path)
        and (
            (kind == "zip" and not isinstance(info, zipfile.ZipInfo))
            or (kind == "zip" and isinstance(info, zipfile.ZipInfo) and not info.is_dir())
            or (kind == "tar" and isinstance(info, tarfile.TarInfo) and info.isreg())
        )
    ]
    if len(candidates) != 1:
        raise _unsafe_archive(
            f"Expected one Stockfish executable, found {len(candidates)}."
        )
    expected = candidates[0]
    destination_path.mkdir(parents=True, exist_ok=True, mode=0o700)

    if kind == "zip":
        with zipfile.ZipFile(archive_path) as zip_archive:
            info = next(
                item for item in zip_archive.infolist()
                if _safe_member_path(item.filename) == expected
            )
            output_path = destination_path / Path(*expected.parts)
            output_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            try:
                with zip_archive.open(info) as source, output_path.open("xb") as output:
                    _copy_bounded_member(source, output)
            except AppError:
                output_path.unlink(missing_ok=True)
                raise
    else:
        with tarfile.open(archive_path) as tar_archive:
            info = next(
                item for item in tar_archive.getmembers()
                if _safe_member_path(item.name) == expected
            )
            source = tar_archive.extractfile(info)
            if source is None:
                raise _unsafe_archive("Stockfish executable could not be read.")
            output_path = destination_path / Path(*expected.parts)
            output_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            try:
                with source, output_path.open("xb") as output:
                    _copy_bounded_member(source, output)
            except AppError:
                output_path.unlink(missing_ok=True)
                raise

    if os.name == "posix":
        output_path.chmod(0o700)
    return output_path


def _copy_bounded_member(source: BinaryIO, output: BinaryIO) -> None:
    total = 0
    while True:
        chunk = source.read(1024 * 1024)
        if not chunk:
            return
        total += len(chunk)
        if total > _MAX_EXECUTABLE_BYTES:
            raise _unsafe_archive("Extracted Stockfish executable is too large.")
        output.write(chunk)


def _download_archive(
    asset: ReleaseAsset,
    destination: Path,
    client: httpx.Client,
) -> str:
    if not _is_official_asset_url(asset.url):
        raise _error(
            ErrorCode.DOWNLOAD_FAILED,
            f"Refusing unofficial Stockfish asset URL: {asset.url}.",
            "Use an official Stockfish release asset.",
        )

    url = asset.url
    redirected_from_github = False
    response: httpx.Response | None = None
    try:
        for _ in range(_MAX_REDIRECTS + 1):
            request = client.build_request("GET", url)
            response = client.send(
                request,
                stream=True,
                follow_redirects=False,
            )
            if response.status_code not in {301, 302, 303, 307, 308}:
                break
            location = response.headers.get("location")
            response.close()
            if location is None:
                raise _error(
                    ErrorCode.DOWNLOAD_FAILED,
                    "Stockfish download redirect has no destination.",
                    "Retry the official release asset.",
                )
            previous_host = (urlparse(url).hostname or "").lower()
            target = urljoin(url, location)
            redirected_from_github = (
                redirected_from_github or previous_host == "github.com"
            )
            if not _is_official_asset_url(
                target,
                redirected=redirected_from_github,
            ):
                raise _error(
                    ErrorCode.DOWNLOAD_FAILED,
                    f"Refusing Stockfish redirect to {target}.",
                    "Use the official Stockfish release redirect chain.",
                )
            url = target
        else:
            raise _error(
                ErrorCode.DOWNLOAD_FAILED,
                "Too many Stockfish download redirects.",
                "Retry the official release asset.",
            )
        if response is None:
            raise _error(
                ErrorCode.DOWNLOAD_FAILED,
                "Stockfish download did not return a response.",
                "Retry the official release asset.",
            )
        response.raise_for_status()
        content_length = response.headers.get("content-length")
        if content_length is not None:
            declared_size = int(content_length)
            if declared_size > _MAX_ARCHIVE_BYTES:
                raise _error(
                    ErrorCode.DOWNLOAD_FAILED,
                    "Stockfish archive exceeds the download size limit.",
                    "Verify the official release asset size.",
                )
            if asset.size is not None and declared_size != asset.size:
                raise _error(
                    ErrorCode.DOWNLOAD_FAILED,
                    "Stockfish archive size does not match release metadata.",
                    "Retry the official release asset.",
                )

        digest = hashlib.sha256()
        size = 0
        with destination.open("xb") as output:
            for chunk in response.iter_bytes():
                size += len(chunk)
                if size > _MAX_ARCHIVE_BYTES:
                    raise _error(
                        ErrorCode.DOWNLOAD_FAILED,
                        "Stockfish archive exceeds the download size limit.",
                        "Verify the official release asset size.",
                    )
                if asset.size is not None and size > asset.size:
                    raise _error(
                        ErrorCode.DOWNLOAD_FAILED,
                        "Stockfish archive exceeds its published size.",
                        "Retry the official release asset.",
                    )
                output.write(chunk)
                digest.update(chunk)
        if asset.size is not None and size != asset.size:
            raise _error(
                ErrorCode.DOWNLOAD_FAILED,
                "Stockfish archive size does not match release metadata.",
                "Retry the official release asset.",
            )
        calculated = digest.hexdigest()
        if asset.digest is not None and calculated != asset.digest.lower():
            raise _error(
                ErrorCode.DOWNLOAD_FAILED,
                "Stockfish archive SHA-256 does not match release metadata.",
                "Discard the download and retry the official release asset.",
            )
        return calculated
    except (httpx.HTTPError, OSError, ValueError) as error:
        if isinstance(error, AppError):
            raise
        raise _error(
            ErrorCode.DOWNLOAD_FAILED,
            f"Stockfish download failed: {error}.",
            "Check connectivity and retry the official release asset.",
        ) from error
    finally:
        if response is not None:
            response.close()


def install_engine(
    plan: InstallPlan,
    approved: bool,
    *,
    client: httpx.Client | None = None,
) -> EngineInfo:
    if not approved:
        raise _error(
            ErrorCode.APPROVAL_REQUIRED,
            "Stockfish installation requires explicit approval.",
            "Review the install plan and approve before downloading.",
        )
    if not _is_official_asset_url(plan.asset.url):
        raise _error(
            ErrorCode.DOWNLOAD_FAILED,
            f"Refusing unofficial Stockfish asset URL: {plan.asset.url}.",
            "Use an official Stockfish release asset.",
        )

    root = _prepare_trusted_root(_trusted_engine_root())
    root, install_dir = _validate_install_destination(
        root,
        plan.install_dir,
    )
    executable_path = plan.executable_path.expanduser().resolve(strict=False)
    expected_name = "stockfish.exe" if plan.target.os == "windows" else "stockfish"
    if (
        executable_path.parent != install_dir
        or executable_path.name.lower() != expected_name
    ):
        raise _error(
            ErrorCode.INVALID_INPUT,
            f"Invalid Stockfish executable destination: {executable_path}.",
            f"Install as {install_dir / expected_name}.",
        )

    owned_client = client is None
    http_client = client or httpx.Client()
    try:
        with tempfile.TemporaryDirectory(
            prefix=".stockfish-install-",
            dir=root,
        ) as temporary:
            temporary_dir = Path(temporary)
            archive_path = temporary_dir / "archive"
            archive_digest = _download_archive(
                plan.asset,
                archive_path,
                http_client,
            )
            extracted = extract_official_archive(
                archive_path,
                temporary_dir / "extracted",
            )
            staged_info = probe_engine([str(extracted)])
            binary_digest = _sha256_file(extracted)

            install_dir = _prepare_install_directory(root, install_dir)
            root = _prepare_trusted_root(_trusted_engine_root())
            install_dir = _prepare_install_directory(root, install_dir)
            _anchored_replace(extracted, install_dir, executable_path.name)
            return EngineInfo(
                name=staged_info.name,
                version=staged_info.version,
                path=str(executable_path),
                sha256=binary_digest,
                options={
                    **staged_info.options,
                    "archive_sha256": archive_digest,
                },
            )
    finally:
        if owned_client:
            http_client.close()


def _sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _parse_option(line: str) -> tuple[str, dict[str, object]] | None:
    tokens = line.split()
    if len(tokens) < 4 or tokens[:2] != ["option", "name"] or "type" not in tokens:
        return None
    type_index = tokens.index("type")
    name = " ".join(tokens[2:type_index])
    if not name or type_index + 1 >= len(tokens):
        raise ValueError(f"Malformed UCI option: {line}")
    data: dict[str, object] = {"type": tokens[type_index + 1]}
    field_indexes = [
        index
        for index, token in enumerate(tokens)
        if token in {"default", "min", "max", "var"} and index > type_index
    ]
    for position, index in enumerate(field_indexes):
        end = (
            field_indexes[position + 1]
            if position + 1 < len(field_indexes)
            else len(tokens)
        )
        value = " ".join(tokens[index + 1:end])
        if tokens[index] in {"default", "min", "max"}:
            try:
                data[tokens[index]] = int(value)
            except ValueError:
                data[tokens[index]] = value
        else:
            data.setdefault("var", [])
            assert isinstance(data["var"], list)
            data["var"].append(value)
    return name, data


def _terminate_process_tree(process: subprocess.Popen[bytes]) -> None:
    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        except OSError:
            process.kill()
            return
        time.sleep(0.05)
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except OSError:
            process.kill()
    elif os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(process.pid)],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=1.0,
            )
        except (OSError, subprocess.SubprocessError):
            process.kill()
    elif process.poll() is None:
        process.kill()

    try:
        process.wait(timeout=0.5)
    except subprocess.TimeoutExpired:
        process.kill()
        try:
            process.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            pass


def _bounded_process_output(
    process: subprocess.Popen[bytes],
    timeout: float,
) -> tuple[bytes, bytes]:
    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    total = 0
    lock = threading.Lock()
    overflow = threading.Event()

    def reader(name: Literal["stdout", "stderr"], pipe: object) -> None:
        nonlocal total
        try:
            while True:
                chunk = pipe.read(64 * 1024)  # type: ignore[attr-defined]
                if not chunk:
                    return
                with lock:
                    total += len(chunk)
                    if total > _MAX_UCI_OUTPUT_BYTES:
                        overflow.set()
                        return
                    buffers[name].extend(chunk)
        except OSError:
            return

    threads = [
        threading.Thread(
            target=reader,
            args=("stdout", process.stdout),
            daemon=True,
        ),
        threading.Thread(
            target=reader,
            args=("stderr", process.stderr),
            daemon=True,
        ),
    ]
    for thread in threads:
        thread.start()

    deadline = time.monotonic() + timeout
    while process.poll() is None:
        if overflow.is_set():
            raise _error(
                ErrorCode.ENGINE_FAILED,
                "Stockfish UCI output exceeded the size limit.",
                "Use a valid Stockfish executable.",
            )
        if time.monotonic() >= deadline:
            raise _error(
                ErrorCode.ENGINE_FAILED,
                "Stockfish UCI probe timed out.",
                "Use a responsive Stockfish executable.",
            )
        time.sleep(0.01)

    for thread in threads:
        thread.join(timeout=0.2)
    if overflow.is_set():
        raise _error(
            ErrorCode.ENGINE_FAILED,
            "Stockfish UCI output exceeded the size limit.",
            "Use a valid Stockfish executable.",
        )
    return bytes(buffers["stdout"]), bytes(buffers["stderr"])


def probe_engine(
    command: str | Path | Sequence[str | Path],
    timeout: float = 5.0,
) -> EngineInfo:
    if isinstance(command, (str, Path)):
        arguments = [str(command)]
    else:
        arguments = [str(argument) for argument in command]
    if not arguments:
        raise _error(
            ErrorCode.ENGINE_FAILED,
            "No Stockfish command was provided.",
            "Provide a Stockfish executable path.",
        )

    popen_options: dict[str, object] = {}
    if os.name == "posix":
        popen_options["start_new_session"] = True
    elif os.name == "nt":
        popen_options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    process: subprocess.Popen[bytes] | None = None
    try:
        process = subprocess.Popen(
            arguments,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            **popen_options,
        )
        assert process.stdin is not None
        process.stdin.write(b"uci\nisready\nquit\n")
        process.stdin.close()
        stdout_bytes, stderr_bytes = _bounded_process_output(process, timeout)
        stdout = stdout_bytes.decode("utf-8", errors="replace")
        stderr = stderr_bytes.decode("utf-8", errors="replace")
    except OSError as error:
        raise _error(
            ErrorCode.ENGINE_FAILED,
            f"Could not launch Stockfish: {error}.",
            "Check that the executable exists and can be run.",
        ) from error
    except AppError:
        raise
    except Exception as error:
        raise _error(
            ErrorCode.ENGINE_FAILED,
            f"Stockfish UCI probe failed: {error}.",
            "Use a valid, responsive Stockfish executable.",
        ) from error
    finally:
        if process is not None:
            _terminate_process_tree(process)
            for pipe in (process.stdin, process.stdout, process.stderr):
                if pipe is not None:
                    pipe.close()

    lines = stdout.splitlines()
    name_line = next(
        (line[8:].strip() for line in lines if line.startswith("id name ")),
        "",
    )
    if (
        process.returncode != 0
        or "uciok" not in lines
        or "readyok" not in lines
        or not name_line
    ):
        detail = stderr.strip()
        raise _error(
            ErrorCode.ENGINE_FAILED,
            "Engine did not complete the UCI handshake."
            + (f" {detail}" if detail else ""),
            "Use a valid, responsive Stockfish executable.",
        )
    options: dict[str, object] = {}
    try:
        for line in lines:
            parsed = _parse_option(line)
            if parsed is not None:
                option_name, option = parsed
                options[option_name] = option
    except (IndexError, ValueError) as error:
        raise _error(
            ErrorCode.ENGINE_FAILED,
            f"Engine reported a malformed UCI option: {error}.",
            "Use a valid Stockfish executable.",
        ) from error
    options["command"] = arguments
    version_match = _VERSION_PATTERN.search(name_line)
    version = version_match.group(1) if version_match is not None else "unknown"
    executable = Path(arguments[0])
    try:
        digest = _sha256_file(executable)
    except OSError as error:
        raise _error(
            ErrorCode.ENGINE_FAILED,
            f"Could not hash Stockfish executable: {error}.",
            "Use a readable Stockfish executable.",
        ) from error
    return EngineInfo(
        name=name_line,
        version=version,
        path=str(executable),
        sha256=digest,
        options=options,
    )
