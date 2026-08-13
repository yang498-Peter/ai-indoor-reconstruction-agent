const canvas = document.querySelector('#canvas');
const context = canvas.getContext('2d');
const layerSelect = document.querySelector('#layer');
const toolSelect = document.querySelector('#tool');
const categorySelect = document.querySelector('#category');
const measure = document.querySelector('#measure');
const colors = { wall:'#ff8468', glass:'#60dff5', door:'#ffd16e', window:'#a2e8ff', column:'#ff9f66', 'floor-zone':'#ab8cff', 'ceiling-zone':'#e982ff', cabinet:'#78d69b', furniture:'#31d4a1', clutter:'#d6a46d' };
const layerSources = { xray:'./generated/focus-pointcloud-xray-composite.png', tabletop:'./generated/focus-pointcloud-tabletop-slice.png', chairs:'./generated/focus-pointcloud-chair-slice.png', overview:'./generated/focus-pointcloud-orthophoto.png' };
let metadata;
let image;
let documentData;
let draft = null;

function pixelToSource(point) {
  return [metadata.minX + point[0] * metadata.resolutionMPerPixel, metadata.maxY - point[1] * metadata.resolutionMPerPixel];
}

function sourceToPixel(point) {
  return [(point[0] - metadata.minX) / metadata.resolutionMPerPixel, (metadata.maxY - point[1]) / metadata.resolutionMPerPixel];
}

function eventPoint(event) {
  const rect = canvas.getBoundingClientRect();
  return [(event.clientX - rect.left) * canvas.width / rect.width, (event.clientY - rect.top) * canvas.height / rect.height];
}

function drawGeometry(element, active = false) {
  const geometry = element.geometry;
  const color = colors[element.category] || '#fff';
  context.save();
  context.strokeStyle = active ? '#fff' : color;
  context.fillStyle = `${color}30`;
  context.lineWidth = active ? 5 : 3;
  context.setLineDash(element.evidence?.state === 'candidate' ? [10, 7] : []);
  context.beginPath();
  if (geometry.type === 'segment') {
    const start = sourceToPixel(geometry.start);
    const end = sourceToPixel(geometry.end);
    context.moveTo(...start);
    context.lineTo(...end);
  } else if (geometry.type === 'rectangle') {
    const center = sourceToPixel(geometry.center);
    const width = geometry.size[0] / metadata.resolutionMPerPixel;
    const depth = geometry.size[1] / metadata.resolutionMPerPixel;
    context.translate(...center);
    context.rotate(-geometry.yaw);
    context.rect(-width / 2, -depth / 2, width, depth);
  } else if (geometry.type === 'polygon') {
    geometry.points.map(sourceToPixel).forEach((point, index) => index ? context.lineTo(...point) : context.moveTo(...point));
    context.closePath();
  }
  context.fill();
  context.stroke();
  context.restore();
}

function redraw() {
  context.clearRect(0, 0, canvas.width, canvas.height);
  context.drawImage(image, 0, 0, canvas.width, canvas.height);
  for (const element of documentData.elements) drawGeometry(element);
  if (draft?.geometry) drawGeometry({ category:categorySelect.value, geometry:draft.geometry, evidence:{state:'candidate'} }, true);
}

function newEvidence() {
  return { sourceLayer:layerSelect.value, reviewer:'human-or-agent', state:'candidate', note:document.querySelector('#note').value.trim(), photoIds:[] };
}

function finishGeometry(geometry) {
  const category = categorySelect.value;
  documentData.elements.push({
    id:`${category}-${String(documentData.elements.length + 1).padStart(3, '0')}`,
    category,
    geometry,
    height:Number(document.querySelector('#height').value),
    baseHeight:Number(document.querySelector('#base-height').value),
    thickness:Number(document.querySelector('#thickness').value),
    material:{description:document.querySelector('#material').value.trim()},
    evidence:newEvidence(),
  });
  draft = null;
  localStorage.setItem(`manualReview:${documentData.dataset}`, JSON.stringify(documentData));
  renderList();
  redraw();
}

function renderList() {
  document.querySelector('#list').innerHTML = documentData.elements.map((element, index) => {
    const geometry = element.geometry;
    const summary = geometry.type === 'segment'
      ? `${Math.hypot(geometry.end[0]-geometry.start[0], geometry.end[1]-geometry.start[1]).toFixed(2)} m`
      : geometry.type === 'rectangle' ? `${geometry.size.map((value)=>value.toFixed(2)).join(' × ')} m` : `${geometry.points.length} 点`;
    return `<div class="item"><b style="color:${colors[element.category]}">${element.id} · ${element.category}</b><small>${geometry.type} · ${summary} · ${element.evidence.sourceLayer}</small><small>${element.evidence.note || '尚未写判断依据'}</small><button type="button" data-delete="${index}">删除</button></div>`;
  }).join('');
}

