/**
 * Auto-support an STL and slice it, supports and raft included, headlessly.
 *
 * SPDX-License-Identifier: AGPL-3.0-or-later
 *
 * Upstream has the pieces but no command that joins them: `scripts/bench-auto-
 * supports.ts` runs the placement pipeline under Node, and `scene slice` slices
 * a scene without its supports. This script runs the app's own code end to end,
 * importing DragonFruit's modules read-only:
 *
 *   STL -> lift off the plate -> voxel islands (the app's scanline worker, through
 *   the bench's in-process shim, joined by `voxel-islands.ts`, a bounded
 *   `detectVoxelIslands`) -> `runAutoPlaceRequest` (the worker's own
 *   entry point) -> `commitAutoPlacePlan` into the support store ->
 *   `buildSupportAndRaftWorldTriangles` (the app's export path) -> model
 *   triangles first, then supports and raft, with `modelTriangleCount` at the
 *   split -> the job `scene slice` assembles for the printer profile ->
 *   `dragonfruit-cli slice run --job`.
 *
 * Run by `nakomis_dragonfruit_mcp.cli.run_ts` with DragonFruit's tsx, its
 * tsconfig (for the `@/` alias) and `NODE_PATH` at its node_modules (for bare
 * imports such as `three`), from the DragonFruit checkout.
 *
 * The model is placed in world space by moving its vertices: centred on the
 * plate in X/Y, its lowest point at the lift height. The support pipeline and
 * the slicer then both see an identity transform, so there is no model/world
 * space to get wrong between them.
 *
 * Islands: the app merges three families. The voxel family runs here through
 * the bench's in-process worker, with the candidates held as runs rather than
 * upstream's one Set entry a voxel (which a large part overflows: NDFM-23);
 * the overhang family is a Tauri command over Rust, which
 * `dragonfruit-mcp-tools overhangs` runs for us (`--tools`); mesh minima (also
 * Tauri) are not run, so a model may get fewer supports at
 * isolated low points than the GUI would give it.
 *
 * Usage:
 *   tsx autosupport-slice.ts --stl <in.stl> [--out <out.ctb>] --cli <dragonfruit-cli> [--tools <dragonfruit-mcp-tools>]
 *     --printer-json <profile.json> [--material <material.json>] [--layer-height N]
 *     [--aa-preset sharp|balanced|smooth|raw] [--lift-mm 7] [--density 1] [--raft solid|line|off]
 *     [--settings <json>] [--coarse-islands] [--no-overhangs] [--px-mm 0.05] [--supported-stl <out.stl>] [--plate-stl <out.stl>] [--job-dir <dir>]
 *     [--keep-out <holes.json>] [--max-support-height <mm>] [--overhang-max-height <mm>] [--min-clearance <mm>] [--no-auto-densify]
 *     [--steep-contact-deg 60] [--keep-steep-contacts]
 *     [--support-internal-islands] [--probe-points <json>] [--no-slice] [--verbose]
 *
 * `--supported-stl` writes model, supports and raft as sliced; `--plate-stl` the
 * model alone in the same plate frame (centred, lifted), to line up with a layer.
 * `--job-dir` keeps the engine's input (positions.bin, job.json) for inspection;
 * with `--no-slice` it writes them without slicing. `--coarse-islands` trades
 * island detail for speed (the bench's resolution rather than the app panel's).
 * `--no-overhangs` skips the overhang family, leaving only the voxel islands:
 * for self-supporting shapes such as lattices, whose saddles read as overhangs.
 * The overhang angle is `--settings '{"overhangSelfSupportAngleDeg": N}'`.
 * Existing output files are overwritten.
 *
 * `--keep-out` names a JSON file of drilled holes in the STL's own frame
 * (`{"holes": [{x, y, z, radius_mm, direction, length_mm, ...}]}`, as
 * `drill_holes` records them; `length_mm` runs from the start point to where the
 * bore leaves the skin). Each becomes a keep-out cylinder (radius + 1 mm) round
 * the hole's axis, anchored on that exit: from 4 mm back along the bore to 3 mm
 * past the exit. DragonFruit's support blockers (the "nogo" paint that candidate
 * generation and the grid lattice honour) are set on every model triangle
 * touching a cylinder, so no contact lands there; then any support whose
 * contact or shaft still enters one (a trunk leaning in from outside) is removed
 * with `removeSupportEntity`, which takes its dependants with it, and shafts
 * left holding nothing are removed in turn. The summary's `keep_out` section
 * counts all of it, per hole.
 *
 * `--max-support-height` caps supports at that height above the model's own
 * base (NDFM-28): islands above it are not supported, the model surface above
 * it is blocked like a keep-out zone, and any support whose contact or shaft
 * still rises above it is removed the same way. The summary's `height_cap`
 * section counts it.
 *
 * `--overhang-max-height` limits the overhang family alone to a band that high
 * above the model's base (NDFM-22), for self-supporting lattices whose saddles
 * read as overhangs: the base gets its grid and the lattice above gets none,
 * while the voxel family's islands are supported wherever they are. Overhang
 * regions are cut at the band's top before they become islands (the scanner
 * may return a whole lattice as one region), so the grid samples only the
 * surface in the band. Nothing is blocked or removed: a voxel island above the
 * band keeps its supports, shafts and all. The summary's `overhang_band`
 * section counts the regions cut and dropped, and checks the result: the
 * highest overhang contact, and any more than 0.5 mm over the band's top (a
 * triangle straddling the line can carry one a little over it), counted and
 * warned about.
 *
 * `--min-clearance` (default 0.3 mm; 0 turns it off) is the clearance check:
 * support surface that comes closer than that to the model, away from every
 * contact tip, is reported in `clearance.grazes` and warned about, worst first.
 * In clear resin such a gap cures shut and the support prints fused to the
 * model. Nothing is removed for it.
 *
 * Sparse support (NDFM-30): after placement, overhang more than 1.25 grid
 * spacings from every contact is reported in `sparse_support`. Unless
 * `--no-auto-densify`, placement then runs again at 1.5x and 2x `--density`,
 * and the run with the least sparse area is restored from its snapshot of the
 * support store before export, so the supports sliced are the ones checked.
 * `density` in the summary lists every try.
 *
 * Steep-wall contacts (NDFM-31): a support whose every tip meets surface
 * steeper than `--steep-contact-deg` (default 60° from horizontal, averaged
 * over 1.5 mm round the tip), with nothing within that 1.5 mm flatter than the
 * self-support angle (no overhang it could be holding), is removed with its dependants, unless it is all
 * that holds an island, when the least steep is kept. `--keep-steep-contacts`
 * only reports them. The summary's `steep_contacts` lists both.
 *
 * Islands whose contact faces a sealed cavity are skipped before placement
 * (hollow interiors are not supported in normal resin practice, and the only
 * way to reach them is a trunk up through the drain hole) unless
 * `--support-internal-islands`. `--probe-points` takes `[[x, y, z], ...]` in the
 * plate frame, prints which keep-out zones contain each, and exits.
 *
 * Prints one JSON summary on stdout; everything else goes to stderr.
 */

