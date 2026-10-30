# Multi-view image constraints and independent review

Use when correcting object position, size or yaw against multiple photographs, or when the user requests a numerical projection-refinement loop. The helper is a presentation measurement tool; it does not mutate Semantic Scene V2 authority or grant construction acceptance. Read [modeling decisions](modeling-decisions.md) for semantic ambiguity and [surface deviation](surface-deviation-reporting.md) for metric comparisons with the cloud.

## Establish usable observations before fitting

Inventory all requested rooms and logical objects, including those with no usable photographs. Distinguish a visible physical feature from its modelling proxy: a cabinet front, an upholstered silhouette and a thin tabletop are different fitting targets. Do not treat the bounding box of furniture plus lamp, bedding or loose items as a single observed cuboid. Fit a well-defined subassembly or hold unobservable parameters fixed.

Keep source XYZ, authoring coordinates and final renderer coordinates explicit. Bounds recorded before a renderer reflection are not final GLB bounds. Verify a basis, actual exported nodes and projection equivalence before retrieving or labelling views; a mirrored retrieval can select convincing pictures of another room.

For every selected image, bind its full path, bytes SHA-256, native dimensions, projection model/intrinsics, camera-to-world pose and coordinate frame. The same frame basename may exist as a raw fisheye and an undistorted pinhole image; they need different intrinsics and pixel labels. Reuse a screening score only as a retrieval hint. Colour correlation, image dimensions, rigid pose matrices or known camera model names do not establish pixel-level calibration.

Check calibration on independent fixed features whose source coordinates and image locations are supported. Record the feature extraction/annotation method, residuals, uncertainty, view-specific scope and evidence hashes. Do not project the model being optimized back into the image and call its own corners calibration truth. `DIAGNOSTIC`/`UNVERIFIED` views may produce conditional diagnostics but cannot pass the tool's candidate gate; missing calibration is resolved by better evidence, not changing a status string.

### Check the camera without moving the target furniture

Use `scripts/camera_control_refinement.py` with `camera-control-refinement-input-v1`. Bind the actual pose file, calibration file and a fixed source-cloud control artifact by SHA-256. Each static architectural junction carries its measured source XYZ and 3D localization uncertainty, an independently marked image pixel and 2D uncertainty, a feature description, and a fit/holdout split. The loader checks the coordinates and uncertainty against the bound control artifact. A repeated frame basename, a model corner or a claim that raw bytes were rehashed is not source evidence.

```powershell
python -X utf8 scripts/camera_control_refinement.py check --input <camera-case.json> --output <camera-check.json>
python -X utf8 scripts/camera_control_refinement.py fit --input <camera-case.json> --output <camera-fit.json>
python -X utf8 scripts/camera_control_refinement.py review --report <camera-fit.json> --review <camera-review.json> --output <camera-receipt.json>
```

Intrinsics stay fixed; only a bounded six-degree-of-freedom local pose may change. Use nondegenerate controls with different positions, heights and depth planes; one flat window grid cannot establish all pose parameters. Keep spatially separated control points out of the fitting objective. Project 3D uncertainty into the image and combine it with marking uncertainty; the covariance is frozen at the original pose, so moving the camera cannot improve its score by inflating uncertainty. Read both native pixel residuals and normalized residuals. Control uncertainty must reflect actual surface/edge ambiguity and cannot be enlarged after seeing errors.

If the original fit controls are already consistent with the stated uncertainty, the helper retains the original pose and still checks the holdout controls. Bound hits, rank/spatial degeneracy and holdout regression reject a pose proposal even when training error falls. A distinct Reviewer must inspect all selected control correspondences and the source definition, not just accept the numerical verdict. The receipt is conditional evidence for this exact image, pose and control definition; it is not intrinsic recalibration, a pixel-perfect camera claim or survey acceptance. Do not transfer it to another frame or convert it into a universal furniture-error threshold.

Tests in `tests/test_camera_control_refinement.py` show independent synthetic controls and the review contract. Reports bind current core/CLI/projection producers and software versions; after changing a producer, regenerate into a new path and rerun review rather than reusing a stale receipt.

## Freeze fit and holdout partitions

