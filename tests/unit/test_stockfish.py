import hashlib
import inspect
import io
import json
import os
import stat
import sys
import tarfile
import zipfile
from pathlib import Path

import httpx
import pytest

import ai_chess.stockfish as stockfish_module
from ai_chess.errors import AppError, ErrorCode
from ai_chess.stockfish import (
    InstallPlan,
    PlatformTarget,
    ReleaseAsset,
    build_install_plan,
    detect_platform,
    discover_engine,
    extract_official_archive,
    install_engine,
    parse_release,
    select_asset,
)


FIXTURES = Path(__file__).parents[1] / "fixtures"
FAKE_UCI = FIXTURES / "fake_uci.py"


def test_install_engine_has_no_trusted_root_parameter() -> None:
    assert "trusted_user_root" not in inspect.signature(install_engine).parameters


def test_windows_process_tree_cleanup_uses_taskkill(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    class FakeProcess:
        pid = 1234

        def poll(self):
            return None

        def wait(self, timeout: float):
            return 0

        def kill(self):
            pytest.fail("taskkill fallback was unexpectedly used")

    monkeypatch.setattr(stockfish_module.os, "name", "nt")
    monkeypatch.setattr(
        stockfish_module.subprocess,
        "run",
        lambda command, **kwargs: calls.append(command),
    )

    stockfish_module._terminate_process_tree(FakeProcess())

    assert calls == [["taskkill", "/T", "/F", "/PID", "1234"]]


def test_parse_official_release_fixture() -> None:
    payload = json.loads(
        (FIXTURES / "stockfish_release.json").read_text(encoding="utf-8")
    )

    version, assets = parse_release(payload)

    assert version == "17.1"
    assert assets[0] == ReleaseAsset(
        name="stockfish-windows-x64-avx2.zip",
        url=(
            "https://github.com/official-stockfish/Stockfish/releases/download/"
            "sf_17.1/stockfish-windows-x64-avx2.zip"
        ),
        digest="a" * 64,
        size=1001,
    )


@pytest.mark.parametrize("machine", ["AMD64", "x86_64"])
def test_detect_platform_normalizes_windows_x64(
    monkeypatch: pytest.MonkeyPatch,
    machine: str,
) -> None:
    monkeypatch.setattr(stockfish_module.platform, "system", lambda: "Windows")
    monkeypatch.setattr(stockfish_module.platform, "machine", lambda: machine)
    monkeypatch.setenv("PROCESSOR_IDENTIFIER", "Intel64 Family 6")

    assert detect_platform() == PlatformTarget("windows", "x86_64", frozenset())


def test_detect_platform_normalizes_windows_arm64(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(stockfish_module.platform, "system", lambda: "Windows")
    monkeypatch.setattr(stockfish_module.platform, "machine", lambda: "ARM64")

    assert detect_platform() == PlatformTarget("windows", "arm64", frozenset())


def test_detect_platform_confirms_windows_avx2_from_identifier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(stockfish_module.platform, "system", lambda: "Windows")
    monkeypatch.setattr(stockfish_module.platform, "machine", lambda: "AMD64")
    monkeypatch.setattr(stockfish_module.platform, "processor", lambda: "")
    monkeypatch.setenv("PROCESSOR_ARCHITECTURE", "AMD64")
    monkeypatch.setenv(
        "PROCESSOR_IDENTIFIER",
        "Intel64 Family 6 Model 151 Stepping 2, GenuineIntel AVX2",
    )

    assert detect_platform() == PlatformTarget(
        "windows",
        "x86_64",
        frozenset({"avx2"}),
    )


def test_detect_platform_keeps_windows_x64_conservative_without_avx2(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(stockfish_module.platform, "system", lambda: "Windows")
    monkeypatch.setattr(stockfish_module.platform, "machine", lambda: "")
    monkeypatch.setattr(
        stockfish_module.platform,
        "processor",
        lambda: "Intel64 Family 6",
    )
    monkeypatch.setenv("PROCESSOR_ARCHITECTURE", "AMD64")
    monkeypatch.delenv("PROCESSOR_ARCHITEW6432", raising=False)
    monkeypatch.setenv("PROCESSOR_IDENTIFIER", "Intel64 Family 6")

    assert detect_platform() == PlatformTarget(
        "windows",
        "x86_64",
        frozenset(),
    )


def test_detect_platform_reads_linux_avx2(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cpuinfo = tmp_path / "cpuinfo"
    cpuinfo.write_text("flags : sse4_2 avx avx2 bmi2\n", encoding="utf-8")
    monkeypatch.setattr(stockfish_module.platform, "system", lambda: "Linux")
    monkeypatch.setattr(stockfish_module.platform, "machine", lambda: "amd64")
    monkeypatch.setattr(stockfish_module, "_LINUX_CPUINFO", cpuinfo)

    assert detect_platform() == PlatformTarget(
        "linux",
        "x86_64",
        frozenset({"sse4_2", "avx", "avx2", "bmi2"}),
    )


@pytest.mark.parametrize(
    ("system", "machine"),
    [("Darwin", "arm64"), ("Linux", "riscv64")],
)
def test_detect_platform_rejects_unsupported_targets(
    monkeypatch: pytest.MonkeyPatch,
    system: str,
    machine: str,
) -> None:
    monkeypatch.setattr(stockfish_module.platform, "system", lambda: system)
    monkeypatch.setattr(stockfish_module.platform, "machine", lambda: machine)

    with pytest.raises(AppError) as caught:
        detect_platform()

    assert caught.value.code == ErrorCode.UNSUPPORTED_PLATFORM


def test_selects_windows_arm64_asset() -> None:
    assets = [
        ReleaseAsset("stockfish-windows-x64-avx2.zip", "https://example/x64", None),
        ReleaseAsset("stockfish-windows-arm64.zip", "https://example/arm64", None),
    ]

    selected = select_asset(
        assets,
        PlatformTarget("windows", "arm64", frozenset()),
    )

    assert selected.name == "stockfish-windows-arm64.zip"


def test_selects_windows_avx2_only_when_confirmed() -> None:
    assets = [
        ReleaseAsset("stockfish-windows-x64-avx2.zip", "https://example/avx2", None),
        ReleaseAsset("stockfish-win64-x64.zip", "https://example/base", None),
    ]

    assert select_asset(
        assets,
        PlatformTarget("windows", "x86_64", frozenset()),
    ).name == "stockfish-win64-x64.zip"
    assert select_asset(
        assets,
        PlatformTarget("windows", "x86_64", frozenset({"avx2"})),
    ).name == "stockfish-windows-x64-avx2.zip"


def test_windows_asset_selection_does_not_match_darwin() -> None:
    assets = [
        ReleaseAsset("stockfish-darwin-x64.tar", "https://example/darwin", None),
    ]

    with pytest.raises(AppError) as caught:
        select_asset(
            assets,
            PlatformTarget("windows", "x86_64", frozenset()),
        )

    assert caught.value.code == ErrorCode.UNSUPPORTED_PLATFORM


def test_selects_linux_avx2_only_when_confirmed() -> None:
    assets = [
        ReleaseAsset("stockfish-ubuntu-x64-avx2.tar", "https://example/avx2", None),
        ReleaseAsset("stockfish-ubuntu-x64.tar", "https://example/base", None),
    ]

    assert select_asset(
        assets,
        PlatformTarget("linux", "x86_64", frozenset()),
    ).name == "stockfish-ubuntu-x64.tar"
    assert select_asset(
        assets,
        PlatformTarget("linux", "x86_64", frozenset({"avx2"})),
    ).name == "stockfish-ubuntu-x64-avx2.tar"


def test_prefers_avx2_over_sse41_popcnt_when_both_are_supported() -> None:
    assets = [
        ReleaseAsset(
            "stockfish-ubuntu-x64-sse41-popcnt.tar",
            "https://example/sse41",
            None,
        ),
        ReleaseAsset(
            "stockfish-ubuntu-x64-avx2.tar",
            "https://example/avx2",
            None,
        ),
    ]

    selected = select_asset(
        assets,
        PlatformTarget(
            "linux",
            "x86_64",
            frozenset({"sse41", "popcnt", "avx2"}),
        ),
    )

    assert selected.name == "stockfish-ubuntu-x64-avx2.tar"


def test_selects_base_instead_of_unconfirmed_sse41_popcnt() -> None:
    assets = [
        ReleaseAsset(
            "stockfish-ubuntu-x64-sse41-popcnt.tar",
            "https://example/sse41",
            None,
        ),
        ReleaseAsset("stockfish-ubuntu-x64.tar", "https://example/base", None),
    ]

    selected = select_asset(
        assets,
        PlatformTarget("linux", "x86_64", frozenset()),
    )

    assert selected.name == "stockfish-ubuntu-x64.tar"


def test_rejects_only_unconfirmed_optimized_assets() -> None:
    assets = [
        ReleaseAsset(
            "stockfish-ubuntu-x64-sse41-popcnt.tar",
            "https://example/sse41",
            None,
        ),
        ReleaseAsset(
            "stockfish-ubuntu-x64-modern-largeboards.tar",
            "https://example/modern",
            None,
        ),
    ]

    with pytest.raises(AppError) as caught:
        select_asset(
            assets,
            PlatformTarget("linux", "x86_64", frozenset()),
        )

    assert caught.value.code == ErrorCode.UNSUPPORTED_PLATFORM


def test_select_asset_rejects_missing_official_linux_arm64() -> None:
    assets = [
        ReleaseAsset("stockfish-ubuntu-x64.tar", "https://example/base", None),
    ]

    with pytest.raises(AppError) as caught:
        select_asset(
            assets,
            PlatformTarget("linux", "arm64", frozenset()),
        )

    assert caught.value.code == ErrorCode.UNSUPPORTED_PLATFORM


def _write_zip(path: Path, members: dict[str, bytes]) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        for name, body in members.items():
            archive.writestr(name, body)


def _write_tar(
    path: Path,
    members: list[tuple[tarfile.TarInfo, bytes]],
) -> None:
    with tarfile.open(path, "w") as archive:
        for info, body in members:
            info.size = len(body)
            archive.addfile(info, io.BytesIO(body))


@pytest.mark.parametrize("member", ["../../escape", "/absolute/stockfish"])
def test_extract_rejects_unsafe_zip_paths(tmp_path: Path, member: str) -> None:
    archive = tmp_path / "stockfish.zip"
    _write_zip(archive, {member: b"bad"})

    with pytest.raises(AppError) as caught:
        extract_official_archive(archive, tmp_path / "out")

    assert caught.value.code == ErrorCode.UNSAFE_ARCHIVE


def test_extract_rejects_zip_symlink(tmp_path: Path) -> None:
    archive = tmp_path / "stockfish.zip"
    with zipfile.ZipFile(archive, "w") as output:
        info = zipfile.ZipInfo("stockfish")
        info.create_system = 3
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        output.writestr(info, "target")

    with pytest.raises(AppError) as caught:
        extract_official_archive(archive, tmp_path / "out")

    assert caught.value.code == ErrorCode.UNSAFE_ARCHIVE


@pytest.mark.parametrize(
    "member_type",
    [tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.CHRTYPE],
)
def test_extract_rejects_unsafe_tar_member_types(
    tmp_path: Path,
    member_type: bytes,
) -> None:
    archive = tmp_path / "stockfish.tar"
    member = tarfile.TarInfo("stockfish")
    member.type = member_type
    member.linkname = "target"
    _write_tar(archive, [(member, b"")])

    with pytest.raises(AppError) as caught:
        extract_official_archive(archive, tmp_path / "out")

    assert caught.value.code == ErrorCode.UNSAFE_ARCHIVE


def test_extract_rejects_multiple_candidate_executables(tmp_path: Path) -> None:
    archive = tmp_path / "stockfish.zip"
    _write_zip(
        archive,
        {
            "one/stockfish": b"first",
            "two/stockfish-linux-x64": b"second",
        },
    )

    with pytest.raises(AppError) as caught:
        extract_official_archive(archive, tmp_path / "out")

    assert caught.value.code == ErrorCode.UNSAFE_ARCHIVE


@pytest.mark.parametrize("kind", ["zip", "tar"])
def test_extract_rejects_too_many_archive_members(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    kind: str,
) -> None:
    monkeypatch.setattr(stockfish_module, "_MAX_ARCHIVE_MEMBERS", 2)
    archive = tmp_path / f"stockfish.{kind}"
    if kind == "zip":
        _write_zip(
            archive,
            {
                "one.txt": b"1",
                "two.txt": b"2",
                "stockfish": b"engine",
            },
        )
    else:
        _write_tar(
            archive,
            [
                (tarfile.TarInfo("one.txt"), b"1"),
                (tarfile.TarInfo("two.txt"), b"2"),
                (tarfile.TarInfo("stockfish"), b"engine"),
            ],
        )

    with pytest.raises(AppError) as caught:
        extract_official_archive(archive, tmp_path / "out")

    assert caught.value.code == ErrorCode.UNSAFE_ARCHIVE
    assert not (tmp_path / "out").exists()


@pytest.mark.parametrize("kind", ["zip", "tar"])
def test_extract_rejects_oversized_executable_before_writing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    kind: str,
) -> None:
    monkeypatch.setattr(stockfish_module, "_MAX_EXECUTABLE_BYTES", 4)
    archive = tmp_path / f"stockfish.{kind}"
    if kind == "zip":
        _write_zip(archive, {"stockfish": b"12345"})
    else:
        _write_tar(
            archive,
            [(tarfile.TarInfo("stockfish"), b"12345")],
        )

    with pytest.raises(AppError) as caught:
        extract_official_archive(archive, tmp_path / "out")

    assert caught.value.code == ErrorCode.UNSAFE_ARCHIVE
    assert not (tmp_path / "out").exists()


def test_extracts_extended_windows_stockfish_executable_name(
    tmp_path: Path,
) -> None:
    archive = tmp_path / "stockfish.zip"
    _write_zip(
        archive,
        {
            "stockfish/README.md": b"documentation",
            "stockfish/helper.exe": b"not the engine",
            "stockfish/Stockfish-Windows-x86-64-avx2.exe": b"engine",
        },
    )

    extracted = extract_official_archive(archive, tmp_path / "out")

    assert extracted.name == "Stockfish-Windows-x86-64-avx2.exe"
    assert extracted.read_bytes() == b"engine"


def test_extract_rejects_unrelated_or_multiple_windows_executables(
    tmp_path: Path,
) -> None:
    unrelated = tmp_path / "unrelated.zip"
    _write_zip(unrelated, {"tools/helper.exe": b"helper"})

    with pytest.raises(AppError) as caught:
        extract_official_archive(unrelated, tmp_path / "unrelated-out")

    assert caught.value.code == ErrorCode.UNSAFE_ARCHIVE

    multiple = tmp_path / "multiple.zip"
    _write_zip(
        multiple,
        {
            "one/stockfish.exe": b"first",
            "two/stockfish-windows-x64.exe": b"second",
        },
    )

    with pytest.raises(AppError) as caught:
        extract_official_archive(multiple, tmp_path / "multiple-out")

    assert caught.value.code == ErrorCode.UNSAFE_ARCHIVE


@pytest.mark.parametrize("member", ["../../escape", "/absolute/stockfish"])
def test_extract_rejects_unsafe_tar_paths_without_touching_victim(
    tmp_path: Path,
    member: str,
) -> None:
    victim = tmp_path / "victim"
    victim.write_bytes(b"untouched")
    archive = tmp_path / "stockfish.tar"
    info = tarfile.TarInfo(member)
    _write_tar(archive, [(info, b"bad")])

    with pytest.raises(AppError) as caught:
        extract_official_archive(archive, tmp_path / "out")

    assert caught.value.code == ErrorCode.UNSAFE_ARCHIVE
    assert victim.read_bytes() == b"untouched"


@pytest.mark.parametrize("kind", ["zip", "tar"])
def test_extracts_one_regular_stockfish_executable(
    tmp_path: Path,
    kind: str,
) -> None:
    archive = tmp_path / f"stockfish.{kind}"
    if kind == "zip":
        _write_zip(archive, {"stockfish/stockfish": b"engine"})
    else:
        member = tarfile.TarInfo("stockfish/stockfish")
        member.mode = 0o755
        _write_tar(archive, [(member, b"engine")])

    extracted = extract_official_archive(archive, tmp_path / "out")

    assert extracted.read_bytes() == b"engine"
    assert extracted.name == "stockfish"
    if os.name == "posix":
        assert extracted.stat().st_mode & stat.S_IXUSR


def test_build_plan_resolves_metadata_without_downloading_asset(
    tmp_path: Path,
) -> None:
    payload = (FIXTURES / "stockfish_release.json").read_bytes()
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, content=payload)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        plan = build_install_plan(
            "latest",
            PlatformTarget("windows", "arm64", frozenset()),
            tmp_path / "engines" / "17.1",
            client=client,
            user_data_root=tmp_path / "engines",
        )

    assert plan.version == "17.1"
    assert plan.install_dir == tmp_path / "engines" / "17.1"
    assert plan.executable_path == plan.install_dir / "stockfish.exe"
    assert len(requests) == 1
    assert requests[0].url.path.endswith("/releases/latest")


def test_build_plan_rejects_unofficial_asset_url(tmp_path: Path) -> None:
    payload = {
        "tag_name": "sf_17.1",
        "assets": [
            {
                "name": "stockfish-windows-arm64.zip",
                "browser_download_url": "https://evil.example/stockfish.zip",
            }
        ],
    }

    with httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json=payload)
        )
    ) as client:
        with pytest.raises(AppError) as caught:
            build_install_plan(
                "latest",
                PlatformTarget("windows", "arm64", frozenset()),
                tmp_path / "engines",
                client=client,
                user_data_root=tmp_path / "engines",
            )

    assert caught.value.code == ErrorCode.DOWNLOAD_FAILED


