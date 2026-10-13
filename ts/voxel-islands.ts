/**
 * The app's voxel island detector, with the candidate voxels held as runs.
 *
 * SPDX-License-Identifier: AGPL-3.0-or-later
 *
 * Upstream's `detectVoxelIslands` (src/volumeAnalysis/Islands/detect.ts) slices
 * every layer on the scanline worker, takes each layer's unsupported candidate
 * voxels, puts every one of them into a `Set<number>` and flood-fills that Set
 * in 3D. A V8 Set holds at most 2^24 (16,777,216) entries, and long before that
 * it costs tens of bytes a voxel, so a large part with a lot of unsupported
 * area throws `RangeError: Set maximum size exceeded` or thrashes (NDFM-23).
 *
 * This keeps the worker (so the per-layer slicing, rasterising and candidate
 * extraction are the app's own) and replaces only what follows it: each
 * layer's candidates become runs along a row (layer, row, start, end), in
 * typed arrays, and the runs are joined by union-find with the same 26- or
 * 6-connectivity the flood fill uses. Memory goes with the number of runs, not
 * voxels: a 60 mm square of candidates is 1,200 runs, not 1.44 million keys.
 *
 * The islands match upstream's: the same ids in the same order (a component is
 * numbered by its first voxel in layer, row, column order, as the Set's
 * insertion order numbers it), contact at the mean of the lowest layer's
 * voxels, the same area, layer span and contact footprint (footprint voxels in
 * row order rather than flood order; contact sums may differ in the last bits).
 */

import type * as THREE from 'three';
import { Vector3 } from 'three';

import { VoxelFootprintBuilder } from '@/volumeAnalysis/Islands/voxelFootprint';
import type { DetectedIsland } from '@/volumeAnalysis/Islands/types';

/** Pixel-centre offsets, as detect.ts (and ScanOrchestrator) place a voxel. */
const VOXEL_OFFSET_X = 0.5;
const VOXEL_OFFSET_Y = 0;

export interface VoxelIslandParams {
    pxMm: number;
    supportBufferMm: number;
    /** Per-layer 2D candidate connectivity, passed to the worker's scanLayer. */
    connectivity?: 4 | 8;
    /** 3D connectivity across layers: true is 26-connected (upstream's default), false 6. */
    diagonal3D?: boolean;
    minAreaMm2?: number;
}

/** Where the grid sits in world space, as detect.ts lays it out. */
export interface GridGeometry {
    width: number;
    height: number;
    originX: number;
    /** Mask row 0 is world Y = -originZ (mask Y is -world Y). */
    originZ: number;
    px: number;
    minZ: number;
    layerHeightMm: number;
}

/**
 * Candidate voxels as row runs, appended layer by layer in layer, row, column
 * order. Four growable Int32Arrays: 16 bytes a run, and nothing for the
 * collector to trace.
 */
export class CandidateRuns {
    layer = new Int32Array(1024);
    row = new Int32Array(1024);
    start = new Int32Array(1024);
    /** Exclusive. */
    end = new Int32Array(1024);
    count = 0;
    voxels = 0;
    /** Where each layer's runs begin; `layerStart[L + 1]` is where they end. */
    private layerStarts: number[] = [0];

    get layers(): number {
        return this.layerStarts.length - 1;
    }

    /** Index range of layer `L`'s runs. */
    layerRange(L: number): [number, number] {
        return [this.layerStarts[L], this.layerStarts[L + 1]];
    }

    /**
     * Add the next layer from the worker's candidate pairs (col, row, col, row,
     * ...), which come in row order with columns rising, as `candidateVoxelPairs`
     * emits them. Layers must be added in order, empty ones included.
     */
    addLayer(pairs: Int32Array | null | undefined): void {
        const L = this.layers;
        if (pairs) {
            let i = 0;
            while (i < pairs.length) {
                const r = pairs[i + 1];
                const s = pairs[i];
                let e = s + 1;
                i += 2;
                while (i < pairs.length && pairs[i + 1] === r && pairs[i] === e) {
                    e++;
                    i += 2;
                }
                this.push(L, r, s, e);
            }
        }
        this.layerStarts.push(this.count);
    }

