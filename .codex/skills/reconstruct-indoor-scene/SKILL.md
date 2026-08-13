---
name: reconstruct-indoor-scene
description: Reconstruct an indoor scene from local colored point clouds, scanner poses, panoramas or ordinary photos into an evidence-backed semantic scene and Three.js model. Use when Codex must identify, measure, draw, complete, model, visually compare, adversarially review, or iteratively correct walls, glass, doors, windows, columns, floors, ceilings, furniture, finishes, and scan gaps without depending on GroundingDINO, TRELLIS, Blender, remote model APIs, or scene-specific automatic recognition.
---

# Reconstruct Indoor Scene

Build the scene as an AI-supervised measurement and drawing workflow. Treat algorithms as measurement assistants, not decision makers. Keep every inference explicit and iterate until the rendered scene agrees with raw evidence.

Read [references/orchestration-contract.md](references/orchestration-contract.md) before editing geometry. The pipeline owns completion; an author never self-certifies its own work.

## Start safely

1. Read the nearest repository instructions and inspect dirty files before editing.
2. Keep the capture read-only. Write derivatives to a dedicated output directory.
3. Run `scripts/discover_capture.py --data <capture> --output <work>/capture-manifest.json`. If it returns `BLOCKED_MULTI_CAPTURE_ROOT`, select exactly one reported `relativeRoot` and rediscover there. If one unit still returns `BLOCKED_AMBIGUOUS_CLOUD`, rerun with `--point-cloud <exact-relative-path>` after inspecting the alternatives. If it returns `BLOCKED_NO_POINT_CLOUD`, stop. Never pair a cloud from one capture unit with photos or poses from another.
4. Inspect an unannotated overview and decide the scene domain. For a new indoor dataset, run `scripts/init_reconstruction_job.py --data <capture-unit> --work <fresh-work> --scene-domain indoor --scaffold <repo>/prototypes/litereality-three-demo`. `outdoor`, `mixed`, and `unknown` fail closed. The work directory must be outside the capture and bound to its fingerprint. This also creates `pipeline-state.json` with resumable stages, capability truth, issue history, and checkpoints.
5. Treat `READY_GEOMETRY_ONLY` as permission for geometry work only. Missing photos blocks material acceptance; missing pose/transform evidence blocks posed-photo association; either condition blocks whole-scene acceptance.
6. Reuse an existing local viewer/generator when present. The current `prototypes/litereality-three-demo` generator and ledgers are example-bound: for a new dataset, parameterize source path, output root, capture fingerprint, and a blank ledger before running it. Never let a new capture reuse the example scene, evidence, screenshots, or decisions.
7. Otherwise adapt the semantic-scene and Three.js scaffold without hardcoding a capture path, filename convention, or room layout.
8. Record the source coordinate system and one explicit source-to-display mapping. Indoor source data is commonly Z-up; Three.js scenes commonly use Y-up.

Before passing a stage, register the actual local tools behind its capabilities with `scripts/reconstruction_loop.py capability`. `AVAILABLE` requires an existing tool/artifact plus a separate probe receipt bound to that file's hash; file existence alone is insufficient. Degraded or blocked capability must stay visibly degraded or blocked.

If a capture lacks photos, continue with point-cloud geometry and mark material or occlusion completion as inference. If a surface is not defensibly modelable, explicitly reject it instead of leaving a candidate.

## Build evidence before geometry

Read [references/evidence-and-geometry.md](references/evidence-and-geometry.md) before drawing building structure.

Generate these views from the full source cloud:

- ceiling-removed colored overview;
- high structural slice for walls and glazing frames;
- furniture X-ray plus table and chair height bands;
- local perpendicular elevation for every accepted segment;
- unannotated regional crops paired with overlays;
- nearest posed photos or panoramas for material, openings, and occlusion.

Inspect the unannotated image first. User arrows, boxes, and sketches may identify a symptom but never supply coordinates.

Prefer browser-free static picking for repeated plan edits. Write an image-hash-bound picks JSON containing source-metre endpoints, then run `scripts/render_static_pick_overlay.py` to create a small overlay PNG and normalized structure JSON. Let algorithms suggest points or axes, but let the Agent select and edit those coordinates. Use a browser only for the final Three.js interaction check or when static evidence cannot expose the defect.

