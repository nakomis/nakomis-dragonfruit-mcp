"""Running DragonFruit's `dragonfruit-cli` (and our own Rust tool) as subprocesses.

The binaries are built by `scripts/build.sh` into `bin/` at the repo root.
`$NDFM_BIN_DIR` overrides that location, and a binary on `$PATH` is the last
resort, so a packaged install can work without the repo checkout.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
DRAGONFRUIT_CLI = "dragonfruit-cli"
MCP_TOOLS = "dragonfruit-mcp-tools"
TS_CLI_SCRIPT = "scripts/dragonfruit-ts-cli.ts"

# Slicing a large model can take minutes on an old machine; nothing we run
# should take longer than this.
DEFAULT_TIMEOUT_S = 30 * 60


class CliError(RuntimeError):
    """A binary was missing, timed out, exited non-zero, or printed bad JSON."""


def bin_dir() -> Path:
    return Path(os.environ.get("NDFM_BIN_DIR", REPO_ROOT / "bin"))


def dragonfruit_dir() -> Path:
    """The DragonFruit checkout: the submodule, or `$NDFM_DRAGONFRUIT_DIR`."""
    return Path(os.environ.get("NDFM_DRAGONFRUIT_DIR", REPO_ROOT / "vendor" / "dragonfruit"))


def find_tsx() -> Path:
    tsx = dragonfruit_dir() / "node_modules" / ".bin" / "tsx"
    if not tsx.exists():
        raise CliError(f"tsx not found at {tsx}. Run scripts/build.sh first (it runs npm ci).")
    return tsx


def find_binary(name: str) -> Path:
    candidate = bin_dir() / name
    if candidate.is_file() and os.access(candidate, os.X_OK):
        return candidate
    on_path = shutil.which(name)
    if on_path:
        return Path(on_path)
    raise CliError(f"{name} not found in {bin_dir()} or on PATH. Run scripts/build.sh first.")


@dataclass
class CliResult:
    exe: Path
    args: list[str]
    stdout: str
    stderr: str
    data: Any = None


def run(
    binary: str,
    args: list[str],
    *,
    parse_json: bool = False,
    timeout: float = DEFAULT_TIMEOUT_S,
) -> CliResult:
    """Run `binary args...`, raising CliError on any failure.

    With parse_json, stdout must be a single JSON document (the CLI's `--json`
    output); it is parsed into `data`.
    """
    exe = find_binary(binary)
    return _run_command(exe, binary, args, parse_json=parse_json, timeout=timeout)


def run_ts(
    args: list[str],
    *,
    script: str | Path = TS_CLI_SCRIPT,
    parse_json: bool = False,
    timeout: float = DEFAULT_TIMEOUT_S,
) -> CliResult:
    """Run a TypeScript script under DragonFruit's tsx, from the DragonFruit checkout.

    By default that is `dragonfruit-ts-cli`. `script` may also be one of our own
    scripts (an absolute path): it runs with DragonFruit's tsconfig, so imports
    of DragonFruit's `src/` (including its `@/` alias) resolve. Upstream's
    stderr chatter (such as the SettingsStore localStorage trace) is left in
    `stderr` and never treated as a failure.
    """
    df = dragonfruit_dir()
    env = {**os.environ, "TSX_TSCONFIG_PATH": str(df / "tsconfig.json")}
    return _run_command(
        find_tsx(),
        f"tsx {Path(script).name}",
        [str(script), *args],
        parse_json=parse_json,
        timeout=timeout,
        cwd=df,
        env=env,
    )


def _run_command(
    exe: Path,
    label: str,
    args: list[str],
    *,
    parse_json: bool,
    timeout: float,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
) -> CliResult:
    cmd = [str(exe), *args]
    try:
        # A session of its own, so a timeout kills any children it started too
        # (the TS CLI shells out to dragonfruit-cli), not just the direct child.
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
            cwd=cwd,
            env=env,
        )
    except OSError as e:
        raise CliError(f"could not run {exe}: {e}") from e
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as e:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.communicate()
        raise CliError(f"{label} timed out after {timeout:g}s: {' '.join(args)}") from e

    if proc.returncode != 0:
        detail = (stderr or stdout).strip()
        raise CliError(f"{label} exited {proc.returncode}: {detail}")

    result = CliResult(exe=exe, args=args, stdout=stdout, stderr=stderr)
    if parse_json:
        try:
            result.data = json.loads(stdout)
        except json.JSONDecodeError as e:
            raise CliError(f"{label} printed invalid JSON: {stdout[:200]!r}") from e
    return result
