export function orientedPolygonFrame(points) {
  if (!Array.isArray(points) || points.length < 3) throw new Error('polygon needs at least three points');
  const plan = points.map((point) => [Number(point[0]), Number(point[2] ?? point[1])]);
  if (plan.some((point) => !point.every(Number.isFinite))) throw new Error('polygon contains a non-finite point');

  let longest = null;
  for (let index = 0; index < plan.length; index += 1) {
    const start = plan[index];
    const end = plan[(index + 1) % plan.length];
    const dx = end[0] - start[0];
    const dz = end[1] - start[1];
    const length = Math.hypot(dx, dz);
    if (!longest || length > longest.length) longest = { dx, dz, length };
  }
  if (!longest || longest.length < 1e-6) throw new Error('polygon has no usable edge');

  let ux = longest.dx / longest.length;
  let uz = longest.dz / longest.length;
  if (ux < 0 || (Math.abs(ux) < 1e-12 && uz < 0)) {
    ux = -ux;
    uz = -uz;
  }
  const vx = -uz;
  const vz = ux;
  const projected = plan.map(([x, z]) => ({ u: x * ux + z * uz, v: x * vx + z * vz }));
  const minU = Math.min(...projected.map((point) => point.u));
  const maxU = Math.max(...projected.map((point) => point.u));
  const minV = Math.min(...projected.map((point) => point.v));
  const maxV = Math.max(...projected.map((point) => point.v));
  const centerU = (minU + maxU) / 2;
  const centerV = (minV + maxV) / 2;

  return {
    center: [centerU * ux + centerV * vx, centerU * uz + centerV * vz],
    width: maxU - minU,
    depth: maxV - minV,
    yaw: Math.atan2(-uz, ux),
    axis: [ux, uz],
    crossAxis: [vx, vz],
  };
}
