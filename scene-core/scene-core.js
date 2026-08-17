// Semantic Scene V2 compile layer.
//
// Authority model: nodes in SOURCE plan meters (x, y), Z-up (see
// semantic-scene-v2.schema.json). This module derives render geometry:
//   - wall joinery: mitered corners at 2-wall junctions, embed extensions at
//     T/X junctions (replaces the render-only 12-24 mm same-material overlap
//     hack and its audit gate);
//   - hosted openings: doors/windows split the wall solid into full-height
//     jamb parts, lintels and sills - walls have real holes;
//   - the single source->display mapping: display = [x, elevation, -y].
//
// The output is the V1 view-model shape the existing viewer consumes, plus
// prism structures (geometryType 'prism') carrying plan footprints. Pure
// math, no Three.js imports, so node:test can execute it directly.

const JUNCTION_TOLERANCE_M = 0.02;
const MITER_MIN_TURN_RAD = (8 * Math.PI) / 180;
const MITER_MAX_TURN_RAD = (172 * Math.PI) / 180;
const T_JOINT_SEARCH_MARGIN_M = 0.05;
const MIN_PART_HEIGHT_M = 0.02;
const EMBED_EPSILON_M = 0.001;

// --- source plan vector helpers -------------------------------------------

const sub = (a, b) => [a[0] - b[0], a[1] - b[1]];
const add = (a, b) => [a[0] + b[0], a[1] + b[1]];
const scale = (a, s) => [a[0] * s, a[1] * s];
const dot = (a, b) => a[0] * b[0] + a[1] * b[1];
const cross = (a, b) => a[0] * b[1] - a[1] * b[0];
const norm = (a) => Math.hypot(a[0], a[1]);
const normalize = (a) => {
  const length = norm(a);
  if (length < 1e-9) throw new Error('degenerate direction');
  return [a[0] / length, a[1] / length];
};
const perp = (a) => [-a[1], a[0]];

function lineIntersect(pointA, dirA, pointB, dirB) {
  const denominator = cross(dirA, dirB);
  if (Math.abs(denominator) < 1e-9) return null;
  const t = cross(sub(pointB, pointA), dirB) / denominator;
  return add(pointA, scale(dirA, t));
}

export function planToDisplay(point, elevation = 0) {
  return [point[0], elevation, -point[1]];
}

export function displayYawFromSource(yaw) {
  // With display = [x, e, -y], a source CCW yaw maps 1:1 onto Three rotation.y.
  return yaw;
}

function wallDirection(wall) {
  return normalize(sub(wall.end, wall.start));
}

function wallLength(wall) {
  return norm(sub(wall.end, wall.start));
}

// --- joinery ---------------------------------------------------------------

// Returns Map(wallId -> [endInfo0, endInfo1]) where each endInfo is
// { extension: number, corners: { left:[x,y], right:[x,y] } | null }.
// 'left' is +perp(direction start->end) at half thickness.
export function computeWallJoinery(walls) {
  const joinery = new Map(walls.map((wall) => [wall.id, [
    { extension: 0, corners: null },
    { extension: 0, corners: null },
  ]]));
  if (!walls.length) return joinery;

  // Cluster endpoints.
  const clusters = [];
  const findCluster = (point) => clusters.find((cluster) => norm(sub(cluster.point, point)) <= JUNCTION_TOLERANCE_M);
  for (const wall of walls) {
    [wall.start, wall.end].forEach((point, endIndex) => {
      let cluster = findCluster(point);
      if (!cluster) {
        cluster = { point: [...point], members: [] };
        clusters.push(cluster);
      }
      cluster.members.push({ wall, endIndex });
    });
  }

  const outwardIntoBody = (member) => {
    const direction = wallDirection(member.wall);
    return member.endIndex === 0 ? direction : scale(direction, -1);
  };

  for (const cluster of clusters) {
    const { members } = cluster;
    if (members.length < 2) continue;
    if (members.length === 2) {
      const [a, b] = members;
      const dirA = outwardIntoBody(a);
      const dirB = outwardIntoBody(b);
      const turn = Math.acos(Math.min(1, Math.max(-1, dot(dirA, dirB))));
      if (turn > MITER_MIN_TURN_RAD && turn < MITER_MAX_TURN_RAD) {
        const applied = applyMiter(cluster.point, a, dirA, b, dirB, joinery);
        if (applied) continue;
      }
    }
    // Degree >= 3, near-collinear pairs, or clamped miters: embed each end
    // into the junction by the largest other half-thickness. The overlap is
    // hidden inside the neighbouring solid.
    for (const member of members) {
      const otherHalf = Math.max(...members
        .filter((other) => other !== member)
        .map((other) => (other.wall.thickness || 0.12) / 2));
      joinery.get(member.wall.id)[member.endIndex].extension = Math.max(
        joinery.get(member.wall.id)[member.endIndex].extension,
        otherHalf - EMBED_EPSILON_M,
      );
    }
  }

  // T-joints: a wall end touching another wall's interior extends flush to
  // that wall's far face.
  for (const wall of walls) {
    [0, 1].forEach((endIndex) => {
      const info = joinery.get(wall.id)[endIndex];
      if (info.corners || info.extension > 0) return;
      const endpoint = endIndex === 0 ? wall.start : wall.end;
      const direction = wallDirection(wall);
      const extendDirection = endIndex === 0 ? scale(direction, -1) : direction;
      for (const other of walls) {
        if (other.id === wall.id) continue;
        const otherDirection = wallDirection(other);
        const hit = lineIntersect(endpoint, extendDirection, other.start, otherDirection);
        if (!hit) continue;
        const along = dot(sub(hit, other.start), otherDirection);
        const otherLen = wallLength(other);
        if (along < T_JOINT_SEARCH_MARGIN_M || along > otherLen - T_JOINT_SEARCH_MARGIN_M) continue;
        const distance = dot(sub(hit, endpoint), extendDirection);
        const otherHalf = (other.thickness || 0.12) / 2;
        if (Math.abs(distance) > otherHalf + T_JOINT_SEARCH_MARGIN_M) continue;
        info.extension = Math.max(0, distance + otherHalf - EMBED_EPSILON_M);
        break;
      }
    });
  }
  return joinery;
}

