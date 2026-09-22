# Exhibition details and a reusable model collection

Read the relevant subsection for booth/counter corrections, screens, supplied branding, doors, guided tours or joining an existing showcase. Use the shared source/transform-bound evidence cache and region ledger from [modeling-decisions.md](modeling-decisions.md). These are presentation refinements, not authority acceptance. INTERGEO examples explain decisions; their coordinates, counts, tolerances and camera route are not defaults for another capture.

## Find the smallest useful correction

Before rebuilding, inspect the current producer, served model hash and user-marked region. Preserve the last working producer, UI and artifact bindings. A practical regional ledger adds `expectedParts`, `hostId`, `faceNormal`, `observedState`, `contacts` and `remainingInference` only where needed.

- Resolve source north/east versus display axes before interpreting a named corner. Record the frame, not just a screenshot arrow.
- Establish a contact sheet covering the whole scene once. For a disputed corner, inspect a frontal and an oblique photo plus local plan bands/elevation; camera proximity alone does not prove visibility.
- Batch the region's structure, screens, supporting objects and branding. Do one geometry build after these related decisions, then inspect the region and its neighbors.
- Keep an inventory of disagreements. Two TV objects may be correctly counted but assigned to the wrong panels; a total count cannot close an instance-level defect.

## Screens and fixtures: record the actual host face

For each observed screen, keep a stable ID, inspected frame IDs, host panel, source-frame center, bottom/top height, width/height evidence, facing normal and mount type. Check the exported mesh exists and points outward on that face. Inspect a front view and an oblique view: mirrored/back-facing screens, slight burial in a board, doubled screens and fixtures hidden by another panel can survive JSON count checks.

Place bezel, display surface, mount and cable-free presentation details in the same parent-local frame. Split independently moving objects before merging static meshes. Do not move a supported panel to compensate for an incorrectly transformed fixture.

## Counters: compare silhouettes at several heights

Use full-resolution regional returns for measurements. A display point preview is sufficient for visual overlay only.

1. Estimate the local floor plane/level. Compute vertical height above it before choosing body, worktop and raised-trim bands. Do not flatten unrelated floors independently.
2. Inspect lower plinth, mid-body, tabletop and raised fascia separately. Draw plan contours and perpendicular elevations. Exclude adjacent visitors, equipment and wall returns from a fitted face.
3. For a suspected raked body, compare clear front-face medians/quantiles in several height bands at a common narrow lateral strip. Perspective in one photo cannot establish a large taper. Preserve support count, ROI and spread; these are conditional local measurements.
4. Fit an asymmetric rounded footprint or height-dependent loft when supported. A rectangular box with a red stripe cannot represent a photographed curved return. Keep the red fascia as a continuous offset path with a credible thickness, corner radius and end return; inspect for self-intersection at the turn.
5. For adjacent counters, inspect actual transformed tabletop footprints and shared edges. Check plan overlap/gap and a low oblique view. Rounded ends and top overhangs must not cross the neighboring cabinet.
6. Compare exhibit lowest points to their supporting tabletop, and counter/plinth feet to the local platform top. Record deltas with a tolerance justified for this model, not a universal 15 mm promise. Contact shadows cannot close a geometry gap.
7. If a cabinet extends past the platform, independently recheck both. Use ground-band occupancy/color transitions and photographs for the platform edge. Do not enlarge the floor or move the cabinet just to pass a containment assertion. If the edge is unseen, keep the regularization explicitly inferred.

Prioritize silhouette, facing, levels, doors/handles, seams and contact before decorative instruments. A GNSS antenna or machine demonstrator may be simplified at presentation scale; record hidden mechanisms and fine internals as simplified rather than inventing measurement accuracy.

## Supplied branding: preserve pixels, then fit the surface

Use the supplied file as the source asset. Inspect it on a contrasting background and record content hash, pixel dimensions, alpha bounds and artwork aspect ratio. Transparent padding changes visible proportions even when the full image is square.

The reusable inspector needs Pillow and does not edit the image:

```powershell
python -X utf8 .agents/skills/reconstruct-indoor-scene/scripts/inspect_brand_asset.py --image <logo.png> --output <work>/brand-inspection.json
```

It reports UV bounds and CSS framing percentages for the nonzero-alpha artwork. Opaque backgrounds are not automatically removed; a fully transparent image is rejected. The bounding box is geometric occupancy, not an optical-centering decision.

- Copy original bytes into the presentation asset folder. Fit artwork proportionally using UVs or a clipped CSS wrapper; do not stretch, redraw, recolor or fake the wordmark.
- Use a neutral material base factor, correct image color space, suitable alpha handling and a small host-normal offset. Do not tint a red/black logo with a red material or create z-fighting on the host face.
- Determine model placement from inspected photos: correct face, size relative to cabinet, height and orientation. A correct source logo in the wrong place is still a modeling defect.
- Check the fetched page asset hash and actual exported model texture/placement. If the exporter re-encodes PNG bytes, compare decoded RGBA pixels; don't demand byte equality of a new encoding. Inspect legibility in a rendered close-up.
- Keep an instance list of brand placements. Do not add logos to every cabinet merely because the asset is available.

