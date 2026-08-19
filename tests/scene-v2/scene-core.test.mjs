import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import {
  planToDisplay,
  computeWallJoinery,
  splitWallParts,
  partFootprint,
  isSceneV2,
  compileSceneV2,
  sceneClaimPayload,
} from '../../scene-core/scene-core.js';

const approx = (actual, expected, epsilon = 1e-6) => {
  assert.ok(Math.abs(actual - expected) <= epsilon, `${actual} !~ ${expected}`);
};
const approxPoint = (actual, expected, epsilon = 1e-6) => {
  approx(actual[0], expected[0], epsilon);
  approx(actual[1], expected[1], epsilon);
};
const accepted = (status, sources) => ({
  status,
  sources,
  claimHash: 'a'.repeat(64),
  acceptedSourceDigest: 'b'.repeat(64),
  reviewer: {
    actorId: 'reviewer-east',
    runId: '22222222-2222-4222-8222-222222222222',
  },
});

test('planToDisplay maps source plan Z-up to three Y-up', () => {
  assert.deepEqual(planToDisplay([3, 2], 1.5), [3, 1.5, -2]);
});

test('sceneClaimPayload matches the cross-runtime contract fixture', () => {
  const fixture = JSON.parse(
    readFileSync(new URL('./claim-payload-fixture.json', import.meta.url), 'utf8'),
  );
  assert.deepEqual(sceneClaimPayload(fixture.scene, 'wall_a'), fixture.wallClaim);
});

test('L-corner: both walls receive exact mitered corners', () => {
  const wallA = { id: 'a', start: [0, 0], end: [4, 0], thickness: 0.2, height: 3 };
  const wallB = { id: 'b', start: [4, 0], end: [4, 3], thickness: 0.2, height: 3 };
  const joinery = computeWallJoinery([wallA, wallB]);
  const endA = joinery.get('a')[1];
  assert.ok(endA.corners, 'wall A end must be mitered');
  // Inner corner (3.9, 0.1) is on wall A's left (+y); outer (4.1, -0.1) on its right.
  approxPoint(endA.corners.left, [3.9, 0.1]);
  approxPoint(endA.corners.right, [4.1, -0.1]);
  const startB = joinery.get('b')[0];
  assert.ok(startB.corners, 'wall B start must be mitered');
  // Wall B runs +y; its left normal is -x, so left is the inner corner (3.9, 0.1).
  approxPoint(startB.corners.left, [3.9, 0.1]);
  approxPoint(startB.corners.right, [4.1, -0.1]);

  // Footprint of A's single full part carries the miter at its end.
  const parts = splitWallParts(wallA, []);
  assert.equal(parts.length, 1);
  const quad = partFootprint(wallA, parts[0], joinery.get('a'));
  approxPoint(quad[0], [0, 0.1]);
  approxPoint(quad[1], [3.9, 0.1]);
  approxPoint(quad[2], [4.1, -0.1]);
  approxPoint(quad[3], [0, -0.1]);
});

test('T-joint: terminating wall embeds flush into the through wall', () => {
  const through = { id: 'through', start: [0, 0], end: [6, 0], thickness: 0.2, height: 3 };
  const stub = { id: 'stub', start: [3, 0], end: [3, 3], thickness: 0.12, height: 3 };
  const joinery = computeWallJoinery([through, stub]);
  approx(joinery.get('stub')[0].extension, 0.099, 1e-6);
  assert.equal(joinery.get('stub')[0].corners, null);
  // The through wall keeps its plain rectangle.
  assert.equal(joinery.get('through')[0].corners, null);
  approx(joinery.get('through')[0].extension, 0);
});

test('collinear continuation and degree-3 junctions embed instead of mitering', () => {
  const a = { id: 'a', start: [0, 0], end: [3, 0], thickness: 0.12, height: 3 };
  const b = { id: 'b', start: [3, 0], end: [6, 0], thickness: 0.12, height: 3 };
  const c = { id: 'c', start: [3, 0], end: [3, 2], thickness: 0.12, height: 3 };
  const joinery = computeWallJoinery([a, b, c]);
  for (const [wallId, endIndex] of [['a', 1], ['b', 0], ['c', 0]]) {
    const info = joinery.get(wallId)[endIndex];
    assert.equal(info.corners, null, `${wallId} must not miter at a 3-way junction`);
    approx(info.extension, 0.06 - 0.001, 1e-9);
  }
});

