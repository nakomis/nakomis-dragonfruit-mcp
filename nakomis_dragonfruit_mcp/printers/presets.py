"""Reading DragonFruit's own printer presets and profiles.

Official presets live in `plugins/*/printers/*.json` of the DragonFruit
checkout, so upstream fixes (and new printers) arrive with a submodule bump.
"""

from __future__ import annotations

import json
from typing import Any

from nakomis_dragonfruit_mcp import cli


def printer_section(profile: dict[str, Any]) -> dict[str, Any]:
    """The printer part of a profile: the profile itself, or a bundle's `printer`."""
    inner = profile.get("printer")
    if isinstance(inner, dict) and "display" not in profile:
        return inner
    return profile


def find(preset_id: str | None) -> dict[str, Any] | None:
    """The official preset with this id, or None."""
    if not preset_id:
        return None
    for path in sorted((cli.dragonfruit_dir() / "plugins").glob("*/printers/*.json")):
        try:
            entries = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        for entry in entries if isinstance(entries, list) else [entries]:
            if isinstance(entry, dict) and entry.get("presetId") == preset_id:
                return entry
    return None


def build_volume(profile: dict[str, Any]) -> dict[str, float | None]:
    """Width, depth, height in mm. A null width or depth comes from screen x pixel size."""
    printer = printer_section(profile)
    if "display" not in printer:
        found = find(printer.get("presetId"))
        if found is None:
            raise ValueError("profile has no display and its preset was not found")
        printer = found
    volume = printer.get("buildVolumeMm") or {}
    display = printer.get("display") or {}
    pixel = printer.get("pixelSize") or {}

    def axis(given: float | None, pixels: str, size: str) -> float | None:
        if given is not None:
            return given
        if display.get(pixels) and pixel.get(size):
            return round(display[pixels] * pixel[size] / 1000, 2)
        return None

    return {
        "width": axis(volume.get("width"), "resolutionX", "x"),
        "depth": axis(volume.get("depth"), "resolutionY", "y"),
        "height": volume.get("height"),
    }
