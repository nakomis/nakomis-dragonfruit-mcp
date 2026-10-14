/**
 * Tests for voxel-islands.ts (NDFM-23), run by tests/test_voxel_islands.py:
 *
 *   cd vendor/dragonfruit && TSX_TSCONFIG_PATH=$PWD/tsconfig.json NODE_PATH=$PWD/node_modules \
 *     node_modules/.bin/tsx --test ../../ts/tests/voxel-islands.test.ts
 *
 * SPDX-License-Identifier: AGPL-3.0-or-later
 *
 * The run-based components are checked against a per-voxel flood fill (the
 * shape of upstream's, on grids small enough for a Set) on random synthetic
 * layers, against upstream's own `detectVoxelIslands` on a generated model, and
 * on a candidate volume larger than a Set can hold.
 */

import assert from 'node:assert/strict';
import { after, test } from 'node:test';
import * as THREE from 'three';

import { disposeEventLoopChannel } from '@/utils/yieldToEventLoop';
import type { DetectedIsland } from '@/volumeAnalysis/Islands/types';

import { CandidateRuns, connectIslands, detectVoxelIslandsBounded, gridFor, type GridGeometry, type WorkerLike } from '../voxel-islands';

// Upstream's detector yields through a MessageChannel that holds the event loop open.
after(disposeEventLoopChannel);

/** A seeded generator, so a failure reproduces. */
function rng(seed: number): () => number {
    let s = seed >>> 0;
    return () => {
        s = (s * 1664525 + 1013904223) >>> 0;
        return s / 2 ** 32;
    };
}

/** Candidate pairs (col, row, ...) in row order from a 0/1 mask. */
function pairsOf(mask: Uint8Array, width: number, height: number): Int32Array {
    const out: number[] = [];
    for (let r = 0; r < height; r++) for (let c = 0; c < width; c++) if (mask[r * width + c]) out.push(c, r);
    return Int32Array.from(out);
}

/**
 * The reference: one Set entry a voxel, flooded in insertion order with the 26
 * (or 6) neighbours, as detect.ts's `buildIslands` does.
 */
function floodIslands(layers: Int32Array[], geom: GridGeometry, diagonal: boolean): DetectedIsland[] {
    const { width, height } = geom;
    const pack = (c: number, r: number, l: number) => (l * height + r) * width + c;
    const all = new Set<number>();
    layers.forEach((pairs, L) => {
        for (let i = 0; i < pairs.length; i += 2) all.add(pack(pairs[i], pairs[i + 1], L));
    });
    const offsets: number[][] = [];
    for (let dc = -1; dc <= 1; dc++) for (let dr = -1; dr <= 1; dr++) for (let dl = -1; dl <= 1; dl++) {
        if (!dc && !dr && !dl) continue;
        if (!diagonal && Math.abs(dc) + Math.abs(dr) + Math.abs(dl) !== 1) continue;
        offsets.push([dc, dr, dl]);
    }
    const islands: DetectedIsland[] = [];
    for (const start of all) {
        const comp: number[] = [];
        const stack = [start];
        all.delete(start);
        while (stack.length) {
            const k = stack.pop()!;
            comp.push(k);
            const c = k % width;
            const r = Math.floor(k / width) % height;
            const l = Math.floor(k / (width * height));
            for (const [dc, dr, dl] of offsets) {
                const nc = c + dc, nr = r + dr, nl = l + dl;
                if (nc < 0 || nc >= width || nr < 0 || nr >= height || nl < 0) continue;
                if (all.delete(pack(nc, nr, nl))) stack.push(pack(nc, nr, nl));
            }
        }
        const layerOf = (k: number) => Math.floor(k / (width * height));
        const minLayer = Math.min(...comp.map(layerOf));
        const maxLayer = Math.max(...comp.map(layerOf));
        const base = comp.filter((k) => layerOf(k) === minLayer);
        const xs = base.map((k) => geom.originX + (k % width) * geom.px + geom.px * 0.5);
        const ys = base.map((k) => -(geom.originZ + (Math.floor(k / width) % height) * geom.px));
        const baseZ = geom.minZ + minLayer * geom.layerHeightMm;
        const xy = new Float32Array(base.length * 2);
        base.forEach((_, i) => { xy[i * 2] = xs[i]; xy[i * 2 + 1] = ys[i]; });
        islands.push({
            id: `v${islands.length}`,
            source: 'voxel',
            contact: new THREE.Vector3(xs.reduce((a, b) => a + b, 0) / base.length, ys.reduce((a, b) => a + b, 0) / base.length, baseZ),
            baseZ,
            areaMm2: base.length * geom.px * geom.px,
            layerSpan: [minLayer, maxLayer],
            contactVoxels: { xy, z: null, count: base.length },
        });
    }
    return islands;
}

