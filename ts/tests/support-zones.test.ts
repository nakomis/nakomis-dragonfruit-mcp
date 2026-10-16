/**
 * Tests for support-zones.ts (NDFM-28, NDFM-22), run by tests/test_support_zones.py:
 *
 *   cd vendor/dragonfruit && TSX_TSCONFIG_PATH=$PWD/tsconfig.json NODE_PATH=$PWD/node_modules \
 *     node_modules/.bin/tsx --test --test-reporter=tap ../../ts/tests/support-zones.test.ts
 *
 * SPDX-License-Identifier: AGPL-3.0-or-later
 */

import assert from 'node:assert/strict';
import { test } from 'node:test';
import * as THREE from 'three';
import { acceleratedRaycast, computeBoundsTree } from 'three-mesh-bvh';

import {
    clipOverhangRegion,
    findGrazes,
    findSparseSupport,
    HeightCapZone,
    KeepOutZone,
    positionsInFaceOrder,
    SurfaceTilt,
    tiltDeg,
    triangleIndicesInZones,
    type ClippableOverhangRegion,
} from '../support-zones';

/** A non-indexed box, as an STL loads: `size` across, its minimum corner at `min`. */
function box(size: [number, number, number], min: [number, number, number]): THREE.BufferGeometry {
    const geometry = new THREE.BoxGeometry(...size).toNonIndexed();
    geometry.translate(min[0] + size[0] / 2, min[1] + size[1] / 2, min[2] + size[2] / 2);
    return geometry;
}

const positions = (...geometries: THREE.BufferGeometry[]): Float32Array => {
    const arrays = geometries.map((g) => g.getAttribute('position').array as Float32Array);
    const out = new Float32Array(arrays.reduce((n, a) => n + a.length, 0));
    let offset = 0;
    for (const a of arrays) {
        out.set(a, offset);
        offset += a.length;
    }
    return out;
};

const v = (x: number, y: number, z: number) => new THREE.Vector3(x, y, z);

test('the height cap holds everything above it, and nothing at or below', () => {
    const cap = new HeightCapZone(40);
    assert.equal(cap.contains(0, 0, 40.01), true);
    assert.equal(cap.contains(100, -100, 40), false);
    assert.equal(cap.contains(0, 0, 7), false);
    // A shaft that rises through the cap enters it; one that stays below does not.
    assert.equal(cap.touchesSegment(v(0, 0, 10), v(3, 0, 45)), true);
    assert.equal(cap.touchesSegment(v(0, 0, 10), v(3, 0, 39.9)), false);
    assert.equal(cap.label, 'the height cap');
});

test('model triangles above the cap are blocked, with the holes counted separately', () => {
    // A 10 mm cube from z 0 to 10, capped at 5: its top and the upper halves of its sides.
    const cube = positions(box([10, 10, 10], [0, 0, 0]));
    const cap = new HeightCapZone(5);
    const hole = new KeepOutZone(0, { x: 5, y: 5, z: 0, radius_mm: 0.5, direction: [0, 0, -1], length_mm: 0 }, v(0, 0, 0));
    const blocked = triangleIndicesInZones(cube, [hole, cap]);
    // Top (2) and four sides (2 each) reach above z 5; the bottom (2) does not.
    assert.equal(blocked.perZone[1], 10);
    // The hole's zone meets the bottom face only.
    assert.equal(blocked.perZone[0], 2);
    assert.equal(blocked.all.size, 12);
});

test('a column running 0.1 mm beside the model is one graze, with its height and run', () => {
    const model = box([10, 10, 10], [0, 0, 0]); // x 0..10, z 0..10
    // A 0.4 mm column, its near face 0.1 mm off the model's x = 10 face, from z 2 to 8.
    const column = box([0.4, 0.4, 6], [10.1, 4.8, 2]);
    const report = findGrazes(model, positions(column), [v(5, 5, 10)]);
    assert.equal(report.grazes.length, 1);
    const [graze] = report.grazes;
    assert.ok(Math.abs(graze.gap_mm - 0.1) < 1e-3, `gap ${graze.gap_mm}`);
    assert.ok(graze.z_range_mm[0] <= 2.01 && graze.z_range_mm[1] >= 7.99, `z range ${graze.z_range_mm}`);
    assert.ok(graze.height_above_base_mm >= 2 && graze.height_above_base_mm <= 8);
    assert.ok(report.samples_checked > 0);
});

