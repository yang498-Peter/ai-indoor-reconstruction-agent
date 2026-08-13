# Detail-joint review

Plan closure is not visual closure. Two measured wall centerlines can share an endpoint while separately rendered boxes still expose a light leak, black seam, T-junction notch, or z-fighting edge in an oblique close-up.

## Required sequence

1. Keep the accepted source centerlines unchanged.
2. Run `scripts/audit_wall_joints.py` on the current semantic scene.
3. Treat `WALL_JOINT_MICRO_GAP` as blocking. Re-measure the endpoints; do not conceal a real geometric gap with a larger mesh.
4. For a centerline-closed, same-material, vertically overlapping solid joint, use a render-only end overlap of 12–24 mm. Do not apply the overlap to doors, glass, windows, scan continuations, or different finish layers.
5. Capture one fresh close-up per joint family: external corner, internal corner, T-junction, wall-to-pier, wall-to-head, and finish transition. Bind the scene hash, Viewer hash, camera, projection, mode, and screenshot hash.
6. Reject the close-up if background pixels are visible through the joint, faces flicker, one wall overshoots the visible finish, or the overlap closes an opening.

## Fast review matrix

Use static views until the final runtime pass:

- global top: missing or skewed structure;
- regional top overlay: endpoint placement and room closure;
- local elevation: openings and vertical bands;
- oblique construction close-up: wall joints and slab edges;
- eye-level object focus: occlusion and camera placement;
- near-floor interior: paper-thin floors and background leaks.

Do not reopen the browser for every coordinate edit. Batch a coherent regional patch, rebuild once, inspect static outputs, and use one final runtime view matrix after the geometry is credible.
