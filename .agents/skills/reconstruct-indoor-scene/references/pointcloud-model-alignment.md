# Correct visible point-cloud/model disagreement

Use after an overlay exposes offsets in fixed objects, openings or furniture. This is a presentation correction method, not a new authority gate. Reuse unchanged source evidence; do not decorate before resolving the discrepancy.

## Separate registration errors from authoring errors

First verify the source identity, complete levelling/origin matrix, display mapping and the actual loaded model. Compare preview points with the declared full-cloud sampling transform. A uniform offset across unrelated surfaces suggests registration; individually wrong objects with aligned floors and surrounding walls need producer corrections. Do not translate the whole building to fix one cabinet.

Keep a before model, the changed producer fields and fixed inspection cameras. For each region, pair an unannotated full-resolution section with the model overlay and relevant original photographs. Capture-specific scripts, camera positions, wall thicknesses and ROI tolerances are not portable defaults.

## Discriminate the competing surfaces

- **Walls and protrusions:** inspect low, middle and high bands along neighbouring clear lengths. Separate cabinet fronts, the wall behind them, local wall returns, boxing and downstands. A dominant plane or a small residual can describe the wrong physical surface. Retain competing peaks and their spatial extent; do not average them into one wall.
- **Windows:** measure the surrounding wall, frame plane, reveal, sill/head, individual mullions and open leaves separately. Determine whether a photographed surface is glass, mirror, radiator or a genuine opening. A correction updates the host opening and dependent floor/trim, not just the visible frame. Record captured sash pose separately from a requested closed presentation pose.
- **Air conditioners and fixed fixtures:** use plan plus elevation to measure mounting centre, width/height and the correct front/back face. A correct front-face residual alone cannot verify lateral position or height. Compare the device to neighbouring wall/ceiling lines.
- **Furniture:** verify each instance's centre, orientation and relevant height bands. Curved seat cushions and chair backs are not planar walls. Correct supports relative to their actual floor/platform, and inspect all instances changed by a shared generator. Repeated count and attractive regular spacing do not establish placement accuracy.

If a narrow ROI merely selects the intended face, repeat with a wider or shifted ROI and inspect competing returns. Evidence that cannot distinguish surfaces stays uncertain. There is no universal millimetre threshold or automatic whole-object acceptance here.

## Portable batch observation tool

Install the presentation dependencies described in [the portable setup](../../../../docs/PORTABILITY.zh-CN.md). Prepare the existing metre/Z-up evidence cache, then provide a local query JSON. All coordinates below belong to a synthetic example, not a real scene:

```json
{
  "modelToCache": [[1,0,0,0],[0,0,1,0],[0,1,0,0],[0,0,0,1]],
  "queries": [{
    "id": "fixture-front", "roi": [[3.2,3.8],[-4.3,-3.7],[1.7,2.3]],
    "axis": 0, "binM": 0.01, "radiusM": 0.01,
    "nodes": ["fixture"], "side": "max"
  }]
}
```

```powershell
python scripts/presentation_alignment.py --cloud <source.las> --work <cache-work> --queries <queries.json> --model <current.glb> --before-model <before.glb> --output <work>/alignment.json
```

`modelToCache` maps world-space GLB vertices to the prepared `[x, depth, height]` cache. Supply the actual rigid mapping; do not copy this example when the producer uses `[u,height,-v]` or another convention. The tool applies GLB node transforms, verifies source/cache hashes once, loads the cache once and handles all queries in that invocation. Exact node names avoid silently matching the wrong material. Optional `modelRoi` limits vertices; optional `beforeNodes` names renamed old nodes. An absent old node is reported as missing, not zero error.

Each result retains query/input/producer hashes, raw ROI point counts, dominant-bin neighbours, selected-surface spread and before/after differences. Empty evidence remains `NO_SUPPORT`. The report always remains `OBSERVATIONS_ONLY`: it is not a plane fit, spatial index, source-to-photo association, global registration check or independent survey validation. The NPZ cache still needs RAM for a full load; use the existing indexed services for larger captures. Batch loading reduces repeated reads, not the geometry work needed to decide which surface is correct.

## Finish on one actual candidate

Edit the geometry producer; rebuild the affected objects, openings and adjacency. Check raw/model pairs in a fixed set of useful regional views, then verify the actual served model hash and loaded cloud. Freeze geometry before regenerating IFC, COLLADA, previews and download manifests. When the geometry changes again, invalidate its exports and screenshots. Keep observation metadata and internal review language out of the customer interface.

Use `scripts/validate_presentation_ifc.py --ifc <model.ifc> --output <work>/ifc-readback.json` for portable schema/EXPRESS and geometry readback. It returns a failing exit code for invalid/empty results and records the file hash. It does not replace solid closure, source-overlay comparison, target importer or desktop checks from [bim-source-overlay.md](bim-source-overlay.md). Use the existing delivery inventory tool for package/served-byte parity rather than creating another checksum format.
