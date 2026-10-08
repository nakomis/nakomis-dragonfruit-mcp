"""Elegoo Mars 5 Ultra: DragonFruit's official preset, written as .goo by default.

The Mars 5 Ultra prints .goo (verified on firmware V1.5.0), and .goo can be
inspected and previewed by this server; `format=".ctb"` gives upstream's .ctb
v5enc, which DragonFruit encrypts and this server cannot read back.

The driver's one override fixes DragonFruit's tilting-mode .goo, which left the
build plate at the bottom for a whole print on this printer (NDFM-17): after
slicing, `postprocess` rewrites the motion fields to the values Chitubox writes
for it (see `goo_motion`), and sets the header's mirror flag from the profile.
"""

from pathlib import Path

from nakomis_dragonfruit_mcp.goo_motion import normalise_tilting_motion
from nakomis_dragonfruit_mcp.printers import Printer, SliceJob, SliceRun


class Mars5Ultra(Printer):
    name = "mars5ultra"
    description = (
        "Elegoo Mars 5 Ultra. Defaults to .goo: the Mars 5 Ultra prints .goo (verified on "
        "firmware V1.5.0), and .goo can be inspected and previewed by this server. "
        "format='.ctb' gives upstream's .ctb v5enc, which DragonFruit encrypts and this "
        "server cannot read back. .goo output is post-processed so the plate moves the way "
        "Chitubox's files make it move on this printer."
    )
    preset_id = "elegoo-mars-5-ultra-ctb"
    default_format = ".goo"

    def postprocess(self, out: Path, job: SliceJob, run: SliceRun) -> Path:
        if out.suffix.lower() == ".goo":
            display = run.profile.get("display", {}) if isinstance(run.profile, dict) else {}
            try:
                normalise_tilting_motion(out, mirror_x=display.get("mirrorX"))
            except Exception:
                # Never leave an unpatched .goo where it could be printed: on this
                # printer its plate would not rise. Keep it, renamed, for diagnosis.
                out.replace(out.with_name(out.name + ".unpatched"))
                raise
        return out
