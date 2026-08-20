#!/usr/bin/env python3
"""Global constraint-graph joint adjustment for wall proposals.

Consumes a structural-proposals document (or an equivalent wall list) plus an
optional authority Scene V2 whose accepted walls act as fixed anchors.  Axis
family alignment, collinearity, corner closure, thickness families and a
per-wall data term become soft constraints solved jointly with
``scipy.optimize.least_squares``.

The output is a reviewable per-wall displacement table plus adjusted geometry.
This module only proposes: it never writes the scene, and the result stays
``ADJUSTMENT_PROPOSED`` until an Agent/reviewer transaction accepts it.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
import json
import math
from pathlib import Path

import numpy as np
from scipy.optimize import least_squares


AXIS_TOLERANCE_DEG = 5.0
COLLINEAR_OFFSET_MAX_M = 0.10
COLLINEAR_ANGLE_MAX_DEG = 3.0
CORNER_GAP_MAX_M = 0.35
THICKNESS_FAMILY_RANGE_M = (0.08, 0.15)
THICKNESS_FAMILY_GAP_M = 0.02

# The data term is deliberately weaker than the structural constraints: the
# whole point of the joint solve is trusting global consistency over each
# wall's noisy individual fit, while still forbidding large excursions.
W_DATA_PERP = 1.0
W_DATA_ALONG = 0.5
W_AXIS = 8.0
W_COLLINEAR = 2.0
W_CORNER = 6.0
W_THICKNESS_DATA = 2.0
W_THICKNESS_FAMILY = 3.0


def _angle_delta(a: float, b: float) -> float:
    delta = abs((a - b) % math.pi)
    return min(delta, math.pi - delta)


def _signed_angle_delta_deg(after_deg: float, before_deg: float) -> float:
    return (after_deg - before_deg + 90.0) % 180.0 - 90.0


@dataclass
class Wall:
    wall_id: str
    start: np.ndarray
    end: np.ndarray
    thickness: float
    support: int
    residual_p90: float
    anchored: bool
    family_angle: float | None = None
    family_id: str | None = None
    constraints: list[str] = field(default_factory=list)

    @property
    def vector(self) -> np.ndarray:
        return self.end - self.start

    @property
    def length(self) -> float:
        return float(np.linalg.norm(self.vector))

    @property
    def angle(self) -> float:
        return math.atan2(float(self.vector[1]), float(self.vector[0])) % math.pi

    @property
    def unit(self) -> np.ndarray:
        return np.asarray([math.cos(self.angle), math.sin(self.angle)], dtype=np.float64)

    @property
    def normal(self) -> np.ndarray:
        unit = self.unit
        return np.asarray([-unit[1], unit[0]], dtype=np.float64)


def _wall_from_row(row: dict, fallback_id: str) -> Wall:
    line = row.get("rawCenterline")
    if isinstance(line, dict):
        start_raw, end_raw = line.get("start"), line.get("end")
    else:
        start_raw, end_raw = row.get("start"), row.get("end")
    start = np.asarray(start_raw, dtype=np.float64)
    end = np.asarray(end_raw, dtype=np.float64)
    if start.shape != (2,) or end.shape != (2,) or not np.isfinite(start).all() or not np.isfinite(end).all():
        raise ValueError(f"wall {fallback_id} has invalid endpoints")
    if float(np.linalg.norm(end - start)) < 0.05:
        raise ValueError(f"wall {fallback_id} is too short to adjust")
    thickness = row.get("thicknessM", row.get("thickness", 0.12))
    return Wall(
        wall_id=str(row.get("id") or fallback_id),
        start=start,
        end=end,
        thickness=float(thickness),
        support=max(1, int(row.get("supportPointCount", 1))),
        residual_p90=float(row.get("fitResidualP90M", 0.02)),
        anchored=False,
    )


def load_proposal_walls(document: dict) -> list[Wall]:
    rows = document.get("wallCandidates")
    if rows is None:
        rows = document.get("walls")
    if not isinstance(rows, list):
        raise ValueError("proposals document has neither 'wallCandidates' nor 'walls'")
    return [_wall_from_row(row, f"wall-{index + 1:03d}") for index, row in enumerate(rows)]


def load_anchor_walls(scene: dict) -> list[Wall]:
    evidence = scene.get("evidence") or {}
    anchors: list[Wall] = []
    for node_id, node in (scene.get("nodes") or {}).items():
        if not isinstance(node, dict) or node.get("type") != "wall":
            continue
        status = str((evidence.get(node_id) or {}).get("status", ""))
        if not status.startswith("accepted"):
            continue
        anchors.append(
            Wall(
                wall_id=str(node_id),
                start=np.asarray(node["start"], dtype=np.float64),
                end=np.asarray(node["end"], dtype=np.float64),
                thickness=float(node.get("thickness", 0.12)),
                support=1_000_000,
                residual_p90=0.0,
                anchored=True,
            )
        )
    return anchors


def _assign_axis_families(walls: list[Wall], tolerance_deg: float) -> list[dict[str, object]]:
    """Snap walls to a dominant orthogonal frame derived from all walls.

    The frame angle is the weighted 4-theta circular mean, so two nearly
    perpendicular families share one exact 90-degree relationship; walls
    outside the tolerance simply do not participate in axis constraints.
    """
    if not walls:
        return []
    sin4 = cos4 = 0.0
    for wall in walls:
        weight = wall.length * math.sqrt(wall.support)
        sin4 += weight * math.sin(4.0 * wall.angle)
        cos4 += weight * math.cos(4.0 * wall.angle)
    if math.hypot(sin4, cos4) < 1e-12:
        frame = 0.0
    else:
        frame = (0.25 * math.atan2(sin4, cos4)) % (math.pi / 2.0)
    family_angles = [frame % math.pi, (frame + math.pi / 2.0) % math.pi]
    families = [
        {"id": f"axis-{index + 1}", "angleDeg": round(math.degrees(angle), 4), "memberWallIds": []}
        for index, angle in enumerate(family_angles)
    ]
    tolerance = math.radians(tolerance_deg)
    for wall in walls:
        deltas = [_angle_delta(wall.angle, angle) for angle in family_angles]
        best = int(np.argmin(deltas))
        if deltas[best] <= tolerance:
            wall.family_angle = family_angles[best]
            wall.family_id = str(families[best]["id"])
            families[best]["memberWallIds"].append(wall.wall_id)
    return families


def _thickness_groups(walls: list[Wall]) -> list[dict[str, object]]:
    lo, hi = THICKNESS_FAMILY_RANGE_M
    eligible = [wall for wall in walls if lo <= wall.thickness <= hi]
    eligible.sort(key=lambda wall: wall.thickness)
    groups: list[list[Wall]] = []
    for wall in eligible:
        if groups and wall.thickness - groups[-1][-1].thickness <= THICKNESS_FAMILY_GAP_M:
            groups[-1].append(wall)
        else:
            groups.append([wall])
    result: list[dict[str, object]] = []
    for group in groups:
        if len(group) < 2:
            continue
        median = float(np.median([wall.thickness for wall in group]))
        result.append({"medianM": median, "walls": group})
    return result


def adjust_walls(
    free_walls: list[Wall],
    anchors: list[Wall] | None = None,
    *,
    axis_tolerance_deg: float = AXIS_TOLERANCE_DEG,
    corner_gap_max_m: float = CORNER_GAP_MAX_M,
) -> dict[str, object]:
    anchors = list(anchors or [])
    all_walls = free_walls + anchors
    families = _assign_axis_families(all_walls, axis_tolerance_deg)
    thickness_groups = _thickness_groups(all_walls)

    # Constraint discovery happens on the pre-adjustment geometry so the
    # constraint graph itself stays deterministic and reviewable.
    collinear_pairs: list[tuple[Wall, Wall, np.ndarray]] = []
    for i, first in enumerate(all_walls):
        for second in all_walls[i + 1:]:
            if first.anchored and second.anchored:
                continue
            if first.family_id is None or first.family_id != second.family_id:
                continue
            if math.degrees(_angle_delta(first.angle, second.angle)) > COLLINEAR_ANGLE_MAX_DEG:
                continue
            assert first.family_angle is not None
            family_normal = np.asarray(
                [-math.sin(first.family_angle), math.cos(first.family_angle)], dtype=np.float64
            )
            offset_first = float(np.dot((first.start + first.end) * 0.5, family_normal))
            offset_second = float(np.dot((second.start + second.end) * 0.5, family_normal))
            if abs(offset_first - offset_second) >= COLLINEAR_OFFSET_MAX_M:
                continue
            collinear_pairs.append((first, second, family_normal))
            first.constraints.append(f"collinear:{second.wall_id}")
            second.constraints.append(f"collinear:{first.wall_id}")

    corner_pairs: list[tuple[Wall, int, Wall, int]] = []
    for i, first in enumerate(all_walls):
        for second in all_walls[i + 1:]:
            if first.anchored and second.anchored:
                continue
            for end_first in (0, 1):
                for end_second in (0, 1):
                    point_first = first.start if end_first == 0 else first.end
                    point_second = second.start if end_second == 0 else second.end
                    if float(np.linalg.norm(point_first - point_second)) < corner_gap_max_m:
                        corner_pairs.append((first, end_first, second, end_second))
                        first.constraints.append(f"corner:{second.wall_id}")
                        second.constraints.append(f"corner:{first.wall_id}")

    for wall in all_walls:
        if wall.family_id is not None and not wall.anchored:
            wall.constraints.append(f"axis:{wall.family_id}")
    for group_index, group in enumerate(thickness_groups):
        for wall in group["walls"]:
            if not wall.anchored:
                wall.constraints.append(f"thickness-family:group-{group_index + 1}")

    variable_index: dict[int, int] = {id(wall): index * 5 for index, wall in enumerate(free_walls)}
    x0 = np.concatenate(
        [np.asarray([*wall.start, *wall.end, wall.thickness], dtype=np.float64) for wall in free_walls]
    ) if free_walls else np.empty(0)

    def endpoint(x: np.ndarray, wall: Wall, which: int) -> np.ndarray:
        base = variable_index.get(id(wall))
        if base is None:
            return wall.start if which == 0 else wall.end
        return x[base:base + 2] if which == 0 else x[base + 2:base + 4]

    def thickness_of(x: np.ndarray, wall: Wall) -> float:
        base = variable_index.get(id(wall))
        return wall.thickness if base is None else float(x[base + 4])

    def residuals(x: np.ndarray) -> np.ndarray:
        rows: list[float] = []
        for wall in free_walls:
            start = endpoint(x, wall, 0)
            end = endpoint(x, wall, 1)
            normal0 = wall.normal
            unit0 = wall.unit
            data_weight = W_DATA_PERP * min(3.0, 0.03 / (wall.residual_p90 + 0.01))
            rows.append(data_weight * float(np.dot(normal0, start - wall.start)))
            rows.append(data_weight * float(np.dot(normal0, end - wall.end)))
            rows.append(W_DATA_ALONG * float(np.dot(unit0, start - wall.start)))
            rows.append(W_DATA_ALONG * float(np.dot(unit0, end - wall.end)))
            rows.append(W_THICKNESS_DATA * (thickness_of(x, wall) - wall.thickness))
            if wall.family_angle is not None:
                family_normal = np.asarray(
                    [-math.sin(wall.family_angle), math.cos(wall.family_angle)], dtype=np.float64
                )
                axis_weight = max(2.0, W_AXIS * math.sqrt(min(1.0, wall.support / 1000.0)))
                rows.append(axis_weight * float(np.dot(family_normal, end - start)))
        for first, second, family_normal in collinear_pairs:
            mid_first = (endpoint(x, first, 0) + endpoint(x, first, 1)) * 0.5
            mid_second = (endpoint(x, second, 0) + endpoint(x, second, 1)) * 0.5
            rows.append(W_COLLINEAR * float(np.dot(family_normal, mid_first - mid_second)))
        for first, end_first, second, end_second in corner_pairs:
            gap = endpoint(x, first, end_first) - endpoint(x, second, end_second)
            rows.append(W_CORNER * float(gap[0]))
            rows.append(W_CORNER * float(gap[1]))
        for group in thickness_groups:
            for wall in group["walls"]:
                if not wall.anchored:
                    rows.append(W_THICKNESS_FAMILY * (thickness_of(x, wall) - float(group["medianM"])))
        return np.asarray(rows, dtype=np.float64)

    if free_walls:
        solution = least_squares(residuals, x0, method="trf")
        x_final = solution.x
        converged = bool(solution.status > 0)
        cost_before = float(np.sum(residuals(x0) ** 2))
        cost_after = float(2.0 * solution.cost)
    else:
        x_final = x0
        converged = True
        cost_before = cost_after = 0.0

    def corner_gaps(x: np.ndarray) -> list[float]:
        return [
            float(np.linalg.norm(endpoint(x, first, end_first) - endpoint(x, second, end_second)))
            for first, end_first, second, end_second in corner_pairs
        ]

    gaps_before = corner_gaps(x0)
    gaps_after = corner_gaps(x_final)

    wall_rows: list[dict[str, object]] = []
    deltas: list[float] = []
    for wall in all_walls:
        start_after = endpoint(x_final, wall, 0)
        end_after = endpoint(x_final, wall, 1)
        thickness_after = thickness_of(x_final, wall)
        angle_before = math.degrees(wall.angle)
        vector_after = end_after - start_after
        angle_after = math.degrees(math.atan2(float(vector_after[1]), float(vector_after[0])) % math.pi)
        delta_start = float(np.linalg.norm(start_after - wall.start))
        delta_end = float(np.linalg.norm(end_after - wall.end))
        if not wall.anchored:
            deltas.extend((delta_start, delta_end))
        wall_rows.append(
            {
                "wallId": wall.wall_id,
                "anchored": wall.anchored,
                "axisFamilyId": wall.family_id,
                "before": {
                    "start": [round(float(v), 6) for v in wall.start],
                    "end": [round(float(v), 6) for v in wall.end],
                    "thicknessM": round(wall.thickness, 5),
                    "angleDeg": round(angle_before, 4),
                },
                "after": {
                    "start": [round(float(v), 6) for v in start_after],
                    "end": [round(float(v), 6) for v in end_after],
                    "thicknessM": round(float(thickness_after), 5),
                    "angleDeg": round(angle_after, 4),
                },
                "deltaStartM": round(delta_start, 5),
                "deltaEndM": round(delta_end, 5),
                "deltaAngleDeg": round(_signed_angle_delta_deg(angle_after, angle_before), 4),
                "deltaThicknessM": round(float(thickness_after) - wall.thickness, 5),
                "constraints": sorted(set(wall.constraints)),
            }
        )

    return {
        "schemaVersion": 1,
        "kind": "wall-graph-adjustment",
        "status": "ADJUSTMENT_PROPOSED",
        "axisFamilies": families,
        "constraintCounts": {
            "axis": sum(1 for wall in free_walls if wall.family_angle is not None),
            "collinear": len(collinear_pairs),
            "corner": len(corner_pairs),
            "thicknessGroups": len(thickness_groups),
        },
        "walls": wall_rows,
        "summary": {
            "freeWallCount": len(free_walls),
            "anchorWallCount": len(anchors),
            "converged": converged,
            "solverCostBefore": round(cost_before, 6),
            "solverCostAfter": round(cost_after, 6),
            "maxEndpointDeltaM": round(max(deltas), 5) if deltas else 0.0,
            "meanEndpointDeltaM": round(float(np.mean(deltas)), 5) if deltas else 0.0,
            "maxCornerGapBeforeM": round(max(gaps_before), 5) if gaps_before else 0.0,
            "maxCornerGapAfterM": round(max(gaps_after), 5) if gaps_after else 0.0,
        },
        "authorityRule": (
            "adjusted geometry is a proposal only; acceptance requires an Agent/reviewer transaction"
        ),
    }


def adjust_from_documents(
    proposals_document: dict,
    anchor_scene: dict | None = None,
    **kwargs: float,
) -> dict[str, object]:
    free_walls = load_proposal_walls(proposals_document)
    anchors = load_anchor_walls(anchor_scene) if anchor_scene else []
    # A proposal duplicating an already-accepted wall id must not float free.
    anchor_ids = {wall.wall_id for wall in anchors}
    free_walls = [wall for wall in free_walls if wall.wall_id not in anchor_ids]
    return adjust_walls(free_walls, anchors, **kwargs)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--proposals", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--anchor-scene", type=Path)
    parser.add_argument("--axis-tol-deg", type=float, default=AXIS_TOLERANCE_DEG)
    parser.add_argument("--corner-gap", type=float, default=CORNER_GAP_MAX_M)
    args = parser.parse_args()
    proposals_document = json.loads(args.proposals.read_text(encoding="utf-8"))
    scene = json.loads(args.anchor_scene.read_text(encoding="utf-8")) if args.anchor_scene else None
    report = adjust_from_documents(
        proposals_document,
        scene,
        axis_tolerance_deg=args.axis_tol_deg,
        corner_gap_max_m=args.corner_gap,
    )
    report["input"] = {
        "proposals": str(args.proposals.resolve()),
        "anchorScene": str(args.anchor_scene.resolve()) if args.anchor_scene else None,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "ok": True,
                "output": str(args.output.resolve()),
                "status": report["status"],
                "summary": report["summary"],
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
