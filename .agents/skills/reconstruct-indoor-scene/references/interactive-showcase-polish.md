# Refine an interactive showcase from observed defects

Read only the sections relevant to the requested change. These lessons came from a mixed workshop/yard presentation; dimensions, product names, scene counts, roads and camera paths are not reusable defaults. Use [modeling-decisions.md](modeling-decisions.md) for evidence and [refinement-and-delivery.md](refinement-and-delivery.md) for customer copy and geometry provenance.

## An exterior looks skewed

Separate capture orientation from a modeling error. Fit a supported facade direction, then construct one orthonormal local frame: origin O, horizontal facade tangent T, perpendicular depth N, and vertical axis. Generate supported rectangular returns, rear closure, eaves, ridge, soffits and attached details in that shared frame. Keep the real facade yaw; independently using world-X for one side and a rotated front can create a sheared building. A genuinely non-rectangular building needs evidence-specific axes, not forced right angles.

Before changing heights, inspect a perpendicular source section and an oblique photo. Preserve a supported roof slope, longitudinal fall and broad overhang. A visible gap can be missing upper-wall/soffit closure rather than a low measured wall. Keep visible opening heads and their measured faces fixed when adding hidden closure. Check roof-to-wall intersection, both gable ends, gutters/downpipes and the rear silhouette together. Unobserved rear walls stay inferred internally; a tidy exterior is not a new measurement claim.

Ground regularization is a separate presentation decision. Fit clear local ground patches, remove scan wrinkles only where requested, and keep indoor floors, thresholds, steps and vehicle contact consistent. Do not flatten all levels into one plane or smooth away a real riser.

## A vehicle is driveable

First separate static, articulated and moving-body geometry. Preserve logical IDs, local pivots and the captured reset pose. Door leaves, steerable front wheels, rolling wheels and steering wheel must remain independently transformable after mesh optimization.

- Define controls and visible hints together: accelerate/brake/reverse, steer, camera, reset, exit and any touch controls. Key release, focus loss and exiting must clear held input. Switching language must preserve the active driving state.
- Ground height and collision geometry must come from the same scene revision. Use body-aware clearance, closed door leaves, walls and fixed equipment. Test an actually clear path; a tractor stopped by a real equipment cart is not a reason to weaken the collider.
- For requested freedom to drive beyond the capture, author continuous connected presentation terrain/routes with sufficient turning space. Keep it separate from measured site boundaries. Verify wheel contact, slopes, thresholds, reverse and return/reset. Do not claim vehicle physics or collision coverage beyond what was implemented.
- External chase cameras can lag smoothly. A cockpit eye must remain in the cabin's local frame, transformed with body translation, yaw and pitch each frame. Apply bounded local look/recenter smoothing; do not interpolate that eye through world space behind an accelerating vehicle.

Reproduce a camera defect on the old build when available, then measure the actual camera transformed back into the cabin frame during acceleration, braking, reverse and turns. Choose a tolerance relative to cabin clearance and intended head motion; one project's centimetre bound is not universal. Inspect the driver's view as well as numbers: seat backs, roof/glass clipping, dashboard, wheel hub/logo, near plane and screen readability. Test real input alongside any deterministic simulation hooks. Hook-only assertions cannot establish usable controls.

After reset or exit, verify captured vehicle pose, body pitch, wheel/door transforms, controls and scan overlay. Switching views must not retain a moving tractor against an unchanged captured point cloud without a clear mode transition.

## Promotional lettering and flags

Start from the requested product name and reading direction. For a vertical product flag, prefer a proportionally rotated wordmark to unrelated stacked characters when it reads better at the intended camera distance. Use a project-approved font, balanced cap height and tracking, restrained color blocks, quiet whitespace and a smooth banner silhouette. Do not add company logos, slogans or fine print after the user has removed them.

If generating outlined text geometry:

1. Resolve and record the exact font/variation axes; a font family name can silently fall back. Keep font files out of delivery unless redistribution is permitted.
2. Flatten font curves at adequate design-unit resolution before scaling. A tiny unit-size outline converted to polygons can leave visibly angular S/G/9 contours even in a high-resolution render.
3. Respect the font's fill rule and contour orientation. In the observed TrueType workflow, outer contour union followed by counter subtraction preserves overlapping strokes; XOR of every contour cuts unintended holes where A/P strokes overlap. Verify the actual library's winding convention rather than assuming one sign works for all fonts.
4. Triangulate holes without filling counters; inspect representative letters/digits and the exported mesh, not just a texture or font preview. Assert that text stays inside an inset of the final curved banner.
5. Give both faces readable orientation and adequate surface offset. Reversing a mesh normal alone can leave mirrored lettering on the back. Keep pole curvature, seam and cloth edge coherent; avoid an oversized base or decorative clutter.

Inspect a close-up and the normal scene preset. Reframe the pair of flags with the primary product; the UI must not cover either name at desktop and intermediate widths. A typography-only change reuses unchanged point evidence, but needs fresh renders and exported model/preview hashes.

## Run the smallest sufficient verification

| Change | Recheck now | Reuse only with unchanged input binding |
| --- | --- | --- |
| Facade/roof/ground | Local evidence sections, shared frame, joints/openings, affected collisions and presets | Unrelated object measurements |
| Vehicle or camera dynamics | Acceleration/braking/reverse/turns, cabin-frame eye, controls, reset/exit and collision paths | Static geometry evidence |
| Lettering/materials | Exported outlines/holes/facing/containment, close and default views, responsive occlusion, model/preview parity | Unchanged dimensions and driving motion |
| Locale/customer copy | Every supported locale and component/state, persistent switching, loading/errors and intermediate widths | Unchanged geometry and tours |
| Tour/path/renderer motion | Complete loop and joining seam, foreground frame timing, manual takeover | Only checks whose inputs remain unchanged |
| Packaging/deployment | Runtime-field parity, inventory/delta, actual-origin routes/assets/downloads and relevant interaction | Evidence bound to byte-identical inputs |

Record the actual inputs of reused checks; never rewrite old receipts to attach a new model hash. For a local geometry change, a producer-generated per-object geometry/material fingerprint can show which objects stayed identical even if the GLB container hash changed. Otherwise do not assume object stability from the same count. Authority invalidation still follows its contract DAG.

Reuse a verified browser/service and batch related corrections. Await the viewer's ready state and settled frame before capture. Inspect visibility/state before clicking optional controls; swallowing repeated timeout exceptions hides real defects and wastes time. Temporary ports belong to their verified process/command; stop only the service started for this task. These are efficiency improvements, not permission to omit requested acceptance.
