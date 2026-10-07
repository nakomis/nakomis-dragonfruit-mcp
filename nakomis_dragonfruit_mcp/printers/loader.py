"""Finding printers: drop-in `.py` drivers and `.json` profiles, and choosing one.

Directories are searched in order and a later one wins a name clash (with a
warning): the built-in directory, `$NDFM_PRINTERS_DIR`, then
`~/.config/nakomis-dragonfruit-mcp/printers/`. Only those directories are
scanned. A `.py` file there is imported, so it runs with this server's
privileges: put nothing in them that you would not run yourself.

A plugin that fails to load is skipped and reported, never fatal.
"""

from __future__ import annotations

import importlib.util
import inspect
import json
import os
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from nakomis_dragonfruit_mcp.printers.base import Printer

BUILTIN_DIR = Path(__file__).resolve().parent / "builtin"
DEFAULT_PRINTER = "mars5ultra"


@dataclass
class Failure:
    source: str
    error: str


@dataclass
class Registry:
    printers: dict[str, Printer] = field(default_factory=dict)
    failures: list[Failure] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def config_dir() -> Path:
    return Path.home() / ".config" / "nakomis-dragonfruit-mcp"


def printer_dirs() -> list[Path]:
    dirs = [BUILTIN_DIR]
    if env := os.environ.get("NDFM_PRINTERS_DIR"):
        dirs.append(Path(env).expanduser())
    dirs.append(config_dir() / "printers")
    return dirs


def discover() -> Registry:
    registry = Registry()
    for directory in printer_dirs():
        if not directory.is_dir():
            continue
        for path in sorted(directory.iterdir()):
            if path.name.startswith(("_", ".")):
                continue
            if path.suffix == ".py":
                loaded = _load_py(path, registry)
            elif path.suffix == ".json":
                loaded = _load_json(path, registry)
            else:
                continue
            for printer in loaded:
                _register(registry, printer)
    return registry


def _register(registry: Registry, printer: Printer) -> None:
    previous = registry.printers.get(printer.name)
    if previous is not None:
        registry.warnings.append(
            f"printer {printer.name!r} from {printer.source} "
            f"replaces the one from {previous.source}"
        )
    registry.printers[printer.name] = printer


def _load_json(path: Path, registry: Registry) -> list[Printer]:
    try:
        profile = json.loads(path.read_text())
        if not isinstance(profile, dict):
            raise ValueError("expected one JSON object (a preset reference, profile or bundle)")
        section = profile["printer"] if isinstance(profile.get("printer"), dict) else profile
        if "presetId" not in section and "display" not in section:
            raise ValueError("neither a presetId nor a display section: not a DragonFruit profile")
        # `description` is ours, not DragonFruit's: take it out before the profile goes on.
        description = profile.pop("description", None)
        if not isinstance(description, str):
            description = section.get("name") if isinstance(section.get("name"), str) else None
        printer = Printer(path.stem, profile=profile, description=description)
    except (OSError, ValueError) as e:
        registry.failures.append(Failure(str(path), f"{type(e).__name__}: {e}"))
        return []
    printer.source = path
    return [printer]


def _load_py(path: Path, registry: Registry) -> list[Printer]:
    module_name = f"_ndfm_printer_{path.stem}_{abs(hash(str(path)))}"
    try:
        spec = importlib.util.spec_from_file_location(module_name, path)
        if spec is None or spec.loader is None:
            raise ImportError("cannot import this file")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        try:
            spec.loader.exec_module(module)
        except BaseException:
            sys.modules.pop(module_name, None)
            raise
        classes = [
            cls
            for _, cls in inspect.getmembers(module, inspect.isclass)
            if issubclass(cls, Printer) and cls is not Printer and cls.__module__ == module_name
        ]
        if not classes:
            raise ValueError("defines no Printer subclass")
        printers = []
        for cls in classes:
            if not cls.name:
                raise ValueError(f"{cls.__name__} has no `name`")
            printer = cls()
            printer.base_profile()  # must declare preset_id or profile
            printer.source = path
            printers.append(printer)
    except (Exception, SystemExit) as e:
        registry.failures.append(Failure(str(path), f"{type(e).__name__}: {e}"))
        return []
    return printers


def configured_printer() -> str | None:
    """`printer = "..."` from config.toml, if there is one."""
    path = config_dir() / "config.toml"
    try:
        value = tomllib.loads(path.read_text()).get("printer")
    except FileNotFoundError:
        return None
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise ValueError(f"cannot read {path}: {e}") from e
    return value if isinstance(value, str) and value else None


def choose_name(requested: str | None) -> tuple[str, str]:
    """The printer name and where the choice came from: arg, env, config or default."""
    if requested:
        return requested, "argument"
    if env := os.environ.get("NDFM_PRINTER"):
        return env, "$NDFM_PRINTER"
    if configured := configured_printer():
        return configured, "config.toml"
    return DEFAULT_PRINTER, "default"


def get(registry: Registry, requested: str | None) -> tuple[Printer, str]:
    name, origin = choose_name(requested)
    printer = registry.printers.get(name)
    if printer is None:
        known = ", ".join(sorted(registry.printers)) or "none"
        failed = (
            f" (failed to load: {', '.join(f.source for f in registry.failures)})"
            if registry.failures
            else ""
        )
        raise ValueError(f"unknown printer {name!r} (from {origin}); available: {known}{failed}")
    return printer, origin
