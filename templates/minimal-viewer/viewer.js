const canvas = document.querySelector('#view');
const status = document.querySelector('#status');
const context = canvas.getContext('2d');

function resize() {
  const ratio = window.devicePixelRatio || 1;
  canvas.width = Math.max(1, Math.floor(canvas.clientWidth * ratio));
  canvas.height = Math.max(1, Math.floor(canvas.clientHeight * ratio));
  context.setTransform(ratio, 0, 0, ratio, 0, 0);
}

function walls(scene) {
  return Object.values(scene.nodes || {}).filter(node => node.type === 'wall' && Array.isArray(node.start) && Array.isArray(node.end));
}

function render(scene) {
  resize();
  const rows = walls(scene);
  if (!rows.length) {
    status.textContent = 'scene loaded; no wall nodes';
    return;
  }
  const points = rows.flatMap(row => [row.start, row.end]);
  const xs = points.map(point => point[0]);
  const ys = points.map(point => point[1]);
  const bounds = { minX: Math.min(...xs), maxX: Math.max(...xs), minY: Math.min(...ys), maxY: Math.max(...ys) };
  const width = canvas.clientWidth;
  const height = canvas.clientHeight;
  const scale = Math.min((width - 48) / Math.max(1, bounds.maxX - bounds.minX), (height - 48) / Math.max(1, bounds.maxY - bounds.minY));
  const project = point => [24 + (point[0] - bounds.minX) * scale, height - 24 - (point[1] - bounds.minY) * scale];
  context.clearRect(0, 0, width, height);
  context.lineCap = 'square';
  for (const wall of rows) {
    const [x0, y0] = project(wall.start);
    const [x1, y1] = project(wall.end);
    context.strokeStyle = wall.evidenceStatus?.startsWith('accepted') ? '#d1d5db' : '#f59e0b';
    context.lineWidth = Math.max(2, Number(wall.thickness || 0.12) * scale);
    context.beginPath(); context.moveTo(x0, y0); context.lineTo(x1, y1); context.stroke();
  }
  status.textContent = `${rows.length} wall nodes; scaffold is not a delivery acceptance view`;
}

const sceneUrl = new URLSearchParams(location.search).get('scene');
if (sceneUrl) {
  fetch(sceneUrl).then(response => {
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    return response.json();
  }).then(render).catch(error => { status.textContent = `load failed: ${error.message}`; });
}
window.addEventListener('resize', () => sceneUrl && location.reload());
