/**
 * Where supports may not go, and where they came too close (NDFM-28, NDFM-22).
 *
 * SPDX-License-Identifier: AGPL-3.0-or-later
 *
 * Zones: a drilled hole's keep-out cylinder (`KeepOutZone`) and the space above
 * a height cap (`HeightCapZone`). autosupport-slice.ts blocks contacts on the
 * model surface inside any zone before placement, and afterwards removes any
 * support whose contact or shaft still enters one.
 *
 * The overhang band (`clipOverhangRegion`, NDFM-22): overhang regions cut at a
 * height, so the overhang family supports only a lattice's base, while the
 * voxel family's islands are left alone.
 *
 * Clearance (`findGrazes`): supports that pass close to the model without
 * touching it. In clear resin a gap of a tenth of a millimetre cures shut, so a
 * column climbing past a face prints fused to it and tears it away when it is
 * removed (Chi's cheek, 9 Oct 2026). This finds such near-misses on the final
 * support mesh; it reports, it removes nothing.
 */

import { readFileSync } from 'node:fs';

import * as THREE from 'three';
import { MeshBVH } from 'three-mesh-bvh';

/** A region supports must keep out of, in the plate frame. */
export interface Zone {
    /** For warnings: "keep-out hole 2", "the height cap". */
    readonly label: string;
    /** Bounding box, for cheap rejection; may be unbounded on some sides. */
    readonly box: THREE.Box3;
    contains(x: number, y: number, z: number): boolean;
    /** Whether the straight run a to b enters the zone. */
    touchesSegment(a: THREE.Vector3, b: THREE.Vector3): boolean;
}

/** Margin round a hole's opening that supports must keep clear of, mm. */
export const KEEP_OUT_MARGIN_MM = 1;
/**
 * The zone is anchored on the hole's exit through the skin, E = start + dir *
 * length_mm: it reaches this far back along the bore (into the wall and the
 * cavity side of it) and this far past E (outside the part). A hole's start may
 * lie well inside a cavity, so the zone never reaches further in than that, and
 * never further than KEEP_OUT_BEHIND_START_MM behind the start itself. The
 * report's `t_range_mm` is this extent measured from the hole's **start**, not
 * its exit.
 */
export const KEEP_OUT_INWARD_OF_EXIT_MM = 4;
export const KEEP_OUT_OUTWARD_OF_EXIT_MM = 3;
export const KEEP_OUT_BEHIND_START_MM = 2;

export interface HoleSpec {
    x: number;
    y: number;
    z: number;
    radius_mm: number;
    direction: [number, number, number];
    /** From the start point, along `direction`, to where the bore leaves the skin. */
    length_mm: number;
    purpose?: string;
}

/** A hole's keep-out cylinder in the plate frame. */
export class KeepOutZone implements Zone {
    readonly label: string;
    readonly start: THREE.Vector3;
    readonly dir: THREE.Vector3;
    readonly exit: THREE.Vector3;
    /** The cylinder runs from `t = tMin` to `t = tMax` along `dir` from `start`. */
    readonly tMin: number;
    readonly tMax: number;
    readonly radius: number;
    readonly box: THREE.Box3;

    constructor(readonly index: number, readonly spec: HoleSpec, shift: THREE.Vector3) {
        this.label = `keep-out hole ${index}`;
        this.start = new THREE.Vector3(spec.x, spec.y, spec.z).add(shift);
        this.dir = new THREE.Vector3(...spec.direction).normalize();
        this.exit = this.start.clone().addScaledVector(this.dir, spec.length_mm);
        this.tMin = Math.max(-KEEP_OUT_BEHIND_START_MM, spec.length_mm - KEEP_OUT_INWARD_OF_EXIT_MM);
        this.tMax = spec.length_mm + KEEP_OUT_OUTWARD_OF_EXIT_MM;
        this.radius = spec.radius_mm + KEEP_OUT_MARGIN_MM;
        this.box = new THREE.Box3()
            .setFromPoints([this.start.clone().addScaledVector(this.dir, this.tMin), this.start.clone().addScaledVector(this.dir, this.tMax)])
            .expandByScalar(this.radius);
    }

