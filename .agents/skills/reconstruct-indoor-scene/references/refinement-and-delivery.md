# Finish a measured presentation without repeating the same corrections

Read for architectural/furniture refinement, bilingual customer demonstrations, synchronized exchange models, or refinement after a first plausible model. Continue to use modeling-decisions.md for source and frame decisions. No3, No4 and INTERGEO motivated these checks; their layouts, dimensions, object counts, tour paths and test totals are not defaults.

When the user finds obvious overlay errors, follow [pointcloud-model-alignment.md](pointcloud-model-alignment.md) before further polish. A supported wall audit can still be fitting a cabinet, window frame or the wrong side of a wall return.

For exterior-frame, vehicle-camera and flag-lettering corrections, read the relevant subsection of [interactive-showcase-polish.md](interactive-showcase-polish.md). For release inventory/delta checks and actual-origin parity, read [delivery-verification.md](delivery-verification.md). Ordinary furniture edits do not need driving or deployment checks.

## Intake and reusable evidence

- If a supplied path is absent, inspect the nearest existing parent for date/time naming differences before asking. Bind the resolved capture, exact final cloud and its hash; never silently substitute a realtime or another processing output.
- Use solver unit metadata and source coordinates to establish units and frame. A zero error field with no RTK/GCP observations is not zero reconstruction error. Keep coordinate convention, floor levels and display rotation explicit.
- Inventory all images and poses once, inspect the projection, then select representative regions. Reuse source-bound full-resolution caches or indexed queries for measurements. Keep display sampling separate; point-cloud preview bounds must not become room boundaries.
- Process adjacent regional questions together. Keep a small producer-generated object ledger with ID, family, inspected source frames, measured centre/yaw/extent, ROI and fitted faces, floor/support host, inferred properties and unresolved discrepancy. A total point count is not an accuracy metric.

## Complete the actual objects before polishing

Give every visible major furniture instance its own evidence decision. Similar chairs can have different backs, cushions, heights or positions; a repeated generator does not prove equal spacing. Audit neighbouring small tables, plant stands and utility units so swapped instances or omitted supports cannot hide behind a plausible overview.

Review in this order where applicable:

1. Object family, count, host face, position and facing direction.
2. Silhouette and dimensions at base, seat/body, worktop and highest trim. Separate scanned face versus recess; do not confuse a window's recessed surface with the room wall plane.
3. Contact and construction: feet, casters, legs, apron, support, shelf and top. Check the lowest supported mesh against its actual floor/platform/host, rather than masking a gap with shadow.
4. Per-instance visible details: slats, drawers, back panels, screen/bezel and mount, hardware, open leaf, seams. Verify generated children and geometry, not just declared counts.
5. Materials, bevels, illumination and appearance at customer camera distance. Hidden joinery and plant leaves stay approximations.

For mixed workshop and yard captures, distinguish visually adjacent equipment before modelling: a vacuum, welding unit, cutting machine and grinder are not interchangeable. Preserve separate lockers in connected rooms. Use height-band fits for circular objects: trampoline outer rim, mat, mid-net and top ring can have different radii and centres; a cluttered ROI bounding box is not a cylinder fit. Fit visible ground separately for each room and local yard patch. Preserve measured worktop levels when extending lower supports to a sloping floor.

Completed panorama batches can improve semantic review. Check batch status, missing poses and exact versus interpolated pose sources. Use the panorama midpoint pose rather than the left-eye camera pose, and validate the quaternion-to-UV convention against a visible landmark before claiming aligned orientation. Retain source timestamps as strings in JavaScript-facing records.

When the user requests a complete exterior, close unobserved side and rear faces using evidence-constrained axes, roof slopes and conservative symmetry. Keep observed geometry unchanged, preserve known window openings through added outer wall layers, and label inferred faces explicitly. Do not fabricate unseen doors or interiors. A full-exterior default view and interior cutaways can coexist.

Use export-safe logical IDs (letters, digits, underscore and hyphen) so GLTF loader name normalization cannot break selection or cutaway rules. Bind attached openings and wall fixtures to their host cutaway state; inspect every preset after completing the exterior because newly added context can occlude equipment or clip the frame.

Top-down alone cannot verify feet or canopy height. Pair a plan/overlay with an eye-level face and a low oblique support view. A canopy has a bottom level, top level, vertical fascia height, horizontal band width and support geometry; measure these separately. Lights need evidence-supported count and hosts before arranging them into a tidy pattern.

## Connected corridors and openings

Inspect the connections beyond the main room before declaring coverage complete. Use local wall elevations and low/mid/high continuity for each door region; keep visible opening bounds, leaf width, frame, jamb and header separate. Far sparse returns support only what is visible. Record regularized widths and lighting spacing; do not populate unseen adjacent rooms.

Before furniture finishes, distinguish the exposed interior wall face, concealed wall thickness, window/frame plane, sill and reveal depth. A recessed window can have strong point support on a different plane from its surrounding masonry. Check several heights and neighbouring wall lengths, then change the shared host parameters and dependent jambs, doors, slabs and cutaways together. Do not pull a measured frame onto the wall plane to improve a residual, or infer both wall faces from a single visible surface.