After each semantic rebuild, run `scripts/render_scene_plan.py` to create a browser-free top model and hash manifest. Review this fresh image for skewed axis families, missing dividers, fake floor patterns, furniture collisions, and visual clutter before spending time on Three.js. The static renderer must reconstruct programmatic chairs from the same layout fields as the Viewer.

After picking a segment, run `scripts/render_segment_elevation.py` against that exact source-metre line. Inspect the unannotated colour elevation to locate openings and vertical construction bands before authoring doors, windows, sills, heads, or wall height. The reported strongest perpendicular offset is only a measurement hint; it never moves an Agent-picked line automatically.

For a room band, enumerate raw cross-walls before declaring spaces. Every persistent cross-wall in the unannotated structural slice must map to a modeled boundary, an opening, or an explicit rejection. Do not check only the rooms already declared by the author.

Before accepting visual finish quality, read [references/detail-joint-review.md](references/detail-joint-review.md) and run `scripts/audit_wall_joints.py`. Endpoint closure is necessary but not sufficient: independently rendered wall solids can still expose a black seam or light leak. Keep measured centerlines unchanged; only same-material accepted solid walls may receive a 12–24 mm render-only overlap. Doors, glass, windows, continuations and different finish layers never inherit that overlap.

Before accepting any exterior wall or glazing face, run `scripts/audit_facade_face_selection.py` on every intersecting cross-wall. Treat the nearest visible return as only one face candidate. If at least half of the cross-walls continue 0.12 m or more beyond the picked face, stop and enumerate parallel inner, centre, and outer facade candidates. Prefer the outer common construction plane for the room envelope unless plan sections or photos prove a different semantic face. A user sketch can flag the wrong side but cannot provide the offset.

When the outer face is beyond a transparent or unscanned gap, keep the measured opaque cross-wall endpoint and the inferred facade-junction endpoint as separate fields/elements. Do not stretch the entire opaque wall through the gap. The room footprint may use the inferred junction, while the solid renderer stops at measured evidence.

## Resolve in construction order

1. Establish floor and ceiling elevations, but never render a camera-path envelope as a floor slab.
2. Draw the exterior shell from continuous high returns and photo-visible facade construction.
3. Close each room with shared endpoints. Fit repeated dividers to one family axis unless raw evidence proves a real deviation.
4. Split walls into openings, doors, glazing, sills, heads, piers, and returns. Do not draw a solid wall across an opening.
5. Draw interior finishes and fixed furniture zones.
6. Add movable furniture from plan footprint, local height, photos, and repeated-layout constraints.
7. Complete scan gaps using symmetry, standard construction, repeated modules, and collision-free topology; label each completion `inference` with a reason.

For every elongated table or workbench, create a raw-data `furnitureValidation` receipt. The proposal may bound a deliberately enlarged search ROI, but the fit must re-estimate center, yaw and supported size from tabletop-height points; it must never copy the accepted yaw or size as the result. Record this as a conditional raw fit unless multi-seed and multi-ROI sensitivity checks converge. Compare it with a leave-one-out local wall/furniture axis family even when the author did not declare a family. A manual parameter compared with a suggestion derived from the same manual search box is one evidence source, never two. If raw coverage is sparse, keep a confidence interval and accept only as inferred with a second posed-photo or repeated-family source; a nearest camera position alone is not visual confirmation.

Every provisional element must end as either `accepted-measured`, `accepted-inferred`, or `rejected`. A finished scene has zero visible or hidden unresolved candidates.

## Run the visual correction loop

For every iteration, open one concrete issue in `pipeline-state.json` and work it to resolution:

1. Regenerate static raw/overlay PNGs first; regenerate the deterministic Three.js view only after the regional static overlay is credible.
2. Compare global raw, global overlay, regional raw/overlay, local elevation and relevant photos. Add Three.js overlay/model views at the regional sign-off and final delivery stages.
3. Check position, direction, endpoints, dimensions, height bands, material, openings, occlusion completion, collision, regularity, and omissions.
4. Write corrections into the human resolution ledger; never let an algorithm silently move accepted geometry.
5. Run `reconstruction_loop.py patch` immediately after the semantic scene parses. It stores a last-known-good hash-addressed checkpoint and invalidates downstream review.
6. Run `reconstruction_loop.py review` with a new render plus independent raw/overlay/elevation/photo evidence. Every issue requires a reviewer other than the patch author; P0/P1 uses an independent regional or adversarial reviewer.
7. Invalidate old screenshots and scores whenever scene geometry changes. Two non-improving attempts trigger a mandatory strategy change before another patch.
8. Repeat failed regions before scoring the whole scene.

