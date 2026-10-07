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
 *   STL -> lift off the plate -> voxel islands (`detectVoxelIslands`, through the
 *   bench's in-process worker shim) -> `runAutoPlaceRequest` (the worker's own
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
 * the bench's in-process worker; the overhang family is a Tauri command over
 * Rust, which `dragonfruit-mcp-tools overhangs` runs for us (`--tools`); mesh
 * minima (also Tauri) are not run, so a model may get fewer supports at
 * isolated low points than the GUI would give it.
 *
 * Usage:
 *   tsx autosupport-slice.ts --stl <in.stl> [--out <out.ctb>] --cli <dragonfruit-cli> [--tools <dragonfruit-mcp-tools>]
 *     --printer-json <profile.json> [--material <material.json>] [--layer-height N]
 *     [--aa-preset sharp|balanced|smooth|raw] [--lift-mm 7] [--density 1] [--raft solid|line|off]
 *     [--settings <json>] [--coarse-islands] [--px-mm 0.05] [--supported-stl <out.stl>] [--plate-stl <out.stl>] [--job-dir <dir>]
 *     [--no-slice] [--verbose]
 *
 * `--supported-stl` writes model, supports and raft as sliced; `--plate-stl` the
 * model alone in the same plate frame (centred, lifted), to line up with a layer.
 * `--job-dir` keeps the engine's input (positions.bin, job.json) for inspection;
 * with `--no-slice` it writes them without slicing. `--coarse-islands` trades
 * island detail for speed (the bench's resolution rather than the app panel's).
 * Existing output files are overwritten.
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
import { getSnapshot, resetKickstandsInState, resetStore } from '@/supports/state';
import { registerSupportHistoryHandlers } from '@/supports/history/useSupportHistoryHandlers';
import { setModelMesh } from '@/supports/autoSupport/meshStore';
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
import { resolveSliceLayerCount, resolveSliceRasterSettings } from '@/features/slicing/sliceJobAssembly';
import { resolveSliceJobAntiAliasing } from '@/features/slicing/sliceAntiAliasing';
import type { PlateFootprintSource } from '@/supports/Rafts/Crenelated/geometry/modelPlateFootprint';
import { disposeEventLoopChannel } from '@/utils/yieldToEventLoop';
import type { RaftSettings } from '@/supports/Rafts/Crenelated/RaftTypes';
import { mergeOverhangRegions, overhangRegionToIsland } from '@/volumeAnalysis/Islands/useIslands';
import { classifyIntersection } from '@/volumeAnalysis/Islands/intersection';
import type { DetectedIsland, OverhangScan } from '@/volumeAnalysis/Islands/types';

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
    supportedStl: string | null;
    plateStl: string | null;
    jobDir: string | null;
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
        supportedStl: null,
        plateStl: null,
        jobDir: null,
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
        else if (arg === '--supported-stl') options.supportedStl = resolve(value());
        else if (arg === '--plate-stl') options.plateStl = resolve(value());
        else if (arg === '--job-dir') options.jobDir = resolve(value());
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
 * The bench's stand-in worker answers synchronously, and the detector asks for
 * the next layer from inside each answer, so the stack grows by a few frames per
 * layer: fine for the bench's small corpus, a stack overflow for a 70 mm part
 * at 50 µm. Delivering each message on its own macrotask (as a real worker
 * would) keeps the stack flat; the in-thread handler and its ordering are
 * unchanged.
 */
async function deferWorkerMessages(install: () => Promise<void>): Promise<void> {
    await install();
    const globals = globalThis as unknown as { Worker: new () => { postMessage: (message: unknown) => void } };
    const InProcessWorker = globals.Worker;
    globals.Worker = function DeferredWorker() {
        const inner = new InProcessWorker();
        return { ...inner, postMessage: (message: unknown) => setImmediate(() => inner.postMessage(message)) };
    } as unknown as typeof globals.Worker;
}

/**
 * The app's `scan_overhangs`, through `dragonfruit-mcp-tools overhangs`, mapped
 * to islands with the app's own `overhangRegionToIsland`.
 */
function scanOverhangs(tools: string, positions: Float32Array, angleDeg: number, hasRaft: boolean): DetectedIsland[] {
    const tmp = mkdtempSync(join(tmpdir(), TEMP_PREFIX));
    try {
        const input = join(tmp, 'positions.bin');
        writeFileSync(input, Buffer.from(positions.buffer, positions.byteOffset, positions.byteLength));
        const args = ['overhangs', input, '--angle', String(angleDeg), '--px-mm', String(OVERHANG_FOOTPRINT_PX_MM)];
        if (hasRaft) args.push('--has-raft');
        const stdout = execFileSync(tools, args, { encoding: 'utf-8', maxBuffer: 512 * 1024 * 1024 });
        const scan = JSON.parse(stdout) as OverhangScan;
        return scan.regions.map(overhangRegionToIsland);
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
            timings[label] = Math.round(performance.now() - started);
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
    accelerateGeometry(geometry);
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

    // Islands, the voxel family first, as the bench detects them.
    type IslandScanModule = typeof import('../vendor/dragonfruit/scripts/bench-island-scan');
    const islandScan = await importDragonFruitScript<IslandScanModule>('scripts/bench-island-scan.ts');
    await deferWorkerMessages(islandScan.installInProcessWorkers);
    const detect = options.coarseIslands ? ISLAND_DETECT.coarse : ISLAND_DETECT.panel;
    const voxelIslands = await time('islands_ms', () => islandScan.detectIslands(geometry, {
        ...islandScan.DEFAULT_DETECT_OPTIONS,
        pxMm: options.pxMm ?? detect.pxMm,
        supportBufferMm: detect.supportBufferMm,
        layerHeightMm,
    }));

    // Then the overhang family, which is what puts a density grid under shallow
    // slopes (the voxel detector only sees surfaces flatter than ~11°). The app
    // runs it as a Tauri command; we run the same Rust through our own tool.
    // Mesh minima (the third family) are not available headless.
    const selfSupportAngleDeg = options.settings.overhangSelfSupportAngleDeg
        ?? createDefaultSettings().autoSupport.overhangSelfSupportAngleDeg;
    let overhangIslands: DetectedIsland[] = [];
    if (options.tools) {
        overhangIslands = await time('overhangs_ms', () => scanOverhangs(
            options.tools!,
            geometry.getAttribute('position').array as Float32Array,
            selfSupportAngleDeg,
            options.raft !== 'off',
        ));
    } else {
        warnings.push('no --tools: overhang regions were not scanned, so shallow slopes get no supports');
    }
    const classified = classifyIntersection(voxelIslands, [], { xyToleranceMm: 0.5, zBandMm: layerHeightMm });
    const islands = mergeOverhangRegions(classified.islands, overhangIslands);

    // Placement, through the worker's entry point, then the app's commit.
    resetStore();
    resetKickstandsInState();
    clearHistory();
    const disposeHistory = registerSupportHistoryHandlers();
    initializeBVH();
    const appSettings = createDefaultSettings();
    const autoDefaults = appSettings.autoSupport;
    const settingsOverride: Partial<AutoSupportSettings> = {
        ...autoDefaults,
        ...options.settings,
    };
    if (options.density !== 1) {
        // Density scales supports per area: twice the density, half the area each carries.
        settingsOverride.areaPerSupportMm2 = (settingsOverride.areaPerSupportMm2 ?? autoDefaults.areaPerSupportMm2) / options.density;
    }
    setSettings(appSettings);
    // The raft is part of the scene placement sees (stumps and plate roots read it).
    const raftSettings: RaftSettings = { ...DEFAULT_RAFT_SETTINGS, bottomMode: options.raft };
    setRaftSettings(raftSettings);
    const modelId = 'model';
    const mesh = new THREE.Mesh(geometry);
    mesh.updateMatrixWorld(true);
    setModelMesh(modelId, mesh);

    const plan = await time('auto_place_ms', () => runAutoPlaceRequest({
        modelId,
        islands: islands.map(serializeIsland),
        settingsOverride,
        sizingBands: resolvedSizingBandsForRun(settingsOverride.sizingPreset ?? autoDefaults.sizingPreset),
        appSettings,
        baseState: getSnapshot(),
        mesh: serializeModelMesh(mesh),
        meshKey: modelMeshKey(mesh),
    }));
    if (!plan) throw new Error(`auto-support produced no plan (${islands.length} islands)`);
    const result = commitAutoPlacePlan(plan);
    disposeHistory();
    const committed = countCommitted();
    const analytics = plan.analytics;
    if (islands.length === 0) warnings.push('no islands found: nothing was supported');
    if (analytics.islandsUncovered > 0) warnings.push(`${analytics.islandsUncovered} of ${islands.length} islands have no support near them`);
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

    // The island detector's MessageChannel keeps the event loop alive.
    disposeEventLoopChannel();

    const placedByType = Object.fromEntries(Object.entries(result.placed).filter(([, count]) => count > 0));
    console.error(`autosupport-slice: ${islands.length} islands, ${committed.contacts} contacts, ${supportTriangles.length} support/raft triangles`);
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
        islands: islands.length,
        islands_by_source: islands.reduce<Record<string, number>>((counts, island) => {
            counts[island.source] = (counts[island.source] ?? 0) + 1;
            return counts;
        }, {}),
        island_list: islands.map((island) => ({
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
        overwritten,
        slice,
        timings_ms: timings,
        warnings,
    })}\n`);
}

main().catch((error) => {
    process.stderr.write(`autosupport-slice: ${error instanceof Error ? error.stack ?? error.message : String(error)}\n`);
    process.exitCode = 1;
});