    contains(x: number, y: number, z: number): boolean {
        const dx = x - this.start.x;
        const dy = y - this.start.y;
        const dz = z - this.start.z;
        const t = dx * this.dir.x + dy * this.dir.y + dz * this.dir.z;
        if (t < this.tMin || t > this.tMax) return false;
        const px = dx - t * this.dir.x;
        const py = dy - t * this.dir.y;
        const pz = dz - t * this.dir.z;
        return px * px + py * py + pz * pz <= this.radius * this.radius;
    }

    /** Sampled at most ~0.25 mm apart. */
    touchesSegment(a: THREE.Vector3, b: THREE.Vector3): boolean {
        const steps = Math.max(1, Math.ceil(a.distanceTo(b) / 0.25));
        for (let i = 0; i <= steps; i++) {
            const f = i / steps;
            if (this.contains(a.x + (b.x - a.x) * f, a.y + (b.y - a.y) * f, a.z + (b.z - a.z) * f)) return true;
        }
        return false;
    }
}

/**
 * Everything above a height, in the plate frame: no contact may land there and
 * no shaft may rise into it. `capZ` is the model's base plus the cap.
 */
export class HeightCapZone implements Zone {
    readonly label = 'the height cap';
    readonly box: THREE.Box3;

    constructor(readonly capZ: number) {
        this.box = new THREE.Box3(new THREE.Vector3(-Infinity, -Infinity, capZ), new THREE.Vector3(Infinity, Infinity, Infinity));
    }

    contains(_x: number, _y: number, z: number): boolean {
        return z > this.capZ;
    }

    touchesSegment(a: THREE.Vector3, b: THREE.Vector3): boolean {
        return a.z > this.capZ || b.z > this.capZ;
    }
}

/** The fields of a `scan_overhangs` region that clipping changes or reads. */
export interface ClippableOverhangRegion {
    triangleIds: number[];
    areaMm2: number;
    projectedAreaMm2: number;
    xyMin: [number, number];
    xyMax: [number, number];
    minZ: number;
    maxZ: number;
    footprint: { width: number; height: number; originX: number; originY: number; pxMm: number; data: number[]; surfaceZ: number[] };
    dragMomentMm3?: number;
}

/**
 * The overhang band (NDFM-22): an overhang region cut down to the part at and
 * below `topZ` (the model's base plus `--overhang-max-height`), or null when
 * none of it is in the band. The scanner may hand back a whole lattice as one
 * region running from its base to its top (the gyroid lantern did), so a
 * whole-region filter would keep all of it or none.
 *
 * Kept: footprint pixels whose surface is at or below `topZ`, and triangles
 * with any corner at or below it. Placement samples the surface on the
 * region's own triangles, so these bound where an overhang contact can land: at
 * most one triangle's height above the line, where a triangle straddles it.
 * The areas and drag moment shrink with the triangle area kept; the projected
 * area with the pixels kept. Perimeter loops are left whole: the ring is
 * sampled on the same triangles. `positions` holds the model's triangles, 9
 * floats each, in the order `triangleIds` counts them.
 */
