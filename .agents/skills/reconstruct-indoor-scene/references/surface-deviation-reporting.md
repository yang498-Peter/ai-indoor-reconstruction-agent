# Numerical surface review, heatmaps and per-face reports

Read when the user requests numerical cloud/model alignment, wall/floor deviation, a heatmap or a per-face report. This is a measurement and reporting procedure; it does not itself pass Semantic Scene V2 authority gates or certify construction quality. For local object correction also use [pointcloud-model-alignment.md](pointcloud-model-alignment.md).

## Establish a reproducible face inventory

Bind the analysis to the actual capture, cloud/cache content hashes, model hash, units, complete coordinate transforms and analysis implementation/configuration. Confirm the model loaded by the viewer has that hash. Keep raw data read-only; a display-decimated cloud is not automatically a measurement sample.

- Give each physical face a stable ID linked to its model object, side, local basis, polygon, height/extent and outward or explicitly fixed normal. Two sides of a wall are separate faces. Include every requested wall/floor in the inventory, including surfaces with no usable observations.
- Record actual openings, boundaries, occlusions and excluded returns. Distinguish modeled area, observable ROI area and supported area; state which area is the coverage denominator. A clipped inspection band cannot establish coverage of the whole wall.
- Associate returns using spatial bounds, local sections and competing distance peaks. Inspect frames, cabinets, opposite wall faces and glass separately. Do not select only points already close to the model and then claim the model is accurate.
- Keep raw point count, valid cell count, grid pitch, minimum support, search range and exclusion rules. Repeat suspect association with wider/shifted ROIs and different height bands. Multimodal returns or unstable membership remain uncertain even when one selected mode fits well.

Choose sampling and support parameters from the capture's density, noise and required measurement scale. Preserve the parameter set with each result. A previous capture's grid spacing, peak windows and search distances are diagnostic settings, not universal defaults or acceptance tolerances.

## Compute quantities with distinct meanings

For a plane with unit normal `n` and equation `n dot x = c`, signed orthogonal residual is `d_i = n dot p_i - c`. Transform cloud points and model geometry into the same frame before computing distances. Convert metres to millimetres once at the reporting boundary.

| Quantity | Calculation and interpretation |
| --- | --- |
| Surface-to-fit residual | Fit a plane to the selected physical surface; report signed median, RMS, P95 of absolute residuals and maximum absolute residual. This describes departure from that fitted plane at the stated sampling scale. |
| Cloud-to-model residual | Measure the same observations against the actual exported face/mesh, respecting finite bounds and openings. Report residual statistics separately from the fitted-plane results; a small fit residual can coexist with a displaced model. |
| Plane displacement and angle | Orient fitted and model normals consistently. For parallel planes, signed offset follows their plane constants. For nonparallel planes, evaluate signed displacement at a declared face anchor and corners, plus the angle between normals; plane-constant subtraction alone is not a displacement. |
| Verticality or levelness | Compare the fitted plane orientation with the declared gravity/level reference. State angular and span-dependent displacement results separately from model agreement. A levelled frame estimated from this same floor does not independently verify that floor's levelness. |
| Whole-object position and dimensions | Check relevant opposite faces, lateral extents, bottom/top and openings. One front-face residual cannot prove a column centre, thickness, wall length or door width. |

Define `RMS = sqrt(mean(d_i^2))`, `P95 = percentile(abs(d_i), 95)` and `maxAbs = max(abs(d_i))`. Name the sampling population and weighting: raw returns, equally weighted cell medians and spatially weighted observations are different measurements. Do not compare their P95 values as an improvement trend without a matched protocol and face membership.

Use a robust plane fit when outliers are present; retain all observations admitted by the declared association protocol in final residual statistics, including fit-rejected cells. Report exclusion and fit-rejection counts separately. A spatial fit/holdout split can expose overfitting; it is not an independent field survey and does not remove SLAM systematic error.

Fit-plane residuals are not automatically a national-standard flatness test. A two-metre straightedge, verticality instrument or opening-squareness test has its own measurement operator and reference. Implement and validate that operator before using its acceptance limits. When the applicable standard, material, edition, method or agreed tolerance is missing, present numerical observations and limitations without a fabricated pass/fail verdict. Do not turn a requested one-centimetre accuracy target into evidence that it was achieved.

## Review and correct one face at a time

For each face, inspect its section, support/coverage, competing peaks, fit/model residual maps and numerical table. Diagnose whether the cause is registration, model position, surface association, real local shape or unresolved scan error. A high P95 alone does not identify the cause. Mark unsupported or ambiguous faces explicitly; missing data is neither zero deviation nor a passing face.

Make evidence-supported corrections in the producer, preserve the previous candidate and regenerate dependent geometry and reports. Recompute on the same source population where comparable; disclose changed masks or sampling. Recheck adjoining walls, holes, columns and floor edges affected by the edit. Leave unresolved cases visible instead of adjusting geometry until a statistic looks good. Noise and occlusion prevent a blanket claim of zero deviation or 100 percent correctness.

## Keep heatmaps and reports consistent

- Name the displayed metric: surface-to-fit or cloud-to-model, signed or absolute, and its sampling scale. Use consistent normals, units and colour limits across compared faces. Label saturation and missing support distinctly. A diagnostic colour ramp must not imply regulatory compliance when no acceptance rule has been established.
- Colour cloud observations at their original source positions. A grid projected onto the model is a separate overlay, identified as such. Do not move points onto a wall to make alignment look correct, interpolate across openings or colour unseen surfaces as measured. Keep model/cloud/heatmap independently inspectable.
- Generate viewer values, per-face images, CSV, HTML and PDF from one immutable analysis result. Each face needs object/side ID, extents, reference frame, counts, coverage denominator, exclusions, fit parameters, fit/model statistics, uncertainty/association notes and method/configuration identity. Preserve full provenance in the technical bundle while keeping private filesystem paths and credentials out of public reports.
- Include the legend, units, method limitations, device/processing uncertainty statement and any unconfirmed faces in the report. Use a supplied approved template when available; otherwise label the document as a sample report, not an exact implementation of an unseen template.

Numerical verification should include known offset/tilt and outlier cases, masks around holes, competing planes, missing coverage and frame transforms. Independently recompute representative distances against the exported mesh rather than only checking the same analysis output twice. Serialization round-trip error is an export check, not survey accuracy.

Exercise the real viewer's model/cloud/heat toggles, face selection, camera focus, legend changes and downloads under its actual nested deployment path. Confirm selected IDs, displayed values and downloaded report rows agree; verify missing/ambiguous faces remain recognizable. Decode exported images, inspect PDF text and rendered representative pages for fonts/clipping, and compare downloaded hashes with the candidate. Test an extracted delivery package without relying on the original developer drive or private cache.

When the interface supports search or filters, keep the selected face, model highlight, displayed values, report link and URL in agreement. Filtering must not silently display a new selection while the viewer retains the old one; expose empty results and make any selection change explicit. When restoring unsaved inspection notes or measurements, bind the saved state to the cloud, model and analysis identities. Do not restore stale state onto different inputs. Make the storage lifetime clear, preserve the old state on save failure, and provide an export path; browser-session storage is not a permanent archive. Verify return links and face-specific deep links, including explicit URL selection after restoring state.

For an already authorized website update, follow [delivery-verification.md](delivery-verification.md): publish a coherent version of model, analysis, viewer and reports with scoped asset checks and rollback. Do not pair a new model with stale heatmaps or substitute an unaccepted native-BIM candidate for the current download. Report import/cache preparation, analysis, rendering/export and verification time separately; analysis duration alone is not end-to-end “scan to report” performance.