Choose fit views with meaningful baseline and complementary visible edges. Reserve genuinely separate camera positions/acquisition groups for holdout. Adjacent video frames, stereo siblings or duplicate content under another filename can leak across the split; inspect image hashes, timestamps/groups and camera separation together. A different file alone is not independent evidence.

Annotate actual visible edges/corners in native image pixels, before reading candidate errors. Record object/subassembly ID, cuboid edge/corner correspondence, uncertainty, image SHA and annotator. Source-image edge detectors can propose lines; verify object identity and the physical boundary. A decorative seam, background wall or shadow is not automatically an object edge. Partial visible segments are valid; do not extend them through occluders or invent hidden corners.

Audit the 3D edge identity independently of proximity to a projected box. For a planar source-supported tabletop, intersect annotated pixel rays with its independently measured surface and check which source coordinate stays nearly constant. This can distinguish a short end from a long side without choosing whichever candidate edge scores best. Inspect native crops around chairs, handles and overlapping furniture; a chair back can resemble the tabletop boundary. The Reviewer can also make an association mistake. If discovered, withdraw the invalid labels through an append-only correspondence audit, preserve the original pixels/reports, and create a versioned correction with the source-based reason. An error computed from withdrawn labels is a historical diagnostic, not model accuracy. Post-inspection corrected validation data are no longer untouched holdout; reserve another test if independent generalization is still required.

Freeze the observations, source/model/producer bindings, parameter bounds, variable mask and error policy. Put the threshold rationale in the case record: image resolution, marking uncertainty, calibration residual and intended use. Do not reuse a promotional example's pixel number, or adjust a gate after seeing holdout results. Never use baseline model projections as image ground truth.

## Run the reusable helper

The input is `multiview-refinement-input-v1` JSON. It contains metre/Z-up cuboid parameters, bounds and free-variable mask; image/view/camera records; fit or holdout splits and acquisition groups; native corner/edge marks with provenance; source bindings and explicit policy. See `tests/test_multiview_refinement.py` for a runnable synthetic input construction independent of customer data.

```powershell
python -X utf8 scripts/multiview_refinement.py inspect --input <case.json> --output <inspection.json>
python -X utf8 scripts/multiview_refinement.py fit --input <case.json> --output <fit-report.json>
python -X utf8 scripts/multiview_refinement.py render --input <case.json> --fit-report <fit-report.json> --output-dir <new-local-review-dir> --panel-width 800
```

Inspect verifies the evidence contract. Fit uses robust bounded least squares on fit-view observations only; holdout pixels never enter its objective. Edge distances use the projected curve, including fisheye curvature and visible near-plane segments. Corner marks still require valid front-facing projection. Scores remain native-image pixels even when review panels are reduced in size.

Read before/after fit and holdout errors per observation, per view and per object; do not rely on a pooled average that can hide a poor view. Check camera separation, Jacobian rank/conditioning, parameter-bound hits and physical displacement. Insufficient views, unobservable parameters, bad calibration, excessive error, holdout regression and missing marks remain explicit; an object does not disappear from coverage merely because it failed.

The tool outputs candidates, not accepted model changes. A lower fitting error can result from the wrong silhouette or from compensating for camera error. Stop a non-improving round and change the evidence or proxy hypothesis. If evidence or the hypothesis changes, create a new version and explain whether the old holdout has become diagnostic; a repeatedly inspected validation view is no longer an untouched test set.

## Independent Reviewer execution

Use a separate actor/runId and the checked-in `reviewer-readonly-v1` policy in `schemas/tool-policies-v1.json`, bound by file hash. The author does not manufacture a reviewer receipt. The reviewer may read source images/clouds, independently recompute observations and render derivatives into its review directory; it does not alter the model, fit observations, thresholds or candidate parameters.

Supply the frozen input, fit-report hash, source bindings, fit/holdout images and native-resolution overlays. The reviewer checks every selected input view for the reviewed object, correspondence/occlusion, calibration evidence and unexplained regressions. Verify the actual object geometry and adjacent walls/supports as well as the proxy; a visually good box may not move the detailed mesh correctly. A room/object coverage table retains unreviewed, rejected and unsupported items.

