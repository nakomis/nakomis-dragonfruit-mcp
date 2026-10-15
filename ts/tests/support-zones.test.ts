/**
 * Tests for support-zones.ts (NDFM-28), run by tests/test_support_zones.py:
 *
 *   cd vendor/dragonfruit && TSX_TSCONFIG_PATH=$PWD/tsconfig.json NODE_PATH=$PWD/node_modules \
 *     node_modules/.bin/tsx --test --test-reporter=tap ../../ts/tests/support-zones.test.ts
 *
 * SPDX-License-Identifier: AGPL-3.0-or-later
 */

import assert from 'node:assert/strict';
import { test } from 'node:test';
import * as THREE from 'three';

import { findGrazes, HeightCapZone, KeepOutZone, triangleIndicesInZones } from '../support-zones';

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
