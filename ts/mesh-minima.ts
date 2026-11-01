/**
 * Mesh-minima island detector, headless.
 *
 * The app's third island family (the first being voxel slice-growth, the second
 * mesh-normal overhangs) is `scan_mesh_minima`, a Rust Tauri command — inert
 * without Tauri, so the MCP could not use it and fell back to the overhang
 * family instead. The overhang family lays a *density grid* under every shallow
 * slope, which is why MCP output is far denser than the app's "Generate
 * Supports" button (voxel + minima, points only). This reimplements the minima
 * detector in TypeScript so the MCP can offer the same sparse, point-based set.
 *
 * Faithful to `src-tauri/src/mesh_minima.rs::scan_minima_internal`: a welded
 * mesh vertex is a local vertical minimum when every vertex within its k-ring
 * (k=2) is strictly higher. The Rust then keeps it outright when it faces down
 * (vertex-normal z < -0.05) and otherwise disambiguates with a downward ray
 * (odd hit count ⇒ material below ⇒ a top-surface dimple, dropped). This v1
 * keeps only the clearly-downward minima (normal z < -0.05) and skips the
 * ray-test tier — conservative: it under-detects the ambiguous cases rather
 * than risk supporting a top surface.
 */
import { BufferAttribute, BufferGeometry, Mesh, Raycaster, Vector3 } from 'three';
import type { DetectedIsland } from '@/volumeAnalysis/Islands/types';

/** Vertex counts as a downward-facing local minimum below this normal-z. */
const DOWNWARD_NZ = -0.05;

/**
 * Detect local vertical minima on a world-space triangle soup (9 floats per
 * triangle). Returns point islands (`source: 'minima'`), the same shape the
 * app's `scanMeshMinima` maps its Rust result into.
 */
export function detectMeshMinima(facePositions: Float32Array, k = 2): DetectedIsland[] {
    const triCount = Math.floor(facePositions.length / 9);
    // Weld the soup: quantise positions so shared vertices collapse to one id.
    const q = 1e-4;
    const keyToId = new Map<string, number>();
    const vx: number[] = [];
    const vy: number[] = [];
    const vz: number[] = [];
    const tri: number[] = []; // 3 vertex ids per triangle
    const idOf = (x: number, y: number, z: number): number => {
        const key = `${Math.round(x / q)}|${Math.round(y / q)}|${Math.round(z / q)}`;
        let id = keyToId.get(key);
        if (id === undefined) {
            id = vx.length;
            vx.push(x); vy.push(y); vz.push(z);
            keyToId.set(key, id);
        }
        return id;
    };
    for (let t = 0; t < triCount; t++) {
        const o = t * 9;
        tri.push(
            idOf(facePositions[o], facePositions[o + 1], facePositions[o + 2]),
            idOf(facePositions[o + 3], facePositions[o + 4], facePositions[o + 5]),
            idOf(facePositions[o + 6], facePositions[o + 7], facePositions[o + 8]),
        );
    }
    const vCount = vx.length;
    // 1-ring adjacency, incident faces, and accumulated vertex normals.
    const adj: Set<number>[] = Array.from({ length: vCount }, () => new Set<number>());
    const firstFace = new Int32Array(vCount).fill(-1);
    const nx = new Float64Array(vCount);
    const ny = new Float64Array(vCount);
    const nz = new Float64Array(vCount);
    const faceCount = tri.length / 3;
    const ux = new Vector3(); const uy = new Vector3(); const n = new Vector3();
    for (let f = 0; f < faceCount; f++) {
        const a = tri[f * 3]; const b = tri[f * 3 + 1]; const c = tri[f * 3 + 2];
        adj[a].add(b); adj[a].add(c); adj[b].add(a); adj[b].add(c); adj[c].add(a); adj[c].add(b);
        if (firstFace[a] < 0) firstFace[a] = f;
        if (firstFace[b] < 0) firstFace[b] = f;
        if (firstFace[c] < 0) firstFace[c] = f;
        ux.set(vx[b] - vx[a], vy[b] - vy[a], vz[b] - vz[a]);
        uy.set(vx[c] - vx[a], vy[c] - vy[a], vz[c] - vz[a]);
        n.crossVectors(ux, uy).normalize();
        for (const v of [a, b, c]) { nx[v] += n.x; ny[v] += n.y; nz[v] += n.z; }
    }
    // A ray-castable mesh of the welded surface, for the downward "is there
    // material below?" test on ambiguous (non-downward) minima — the tier the
    // Rust does with a BVH. Positions are world-space, so identity transform.
    const posArr = new Float32Array(vCount * 3);
    for (let i = 0; i < vCount; i++) {
        posArr[i * 3] = vx[i]; posArr[i * 3 + 1] = vy[i]; posArr[i * 3 + 2] = vz[i];
    }
    const geom = new BufferGeometry();
    geom.setAttribute('position', new BufferAttribute(posArr, 3));
    geom.setIndex(tri);
    const rayMesh = new Mesh(geom);
    rayMesh.updateMatrixWorld(true);
    const raycaster = new Raycaster();
    const DOWN = new Vector3(0, 0, -1);
    const rayOrigin = new Vector3();

    // k-ring BFS reused across vertices via a stamp array (no per-vertex alloc).
    const stamp = new Int32Array(vCount).fill(-1);
    const out: DetectedIsland[] = [];
    let mi = 0;
    let queue: number[] = [];
    let next: number[] = [];
    for (let v = 0; v < vCount; v++) {
        const zi = vz[v];
        // Collect the k-ring, checking the strict-minimum condition as we go.
        stamp[v] = v;
        queue.length = 0; queue.push(v);
        let isMin = true;
        let ringSize = 0;
        for (let d = 0; d < k && isMin; d++) {
            next.length = 0;
            for (const u of queue) {
                for (const w of adj[u]) {
                    if (stamp[w] === v) continue;
                    stamp[w] = v;
                    if (vz[w] <= zi) { isMin = false; break; }
                    ringSize++;
                    next.push(w);
                }
                if (!isMin) break;
            }
            const swap = queue; queue = next; next = swap;
        }
        if (!isMin || ringSize === 0) continue;
        const len = Math.hypot(nx[v], ny[v], nz[v]);
        const normalZ = len > 0 ? nz[v] / len : 0;
        if (normalZ >= DOWNWARD_NZ) {
            // Ambiguous or upward-facing: a genuine low point has nothing below it.
            // Start just under the vertex and perturb xy off the lattice so the
            // ray never grazes an edge exactly; an odd hit count means material
            // below (a top-surface dimple), which is dropped.
            rayOrigin.set(vx[v] + 1.123e-5, vy[v] + 2.456e-5, vz[v] - 1e-4);
            raycaster.set(rayOrigin, DOWN);
            if (raycaster.intersectObject(rayMesh, false).length % 2 === 1) continue;
        }
        out.push({
            id: `m${mi++}`,
            source: 'minima' as const,
            contact: new Vector3(vx[v], vy[v], vz[v]),
            baseZ: zi,
            vertexIndex: v,
            seedTriangleId: firstFace[v] < 0 ? 0 : firstFace[v],
        } as DetectedIsland);
    }
    return out;
}
