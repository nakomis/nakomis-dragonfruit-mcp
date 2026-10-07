import stat
import time
from pathlib import Path

import pytest

from nakomis_dragonfruit_mcp import cli
from nakomis_dragonfruit_mcp.tools import engine


def make_fake_binary(directory: Path, name: str, script: str) -> Path:
    path = directory / name
    path.write_text(f"#!/bin/sh\n{script}\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


@pytest.fixture
def bin_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("NDFM_BIN_DIR", str(tmp_path))
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    return tmp_path


def test_bin_dir_defaults_to_repo_bin(monkeypatch):
    monkeypatch.delenv("NDFM_BIN_DIR", raising=False)
    assert cli.bin_dir() == cli.REPO_ROOT / "bin"


def test_find_binary_prefers_bin_dir(bin_dir):
    fake = make_fake_binary(bin_dir, "dragonfruit-cli", "exit 0")
    assert cli.find_binary("dragonfruit-cli") == fake


def test_find_binary_falls_back_to_path(bin_dir):
    found = cli.find_binary("sh")
    assert found.name == "sh"
    assert found.parent != bin_dir


def test_find_binary_missing_says_how_to_fix(bin_dir):
    with pytest.raises(cli.CliError, match="scripts/build.sh"):
        cli.find_binary("no-such-binary-ndfm")


def test_find_binary_ignores_non_executable(bin_dir):
    (bin_dir / "no-such-binary-ndfm").write_text("not executable")
    with pytest.raises(cli.CliError):
        cli.find_binary("no-such-binary-ndfm")


def test_run_captures_stdout_and_args(bin_dir):
    make_fake_binary(bin_dir, "fake", 'echo "hello $1"')
    result = cli.run("fake", ["world"])
    assert result.stdout.strip() == "hello world"
    assert result.args == ["world"]
    assert result.data is None


def test_run_parses_json(bin_dir):
    make_fake_binary(bin_dir, "fake", "echo '{\"triangles\": 12}'")
    assert cli.run("fake", [], parse_json=True).data == {"triangles": 12}


def test_run_bad_json_raises(bin_dir):
    make_fake_binary(bin_dir, "fake", "echo not json")
    with pytest.raises(cli.CliError, match="invalid JSON"):
        cli.run("fake", [], parse_json=True)


def test_run_nonzero_exit_reports_stderr(bin_dir):
    make_fake_binary(bin_dir, "fake", "echo boom >&2; exit 3")
    with pytest.raises(cli.CliError, match="exited 3: boom"):
        cli.run("fake", [])


def test_run_timeout(bin_dir):
    # sh forks sleep, so this also checks the grandchild is killed: without
    # the process-group kill, communicate() would wait out the full sleep.
    make_fake_binary(bin_dir, "fake", "sleep 30")
    start = time.monotonic()
    with pytest.raises(cli.CliError, match="timed out"):
        cli.run("fake", [], timeout=0.2)
    assert time.monotonic() - start < 5


def test_run_spawn_failure_is_cli_error(bin_dir):
    bad = bin_dir / "fake"
    bad.write_bytes(b"\x00not an executable format")
    bad.chmod(0o755)
    with pytest.raises(cli.CliError, match="could not run"):
        cli.run("fake", [])


def test_engine_info(bin_dir):
    make_fake_binary(
        bin_dir,
        "dragonfruit-cli",
        """echo '{"version": "1.0.0", "supported_formats": [".goo", ".ctb"]}'""",
    )
    result = engine.engine_info()
    assert result.version == "1.0.0"
    assert result.formats == [".goo", ".ctb"]
    assert result.cli_path == str(bin_dir / "dragonfruit-cli")
    assert result.warnings == ["`dragonfruit-cli info` no longer reports slice_defaults"]


@pytest.mark.parametrize("output", ["'[]'", """'{"version": "1.0.0"}'"""])
def test_engine_info_unexpected_schema(bin_dir, output):
    make_fake_binary(bin_dir, "dragonfruit-cli", f"echo {output}")
    with pytest.raises(cli.CliError, match="unexpected"):
        engine.engine_info()


def _real_cli_available() -> bool:
    try:
        cli.find_binary(cli.DRAGONFRUIT_CLI)
    except cli.CliError:
        return False
    return True


@pytest.mark.integration
@pytest.mark.skipif(
    not _real_cli_available(),
    reason="dragonfruit-cli not built (run scripts/build.sh)",
)
def test_engine_info_real_binary_lists_goo():
    assert ".goo" in engine.engine_info().formats


def test_dragonfruit_dir_override(monkeypatch, tmp_path):
    monkeypatch.setenv("NDFM_DRAGONFRUIT_DIR", str(tmp_path))
    assert cli.dragonfruit_dir() == tmp_path


def test_run_ts_missing_tsx_says_how_to_fix(monkeypatch, tmp_path):
    monkeypatch.setenv("NDFM_DRAGONFRUIT_DIR", str(tmp_path))
    with pytest.raises(cli.CliError, match="scripts/build.sh"):
        cli.run_ts(["--help"])


def test_run_ts_runs_script_from_dragonfruit_dir(monkeypatch, tmp_path):
    # A stand-in tsx that reports its cwd, script, args and the tsconfig env var.
    monkeypatch.setenv("NDFM_DRAGONFRUIT_DIR", str(tmp_path))
    tsx_dir = tmp_path / "node_modules" / ".bin"
    tsx_dir.mkdir(parents=True)
    make_fake_binary(
        tsx_dir,
        "tsx",
        'printf \'{"cwd": "%s", "argv": "%s", "tsconfig": "%s"}\' "$PWD" "$*" "$TSX_TSCONFIG_PATH"',
    )
    result = cli.run_ts(["scene", "list-models"], parse_json=True)
    assert Path(result.data["cwd"]).resolve() == tmp_path.resolve()
    assert result.data["argv"] == "scripts/dragonfruit-ts-cli.ts scene list-models"
    assert result.data["tsconfig"] == str(tmp_path / "tsconfig.json")


@pytest.mark.integration
@pytest.mark.skipif(
    not (cli.dragonfruit_dir() / "node_modules" / ".bin" / "tsx").exists(),
    reason="DragonFruit node_modules not installed (run scripts/build.sh)",
)
def test_run_ts_real_cli_help():
    assert "scene" in cli.run_ts(["--help"]).stdout
