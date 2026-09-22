# Panorama and point-cloud fusion

Use this procedure when panoramas or a dense ordinary-photo sequence contains
more semantic layout information than the residual point cloud.

## Build a visual inventory before instance geometry

1. Inventory every panorama, resolution, timestamp and pose class. Keep exact,
   interpolated, nearest and missing poses distinct.
2. Render numbered contact sheets for the full sequence. Review the complete
   sequence once to identify stable walls, circulation, furniture families and
   camera motion. Do not select only visually convenient frames.
3. Select key frames that collectively expose every perimeter run and furniture
   zone. Convert equirectangular frames to rectilinear headings, normally four
   headings 90 degrees apart; add narrower intermediate headings when seams,
   people or foreground furniture obscure the view.
4. For each stable family, record what the images prove: approximate count,
   orientation, repeated spacing, seat or monitor count, wall association,
   material and occluded parts. Separately record what they do not prove, such
   as exact source-metre centroid, hidden depth or construction thickness.

Contact sheets prove sequence consistency; rectilinear views support local
semantic reading. Never measure wall or furniture dimensions directly from an
equirectangular thumbnail.

## Fuse evidence by role

- Panoramas lead semantic type, approximate count, facing direction, repeated
  layout, adjacency, material, openings and occlusion completion.
- The colored point cloud leads global axes, floor and ceiling levels, wall
  planes, supported centroids, dimensions, heights and collision checks.
- Valid poses bind an image observation to a source region. A nearest or
  interpolated pose can support a broad room zone but must not be presented as
  exact instance binding.
- Repeated visual modules constrain missing instances. Fit the visible members
  to one building-axis family, then place an occluded member only when spacing,
  circulation and residual points are mutually compatible.

If visual and point-cloud evidence disagree, first test projection, coordinate
mapping, dynamic people, reflections and scan sparsity. Keep both hypotheses
visible until one explanation fits the whole sequence. Do not automatically
prefer the point cloud when the disputed surface lies in an obvious scan gap.

## Complete scan gaps without laundering certainty

Photo-proven furniture must not disappear merely because its tabletop or legs
are missing from the scan. Complete a logical family from the most stable
visible properties: for a workstation this may include tabletop, divider,
monitors, pedestals and chairs. Snap inferred members to the measured family
axis and keep clear circulation.

Every completion beyond measured support remains `accepted-inferred` and stores:

- the panorama or photo frames that prove existence and family;
- the point-cloud region or repeated-axis rule used for placement;
- an uncertainty interval for centroid, yaw or size;
- a plain-language reason explaining the missing scan evidence;
- an explicit distinction between approximate count/layout and exact instance
  authority.

Reject an inferred object when it contradicts another stable view, blocks a
photo-visible passage, intersects accepted structure, duplicates a single
object seen from multiple frames, or requires irregular spacing without an
independent explanation.

## Visual layout gate

Before presentation review, compare a fresh top/oblique model against at least
two independent key panoramas per major furniture zone. The following must be
recognizably consistent without reading the ledger:

- number of major desks or workstation islands;
- their dominant direction and row arrangement;
- approximate seats and monitors per logical group;
- relationship to window walls, storage, doors and main circulation;
- which loose people, bags, papers and temporary cables were intentionally
  withheld.

A geometrically clean but semantically wrong furniture arrangement fails this
gate. A sparse raw footprint is not a sufficient reason to keep the wrong count
or orientation.

## Reusable helper

When the checkout contains `scene-core/panorama_rectilinear.py`, inspect its
current help and projection contract before using it for rectilinear evidence
sheets. Otherwise use an available verified panorama projection tool; do not
assume a capture-specific helper was distributed with this skill. Preserve the
input panorama hash, heading, pitch and field of view in the evidence record.
Projection does not perform recognition or validate a camera pose.
