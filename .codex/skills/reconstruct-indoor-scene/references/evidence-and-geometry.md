# Evidence and geometry rules

## Evidence order

1. Unannotated source point cloud, local sections, and source coordinate measurements.
2. Posed source photos or panoramas.
3. Repeated construction, symmetry, collision constraints, and spatial common sense.
4. Generic algorithm suggestions.

Never average conflicting evidence blindly. Explain the conflict, choose the stronger source, and record the decision.

## Required element evidence

For each accepted building element record:

- source start/end, center/size, or polygon vertices;
- bottom, top, thickness, and coordinate mapping;
- plan evidence and perpendicular residual;
- elevation evidence and observed height bands;
- photo/material evidence or an explicit inference reason;
- opening disposition;
- accepted/rejected status and replaced element IDs.

## Geometry rules

- Fit the shell before room dividers and the dividers before furniture.
- Share exact endpoints at wall, window, door, and pier junctions.
- Measure both faces when a wall or facade has thickness; choose and name the centerline or visible face consistently.
- For repeated rooms, derive one facade axis and one divider normal. Intersect them analytically instead of snapping each room independently.
- Split glazing into glass, frames/piers, sill or spandrel, and opaque head. A photo-confirmed floor-reaching window must not inherit an arbitrary 0.55 m sill.
- Model doors as gaps plus door/frame geometry. Never place a wall segment through a door interval.
- Treat transparent returns as sparse evidence, not absence. Use frames, blinds, mullions, adjacent intersections, and photos.
- A camera trajectory or crop envelope is not a floor boundary.
- Do not close intentional passage openings merely to satisfy topology.

## Inference

Inference is allowed when scans are incomplete. Accept it only when it improves physical consistency without contradicting measured evidence. Record the assumption, affected parameters, and how a later scan could falsify it.