function applyMiter(junction, memberA, dirA, memberB, dirB, joinery) {
  const halfA = (memberA.wall.thickness || 0.12) / 2;
  const halfB = (memberB.wall.thickness || 0.12) / 2;
  const bisector = normalize(add(dirA, dirB));
  const corners = [];
  for (const side of [1, -1]) {
    const sigmaA = dot(perp(dirA), bisector) >= 0 ? side : -side;
    const sigmaB = dot(perp(dirB), bisector) >= 0 ? side : -side;
    const baseA = add(junction, scale(perp(dirA), sigmaA * halfA));
    const baseB = add(junction, scale(perp(dirB), sigmaB * halfB));
    const point = lineIntersect(baseA, dirA, baseB, dirB);
    if (!point || norm(sub(point, junction)) > 4 * (halfA + halfB) + Math.max(halfA, halfB)) return false;
    corners.push(point);
  }
  for (const member of [memberA, memberB]) {
    const wallNormal = perp(wallDirection(member.wall));
    const endpoint = member.endIndex === 0 ? member.wall.start : member.wall.end;
    const assigned = { left: null, right: null };
    for (const corner of corners) {
      if (dot(sub(corner, endpoint), wallNormal) >= 0) assigned.left = corner;
      else assigned.right = corner;
    }
    if (!assigned.left || !assigned.right) return false;
    joinery.get(member.wall.id)[member.endIndex].corners = assigned;
  }
  return true;
}

// --- opening split ---------------------------------------------------------

// Returns solid parts: { kind, u0, u1, base, height, atStart, atEnd }.
export function splitWallParts(wall, openings) {
  const length = wallLength(wall);
  const wallBase = wall.baseHeight || 0;
  const wallTop = wallBase + wall.height;
  const sorted = openings
    .map((opening) => {
      const center = opening.hostOffsetM ?? 0;
      const half = opening.width / 2;
      return {
        opening,
        u0: Math.max(0, center - half),
        u1: Math.min(length, center + half),
        sill: Math.max(wallBase, wallBase + (opening.sillHeight || 0)),
        top: Math.min(wallTop, wallBase + (opening.sillHeight || 0) + opening.height),
      };
    })
    .filter((entry) => entry.u1 - entry.u0 > 0.01)
    .sort((a, b) => a.u0 - b.u0);

  const parts = [];
  let cursor = 0;
  for (const entry of sorted) {
    if (entry.u0 - cursor > 0.01) {
      parts.push({ kind: 'full', u0: cursor, u1: entry.u0, base: wallBase, height: wall.height });
    }
    if (wallTop - entry.top > MIN_PART_HEIGHT_M) {
      parts.push({ kind: 'lintel', u0: entry.u0, u1: entry.u1, base: entry.top, height: wallTop - entry.top });
    }
    if (entry.sill - wallBase > MIN_PART_HEIGHT_M) {
      parts.push({ kind: 'sill', u0: entry.u0, u1: entry.u1, base: wallBase, height: entry.sill - wallBase });
    }
    cursor = Math.max(cursor, entry.u1);
  }
  if (length - cursor > 0.01) {
    parts.push({ kind: 'full', u0: cursor, u1: length, base: wallBase, height: wall.height });
  }
  if (!parts.length) {
    parts.push({ kind: 'full', u0: 0, u1: length, base: wallBase, height: wall.height });
  }
  for (const part of parts) {
    part.atStart = part.u0 <= 0.011;
    part.atEnd = part.u1 >= length - 0.011;
  }
  return parts;
}

