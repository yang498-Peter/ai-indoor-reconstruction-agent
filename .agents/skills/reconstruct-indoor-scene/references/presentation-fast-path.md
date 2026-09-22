# Fast, evidence-guided presentation reconstruction

Use when the user wants a complete, attractive, locally viewable indoor model and allows reasonable inference. This is a separate presentation task, not an alternative evaluator for Semantic Scene V2. No PASS writes, authority hand edits, weakened thresholds, or fabricated review receipts. Raw captures remain read-only and outside Git.

For a fresh capture or repeated corrections, also read [modeling-decisions.md](modeling-decisions.md) for accuracy decisions and selective rebuild rules. For runnable preparation and handoff steps, use the repository [Chinese SOP](../../../../docs/室内建模准确性与快速交付SOP.zh-CN.md).

For booth details, logos, interactive doors/tours or a multi-model homepage, use [exhibition-and-collection.md](exhibition-and-collection.md). Start with its relevant subsection; ordinary room reconstruction does not need the full exhibition checklist.

## Start from the actual data, then reuse work

1. Inspect repository instructions and dirty files. Locate the actual capture, exact cloud, pictures and poses; a historical drive letter or screenshot is only a lead. Verify the cloud fingerprint. Use one capture unit, declared units and a documented source-to-display mapping. If a local demonstration exists, inspect its producer and source binding before editing it.
2. Read the unannotated plan and a full photo index before deciding room topology. Record each requested region, semantic correction and evidence gap. User-provided room names override guessed names; user arrows do not define coordinates.
3. Prepare a fingerprint-bound cache once per task. Reuse it for regional slices; do not reread the LAS, regenerate all photographs or restart the browser for each endpoint edit.

From the repository root, the deterministic helper is:

```powershell
python scripts/indoor_presentation_evidence.py prepare --cloud <exact-las-or-laz> --work <fresh-work> --origin <x> <y> <z> --yaw-deg <angle> --length-unit metre --up-axis Z
python scripts/indoor_presentation_evidence.py region --work <work> --name southeast --bounds <xmin> <depthmin> <xmax> <depthmax> --photo <selected-image> --photo <second-image>
```

The helper accepts metre/Z-up LAS or LAZ only. Other units/formats require a verified adapter, not relabeling. `prepare` checks cloud content and transform, verifies cache content on reuse, and refuses a work directory bound to different input. `region` emits three raw colour bands, local height histograms, selected-photo contact sheets and an observations-only JSON. Its cache axes are `[aligned x, aligned depth, height]`; Three.js uses `[x, height, depth]`. Height ranges are absolute in that local frame: override `--bands` and `--height-range` for another elevation or furniture family. Default heights are examples, not measured floors.

For large datasets beyond practical RAM, use the existing indexed-cloud services. Do not feed a display-decimated cloud to measurements. The helper does not classify rooms, validate poses, fit a floor plane, author geometry or grant acceptance. It is a fast observation producer.

## Photos determine semantics; raw returns constrain dimensions

- Index all frames, including stationary panorama intervals skipped by sparse keyframe sets. Select multiple viewpoints per uncertain region. Camera proximity is a retrieval hint, not evidence that an object is visible.
- Inspect the actual projection. A 2:1 JPG with fisheye lobes or black borders is not automatically a valid equirectangular panorama. Use original rectified images or the verified calibration; never obtain precise lengths from distorted pixels.
- Write a short observation before modeling: what is visible, what is occluded, frame identifiers, furniture family, parts, seat/computer count, material and direction. Bind instance identity with visual context and geometry, not just nearest timestamp.
- For a step, compare clear floor patches immediately outside and inside the same bay. Remove legs, seats and vertical edge returns; inspect spatial support. Report a range and a local relative rise. A whole-room height histogram is not a step measurement, especially with drift or tilted floors.
- For a passage, inspect a perpendicular elevation and low/mid/high plan bands. Draw an actual hole and header; do not overlay a glass door on a solid wall. Side-wall continuation and a guessed far-end cap are separate claims.

## Model rooms and connected circulation before detail

Build a coherent shell, rooms, circulation, floor surface, wall/glazing families and repeated furniture layout. Make a top and oblique preview early; improve it regionally. Keep measured placement distinct from inferred scan closure, hidden furniture and decorative detail. Edit the producer and regenerate atomically, never patch generated model/review JSON.

The Office 0421 demo, when available locally at `demos/astra-office/`, is a **visual and interaction reference only**, not a portable skill dependency. Its `astra_office_*` scripts, floor polygon, additions and camera presets are capture-specific. Do not run them on another capture or copy their coordinates. For another dataset, supply a new geometry producer, source manifest, room labels, object instances, floor levels and camera bounds. Reuse parameterized furniture functions and rendering patterns only after separating scene-specific extras.

Logical furniture families must contain their real components:

- A repeated workstation has declared seats per side and exactly matching monitor/keyboard instances unless an observed exception is recorded. Do not use modulo/random styling conditions to suppress required components.
- A booth records bay width/depth, one-sided versus opposing seating, table long axis, floor/step level, cushions and pedestal. Raise the furniture with its platform rather than hiding a step under a floor-level seat.
- A private office is not automatically a meeting room: distinguish the main work desk, owner chair, visitor chairs and separate lounge table from photos.
- A door records opening width, height, hinge side, leaf count and display angle. Keep clear opening and leaf geometry separate, with sensible circulation and no wall duplicate across the opening.
- Use parent-local coordinates for every chair, screen, handle, cabinet and support. Preserve stable logical objects for picking and per-family counts.

Use rounded edges where photographed, believable legs and bases, restrained local textures, diffuse fill and contact shadows. Show a solid floor slab and complete ceiling in eye-level mode; cut away camera-side walls only for readable overview. Static geometry can be merged by logical object/material to reduce draw calls without losing selection or visibility controls. Do not merge parts that must later move independently.

## Fixed correction and browser check loop

Batch one coherent region, regenerate once, compare fresh raw/overlay images and photographs, then check the real browser. Two non-improving iterations mean reconsider the semantic family, frame mapping or topology instead of making smaller blind nudges. Keep user-facing progress concise; do not replace model work with long acceptance paperwork.

The quality baseline is observable behavior, not a promised score or universal automatic accuracy:

1. Overview and plan show all photo-supported major spaces, repeated modules and connections; no unexplained large missing area or old capture geometry remains.
2. Every user correction maps to a model change, an inspected evidence item, or an explicit unresolved limitation. Check edge regions, open doors and floor level changes deliberately.
3. Inspect raw/model overlay, an eye-level furniture close-up, and a near-floor step/joint view. Verify model count from the generated child instances, not only the source JSON. Test conservative chair/wall clearance; report that this is not a complete collision proof.
4. In a foreground browser, test orbit, zoom, direct view changes, WASD, return to overview, model/raw/overlay, shell/ceiling, inference styling, picking and photo loading. Background requestAnimationFrame throttling is not a movement failure.
5. Check JS errors, missing resources, label overlap, control clipping and an intermediate viewport width. Measure frame timing and draw calls on this machine; do not claim one example's performance is guaranteed everywhere.
6. Save fresh screenshots with model/renderer hashes and camera parameters. Record requested presets separately from actual post-render camera/target/zoom; wait for the changed scene to render before reading performance counters or taking evidence. Geometry or renderer changes invalidate older screenshots. Leave the final runnable browser on a useful view.

Deliver the local URL, short list of corrections and clear residual uncertainty. Keep review observations separate from formal authority acceptance. Faster reuse means cached, parameterized evidence and a stable visual checklist; it does not mean new captures can skip semantic inspection.
