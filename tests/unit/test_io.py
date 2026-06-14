import json
import os
import stat

import pytest

from ai_chess import io


def test_atomic_write_json_writes_compact_json_and_sha256(tmp_path):
    path = tmp_path / "nested" / "manifest.json"

    io.atomic_write_json(path, {"schema_version": "manifest.v1"})

    assert path.read_bytes() == b'{"schema_version":"manifest.v1"}\n'
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "schema_version": "manifest.v1"
    }
    assert list(path.parent.glob("*.tmp")) == []

    assert (
        io.sha256_file(path)
        == "c4644f4628e54043643ff4917eace0892aa410036cde801f6da23d8b8d9adee4"
    )


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits required")
def test_atomic_write_json_preserves_destination_permissions(tmp_path):
    path = tmp_path / "manifest.json"
    path.write_text("old\n", encoding="utf-8")
    path.chmod(0o644)

    io.atomic_write_json(path, {"schema_version": "manifest.v1"})

    assert stat.S_IMODE(path.stat().st_mode) == 0o644


def test_atomic_write_json_rejects_nan_and_cleans_temp_file(tmp_path):
    path = tmp_path / "manifest.json"

    with pytest.raises(ValueError):
        io.atomic_write_json(path, {"value": float("nan")})

    assert not path.exists()
    assert list(tmp_path.glob("*.tmp")) == []


def test_atomic_write_json_cleans_temp_file_when_replace_fails(tmp_path, monkeypatch):
    path = tmp_path / "manifest.json"

    def fail_replace(source, destination):
        raise OSError("replace failed")

    monkeypatch.setattr(io.os, "replace", fail_replace)

    with pytest.raises(OSError, match="replace failed"):
        io.atomic_write_json(path, {"schema_version": "manifest.v1"})

    assert not path.exists()
    assert list(tmp_path.glob("*.tmp")) == []