// Plan footprint quad (source coordinates) for one solid part, applying end
// joinery. Order: startLeft, endLeft, endRight, startRight.
export function partFootprint(wall, part, endInfos) {
  const direction = wallDirection(wall);
  const half = (wall.thickness || 0.12) / 2;
  const left = scale(perp(direction), half);
  const pointAt = (u) => add(wall.start, scale(direction, u));

  let u0 = part.u0;
  let u1 = part.u1;
  if (part.atStart) u0 -= endInfos[0].extension;
  if (part.atEnd) u1 += endInfos[1].extension;

  let startLeft = add(pointAt(u0), left);
  let startRight = sub(pointAt(u0), left);
  let endLeft = add(pointAt(u1), left);
  let endRight = sub(pointAt(u1), left);
  if (part.atStart && endInfos[0].corners) {
    startLeft = endInfos[0].corners.left;
    startRight = endInfos[0].corners.right;
  }
  if (part.atEnd && endInfos[1].corners) {
    endLeft = endInfos[1].corners.left;
    endRight = endInfos[1].corners.right;
  }
  return [startLeft, endLeft, endRight, startRight];
}

// --- compile ---------------------------------------------------------------

export function isSceneV2(raw) {
  return raw && raw.schemaVersion === '2.0' && raw.nodes && typeof raw.nodes === 'object';
}

function statusOf(raw, nodeId) {
  return raw.evidence?.[nodeId]?.status || 'candidate';
}

function isAccepted(status) {
  return status === 'accepted-measured' || status === 'accepted-inferred';
}

function hostedOpenings(raw, wall) {
  return (wall.children || [])
    .map((childId) => raw.nodes[childId])
    .filter((child) => child && ['door', 'window', 'opening'].includes(child.type))
    .filter((child) => statusOf(raw, child.id) !== 'rejected');
}

function segmentStructure(raw, node, wall, category) {
  const direction = wallDirection(wall);
  const center = node.hostOffsetM ?? 0;
  const u0 = center - node.width / 2;
  const u1 = center + node.width / 2;
  const start = add(wall.start, scale(direction, u0));
  const end = add(wall.start, scale(direction, u1));
  const base = (wall.baseHeight || 0) + (node.sillHeight || 0);
  return {
    id: node.id,
    category,
    geometryType: 'segment',
    start: planToDisplay(start, base),
    end: planToDisplay(end, base),
    height: node.height,
    baseHeight: base,
    thickness: Math.max(0.03, (wall.thickness || 0.12) - 0.02),
    material: node.material || {},
    decision: { status: statusOf(raw, node.id) },
  };
}

function freeSegmentStructure(raw, node) {
  const free = node.freeSegment;
  const base = node.sillHeight || 0;
  return {
    id: node.id,
    category: node.type === 'opening' ? 'window' : node.type,
    geometryType: 'segment',
    start: planToDisplay(free.start, base),
    end: planToDisplay(free.end, base),
    height: node.height,
    baseHeight: base,
    thickness: free.thickness || 0.06,
    material: node.material || {},
    decision: { status: statusOf(raw, node.id) },
  };
}