export function clipOverhangRegion<R extends ClippableOverhangRegion>(region: R, topZ: number, positions: ArrayLike<number>): R | null {
    const { width, originX, originY, pxMm, data, surfaceZ } = region.footprint;
    const keptData = data.map((inside, i) => (inside && surfaceZ[i] <= topZ ? 1 : 0));
    let pixelsBefore = 0;
    let pixelsKept = 0;
    let minX = Infinity;
    let minY = Infinity;
    let maxX = -Infinity;
    let maxY = -Infinity;
    for (let i = 0; i < keptData.length; i++) {
        if (data[i]) pixelsBefore++;
        if (!keptData[i]) continue;
        pixelsKept++;
        const x = originX + ((i % width) + 0.5) * pxMm;
        const y = originY + (Math.floor(i / width) + 0.5) * pxMm;
        minX = Math.min(minX, x);
        minY = Math.min(minY, y);
        maxX = Math.max(maxX, x);
        maxY = Math.max(maxY, y);
    }
    const a = new THREE.Vector3();
    const b = new THREE.Vector3();
    const c = new THREE.Vector3();
    const triangle = new THREE.Triangle(a, b, c);
    let areaBefore = 0;
    let areaKept = 0;
    const keptTriangles: number[] = [];
    for (const t of region.triangleIds) {
        a.fromArray(positions, t * 9);
        b.fromArray(positions, t * 9 + 3);
        c.fromArray(positions, t * 9 + 6);
        const area = triangle.getArea();
        areaBefore += area;
        if (Math.min(a.z, b.z, c.z) > topZ) continue;
        keptTriangles.push(t);
        areaKept += area;
    }
    if (pixelsKept === 0 || (region.triangleIds.length > 0 && keptTriangles.length === 0)) return null;
    if (pixelsKept === pixelsBefore && keptTriangles.length === region.triangleIds.length) return region;
    const areaShare = areaBefore > 0 ? areaKept / areaBefore : pixelsKept / pixelsBefore;
    return {
        ...region,
        triangleIds: keptTriangles,
        areaMm2: region.areaMm2 * areaShare,
        projectedAreaMm2: region.projectedAreaMm2 * (pixelsKept / pixelsBefore),
        ...(region.dragMomentMm3 !== undefined ? { dragMomentMm3: region.dragMomentMm3 * areaShare } : {}),
        xyMin: [minX - pxMm / 2, minY - pxMm / 2],
        xyMax: [maxX + pxMm / 2, maxY + pxMm / 2],
        // The lowest point is in the band, or nothing would be; the top is cut at the line.
        maxZ: Math.min(region.maxZ, topZ),
        footprint: { ...region.footprint, data: keptData },
    };
}

/**
 * The model's triangles in the order its index lists them, 9 floats each: the
 * order raycasts' face indices count in once a BVH has sorted the index (an
 * STL loads unindexed; three-mesh-bvh gives it an index and reorders that).
 * Anything that names triangles to placement (the overhang scan's region
 * triangles, the support blockers) must count them this way (NDFM-22).
 */
export function positionsInFaceOrder(geometry: THREE.BufferGeometry): Float32Array {
    const position = geometry.getAttribute('position').array as Float32Array;
    const index = geometry.getIndex()?.array;
    if (!index) return position;
    const out = new Float32Array(index.length * 3);
    for (let i = 0; i < index.length; i++) {
        const v = index[i] * 3;
        out[i * 3] = position[v];
        out[i * 3 + 1] = position[v + 1];
        out[i * 3 + 2] = position[v + 2];
    }
    return out;
}

export function parseHoles(path: string): HoleSpec[] {
    const raw = JSON.parse(readFileSync(path, 'utf-8')) as { holes?: unknown };
    if (!raw || !Array.isArray(raw.holes)) throw new Error(`${path}: expected {"holes": [...]}`);
    return raw.holes.map((entry, i) => {
        const h = entry as Partial<HoleSpec>;
        const finite = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v);
        if (!finite(h.x) || !finite(h.y) || !finite(h.z) || !finite(h.radius_mm) || h.radius_mm <= 0
            || !finite(h.length_mm) || !Array.isArray(h.direction) || h.direction.length !== 3 || !h.direction.every(finite)
            || Math.hypot(...(h.direction as number[])) === 0) {
            throw new Error(`${path}: hole ${i} needs numeric x, y, z, radius_mm > 0, length_mm and a non-zero direction [dx, dy, dz]`);
        }
        return h as HoleSpec;
    });
}

/**
 * Indices of the model triangles that touch any zone: a vertex, an edge
 * crossing it, or the centroid. Per-zone counts, then the union. A triangle
 * whose bounding box misses a zone's is rejected before any sampling.
 */