    private push(L: number, r: number, s: number, e: number): void {
        if (this.count > 0) {
            // A run must come after the last (and not touch it on its row); the
            // union-find's neighbour search and the island numbering rely on it.
            const k = this.count - 1;
            const after = L > this.layer[k] || (L === this.layer[k] && (r > this.row[k] || (r === this.row[k] && s > this.end[k])));
            if (!after) throw new Error(`candidate voxels out of order at layer ${L}, row ${r}, column ${s}`);
        }
        if (this.count === this.layer.length) this.grow();
        this.layer[this.count] = L;
        this.row[this.count] = r;
        this.start[this.count] = s;
        this.end[this.count] = e;
        this.count++;
        this.voxels += e - s;
    }

    private grow(): void {
        const size = this.layer.length * 2;
        const grown = (old: Int32Array) => {
            const next = new Int32Array(size);
            next.set(old);
            return next;
        };
        this.layer = grown(this.layer);
        this.row = grown(this.row);
        this.start = grown(this.start);
        this.end = grown(this.end);
    }
}

/** Union-find root, halving the path as it goes. */
function find(parent: Int32Array, i: number): number {
    while (parent[i] !== i) {
        parent[i] = parent[parent[i]];
        i = parent[i];
    }
    return i;
}

/** Join two sets under the smaller index, so a root is always its set's first run. */
function union(parent: Int32Array, a: number, b: number): void {
    const ra = find(parent, a);
    const rb = find(parent, b);
    if (ra < rb) parent[rb] = ra;
    else if (rb < ra) parent[ra] = rb;
}

/** First run of each row in [from, to), indexed by row; rows with none get the next row's start. */
function rowIndex(runs: CandidateRuns, from: number, to: number, height: number): Int32Array {
    const index = new Int32Array(height + 1);
    let k = from;
    for (let r = 0; r <= height; r++) {
        while (k < to && runs.row[k] < r) k++;
        index[r] = k;
    }
    return index;
}

/**
 * Union every run in [from, to) (one row) with the runs in [ga, gb) (another
 * row) it touches. `tol` 1 counts a diagonal touch (columns one apart), 0 only
 * an overlap. Both ranges are sorted by column and do not overlap themselves.
 */
function joinRows(runs: CandidateRuns, parent: Int32Array, from: number, to: number, ga: number, gb: number, tol: number): void {
    let p = ga;
    for (let i = from; i < to; i++) {
        const s = runs.start[i];
        const e = runs.end[i];
        while (p < gb && runs.end[p] + tol <= s) p++;
        for (let j = p; j < gb && runs.start[j] < e + tol; j++) union(parent, i, j);
    }
}

/**
 * 3D connected components of the candidate runs, as islands. The same
 * neighbourhood as upstream's flood fill: with `diagonal`, any of the 26 voxels
 * round a voxel; without, the 6 that share a face.
 */
