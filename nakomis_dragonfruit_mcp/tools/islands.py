"""find_islands: where a model first needs support, from DragonFruit's island scan."""

from __future__ import annotations

import json
import math
import tempfile
import time
from pathlib import Path

from pydantic import BaseModel, Field

from nakomis_dragonfruit_mcp import cli
from nakomis_dragonfruit_mcp.app import ToolResult, mcp

# Detections this close in XY and this few layers apart are one feature: a
# sloping tip leaves a fresh speck in nearly every layer it climbs through.
CLUSTER_LAYERS = 10
# A detection of a single pixel is at the raster's resolution.
SINGLE_PIXEL_TOLERANCE = 1.01


class Island(BaseModel):
    layer: int = Field(description="First layer (0-based) in which the island appears")
    z_mm: float = Field(description="Bottom of that layer, in the STL's own Z")
    x_mm: float = Field(description="Island centroid in the STL's own X")
    y_mm: float = Field(description="Island centroid in the STL's own Y")
    area_mm2: float = Field(
        description="Unsupported area in its first layer (summed over a cluster)"
    )
    detections: int = Field(description="Raw island detections merged into this entry")


class FindIslandsResult(ToolResult):
    path: str
    layers: int
    layer_height_mm: float
    px_mm: float
    support_buffer_mm: float
    bbox_min: list[float] = Field(
        description="[x, y, z] mm of the model, to check positions against"
    )
    bbox_max: list[float]
    islands_total: int = Field(description="Islands found after filtering and clustering")
    raw_detections: int = Field(
        description="Off-plate islands the tracker reported, before filtering and clustering"
    )
    plate_contacts: int = Field(description="Layer-0 regions: on the plate, not islands")
    total_area_mm2: float
    truncated: bool = Field(description="True when islands holds fewer than islands_total")
    islands: list[Island] = Field(description="Largest first-layer area first")
    elapsed_s: float


def _read_json(path: Path) -> object:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as e:
        raise cli.CliError(f"could not read {path.name} from the island scan: {e}") from e


def _centroid(snapshots: dict[int, list], layer: int, island_id: int, out: Path) -> dict:
    """The island's centroid in its first layer, in pixels.

    `islands[].centroid` averages every layer the island lives through, and an
    island that merges into the body lives for hundreds, so the tracker's
    per-layer snapshot at the island's first layer is the one that locates its tip.
    """
    if layer not in snapshots:
        data = _read_json(out / "tracker-state" / f"{layer:03d}.islands.json")
        if not isinstance(data, list):
            raise cli.CliError(f"unexpected tracker-state for layer {layer}")
        snapshots[layer] = data
    for snap in snapshots[layer]:
        if snap.get("id") == island_id:
            centroid = snap.get("last_layer_centroid")
            if centroid:
                return centroid
    raise cli.CliError(f"island {island_id} missing from tracker-state at layer {layer}")


def _cluster(found: list[Island], cluster_mm: float) -> list[Island]:
    clusters: list[list[Island]] = []
    for isl in sorted(found, key=lambda i: i.layer):
        for members in clusters:
            last = members[-1]
            if (
                isl.layer - last.layer <= CLUSTER_LAYERS
                and math.hypot(isl.x_mm - last.x_mm, isl.y_mm - last.y_mm) <= cluster_mm
            ):
                members.append(isl)
                break
        else:
            clusters.append([isl])
    merged = []
    for members in clusters:
        area = sum(m.area_mm2 for m in members)
        first = members[0]
        merged.append(
            Island(
                layer=first.layer,
                z_mm=first.z_mm,
                # Weighted by area, so a tip's widest detection dominates its position.
                x_mm=sum(m.x_mm * m.area_mm2 for m in members) / area,
                y_mm=sum(m.y_mm * m.area_mm2 for m in members) / area,
                area_mm2=area,
                detections=sum(m.detections for m in members),
            )
        )
    return merged