test('a support tip meeting the surface is not a graze, nor is anything well clear or below the base', () => {
    const model = box([10, 10, 10], [0, 0, 7]); // lifted: base at z 7
    // A tip touching the top at (5, 5, 17), with its contact listed.
    const tip = box([0.4, 0.4, 2], [4.8, 4.8, 17]);
    // A column 2 mm off the side: clear.
    const clear = box([0.4, 0.4, 6], [12, 4.8, 9]);
    // A trunk under the model, 0.05 mm below its base: the raft side, not checked.
    const under = box([0.4, 0.4, 6.95], [4.8, 4.8, 0]);
    const report = findGrazes(model, positions(tip, clear, under), [v(5, 5, 17)]);
    assert.deepEqual(report.grazes, []);
});

test('two columns on opposite sides are two grazes, the closer one first', () => {
    const model = box([10, 10, 10], [0, 0, 0]);
    const near = box([0.4, 0.4, 4], [10.05, 4.8, 3]); // 0.05 mm off x = 10
    const far = box([0.4, 0.4, 4], [-0.6, 4.8, 3]); // 0.2 mm off x = 0
    const report = findGrazes(model, positions(far, near), []);
    assert.equal(report.grazes.length, 2);
    assert.ok(report.grazes[0].gap_mm < report.grazes[1].gap_mm);
    assert.ok(report.grazes[0].at_plate_mm[0] > 10);
});

test('a clearance of 0 turns the check off', () => {
    const model = box([10, 10, 10], [0, 0, 0]);
    const column = box([0.4, 0.4, 6], [10.1, 4.8, 2]);
    const report = findGrazes(model, positions(column), [], { minClearanceMm: 0 });
    assert.equal(report.samples_checked, 0);
    assert.deepEqual(report.grazes, []);
});

/**
 * A region as the scanner returns it, over a staircase of three flat
 * down-facing squares at z 1, 5 and 9, side by side in X: two triangles each
 * (0-1, 2-3, 4-5), one footprint pixel a millimetre square under each.
 */
function staircase(): { region: ClippableOverhangRegion; positions: Float32Array } {
    const quads = [1, 5, 9].map((z, i) => {
        const g = new THREE.PlaneGeometry(1, 1).toNonIndexed();
        g.rotateX(Math.PI); // facing down
        g.translate(i + 0.5, 0.5, z);
        return g;
    });
    return {
        positions: positions(...quads),
        region: {
            triangleIds: [0, 1, 2, 3, 4, 5],
            areaMm2: 3,
            projectedAreaMm2: 3,
            xyMin: [0, 0],
            xyMax: [3, 1],
            minZ: 1,
            maxZ: 9,
            dragMomentMm3: 15,
            footprint: { width: 3, height: 1, originX: 0, originY: 0, pxMm: 1, data: [1, 1, 1], surfaceZ: [1, 5, 9] },
        },
    };
}

test('an overhang region is cut at the band, its areas shrinking with it', () => {
    const { region, positions: stairs } = staircase();
    const clipped = clipOverhangRegion(region, 6, stairs)!;
    assert.deepEqual(clipped.triangleIds, [0, 1, 2, 3]);
    assert.deepEqual(clipped.footprint.data, [1, 1, 0]);
    assert.deepEqual(clipped.footprint.surfaceZ, [1, 5, 9]);
    assert.ok(Math.abs(clipped.areaMm2 - 2) < 1e-6 && Math.abs(clipped.projectedAreaMm2 - 2) < 1e-6);
    assert.ok(Math.abs(clipped.dragMomentMm3! - 10) < 1e-6);
    assert.deepEqual(clipped.xyMin, [0, 0]);
    assert.deepEqual(clipped.xyMax, [2, 1]);
    assert.equal(clipped.minZ, 1);
    assert.equal(clipped.maxZ, 6);
    // The scanner's own record is left alone.
    assert.deepEqual(region.footprint.data, [1, 1, 1]);
    assert.equal(region.triangleIds.length, 6);
});