export function compileSceneV2(raw) {
  const nodes = Object.values(raw.nodes);
  const structures = [];
  const structureCandidates = [];
  const objects = [];
  const levels = [];

  const solidWalls = nodes.filter((node) => node.type === 'wall'
    && (node.wallKind || 'solid') === 'solid'
    && isAccepted(statusOf(raw, node.id)));
  const joinery = computeWallJoinery(solidWalls);

  for (const node of nodes) {
    const status = statusOf(raw, node.id);
    if (status === 'rejected') continue;

    if (node.type === 'level') {
      levels.push({ id: node.id, name: node.name || node.id, height: node.height, elevation: node.elevation || 0 });
    } else if (node.type === 'wall') {
      const openings = hostedOpenings(raw, node);
      if ((node.wallKind || 'solid') === 'elevated-band') {
        const target = isAccepted(status) ? structures : structureCandidates;
        target.push({
          id: node.id,
          sourceId: node.id,
          category: 'elevated-band',
          geometryType: 'segment',
          start: planToDisplay(node.start, node.baseHeight || 0),
          end: planToDisplay(node.end, node.baseHeight || 0),
          height: node.height,
          baseHeight: node.baseHeight || 0,
          thickness: node.thickness || 0.10,
          material: node.material || {},
          decision: { status },
        });
      } else if ((node.wallKind || 'solid') === 'glass') {
        // Glass panes render as V1 glass segments split around openings.
        const direction = wallDirection(node);
        const length = wallLength(node);
        let cursor = 0;
        const holes = openings
          .map((opening) => [(opening.hostOffsetM ?? 0) - opening.width / 2, (opening.hostOffsetM ?? 0) + opening.width / 2])
          .sort((a, b) => a[0] - b[0]);
        const paneTargets = isAccepted(status) ? structures : structureCandidates;
        const emitPane = (u0, u1) => {
          if (u1 - u0 < 0.02) return;
          paneTargets.push({
            id: `${node.id}::pane${paneTargets.length}`,
            sourceId: node.id,
            category: 'glass',
            geometryType: 'segment',
            start: planToDisplay(add(node.start, scale(direction, u0)), node.baseHeight || 0),
            end: planToDisplay(add(node.start, scale(direction, u1)), node.baseHeight || 0),
            height: node.height,
            baseHeight: node.baseHeight || 0,
            thickness: node.thickness || 0.045,
            material: node.material || {},
            decision: { status },
          });
        };
        for (const [u0, u1] of holes) {
          emitPane(cursor, Math.max(cursor, u0));
          cursor = Math.max(cursor, u1);
        }
        emitPane(cursor, length);
      } else if (isAccepted(status)) {
        const endInfos = joinery.get(node.id) || [{ extension: 0, corners: null }, { extension: 0, corners: null }];
        const parts = splitWallParts(node, openings);
        parts.forEach((part, index) => {
          const footprint = partFootprint(node, part, endInfos).map((point) => {
            const display = planToDisplay(point, 0);
            return [display[0], display[2]];
          });
          structures.push({
            id: `${node.id}::part${index}`,
            sourceId: node.id,
            category: 'wall',
            geometryType: 'prism',
            footprint,
            baseHeight: part.base,
            height: part.height,
            material: node.material || {},
            decision: { status },
          });
        });
      } else {
        structureCandidates.push({
          id: node.id,
          category: 'wall',
          geometryType: 'segment',
          start: planToDisplay(node.start, node.baseHeight || 0),
          end: planToDisplay(node.end, node.baseHeight || 0),
          height: node.height,
          baseHeight: node.baseHeight || 0,
          thickness: node.thickness,
          material: node.material || {},
          decision: { status },
        });
      }
      // Doors and windows render on top of the wall hole in both kinds.
      for (const opening of openings) {
        if (opening.type === 'opening') continue; // plain hole, nothing to draw
        const target = isAccepted(statusOf(raw, opening.id)) ? structures : structureCandidates;
        target.push(segmentStructure(raw, opening, node, opening.type));
      }
    } else if (['door', 'window', 'opening'].includes(node.type)) {
      const parent = raw.nodes[node.parentId ?? ''];
      if (parent && parent.type === 'wall') continue; // handled with its wall
      if (node.freeSegment && node.type !== 'opening') {
        const target = isAccepted(status) ? structures : structureCandidates;
        target.push(freeSegmentStructure(raw, node));
      }
    } else if (node.type === 'column') {
      const target = isAccepted(status) ? structures : structureCandidates;
      target.push({
        id: node.id,
        category: 'column',
        geometryType: 'rectangle',
        center: planToDisplay(node.center, 0),
        size: node.size,
        yaw: displayYawFromSource(node.yaw || 0),
        height: node.height,
        baseHeight: node.baseHeight || 0,
        material: node.material || {},
        decision: { status },
      });
    } else if (node.type === 'slab' || node.type === 'ceiling') {
      if (!isAccepted(status)) continue;
      structures.push({
        id: node.id,
        category: node.type === 'slab' ? 'floor-zone' : 'ceiling-zone',
        geometryType: 'polygon',
        points: node.polygon.map((point) => planToDisplay(point, 0)),
        height: node.type === 'slab' ? (node.thickness || 0.05) : undefined,
        baseHeight: node.elevation || 0,
        material: node.material || {},
        decision: { status },
      });
    } else if (node.type === 'item') {
      objects.push({
        id: node.id,
        category: node.category,
        center: planToDisplay(node.center, node.elevation || 0),
        yaw: displayYawFromSource(node.yaw || 0),
        size: [...node.size],
        color: node.color || '#b9b4a8',
        confidence: node.confidence ?? 0.85,
        layout: node.layout,
        deliveryValidation: { status: isAccepted(status) ? 'PASS' : 'REVIEW' },
        furnitureValidation: { evidenceClass: status },
      });
    }
    // zone/scan/guide: topology and evidence metadata; not rendered here.
  }

  // The viewer frames and orbits around the display origin, so recenter the
  // compiled view model on the plan centroid. displayOffset records the shift:
  // source_x = display_x + offset[0], source_y = -(display_z + offset[1]).
  // meta.displayOffset pins the shift so external artifacts (point clouds)
  // stay registered while the scene geometry evolves.
  const envelope = recenterViewModel(structures, structureCandidates, objects, raw.meta?.displayOffset);

  const pipeline = raw.meta?.pipeline?.length ? raw.meta.pipeline : synthesizePipeline(raw);
  return {
    schemaVersion: raw.schemaVersion,
    dataset: raw.dataset,
    source: raw.meta?.source || { samplePointCount: 0 },
    pipeline,
    levels: levels.length ? levels : [{ id: 'level_unknown', name: 'Level', height: 3.05, elevation: 0 }],
    walls: [],
    structures,
    structureCandidates,
    derivedGeometry: raw.meta?.derivedGeometry || [],
    objects,
    qualityLoops: raw.review?.qualityLoops || [],
    photos: raw.meta?.photos || [],
    cameraPath: raw.meta?.cameraPath || [],
    artifacts: raw.meta?.artifacts,
    focusEnvelope: raw.meta?.focusEnvelope || envelope,
  };
}