```powershell
python -X utf8 scripts/multiview_refinement.py report --fit-report <fit-report.json> --review <independent-review.json> --output <review-summary.json>
```

The report verifier checks identity/policy, artifact hashes and selected-view coverage. It cannot prove that a human or VLM actually inspected an image; the independent execution and its observations provide that evidence. Providing a reviewer script is not the same as running the review. Rejected or incomplete results must not be relabelled as accepted for publication.

### Run a complete room/object queue

`scripts/multiview_batch.py` reads a `multiview-batch-job-v1` and a separately hash-bound `multiview-object-registry-v1`. The registry fixes required rooms, the entire logical-object denominator, room associations, and fitting targets with `component` or `whole_object` scope. Keep original unassigned room metadata when a photograph later establishes an explicit association. Do not silently attach unknown objects to the nearest room. An empty required room and an unassociated object remain visible in coverage.

```powershell
python -X utf8 scripts/multiview_batch.py --job <batch-job.json> --output-dir <new-batch-dir>
```

The job binds model bytes, case inputs and exact target-to-logical-object mappings. Cases with authored observations run the actual fitter; absent cases remain `UNOBSERVED`, inadequate cases remain `INSUFFICIENT`, and invalid or rejected cases retain their error codes. The output records numerical fit/holdout summaries, physical candidate parameters, solver results and the next required action. It does not create missing observations, run the Reviewer or mutate any model. For reviewed cases, bind the complete fit report, independently authored review and verified receipt; the batch recomputes the fit, compares the full report content and re-verifies the independent review before counting acceptance.

A rejected component cannot be washed out by a successful component or a later attempt in the same queue. Acceptance of a tabletop alone never accepts the whole dining group. The complete JSON/Markdown/HTML coverage output omits private file paths and retains every object; it is an aggregate report, not a replacement for the full private evidence package. See `tests/test_multiview_batch.py` for the registry/job example and failure cases. Use a new output directory for each run; input aliases and replacement of existing output directories are rejected.

## Apply and verify supported changes

Apply only independently accepted parameters in the scene producer, into a separate candidate. Keep unsupported parts fixed and preserve exact mappings from fit target to source subassembly. Rebuild detailed geometry, affected host relations, navigation/collision data and exchange files; do not hand-patch generated JSON or a deployed GLB. Compare exported geometry with the approved parameter change and check adjacent contacts, support and room connectivity.

Check the entire producer chain for later reconstruction of the same object. Correcting a library call's door count can be undone by a subsequent refit that silently uses the old default. Prefer an explicit final parameter mapping and verify the actual exported part count, group layout and source-supported heights. A narrow horizontal rail supports a local front anchor; its fitted vertical coefficient does not measure a full-height cabinet tilt. Keep vertical regularization and hidden depth/casing/hardware inferences explicit. A source-supported layout correction and a failed pixel-fit candidate use different evidence and review scopes; one cannot turn the other into acceptance.

Separate source-support review from final-candidate review when the detailed producer adds inferred closures. Bind both reviews to the exact parameters, producer and frozen detailed artifacts. Compare all unaffected logical objects, architectural/floor bytes, exported bounds and exact wall intersections; rebind navigation to the final model and test a real approach to the changed obstacle. Preserve IFC product GUIDs and independently read back its changed element and attributes. Whole-assembly non-manifold contacts are different from an open individual solid. In photo comparisons use a proper depth buffer: average-depth triangle painting can manufacture apparent crossings. Keep a broken renderer's derivatives as invalid diagnostics and regenerate them through its producer.

Rerun affected views and real interactions before replacing the website candidate. Use [delivery verification](delivery-verification.md) for final download hashes, protected routes and atomic publication. Public packages contain the authorized model and suitable aggregate results; raw images, cloud captures, internal annotations and machine paths stay local. Keep the untouched baseline available for comparison and rollback.

Report the full requested denominator, usable view coverage, fitted/rejected/unresolved objects, independent reviewer coverage, before/after errors and actual applied subset. Image-edge error, cloud-to-model discrepancy, visual quality, native BIM behaviour and physical accuracy are separate results. No single pixel score certifies the entire reconstructed room.