@mcp.tool()
def find_islands(
    stl_path: str,
    layer_height: float = 0.05,
    px_mm: float = 0.1,
    support_buffer_mm: float = 0.6,
    min_area_mm2: float = 0.0,
    cluster_mm: float = 1.0,
    max_islands: int = 25,
) -> FindIslandsResult:
    """Find islands: regions that appear in a layer with nothing solid beneath them.

    These are where a resin print needs supports. The model is scanned as it sits
    in the STL: layer 0 is its lowest point, as if lying on the plate, and no
    supports are considered. Positions are in the STL's own X/Y/Z (mm). Layers are
    sliced `layer_height` apart on a `px_mm` grid; a region counts as supported if
    solid lies within `support_buffer_mm` of it in the layer below. Islands whose
    first-layer area is under `min_area_mm2` are dropped. Detections within
    `cluster_mm` (and ten layers) of each other are merged into one entry (0
    disables that). Only the `max_islands` largest are returned, with `truncated`
    set if there were more.
    """
    if min(layer_height, px_mm) <= 0 or support_buffer_mm < 0 or cluster_mm < 0:
        raise cli.CliError(
            "layer_height and px_mm must be positive; buffer and cluster non-negative"
        )
    if max_islands < 1:
        raise cli.CliError("max_islands must be at least 1")
    path = Path(stl_path).expanduser()
    if not path.exists():
        raise cli.CliError(f"mesh not found: {path}")

    start = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="ndfm-islands-") as tmp:
        out = Path(tmp)
        # The CLI also prints the result on stdout (--json); result.json holds the same.
        cli.run(
            cli.DRAGONFRUIT_CLI,
            [
                "island",
                "full",
                str(path),
                "-o",
                str(out),
                "--json",
                "--px-mm",
                str(px_mm),
                "--layer-height",
                str(layer_height),
                "--buffer",
                str(support_buffer_mm),
            ],  # fmt: skip
        )
        result = _read_json(out / "result.json")
        if not isinstance(result, dict) or not {"layers", "params", "islands"} <= result.keys():
            raise cli.CliError("unexpected island scan result.json: missing layers/params/islands")
        try:
            bbox = result["params"]["bbox"]
            lo = [float(bbox[f"min_{a}"]) for a in "xyz"]
            hi = [float(bbox[f"max_{a}"]) for a in "xyz"]
            raw = result["islands"]
            snapshots: dict[int, list] = {}
            found: list[Island] = []
            plate = 0
            for isl in raw:
                layer = int(isl["first_layer"])
                if layer == 0:
                    plate += 1
                    continue
                area = float(isl["per_layer_area_mm2"][str(layer)])
                if area < min_area_mm2:
                    continue
                c = _centroid(snapshots, layer, isl["id"], out)
                found.append(
                    Island(
                        layer=layer,
                        z_mm=lo[2] + layer * layer_height,
                        # Pixel indices count from the grid's corner (min X, max Y);
                        # +0.5 is the pixel centre. Verified against a box of known position.
                        x_mm=lo[0] + (c["x"] + 0.5) * px_mm,
                        y_mm=hi[1] - (c["y"] + 0.5) * px_mm,
                        area_mm2=area,
                        detections=1,
                    )
                )
        except (KeyError, TypeError, ValueError) as e:
            raise cli.CliError(f"unexpected island scan output: {e!r}") from e
        layers = int(result["layers"])
    elapsed = time.monotonic() - start

    merged = _cluster(found, cluster_mm) if cluster_mm > 0 else found
    merged.sort(key=lambda i: (-i.area_mm2, i.layer))
    shown = merged[:max_islands]

    warnings = []
    if not merged:
        warnings.append("No islands found: nothing in the model starts in mid-air.")
    if len(merged) > len(shown):
        warnings.append(
            f"Showing the {len(shown)} largest of {len(merged)} islands; "
            "raise max_islands to see the rest."
        )
    one_px = [i for i in merged if i.area_mm2 <= px_mm * px_mm * SINGLE_PIXEL_TOLERANCE]
    if one_px:
        warnings.append(
            f"{len(one_px)} islands are a single pixel ({px_mm * px_mm:.3g} mm2): tips at the "
            f"scan's resolution, which may be noise. Rerun with a smaller px_mm to check."
        )
    outside = [
        i
        for i in shown
        if not (lo[0] <= i.x_mm <= hi[0] and lo[1] <= i.y_mm <= hi[1] and lo[2] <= i.z_mm <= hi[2])
    ]
    if outside:
        warnings.append(f"{len(outside)} island positions fall outside the model's bounding box.")

    return FindIslandsResult(
        path=str(path),
        layers=layers,
        layer_height_mm=layer_height,
        px_mm=px_mm,
        support_buffer_mm=support_buffer_mm,
        bbox_min=lo,
        bbox_max=hi,
        islands_total=len(merged),
        raw_detections=len(raw) - plate,
        plate_contacts=plate,
        total_area_mm2=sum(i.area_mm2 for i in merged),
        truncated=len(merged) > len(shown),
        islands=shown,
        elapsed_s=round(elapsed, 2),
        warnings=warnings,
    )