export function triangleIndicesInZones(positions: ArrayLike<number>, zones: Zone[]): { perZone: number[]; all: Set<number> } {
    const perZone = zones.map(() => 0);
    const all = new Set<number>();
    const a = new THREE.Vector3();
    const b = new THREE.Vector3();
    const c = new THREE.Vector3();
    const centroid = new THREE.Vector3();
    const box = new THREE.Box3();
    for (let t = 0; t < positions.length / 9; t++) {
        a.fromArray(positions, t * 9);
        b.fromArray(positions, t * 9 + 3);
        c.fromArray(positions, t * 9 + 6);
        box.makeEmpty().expandByPoint(a).expandByPoint(b).expandByPoint(c);
        centroid.copy(a).add(b).add(c).divideScalar(3);
        zones.forEach((zone, k) => {
            if (!zone.box.intersectsBox(box)) return;
            if (zone.contains(a.x, a.y, a.z) || zone.contains(b.x, b.y, b.z) || zone.contains(c.x, c.y, c.z)
                || zone.contains(centroid.x, centroid.y, centroid.z)
                || zone.touchesSegment(a, b) || zone.touchesSegment(b, c) || zone.touchesSegment(c, a)) {
                perZone[k]++;
                all.add(t);
            }
        });
    }
    return { perZone, all };
}

/** Defaults for `findGrazes`. */
export const DEFAULT_MIN_CLEARANCE_MM = 0.3;
/**
 * Samples this close to any contact point are a support's own tip meeting the
 * surface, as it should: DragonFruit's auto-support tips run up to 2.5 mm.
 */
export const TIP_ALLOWANCE_MM = 3;
/** Sample spacing over the support surface. */
export const GRAZE_SAMPLE_MM = 0.25;
/** Flagged samples closer than this to each other are one graze. */
export const GRAZE_CLUSTER_MM = 1;

/** One place a support passes the model too closely. Plate frame, mm. */
export interface Graze {
    /** The smallest gap found between support and model surface (0: they touch or cross). */
    gap_mm: number;
    /** Where that smallest gap is, on the support. */
    at_plate_mm: [number, number, number];
    /** Its height above the model's base. */
    height_above_base_mm: number;
    /** The lowest and highest flagged points: how far it runs alongside. */
    z_range_mm: [number, number];
    samples: number;
}

export interface ClearanceReport {
    min_clearance_mm: number;
    tip_allowance_mm: number;
    samples_checked: number;
    grazes: Graze[];
}

export interface GrazeOptions {
    minClearanceMm?: number;
    tipAllowanceMm?: number;
    sampleMm?: number;
    clusterMm?: number;
}

/**
 * Points over a triangle's surface no more than `spacing` apart along its two
 * shorter edges: thin slivers (a column's side) get a line of points, not a
 * square grid sized by their length.
 */
function sampleTriangle(ax: number, ay: number, az: number, bx: number, by: number, bz: number,
    cx: number, cy: number, cz: number, spacing: number, visit: (x: number, y: number, z: number) => void): void {
    // Corner opposite the longest edge, so its two edges are the shorter pair.
    const ab = Math.hypot(bx - ax, by - ay, bz - az);
    const bc = Math.hypot(cx - bx, cy - by, cz - bz);
    const ca = Math.hypot(ax - cx, ay - cy, az - cz);
    let o = [ax, ay, az];
    let p = [bx, by, bz];
    let q = [cx, cy, cz];
    if (ab >= bc && ab >= ca) [o, p, q] = [q, p, [ax, ay, az]];
    else if (ca >= ab && ca >= bc) [o, p, q] = [p, [ax, ay, az], q];
    const u = [p[0] - o[0], p[1] - o[1], p[2] - o[2]];
    const v = [q[0] - o[0], q[1] - o[1], q[2] - o[2]];
    const nu = Math.max(1, Math.ceil(Math.hypot(u[0], u[1], u[2]) / spacing));
    const nv = Math.max(1, Math.ceil(Math.hypot(v[0], v[1], v[2]) / spacing));
    for (let i = 0; i <= nu; i++) {
        const s = i / nu;
        for (let j = 0; j <= nv; j++) {
            const t = j / nv;
            if (s + t > 1 + 1e-9) break;
            visit(o[0] + s * u[0] + t * v[0], o[1] + s * u[1] + t * v[1], o[2] + s * u[2] + t * v[2]);
        }
    }
}