test('a region wholly in the band is returned as it is, and one wholly above it is dropped', () => {
    const { region, positions: stairs } = staircase();
    assert.equal(clipOverhangRegion(region, 9, stairs), region);
    assert.equal(clipOverhangRegion(region, 0.5, stairs), null);
});

test("after a BVH sorts the model, a raycast's face index names the triangle positionsInFaceOrder puts there", () => {
    // Many small boxes, so the BVH has something to sort.
    const parts: THREE.BufferGeometry[] = [];
    for (let i = 0; i < 40; i++) parts.push(box([1, 1, 1], [(i * 7) % 13, (i * 5) % 11, i % 3]));
    const geometry = new THREE.BufferGeometry();
    geometry.setAttribute('position', new THREE.BufferAttribute(positions(...parts), 3));
    assert.equal(positionsInFaceOrder(geometry), geometry.getAttribute('position').array);
    (geometry as unknown as { computeBoundsTree: typeof computeBoundsTree }).computeBoundsTree = computeBoundsTree;
    (geometry as unknown as { computeBoundsTree: () => void }).computeBoundsTree();
    const index = geometry.getIndex()!.array;
    let moved = 0;
    for (let f = 0; f < index.length / 3; f++) if (index[f * 3] !== f * 3) moved++;
    assert.ok(moved > 0, 'the BVH left the triangles in their order, so this test proves nothing');
    const faces = positionsInFaceOrder(geometry);
    const mesh = new THREE.Mesh(geometry);
    mesh.raycast = acceleratedRaycast;
    const raycaster = new THREE.Raycaster(v(0.5, 0.5, -5), v(0, 0, 1));
    const [hit] = raycaster.intersectObject(mesh, false);
    // The hit point lies in the triangle that face index names, in face order.
    const f = hit.faceIndex!;
    const triangle = new THREE.Triangle(v(faces[f * 9], faces[f * 9 + 1], faces[f * 9 + 2]),
        v(faces[f * 9 + 3], faces[f * 9 + 4], faces[f * 9 + 5]), v(faces[f * 9 + 6], faces[f * 9 + 7], faces[f * 9 + 8]));
    const onIt = new THREE.Vector3();
    triangle.closestPointToPoint(hit.point, onIt);
    assert.ok(onIt.distanceTo(hit.point) < 1e-6, `face ${f} is not where the ray hit`);
});


// Sparse support (NDFM-30). A box from z 5 to 6: its 10 x 10 mm underside is a
// flat overhang (the base face points straight down), its sides are vertical.
const SPARSE = { selfSupportAngleDeg: 45, limitMm: 4 };

test('a flat underside held only at one corner is sparse; a 3 mm grid of contacts is not', () => {
    const slab = positions(box([10, 10, 1], [0, 0, 5]));
    const lonely = findSparseSupport(slab, [v(0, 0, 5)], 5, SPARSE);
    assert.ok(Math.abs(lonely.overhang_area_mm2 - 100) < 1e-6, `overhang ${lonely.overhang_area_mm2}`);
    assert.equal(lonely.regions.length, 1);
    const [region] = lonely.regions;
    // Everything beyond 4 mm of the corner: 100 - π·4²/4 ≈ 87 mm².
    assert.ok(region.area_mm2 > 80 && region.area_mm2 < 92, `area ${region.area_mm2}`);
    assert.ok(region.max_gap_mm > 12, `gap ${region.max_gap_mm}`);
    assert.ok(Math.abs(region.height_above_base_mm) < 1e-6);
    assert.equal(lonely.sparse_area_mm2, region.area_mm2);

    const grid: THREE.Vector3[] = [];
    for (let x = 0; x <= 10; x += 3) for (let y = 0; y <= 10; y += 3) grid.push(v(x + 0.5, y + 0.5, 5));
    const held = findSparseSupport(slab, grid, 5, SPARSE);
    assert.deepEqual(held.regions, []);
    assert.equal(held.sparse_area_mm2, 0);
});

test('steep and upward-facing surfaces are not overhang', () => {
    // A tall thin box: its top faces up, its sides are vertical; only the bottom counts.
    const tower = positions(box([2, 2, 20], [0, 0, 0]));
    const report = findSparseSupport(tower, [], 0, SPARSE);
    assert.ok(Math.abs(report.overhang_area_mm2 - 4) < 1e-6, `overhang ${report.overhang_area_mm2}`);
    // 4 mm² of underside is above the 2 mm² minimum, and no contact is anywhere near.
    assert.equal(report.regions.length, 1);
});

