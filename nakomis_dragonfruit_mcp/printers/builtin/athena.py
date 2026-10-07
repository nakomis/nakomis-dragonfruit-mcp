"""Concepts3D Athena 8K: the built-in example of a `.py` printer driver.

The profile is DragonFruit's official preset (7680 x 4320, 28.5 um pixels,
NanoDLP, X-packing `rgb8_div3`), all of which `scene slice` applies itself. The
driver adds one real override: `postprocess` writes a sidecar JSON next to the
output recording the profile and the CLI arguments the slice used, so a
`.nanodlp` can be reproduced later.
"""

import json
from pathlib import Path

from nakomis_dragonfruit_mcp.printers import Printer, SliceJob, SliceRun


class Athena8K(Printer):
    name = "athena8k"
    description = "Concepts3D Athena 8K (NanoDLP); writes a sidecar JSON of how it was sliced"
    preset_id = "concepts3d-athena1-8k-nanodlp"

    def postprocess(self, out: Path, job: SliceJob, run: SliceRun) -> Path:
        sidecar = out.with_name(out.name + ".json")
        sidecar.write_text(
            json.dumps(
                {
                    "printer": self.name,
                    "stl": str(job.stl_path),
                    "profile": run.profile,
                    "scene_slice_args": run.cli_args,
                },
                indent=2,
            )
        )
        run.extra_files.append(sidecar)
        return out
