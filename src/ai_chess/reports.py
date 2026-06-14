import html
import json
import os
import stat
import tempfile
from importlib.resources import files
from pathlib import Path

from ai_chess.analysis import _load_existing_artifact
from ai_chess.models import AnalysisArtifact


def inert_json(payload: object) -> str:
    return (
        json.dumps(payload, ensure_ascii=True, allow_nan=False, separators=(",", ":"))
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
    )


def _template_text() -> str:
    return files("ai_chess").joinpath("assets", "report.html").read_text(encoding="utf-8")


def render_report(artifact: AnalysisArtifact, insights: str | None = None) -> str:
    template = _template_text()
    rendered_insights = (
        f'<pre class="insights-text">{html.escape(insights)}</pre>'
        if insights is not None
        else '<p class="muted">No additional insights provided.</p>'
    )
    return (
        template.replace("__ANALYSIS_JSON__", inert_json(artifact.to_dict()))
        .replace("__INSIGHTS_HTML__", rendered_insights)
    )


def _atomic_write_text(path: Path, contents: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        destination_mode = stat.S_IMODE(path.stat().st_mode)
    except FileNotFoundError:
        destination_mode = None

    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)
            temporary_file.write(contents)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())

        if destination_mode is not None:
            temporary_path.chmod(destination_mode)
        os.replace(temporary_path, path)
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def write_report(
    artifact_path: Path,
    output_path: Path,
    insights_path: Path | None = None,
) -> Path:
    artifact = _load_existing_artifact(artifact_path)
    insights = (
        insights_path.read_text(encoding="utf-8") if insights_path is not None else None
    )
    _atomic_write_text(output_path, render_report(artifact, insights))
    return output_path