/** A footprint as sorted "x,y" strings, so flood order and row order compare equal. */
function footprintKeys(island: DetectedIsland): string[] {
    const f = island.contactVoxels!;
    const keys: string[] = [];
    for (let i = 0; i < f.count; i++) keys.push(`${f.xy[i * 2].toFixed(4)},${f.xy[i * 2 + 1].toFixed(4)}`);
    return keys.sort();
}

function assertSameIslands(actual: DetectedIsland[], expected: DetectedIsland[]): void {
    assert.equal(actual.length, expected.length, 'island count');
    actual.forEach((island, i) => {
        const want = expected[i];
        assert.equal(island.id, want.id);
        assert.equal(island.source, 'voxel');
        assert.deepEqual(island.layerSpan, want.layerSpan, `${island.id} layer span`);
        assert.ok(Math.abs(island.areaMm2! - want.areaMm2!) < 1e-9, `${island.id} area ${island.areaMm2} vs ${want.areaMm2}`);
        assert.ok(island.contact.distanceTo(want.contact) < 1e-6, `${island.id} contact ${island.contact.toArray()} vs ${want.contact.toArray()}`);
        assert.equal(island.baseZ, want.baseZ);
        assert.deepEqual(footprintKeys(island), footprintKeys(want), `${island.id} footprint`);
    });
}

const GEOM: GridGeometry = { width: 23, height: 17, originX: -1.15, originZ: -0.85, px: 0.1, minZ: 2, layerHeightMm: 0.05 };

test('runs join exactly as the per-voxel flood fill does, 26- and 6-connected', () => {
    for (const diagonal of [true, false]) {
        for (let seed = 1; seed <= 40; seed++) {
            const random = rng(seed);
            const density = 0.05 + 0.5 * random();
            const layers: Int32Array[] = [];
            for (let L = 0; L < 9; L++) {
                // Some empty layers, to break the stack into separate islands.
                const mask = new Uint8Array(GEOM.width * GEOM.height);
                if (random() > 0.2) for (let i = 0; i < mask.length; i++) mask[i] = random() < density ? 1 : 0;
                layers.push(pairsOf(mask, GEOM.width, GEOM.height));
            }
            const runs = new CandidateRuns();
            for (const pairs of layers) runs.addLayer(pairs);
            assertSameIslands(connectIslands(runs, GEOM, diagonal), floodIslands(layers, GEOM, diagonal));
        }
    }
});

test('diagonal neighbours: a corner touch joins only when 26-connected', () => {
    // Layer 0 has (0, 0); layer 1 has (1, 1): touching only at a corner.
    const layers = [Int32Array.from([0, 0]), Int32Array.from([1, 1])];
    for (const [diagonal, expected] of [[true, 1], [false, 2]] as const) {
        const runs = new CandidateRuns();
        layers.forEach((pairs) => runs.addLayer(pairs));
        assert.equal(connectIslands(runs, GEOM, diagonal).length, expected);
    }
});

test('runs refuse candidates out of order', () => {
    const runs = new CandidateRuns();
    assert.throws(() => runs.addLayer(Int32Array.from([5, 3, 1, 2])), /out of order/);
});

test('more candidate voxels than a Set can hold', () => {
    // 4,000 x 1,500 px, every voxel a candidate, 3 layers: 18 million voxels,
    // past the 2^24 a V8 Set holds (upstream's buildIslands throws
    // "RangeError: Set maximum size exceeded" here). As runs it is 4,500.
    const geom: GridGeometry = { width: 4000, height: 1500, originX: -100, originZ: -37.5, px: 0.05, minZ: 7, layerHeightMm: 0.05 };
    const runs = new CandidateRuns();
    const pairs = new Int32Array(geom.width * geom.height * 2);
    for (let r = 0, i = 0; r < geom.height; r++) for (let c = 0; c < geom.width; c++, i += 2) {
        pairs[i] = c;
        pairs[i + 1] = r;
    }
    for (let L = 0; L < 3; L++) runs.addLayer(pairs);
    assert.ok(runs.voxels > 2 ** 24);
    assert.equal(runs.count, 3 * geom.height);
    const started = performance.now();
    const islands = connectIslands(runs, geom);
    assert.ok(performance.now() - started < 30_000, 'joining runs should take seconds, not minutes');
    assert.equal(islands.length, 1);
    assert.deepEqual(islands[0].layerSpan, [0, 2]);
    assert.ok(Math.abs(islands[0].areaMm2! - geom.width * geom.height * geom.px * geom.px) < 1e-6);
    assert.equal(islands[0].contactVoxels!.count, geom.width * geom.height);
});