def test_build_plan_does_not_follow_client_default_redirects(
    tmp_path: Path,
) -> None:
    hosts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hosts.append(request.url.host)
        if request.url.host == "api.github.com":
            return httpx.Response(
                302,
                headers={"Location": "https://evil.example/release.json"},
            )
        pytest.fail("evil metadata redirect was fetched")

    with httpx.Client(
        transport=httpx.MockTransport(handler),
        follow_redirects=True,
    ) as client:
        with pytest.raises(AppError) as caught:
            build_install_plan(
                "latest",
                PlatformTarget("windows", "arm64", frozenset()),
                tmp_path / "engines",
                client=client,
                user_data_root=tmp_path / "engines",
            )

    assert caught.value.code == ErrorCode.DOWNLOAD_FAILED
    assert hosts == ["api.github.com"]


@pytest.mark.parametrize(
    "install_dir",
    [
        Path("/"),
        Path("/usr/local/lib/stockfish"),
        Path("/opt/stockfish"),
        Path(r"C:\Program Files\Stockfish"),
    ],
)
def test_build_plan_rejects_install_outside_user_owned_root(
    tmp_path: Path,
    install_dir: Path,
) -> None:
    payload = (FIXTURES / "stockfish_release.json").read_bytes()

    with httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, content=payload)
        )
    ) as client:
        with pytest.raises(AppError) as caught:
            build_install_plan(
                "latest",
                PlatformTarget("windows", "arm64", frozenset()),
                install_dir,
                client=client,
                user_data_root=tmp_path / "engines",
            )

    assert caught.value.code == ErrorCode.INVALID_INPUT