function recenterViewModel(structures, structureCandidates, objects, pinnedOffset) {
  const bounds = { minX: Infinity, maxX: -Infinity, minZ: Infinity, maxZ: -Infinity };
  const feed = (x, z) => {
    bounds.minX = Math.min(bounds.minX, x);
    bounds.maxX = Math.max(bounds.maxX, x);
    bounds.minZ = Math.min(bounds.minZ, z);
    bounds.maxZ = Math.max(bounds.maxZ, z);
  };
  const everyStructure = [...structures, ...structureCandidates];
  for (const structure of everyStructure) {
    if (structure.start) feed(structure.start[0], structure.start[2]);
    if (structure.end) feed(structure.end[0], structure.end[2]);
    if (structure.center) feed(structure.center[0], structure.center[2]);
    for (const point of structure.footprint || []) feed(point[0], point[1]);
    for (const point of structure.points || []) feed(point[0], point[2]);
  }
  for (const object of objects) feed(object.center[0], object.center[2]);
  if (!Number.isFinite(bounds.minX)) {
    return { width: 10, depth: 10, displayOffset: pinnedOffset || [0, 0] };
  }
  const offsetX = pinnedOffset ? pinnedOffset[0] : (bounds.minX + bounds.maxX) / 2;
  const offsetZ = pinnedOffset ? pinnedOffset[1] : (bounds.minZ + bounds.maxZ) / 2;
  const shift3 = (point) => { point[0] -= offsetX; point[2] -= offsetZ; };
  for (const structure of everyStructure) {
    if (structure.start) shift3(structure.start);
    if (structure.end) shift3(structure.end);
    if (structure.center) shift3(structure.center);
    for (const point of structure.footprint || []) { point[0] -= offsetX; point[1] -= offsetZ; }
    for (const point of structure.points || []) shift3(point);
  }
  for (const object of objects) shift3(object.center);
  return {
    width: bounds.maxX - bounds.minX,
    depth: bounds.maxZ - bounds.minZ,
    displayOffset: [offsetX, offsetZ],
  };
}

function synthesizePipeline(raw) {
  const statuses = Object.keys(raw.nodes)
    .filter((nodeId) => raw.nodes[nodeId].type !== 'level')
    .map((nodeId) => statusOf(raw, nodeId));
  const unresolved = statuses.filter((status) => status === 'candidate').length;
  const accepted = statuses.filter((status) => isAccepted(status)).length;
  const stageStatus = unresolved || accepted === 0 ? 'REVIEW' : 'PASS';
  return [
    { id: 'ingest', label: '点云与相机注册', status: 'PASS' },
    { id: 'seed', label: 'AI 候选复核', status: stageStatus },
    { id: 'objects', label: '全平面目视检查', status: stageStatus },
    { id: 'structures', label: '结构证据门禁', status: stageStatus },
    { id: 'assets', label: '参数化场景生成', status: 'PASS' },
    { id: 'author', label: '三维补全与终审', status: stageStatus },
  ];
}