/** Non-indexed positions for axis-aligned boxes, 12 triangles each, wound outwards. */
function boxPositions(boxes: number[][]): Float32Array {
    const faces = [[0, 1, 3], [0, 3, 2], [4, 6, 7], [4, 7, 5], [0, 4, 5], [0, 5, 1], [2, 3, 7], [2, 7, 6], [0, 2, 6], [0, 6, 4], [1, 5, 7], [1, 7, 3]];
    const out: number[] = [];
    for (const [x0, y0, z0, x1, y1, z1, tilt = 0] of boxes) {
        // `tilt` raises the bottom face's far (+Y) edge, so the base meets the
        // layers a strip at a time, as most real parts do.
        const v: number[][] = [];
        for (const x of [x0, x1]) for (const y of [y0, y1]) for (const z of [z0, z1]) v.push([x, y, z === z0 ? z0 + (tilt * (y - y0)) / (y1 - y0) : z1]);
        for (const f of faces) for (const i of f) out.push(...v[i]);
    }
    return Float32Array.from(out);
}

async function loadWorkers(): Promise<{ islandScan: typeof import('../../vendor/dragonfruit/scripts/bench-island-scan'); Worker: new () => WorkerLike }> {
    const islandScan = await import('../../vendor/dragonfruit/scripts/bench-island-scan');
    await islandScan.installInProcessWorkers();
    return { islandScan, Worker: (globalThis as unknown as { Worker: new () => WorkerLike }).Worker };
}

/** A small part with a tilted base, an overhanging shelf and a floating block. */
const MODEL = boxPositions([
    [0, 0, 0, 12, 10, 6, 0.4],
    [-4, -3, 6.03, 16, 13, 7.5],
    [3, 3, 9.07, 6, 6, 10.5],
]);

function bboxOf(positions: Float32Array): THREE.Box3 {
    const geometry = new THREE.BufferGeometry();
    geometry.setAttribute('position', new THREE.BufferAttribute(positions, 3));
    geometry.computeBoundingBox();
    return geometry.boundingBox!;
}

test('the same islands as upstream detectVoxelIslands on a generated model', async () => {
    const { islandScan, Worker } = await loadWorkers();
    const options = { ...islandScan.DEFAULT_DETECT_OPTIONS, pxMm: 0.1, supportBufferMm: 0.25, layerHeightMm: 0.05 };
    const geometry = new THREE.BufferGeometry();
    geometry.setAttribute('position', new THREE.BufferAttribute(MODEL.slice(), 3));
    // Upstream, through the bench's synchronous shim (where its dispatcher is correct).
    const expected = await islandScan.detectIslands(geometry, options);
    const actual = await detectVoxelIslandsBounded(() => new Worker(), MODEL, bboxOf(MODEL), options.layerHeightMm, options);
    assert.ok(expected.length >= 3, `expected the base strips, the shelf and the floating block, got ${expected.length}`);
    assertSameIslands(actual, expected);
});

test('a worker that answers later (as a real one does) gives the same islands', async () => {
    // The old deferred shim posted from inside a reply, so one reply reached
    // every listener added while the shim was still iterating: each layer took
    // an earlier layer's candidates, and a solid's candidate Set overflowed.
    const { Worker } = await loadWorkers();
    const deferred = (): WorkerLike => {
        const inner = new Worker();
        return { ...inner, postMessage: (message: unknown) => setImmediate(() => inner.postMessage(message)) };
    };
    const { geom } = gridFor(bboxOf(MODEL), 0.1, 0.05);
    const params = { pxMm: geom.px, supportBufferMm: 0.25, connectivity: 4 as const };
    const now = await detectVoxelIslandsBounded(() => new Worker(), MODEL, bboxOf(MODEL), 0.05, params);
    const later = await detectVoxelIslandsBounded(deferred, MODEL, bboxOf(MODEL), 0.05, params);
    assert.ok(now.length > 0);
    assertSameIslands(later, now);
});