def test_build_plan_rejects_symlink_escape_from_user_owned_root(
    tmp_path: Path,
) -> None:
    user_root = tmp_path / "user" / "engines"
    outside = tmp_path / "outside"
    user_root.mkdir(parents=True)
    outside.mkdir()
    (user_root / "escape").symlink_to(outside, target_is_directory=True)
    payload = (FIXTURES / "stockfish_release.json").read_bytes()

    with httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, content=payload)
        )
    ) as client:
        with pytest.raises(AppError) as caught:
            build_install_plan(
                "latest",
                PlatformTarget("windows", "arm64", frozenset()),
                user_root / "escape" / "17.1",
                client=client,
                user_data_root=user_root,
            )

    assert caught.value.code == ErrorCode.INVALID_INPUT


def test_install_requires_approval_before_network_or_filesystem(
    tmp_path: Path,
) -> None:
    install_dir = tmp_path / "install"
    plan = InstallPlan(
        "17.1",
        PlatformTarget("linux", "x86_64", frozenset()),
        ReleaseAsset(
            "stockfish-ubuntu-x64.tar",
            (
                "https://github.com/official-stockfish/Stockfish/releases/"
                "download/sf_17.1/stockfish-ubuntu-x64.tar"
            ),
            None,
        ),
        install_dir,
        install_dir / "stockfish",
    )

    with pytest.raises(AppError) as caught:
        install_engine(plan, approved=False)

    assert caught.value.code == ErrorCode.APPROVAL_REQUIRED
    assert not install_dir.exists()