test('opening split conserves solid area: wall - door - window', () => {
  const wall = { id: 'w', start: [0, 0], end: [10, 0], thickness: 0.12, height: 3, baseHeight: 0 };
  const door = { id: 'd', type: 'door', hostOffsetM: 2, width: 1, height: 2.1, sillHeight: 0 };
  const win = { id: 'n', type: 'window', hostOffsetM: 6, width: 2, height: 1.5, sillHeight: 0.9 };
  const parts = splitWallParts(wall, [door, win]);
  const kinds = parts.map((part) => part.kind).sort();
  assert.deepEqual(kinds, ['full', 'full', 'full', 'lintel', 'lintel', 'sill']);
  const solidArea = parts.reduce((sum, part) => sum + (part.u1 - part.u0) * part.height, 0);
  approx(solidArea, 10 * 3 - 1 * 2.1 - 2 * 1.5, 1e-9);
  // Lintel over the window sits exactly on the opening head.
  const winLintel = parts.find((part) => part.kind === 'lintel' && part.u0 === 5);
  approx(winLintel.base, 2.4);
  approx(winLintel.height, 0.6);
});

test('compileSceneV2 separates accepted, candidate and rejected', () => {
  const raw = {
    schemaVersion: '2.0',
    dataset: 'unit',
    coordinateFrame: {},
    nodes: {
      level_1: { id: 'level_1', type: 'level', parentId: null, children: [], height: 3 },
      wall_a: {
        id: 'wall_a', type: 'wall', parentId: 'level_1', children: ['door_a'],
        start: [0, 0], end: [6, 0], height: 3, thickness: 0.12,
      },
      door_a: {
        id: 'door_a', type: 'door', parentId: 'wall_a', children: [],
        hostOffsetM: 2, width: 1, height: 2.1, sillHeight: 0,
      },
      wall_candidate: {
        id: 'wall_candidate', type: 'wall', parentId: 'level_1', children: [],
        start: [0, 2], end: [3, 2], height: 3, thickness: 0.12,
      },
      wall_rejected: {
        id: 'wall_rejected', type: 'wall', parentId: 'level_1', children: [],
        start: [0, 4], end: [3, 4], height: 3, thickness: 0.12,
      },
      glass_a: {
        id: 'glass_a', type: 'wall', wallKind: 'glass', parentId: 'level_1', children: ['door_g'],
        start: [0, 6], end: [6, 6], height: 3, thickness: 0.045,
      },
      elevated_head: {
        id: 'elevated_head', type: 'wall', wallKind: 'elevated-band', parentId: 'level_1', children: [],
        start: [1, 8], end: [5, 8], baseHeight: 2.4, height: 0.6, thickness: 0.1,
      },
      door_g: {
        id: 'door_g', type: 'door', parentId: 'glass_a', children: [],
        hostOffsetM: 3, width: 1, height: 2.05, sillHeight: 0,
      },
      item_ok: {
        id: 'item_ok', type: 'item', parentId: 'level_1', children: [],
        category: 'table', center: [2, 1], size: [1.6, 0.75, 0.8], yaw: 0.4,
      },
      item_pending: {
        id: 'item_pending', type: 'item', parentId: 'level_1', children: [],
        category: 'chair', center: [4, 1], size: [0.5, 1, 0.5],
      },
    },
    rootNodeIds: ['level_1'],
    evidence: {
      wall_a: accepted('accepted-measured', [{ type: 'overview' }]),
      door_a: accepted('accepted-measured', [{ type: 'photo' }]),
      glass_a: accepted('accepted-measured', [{ type: 'overview' }]),
      elevated_head: accepted('accepted-measured', [{ type: 'elevation' }]),
      door_g: accepted('accepted-measured', [{ type: 'photo' }]),
      wall_rejected: { status: 'rejected', reason: 'shadow artifact' },
      item_ok: {
        ...accepted('accepted-inferred', [{ type: 'tabletop' }, { type: 'photo' }]),
        reason: 'x',
      },
    },
  };
  for (const nodeId of ['wall_a', 'door_a', 'glass_a', 'elevated_head', 'door_g', 'item_ok']) {
    raw.evidence[nodeId].claimSnapshot = sceneClaimPayload(raw, nodeId);
  }
  assert.ok(isSceneV2(raw));
  const compiled = compileSceneV2(raw);

  const prisms = compiled.structures.filter((s) => s.geometryType === 'prism');
  // wall_a with one door: full + lintel + full = 3 prisms.
  assert.equal(prisms.length, 3);
  assert.ok(prisms.every((s) => s.sourceId === 'wall_a'));

  // Glass wall splits into two panes around the glass door, plus 2 door structures.
  const panes = compiled.structures.filter((s) => s.category === 'glass');
  assert.equal(panes.length, 2);
  assert.equal(compiled.structures.filter((s) => s.category === 'door').length, 2);

  // A measured high facade band renders once, but never enters solid-wall
  // joinery or opening compilation.
  const elevated = compiled.structures.filter((s) => s.category === 'elevated-band');
  assert.equal(elevated.length, 1);
  assert.equal(elevated[0].geometryType, 'segment');
  assert.equal(elevated[0].baseHeight, 2.4);

  // Candidate wall goes to candidates; rejected wall disappears entirely.
  assert.deepEqual(compiled.structureCandidates.map((s) => s.id), ['wall_candidate']);
  assert.ok(!JSON.stringify(compiled).includes('wall_rejected'));

  // Items map acceptance onto deliveryValidation.
  const byId = Object.fromEntries(compiled.objects.map((o) => [o.id, o]));
  assert.equal(byId.item_ok.deliveryValidation.status, 'PASS');
  assert.equal(byId.item_ok.furnitureValidation.evidenceClass, 'accepted-inferred');
  assert.equal(byId.item_pending.deliveryValidation.status, 'REVIEW');
  // Recentering: display + offset restores the raw display frame, and source
  // coordinates round-trip through it. Source (2,1) -> raw display [2, 0, -1].
  const offset = compiled.focusEnvelope.displayOffset;
  approx(byId.item_ok.center[0] + offset[0], 2);
  approx(byId.item_ok.center[1], 0);
  approx(byId.item_ok.center[2] + offset[1], -1);
  assert.ok(compiled.focusEnvelope.width > 5.9);

  // Panels contract: pipeline synthesized, levels present.
  assert.equal(compiled.pipeline.length, 6);
  assert.equal(compiled.levels[0].height, 3);
});

