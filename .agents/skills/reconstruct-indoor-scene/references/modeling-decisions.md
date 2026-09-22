# Accurate decisions with less repeated work

Read for a new indoor capture, a recurring reconstruction defect, or a workflow update. This supplements the presentation workflow; it does not change authority contracts. Outdoor/mixed captures need their own scene-domain workflow. Tool paths below are relative to the repository root.

## Minimum working context

Keep one small region ledger in the capture work directory, maintained through its producer if generated. Store: source/transform binding; region ID and bounds; user terminology/corrections; inspected frame paths and projection; observation; geometry hypothesis; measured versus inferred properties; next discriminating measurement; changed producer fields; check result and limitations. Do not build a second stage-PASS system.

On resume, read that ledger and current source/producer hashes. Reuse matching evidence, and inspect only changed or unresolved regions. Prior screenshots, ports and drive letters are retrieval leads, not proof of the current runtime.

## Decide which evidence answers the question

| Question | First evidence | Decision boundary |
| --- | --- | --- |
| Room name or function | User correction and visible use | User name is not a dimensional measurement |
| Furniture type, parts, direction, repeated count | Inspected multi-view photos | Nearest camera or same furniture family does not bind the instance |
| Position, edge, yaw, height, thickness | Full-resolution local point support in a declared frame | Point count or ROI residual is not whole-object accuracy |
| Door opening and circulation | Photo + wall elevation + low/mid/high continuity | Empty scan cannot distinguish glass, doorway and occlusion |
| Step or platform | Adjacent clear floor patches and photo-visible riser | Compare local relative levels, not unrelated global peaks |
| Hidden closure and furniture | Explicit spatial hypothesis | Do not stretch a measured claim across an unseen part |

If evidence disagrees, check frame mapping, image projection, time pairing, foreground occlusion and competing surfaces first. Do not average contradictory surfaces into an invented wall. Use a different view/ROI to discriminate; if unresolved, retain alternatives or an uncertainty range. Do not manufacture a confidence percentage.

## Calibrate before copying geometry

- Record units, source origin, up axis, axis rotation, display mapping and handedness. Test a horizontal basis pair and vertical displacement; confirm with raw/model overlay. The evidence helper reverses horizontal orientation when mapping depth: an unconverted source yaw must not be applied directly to a display object.
- Estimate floor tilt or multiple levels before applying a global floor value. Do not normalize different local floors independently without preserving their relative heights.
- Use local measurement bands, not fixed Office 0421 heights. Separate support count, spatial coverage, fit residual, fitted face and uncertainty. A picked ROI makes a result conditional; test a modestly shifted/enlarged ROI when an edge or axis is ambiguous.
- Keep raw fit, author decision and presentation regularization separate. Snapping repeated families is a display decision unless evidence confirms the axis; retain real rotated exceptions.

## Avoid demonstrated rework patterns

1. **Missed stationary observations:** inventory all pictures before subsampling. Organize by camera, time and region; show bounded contact pages with frame IDs. Retrieve additional panorama intervals when a needed region is absent. `region --photo` creates contacts only for supplied photos, not a full-photo index or pose validation.
2. **Projection assumptions:** inspect the original image. A 2:1 frame with black lobes can be a fisheye layout. Use validated rectification/calibration or undistorted pictures for shape checks. Unknown projection cannot support precise pixel measurements.
3. **Wrong furniture family:** decide desk versus meeting table, owner versus visitor side, one-sided versus opposing booth, and table long axis before creating meshes. Missing devices are per-instance observations, not global count rules.
4. **Repeated omissions:** declare instance IDs and expected component counts from evidence. Compare with actually generated children per instance and per side before merging meshes. Modulo/random decoration must not remove required components; aggregate counts can hide an overfull row and an incomplete row.
5. **Uniform bay-width gaps:** derive platform/seat width from host partition inner faces using shared boundaries. Do not reuse one measured width for all bays. Inspect joints/riser at low oblique angle; never move measured walls to close a decorative seam.
6. **Door over solid wall:** split the host into opening, jambs and header. Distinguish photographed open/closed state from demonstration pose. Check leaf swing, aperture and path; ignoring every door in collision code can allow walking through closed geometry.
7. **Unsupported remote room:** inspect boundary regions, photo-visible connections and wall continuations outside the first overview crop. Separate supported extents, guessed end cap and unknown space beyond it.
8. **Unverified finish:** prioritize topology, openings, levels, object family/pose and count before materials and decoration. Contact shadows do not repair floating geometry; JSON parsing does not validate appearance.

## Rebuild only what changed

| Changed input | Recompute | Reuse when still bound |
| --- | --- | --- |
| Cloud, units, origin or transform | New cache, measurements, model and dependent checks | Unrelated capture work only |
| Photo selection or semantic label | Observations, affected semantic/model output and visible checks | Point cache and unrelated measurements |
| Local geometry/opening/level | Region, adjacency/clearance, overview/overlay and detail views | Source cache and independent regions |
| Repeated furniture generator | Every instance/count and affected clearances/views | Shell measurements |
| Materials, renderer, UI or camera | Affected browser views, counters, selection and fresh render evidence | Geometry evidence if geometry truly unchanged |
| Collection card or return link only | Every changed entry, model readiness/identity, return path, preview loading and responsive layout | Unchanged geometry and motion checks; identify their older input bindings explicitly |

For authority work the contract DAG determines invalidations. This table is an optimization guide, not permission to preserve stale acceptance receipts.

## Tool use and recovery

- Probe the needed tool using current `--help`; avoid broad dependency discovery or reinstalling a working environment. Old machine paths are hints, not portable instructions.
- Run `prepare` on task intake/resume, then batch regional questions. The current helper hashes and loads the full NPZ on each region call; caching avoids LAS parsing/transform but is not a spatial index. Many large ROIs justify indexed services or a separately tested batch optimization, not removing hash checks.
- Distinguish runtime failure from modeling failure. Inspect a permission/unavailable-tool error once, then use an authorized equivalent if available. Do not embed application-version-specific escalation binaries in reusable instructions or repeatedly invoke the same failing entrypoint.
- For pytest temp-folder ACL errors, use a newly created isolated work directory and preserve assertions. Do not disable tests or delete shared caches.
- Reuse one verified browser session. Confirm URL, served model identity, readiness and a completed foreground render. A queued open, HTTP 200 or previous screenshot does not prove the requested view.
- Wait for camera settling/rendering before sampling draw calls. Save actual camera/target/zoom when exposed; otherwise identify presets as requested. Report viewport and conditions with performance, not universal FPS.
- After two non-improving correction cycles, change evidence, region or family hypothesis. If progress needs new evidence or user authority, state the precise missing fact; continue safe unaffected work where possible.

## Test reuse before claiming generality

Use isolated synthetic data with another origin, orientation, footprint and furniture distribution. Check that old source paths, object coordinates, names, counts, clipping bounds or screenshots are not inherited. `tests/test_presentation_evidence_fast.py` covers the helper's frame/ROI/cache behavior only, not a generic scene generator or real-capture quality. Office 0421's scene producer and browser manifest script remain example-bound.