def test_install_rejects_digest_mismatch_and_preserves_existing_binary(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    install_dir = tmp_path / "install"
    install_dir.mkdir()
    executable = install_dir / "stockfish"
    executable.write_bytes(b"existing")
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w") as output:
        member = tarfile.TarInfo("stockfish/stockfish")
        member.mode = 0o755
        body = b"replacement"
        member.size = len(body)
        output.addfile(member, io.BytesIO(body))
    plan = InstallPlan(
        "17.1",
        PlatformTarget("linux", "x86_64", frozenset()),
        ReleaseAsset(
            "stockfish-ubuntu-x64.tar",
            (
                "https://github.com/official-stockfish/Stockfish/releases/"
                "download/sf_17.1/stockfish-ubuntu-x64.tar"
            ),
            "0" * 64,
            len(archive.getvalue()),
        ),
        install_dir,
        executable,
    )
    monkeypatch.setattr(
        stockfish_module,
        "_trusted_engine_root",
        lambda: tmp_path,
    )

    with httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, content=archive.getvalue())
        )
    ) as client:
        with pytest.raises(AppError) as caught:
            install_engine(plan, approved=True, client=client)

    assert caught.value.code == ErrorCode.DOWNLOAD_FAILED
    assert executable.read_bytes() == b"existing"


