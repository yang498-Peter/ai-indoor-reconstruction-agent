# Presentation BIM and source point-cloud overlay

Read when exporting a reconstructed presentation into IFC for a receiving BIM/point-cloud viewer. This route does not certify measured authority or parametric construction design.

## Bind geometry to the receiving coordinate frame

Inspect the actual importer before choosing an IFC dialect and geometry representation. The current MVPStudio worker is `cloudstudio-windows/web-uploader/scripts/ifc_worker.py`; it accepts IFC2X3/IFC4, metre-normalizes geometry, uses world coordinates and rebases its cache around a model origin. Recheck this contract on later versions.

Before exporting a full detailed scene, exercise a small synthetic fixture through the intended schema and importer: a wall with a filled opening, a second-storey slab, a multi-material object and a non-zero translated/tilted frame. Check the round trip, voids, colours and product counts. Reuse its still-bound result until the exporter, runtime or importer changes. This catches representation/material incompatibility before a costly whole-scene export; it is not evidence of the real scene's dimensional accuracy.

- Keep source XYZ, levelled presentation coordinates and renderer coordinates distinct. Export vertices with the full inverse source-to-presentation matrix, including fitted floor tilt and translation. An axis swap alone loses alignment. For small fixtures above the floor, transform the measured 3D centre; mapping only its floor footprint and adding height changes horizontal position under tilt.
- Record source and model hashes, units, matrix, source frame identity and exported pose. A local capture without a CRS must not acquire an invented EPSG or map conversion.
- Use semantic classes, a project/site/building/storey hierarchy, stable logical-object GUIDs, material/style assignments and evidence properties. Preserve genuine door apertures and explicit void/fill relationships. Aperture extents must match the host's actual opening; a slightly oversized boolean can remove jambs and frames. Door frames belong to the door assembly, not material parts cut by the wall opening.
- Parametric solids are useful when geometry is truly described by parameters; IFC4 tessellation is suitable for reconstructed curved furniture. Name that delivery honestly and keep simplified/hidden construction identifiable in properties.

## Retain editable architectural intent

Preserve wall/slab/ceiling profiles, thickness or extrusion depth, bottom/top levels, aperture bounds and host/fill IDs in the scene producer's architectural data. Derive both the web meshes and IFC sweeps from those parameters. Do not reconstruct an editable wall from a decorative mesh's bounding box or hand-patch a generated scene. Use physical storeys and spaces, not renderer group indices, for spatial containment; decorative cabinet glazing is not an architectural `IfcWindow`.

Check both a boolean void and its semantic relationship: `IfcOpeningElement` belongs to the host through `IfcRelVoidsElement`; a real door/window filling uses `IfcRelFillsElement`, while an open passage need not have a filler. Keep frames and jambs outside the subtracted solid. Avoid invalid material associations on opening elements; validate against the selected IFC schema. Retain a stable source/object identity across architectural and source-frame variants and label them as alternatives so users do not import duplicate models.

For building solids, test watertightness, positive volume, aperture-through checks and net volume after void subtraction. Inspect L/T junction gaps, overlaps and stair openings separately: individually closed walls can still meet incorrectly. Thin glass requires an explicit physical thickness when a solid is intended; open print/plant surfaces may remain surfaces with that scope recorded. Hidden wall-core thickness and unobserved construction stay inferred; do not fabricate load-bearing, fire-rating or certified LOD properties. Per-element net volume is not a unioned building quantity takeoff.

## Preserve the visible result in the actual importer

MVPStudio currently takes the first material of each represented product and does not display UV textures. Use one material per represented part, aggregated under its semantic logical object. Do not duplicate a represented parent and its represented children.

For necessary branding or printed panels, derive coloured contour tessellation from the provided texture, retaining alpha holes and UV mapping. Preserve the original file/decoded pixels in the visual model; the IFC rendition is a separate derived approximation. Use constrained triangulation for polygons with holes, preserve face orientation, and keep graphics outside their host without z-fighting. Inspect logos and lettering at the intended camera distance; colour counts alone do not prove legibility.

New fixtures need an evidence loop: selected full-resolution height bands and local clusters for count/centre, photographs for arms/lenses/attachment. Keep ROI/support/spread and distinguish the observed lamp centre from interpreted fittings. Avoid assuming lamp heads sit above the wall top just because their brackets do.

Use a small number of shadow-casting lights in the web presentation; visible fixtures and non-shadow-casting spotlights can supply the remaining illumination. Verify the whole existing tour after a rendering change and report the measured browser/viewport, not a generic FPS promise.

## Validation and delivery

1. Reopen the IFC and run schema/EXPRESS validation plus geometry creation. Check finite coordinates, units, semantic counts, missing parts, styles and object bounds. Open printed surfaces are intentional; building/furniture solids should not become open merely to bypass a failed check. A valid IFC4 file does not establish compliance with a specified MVD/IDS, construction code or dimensional acceptance requirement; validate those separately when requested.
2. Compare stored IFC coordinates and placements against the inverse-transformed source geometry. Geometry kernels can retriangulate collinear artwork vertices, so a nearest-vertex comparison alone is not a surface-equivalence test. Keep numerical export round-trip error separate from reconstruction accuracy.
3. Import through the actual receiving backend/worker and bind the resulting cache to the final IFC hash; inspect represented/failed product counts and geometry. Then, where the target UI is available, load it with the actual source point cloud. In MVPStudio, new imports default to manual placement near the camera pivot: use **Alignment → Use IFC coordinates in current dataset** when source units and frame match. Verify the matrix, source binding and visible overlap. Worker success alone is not proof of UI alignment, desktop acceptance or save/reopen behaviour; report exactly which layer ran.
4. Check model-only and simultaneous overlay views, colour/opacity, then save and reopen. Verify placement and camera framing separately. A successful geometry restore can coexist with a late scanner fit resetting the camera; record it and verify **Locate** as a workaround instead of reporting full project-display acceptance.
5. Deliver the `.ifc`, its input-bound validation report and a short import guide. Add the download to the existing showcase and allow `.ifc` on its explicit static-extension list. Check downloaded bytes against the validated IFC hash. Preserve other model routes and raw captures.

Use a verified installed IfcOpenShell runtime when available; another computer can install this repository's `requirements-presentation.txt` in its own virtual environment. `scripts/validate_presentation_ifc.py` provides standalone schema/geometry readback. In a separate MVPStudio checkout, probe its current worker and dependencies rather than assuming that another machine's `web-uploader/.pydeps/site-packages` exists. Its `tests/helpers/roadmap-real-workflow.mjs`, when present, can exercise import/save/reopen; it is not shipped here and is not a prerequisite for this repository's portable smoke. Backend tests are not installed WebView2 or external BIM application acceptance.