test('compileSceneV2 demotes an accepted claim when hosted geometry changes', () => {
  const raw = {
    schemaVersion: '2.0', dataset: 'stale-host', coordinateFrame: {},
    nodes: {
      level_1: { id: 'level_1', type: 'level', parentId: null, children: ['wall_a'], height: 3 },
      wall_a: { id: 'wall_a', type: 'wall', parentId: 'level_1', children: ['door_a'], start: [0, 0], end: [4, 0], height: 3, thickness: 0.12 },
      door_a: { id: 'door_a', type: 'door', parentId: 'wall_a', children: [], hostOffsetM: 2, width: 0.9, height: 2.1, sillHeight: 0 },
    },
    rootNodeIds: ['level_1'], evidence: {},
  };
  raw.evidence.wall_a = accepted('accepted-measured', [{ type: 'overview' }]);
  raw.evidence.wall_a.claimSnapshot = sceneClaimPayload(raw, 'wall_a');
  raw.nodes.door_a.width = 1.2;
  const compiled = compileSceneV2(raw);
  assert.equal(compiled.structures.length, 0);
  assert.ok(compiled.structureCandidates.some((value) => value.id === 'wall_a'));
});

test('compileSceneV2 refuses legacy accepted labels without a bound claim receipt', () => {
  const raw = {
    schemaVersion: '2.0', dataset: 'legacy-label', coordinateFrame: {},
    nodes: {
      level_1: { id: 'level_1', type: 'level', parentId: null, children: ['wall_a'], height: 3 },
      wall_a: {
        id: 'wall_a', type: 'wall', parentId: 'level_1', children: [],
        start: [0, 0], end: [4, 0], height: 3, thickness: 0.12,
      },
    },
    rootNodeIds: ['level_1'],
    evidence: {
      wall_a: { status: 'accepted-measured', sources: [{ type: 'overview' }] },
    },
  };
  const compiled = compileSceneV2(raw);
  assert.equal(compiled.structures.length, 0);
  assert.deepEqual(compiled.structureCandidates.map((value) => value.id), ['wall_a']);
  assert.ok(compiled.pipeline.some((stage) => stage.status === 'REVIEW'));
});

test('compileSceneV2 keeps explicit presentation-layer inference visible', () => {
  const raw = {
    schemaVersion: '2.0', sceneLayer: 'presentation', dataset: 'presentation', coordinateFrame: {},
    nodes: {
      level_1: { id: 'level_1', type: 'level', parentId: null, children: ['wall_a'], height: 3 },
      wall_a: { id: 'wall_a', type: 'wall', parentId: 'level_1', children: [], start: [0, 0], end: [4, 0], height: 3, thickness: 0.12 },
    },
    rootNodeIds: ['level_1'],
    evidence: { wall_a: { status: 'accepted-inferred', reason: 'presentation-only completion' } },
  };
  const compiled = compileSceneV2(raw);
  assert.ok(compiled.structures.length > 0);
  assert.equal(compiled.structureCandidates.length, 0);
});

test('compileSceneV2 keeps an all-rejected scene under review', () => {
  const raw = {
    schemaVersion: '2.0', dataset: 'rejected-only', coordinateFrame: {},
    nodes: {
      level_1: { id: 'level_1', type: 'level', parentId: null, children: ['wall_rejected'], height: 3 },
      wall_rejected: { id: 'wall_rejected', type: 'wall', parentId: 'level_1', children: [], start: [0, 0], end: [2, 0], height: 3, thickness: 0.12 },
    },
    rootNodeIds: ['level_1'],
    evidence: { wall_rejected: { status: 'rejected', reason: 'not structure' } },
  };
  const compiled = compileSceneV2(raw);
  assert.equal(compiled.structures.length, 0);
  assert.ok(compiled.pipeline.some((stage) => stage.status === 'REVIEW'));
});
