---
name: reconstruct-site-scene
description: Reconstruct an outdoor or mixed site capture (building exteriors, porches, roofs, trees, vehicles, fences) from a local colored LAS into an evidence-backed Semantic Scene V2 with live visualization. Use when the capture is a building site, yard, campus or any scene where reconstruct-indoor-scene fails closed on outdoor/mixed domains. Works with weak or strong agents: every judgment is pinned by a tool output and a gate, so following the steps mechanically still produces a defensible scene.
---

# Reconstruct Site Scene (outdoor / mixed)

The pipeline is: evidence -> candidates -> measurement -> assembly through
scene_api -> live view -> adversarial review -> fix loop. Algorithms only
propose; every acceptance carries evidence files and an independent reviewer.
All coordinates are SOURCE plan meters; pick one LAS ground z as scene
elevation 0 and record it in the assembly script.

## 0. Ground rules

- Capture stays read-only. All outputs go to `outputs/<capture-name>/`.
- Never hand-edit scene JSON. Every mutation goes through
  `scene-core/scene_api.py` (or the MCP server `scene-core/mcp_server.py`).
- Use distinct actor ids: `<name>-builder` for creation, `<name>-reviewer`
  for acceptance. Self-review is rejected by the API.
- CLI note: values starting with `-` (negative coordinates) must use the
  `--flag=value` form, e.g. `"--crop=-2,-14,18,6"`.
- Python deps: `pip install -r requirements.txt` (laspy, numpy, Pillow).

## 1. Evidence first (look before deciding anything)

```powershell
# Full-site overview: orthophotos, heightmap, ground-relative bands
python scene-core/pointcloud_evidence.py overview --las <cloud.las> `
  --output outputs/<cap>/evidence --cell 0.10 --every 2

# Read ortho-top.png, band-walls.png, heightmap.png YOURSELF before any
# modeling decision. Identify: building(s), tree areas, fences, vehicles.

# High-res crop around each building (cell 0.03), with site-appropriate bands
python scene-core/pointcloud_evidence.py overview --las <cloud.las> `
  --output outputs/<cap>/house-crop --cell 0.03 "--crop=<x0,y0,x1,y1>" `
  --bands "ground=-0.30:0.30,low=0.30:1.00,walls=1.00:2.60,eaves=2.60:4.50,roof=4.50:12.00"

# Vertical sections through every structure axis (N-S and E-W minimum)
python scene-core/pointcloud_evidence.py elevation --las <cloud.las> `
  --output outputs/<cap>/sections --line "x0,y0;x1,y1" --width 0.8 `
  "--zrange=-2,8" --cell 0.03 --name <name>
```

Every image's manifest carries the exact pixel->meter transform. Quote it in
any measurement you record.

## 2. Tree candidates (algorithm proposes, agent verifies)

```powershell
python scene-core/detect_trees.py --las <cloud.las> `
  --output outputs/<cap>/tree-candidates.json --min-height 4 `
  --min-separation 3.5 --every 2 "--exclude=<building bbox>"
```

Then: merge candidates closer than 2 m (keep the taller), drop
`trunkVerified: false` unless the orthophoto clearly shows a crown, and draw
the survivors on ortho-top.png to eyeball every circle against a real crown
before creating any tree item. Conifer heuristic: canopyRadius/height < 0.12.

## 3. Structure measurement (a dedicated measurement pass)

Give the measurement task (ideally a subagent) ONLY evidence images plus the
pixel math, and require a structured JSON with per-item confidence notes:

- First solve the building yaw (projection sharpness of a 1.4-1.9 m band);
  measure in the rotated frame, convert corners back to x/y.
- Wall planes from raw-LAS face histograms give true thickness - do not use
  placeholder thicknesses.
- Cut a dedicated section along EVERY wall (openings), every post row, and
  both gable ends (a plan raster reads the interior ceiling, not the roof -
  only end-wall silhouettes prove roof shape).
- Openings: an open doorway is a void in the wall-plane occupancy; glass
  (windows, storm doors) returns points and must be read from colour
  elevations - mark those MEDIUM confidence.
- Classify every uncertain item REVIEW with a reason instead of guessing.

## 4. Assembly through scene_api (gates enforced)

Write a capture-specific `outputs/<cap>/build_*.py` that imports
`scene-core/scene_api.py` and converts the measurement JSON into ops.
Conventions that worked:

- rel(z) = z - groundZ; walls on a raised floor use baseHeight = floor level.
- Gable roof = two `roof-panel` items about the ridge; porch lean-to = one
  panel. Viewer semantics: positive `layout.pitchDeg` lifts the panel's
  local NORTH edge (verify against a section; flip the sign if wrong).
- Posts = `column` nodes; vehicles = `vehicle` items with `layout.kind`
  (pickup/suv); steps = `step` items; lattice/fence = thin wall with
  `opening` children for gaps.
- Trees / vehicles measured as bounding boxes accept as `inferred` with the
  reason; voids measured in occupancy maps accept as `measured`.
- Attach the actual section/orthophoto files as evidence (the API hashes
  them); acceptance without an existing file fails closed - that is correct.

## 5. Live visualization while building

Serve the repo (`python -m http.server 8765`) and open:

```text
viewer.html?scene=/outputs/<cap>/scene.json&watch=2&mode=model
```

The viewer hot-rebuilds within `watch` seconds of every scene write without
moving the camera. Add a site ground slab early - outdoor scenes without a
ground plane are unreadable.

## 6. Quality loop (mandatory before claiming done)

```powershell
# Overlay authority geometry on the evidence rasters
python scene-core/render_scene_overlay.py --scene outputs/<cap>/scene.json `
  --manifest outputs/<cap>/house-crop/evidence-manifest.json `
  --base outputs/<cap>/house-crop/band-walls.png `
  --output outputs/<cap>/qa-overlay-house.png --dim 0.85
```

Inspect the overlay yourself, then hand the scene + overlays + sections to an
INDEPENDENT adversarial reviewer (different actor; subagent if available)
with a fixed checklist: wall alignment (<0.15 m), roof pitch directions,
opening positions vs wall elevations, post/vehicle placement, missed/false
trees, and ledger entries whose cited evidence does not actually show the
element. The reviewer writes `review/adversarial-review.json` with
PASS/FAIL/SUSPECT per check and concrete fix values.

Fix every FAIL through scene_api (geometry updates auto-demote acceptance to
candidate - re-accept with fresh evidence), regenerate overlays, and re-check
the failed items. Two non-improving attempts on one issue = change evidence
or decomposition before patching again.

## 7. Deliver

Deliverables: `scene.json` (with full evidence ledger), evidence + sections
directories, QA overlays, the review JSON, and viewer screenshots (top +
oblique + eye-level). State remaining REVIEW items and unresolved candidates
explicitly; never fold them into the accepted count.