Batch one coherent regional correction before rebuilding. Do not reopen the browser for every endpoint adjustment. Use a fixed final runtime matrix after static review: global top, regional overlay, oblique construction close-up, eye-level object focus and near-floor interior. A top view cannot accept wall joints, floor thickness or camera occlusion.

Use fixed Y-up camera controls. Keep top view slightly off the orbit pole and clear damping when switching fixed views.

## Require adversarial review

Read [references/adversarial-review.md](references/adversarial-review.md). If subagents are available, assign independent cross-region reviewers and prohibit them from editing the same authoritative scene file. Do not tell a reviewer the expected answer.

Do not accept `candidateCount=0` as proof of quality. Reviewers must try to find forced acceptance, hidden omissions, duplicate walls, fake slabs, skewed partitions, crossed openings, and evidence laundering.

Pass `regional-review` only after every known regional issue resolves. Then run a fresh whole-scene review and pass `global-review` against the same scene hash. Opening or patching any issue invalidates both reviews.

## Score and gate

After visual review, create a scene-bound review receipt using the schema in [references/review-receipt-schema.md](references/review-receipt-schema.md), then run:

For a browser-free static review, do not hand-edit `PASS`. First render the final top and cutaway manifests, then run `scripts/accept_static_visual_review.py`. It verifies the current scene, renderer and output hashes, all blocking scene gates, and the checklist semantic hash before it writes the checklist and receipt. Any later scene or renderer change makes that acceptance stale.

```powershell
python .codex\skills\reconstruct-indoor-scene\scripts\score_scene.py --scene <scene.json> --visual-review <review.json> --output <score.json>
```

`score_scene.py` automatically reads `<scene-dir>/wall-joint-review.json`; pass `--wall-joint-review <path>` only when the receipt is stored elsewhere. Missing or failed endpoint/T-junction review is blocking.

Pass only when all are true:

- no unresolved candidates;
- no P0 or P1 visual findings;
- every declared room closes and publishes all boundary elements;
- repeated partitions differ by at most 2 degrees unless evidence records an exception;
- measured plan offsets are at most 0.08 m; larger or unmeasured completions must be `accepted-inferred`, retain an explicit reason, and link at least two distinct verified evidence files;
- every reviewed region scores at least 85 and total score is at least 90;
- the review receipt hash matches the current scene;
- `structures` and `author` pipeline stages, all blocking quality loops, every region declared by the current scene, declared topology, and overlap review all pass;
- no fake camera envelope, door-filling wall, exact duplicate structure, or stale screenshot remains.
- no persistent raw cross-wall remains unexplained inside a declared room band;
- no accepted envelope face is contradicted by structural cross-walls continuing beyond it;
- no tabletop or programmatically generated chair footprint crosses an accepted wall, glazing plane, or room partition;
- every elongated furniture object has a current independent pose receipt; raw yaw residual and local-family yaw residual are at most 3 degrees unless a distinct photo or raw-point explanation records the real deviation; gross all-height point counts and self-comparison deltas never satisfy this gate;
- no collision solver silently compresses a furniture layout into an implausible pose; a sub-0.30 m meeting-chair clearance must be explicitly labeled as an inferred stored or tucked-in state and must pass fresh visual review;
- every programmatic child detail inherits a parent-local frame and stays inside its parent footprint;
- every same-material solid-wall joint passes the centerline gap audit and a fresh oblique close-up; no background pixel, light leak, z-fighting face, overshoot or opening closure is visible at the joint;
- runtime views include an eye-level focus and near-floor interior frame; the camera stays above the floor, floor surfaces are solid slabs rather than transparent sheets, and object focus is not hidden behind a wall;
- final visual evidence binds the scene hash, renderer hashes, view mode and camera parameters, and is newer than all bound inputs.

Prefer one final syntax/JSON/UTF-8 check over repeatedly running broad regression suites. Visual reconstruction quality comes from evidence comparison and correction, not test volume.

After `score_scene.py` passes, call `reconstruction_loop.py publish`. It refuses stale hashes, unresolved issues, incomplete stages, geometry-only limitations, or an existing publish directory, and writes a new immutable hash-addressed snapshot.

## Deliver

Provide the runnable viewer, semantic scene, resolution ledger, evidence images, score report, and concise remaining limitations. Separate geometry completion from whole-scene acceptance; do not claim perfection when any region or evidence gate remains open.