/**
 * Near-misses between the support mesh and the model: support surface within
 * `minClearanceMm` of the model's, away from every contact tip and above the
 * model's base. `supportPositions` holds the support and raft triangles (9
 * floats each); `contacts` every live support's contact points.
 */
export function findGrazes(
    model: THREE.BufferGeometry,
    supportPositions: ArrayLike<number>,
    contacts: THREE.Vector3[],
    options: GrazeOptions = {},
): ClearanceReport {
    const minClearance = options.minClearanceMm ?? DEFAULT_MIN_CLEARANCE_MM;
    const tipAllowance = options.tipAllowanceMm ?? TIP_ALLOWANCE_MM;
    const spacing = options.sampleMm ?? GRAZE_SAMPLE_MM;
    const clusterMm = options.clusterMm ?? GRAZE_CLUSTER_MM;
    const report: ClearanceReport = { min_clearance_mm: minClearance, tip_allowance_mm: tipAllowance, samples_checked: 0, grazes: [] };
    if (minClearance <= 0 || supportPositions.length === 0) return report;

    model.computeBoundingBox();
    const modelBox = model.boundingBox!;
    const baseZ = modelBox.min.z;
    const near = modelBox.clone().expandByScalar(minClearance);
    const bvh = new MeshBVH(model);

    // Contacts bucketed by cell, so the tip test looks only at its neighbours.
    const cell = Math.max(tipAllowance, 1e-6);
    const key = (x: number, y: number, z: number) => `${Math.floor(x / cell)},${Math.floor(y / cell)},${Math.floor(z / cell)}`;
    const contactCells = new Map<string, THREE.Vector3[]>();
    for (const c of contacts) {
        const k = key(c.x, c.y, c.z);
        (contactCells.get(k) ?? contactCells.set(k, []).get(k)!).push(c);
    }
    const nearTip = (x: number, y: number, z: number): boolean => {
        const cx = Math.floor(x / cell);
        const cy = Math.floor(y / cell);
        const cz = Math.floor(z / cell);
        for (let dx = -1; dx <= 1; dx++) for (let dy = -1; dy <= 1; dy++) for (let dz = -1; dz <= 1; dz++) {
            for (const c of contactCells.get(`${cx + dx},${cy + dy},${cz + dz}`) ?? []) {
                const ex = c.x - x;
                const ey = c.y - y;
                const ez = c.z - z;
                if (ex * ex + ey * ey + ez * ez <= tipAllowance * tipAllowance) return true;
            }
        }
        return false;
    };

    const point = new THREE.Vector3();
    const target = { point: new THREE.Vector3(), distance: 0, faceIndex: 0 };
    const flagged: { x: number; y: number; z: number; gap: number }[] = [];
    const visit = (x: number, y: number, z: number) => {
        if (z < baseZ || !near.containsPoint(point.set(x, y, z))) return;
        if (nearTip(x, y, z)) return;
        report.samples_checked++;
        const hit = bvh.closestPointToPoint(point, target, 0, minClearance);
        if (hit && hit.distance < minClearance) flagged.push({ x, y, z, gap: hit.distance });
    };
    const box = new THREE.Box3();
    const a = new THREE.Vector3();
    const b = new THREE.Vector3();
    const c = new THREE.Vector3();
    for (let t = 0; t + 8 < supportPositions.length; t += 9) {
        a.set(supportPositions[t], supportPositions[t + 1], supportPositions[t + 2]);
        b.set(supportPositions[t + 3], supportPositions[t + 4], supportPositions[t + 5]);
        c.set(supportPositions[t + 6], supportPositions[t + 7], supportPositions[t + 8]);
        box.makeEmpty().expandByPoint(a).expandByPoint(b).expandByPoint(c);
        if (!box.intersectsBox(near) || box.max.z < baseZ) continue;
        sampleTriangle(a.x, a.y, a.z, b.x, b.y, b.z, c.x, c.y, c.z, spacing, visit);
    }

    // Cluster: flagged samples within `clusterMm` of each other are one graze.
    const parent = flagged.map((_, i) => i);
    const find = (i: number): number => {
        while (parent[i] !== i) i = parent[i] = parent[parent[i]];
        return i;
    };
    const grid = new Map<string, number[]>();
    const gkey = (x: number, y: number, z: number) => `${Math.floor(x / clusterMm)},${Math.floor(y / clusterMm)},${Math.floor(z / clusterMm)}`;
    flagged.forEach((f, i) => {
        const gx = Math.floor(f.x / clusterMm);
        const gy = Math.floor(f.y / clusterMm);
        const gz = Math.floor(f.z / clusterMm);
        for (let dx = -1; dx <= 1; dx++) for (let dy = -1; dy <= 1; dy++) for (let dz = -1; dz <= 1; dz++) {
            for (const j of grid.get(`${gx + dx},${gy + dy},${gz + dz}`) ?? []) {
                const g = flagged[j];
                if ((g.x - f.x) ** 2 + (g.y - f.y) ** 2 + (g.z - f.z) ** 2 <= clusterMm * clusterMm) parent[find(i)] = find(j);
            }
        }
        const k = gkey(f.x, f.y, f.z);
        (grid.get(k) ?? grid.set(k, []).get(k)!).push(i);
    });
    const groups = new Map<number, number[]>();
    flagged.forEach((_, i) => {
        const r = find(i);
        (groups.get(r) ?? groups.set(r, []).get(r)!).push(i);
    });
    for (const members of groups.values()) {
        let best = flagged[members[0]];
        let lo = Infinity;
        let hi = -Infinity;
        for (const i of members) {
            const f = flagged[i];
            if (f.gap < best.gap) best = f;
            lo = Math.min(lo, f.z);
            hi = Math.max(hi, f.z);
        }
        report.grazes.push({
            gap_mm: best.gap,
            at_plate_mm: [best.x, best.y, best.z],
            height_above_base_mm: best.z - baseZ,
            z_range_mm: [lo, hi],
            samples: members.length,
        });
    }
    // Worst first: the smallest gap, then the longest run alongside.
    report.grazes.sort((p, q) => p.gap_mm - q.gap_mm || (q.z_range_mm[1] - q.z_range_mm[0]) - (p.z_range_mm[1] - p.z_range_mm[0]));
    return report;
}