def test_install_accepts_validated_github_object_redirect(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr("stockfish/stockfish.exe", b"replacement")
    body = archive.getvalue()
    digest = hashlib.sha256(body).hexdigest()
    plan = InstallPlan(
        "17.1",
        PlatformTarget("windows", "arm64", frozenset()),
        ReleaseAsset(
            "stockfish-windows-arm64.zip",
            (
                "https://github.com/official-stockfish/Stockfish/releases/"
                "download/sf_17.1/stockfish-windows-arm64.zip"
            ),
            digest,
            len(body),
        ),
        tmp_path / "install",
        tmp_path / "install" / "stockfish.exe",
    )
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.host == "github.com":
            return httpx.Response(
                302,
                headers={
                    "Location": (
                        "https://objects.githubusercontent.com/release/"
                        "stockfish-windows-arm64.zip"
                    )
                },
            )
        return httpx.Response(200, content=body)

    monkeypatch.setattr(
        stockfish_module,
        "probe_engine",
        lambda command, timeout=5.0: stockfish_module.EngineInfo(
            "Stockfish 17.1",
            "17.1",
            str(command[0]),
            hashlib.sha256(Path(command[0]).read_bytes()).hexdigest(),
        ),
    )
    monkeypatch.setattr(
        stockfish_module,
        "_trusted_engine_root",
        lambda: tmp_path,
    )
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        installed = install_engine(plan, approved=True, client=client)

    assert installed.path == str(plan.executable_path)
    assert installed.options["archive_sha256"] == digest
    assert [request.url.host for request in requests] == [
        "github.com",
        "objects.githubusercontent.com",
    ]


def test_install_rejects_evil_redirect_with_follow_redirects_client(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    install_dir = tmp_path / "engines" / "17.1"
    plan = InstallPlan(
        "17.1",
        PlatformTarget("linux", "x86_64", frozenset()),
        ReleaseAsset(
            "stockfish-ubuntu-x64.tar",
            (
                "https://github.com/official-stockfish/Stockfish/releases/"
                "download/sf_17.1/stockfish-ubuntu-x64.tar"
            ),
            None,
        ),
        install_dir,
        install_dir / "stockfish",
    )
    hosts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hosts.append(request.url.host)
        if request.url.host == "github.com":
            return httpx.Response(
                302,
                headers={"Location": "https://evil.example/payload.tar"},
            )
        pytest.fail("evil redirect payload was fetched")

    monkeypatch.setattr(
        stockfish_module,
        "_trusted_engine_root",
        lambda: tmp_path / "engines",
    )
    with httpx.Client(
        transport=httpx.MockTransport(handler),
        follow_redirects=True,
    ) as client:
        with pytest.raises(AppError) as caught:
            install_engine(plan, approved=True, client=client)

    assert caught.value.code == ErrorCode.DOWNLOAD_FAILED
    assert hosts == ["github.com"]


def test_install_rejects_direct_github_object_url(tmp_path: Path) -> None:
    plan = InstallPlan(
        "17.1",
        PlatformTarget("windows", "arm64", frozenset()),
        ReleaseAsset(
            "stockfish-windows-arm64.zip",
            "https://objects.githubusercontent.com/release/stockfish.zip",
            None,
        ),
        tmp_path,
        tmp_path / "stockfish.exe",
    )

    with pytest.raises(AppError) as caught:
        install_engine(plan, approved=True)

    assert caught.value.code == ErrorCode.DOWNLOAD_FAILED


def test_install_rejects_crafted_plan_outside_recorded_user_root(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    trusted_root = tmp_path / "user" / "engines"
    plan = InstallPlan(
        "17.1",
        PlatformTarget("linux", "x86_64", frozenset()),
        ReleaseAsset(
            "stockfish-ubuntu-x64.tar",
            (
                "https://github.com/official-stockfish/Stockfish/releases/"
                "download/sf_17.1/stockfish-ubuntu-x64.tar"
            ),
            None,
        ),
        tmp_path / "outside",
        tmp_path / "outside" / "stockfish",
    )
    monkeypatch.setattr(
        stockfish_module,
        "_trusted_engine_root",
        lambda: trusted_root,
    )

    with pytest.raises(AppError) as caught:
        install_engine(plan, approved=True)

    assert caught.value.code == ErrorCode.INVALID_INPUT
    assert not plan.install_dir.exists()


def test_install_rejects_crafted_plan_symlink_escape(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    user_root = tmp_path / "user" / "engines"
    outside = tmp_path / "outside"
    user_root.mkdir(parents=True)
    outside.mkdir()
    (user_root / "escape").symlink_to(outside, target_is_directory=True)
    install_dir = user_root / "escape" / "17.1"
    plan = InstallPlan(
        "17.1",
        PlatformTarget("linux", "x86_64", frozenset()),
        ReleaseAsset(
            "stockfish-ubuntu-x64.tar",
            (
                "https://github.com/official-stockfish/Stockfish/releases/"
                "download/sf_17.1/stockfish-ubuntu-x64.tar"
            ),
            None,
        ),
        install_dir,
        install_dir / "stockfish",
    )
    monkeypatch.setattr(
        stockfish_module,
        "_trusted_engine_root",
        lambda: user_root,
    )

    with pytest.raises(AppError) as caught:
        install_engine(plan, approved=True)

    assert caught.value.code == ErrorCode.INVALID_INPUT
    assert list(outside.iterdir()) == []


def test_install_rejects_forged_plan_root_bypass(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    trusted_root = tmp_path / "user" / "engines"
    outside = tmp_path / "opt" / "stockfish"
    plan = InstallPlan(
        "17.1",
        PlatformTarget("linux", "x86_64", frozenset()),
        ReleaseAsset(
            "stockfish-ubuntu-x64.tar",
            (
                "https://github.com/official-stockfish/Stockfish/releases/"
                "download/sf_17.1/stockfish-ubuntu-x64.tar"
            ),
            None,
        ),
        outside,
        outside / "stockfish",
    )
    monkeypatch.setattr(
        stockfish_module,
        "_trusted_engine_root",
        lambda: trusted_root,
    )

    with pytest.raises(AppError) as caught:
        install_engine(plan, approved=True)

    assert caught.value.code == ErrorCode.INVALID_INPUT
    assert not outside.exists()


def test_install_accepts_independently_injected_trusted_root(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    trusted_root = tmp_path / "user" / "engines"
    install_dir = trusted_root / "17.1"
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w") as output:
        member = tarfile.TarInfo("stockfish/stockfish")
        member.mode = 0o755
        body = b"replacement"
        member.size = len(body)
        output.addfile(member, io.BytesIO(body))
    archive_body = archive.getvalue()
    plan = InstallPlan(
        "17.1",
        PlatformTarget("linux", "x86_64", frozenset()),
        ReleaseAsset(
            "stockfish-ubuntu-x64.tar",
            (
                "https://github.com/official-stockfish/Stockfish/releases/"
                "download/sf_17.1/stockfish-ubuntu-x64.tar"
            ),
            hashlib.sha256(archive_body).hexdigest(),
            len(archive_body),
        ),
        install_dir,
        install_dir / "stockfish",
    )
    monkeypatch.setattr(
        stockfish_module,
        "probe_engine",
        lambda command, timeout=5.0: stockfish_module.EngineInfo(
            "Stockfish 17.1",
            "17.1",
            str(command[0]),
            hashlib.sha256(Path(command[0]).read_bytes()).hexdigest(),
        ),
    )
    monkeypatch.setattr(
        stockfish_module,
        "_trusted_engine_root",
        lambda: trusted_root,
    )

    with httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, content=archive_body)
        )
    ) as client:
        installed = install_engine(plan, approved=True, client=client)

    assert installed.path == str(install_dir / "stockfish")
    assert Path(installed.path).read_bytes() == b"replacement"


@pytest.mark.skipif(os.name != "posix", reason="POSIX directory anchoring required")
def test_install_rejects_symlink_swap_before_final_replace(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    trusted_root = tmp_path / "engines"
    install_dir = trusted_root / "17.1"
    outside = tmp_path / "outside"
    outside.mkdir()
    victim = outside / "stockfish"
    victim.write_bytes(b"victim")
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w") as output:
        member = tarfile.TarInfo("stockfish/stockfish")
        body = b"replacement"
        member.size = len(body)
        output.addfile(member, io.BytesIO(body))
    archive_body = archive.getvalue()
    plan = InstallPlan(
        "17.1",
        PlatformTarget("linux", "x86_64", frozenset()),
        ReleaseAsset(
            "stockfish-ubuntu-x64.tar",
            (
                "https://github.com/official-stockfish/Stockfish/releases/"
                "download/sf_17.1/stockfish-ubuntu-x64.tar"
            ),
            hashlib.sha256(archive_body).hexdigest(),
            len(archive_body),
        ),
        install_dir,
        install_dir / "stockfish",
    )

    def swap_install_dir(_path: Path) -> None:
        _path.rmdir()
        _path.symlink_to(outside, target_is_directory=True)

    monkeypatch.setattr(stockfish_module, "_trusted_engine_root", lambda: trusted_root)
    monkeypatch.setattr(stockfish_module, "_before_final_replace", swap_install_dir)
    monkeypatch.setattr(
        stockfish_module,
        "probe_engine",
        lambda command, timeout=5.0: stockfish_module.EngineInfo(
            "Stockfish 17.1",
            "17.1",
            str(command[0]),
            hashlib.sha256(Path(command[0]).read_bytes()).hexdigest(),
        ),
    )
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, content=archive_body)
        )
    ) as client:
        with pytest.raises(AppError) as caught:
            install_engine(plan, approved=True, client=client)

    assert caught.value.code == ErrorCode.INVALID_INPUT
    assert victim.read_bytes() == b"victim"


@pytest.mark.skipif(os.name != "posix", reason="POSIX permissions required")
def test_install_rejects_insecure_trusted_root_permissions(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    trusted_root = tmp_path / "engines"
    trusted_root.mkdir(mode=0o777)
    trusted_root.chmod(0o777)
    install_dir = trusted_root / "17.1"
    plan = InstallPlan(
        "17.1",
        PlatformTarget("linux", "x86_64", frozenset()),
        ReleaseAsset(
            "stockfish-ubuntu-x64.tar",
            (
                "https://github.com/official-stockfish/Stockfish/releases/"
                "download/sf_17.1/stockfish-ubuntu-x64.tar"
            ),
            None,
        ),
        install_dir,
        install_dir / "stockfish",
    )
    monkeypatch.setattr(stockfish_module, "_trusted_engine_root", lambda: trusted_root)

    with pytest.raises(AppError) as caught:
        install_engine(plan, approved=True)

    assert caught.value.code == ErrorCode.INVALID_INPUT


@pytest.mark.skipif(os.name != "posix", reason="executable wrappers require POSIX")
def test_discovery_skips_malformed_option_engine(
    tmp_path: Path,
) -> None:
    explicit = tmp_path / "broken"
    explicit.write_text(
        f"#!{sys.executable}\n"
        "import runpy, sys\n"
        f"sys.argv = [{str(FAKE_UCI)!r}, 'malformed-option']\n"
        f"runpy.run_path({str(FAKE_UCI)!r}, run_name='__main__')\n",
        encoding="utf-8",
    )
    explicit.chmod(0o700)
    cached = tmp_path / "cache" / "stockfish"
    cached.parent.mkdir()
    cached.write_text(
        f"#!{sys.executable}\n"
        "import runpy, sys\n"
        f"sys.argv = [{str(FAKE_UCI)!r}]\n"
        f"runpy.run_path({str(FAKE_UCI)!r}, run_name='__main__')\n",
        encoding="utf-8",
    )
    cached.chmod(0o700)

    found = discover_engine(explicit, cached.parent)

    assert found is not None
    assert found.path == str(cached)


def test_discovery_probes_candidates_in_order_and_ignores_broken(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    explicit = tmp_path / "broken"
    explicit.write_bytes(b"broken")
    cached = tmp_path / "cache" / "stockfish"
    cached.parent.mkdir()
    cached.write_bytes(b"valid")
    seen: list[Path] = []

    def fake_probe(command: list[str], timeout: float = 5.0):
        path = Path(command[0])
        seen.append(path)
        if path == explicit:
            raise AppError(ErrorCode.ENGINE_FAILED, "broken", "replace it")
        return stockfish_module.EngineInfo(
            "FixtureFish 1.0",
            "1.0",
            str(path),
            hashlib.sha256(path.read_bytes()).hexdigest(),
        )

    monkeypatch.setattr(stockfish_module, "probe_engine", fake_probe)
    monkeypatch.setattr(stockfish_module.shutil, "which", lambda name: None)

    found = discover_engine(explicit, cached.parent)

    assert found is not None
    assert found.path == str(cached)
    assert seen == [explicit, cached]
