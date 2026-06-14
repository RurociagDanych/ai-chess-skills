import pytest

from ai_chess.cli import main


def test_version_prints_json(capsys):
    assert main(["--version"]) == 0
    assert capsys.readouterr().out == '{"version":"0.1.0"}\n'


def test_help_uses_console_script_name(capsys):
    with pytest.raises(SystemExit) as exc_info:
        main(["--help"])

    assert exc_info.value.code == 0
    assert capsys.readouterr().out.startswith("usage: ai-chess")