/**
 * Sparse support (NDFM-30): overhanging surface too far from any support
 * contact. Each layer can rest on the one below and still tear: on the 100 mm
 * Chi the bulge of a haunch leaned out over his tail with one contact every
 * 9 mm, and the peel of a large cross-section ripped the edge between them.
 */
/** Flag overhang this many grid spacings (√areaPerSupportMm2) from any contact. */
export const SPARSE_SPACING_FACTOR = 1.25;
/** Sparse regions smaller than this are ignored, mm². */
export const SPARSE_MIN_AREA_MM2 = 2;
export const SPARSE_SAMPLE_MM = 0.5;
export const SPARSE_CLUSTER_MM = 1.5;

export interface SparseRegion {
    /** Overhang area in the region, mm². */
    area_mm2: number;
    /** The furthest any of its points lies from a contact (capped at the search reach). */
    max_gap_mm: number;
    /** Where that is, plate frame. */
    at_plate_mm: [number, number, number];
    centre_plate_mm: [number, number, number];
    height_above_base_mm: number;
    z_range_mm: [number, number];
    samples: number;
}

export interface SparseSupportReport {
    /** Points further than this from every contact count as sparse. */
    limit_mm: number;
    self_support_angle_deg: number;
    min_area_mm2: number;
    samples_checked: number;
    /** All overhang surface checked, mm². */
    overhang_area_mm2: number;
    /** Sum of the reported regions' areas. */
    sparse_area_mm2: number;
    /** Largest first. */
    regions: SparseRegion[];
    /** Regions dropped because they face into a sealed cavity. */
    internal_regions_skipped: number;
}