## Door and tour interactions

Define a door's left/right hinge from a named viewing side or outward face normal. Store leaf-local coordinates, pivot, closed transform, swing direction, opening width/height and photographed state. Transform the complete door assembly into the scene; a world-axis guess can reverse the hinge after rotation.

Verify a real opening exists in the host. Click the visible leaf and the selection control to close/reopen; the hinge must remain fixed. Reverse during animation and check continuity. Test the handle/frame, clearance and closed-state navigation for the scope actually implemented. Animation is not a full pedestrian collision system.

For a requested guided tour, parameterize camera and target paths separately. Use an arc-length-aware closed path with smooth joining from the current camera, elapsed-time motion, pause/resume, stop and manual takeover. Choose collision-free paths based on the scene; an exterior orbit is not evidence of a traversable interior. Ensure controls remain accessible in fullscreen and at smaller viewports.

After a path/motion change, observe one complete loop including its seam. Record camera/target step sizes and frame timing with browser/viewport conditions. Reuse that run when only a collection link changes; rerun motion when its inputs change. Background tab throttling is not a path defect; use a foreground or controlled headless browser and label which was measured.

## Integrate with the existing model homepage

If this checkout includes the local showcase, inspect `demos/showcase/index.html`, `server.py` and `Start-Showcase.cmd`. In that adapter, `/health` identifies the service and the launcher searches ports 8780–8789. Capture-specific demonstrations are not required portable skill assets. For another checkout, discover its actual catalog/server and readiness API; do not assume the example files or historical port exist, and do not stop unrelated listeners.

Read the live catalog and route mounts before adding a scene. Do not hardcode a historical scene list or count into navigation checks.

- Add/update only the intended card and mount. Preserve the other scene sources, routes and geometry. A new scene has its own output directory and source manifest.
- Use a current rendered preview, accurate scene count, concise English text when requested, and useful supported deep links. Add an obvious `Model collection` / `Collection` return link; a clickable logo alone is too easy to miss.
- Prefer same-origin relative links within the mounted collection so fallback ports work. Standalone scene servers need an explicit collection URL/configuration; do not pretend their own `/` is a gallery.
- Keep local original photographs outside the static output copy. If source photos are served, preserve the existing explicit indexed local routes and keep the service loopback-only.
- Test every added/changed homepage entry in a real browser. Wait for each distinct viewer's ready/render state, inspect its scene identity and screenshot, then click back to the collection. HTTP 200 or a queued panel open does not prove a loaded model.
- Check all card previews, all existing models when navigation/catalog changes, responsive layout and resource/console failures. Unchanged models need a navigation/readiness smoke, not a fresh geometric re-acceptance.

When that optional verifier is present, run `node demos/showcase/verify.cjs --url <active-loopback-root> --output <work>/collection-checks` with an installed Playwright runtime. Check its `--help` before reusing arguments. Otherwise implement the equivalent checks against the current viewer API: click every changed card action, wait for a real render, bind served asset hashes/screenshots and return to the collection. A new viewer API needs an explicit readiness adapter; don't replace readiness with a timeout or page-title check. Navigation verification does not perform geometry or complete-tour acceptance.

## Rebuild, verify, copy and retain

Use an isolated candidate directory if the served page should remain usable during edits. Before copying verified outputs, compare destination hashes with the recorded baseline to catch concurrent changes. Back up only owned paths and replace each file atomically. Do not describe a sequence of per-file replacements as an atomic whole-site switch.

Keep observation reports input-bound: source/transform, generated model, renderer/HTML/CSS and actual rendered view. If a navigation-only edit follows a full model QA, retain the older QA hash bindings and add a separate current navigation check; do not edit old reports to make them appear fresh. Invalidated checks must be run by their producer.

For a compact GLB, reading the complete ArrayBuffer, checking its manifest hash and then parsing it provides an explicit version boundary. Do not mark readiness before parsing/rendering. If a loader reports canceled requests despite a visible scene, trace request start/finish/failure and the actual decoded model before changing checks; don't silently ignore cancellations. A loader change requires model interaction regression, not navigation smoke alone. Large assets may need a streaming integrity strategy rather than an extra full-memory copy.

Complete the current loopback delivery and open the collection on a useful view. Return the homepage/scene URL, current output folder, concise changes and meaningful uncertainty. This workflow reduces repeated scanning and rework; do not claim a percentage speed or accuracy gain without a comparable measured run.