import { execFileSync } from 'node:child_process';
import { existsSync, mkdirSync, mkdtempSync, readdirSync, readFileSync, rmSync, statSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { basename, dirname, extname, join, resolve } from 'node:path';
import { pathToFileURL } from 'node:url';
import * as THREE from 'three';
import { STLLoader } from 'three/examples/jsm/loaders/STLLoader.js';

import { initializeBVH, accelerateGeometry } from '@/utils/bvh';
import { createDefaultSettings } from '@/supports/Settings/types';
import { setSettings } from '@/supports/Settings/state';
import { clearHistory } from '@/history/historyStore';
import { cloneSupportState, getSnapshot, removeSupportEntity, resetKickstandsInState, resetStore, setSnapshot } from '@/supports/state';
import { registerSupportHistoryHandlers } from '@/supports/history/useSupportHistoryHandlers';
import { setModelMesh } from '@/supports/autoSupport/meshStore';
import { deleteSupportBlockers, setSupportBlockedTriangles } from '@/supports/autoSupport/supportBlockers';
import { commitAutoPlacePlan } from '@/supports/autoSupport/autoPlace';
import { resolvedSizingBandsForRun } from '@/supports/autoSupport/parameterSizing';
import {
    modelMeshKey,
    runAutoPlaceRequest,
    serializeIsland,
    serializeModelMesh,
} from '@/supports/autoSupport/autoPlace.worker.shared';
import type { AutoSupportSettings } from '@/supports/autoSupport/settings';
import { SUPPORT_STATE_TYPES } from '@/supports/supportTypeRegistry';
import { DEFAULT_LIFT_DISTANCE_MM } from '@/features/transform/liftDefaults';
import { DEFAULT_RAFT_SETTINGS } from '@/supports/Rafts/Crenelated/RaftDefaults';
import { setRaftSettings } from '@/supports/Rafts/Crenelated/RaftState';
import {
    getActiveMaterialProfile,
    getActivePrinterProfile,
    setActiveMaterialProfile,
    setActivePrinterProfile,
} from '@/features/profiles/profileStore';
import { buildSupportAndRaftWorldTriangles } from '@/features/slicing/rasterLayerZipExport';

import {
    clipOverhangRegion,
    DEFAULT_MIN_CLEARANCE_MM,
    findGrazes,
    findSparseSupport,
    HeightCapZone,
    KeepOutZone,
    parseHoles,
    positionsInFaceOrder,
    SPARSE_SPACING_FACTOR,
    STEEP_CONTACT_DEG,
    STEEP_RADIUS_MM,
    SurfaceTilt,
    triangleIndicesInZones,
    type ClearanceReport,
    type SparseSupportReport,
    type Zone,
} from './support-zones';
import { resolveSliceLayerCount, resolveSliceRasterSettings } from '@/features/slicing/sliceJobAssembly';
import { resolveSliceJobAntiAliasing } from '@/features/slicing/sliceAntiAliasing';
import type { PlateFootprintSource } from '@/supports/Rafts/Crenelated/geometry/modelPlateFootprint';
import { disposeEventLoopChannel } from '@/utils/yieldToEventLoop';
import type { RaftSettings } from '@/supports/Rafts/Crenelated/RaftTypes';
import { mergeOverhangRegions, overhangRegionToIsland } from '@/volumeAnalysis/Islands/useIslands';
import { classifyIntersection } from '@/volumeAnalysis/Islands/intersection';
import type { DetectedIsland, OverhangRegion, OverhangScan } from '@/volumeAnalysis/Islands/types';

import { detectVoxelIslandsBounded, type WorkerLike } from './voxel-islands';

/** The footprint resolution the app's island panel asks `scan_overhangs` for (useIslands.ts). */
const OVERHANG_FOOTPRINT_PX_MM = 0.25;

/**
 * Voxel island resolution. `panel` is what the app's Islands panel starts from
 * (useIslands.ts: pxMm 0.05, support buffer 0.25 mm); `coarse` is the bench's
 * faster default (0.1 / 0.6), which misses smaller and shallower islands.
 */
const ISLAND_DETECT = {
    panel: { pxMm: 0.05, supportBufferMm: 0.25 },
    coarse: { pxMm: 0.1, supportBufferMm: 0.6 },
} as const;

/**
 * How far over the overhang band's top a contact may sit before the check
 * counts it: a region keeps a triangle that straddles the line, and its
 * contacts can land on the part above (NDFM-22).
 */
const OVERHANG_BAND_SLACK_MM = 0.5;

/** Our temp dirs share a prefix, so a run can clear what a killed run left behind. */
const TEMP_PREFIX = 'ndfm-autosupport-';
/** Older than any run could be (the Python side times out at 45 minutes). */
const STALE_TEMP_MS = 2 * 60 * 60 * 1000;

type RaftMode = 'solid' | 'line' | 'off';

interface Options {
    stl: string;
    out: string | null;
    cli: string | null;
    tools: string | null;
    printerJson: string | null;
    material: string | null;
    layerHeight: string | null;
    aaPreset: string | null;
    liftMm: number;
    density: number;
    raft: RaftMode;
    settings: Partial<AutoSupportSettings>;
    pxMm: number | null;
    coarseIslands: boolean;
    overhangs: boolean;
    supportedStl: string | null;
    plateStl: string | null;
    jobDir: string | null;
    keepOut: string | null;
    /** Supports only up to this height above the model's base, mm (NDFM-28). */
    maxSupportHeightMm: number | null;
    /** Overhang-family contacts only up to this height above the model's base, mm (NDFM-22). */
    overhangMaxHeightMm: number | null;
    /** Report supports passing closer than this to the model; 0 turns the check off. */
    minClearanceMm: number;
    /** On finding sparsely supported overhang, retry placement at 1.5x and 2x density (NDFM-30). */
    autoDensify: boolean;
    /** Contacts on surface steeper than this, from horizontal, are steep-wall contacts (NDFM-31). */
    steepContactDeg: number;
    /** Remove steep-wall contacts (keeping one where it is an island's only support). */
    avoidSteepContacts: boolean;
    supportInternalIslands: boolean;
    probePoints: number[][] | null;
    slice: boolean;
    verbose: boolean;
}

function parseArgs(argv: string[]): Options {
    const options: Options = {
        stl: '',
        out: null,
        cli: null,
        tools: null,
        printerJson: null,
        material: null,
        layerHeight: null,
        aaPreset: null,
        liftMm: DEFAULT_LIFT_DISTANCE_MM,
        density: 1,
        raft: 'solid',
        settings: {},
        pxMm: null,
        coarseIslands: false,
        overhangs: true,
        supportedStl: null,
        plateStl: null,
        jobDir: null,
        keepOut: null,
        maxSupportHeightMm: null,
        overhangMaxHeightMm: null,
        minClearanceMm: DEFAULT_MIN_CLEARANCE_MM,
        autoDensify: true,
        steepContactDeg: STEEP_CONTACT_DEG,
        avoidSteepContacts: true,
        supportInternalIslands: false,
        probePoints: null,
        slice: true,
        verbose: false,
    };
    for (let i = 0; i < argv.length; i++) {
        const arg = argv[i];
        const value = (): string => {
            const next = argv[++i];
            if (next === undefined) throw new Error(`${arg} needs a value`);
            return next;
        };
        const number = (): number => {
            const raw = value();
            const parsed = Number(raw);
            if (!Number.isFinite(parsed)) throw new Error(`${arg} needs a number, got "${raw}"`);
            return parsed;
        };
        if (arg === '--stl') options.stl = resolve(value());
        else if (arg === '--out') options.out = resolve(value());
        else if (arg === '--cli') options.cli = resolve(value());
        else if (arg === '--tools') options.tools = resolve(value());
        else if (arg === '--printer-json') options.printerJson = resolve(value());
        else if (arg === '--material') options.material = resolve(value());
        else if (arg === '--layer-height') options.layerHeight = String(number());
        else if (arg === '--aa-preset') options.aaPreset = value();
        else if (arg === '--lift-mm') options.liftMm = number();
        else if (arg === '--density') options.density = number();
        else if (arg === '--raft') options.raft = value() as RaftMode;
        else if (arg === '--settings') options.settings = JSON.parse(value()) as Partial<AutoSupportSettings>;
        else if (arg === '--px-mm') options.pxMm = number();
        else if (arg === '--coarse-islands') options.coarseIslands = true;
        else if (arg === '--no-overhangs') options.overhangs = false;
        else if (arg === '--supported-stl') options.supportedStl = resolve(value());
        else if (arg === '--plate-stl') options.plateStl = resolve(value());
        else if (arg === '--job-dir') options.jobDir = resolve(value());
        else if (arg === '--keep-out') options.keepOut = resolve(value());
        else if (arg === '--max-support-height') options.maxSupportHeightMm = number();
        else if (arg === '--overhang-max-height') options.overhangMaxHeightMm = number();
        else if (arg === '--min-clearance') options.minClearanceMm = number();
        else if (arg === '--no-auto-densify') options.autoDensify = false;
        else if (arg === '--steep-contact-deg') options.steepContactDeg = number();
        else if (arg === '--keep-steep-contacts') options.avoidSteepContacts = false;
        else if (arg === '--support-internal-islands') options.supportInternalIslands = true;
        else if (arg === '--probe-points') options.probePoints = JSON.parse(value()) as number[][];
        else if (arg === '--no-slice') options.slice = false;
        else if (arg === '--verbose') options.verbose = true;
        else throw new Error(`unknown argument "${arg}"`);
    }
    if (!options.stl) throw new Error('--stl is required');
    if (!options.printerJson) throw new Error('--printer-json is required (a preset reference, custom profile or bundle)');
    if (options.slice && !options.cli) throw new Error('--cli is required unless --no-slice');
    if (!['solid', 'line', 'off'].includes(options.raft)) throw new Error('--raft must be solid, line or off');
    if (options.liftMm < 0) throw new Error('--lift-mm must not be negative');
    if (options.density <= 0) throw new Error('--density must be positive');
    if (options.maxSupportHeightMm !== null && options.maxSupportHeightMm <= 0) throw new Error('--max-support-height must be positive');
    if (options.overhangMaxHeightMm !== null && options.overhangMaxHeightMm <= 0) throw new Error('--overhang-max-height must be positive');
    if (options.minClearanceMm < 0) throw new Error('--min-clearance must not be negative');
    if (!(options.steepContactDeg > 0 && options.steepContactDeg < 180)) throw new Error('--steep-contact-deg must be between 0 and 180');
    return options;
}

/**
 * The pipeline logs freely through console.log, and stdout carries our JSON.
 * Its chatter goes to stderr with --verbose and nowhere otherwise.
 */
function quietPipelineLogs(verbose: boolean): void {
    const toStderr = (...args: unknown[]) => {
        if (verbose) process.stderr.write(`${args.map((a) => (typeof a === 'string' ? a : JSON.stringify(a))).join(' ')}\n`);
    };
    console.log = toStderr;
    console.info = toStderr;
    console.warn = toStderr;
    console.debug = toStderr;
}

/** Scripts outside `src/` are not under the `@/` alias, so load them by path. */
async function importDragonFruitScript<T>(relativePath: string): Promise<T> {
    return (await import(pathToFileURL(join(process.cwd(), relativePath)).href)) as T;
}

/**
 * The app's `scan_overhangs`, through `dragonfruit-mcp-tools overhangs`: its
 * regions, which the app's own `overhangRegionToIsland` maps to islands.
 */
function scanOverhangs(tools: string, positions: Float32Array, angleDeg: number, hasRaft: boolean): OverhangRegion[] {
    const tmp = mkdtempSync(join(tmpdir(), TEMP_PREFIX));
    try {
        const input = join(tmp, 'positions.bin');
        writeFileSync(input, Buffer.from(positions.buffer, positions.byteOffset, positions.byteLength));
        const args = ['overhangs', input, '--angle', String(angleDeg), '--px-mm', String(OVERHANG_FOOTPRINT_PX_MM)];
        if (hasRaft) args.push('--has-raft');
        const stdout = execFileSync(tools, args, { encoding: 'utf-8', maxBuffer: 512 * 1024 * 1024 });
        const scan = JSON.parse(stdout) as OverhangScan;
        return scan.regions;
    } finally {
        rmSync(tmp, { recursive: true, force: true });
    }
}

/** Clear temp dirs a killed run left behind (only ours, and only stale ones). */
function pruneStaleTempDirs(): void {
    try {
        for (const name of readdirSync(tmpdir())) {
            if (!name.startsWith(TEMP_PREFIX)) continue;
            const path = join(tmpdir(), name);
            if (Date.now() - statSync(path).mtimeMs > STALE_TEMP_MS) rmSync(path, { recursive: true, force: true });
        }
    } catch {
        // Best effort: a dir another run is removing is not our problem.
    }
}

/** Create the parent directory, and note whether the file was already there. */
function prepareOutput(path: string): boolean {
    mkdirSync(dirname(path), { recursive: true });
    return existsSync(path);
}

/** Centre the model on the plate in X/Y and put its lowest point at `liftMm`. */
function placeOnPlate(geometry: THREE.BufferGeometry, liftMm: number): THREE.Vector3 {
    geometry.computeBoundingBox();
    const box = geometry.boundingBox!;
    const shift = new THREE.Vector3(
        -(box.min.x + box.max.x) / 2,
        -(box.min.y + box.max.y) / 2,
        liftMm - box.min.z,
    );
    geometry.translate(shift.x, shift.y, shift.z);
    geometry.computeBoundingBox();
    return shift;
}

/** Every entity in the committed store, and its contacts, per registry type. */
function countCommitted(): { byType: Record<string, number>; contacts: number; roots: number } {
    const state = getSnapshot() as unknown as Record<string, Record<string, Record<string, unknown>>>;
    const byType: Record<string, number> = {};
    let contacts = 0;
    for (const descriptor of SUPPORT_STATE_TYPES) {
        const entities = Object.values(state[descriptor.location.key] ?? {});
        if (entities.length > 0) byType[descriptor.id] = entities.length;
        for (const entity of entities) {
            for (const field of descriptor.contactFields) if (entity[field]) contacts++;
        }
    }
    return { byType, contacts, roots: Object.keys(state.roots ?? {}).length };
}

/** Contacts in a saved support state (an attempt's snapshot). */
function countContacts(snapshot: ReturnType<typeof getSnapshot>): number {
    const state = snapshot as unknown as Record<string, Record<string, Record<string, unknown>>>;
    let contacts = 0;
    for (const descriptor of SUPPORT_STATE_TYPES) {
        for (const entity of Object.values(state[descriptor.location.key] ?? {})) {
            for (const field of descriptor.contactFields) if (entity[field]) contacts++;
        }
    }
    return contacts;
}

/** Directions probed from a point to tell a cavity from open air: the 26 neighbours of a cube. */
const ENCLOSURE_DIRECTIONS: THREE.Vector3[] = [];
for (const dx of [-1, 0, 1]) for (const dy of [-1, 0, 1]) for (const dz of [-1, 0, 1]) {
    if (dx || dy || dz) ENCLOSURE_DIRECTIONS.push(new THREE.Vector3(dx, dy, dz).normalize());
}

/**
 * Whether an island's contact is on a surface facing into a closed cavity: a
 * point just below the contact (islands are overhangs, so the air is below)
 * from which every one of 26 rays meets the model. Open air, even under a deep
 * overhang, lets at least one ray escape; a sealed hollow does not. (Ray parity
 * cannot tell them apart: a cavity point and an outside point are both "even".)
 * A drilled hole is a leak, but a small one: a few of the 26 rays could pass
 * through it, so a point in a drained cavity can read as open only if it sits
 * right in line with the hole.
 */
function isInternalContact(mesh: THREE.Mesh, contact: { x: number; y: number; z: number }): boolean {
    const raycaster = new THREE.Raycaster();
    const origin = new THREE.Vector3(contact.x, contact.y, contact.z - 0.3);
    for (const direction of ENCLOSURE_DIRECTIONS) {
        raycaster.set(origin, direction);
        if (raycaster.intersectObject(mesh, false).length === 0) return false;
    }
    return true;
}

interface SupportProbe {
    id: string;
    contacts: THREE.Vector3[];
    runs: [THREE.Vector3, THREE.Vector3][];
}

/**
 * Every committed support's contact points and shaft runs, in the plate frame
 * (the store is already there). A shaft starts at its root's plate position, or
 * the knot it hangs from; runs through its segments' joints; and ends at its
 * contact. Braces are the straight run between their two knots.
 */
function probeCommittedSupports(): SupportProbe[] {
    type P = { x: number; y: number; z: number };
    type Seg = { bottomJoint?: { pos: P }; topJoint?: { pos: P } };
    const state = getSnapshot() as unknown as Record<string, Record<string, Record<string, unknown>>>;
    const vec = (p: P) => new THREE.Vector3(p.x, p.y, p.z);
    const rootPos = (id: unknown): THREE.Vector3 | null => {
        const root = state.roots?.[id as string] as { transform?: { pos?: P } } | undefined;
        return root?.transform?.pos ? vec(root.transform.pos) : null;
    };
    const knotPos = (id: unknown): THREE.Vector3 | null => {
        const knot = state.knots?.[id as string] as { pos?: P } | undefined;
        return knot?.pos ? vec(knot.pos) : null;
    };
    const probes: SupportProbe[] = [];
    for (const descriptor of SUPPORT_STATE_TYPES) {
        for (const [id, entity] of Object.entries(state[descriptor.location.key] ?? {})) {
            const probe: SupportProbe = { id, contacts: [], runs: [] };
            for (const field of descriptor.contactFields) {
                const cone = entity[field] as { pos?: P } | undefined;
                if (cone?.pos) probe.contacts.push(vec(cone.pos));
            }
            const start = rootPos(entity.rootId) ?? knotPos(entity.parentKnotId);
            let previous = start;
            const segments = (entity.segments ?? []) as Seg[];
            segments.forEach((seg, i) => {
                const bottom = seg.bottomJoint ? vec(seg.bottomJoint.pos) : previous;
                const top = seg.topJoint ? vec(seg.topJoint.pos) : (i === segments.length - 1 ? probe.contacts[0] ?? null : null);
                if (bottom && top) probe.runs.push([bottom, top]);
                previous = top ?? previous;
            });
            // A shaft-less support (a leaf) is a straight run from its knot to its contact.
            if (segments.length === 0 && start && probe.contacts[0]) probe.runs.push([start, probe.contacts[0]]);
            const a = knotPos(entity.startKnotId);
            const b = knotPos(entity.endKnotId);
            if (a && b) probe.runs.push([a, b]);
            probes.push(probe);
        }
    }
    return probes;
}

/**
 * The contact heights of the supports the overhang family placed: its grid
 * (origin `overhang`) and its single-candidate regions (`standalone`, autoPlace's
 * ORIGIN_BY_SOURCE).
 */
function overhangContactHeights(): number[] {
    const state = getSnapshot() as unknown as Record<string, Record<string, Record<string, unknown>>>;
    const heights: number[] = [];
    for (const descriptor of SUPPORT_STATE_TYPES) {
        for (const entity of Object.values(state[descriptor.location.key] ?? {})) {
            if (entity.origin !== 'overhang' && entity.origin !== 'standalone') continue;
            for (const field of descriptor.contactFields) {
                const cone = entity[field] as { pos?: { z: number } } | undefined;
                if (cone?.pos) heights.push(cone.pos.z);
            }
        }
    }
    return heights;
}

/** Every support entity in the store, of any type. */
function totalEntities(): number {
    return Object.values(countCommitted().byType).reduce((sum, n) => sum + n, 0);
}

/**
 * Shafts that hold nothing: a trunk, branch or stick with no contact of its own
 * and nothing hanging off its knots (no hosted support, no brace end).
 */
function findDanglingShafts(): string[] {
    const state = getSnapshot() as unknown as Record<string, Record<string, Record<string, unknown>>>;
    const knotHost = (knotId: unknown): string | null => {
        const knot = state.knots?.[knotId as string] as { parentShaftId?: string } | undefined;
        return knot?.parentShaftId ?? null;
    };
    const hosted = new Set<string>();
    for (const descriptor of SUPPORT_STATE_TYPES) {
        for (const entity of Object.values(state[descriptor.location.key] ?? {})) {
            for (const field of ['parentKnotId', 'startKnotId', 'endKnotId']) {
                const host = entity[field] ? knotHost(entity[field]) : null;
                if (host) hosted.add(host);
            }
        }
    }
    const dangling: string[] = [];
    for (const descriptor of SUPPORT_STATE_TYPES) {
        if (descriptor.contactFields.length === 0) continue; // braces, stumps and the like carry no shaft of their own
        for (const [id, entity] of Object.entries(state[descriptor.location.key] ?? {})) {
            const hasShaft = Array.isArray(entity.segments) && entity.segments.length > 0;
            const hasContact = descriptor.contactFields.some((field) => entity[field]);
            if (hasShaft && !hasContact && !hosted.has(id)) dangling.push(id);
        }
    }
    return dangling;
}

function writeBinaryStl(path: string, positions: Float32Array): void {
    const triangles = positions.length / 9;
    const buffer = Buffer.alloc(84 + triangles * 50);
    buffer.write('nakomis-dragonfruit-mcp autosupport-slice', 0, 'ascii');
    buffer.writeUInt32LE(triangles, 80);
    let offset = 84;
    for (let t = 0; t < triangles; t++) {
        offset += 12; // zero normal: readers recompute it from the winding
        for (let k = 0; k < 9; k++) {
            buffer.writeFloatLE(positions[t * 9 + k], offset);
            offset += 4;
        }
        offset += 2;
    }
    writeFileSync(path, buffer);
}

function maxZ(positions: Float32Array): number {
    let top = 0;
    for (let i = 2; i < positions.length; i += 3) if (positions[i] > top) top = positions[i];
    return top;
}

async function main(): Promise<void> {
    const options = parseArgs(process.argv.slice(2));
    const startupMs = Math.round(process.uptime() * 1000);
    quietPipelineLogs(options.verbose);
    pruneStaleTempDirs();
    const timings: Record<string, number> = { startup_ms: startupMs };
    const warnings: string[] = [];
    const time = async <T>(label: string, run: () => T | Promise<T>): Promise<T> => {
        process.stderr.write(`autosupport-slice: ${label.replace(/_ms$/, '')}...\n`);
        const started = performance.now();
        try {
            return await run();
        } finally {
            // Summed: placement and the sparse check run once per density tried.
            timings[label] = (timings[label] ?? 0) + Math.round(performance.now() - started);
        }
    };

    // The printer first: its material's layer height drives the island scan, and
    // the support export reads the *active* profiles for tip penetration (with
    // none active it silently uses 0).
    type SliceJobModule = typeof import('../vendor/dragonfruit/scripts/cli/sceneSliceJob');
    type SceneSliceGeometry = import('../vendor/dragonfruit/scripts/cli/sceneSliceJob').SceneSliceGeometry;
    const sliceJob = await time('profiles_ms', () => importDragonFruitScript<SliceJobModule>('scripts/cli/sceneSliceJob.ts'));
    // As `scene slice` does: the parsed printer and material JSON, through the
    // profile store the way the app adds them.
    const readJson = (path: string): unknown => JSON.parse(readFileSync(path, 'utf-8'));
    const job = sliceJob.resolveSceneSliceJob({
        printer: readJson(options.printerJson!),
        material: options.material ? readJson(options.material) : undefined,
        layerHeight: options.layerHeight ?? undefined,
        aaPreset: (options.aaPreset ?? undefined) as Parameters<typeof sliceJob.resolveSceneSliceJob>[0]['aaPreset'],
    });
    if (!job.printer || !job.material) throw new Error(`the printer profile in ${options.printerJson} did not resolve`);
    // The printer decides the format: without --out the print goes beside the
    // STL, named for it; an --out naming another format is refused rather than
    // written as a file whose extension lies about its contents.
    const format = `.${job.printer.display.outputFormat.replace(/^\./, '').toLowerCase()}`;
    if (!options.out) options.out = join(dirname(options.stl), `${basename(options.stl).replace(/\.stl$/i, '')}-supported${format}`);
    if (options.slice && extname(options.out).toLowerCase() !== format) {
        throw new Error(`--out ${options.out}: printer '${job.printer.name}' writes ${format} files, so the output must end in ${format}`);
    }
    setActivePrinterProfile(job.printer.id);
    setActiveMaterialProfile(job.material.id);
    // The support export reads these, and quietly uses no tip penetration without them.
    if (getActivePrinterProfile()?.id !== job.printer.id || getActiveMaterialProfile()?.id !== job.material.id) {
        throw new Error('the profile store did not take the printer and material as active');
    }
    const layerHeightMm = job.material.layerHeightMm;

    // Load and place the model.
    const geometry = await time('load_ms', () => {
        const bytes = readFileSync(options.stl);
        const loaded = new STLLoader().parse(
            bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength) as ArrayBuffer,
        );
        loaded.deleteAttribute('normal');
        return loaded;
    });
    const plateShift = placeOnPlate(geometry, options.liftMm);
    geometry.computeVertexNormals();
    // The BVH reorders the model's triangles (it gives the STL's unindexed
    // triangles an index and sorts it), and placement, in the worker, builds the
    // same order again from the index we send it: its raycasts' face indices
    // count triangles in that order. So build it here, before anything names a
    // triangle, and hand everything that does (the overhang scan's region
    // triangles, the support blockers) the triangles in that order. Without it
    // the overhang grid sampled surface on the wrong triangles, landing contacts
    // anywhere up a lattice, and the blockers blocked the wrong ones (NDFM-22).
    initializeBVH();
    accelerateGeometry(geometry);
    const facePositions = positionsInFaceOrder(geometry);
    const box = geometry.boundingBox!;
    const build = job.printer.buildVolumeMm;
    if (box.max.x - box.min.x > build.width || box.max.y - box.min.y > build.depth) {
        warnings.push(`the model (${(box.max.x - box.min.x).toFixed(1)} x ${(box.max.y - box.min.y).toFixed(1)} mm) is larger than the plate (${build.width} x ${build.depth} mm)`);
    }
    // Z, before the slow part: supports and raft never rise above the model,
    // so its top is the print's. The engine would clamp a taller print to the
    // build height (resolveSliceLayerCount), silently cutting off the top.
    const buildHeightMm = Number(build.height) || 0;
    if (box.max.z > buildHeightMm) {
        throw new Error(
            `the print would be ${box.max.z.toFixed(2)} mm tall (model ${(box.max.z - box.min.z).toFixed(2)} mm + lift ${options.liftMm} mm), `
            + `but '${job.printer.name}' builds only ${buildHeightMm} mm: lower the lift or scale the model down`,
        );
    }
    if (options.liftMm === 0) {
        warnings.push('lift 0: the model sits on the plate, so only overhangs above its base are supported');
    }

    // Keep-out zones: the holes come in the STL's frame, the model now sits in the plate's.
    const zones = options.keepOut ? parseHoles(options.keepOut).map((spec, i) => new KeepOutZone(i, spec, plateShift)) : [];
    // The height cap is measured from the model's own base, whatever the lift.
    const capZone = options.maxSupportHeightMm !== null ? new HeightCapZone(box.min.z + options.maxSupportHeightMm) : null;
    const allZones: Zone[] = capZone ? [...zones, capZone] : zones;
    if (options.probePoints) {
        process.stdout.write(`${JSON.stringify({
            zones: zones.map((zone) => ({
                hole: zone.index,
                exit_plate_mm: zone.exit.toArray(),
                t_range_mm: [zone.tMin, zone.tMax],
                radius_mm: zone.radius,
                contains: options.probePoints!.map(([x, y, z]) => zone.contains(x, y, z)),
            })),
        })}\n`);
        return;
    }

    // Islands, the voxel family first, as the bench detects them.
    type IslandScanModule = typeof import('../vendor/dragonfruit/scripts/bench-island-scan');
    const islandScan = await importDragonFruitScript<IslandScanModule>('scripts/bench-island-scan.ts');
    await islandScan.installInProcessWorkers();
    const InProcessWorker = (globalThis as unknown as { Worker: new () => WorkerLike }).Worker;
    const detect = options.coarseIslands ? ISLAND_DETECT.coarse : ISLAND_DETECT.panel;
    const voxelIslands = await time('islands_ms', () => detectVoxelIslandsBounded(
        () => new InProcessWorker(),
        geometry.getAttribute('position').array as Float32Array,
        box.clone(),
        layerHeightMm,
        {
            pxMm: options.pxMm ?? detect.pxMm,
            supportBufferMm: detect.supportBufferMm,
            connectivity: islandScan.DEFAULT_DETECT_OPTIONS.connectivity,
            minAreaMm2: islandScan.DEFAULT_DETECT_OPTIONS.minAreaMm2,
        },
    ));

    // Then the overhang family, which is what puts a density grid under shallow
    // slopes (the voxel detector only sees surfaces flatter than ~11°). The app
    // runs it as a Tauri command; we run the same Rust through our own tool.
    // Mesh minima (the third family) are not available headless.
    const selfSupportAngleDeg = options.settings.overhangSelfSupportAngleDeg
        ?? createDefaultSettings().autoSupport.overhangSelfSupportAngleDeg;
    let overhangRegions: OverhangRegion[] | null = null;
    if (!options.overhangs) {
        // Asked for: self-supporting shapes (lattices) whose saddles read as overhangs.
    } else if (options.tools) {
        overhangRegions = await time('overhangs_ms', () => scanOverhangs(
            options.tools!,
            facePositions,
            selfSupportAngleDeg,
            options.raft !== 'off',
        ));
    } else {
        warnings.push('no --tools: overhang regions were not scanned, so shallow slopes get no supports');
    }
    // The overhang band (NDFM-22), measured from the model's own base like the cap.
    // Regions are cut at its top before they become islands, so the voxel
    // islands they no longer cover come back in the merge below.
    const bandTopZ = options.overhangMaxHeightMm !== null ? box.min.z + options.overhangMaxHeightMm : null;
    const bandReport = bandTopZ !== null && overhangRegions ? {
        overhang_max_height_mm: options.overhangMaxHeightMm,
        band_top_plate_z_mm: bandTopZ,
        regions_scanned: overhangRegions.length,
        regions_clipped: 0,
        regions_dropped: 0,
        voxel_islands_above_band: [] as string[],
        highest_overhang_contact_mm: null as number | null,
        overhang_contacts_above_band: 0,
    } : null;
    if (bandTopZ !== null && !overhangRegions) {
        warnings.push('--overhang-max-height has no effect: the overhang family was not run');
    }
    if (bandReport && overhangRegions) {
        overhangRegions = overhangRegions.flatMap((region) => {
            const clipped = clipOverhangRegion(region, bandTopZ!, facePositions);
            if (!clipped) bandReport.regions_dropped++;
            else if (clipped !== region) bandReport.regions_clipped++;
            return clipped ? [clipped] : [];
        });
    }
    const overhangIslands = (overhangRegions ?? []).map(overhangRegionToIsland);
    const classified = classifyIntersection(voxelIslands, [], { xyToleranceMm: 0.5, zBandMm: layerHeightMm });
    const islands = mergeOverhangRegions(classified.islands, overhangIslands);

    // Placement settings. Density scales supports per area: twice the density,
    // half the area each carries. Placement may run more than once (NDFM-30).
    initializeBVH();
    const appSettings = createDefaultSettings();
    const autoDefaults = appSettings.autoSupport;
    const baseSettings: Partial<AutoSupportSettings> = { ...autoDefaults, ...options.settings };
    const baseAreaPerSupportMm2 = baseSettings.areaPerSupportMm2 ?? autoDefaults.areaPerSupportMm2;
    const settingsFor = (density: number): Partial<AutoSupportSettings> => (
        density === 1 ? { ...baseSettings } : { ...baseSettings, areaPerSupportMm2: baseAreaPerSupportMm2 / density }
    );
    setSettings(appSettings);
    // The raft is part of the scene placement sees (stumps and plate roots read it).
    const raftSettings: RaftSettings = { ...DEFAULT_RAFT_SETTINGS, bottomMode: options.raft };
    setRaftSettings(raftSettings);
    const modelId = 'model';
    const mesh = new THREE.Mesh(geometry);
    mesh.updateMatrixWorld(true);
    setModelMesh(modelId, mesh);

    // Which islands face into a sealed cavity. Hollow interiors go unsupported
    // by default: the only way in is a trunk routed up through the drain hole.
    const internalIds = new Set(islands.filter((island) => isInternalContact(mesh, island.contact)).map((island) => island.id));
    const skippedInternal = options.supportInternalIslands ? [] : islands.filter((island) => internalIds.has(island.id));
    // Above the cap nothing is supported: such islands must hold themselves up (Chi's ear tufts did).
    const aboveCap = capZone ? islands.filter((island) => capZone.contains(island.contact.x, island.contact.y, island.contact.z)) : [];
    const aboveCapIds = new Set(aboveCap.map((island) => island.id));
    const placed = (options.supportInternalIslands ? islands : islands.filter((island) => !internalIds.has(island.id)))
        .filter((island) => !aboveCapIds.has(island.id));
    const tag = (island: DetectedIsland) => (internalIds.has(island.id) ? 'internal' : 'external');
    const describe = (island: DetectedIsland) => `${island.id} (${tag(island)} ${island.source}, contact ${island.contact.x.toFixed(1)}, ${island.contact.y.toFixed(1)}, ${island.contact.z.toFixed(1)})`;
    if (skippedInternal.length > 0) {
        warnings.push(`${skippedInternal.length} of ${islands.length} islands face into a cavity and were not supported (hollow interiors are left bare; pass support_internal_islands to support them)`);
    }

    // One placement at a density: reset the store, place, commit, then remove
    // whatever enters a keep-out zone or rises above the cap. Its warnings and
    // reports are its own until it is chosen.
    // The surface's tilt round any point, for judging where tips land (NDFM-31).
    const surfaceTilt = new SurfaceTilt(facePositions);

    const attempt = async (density: number) => {
        const warnings: string[] = [];
        const band = bandReport ? { ...bandReport } : null;
        resetStore();
        resetKickstandsInState();
        clearHistory();
        const disposeHistory = registerSupportHistoryHandlers();
        const settingsOverride = settingsFor(density);
        const keepOutReport = zones.map((zone) => ({
            hole: zone.index,
            purpose: zone.spec.purpose ?? null,
            start_plate_mm: zone.start.toArray(),
            exit_plate_mm: zone.exit.toArray(),
            t_range_mm: [zone.tMin, zone.tMax],
            direction: zone.dir.toArray(),
            keep_out_radius_mm: zone.radius,
            blocked_triangles: 0,
            islands_inside: [] as string[],
            islands_inside_internal: [] as string[],
            contacts_removed: 0,
            supports_removed: 0,
            lost_coverage: [] as string[],
        }));
        const capReport = capZone ? {
            max_support_height_mm: options.maxSupportHeightMm,
            cap_plate_z_mm: capZone.capZ,
            blocked_triangles: 0,
            islands_above_cap: aboveCap.map((island) => island.id),
            contacts_removed: 0,
            supports_removed: 0,
            lost_coverage: [] as string[],
        } : null;
        if (aboveCap.length > 0) {
            warnings.push(`${aboveCap.length} of ${islands.length} islands lie above the height cap (${options.maxSupportHeightMm} mm above the model's base) and were not supported`);
        }
        if (band) {
            band.voxel_islands_above_band = placed
                .filter((island) => island.source !== 'overhang' && island.contact.z > bandTopZ!)
                .map((island) => island.id);
        }
        // Removal counters for every zone, holes first, in `allZones` order.
        const counters: { contacts_removed: number; supports_removed: number; lost_coverage: string[] }[] = capReport ? [...keepOutReport, capReport] : keepOutReport;
        if (allZones.length > 0) {
            const blocked = triangleIndicesInZones(facePositions, allZones);
            if (capReport) capReport.blocked_triangles = blocked.perZone[zones.length];
            blocked.perZone.slice(0, zones.length).forEach((count, k) => {
                keepOutReport[k].blocked_triangles = count;
                if (count === 0) warnings.push(`keep-out hole ${k}: no model surface lies within ${zones[k].radius} mm of its axis (does the holes file match this STL?)`);
            });
            setSupportBlockedTriangles(modelId, blocked.all);
            for (const island of islands) {
                zones.forEach((zone, k) => {
                    if (!zone.contains(island.contact.x, island.contact.y, island.contact.z)) return;
                    (internalIds.has(island.id) ? keepOutReport[k].islands_inside_internal : keepOutReport[k].islands_inside).push(island.id);
                });
            }
            keepOutReport.forEach((r, k) => {
                if (r.islands_inside.length + r.islands_inside_internal.length === 0) return;
                warnings.push(`keep-out hole ${k}: ${r.islands_inside.length} external island(s) [${r.islands_inside.join(', ')}] and ${r.islands_inside_internal.length} internal island(s) [${r.islands_inside_internal.join(', ')}] have their contact point inside the zone, so cannot take a support there`);
            });
        }

        const plan = await time('auto_place_ms', () => runAutoPlaceRequest({
            modelId,
            islands: placed.map(serializeIsland),
            settingsOverride,
            sizingBands: resolvedSizingBandsForRun(settingsOverride.sizingPreset ?? autoDefaults.sizingPreset),
            appSettings,
            baseState: getSnapshot(),
            mesh: serializeModelMesh(mesh),
            meshKey: modelMeshKey(mesh),
        }));
        if (!plan) throw new Error(`auto-support produced no plan (${placed.length} islands)`);
        const result = commitAutoPlacePlan(plan);
        disposeHistory();
        let removalTotals: Record<string, unknown> | null = null;
        if (allZones.length > 0) {
            deleteSupportBlockers(modelId);
            const entitiesBefore = totalEntities();
            // Whatever still enters a zone: a contact the blockers missed or a shaft leaning in.
            const probes = probeCommittedSupports();
            const reachOf = (island: DetectedIsland) => Math.max(3, Math.sqrt((island.areaMm2 ?? 0) / Math.PI) + 1);
            const at = (island: DetectedIsland) => new THREE.Vector3(island.contact.x, island.contact.y, island.contact.z);
            const coverageNear = (island: DetectedIsland, live: SupportProbe[]): number => {
                const where = at(island);
                return live.filter((p) => p.contacts.some((q) => q.distanceTo(where) <= reachOf(island))).length;
            };
            const before = new Map(placed.map((island) => [island.id, coverageNear(island, probes)]));
            const removedBy = new Map<string, Set<number>>();
            for (const probe of probes) {
                allZones.forEach((zone, k) => {
                    const contact = probe.contacts.some((q) => zone.contains(q.x, q.y, q.z));
                    const shaft = probe.runs.some(([p, q]) => zone.touchesSegment(p, q));
                    if (!contact && !shaft) return;
                    if (contact) counters[k].contacts_removed++;
                    counters[k].supports_removed++;
                    removedBy.set(probe.id, (removedBy.get(probe.id) ?? new Set<number>()).add(k));
                });
            }
            for (const id of removedBy.keys()) removeSupportEntity(id); // cascades to what hangs off it; a repeat is a harmless null
            // Shafts left holding nothing (no contact of their own, nothing hanging off them), to a fixed point.
            const danglingRemoved: string[] = [];
            for (;;) {
                const dangling = findDanglingShafts();
                if (dangling.length === 0) break;
                for (const id of dangling) removeSupportEntity(id);
                danglingRemoved.push(...dangling);
            }
            const live = probeCommittedSupports();
            const zoneNear = (island: DetectedIsland): number[] => zones.filter((zone) => {
                const where = at(island);
                const t = Math.min(zone.tMax, Math.max(zone.tMin, where.clone().sub(zone.start).dot(zone.dir)));
                return where.distanceTo(zone.start.clone().addScaledVector(zone.dir, t)) <= reachOf(island) + zone.radius;
            }).map((zone) => zone.index);
            for (const island of placed) {
                if (coverageNear(island, live) > 0) continue;
                if ((before.get(island.id) ?? 0) > 0) {
                    // Supported before the removals, bare after: say which hole's supports went.
                    const where = at(island);
                    const holes = new Set<number>();
                    for (const probe of probes) {
                        if (!removedBy.has(probe.id) || !probe.contacts.some((q) => q.distanceTo(where) <= reachOf(island))) continue;
                        removedBy.get(probe.id)!.forEach((k) => holes.add(k));
                    }
                    holes.forEach((k) => counters[k].lost_coverage.push(island.id));
                    warnings.push(`island ${describe(island)} lost all its supports to ${[...holes].map((k) => allZones[k].label).join(', ') || '?'}`);
                } else {
                    // Never had a support: the blockers (or placement) left it bare. Name a hole if one is within reach.
                    const near = zoneNear(island);
                    if (near.length > 0) warnings.push(`island ${describe(island)} has no support within reach; keep-out hole(s) ${near.join(', ')} block the surface nearby`);
                }
            }
            for (const [k, zone] of allZones.entries()) {
                const r = counters[k];
                if (r.supports_removed > 0) {
                    const where = zone instanceof KeepOutZone ? `entered ${zone.radius} mm of its axis` : 'rose above it';
                    warnings.push(`${zone.label}: removed ${r.supports_removed} support(s) (${r.contacts_removed} by contact, ${r.supports_removed - r.contacts_removed} by shaft) that ${where}`);
                }
            }
            // A last look at what is committed: nothing may be left inside any zone.
            const residual = live.filter((probe) => allZones.some((zone) => probe.contacts.some((q) => zone.contains(q.x, q.y, q.z))
                || probe.runs.some(([p, q]) => zone.touchesSegment(p, q)))).length;
            if (residual > 0) warnings.push(`${residual} support(s) are still inside ${capZone ? "a keep-out zone or above the height cap" : "a keep-out zone"} after removal`);
            removalTotals = {
                entities_before: entitiesBefore,
                entities_after: totalEntities(),
                entities_removed_total: entitiesBefore - totalEntities(),
                dangling_removed: danglingRemoved.length,
                residual_in_zone: residual,
            };
        }
        // Steep-wall contacts (NDFM-31): supports whose every tip meets surface
        // steeper than the limit. They hold up a wall that holds itself up and
        // tear a strip out of it on removal, so (by default) they go, except
        // where one is all that holds an island: then the least steep stays.
        type SteepContact = { tilt_deg: number; at_plate_mm: number[]; height_above_base_mm: number; island?: string };
        const steep = {
            limit_deg: options.steepContactDeg,
            radius_mm: STEEP_RADIUS_MM,
            avoid: options.avoidSteepContacts,
            contacts_checked: 0,
            supports_steep: 0,
            removed: [] as SteepContact[],
            kept_for_coverage: [] as SteepContact[],
            entities_removed: 0,
        };
        {
            const live = probeCommittedSupports();
            const steepBy = new Map<string, SteepContact>();
            for (const probe of live) {
                if (probe.contacts.length === 0) continue;
                // Steep where the surface round the tip is steep on average and
                // none of it needs support: a tip beside a real overhang stays.
                const around = probe.contacts.map((q) => surfaceTilt.around(q));
                const tilts = around.map((a) => a.mean);
                steep.contacts_checked += tilts.length;
                if (!around.every((a) => Number.isFinite(a.mean) && a.mean > options.steepContactDeg && a.flattest >= selfSupportAngleDeg)) continue;
                const i = tilts.indexOf(Math.min(...tilts));
                const q = probe.contacts[i];
                steepBy.set(probe.id, { tilt_deg: tilts[i], at_plate_mm: q.toArray(), height_above_base_mm: q.z - box.min.z });
            }
            steep.supports_steep = steepBy.size;
            const reach = (island: DetectedIsland) => Math.max(3, Math.sqrt((island.areaMm2 ?? 0) / Math.PI) + 1);
            for (const island of placed) {
                const where = new THREE.Vector3(island.contact.x, island.contact.y, island.contact.z);
                const near = live.filter((probe) => probe.contacts.some((q) => q.distanceTo(where) <= reach(island)));
                if (near.length === 0 || near.some((probe) => !steepBy.has(probe.id))) continue;
                // Everything near this island is steep-walled: keep the least steep.
                const keep = near.reduce((p, q) => (steepBy.get(p.id)!.tilt_deg <= steepBy.get(q.id)!.tilt_deg ? p : q));
                steep.kept_for_coverage.push({ ...steepBy.get(keep.id)!, island: island.id });
                steepBy.delete(keep.id);
            }
            steep.removed = [...steepBy.values()];
            if (options.avoidSteepContacts && steepBy.size > 0) {
                const before = totalEntities();
                for (const id of steepBy.keys()) removeSupportEntity(id);
                for (;;) {
                    const dangling = findDanglingShafts();
                    if (dangling.length === 0) break;
                    for (const id of dangling) removeSupportEntity(id);
                }
                steep.entities_removed = before - totalEntities();
            }
            if (steep.removed.length > 0) {
                warnings.push(options.avoidSteepContacts
                    ? `removed ${steep.removed.length} support(s) whose tips met walls steeper than ${options.steepContactDeg}° (scar risk, little support value)`
                    : `${steep.removed.length} support(s) have their tips on walls steeper than ${options.steepContactDeg}°: they can tear a strip from the surface when removed`);
            }
            if (steep.kept_for_coverage.length > 0) {
                warnings.push(`kept ${steep.kept_for_coverage.length} steep-wall support(s), each the only support for its island (${steep.kept_for_coverage.map((k) => k.island).join(', ')})`);
            }
        }
        if (band) {
            // The check: no overhang support's contact above the band, give or take
            // a triangle straddling its top.
            const heights = overhangContactHeights();
            band.highest_overhang_contact_mm = heights.length > 0 ? Math.max(...heights) - box.min.z : null;
            band.overhang_contacts_above_band = heights.filter((z) => z > bandTopZ! + OVERHANG_BAND_SLACK_MM).length;
            if (band.overhang_contacts_above_band > 0) {
                warnings.push(`${band.overhang_contacts_above_band} overhang support contact(s) landed more than ${OVERHANG_BAND_SLACK_MM} mm above the overhang band (${options.overhangMaxHeightMm} mm above the model's base)`);
            }
        }
        return { density, plan, result, settingsOverride, keepOutReport, capReport, removalTotals, band, warnings, steep };
    };

    // Sparse support (NDFM-30): overhang further than the limit from every
    // contact. The limit comes from the grid spacing at the starting settings,
    // so a denser retry is judged against the same yardstick.
    const sparseLimitMm = SPARSE_SPACING_FACTOR * Math.sqrt(baseAreaPerSupportMm2);
    const sparseCheck = (): SparseSupportReport => findSparseSupport(
        facePositions,
        probeCommittedSupports().flatMap((probe) => probe.contacts),
        box.min.z,
        {
            selfSupportAngleDeg,
            limitMm: sparseLimitMm,
            // Surface left bare on purpose is not sparse: in a zone, above the
            // cap, above the overhang band (the overhang family stops there), or
            // anywhere when the overhang family is off (lattices, NDFM-21).
            skip: (x, y, z) => !options.overhangs || (bandTopZ !== null && z > bandTopZ)
                || allZones.some((zone) => zone.contains(x, y, z)),
            ignoreBelowZ: options.liftMm === 0 ? box.min.z + layerHeightMm : undefined,
            isEnclosed: (p) => isInternalContact(mesh, p),
        },
    );
    // Without the overhang family the check has nothing to judge, so no retry.
    const densities = options.autoDensify && options.overhangs ? [1, 1.5, 2].map((k) => options.density * k) : [options.density];
    type Attempt = Awaited<ReturnType<typeof attempt>> & { sparse: SparseSupportReport; snapshot: ReturnType<typeof getSnapshot> };
    const tries: Attempt[] = [];
    for (const density of densities) {
        const placedRun = await attempt(density);
        const sparse = await time('sparse_check_ms', sparseCheck);
        tries.push({ ...placedRun, sparse, snapshot: cloneSupportState(getSnapshot()) });
        if (sparse.regions.length === 0) break;
    }
    // The least sparse area wins; a tie goes to the earlier, lower density.
    const best = tries.reduce((p, q) => (q.sparse.sparse_area_mm2 < p.sparse.sparse_area_mm2 ? q : p));
    if (best !== tries[tries.length - 1]) setSnapshot(cloneSupportState(best.snapshot));
    const { plan, result, settingsOverride, keepOutReport, capReport, removalTotals } = best;
    if (bandReport && best.band) Object.assign(bandReport, best.band);
    warnings.push(...best.warnings);
    const sparse = best.sparse;
    if (tries.length > 1) {
        warnings.push(`sparse support at density ${tries[0].density}: ${tries[0].sparse.sparse_area_mm2.toFixed(1)} mm² of overhang more than ${sparseLimitMm.toFixed(1)} mm from any contact; `
            + `tried ${tries.slice(1).map((t) => `${t.density} (${t.sparse.sparse_area_mm2.toFixed(1)} mm²)`).join(', ')}; kept density ${best.density}`);
    }
    const SPARSE_WARNINGS = 3;
    for (const region of sparse.regions.slice(0, SPARSE_WARNINGS)) {
        const [x, y, z] = region.centre_plate_mm;
        warnings.push(`${region.area_mm2.toFixed(1)} mm² of overhang ${region.height_above_base_mm.toFixed(1)} mm above the model's base (plate ${x.toFixed(1)}, ${y.toFixed(1)}, ${z.toFixed(1)}) `
            + `is up to ${region.max_gap_mm.toFixed(1)} mm from any support: it may sag or tear`);
    }
    if (sparse.regions.length > SPARSE_WARNINGS) warnings.push(`${sparse.regions.length - SPARSE_WARNINGS} more sparsely supported overhang region(s) (see sparse_support.regions)`);
    const committed = countCommitted();
    const analytics = plan.analytics;
    if (placed.length === 0) warnings.push('no islands to support: nothing was supported');
    if (analytics.islandsUncovered > 0) warnings.push(`${analytics.islandsUncovered} of ${placed.length} islands have no support near them`);
    const orphans = analytics.forestReport?.orphans?.length ?? 0;
    if (orphans > 0) warnings.push(`${orphans} supports were culled as orphans`);

    // The export path's support and raft triangles. The tip shrink comes from
    // the job's anti-aliasing, as the app's export orchestrator resolves it.
    const raster = resolveSliceRasterSettings({ printerProfile: job.printer, materialProfile: job.material });
    const { supportTipShrinkPercent } = resolveSliceJobAntiAliasing({
        printerProfile: job.printer,
        materialProfile: job.material,
        layerHeightMm: raster.layerHeightMm,
        request: job.antiAliasing ?? undefined,
    });
    const plateClearance: PlateFootprintSource[] = [{
        geometry: { geometry, center: new THREE.Vector3() },
        transform: { position: new THREE.Vector3(), rotation: new THREE.Euler(), scale: new THREE.Vector3(1, 1, 1) },
        visible: true,
    }];
    const supportTriangles = await time('support_mesh_ms', () => buildSupportAndRaftWorldTriangles(
        new Set([modelId]),
        undefined,
        supportTipShrinkPercent,
        plateClearance,
    ));

    // Model first, then supports: the engine splits the buffer at modelTriangleCount.
    const modelPositions = geometry.getAttribute('position').array as Float32Array;
    const modelTriangleCount = modelPositions.length / 9;
    const merged = new Float32Array(modelPositions.length + supportTriangles.length * 9);
    merged.set(modelPositions, 0);
    let offset = modelPositions.length;
    for (const t of supportTriangles) {
        merged.set([t.ax, t.ay, t.az, t.bx, t.by, t.bz, t.cx, t.cy, t.cz], offset);
        offset += 9;
    }
    if (supportTriangles.length === 0 && committed.roots > 0) warnings.push('supports were placed but produced no triangles');

    // Clearance (NDFM-28): supports that pass the model without touching it.
    // On a clone, as building a BVH reorders the geometry's index.
    const clearance: ClearanceReport = await time('clearance_ms', () => findGrazes(
        geometry.clone(),
        merged.subarray(modelPositions.length),
        probeCommittedSupports().flatMap((probe) => probe.contacts),
        { minClearanceMm: options.minClearanceMm },
    ));
    const GRAZE_WARNINGS = 5;
    for (const graze of clearance.grazes.slice(0, GRAZE_WARNINGS)) {
        const [x, y, z] = graze.at_plate_mm;
        const run = graze.z_range_mm[1] - graze.z_range_mm[0];
        warnings.push(
            `a support passes ${graze.gap_mm.toFixed(2)} mm from the model ${graze.height_above_base_mm.toFixed(1)} mm above its base `
            + `(plate ${x.toFixed(1)}, ${y.toFixed(1)}, ${z.toFixed(1)})${run >= 0.5 ? `, alongside for ${run.toFixed(1)} mm` : ''}: `
            + `a gap under ${clearance.min_clearance_mm} mm can cure shut, fusing it to the surface`,
        );
    }
    if (clearance.grazes.length > GRAZE_WARNINGS) {
        warnings.push(`${clearance.grazes.length - GRAZE_WARNINGS} more support(s) pass within ${clearance.min_clearance_mm} mm of the model (see clearance.grazes)`);
    }

    // The same check on what will actually be sliced, in case a support or the
    // raft ever does rise above the model.
    const topMm = maxZ(merged);
    const layerCount = resolveSliceLayerCount({ maxZMm: topMm, printerProfile: job.printer, layerHeightMm: raster.layerHeightMm });
    if (layerCount.tallestObjectHeightMm < topMm) {
        throw new Error(
            `the print is ${topMm.toFixed(2)} mm tall (model ${(box.max.z - box.min.z).toFixed(2)} mm + lift ${options.liftMm} mm), `
            + `but '${job.printer.name}' builds only ${buildHeightMm} mm: lower the lift or scale the model`,
        );
    }
    const overwritten: string[] = [];

    if (options.supportedStl) {
        if (prepareOutput(options.supportedStl)) overwritten.push(options.supportedStl);
        writeBinaryStl(options.supportedStl, merged);
    }
    // The model alone, in the frame the slicer gets: for a viewer to lay beside a layer image.
    if (options.plateStl) {
        if (prepareOutput(options.plateStl)) overwritten.push(options.plateStl);
        writeBinaryStl(options.plateStl, modelPositions);
    }

    // How a layer image maps to the plate frame, for a viewer laying the plate
    // STL beside one: the image spans the build area centred on the origin,
    // image rows run from +Y down, and the printer may mirror either axis.
    const layerFrame = {
        source_width_px: raster.sourceResolutionX,
        source_height_px: raster.sourceResolutionY,
        width_px: raster.widthPx,
        height_px: raster.heightPx,
        x_packing_mode: raster.xPackingMode,
        build_width_mm: build.width,
        build_depth_mm: build.depth,
        layer_height_mm: raster.layerHeightMm,
        mirror_x: raster.mirrorX,
        mirror_y: raster.mirrorY,
    };

    let slice: Record<string, unknown> | null = null;
    if (options.slice || options.jobDir) {
        const tmp = options.jobDir ?? mkdtempSync(join(tmpdir(), TEMP_PREFIX));
        if (options.jobDir) mkdirSync(tmp, { recursive: true });
        try {
            const positionsPath = join(tmp, 'positions.bin');
            writeFileSync(positionsPath, Buffer.from(merged.buffer, merged.byteOffset, merged.byteLength));
            const jobPath = join(tmp, 'job.json');
            const geometryForJob: SceneSliceGeometry = {
                maxZMm: topMm,
                models: [{
                    id: modelId,
                    name: basename(options.stl).replace(/\.stl$/i, ''),
                    polygonCount: modelTriangleCount,
                    transform: { position: { x: 0, y: 0, z: 0 }, rotation: { x: 0, y: 0, z: 0 }, scale: { x: 1, y: 1, z: 1 } },
                }],
            };
            const run = sliceJob.buildSceneSliceRun(job, geometryForJob, positionsPath, options.out!, jobPath);
            // `scene slice` sends no split (it never slices supports); ours goes
            // where the app's export puts it, after the model's own triangles.
            const payload = JSON.parse(run.jobJson!) as Record<string, unknown>;
            if (!('model_triangle_count' in payload)) throw new Error('the slice job no longer carries model_triangle_count');
            payload.model_triangle_count = modelTriangleCount;
            writeFileSync(jobPath, JSON.stringify(payload));
            if (options.slice) {
                if (prepareOutput(options.out!)) overwritten.push(options.out!);
                const stdout = await time('slice_ms', () => execFileSync(options.cli!, run.args, {
                    encoding: 'utf-8',
                    maxBuffer: 64 * 1024 * 1024,
                    stdio: ['ignore', 'pipe', 'inherit'],
                }));
                slice = JSON.parse(stdout) as Record<string, unknown>;
            }
        } finally {
            if (!options.jobDir) rmSync(tmp, { recursive: true, force: true });
        }
    }

    const placedByType = Object.fromEntries(Object.entries(result.placed).filter(([, count]) => count > 0));
    console.error(`autosupport-slice: ${placed.length} islands, ${committed.contacts} contacts, ${supportTriangles.length} support/raft triangles`);
    process.stdout.write(`${JSON.stringify({
        stl: options.stl,
        printer: { preset_id: job.printer.officialPresetId ?? null, name: job.printer.name, output_format: format, material: job.material.name, layer_height_mm: layerHeightMm },
        lift_mm: options.liftMm,
        model_bbox_mm: { min: box.min.toArray(), max: box.max.toArray() },
        // Plate frame: the engine's X/Y origin is the plate centre, Z is up from the plate.
        plate_transform: { translate_mm: plateShift.toArray(), rotation: null, scale: 1 },
        plate_stl: options.plateStl,
        layer_frame: layerFrame,
        height_mm: topMm,
        build_height_mm: buildHeightMm,
        layers_expected: layerCount.totalLayers,
        support_tip_shrink_percent: supportTipShrinkPercent,
        island_detection: { px_mm: options.pxMm ?? detect.pxMm, support_buffer_mm: detect.supportBufferMm },
        islands: placed.length,
        islands_by_source: placed.reduce<Record<string, number>>((counts, island) => {
            counts[island.source] = (counts[island.source] ?? 0) + 1;
            return counts;
        }, {}),
        island_list: placed.map((island) => ({
            id: island.id,
            source: island.source,
            contact: [island.contact.x, island.contact.y, island.contact.z],
            area_mm2: island.areaMm2 ?? null,
        })),
        status: result.status,
        placed_by_type: placedByType,
        entities_by_type: committed.byType,
        contacts: committed.contacts,
        roots: committed.roots,
        islands_covered: analytics.islandsCovered,
        islands_uncovered: analytics.islandsUncovered,
        area_coverage: analytics.areaCoverage,
        area_per_support_mm2: settingsOverride.areaPerSupportMm2,
        raft: options.raft,
        model_triangles: modelTriangleCount,
        support_triangles: supportTriangles.length,
        total_triangles: merged.length / 9,
        supported_stl: options.supportedStl,
        output: options.slice ? options.out : null,
        job_dir: options.jobDir,
        keep_out: options.keepOut ? { source: options.keepOut, holes: keepOutReport, ...removalTotals } : null,
        height_cap: capReport ? { ...capReport, ...removalTotals } : null,
        overhang_band: bandReport,
        clearance,
        sparse_support: sparse,
        steep_contacts: best.steep,
        density: {
            requested: options.density,
            auto_densify: options.autoDensify,
            chosen: best.density,
            tries: tries.map((t) => ({ density: t.density, contacts: countContacts(t.snapshot), sparse_area_mm2: t.sparse.sparse_area_mm2, sparse_regions: t.sparse.regions.length })),
        },
        islands_detected: islands.length,
        islands_internal: internalIds.size,
        islands_internal_skipped: skippedInternal.length,
        overwritten,
        slice,
        timings_ms: timings,
        warnings,
    })}\n`);
}

/**
 * Exit once stdout and stderr have drained, success or failure. The pipeline
 * leaves handles open that nothing in the app closes (the yield
 * MessageChannel, which holds the event loop for as long as it has a
 * listener, and possibly others), so returning from `main` is not enough: a
 * run that failed half way used to print its error and then sit until the
 * Python side's 45-minute timeout (NDFM-23).
 */
function exitWhenFlushed(code: number): void {
    disposeEventLoopChannel();
    process.exitCode = code;
    let pending = 2;
    const done = () => {
        if (--pending === 0) process.exit(code);
    };
    process.stdout.write('', done);
    process.stderr.write('', done);
}

main().then(
    () => exitWhenFlushed(0),
    (error) => {
        process.stderr.write(`autosupport-slice: ${error instanceof Error ? error.stack ?? error.message : String(error)}\n`);
        exitWhenFlushed(1);
    },
);
