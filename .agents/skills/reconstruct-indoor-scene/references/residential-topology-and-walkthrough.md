# Residential topology, completion and walking

Read for recurring room/wall/opening errors, multi-storey residential completion, or a requested door-and-walking experience. Use the existing [modeling decisions](modeling-decisions.md) for evidence selection and the site-scene skill for outdoor capture interpretation. This reference adds checks at room boundaries; it is not a universal house template.

## Review connections, not only isolated rooms

Extend the region ledger with the adjoining room, passage or exterior zone, expected connection, visible boundary and current model object IDs. Include transitions easily excluded by room crops: stair-back enclosures, under-stair returns, kitchen island ends, laundry/pantry entrances, bay windows, front/back doors and house-to-yard steps. Only add regions supported by this capture or the user's stated scope.

Review a room plan together with its wall elevations and the next space. A convincing room image can hide a false narrow room, a sealed entry or a missed short wall. A free wall end is a question to investigate, not an automatic command to extend the wall to the nearest line.

| Ambiguous observation | Discriminating check | Avoid |
| --- | --- | --- |
| Strong vertical plane | Separate wall face, cabinet front, open door leaf, mirror and high-level band using photos and narrow depth slices | Extruding every 2D line through the whole storey |
| Scan void at a candidate door | Inspect both sides, visible frame/head, leaf position and multiple heights | Treating shared empty pixels or the unoccupied half of a double door as its full width |
| Parallel planes enclosing a thin space | Inspect camera-side visibility and near/far surface ordering; compare separate narrow depth bands | Removing the rear wall because a foreground cabinet dominates the projection |
| Short segment beside stairs | Inspect the head, return wall, tread underside and clear passage separately | Converting a lintel or partially hidden return into a full-height pier |
| Bay/window replacement | Inventory every superseded wall segment and adjacent wall tail | Adding glass while an old opaque wall or oversized floor patch remains |

Use explicit logical hosts for confirmed openings. Nearest distance alone can cut a neighbouring wall and leave the host's short connector intact. Retain rough opening, frame envelope, clear passage, sill/head and side-light roles separately. Open archways need not acquire ordinary door leaves; glazed side lights need not become pedestrian passages.

After a topology repair, regenerate affected wall pieces, frames, floor/ceiling boundaries, roof exclusions and navigation data. Inspect actual exported triangles with suitable section, ray or footprint-union checks as well as photographs. Classify intended junction/hinge contact separately from unexpected overlap; broad collision exemptions are not a repair. Do not sum overlapping regional check counts as independent accuracy samples.

## Complete floors, stairs and exterior without moving measured faces

- Anchor an assumed wall thickness on the supported face; do not mistake the observed room face for a centreline. Adjust the hidden side only when consistent with adjacent geometry and record the inference.
- Keep the observed finished floor, visible soffit and inferred assembly thickness separate. Removing a hidden floor/ceiling overlap should preserve supported visible levels, with the thickness change reported.
- Review stairs as flights, treads/risers, landings, railings, underside and upper/lower arrival connections. Check the actual tread sequence and stair opening in the exported model; global height bounds alone miss blocked landings. Report nonuniform rises or floor regularization rather than silently forcing a standard stair.
- Compare roof coverage with the union/difference of floor footprints to find missed lower-storey extensions and gable ends. A footprint identifies a candidate omission, not a measured roof pitch; use photos/cloud support or label the closure inferred.
- Check furniture against local support surfaces and each affected wall/door, including pillows, handles and wardrobe contents. Preserve the observed front face before shortening an inferred hidden cabinet back. Retest all instances changed by a shared generator.
- Match furniture and vehicles by silhouette, major parts, colour and orientation before small decoration. Inspect both sides and the underside of thin cloth, roofs and vehicle panels; a double-sided material does not close a genuinely missing surface. Copy only legible, source-supported lettering/plates; retain uncertainty where characters cannot be read. IFC without textures may need a separately documented appearance approximation.
- For sloped yards/driveways, place wheels, steps and furniture on the local surface. Do not flatten the entire exterior to an interior storey datum. Keep supported terrain and extrapolated enclosure distinguishable.

## Door interaction and continuous walking

Keep the downloaded model's rest pose explicit. Store each leaf's logical ID, fixed frame, hinge transform, swing and grouped-leaf relationship separately from the runtime animation. Test the real leaf and handle against the frame/wall over the swing, not just a box around the closed door. Check click targets, paired leaves, reversal during animation, room switches and reduced-motion handling when these features are present.

Derive navigation from the current exported geometry, bound to its hash. Floor surfaces and height-aware obstacle slices should retain stair access and door apertures; one full-height convex hull around an entire stair can block it. Door collisions follow the current leaf pose, and closing should stop or reverse before intersecting the walker. State conservative collision-proxy limitations.

Exercise a continuous ground-to-upper-floor-and-back route using real input, without teleporting between flights. Also test a closed door blocking movement, opening it and passing through, exterior slopes and every new spawn preset. A valid initial position does not establish route connectivity or headroom.

Track physical keys independently when several keys map to one action. Clear movement on blur, interface focus and exit; releasing one of two forward keys must leave the other active. A slow frame alone is not a key-release event: bound/substep movement integration instead. Test pointer lock, escape, drag look and touch-button release only when supported. Do not run focus-changing tools concurrently with a held-key acceptance test; retain a failed observation and investigate focus separately before attributing the cause.

## Keep the runtime and delivery small

Build a compact runtime index from the current producer outputs for labels, selection, door and navigation fields. Do not download full construction intermediates merely to read object names. Validate the index against the model and regenerate it when geometry changes; retain IDs and movement fields needed by controllers.

For an authorized web publication, use the existing [delivery verification](delivery-verification.md) workflow: explicit public files, protected existing routes, atomic release and rollback, fetched byte hashes and actual browser checks. A configured domain must be verified through DNS/TLS/HTTP and served model identity; an IP URL or a config entry alone does not prove the domain works. Keep raw captures, credentials and internal evidence out of the public runtime bundle; publish derived previews only within the authorized scope.

User approval of the model's appearance is useful presentation feedback. It does not complete unrun source-accuracy, native BIM or construction checks. Reuse this decision process, not the previous home's room counts, transforms, offsets, text, thresholds or acceptance receipts.