canvas.addEventListener('pointerdown', (event) => {
  if (toolSelect.value === 'select') return;
  const point = eventPoint(event);
  if (toolSelect.value === 'polygon') {
    if (!draft) draft = {points:[point]}; else draft.points.push(point);
    draft.geometry = {type:'polygon', points:draft.points.map(pixelToSource)};
    measure.textContent = `多边形 ${draft.points.length} 个点；双击闭合`;
    redraw();
    return;
  }
  draft = {start:point, end:point};
  canvas.setPointerCapture(event.pointerId);
});

canvas.addEventListener('pointermove', (event) => {
  if (!draft?.start) {
    const source = pixelToSource(eventPoint(event));
    measure.textContent = `源坐标 X ${source[0].toFixed(3)} m · Y ${source[1].toFixed(3)} m`;
    return;
  }
  draft.end = eventPoint(event);
  const start = pixelToSource(draft.start);
  const end = pixelToSource(draft.end);
  if (toolSelect.value === 'segment') {
    draft.geometry = {type:'segment', start, end};
    measure.textContent = `长度 ${Math.hypot(end[0]-start[0], end[1]-start[1]).toFixed(3)} m`;
  } else {
    draft.geometry = {type:'rectangle', center:[(start[0]+end[0])/2,(start[1]+end[1])/2], size:[Math.abs(end[0]-start[0]),Math.abs(end[1]-start[1])], yaw:Number(document.querySelector('#yaw').value) * Math.PI / 180};
    measure.textContent = `尺寸 ${draft.geometry.size.map((value)=>value.toFixed(3)).join(' × ')} m`;
  }
  redraw();
});

canvas.addEventListener('pointerup', (event) => {
  if (!draft?.start || !draft.geometry) return;
  finishGeometry(draft.geometry);
  canvas.releasePointerCapture(event.pointerId);
});

canvas.addEventListener('dblclick', (event) => {
  event.preventDefault();
  if (toolSelect.value === 'polygon' && draft?.points?.length >= 3) finishGeometry({type:'polygon', points:draft.points.map(pixelToSource)});
});

window.addEventListener('keydown', (event) => { if (event.key === 'Escape') { draft = null; redraw(); } });
document.querySelector('#list').addEventListener('click', (event) => { const button=event.target.closest('[data-delete]'); if(button){documentData.elements.splice(Number(button.dataset.delete),1);localStorage.setItem(`manualReview:${documentData.dataset}`,JSON.stringify(documentData));renderList();redraw();} });
document.querySelector('#undo').addEventListener('click', () => { documentData.elements.pop(); localStorage.setItem(`manualReview:${documentData.dataset}`,JSON.stringify(documentData)); renderList(); redraw(); });
document.querySelector('#export').addEventListener('click', () => {
  const blob = new Blob([JSON.stringify(documentData, null, 2)], {type:'application/json'});
  const link = document.createElement('a');
  link.href=URL.createObjectURL(blob);
  link.download='manual-review.json';
  link.click();
  URL.revokeObjectURL(link.href);
});
document.querySelector('#import').addEventListener('change', async (event) => { const file=event.target.files[0]; if(file){documentData=JSON.parse(await file.text());localStorage.setItem(`manualReview:${documentData.dataset}`,JSON.stringify(documentData));renderList();redraw();} });

async function loadLayer(name) {
  const next = new Image();
  next.src=layerSources[name];
  await next.decode();
  image=next;
  canvas.width=image.naturalWidth;
  canvas.height=image.naturalHeight;
  redraw();
}
layerSelect.addEventListener('change', () => loadLayer(layerSelect.value));

async function init() {
  const [metaResponse, reviewResponse] = await Promise.all([
    fetch('./generated/focus-orthophoto-metadata.json',{cache:'no-store'}),
    fetch('./manual-review.json',{cache:'no-store'}),
  ]);
  metadata=await metaResponse.json();
  documentData=await reviewResponse.json();
  const saved = localStorage.getItem(`manualReview:${documentData.dataset}`);
  if (saved) documentData = JSON.parse(saved);
  await loadLayer('xray');
  renderList();
  document.documentElement.dataset.annotatorReady='true';
}
init().catch((error)=>{measure.textContent=error.message;console.error(error);});
