# Native BIM editability and storey constraints

Read when the requested deliverable must be editable in Revit or Archicad: native walls/slabs, hosted doors/windows, curtain-wall systems or elements that follow storey changes. For a visual IFC overlay alone, use [bim-source-overlay.md](bim-source-overlay.md). Apply the checks below to the requested component classes and receiving workflow; do not turn every presentation export into a native-authoring project.

## Establish the receiving behavior before bulk export

Use known session context first. Resolve the receiving application/version, Open IFC versus Link/Merge/import workflow, translator/settings and required editing behavior when they materially affect the result. If both applications are required, keep separate acceptance records; success in one does not certify the other. A reference IFC header identifies its exporter, not the user's current application version or the ability to recreate native objects on import.

Probe a small representative set early: wall with hosted door/window, floor slab, curtain-wall module and storeys. Use a synthetic fixture to test exchange behavior and representative actual geometry to check scene-specific fidelity. Exercise the intended edits in the actual receiving application before producing repeated full-scene revisions. If the user reports the same native-editing failure after semantic-field changes, investigate the importer or native-authoring route; do not keep adding property names and claim the behavior is fixed.

| Evidence layer | What it establishes | What remains separate |
| --- | --- | --- |
| IFC schema, relations and geometry reopen | Exchange data is structurally readable; classes, host relations and bounds can be checked. | Native tool/category, usable parameters and dynamic constraints. |
| Web/worker import and overlay | The tested importer renders the exported geometry and coordinate mapping. | Revit/Archicad behavior and real-world accuracy. |
| Receiving application's object inspection | The actual imported category, tool/type and parameter values. | Whether edits work and survive save/reopen. |
| Native edit plus save/reopen | Tested parameters and host behavior persist for the tested version/workflow. | Storey linkage unless a storey is actually changed; other applications or untested classes. |
| Storey perturbation and restoration | Tested elements respond correctly to their intended base/top/home-storey relation. | Dimensional accuracy against independent physical measurements. |

Record these results separately. External-application tests not performed stay `NOT_RUN`; fixture/source/API-mock tests cannot substitute for them.

## Preserve semantics and test native operations

Keep source-bound profiles, thicknesses, opening extents, base/top offsets and stable logical IDs in the producer. Preserve the original capture, accepted geometry and source/building-coordinate variants; label coordinate variants as alternatives. Compare a supplied native-authored reference by class/type, representations, placements, containment, materials and void/fill relationships. Do not copy its dimensions, storey heights, door swing, library names or construction properties into another capture without evidence.

| Component | Exchange intent to preserve | Receiving behavior to test |
| --- | --- | --- |
| Wall | Wall class, profile/axis, thickness, material intent and host openings. | Wall tool/type; change thickness; insert or modify a hosted opening without losing the wall. |
| Slab | Slab role, footprint/holes, thickness, elevation and home storey/offset. | Slab tool; edit thickness/contour and verify elevation behavior. A slab need not have a wall-like top link. |
| Door/window | Correct class/type, width/height, opening placement, host void/fill and supported operation evidence. | Native Door/Window tool with a valid host and required library/parameters; edit size and verify the host opening. An object named “Door” is insufficient. |
| Curtain wall | Curtain-wall system plus meaningful panel/frame decomposition and source geometry. | Curtain Wall tool/system with editable grid/frame/panel settings. A generic object or grouped glass meshes are not equivalent. |
| Column/beam | Correct class, placement, section and supported level relations. | Native tool/section edits and the class-specific base/top/home-storey behavior requested. |

Schema class labels, custom property sets, `IfcMetric` constraints and storey containment do not by themselves establish a receiving application's native parametric constraint. A standard IFC element exported from a native project also does not guarantee its editable native history can be recreated on import. Keep visual material-part workarounds conditional on the target importer; do not apply the viewer's first-material workaround blindly to native authoring.

## Distinguish floor observations from storey design

Define observed floor/soffit elevations, inferred structural thickness, modelling reference storeys and intended element constraints separately. A scanned slab underside plus assumed thickness does not establish the next storey's measured floor. Do not copy a reference model's storey height or invent a level merely to make a constraint appear complete.

For each affected element, retain home/base storey, bottom offset, optional top-linked storey and top offset, with their provenance. Preserve intentional negative offsets and existing bounds when adding supported links. Linking must not silently stretch a measured wall to a guessed level. Check required upper storeys exist and disambiguate conflicting level mappings before native mutation.

To verify a dynamic link, work in a backed-up project copy. Change a representative storey by a documented small amount appropriate to the model; read the expected linked endpoints and geometry, and confirm unrelated/fixed elements behave as specified. Restore the storey, then read back both settings and geometry against the baseline and save/reopen. An API success response, home-storey label or a visible level line alone is not this test. If restoration cannot be verified, retain the backup and mark the test unresolved.

## Measure the cost of native simplification

Before converting irregular facade geometry into a regular native curtain-wall system, compare original and candidate frame axes/corners and panel surfaces in a common frame. Report per-module displacement, angle, maximum error and withheld panels. Keep model-to-model regularization error separate from cloud-to-model residuals using [surface-deviation-reporting.md](surface-deviation-reporting.md).

Separate frame reference plane, glass setback and captured open-sash pose; glass-corner distance to a frame plane is not automatically scan misalignment. Investigate large changes instead of raising a gate or deleting unconfirmed panels. Choose a better-supported system subdivision or retain an explicitly limited candidate when native regularity conflicts with measured geometry. Native classification does not excuse a less faithful reconstruction.

## If a native adapter is needed

Check the supported receiving-app API/add-on and version-specific field behavior before implementing it. Prepare an explicit dry run, element/IFC-ID mapping, proposed creations/changes and geometry-difference report. Prefer exact stable IDs; never silently match by nearest position or display name. Reference exporter versions and IFC GUID representations are evidence to verify, not defaults for every machine.

Use a backed-up copy, check active project identity and units, reject missing/duplicate mappings and validate required story/library dependencies before mutation. Persist per-element results and old/new native IDs so a partial run is recoverable and a retry cannot duplicate doors or curtain walls. After a timeout with an unknown mutation outcome, query current state before retrying. Preserve originals until replacements, hosts and geometry are verified; then check the intended final set has no duplicates. Never silently modify other open projects or depend on a plugin's success message without read-back.

In one Tapir/Archicad prototype, top linkage required dedicated wall/column modification fields; supplying height concurrently could cancel the link, and generic element-detail writes did not implement every native parameter. IFC-ID matching also required a version-specific representation check. These observations are not a proven portable repair recipe. Verify the installed API contract anew when adopting an adapter.

If Archicad/Revit runs only on a colleague's computer, finish the locally possible schema, geometry, mapping and package checks. Deliver a clearly labelled candidate plus concise receiving-side steps and expected results for native category, thickness/size, hosting, curtain-grid edits and storey perturbation/save/reopen. Keep native acceptance pending; do not publish a candidate as the confirmed fix or overwrite a previously accepted download merely because its local tests pass. An explicitly requested candidate publication must remain labelled as unverified. Do not keep asking for machine location once answered.