export interface SparseOptions {
    /** Down-facing surface flatter than this, from horizontal, is overhang. */
    selfSupportAngleDeg: number;
    limitMm: number;
    minAreaMm2?: number;
    sampleMm?: number;
    clusterMm?: number;
    /** Points not to check: inside a keep-out zone, above the cap. */
    skip?: (x: number, y: number, z: number) => boolean;
    /** Points sitting on the plate rather than over it (lift 0): below this z. */
    ignoreBelowZ?: number;
    /** Whether a point on a down-facing surface faces into a sealed cavity. */
    isEnclosed?: (p: THREE.Vector3) => boolean;
}

/** Union-find clusters of points within `mm` of each other (indices into `pts`). */
function clusterPoints(pts: { x: number; y: number; z: number }[], mm: number): number[][] {
    const parent = pts.map((_, i) => i);
    const find = (i: number): number => {
        while (parent[i] !== i) i = parent[i] = parent[parent[i]];
        return i;
    };
    const grid = new Map<string, number[]>();
    pts.forEach((p, i) => {
        const gx = Math.floor(p.x / mm);
        const gy = Math.floor(p.y / mm);
        const gz = Math.floor(p.z / mm);
        for (let dx = -1; dx <= 1; dx++) for (let dy = -1; dy <= 1; dy++) for (let dz = -1; dz <= 1; dz++) {
            for (const j of grid.get(`${gx + dx},${gy + dy},${gz + dz}`) ?? []) {
                const q = pts[j];
                if ((q.x - p.x) ** 2 + (q.y - p.y) ** 2 + (q.z - p.z) ** 2 <= mm * mm) parent[find(i)] = find(j);
            }
        }
        const k = `${gx},${gy},${gz}`;
        (grid.get(k) ?? grid.set(k, []).get(k)!).push(i);
    });
    const groups = new Map<number, number[]>();
    pts.forEach((_, i) => {
        const r = find(i);
        (groups.get(r) ?? groups.set(r, []).get(r)!).push(i);
    });
    return [...groups.values()];
}

/**
 * Overhang surface of the model (`positions`, 9 floats a triangle, outward
 * winding) that lies further than `limitMm` from every support contact.
 */
