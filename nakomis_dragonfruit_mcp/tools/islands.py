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
    x_mm: float = Field(description="Island centroid in the STL's own X (area-weighted)")
    y_mm: float = Field(description="Island centroid in the STL's own Y (area-weighted)")
    first_area_mm2: float = Field(
        description="Unsupported area in the island's first layer; the ranking key"
    )
    footprint_mm2: float = Field(
        description="First-layer areas of every detection merged into this entry, summed"
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
    islands_total: int = Field(description="Islands found after clustering and filtering")
    raw_detections: int = Field(
        description="Off-plate islands the tracker reported, before clustering and filtering"
    )
    plate_contacts: int = Field(description="Layer-0 regions: on the plate, not islands")
    total_area_mm2: float = Field(
        description="Unsupported footprint: first-layer areas of all islands_total islands "
        "(every merged detection), not just the ones listed"
    )
    truncated: bool = Field(description="True when islands holds fewer than islands_total")
    islands: list[Island] = Field(description="Largest first_area_mm2 first")
    elapsed_s: float


def _read_json(path: Path) -> object:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as e:
        raise cli.CliError(f"could not read {path.name} from the island scan: {e}") from e


def _centroid(
    snapshots: dict[int, list | None], layer: int, island_id: int, out: Path
) -> dict | None:
    """The island's centroid in its first layer, in pixels; None if the scan lacks it.

    `islands[].centroid` averages every layer the island lives through, and an
    island that merges into the body lives for hundreds, so the tracker's
    per-layer snapshot at the island's first layer is the one that locates its tip.
    """
    if layer not in snapshots:
        try:
            data = json.loads((out / "tracker-state" / f"{layer:03d}.islands.json").read_text())
        except (OSError, json.JSONDecodeError):
            data = None
        snapshots[layer] = data if isinstance(data, list) else None
    for snap in snapshots[layer] or []:
        if isinstance(snap, dict) and snap.get("id") == island_id:
            centroid = snap.get("last_layer_centroid")
            if isinstance(centroid, dict) and "x" in centroid and "y" in centroid:
                return centroid
    return None


def _cluster(found: list[Island], cluster_mm: float) -> list[Island]:
    """Merge detections of one climbing tip.

    A detection joins the nearest cluster whose first detection is within
    `cluster_mm` in XY and at most CLUSTER_LAYERS layers below it; otherwise it
    starts a new cluster. Measuring from the first detection (not the latest)
    stops a slow ramp of specks chaining into one ever-growing cluster.
    """
    clusters: list[list[Island]] = []
    for isl in sorted(found, key=lambda i: i.layer):
        best, best_dist = None, cluster_mm
        for members in clusters:
            anchor = members[0]
            dist = math.hypot(isl.x_mm - anchor.x_mm, isl.y_mm - anchor.y_mm)
            if isl.layer - anchor.layer <= CLUSTER_LAYERS and dist <= best_dist:
                best, best_dist = members, dist
        if best is None:
            clusters.append([isl])
        else:
            best.append(isl)
    merged = []
    for members in clusters:
        first = members[0]
        footprint = sum(m.footprint_mm2 for m in members)
        # Weighted by area, so a tip's widest detection dominates its position.
        weights = [m.footprint_mm2 for m in members] if footprint > 0 else [1.0] * len(members)
        total = sum(weights)
        merged.append(
            Island(
                layer=first.layer,
                z_mm=first.z_mm,
                x_mm=sum(m.x_mm * w for m, w in zip(members, weights, strict=True)) / total,
                y_mm=sum(m.y_mm * w for m, w in zip(members, weights, strict=True)) / total,
                first_area_mm2=first.first_area_mm2,
                footprint_mm2=footprint,
                detections=sum(m.detections for m in members),
            )
        )
    return merged


def _plural(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


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
    solid lies within `support_buffer_mm` of it in the layer below. Detections that
    start within `cluster_mm` (XY distance from the cluster's first detection) and
    ten layers of each other are merged into one entry; 0 disables that. Entries
    whose `first_area_mm2` is under `min_area_mm2` are then dropped, and the
    `max_islands` largest by that area are returned, with `truncated` set if there
    were more.
    """
    numbers = (layer_height, px_mm, support_buffer_mm, min_area_mm2, cluster_mm)
    if not all(math.isfinite(n) for n in numbers):
        raise cli.CliError("island parameters must be finite numbers")
    if min(layer_height, px_mm) <= 0 or min(support_buffer_mm, min_area_mm2, cluster_mm) < 0:
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
        # No --json: result.json is written either way, and --json would also print it all.
        cli.run(
            cli.DRAGONFRUIT_CLI,
            [
                "island",
                "full",
                str(path),
                "-o",
                str(out),
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
            skipped = 0
            for isl in raw:
                layer = int(isl["first_layer"])
                if layer == 0:
                    plate += 1
                    continue
                area = float(isl["per_layer_area_mm2"][str(layer)])
                c = _centroid(snapshots, layer, isl["id"], out)
                if c is None:
                    skipped += 1
                    continue
                found.append(
                    Island(
                        layer=layer,
                        z_mm=lo[2] + layer * layer_height,
                        # Pixel indices count from the grid's corner (min X, max Y);
                        # +0.5 is the pixel centre. Verified against a box of known position.
                        x_mm=lo[0] + (c["x"] + 0.5) * px_mm,
                        y_mm=hi[1] - (c["y"] + 0.5) * px_mm,
                        first_area_mm2=area,
                        footprint_mm2=area,
                        detections=1,
                    )
                )
        except (KeyError, TypeError, ValueError) as e:
            raise cli.CliError(f"unexpected island scan output: {e!r}") from e
        layers = int(result["layers"])
    elapsed = time.monotonic() - start

    merged = _cluster(found, cluster_mm) if cluster_mm > 0 else found
    merged = [i for i in merged if i.first_area_mm2 >= min_area_mm2]
    merged.sort(key=lambda i: (-i.first_area_mm2, -i.footprint_mm2, i.layer))
    shown = merged[:max_islands]

    warnings = []
    if skipped:
        warnings.append(
            f"{_plural(skipped, 'island was', 'islands were')} skipped: the scan "
            f"has no tracker snapshot locating {'it' if skipped == 1 else 'them'}."
        )
    if not merged:
        warnings.append("No islands found: nothing in the model starts in mid-air.")
    if len(merged) > len(shown):
        warnings.append(
            f"Showing the {len(shown)} largest of {len(merged)} islands; "
            "raise max_islands to see the rest."
        )
    one_px = [i for i in merged if i.first_area_mm2 <= px_mm * px_mm * SINGLE_PIXEL_TOLERANCE]
    if one_px:
        warnings.append(
            f"{_plural(len(one_px), 'island is', 'islands are')} a single pixel "
            f"({px_mm * px_mm:.3g} mm2) in the first layer: at the scan's resolution, "
            "so possibly noise. Rerun with a smaller px_mm to check."
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
        total_area_mm2=sum(i.footprint_mm2 for i in merged),
        truncated=len(merged) > len(shown),
        islands=shown,
        elapsed_s=round(elapsed, 2),
        warnings=warnings,
    )