test('small regions, skipped points and cavity ceilings are left out', () => {
    const slab = positions(box([10, 10, 1], [0, 0, 5]));
    // A larger minimum area than the whole underside.
    assert.deepEqual(findSparseSupport(slab, [], 5, { ...SPARSE, minAreaMm2: 150 }).regions, []);
    // Every point skipped (as inside a keep-out zone or above the cap).
    const skipped = findSparseSupport(slab, [], 5, { ...SPARSE, skip: () => true });
    assert.equal(skipped.samples_checked, 0);
    assert.equal(skipped.overhang_area_mm2, 0);
    // The whole underside facing into a cavity.
    const cavity = findSparseSupport(slab, [], 5, { ...SPARSE, isEnclosed: () => true });
    assert.deepEqual(cavity.regions, []);
    assert.equal(cavity.internal_regions_skipped, 1);
    // A model standing on the plate: its bottom face is not overhang at all.
    const onPlate = findSparseSupport(slab, [], 5, { ...SPARSE, ignoreBelowZ: 5.05 });
    assert.equal(onPlate.samples_checked, 0);
});

test('two separate sparse patches are two regions, the larger first', () => {
    const big = box([10, 10, 1], [0, 0, 5]);
    const small = box([4, 4, 1], [30, 0, 5]);
    const report = findSparseSupport(positions(small, big), [], 5, SPARSE);
    assert.equal(report.regions.length, 2);
    assert.ok(report.regions[0].area_mm2 > report.regions[1].area_mm2);
    assert.ok(report.regions[0].centre_plate_mm[0] < 15);
});

test('a triangle partly skipped counts only the part kept', () => {
    const slab = positions(box([10, 10, 1], [0, 0, 5]));
    // Skip the half of the underside with x < 5.
    const half = findSparseSupport(slab, [], 5, { ...SPARSE, skip: (x) => x < 5 });
    assert.ok(half.overhang_area_mm2 > 40 && half.overhang_area_mm2 < 60, `overhang ${half.overhang_area_mm2}`);
    assert.ok(half.sparse_area_mm2 > 40 && half.sparse_area_mm2 < 60, `sparse ${half.sparse_area_mm2}`);
});


// Steep-wall contacts (NDFM-31).
test('tilt is 0 for a flat underside, 90 for a wall, 180 for a top', () => {
    assert.ok(Math.abs(tiltDeg(-1)) < 1e-9);
    assert.ok(Math.abs(tiltDeg(0) - 90) < 1e-9);
    assert.ok(Math.abs(tiltDeg(1) - 180) < 1e-9);
    assert.ok(Math.abs(tiltDeg(-Math.cos(Math.PI / 4)) - 45) < 1e-9);
});

test('the surface round a tip: underside, wall and top of a box', () => {
    // A 20 mm cube: points 0.01 mm inside each face's middle.
    const tilt = new SurfaceTilt(positions(box([20, 20, 20], [0, 0, 0])));
    assert.ok(Math.abs(tilt.at({ x: 10, y: 10, z: 0.01 })) < 1e-6, 'underside');
    assert.ok(Math.abs(tilt.at({ x: 0.01, y: 10, z: 10 }) - 90) < 1e-6, 'wall');
    assert.ok(Math.abs(tilt.at({ x: 10, y: 10, z: 19.99 }) - 180) < 1e-6, 'top');
    // Off the model altogether: no answer.
    assert.ok(Number.isNaN(tilt.at({ x: 100, y: 100, z: 100 })));
});

test('a tip at the edge between an underside and a wall averages both', () => {
    // Small triangles, so the 1.5 mm radius sees both faces near the edge.
    const fine = new THREE.BoxGeometry(20, 20, 20, 20, 20, 20).toNonIndexed();
    fine.translate(10, 10, 10);
    const tilt = new SurfaceTilt(fine.getAttribute('position').array as Float32Array);
    const edge = tilt.at({ x: 0, y: 10, z: 0 });
    assert.ok(edge > 30 && edge < 60, `edge ${edge}`);
});
