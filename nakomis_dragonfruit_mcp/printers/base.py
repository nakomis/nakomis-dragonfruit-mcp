"""The printer plugin interface.

A printer is a DragonFruit printer profile, plus optional Python behaviour:

- A profile is one of: an official preset reference (`{"presetId": ...}`), a
  full custom profile (the shape of DragonFruit's `printers.json` entries), or
  an app-exported bundle (`{"printer": {...}, "materials": [...]}`).
- A `Printer` subclass in a `.py` drop-in names its profile with `preset_id` or
  `profile` and may override the hooks below. Printers that need no behaviour
  are plain `.json` files; the loader wraps those in a bare `Printer`.

`dragonfruit-ts-cli scene slice` turns the profile into the slice job (screen,
build plate, format version, X-packing, anti-aliasing), so there is no
preset-to-flags mapping here.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar

from nakomis_dragonfruit_mcp.printers import presets


@dataclass
class SliceJob:
    """What the caller asked for, as the driver hooks see it."""

    stl_path: Path
    out_path: Path
    format: str | None = None
    layer_height: float | None = None
    material: Path | None = None
    aa_preset: str | None = None
    # Free-form, for drivers: a weird printer can take a weird option without
    # the MCP knowing about it.
    options: dict[str, Any] = field(default_factory=dict)


@dataclass
class SliceRun:
    """What was actually run; handed to `postprocess`."""

    profile: dict[str, Any]
    cli_args: list[str]
    result: dict[str, Any]
    # Files `postprocess` wrote next to the output; reported to the caller.
    extra_files: list[Path] = field(default_factory=list)


class Printer:
    """A printer. Subclass in a `.py` drop-in; set `name` and one profile attribute."""

    # What `printer=` matches. Drop-in `.json` printers are named by file stem.
    name: ClassVar[str] = ""
    description: ClassVar[str] = ""
    # Exactly one of these names the profile. `preset_id` is an official DragonFruit
    # preset; `profile` is a custom profile or an app-exported bundle.
    preset_id: ClassVar[str | None] = None
    profile: ClassVar[dict[str, Any] | None] = None

    def __init__(
        self,
        name: str | None = None,
        *,
        profile: dict[str, Any] | None = None,
        description: str | None = None,
    ) -> None:
        # Instances of the base class are the JSON route; `name` and `profile`
        # come from the file. Subclasses use their class attributes.
        if name is not None:
            self.name = name
        if profile is not None:
            self.profile = profile
        if description is not None:
            self.description = description
        self.route = "py" if type(self) is not Printer else "json"
        self.source: Path | None = None

    # -- profile ---------------------------------------------------------

    def base_profile(self) -> dict[str, Any]:
        """The profile as declared: a preset reference, custom profile or bundle."""
        if self.profile is not None:
            return copy.deepcopy(self.profile)
        if self.preset_id:
            return {"presetId": self.preset_id}
        raise ValueError(f"printer {self.name!r} declares neither preset_id nor profile")

    def effective_profile(self, format: str | None = None) -> dict[str, Any]:
        """The profile handed to `scene slice`, with the output format swapped if asked.

        Swapping the format means a custom profile: the preset is resolved,
        `presetId` is dropped (so DragonFruit stores it as a custom printer) and
        so is `formatVersion`, which belongs to the old format and which
        DragonFruit then derives for the new one (as upstream's Mars 4 preset
        does on the same screen).
        """
        profile = self.base_profile()
        if format is None:
            return profile
        printer = profile["printer"] if isinstance(profile.get("printer"), dict) else profile
        if "display" not in printer:
            resolved = presets.find(printer.get("presetId"))
            if resolved is None:
                raise ValueError(
                    f"cannot change the output format of {self.name!r}: "
                    f"preset {printer.get('presetId')!r} was not found in DragonFruit's plugins"
                )
            printer.clear()
            printer.update(resolved)
        printer.pop("presetId", None)
        display = printer.setdefault("display", {})
        display["outputFormat"] = format
        display.pop("formatVersion", None)
        return profile

    def output_format(self, format: str | None = None) -> str | None:
        """The extension the sliced file will have, if the profile says."""
        if format:
            return format
        section = presets.printer_section(self.base_profile())
        if "display" not in section:
            section = presets.find(section.get("presetId")) or {}
        return (section.get("display") or {}).get("outputFormat")

    def build_volume_mm(self) -> dict[str, float | None] | None:
        """Width, depth and height of the build plate, derived from the screen if need be."""
        try:
            return presets.build_volume(self.base_profile())
        except ValueError:
            return None

    # -- hooks -----------------------------------------------------------

    def prepare(self, job: SliceJob) -> SliceJob:
        """Adjust the job before slicing."""
        return job

    def extra_slice_args(self, job: SliceJob) -> list[str]:
        """Extra flags appended to `dragonfruit-ts-cli scene slice`."""
        return []

    def postprocess(self, out: Path, job: SliceJob, run: SliceRun) -> Path:
        """Rename, wrap, convert or upload the output; return the final path."""
        return out

    def warnings(self) -> list[str]:
        """Anything the caller must hear about this printer."""
        return []