Fit a connected corridor's own long axis and floor patches when they differ from the main rooms; keep their common capture transform and physical connection. Regularize each door only within its observed opening. A corridor is a space on its actual building storey, not an extra storey because the viewer assigns it a context/group index. Check landings, slab outlines and the stair void against the corrected shell before furnishing. Full-resolution cloud bounds and visible continuations determine extent, not the smaller preview crop.

Define the door hinge in leaf-local coordinates relative to a named viewing side. Verify the closed transform, fixed pivot, swing clearance and direct click after rotating the assembly into display space. If walkthrough is present, a closed door must not be treated like an always-open gap. Keep motion and collision claims proportional to implemented checks.

## Multilingual demonstrations and artifact parity

For requested languages (including Chinese, English or German), translate the whole interaction: static controls, object names, selected-object cards, changing door/tour buttons, reference-photo options, dimensions/units, loading/errors, accessibility text and download explanations. Switching must preserve camera, mode and selection. Verify persistence and intermediate/narrow viewport widths, not just the initial title. Long German controls can fail at an intermediate width even when desktop and mobile layouts work.

Keep geometry in one producer and regenerate dependent exports and audit records from the same scene revision. Bind model, renderer/UI, preview and exchange-file hashes. A screenshot from a previous model or a download from a different revision is a delivery defect even if each file opens.

For a requested SketchUp exchange, first probe a usable native application or official SDK and the receiving version. If native SKP is unavailable, continue independent modelling/IFC work and state the precise alternative: a COLLADA DAE plus textures package, subject to the target version's import support. Honour an agreed deferral without repeatedly asking for an installation; never rename DAE to SKP or claim that an independent loader proves SketchUp desktop import. Do not upload a private capture to a conversion service without authorization.

Preserve units, handedness, explicit up-axis mapping, logical component names, texture references and instance transforms in COLLADA. Check that texture and base-colour multiplication survive the importer representation. Validate unique XML IDs, references, finite coordinates, index ranges, triangle counts and geometry against the source; verify relative texture and ZIP paths, then independently load/render the package. Package instructions should explain extraction, DAE import and later Save As SKP; keep actual desktop import as a distinct result. For IFC, use [bim-source-overlay.md](bim-source-overlay.md).

Settle shell topology and verify the local visual candidate before the final multi-format export. Export one frozen scene revision to the requested formats, regenerate previews, then build the download manifest/package. Geometry changes invalidate its exports and geometry screenshots; a label-only fix does not justify rebuilding the cloud or unchanged IFC. A complete exchange package can include model files, a component schedule, coordinate transform, verification summary and concise limitations; raw point clouds, panoramas and internal paths are not default contents. Exercise real browser download behaviour and hash-check served bytes for every delivered format against the validated candidate.

## Customer-facing copy

Treat a client or investor showcase as a product surface. Keep evidence notes, inference reasons, author requests and delivery gates in the internal ledger and work record. Never render `claim`, evidence commentary, inferred-status explanations or raw limitations as selection-card copy or navigation labels. Use an explicit customer-copy layer; when no useful description exists, show the object name and relevant controls without a boilerplate paragraph. Keep inferred/observed distinctions in the source-bound delivery record and never replace them with unsupported accuracy or performance claims.

Describe the object or the action directly: “GC30 guidance controller”, “Drive tractor”, “Door controls”. Phrases such as “added at user request”, “presentation simplification”, “interactive equipment demo”, “not in the original scan” or “independent acceptance not run” belong in work records, not customer walkthroughs. Product dimensions must describe the product itself; do not label an assembly bound including mounting cables as the device size. Label model dimensions accurately when they help a BIM user.

Before delivery, inspect every supported language across every component name and selection card, menus, information/download panels, photo captions, tooltips, accessibility labels and dynamic driving/door/tour states. Include initial loading and error states, narrow screens and a language switch while a message is visible. Add a focused regression for any observed leak: inject a unique marker into internal metadata and verify it never reaches rendered copy. Keep readable screenshots and bind the check to the final served UI files. Audit all records on this surface rather than fixing only the sentence the user quoted.

## Focused acceptance and safe handoff

Close each observed discrepancy with updated evidence or a precise remaining uncertainty. If the same correction does not improve twice, change the evidence view or geometry hypothesis. Do not keep nudging a parameter without a discriminating observation.

Check the final served model hash, real render, supported views, orbit/zoom, selection and actual downloads. Exercise model/cloud/overlay with loaded point counts, source-image readiness, dynamic language states and every added route/return link. Run a full tour loop after path or motion changes and inspect its joining seam; sampling must distinguish actual motion from background throttling. Reuse still-bound evidence for unchanged geometry.

When external synchronization is requested, build an isolated prefix-safe package, preserve previous collection members and deployed fallback assets, validate relative asset/deep links and download extensions, and verify on the actual origin. GLTF embedded textures need the existing applicable blob CSP support; do not weaken the main site's policy as a workaround. Compare fetched assets with release hashes and keep a previous release for rollback. New capture reconstruction alone does not authorize uploading its source data or changing an unrelated service.

Completion states remain distinct: presentation refinement, local browser verification, public-site verification, desktop import and independent dimensional acceptance. State any unfinished requested gate explicitly; do not translate visual polish, support counts or passing UI tests into a measurement-accuracy claim.