export function connectIslands(runs: CandidateRuns, geom: GridGeometry, diagonal = true): DetectedIsland[] {
    const n = runs.count;
    const parent = new Int32Array(n);
    for (let i = 0; i < n; i++) parent[i] = i;
    const tol = diagonal ? 1 : 0;

    let nextIndex: Int32Array | null = null;
    for (let L = 0; L < runs.layers; L++) {
        const [a, b] = runs.layerRange(L);
        if (a === b) {
            nextIndex = null;
            continue;
        }
        const here = nextIndex ?? rowIndex(runs, a, b, geom.height);
        const [na, nb] = L + 1 < runs.layers ? runs.layerRange(L + 1) : [0, 0];
        nextIndex = na < nb ? rowIndex(runs, na, nb, geom.height) : null;
        for (let r = 0; r < geom.height; r++) {
            const from = here[r];
            const to = here[r + 1];
            if (from === to) continue;
            // Same layer, the row below (the row above was joined from there).
            if (r + 1 < geom.height) joinRows(runs, parent, from, to, here[r + 1], here[r + 2], tol);
            if (!nextIndex) continue;
            // The layer above: the same row, and its neighbours when diagonal.
            joinRows(runs, parent, from, to, nextIndex[r], nextIndex[r + 1], tol);
            if (diagonal) {
                if (r > 0) joinRows(runs, parent, from, to, nextIndex[r - 1], nextIndex[r], tol);
                if (r + 1 < geom.height) joinRows(runs, parent, from, to, nextIndex[r + 1], nextIndex[r + 2], tol);
            }
        }
    }

    // Number the components by their first run. Every union points the larger
    // root at the smaller, so a run's parent is never after it and a root is
    // its set's first run (and its layer the set's lowest): one forward pass
    // meets each root before the rest of its set.
    const component = new Int32Array(n);
    const roots: number[] = [];
    for (let i = 0; i < n; i++) {
        const root = find(parent, i);
        if (root === i) {
            component[i] = roots.length;
            roots.push(i);
        } else {
            component[i] = component[root];
        }
    }
    const count = roots.length;
    const minLayer = new Int32Array(count);
    const maxLayer = new Int32Array(count);
    const baseVoxels = new Float64Array(count);
    for (let c = 0; c < count; c++) {
        minLayer[c] = runs.layer[roots[c]];
        maxLayer[c] = minLayer[c];
    }
    for (let i = 0; i < n; i++) {
        const c = component[i];
        if (runs.layer[i] > maxLayer[c]) maxLayer[c] = runs.layer[i];
        if (runs.layer[i] === minLayer[c]) baseVoxels[c] += runs.end[i] - runs.start[i];
    }
    const sumX = new Float64Array(count);
    const sumY = new Float64Array(count);
    const footprints = Array.from({ length: count }, (_, c) => new VoxelFootprintBuilder(Math.min(baseVoxels[c], 1 << 20)));
    for (let i = 0; i < n; i++) {
        const c = component[i];
        if (runs.layer[i] !== minLayer[c]) continue;
        const vy = -(geom.originZ + runs.row[i] * geom.px - geom.px * VOXEL_OFFSET_Y);
        for (let col = runs.start[i]; col < runs.end[i]; col++) {
            const vx = geom.originX + col * geom.px + geom.px * VOXEL_OFFSET_X;
            sumX[c] += vx;
            sumY[c] += vy;
            footprints[c].push(vx, vy);
        }
    }

    const islands: DetectedIsland[] = [];
    for (let c = 0; c < count; c++) {
        const baseZ = geom.minZ + minLayer[c] * geom.layerHeightMm;
        islands.push({
            id: `v${c}`,
            source: 'voxel',
            contact: new Vector3(sumX[c] / baseVoxels[c], sumY[c] / baseVoxels[c], baseZ),
            baseZ,
            areaMm2: baseVoxels[c] * geom.px * geom.px,
            layerSpan: [minLayer[c], maxLayer[c]],
            contactVoxels: footprints[c].build(),
        });
    }
    return islands;
}

export interface WorkerLike {
    postMessage: (message: unknown) => void;
    addEventListener: (type: string, listener: (event: { data: unknown }) => void) => void;
    removeEventListener: (type: string, listener: (event: { data: unknown }) => void) => void;
    terminate: () => void;
}

/**
 * Each layer's candidate pairs from the scanline worker, one layer at a time,
 * handed to `onLayer` as they come so no layer outlives its turn.
 *
 * `createWorker` is the bench's in-process stand-in (or a real worker). One
 * listener stays for the whole scan and every request is posted from this
 * loop, never from inside a reply: posting from the reply is what upstream's
 * dispatcher does, and against the in-process stand-in that either grows the
 * stack a few frames a layer or, with delivery deferred, hands one reply to
 * every listener added while the stand-in is still iterating its listeners.
 */
