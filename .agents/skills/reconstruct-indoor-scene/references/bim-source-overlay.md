# Presentation BIM and source point-cloud overlay

Read when exporting a reconstructed presentation into IFC for a receiving BIM/point-cloud viewer. This route does not certify measured authority or parametric construction design.

## Bind geometry to the receiving coordinate frame

Inspect the actual importer before choosing an IFC dialect and geometry representation. The current MVPStudio worker is `cloudstudio-windows/web-uploader/scripts/ifc_worker.py`; it accepts IFC2X3/IFC4, metre-normalizes geometry, uses world coordinates and rebases its cache around a model origin. Recheck this contract on later versions.

- Keep source XYZ, levelled presentation coordinates and renderer coordinates distinct. Export vertices with the full inverse source-to-presentation matrix, including fitted floor tilt and translation. An axis swap alone loses alignment. For small fixtures above the floor, transform the measured 3D centre; mapping only its floor footprint and adding height changes horizontal position under tilt.
- Record source and model hashes, units, matrix, source frame identity and exported pose. A local capture without a CRS must not acquire an invented EPSG or map conversion.
- Use semantic classes, a project/site/building/storey hierarchy, stable logical-object GUIDs, material/style assignments and evidence properties. Preserve genuine door apertures and explicit void/fill relationships. Aperture extents must match the host's actual opening; a slightly oversized boolean can remove jambs and frames. Door frames belong to the door assembly, not material parts cut by the wall opening.
- Parametric solids are useful when geometry is truly described by parameters; IFC4 tessellation is suitable for reconstructed curved furniture. Name that delivery honestly and keep simplified/hidden construction identifiable in properties.

## Preserve the visible result in the actual importer

MVPStudio currently takes the first material of each represented product and does not display UV textures. Use one material per represented part, aggregated under its semantic logical object. Do not duplicate a represented parent and its represented children.

For necessary branding or printed panels, derive coloured contour tessellation from the provided texture, retaining alpha holes and UV mapping. Preserve the original file/decoded pixels in the visual model; the IFC rendition is a separate derived approximation. Use constrained triangulation for polygons with holes, preserve face orientation, and keep graphics outside their host without z-fighting. Inspect logos and lettering at the intended camera distance; colour counts alone do not prove legibility.

New fixtures need an evidence loop: selected full-resolution height bands and local clusters for count/centre, photographs for arms/lenses/attachment. Keep ROI/support/spread and distinguish the observed lamp centre from interpreted fittings. Avoid assuming lamp heads sit above the wall top just because their brackets do.

Use a small number of shadow-casting lights in the web presentation; visible fixtures and non-shadow-casting spotlights can supply the remaining illumination. Verify the whole existing tour after a rendering change and report the measured browser/viewport, not a generic FPS promise.

## Validation and delivery

1. Reopen the IFC and run schema/EXPRESS validation plus geometry creation. Check finite coordinates, units, semantic counts, missing parts, styles and object bounds. Open printed surfaces are intentional; building/furniture solids should not become open merely to bypass a failed check.
2. Compare stored IFC coordinates and placements against the inverse-transformed source geometry. Geometry kernels can retriangulate collinear artwork vertices, so a nearest-vertex comparison alone is not a surface-equivalence test. Keep numerical export round-trip error separate from reconstruction accuracy.
3. Import through the actual MVPStudio UI/backend/worker with the actual source point cloud. New imports default to manual placement near the camera pivot. Use **Alignment → Use IFC coordinates in current dataset** when source units and frame match. A button's confirmed status alone does not prove a matching dataset: verify the matrix, source binding and visible overlap.
4. Check model-only and simultaneous overlay views, colour/opacity, then save and reopen. Verify placement and camera framing separately. A successful geometry restore can coexist with a late scanner fit resetting the camera; record it and verify **Locate** as a workaround instead of reporting full project-display acceptance.
5. Deliver the `.ifc`, its input-bound validation report and a short import guide. Add the download to the existing showcase and allow `.ifc` on its explicit static-extension list. Check downloaded bytes against the validated IFC hash. Preserve other model routes and raw captures.

Use the installed IfcOpenShell runtime when available. In this Windows checkout it is supplied through `web-uploader/.pydeps/site-packages`; a missing system-Python import does not mean it must be reinstalled. `tests/helpers/roadmap-real-workflow.mjs` can start an isolated real backend and exercise import/save/reopen; it is not a replacement for installed WebView2 or external BIM application acceptance.
