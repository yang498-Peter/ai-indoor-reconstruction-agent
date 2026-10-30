# Recovering architectural IFC from a display model

Read when exporting fragmented architectural data or investigating schema, geometry or receiving-app failures. Start with [source-frame BIM](bim-source-overlay.md) and, if native editing is requested, [native editability](native-bim-editability.md). These are exporter decision rules, not a portable copy of a capture-specific generator.

## Choose and probe the receiving path

Use the user's known application/version and intended Open/Link workflow. For the Revit 2024 Open IFC workflow, IFC2x3 Coordination View 2.0 was selected using [Autodesk's Open IFC guidance](https://autodesk.ifc-manual.com/revit/using-ifc-files-in-revit/open-ifc), which recommends that exchange path and disabling automatic joins/off-axis corrections. Recheck the guidance and importer for other versions/workflows. A header declaring an MVD is not certification, and a newer IFC dialect is not automatically better for native editing.

Before exporting thousands of detailed parts, probe a small set containing a voided wall, a hosted filler, an upper-storey slab with a hole, shared types, a multi-material assembly and a transformed part. Exercise every used primitive/transform branch, including boxes or beams with an additional transform, and the matrix handedness. Unsupported part paths must fail explicitly rather than omit objects. Keep the runtime/version and fixture result bound to the exporter.

## Derive hosts from architectural parameters

1. Retain source panel IDs, storey, wall direction/plane, thickness and vertical profiles. Group only geometrically compatible panels in the declared frame; nearby parallel faces and different thicknesses are not automatically one wall.
2. Union exact coplanar profiles to remove duplicate subvolumes and report the removed volume. Split disconnected components into distinct hosts. Do not replace a complex profile by an uncut bounding box or snap measured planes to make grouping easier.
3. Use the profile envelope and its difference from the actual wall union to derive explicit void geometry. Confirm each door/window against the intended host, aperture coverage and source identity, then create void/fill relationships. Reject missing hosts and unintended multiple fillers of one opening; intentional open passages may remain unfilled.
4. A semantic carrier under a header may extend through empty space only if that entire added region is voided and readback preserves the original visible wall union. Record this modelling choice. It does not prove native door resizing will update adjacent independently measured wall fragments.
5. Give architectural sweeps their axes, dimensions, material layers, stable GUIDs and source properties. Keep coordinates correct while parenting placements to storeys: converting world placement to a storey-relative one requires subtracting the storey transform, not applying its elevation twice.

Keep fixed frames, leaf assemblies and glazing out of duplicate furniture exports. When extracting columns/beams from a porch or carport assembly, remove their geometry from the represented parent. Choose real semantic classes where supported; use an honest proxy for uncertain objects rather than inventing structural intent. Never label cladding as a load-bearing footing solely to avoid a proxy.

## Diagnose failures without weakening acceptance

| Failure | Investigation and repair | Result to retain |
| --- | --- | --- |
| IFC2x3 shared type inverse cardinality | A type must not acquire a new `IfcRelDefinesByType` for every instance when its inverse allows at most one. Reuse the relationship and append related occurrences. | Schema + EXPRESS validation on regenerated files, not merely successful serialization |
| Open or zero-volume slab after tessellation | Inspect near-coincident vertices, microscopic spikes and collapsed strips. Apply producer-side precision cleanup only at a justified numerical scale; retain real holes and compare area, symmetric difference and bounds. | Before/after profile differences and collapsed components; no inherited repair tolerance |
| Multi-solid landing appears non-manifold after welding | Inspect representation items/connected solids independently. Touching closed solids can become non-manifold when indiscriminately merged. | Closure and positive volume per intended solid; aggregate contacts/overlaps assessed separately |
| Floor and ceiling occupy the same hidden layer | Compare physical intervals and supported finished/soffit levels; revise the inferred assembly partition. Renderer depth bias only hides the overlap. | Changed thickness and double-counted volume, with supported surfaces preserved |
| Lost or misplaced details after conversion | Inspect primitive type, all transform stages, matrix determinant/winding, per-item colours and actual product bounds. | Counts and readback views from the final IFC, not from the original GLB |

Precision cleanup is an export repair, not scan accuracy or construction regularization. An intentionally open soft furnishing/printed surface may stay a surface; a failed wall/slab solid must not be relabelled as a surface just to pass. Individually valid solids can still overlap or block circulation. Per-element quantities are not automatically a unioned material takeoff.

## Validate and deliver the actual exported files

- Reopen each final IFC. Check schema/EXPRESS, finite geometry, relevant positive volumes/closure, unique GUIDs, units, hierarchy, material/type relations, required properties and host/void/fill coverage. Compare wall net volume after subtraction with its source profile and stored quantities.
- Generate the inspection mesh from that IFC tessellation. Inspect both storey plans, stair openings, roofs/site and affected details; preserve material IDs per face/part when the reader supports them. Compare it with the source model in a declared frame and pick actual IFC GUIDs. A screenshot of the old web GLB cannot validate the IFC.
- Keep an architecture-only file and a detailed furnishing/site alternative when that reduces receiving-app cost. Preserve shared logical GUIDs and label the files as alternatives, not additive layers. Include dimensions/profiles and an element schedule for tracing edits; do not describe a parameter JSON as a native project.
- Package final file hashes, input/producer/runtime identity, properties, geometry-change notes and a concise version-specific import guide. Include the appearance limitations of curves/textures/lettering and the unresolved measured/inferred scope. Check ZIP integrity and fetched bytes if a download is published; use the existing delivery inventory tooling rather than another parallel hash framework.
- If the target app is unavailable, finish these local checks and deliver a clearly labelled candidate. A receiving-side read-only helper can distinguish actual Wall/Floor/FamilyInstance classes from DirectShape/imported geometry by stable IDs. Merely providing such a helper does not mean it ran. Native parameter edits, host response, storey perturbation and save/reopen remain separate pending tests.

Maintain reusable invariants rather than exporting the case generator wholesale. Before promoting a helper, remove capture IDs, fixed host maps, local paths and floor levels; test different topology, coordinate transforms, typed occurrences, overlapping fragments and profiles with real holes. Publication of a skill does not authorize publishing its source capture or unverified BIM candidate.