export async function sliceCandidateLayers(
    createWorker: () => WorkerLike,
    positions: Float32Array,
    geom: GridGeometry,
    numLayers: number,
    opts: { px_mm: number; support_buffer_mm: number; connectivity: 4 | 8 },
    onLayer: (layer: number, pairs: Int32Array) => void,
): Promise<void> {
    const worker = createWorker();
    let waiting: ((pairs: Int32Array) => void) | null = null;
    const onMessage = (event: { data: unknown }) => {
        const msg = event.data as { type?: string; result?: { candidatePairs: Int32Array } };
        if (msg?.type !== 'done' || !waiting) return;
        const resolve = waiting;
        waiting = null;
        resolve(msg.result!.candidatePairs);
    };
    worker.addEventListener('message', onMessage);
    try {
        worker.postMessage({ type: 'init', positions });
        const gridRef = { originX: geom.originX, originZ: geom.originZ, width: geom.width, height: geom.height, px_mm: geom.px };
        for (let L = 0; L < numLayers; L++) {
            const zTop = geom.minZ + (L + 1) * geom.layerHeightMm + 1e-6;
            const reply = new Promise<Int32Array>((resolve) => { waiting = resolve; });
            worker.postMessage({ type: 'layer', z: zTop, layerHeightMm: geom.layerHeightMm, gridRef, opts });
            onLayer(L, await reply);
        }
    } finally {
        worker.removeEventListener('message', onMessage);
        worker.terminate();
    }
}

/** The grid detect.ts lays over a bounding box. */
export function gridFor(bbox: THREE.Box3, pxMm: number, layerHeightMm: number): { geom: GridGeometry; numLayers: number } {
    return {
        geom: {
            width: Math.max(1, Math.ceil((bbox.max.x - bbox.min.x) / pxMm)),
            height: Math.max(1, Math.ceil((bbox.max.y - bbox.min.y) / pxMm)),
            originX: bbox.min.x,
            originZ: -bbox.max.y,
            px: pxMm,
            minZ: bbox.min.z,
            layerHeightMm,
        },
        numLayers: Math.max(0, Math.ceil((bbox.max.z - bbox.min.z) / layerHeightMm)),
    };
}

/**
 * `detectVoxelIslands`, bounded: the same worker, grid and islands, with the
 * candidates as runs. `createWorker` must already have the scanline worker
 * loaded (the bench's `installInProcessWorkers`).
 */
export async function detectVoxelIslandsBounded(
    createWorker: () => WorkerLike,
    positions: Float32Array,
    bbox: THREE.Box3,
    layerHeightMm: number,
    params: VoxelIslandParams,
): Promise<DetectedIsland[]> {
    const { geom, numLayers } = gridFor(bbox, params.pxMm, layerHeightMm);
    if (numLayers === 0) return [];
    console.log(`[Islands] grid ${geom.width}x${geom.height} px @ ${params.pxMm} mm, ${numLayers} layers @ ${layerHeightMm} mm (runs)`);
    const runs = new CandidateRuns();
    await sliceCandidateLayers(createWorker, positions, geom, numLayers, {
        px_mm: params.pxMm,
        support_buffer_mm: params.supportBufferMm,
        connectivity: params.connectivity ?? 4,
    }, (_layer, pairs) => runs.addLayer(pairs));
    console.log(`[Islands] candidate voxels: ${runs.voxels} in ${runs.count} runs`);
    const all = connectIslands(runs, geom, params.diagonal3D !== false);
    const kept = all.filter((island) => (island.areaMm2 ?? 0) >= (params.minAreaMm2 ?? 0.02));
    console.log(`[Islands] islands: ${all.length}, kept: ${kept.length}`);
    return kept;
}
