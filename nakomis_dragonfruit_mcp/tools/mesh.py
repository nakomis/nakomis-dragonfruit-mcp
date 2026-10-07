"""mesh_info: size, volume and sanity checks for an STL before it goes anywhere near a slicer."""

from __future__ import annotations

import math
from pathlib import Path

from pydantic import BaseModel, Field, ValidationError

from nakomis_dragonfruit_mcp import cli
from nakomis_dragonfruit_mcp.app import ToolResult, mcp

# Resin models are measured in millimetres. A model smaller than this is almost
# certainly in metres or inches; one larger than the other limit, in microns.
TINY_MM = 1.0
HUGE_MM = 300.0
# A closed solid fills a decent fraction of its bounding box. Far below this and
# the signed volume has probably cancelled out (open or self-overlapping mesh).
MIN_FILL_RATIO = 0.001


class Vec3(BaseModel):
    x: float
    y: float
    z: float


class MeshInfo(ToolResult):
    path: str
    source: str = Field(description="What the CLI read: stl, voxl or directory")
    triangles: int
    vertices: int = Field(description="Unwelded: three per triangle")
    bbox_min: Vec3 = Field(description="mm, in the file's own coordinate frame")
    bbox_max: Vec3
    size_mm: Vec3
    volume_mm3: float = Field(description="Signed-volume integral, absolute value")
    volume_ml: float = Field(description="volume_mm3 / 1000: the resin a solid print needs")


def _vec(values: object, what: str) -> Vec3:
    if not isinstance(values, list) or len(values) != 3:
        raise cli.CliError(f"unexpected `mesh info` {what}: {values!r}")
    return Vec3(x=values[0], y=values[1], z=values[2])


@mcp.tool()
def mesh_info(stl_path: str) -> MeshInfo:
    """Report an STL's triangle count, bounding box, size (mm) and volume (mm3 and ml).

    Warns about sizes that suggest the wrong units and volumes that suggest the
    mesh is not a closed solid. Volume is the CLI's signed-volume sum, which
    cannot tell a closed mesh from an open one that happens to integrate sensibly;
    it does not report watertightness itself. The absence of the fill-ratio
    warning therefore proves nothing about whether the mesh is closed.
    """
    path = Path(stl_path).expanduser()
    if not path.exists():
        raise cli.CliError(f"mesh not found: {path}")
    result = cli.run(cli.DRAGONFRUIT_CLI, ["mesh", "info", str(path), "--json"], parse_json=True)
    info = result.data
    # Upstream dev moves fast: a changed schema should say so, not KeyError.
    if not isinstance(info, dict) or not {"triangles", "bbox", "volume_mm3"} <= info.keys():
        raise cli.CliError(
            f"unexpected `dragonfruit-cli mesh info` output: {result.stdout[:200]!r}"
        )
    bbox = info["bbox"]
    if not isinstance(bbox, dict) or not {"min", "max", "size"} <= bbox.keys():
        raise cli.CliError(f"unexpected `mesh info` bbox: {bbox!r}")
    # Null, text or non-finite numbers are a schema change too, not a crash.
    try:
        size = _vec(bbox["size"], "size")
        bbox_min = _vec(bbox["min"], "min")
        bbox_max = _vec(bbox["max"], "max")
        volume = float(info["volume_mm3"])
        triangles = int(info["triangles"])
        vertices = int(info.get("vertices", triangles * 3))
    except (TypeError, ValueError, ValidationError) as e:
        raise cli.CliError(f"unexpected `mesh info` values: {e}") from e
    numbers = [
        volume,
        size.x,
        size.y,
        size.z,
        *bbox_min.model_dump().values(),
        *bbox_max.model_dump().values(),
    ]
    if not all(math.isfinite(n) for n in numbers):
        raise cli.CliError("unexpected `mesh info` values: non-finite number")

    warnings = []
    if triangles == 0:
        warnings.append("The mesh has no triangles.")
    else:
        dims = (size.x, size.y, size.z)
        if max(dims) < TINY_MM:
            warnings.append(
                f"The model is only {max(dims):.3g} mm across at most: the file may be in "
                "metres or inches rather than millimetres."
            )
        elif max(dims) > HUGE_MM:
            warnings.append(
                f"The model is {max(dims):.0f} mm along one axis: the file may be in "
                "microns or otherwise have the wrong units."
            )
        box = size.x * size.y * size.z
        if box > 0 and volume / box < MIN_FILL_RATIO:
            warnings.append(
                f"Volume ({volume:.2f} mm3) is almost nothing next to the bounding box "
                f"({box:.0f} mm3): the mesh is probably not a closed solid (holes, "
                "inverted or overlapping shells). Repair it before slicing."
            )
        elif box == 0:
            warnings.append("The model is flat along at least one axis, so it has no volume.")

    return MeshInfo(
        path=str(path),
        source=str(info.get("source", "stl")),
        triangles=triangles,
        vertices=vertices,
        bbox_min=bbox_min,
        bbox_max=bbox_max,
        size_mm=size,
        volume_mm3=volume,
        volume_ml=volume / 1000.0,
        warnings=warnings,
    )