export function findSparseSupport(
    positions: ArrayLike<number>,
    contacts: THREE.Vector3[],
    baseZ: number,
    options: SparseOptions,
): SparseSupportReport {
    const limit = options.limitMm;
    const minArea = options.minAreaMm2 ?? SPARSE_MIN_AREA_MM2;
    const spacing = options.sampleMm ?? SPARSE_SAMPLE_MM;
    const clusterMm = options.clusterMm ?? SPARSE_CLUSTER_MM;
    const report: SparseSupportReport = {
        limit_mm: limit,
        self_support_angle_deg: options.selfSupportAngleDeg,
        min_area_mm2: minArea,
        samples_checked: 0,
        overhang_area_mm2: 0,
        sparse_area_mm2: 0,
        regions: [],
        internal_regions_skipped: 0,
    };
    // Down-facing and flatter than the self-support angle: the unit normal's z
    // is below -cos(angle).
    const maxNz = -Math.cos((options.selfSupportAngleDeg * Math.PI) / 180);

    // Contacts by cell; the search reaches REACH cells out for the gap figure.
    const REACH = 3;
    const cell = Math.max(limit, 1e-6);
    const cells = new Map<string, THREE.Vector3[]>();
    for (const c of contacts) {
        const k = `${Math.floor(c.x / cell)},${Math.floor(c.y / cell)},${Math.floor(c.z / cell)}`;
        (cells.get(k) ?? cells.set(k, []).get(k)!).push(c);
    }
    const nearest = (x: number, y: number, z: number): number => {
        const cx = Math.floor(x / cell);
        const cy = Math.floor(y / cell);
        const cz = Math.floor(z / cell);
        let best = Infinity;
        for (let r = 0; r <= REACH; r++) {
            // The shell of cells r out (Chebyshev distance).
            for (let dx = -r; dx <= r; dx++) for (let dy = -r; dy <= r; dy++) for (let dz = -r; dz <= r; dz++) {
                if (Math.max(Math.abs(dx), Math.abs(dy), Math.abs(dz)) !== r) continue;
                for (const c of cells.get(`${cx + dx},${cy + dy},${cz + dz}`) ?? []) {
                    best = Math.min(best, Math.hypot(c.x - x, c.y - y, c.z - z));
                }
            }
            // Every contact in a further shell is at least r * cell away.
            if (best <= r * cell) break;
        }
        return Math.min(best, (REACH + 1) * cell);
    };

    const flagged: { x: number; y: number; z: number; gap: number; area: number }[] = [];
    const a = new THREE.Vector3();
    const b = new THREE.Vector3();
    const c = new THREE.Vector3();
    const n = new THREE.Vector3();
    const ab = new THREE.Vector3();
    const ac = new THREE.Vector3();
    for (let t = 0; t + 8 < positions.length; t += 9) {
        a.set(positions[t], positions[t + 1], positions[t + 2]);
        b.set(positions[t + 3], positions[t + 4], positions[t + 5]);
        c.set(positions[t + 6], positions[t + 7], positions[t + 8]);
        ab.subVectors(b, a);
        ac.subVectors(c, a);
        n.crossVectors(ab, ac);
        const twiceArea = n.length();
        if (twiceArea === 0 || n.z / twiceArea >= maxNz) continue;
        const area = twiceArea / 2;
        const pts: [number, number, number][] = [];
        let all = 0;
        sampleTriangle(a.x, a.y, a.z, b.x, b.y, b.z, c.x, c.y, c.z, spacing, (x, y, z) => {
            all++;
            if (options.ignoreBelowZ !== undefined && z < options.ignoreBelowZ) return;
            if (options.skip?.(x, y, z)) return;
            pts.push([x, y, z]);
        });
        if (pts.length === 0) continue;
        // Each sample stands for its share of the whole triangle, so a triangle
        // part-skipped (straddling a zone or the cap) counts only the part kept.
        const weight = area / all;
        report.overhang_area_mm2 += weight * pts.length;
        for (const [x, y, z] of pts) {
            report.samples_checked++;
            const gap = nearest(x, y, z);
            if (gap > limit) flagged.push({ x, y, z, gap, area: weight });
        }
    }

    for (const members of clusterPoints(flagged, clusterMm)) {
        let area = 0;
        let worst = flagged[members[0]];
        let lo = Infinity;
        let hi = -Infinity;
        const centre = new THREE.Vector3();
        for (const i of members) {
            const f = flagged[i];
            area += f.area;
            if (f.gap > worst.gap) worst = f;
            lo = Math.min(lo, f.z);
            hi = Math.max(hi, f.z);
            centre.x += f.x * f.area;
            centre.y += f.y * f.area;
            centre.z += f.z * f.area;
        }
        if (area < minArea) continue;
        centre.divideScalar(area);
        // A cavity's ceiling is overhang nothing can support; leave it out.
        // Probe at the region's sample nearest its centre (the centre itself
        // may lie off the surface).
        let probe = flagged[members[0]];
        for (const i of members) {
            const f = flagged[i];
            if ((f.x - centre.x) ** 2 + (f.y - centre.y) ** 2 + (f.z - centre.z) ** 2
                < (probe.x - centre.x) ** 2 + (probe.y - centre.y) ** 2 + (probe.z - centre.z) ** 2) probe = f;
        }
        if (options.isEnclosed?.(new THREE.Vector3(probe.x, probe.y, probe.z))) {
            report.internal_regions_skipped++;
            continue;
        }
        report.regions.push({
            area_mm2: area,
            max_gap_mm: worst.gap,
            at_plate_mm: [worst.x, worst.y, worst.z],
            centre_plate_mm: [centre.x, centre.y, centre.z],
            height_above_base_mm: centre.z - baseZ,
            z_range_mm: [lo, hi],
            samples: members.length,
        });
    }
    report.regions.sort((p, q) => q.area_mm2 - p.area_mm2);
    report.sparse_area_mm2 = report.regions.reduce((s, r) => s + r.area_mm2, 0);
    return report;
}
