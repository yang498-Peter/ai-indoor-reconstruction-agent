const rawImage = document.querySelector('#raw-image');
const overlay = document.querySelector('#overlay');
const overlayContext = overlay.getContext('2d');
const detail = document.querySelector('#detail');
const detailContext = detail.getContext('2d');
const detailElevation = document.querySelector('#detail-elevation');
const opacityInput = document.querySelector('#opacity');
let sceneData;
let metadata;
let image;
let selected;
let selectedStructure;
let showLabels = false;
let activeLayer = 'structure';
const layerSources = {
  structure: './generated/focus-pointcloud-high-structure-slice.png',
  xray: './generated/focus-pointcloud-xray-composite.png',
  tabletop: './generated/focus-pointcloud-tabletop-slice.png',
  chairs: './generated/focus-pointcloud-chair-slice.png',
  overview: './generated/focus-pointcloud-orthophoto.png',
};

function allStructures() {
  return [...(sceneData.structures || []), ...(sceneData.structureCandidates || [])];
}

function sourceCenter(object) {
  const origin = sceneData.coordinateSystem.sourceOrigin;
  return [object.center[0] + origin[0], origin[1] - object.center[2]];
}

function sourceToPixel(point) {
  return [
    (point[0] - metadata.minX) / metadata.resolutionMPerPixel,
    (metadata.maxY - point[1]) / metadata.resolutionMPerPixel,
  ];
}

function drawObject(context, object, scale = 1, offset = [0, 0], emphasize = false) {
  const center = sourceToPixel(sourceCenter(object));
  const x = (center[0] - offset[0]) * scale;
  const y = (center[1] - offset[1]) * scale;
  const width = object.size[0] / metadata.resolutionMPerPixel * scale;
  const depth = object.size[2] / metadata.resolutionMPerPixel * scale;
  context.save();
  context.translate(x, y);
  context.rotate(-object.yaw);
  context.fillStyle = `rgba(255,77,120,${Number(opacityInput.value) / 100})`;
  const posePassed = object.deliveryValidation?.status === 'PASS';
  context.strokeStyle = posePassed ? (emphasize ? '#fff27a' : '#2de1bf') : '#ff9a55';
  context.lineWidth = (emphasize ? 4 : 2.2) * Math.max(1, scale * 0.45);
  context.setLineDash(posePassed ? [] : [7, 6]);
  context.fillRect(-width / 2, -depth / 2, width, depth);
  context.strokeRect(-width / 2, -depth / 2, width, depth);
  context.beginPath();
  context.moveTo(0, 0);
  context.lineTo(width * 0.42, 0);
  context.stroke();
  context.restore();
  if (showLabels && scale === 1) {
    context.font = '600 13px "Microsoft YaHei", sans-serif';
    context.fillStyle = '#dffdf4';
    context.fillText(object.id, x + 7, y - 7);
  }
}

function drawStructure(context, structure, scale = 1, offset = [0, 0], emphasize = false) {
  const colors = { glass: '#55e7ff', window: '#61a9ff', door: '#ffbd5b', wall: '#ff8b6b' };
  const candidate = structure.validation?.publish === false;
  context.save();
  context.strokeStyle = candidate ? '#ff9a55' : (colors[structure.category] || '#f1a85b');
  context.fillStyle = context.strokeStyle;
  context.lineWidth = (emphasize ? 7 : 4) * Math.max(1, scale * 0.45);
  context.setLineDash(candidate ? [5, 7] : (structure.category === 'glass' ? [10, 6] : []));
  if (structure.geometryType === 'segment' && structure.sourceStart && structure.sourceEnd) {
    const start = sourceToPixel(structure.sourceStart);
    const end = sourceToPixel(structure.sourceEnd);
    const x1 = (start[0] - offset[0]) * scale;
    const y1 = (start[1] - offset[1]) * scale;
    const x2 = (end[0] - offset[0]) * scale;
    const y2 = (end[1] - offset[1]) * scale;
    context.beginPath();
    context.moveTo(x1, y1);
    context.lineTo(x2, y2);
    context.stroke();
    for (const [x, y] of [[x1, y1], [x2, y2]]) {
      context.beginPath();
      context.arc(x, y, 4.5 * Math.max(1, scale * 0.35), 0, Math.PI * 2);
      context.fill();
    }
    if (showLabels && scale === 1) {
      context.font = '600 12px "Microsoft YaHei", sans-serif';
      context.fillText(structure.id, (x1 + x2) / 2 + 7, (y1 + y2) / 2 - 7);
    }
  } else if (structure.geometryType === 'rectangle' && structure.sourceCenter) {
    const center = sourceToPixel(structure.sourceCenter);
    const x = (center[0] - offset[0]) * scale;
    const y = (center[1] - offset[1]) * scale;
    const width = structure.size[0] / metadata.resolutionMPerPixel * scale;
    const depth = structure.size[1] / metadata.resolutionMPerPixel * scale;
    context.translate(x, y);
    context.rotate(-structure.yaw);
    context.strokeRect(-width / 2, -depth / 2, width, depth);
  } else if (structure.geometryType === 'polygon' && structure.sourcePoints?.length >= 3) {
    context.beginPath();
    structure.sourcePoints.forEach((point, index) => {
      const pixel = sourceToPixel(point);
      const x = (pixel[0] - offset[0]) * scale;
      const y = (pixel[1] - offset[1]) * scale;
      if (index) context.lineTo(x, y); else context.moveTo(x, y);
    });
    context.closePath();
    context.stroke();
  }
  context.restore();
}

function drawSuggestedStructurePlane(context, structure, scale = 1, offset = [0, 0]) {
  if (structure.geometryType !== 'segment' || !structure.sourceStart || !structure.sourceEnd) return;
  const shift = structure.validation?.planOffsetSuggestionM;
  if (!Number.isFinite(shift) || Math.abs(shift) <= structure.validation.maxPlanOffsetM) return;
  const dx = structure.sourceEnd[0] - structure.sourceStart[0];
  const dy = structure.sourceEnd[1] - structure.sourceStart[1];
  const length = Math.hypot(dx, dy);
  const perpendicular = [-dy / length, dx / length];
  const shiftedStart = [structure.sourceStart[0] + perpendicular[0] * shift, structure.sourceStart[1] + perpendicular[1] * shift];
  const shiftedEnd = [structure.sourceEnd[0] + perpendicular[0] * shift, structure.sourceEnd[1] + perpendicular[1] * shift];
  const start = sourceToPixel(shiftedStart);
  const end = sourceToPixel(shiftedEnd);
  context.save();
  context.strokeStyle = '#a8ff69';
  context.lineWidth = 3.5 * Math.max(1, scale * 0.45);
  context.setLineDash([3, 4]);
  context.beginPath();
  context.moveTo((start[0] - offset[0]) * scale, (start[1] - offset[1]) * scale);
  context.lineTo((end[0] - offset[0]) * scale, (end[1] - offset[1]) * scale);
  context.stroke();
  context.restore();
}

function drawOverlay() {
  overlay.width = image.naturalWidth;
  overlay.height = image.naturalHeight;
  overlayContext.drawImage(image, 0, 0);
  for (const structure of allStructures()) drawStructure(overlayContext, structure, 1, [0, 0], structure === selectedStructure);
  if (selectedStructure) drawSuggestedStructurePlane(overlayContext, selectedStructure);
  if (activeLayer !== 'structure') {
    for (const object of sceneData.objects) drawObject(overlayContext, object, 1, [0, 0], object === selected);
  }
}

function drawDetail(object) {
  selected = object;
  selectedStructure = null;
  detailElevation.hidden = true;
  const center = sourceToPixel(sourceCenter(object));
  const worldSpan = Math.max(object.size[0], object.size[2]) + 2.8;
  const sourceSpan = worldSpan / metadata.resolutionMPerPixel;
  const aspect = detail.width / detail.height;
  const cropWidth = Math.max(sourceSpan, sourceSpan * aspect);
  const cropHeight = cropWidth / aspect;
  const sx = Math.max(0, Math.min(image.naturalWidth - cropWidth, center[0] - cropWidth / 2));
  const sy = Math.max(0, Math.min(image.naturalHeight - cropHeight, center[1] - cropHeight / 2));
  detailContext.clearRect(0, 0, detail.width, detail.height);
  detailContext.drawImage(image, sx, sy, cropWidth, cropHeight, 0, 0, detail.width, detail.height);
  const scale = detail.width / cropWidth;
  drawObject(detailContext, object, scale, [sx, sy], true);
  document.querySelector('#detail-meta').innerHTML = `
    <b>${object.id} · ${object.category}</b>
    中心 <code>${sourceCenter(object).map((value) => value.toFixed(2)).join(', ')}</code> m ·
    尺寸 <code>${object.size[0].toFixed(2)} × ${object.size[2].toFixed(2)}</code> m ·
    源角度 <code>${(object.yaw * 180 / Math.PI).toFixed(1)}°</code><br>
    交付状态 <code>${object.deliveryValidation?.status ?? 'REVIEW'} · 姿态 ${object.deliveryValidation?.poseStatus ?? 'MISSING'} · 碰撞 ${object.deliveryValidation?.clearanceStatus ?? 'MISSING'}</code><br>
    原始点云姿态复算 <code>${object.furnitureValidation?.status ?? 'REVIEW'} · 原始轴差 ${object.furnitureValidation?.yawResidualDeg?.toFixed(1) ?? '—'}° · 留一法家具轴差 ${object.furnitureValidation?.localFamilyYawResidualDeg?.toFixed(1) ?? '—'}°</code><br>
    <small>${object.furnitureValidation?.blockers?.join(' · ') || object.furnitureValidation?.independentSource || '缺少独立点云姿态回执'}</small><br>
    ${object.evidence.completion || ''}`;
  document.querySelectorAll('[data-object]').forEach((button) => button.classList.toggle('active', button.dataset.object === object.id));
  document.querySelectorAll('[data-structure]').forEach((button) => button.classList.remove('active'));
  drawOverlay();
}

function drawStructureDetail(structure) {
  selected = null;
  selectedStructure = structure;
  if (structure.geometryType === 'segment') {
    detailElevation.src = `./generated/structure-elevations/${structure.id}-suggested-offset-elevation.png`;
    detailElevation.hidden = false;
  } else {
    detailElevation.hidden = true;
  }
  let points;
  if (structure.geometryType === 'segment') points = [structure.sourceStart, structure.sourceEnd];
  else if (structure.geometryType === 'polygon') points = structure.sourcePoints;
  else {
    const [x, y] = structure.sourceCenter;
    const [width, depth] = structure.size;
    points = [[x - width / 2, y - depth / 2], [x + width / 2, y + depth / 2]];
  }
  const pixels = points.map(sourceToPixel);
  const minX = Math.min(...pixels.map((point) => point[0]));
  const maxX = Math.max(...pixels.map((point) => point[0]));
  const minY = Math.min(...pixels.map((point) => point[1]));
  const maxY = Math.max(...pixels.map((point) => point[1]));
  const padding = 1.8 / metadata.resolutionMPerPixel;
  const sourceWidth = Math.max(maxX - minX + padding * 2, 180);
  const sourceHeight = Math.max(maxY - minY + padding * 2, 130);
  const aspect = detail.width / detail.height;
  const cropWidth = Math.max(sourceWidth, sourceHeight * aspect);
  const cropHeight = cropWidth / aspect;
  const centerX = (minX + maxX) / 2;
  const centerY = (minY + maxY) / 2;
  const sx = Math.max(0, Math.min(image.naturalWidth - cropWidth, centerX - cropWidth / 2));
  const sy = Math.max(0, Math.min(image.naturalHeight - cropHeight, centerY - cropHeight / 2));
  detailContext.clearRect(0, 0, detail.width, detail.height);
  detailContext.drawImage(image, sx, sy, cropWidth, cropHeight, 0, 0, detail.width, detail.height);
  drawStructure(detailContext, structure, detail.width / cropWidth, [sx, sy], true);
  drawSuggestedStructurePlane(detailContext, structure, detail.width / cropWidth, [sx, sy]);
  const length = structure.geometryType === 'segment'
    ? Math.hypot(structure.sourceEnd[0] - structure.sourceStart[0], structure.sourceEnd[1] - structure.sourceStart[1])
    : null;
  document.querySelector('#detail-meta').innerHTML = `
    <b>${structure.id} · ${structure.category}</b>
    ${length ? `长度 <code>${length.toFixed(2)}</code> m · ` : ''}高度 <code>${structure.height.toFixed(2)}</code> m · 厚度 <code>${structure.thickness.toFixed(3)}</code> m<br>
    证据状态 <code>${structure.evidence?.state || '未标记'}</code> · 图层 <code>${structure.evidence?.sourceLayer || '—'}</code><br>
    发布门禁 <code>${structure.validation?.publish ? 'ACCEPTED' : 'REVIEW'}</code> · 平面差 <code>${structure.validation?.planOffsetDisagreementM?.toFixed(3) ?? '—'}</code> m<br>
    ${structure.validation?.planContradictionDisposition ? `算法强面处置 <code>${structure.validation.planContradictionDisposition}</code><br>` : ''}
    ${structure.evidence?.note || ''}`;
  document.querySelectorAll('[data-object]').forEach((button) => button.classList.remove('active'));
  document.querySelectorAll('[data-structure]').forEach((button) => button.classList.toggle('active', button.dataset.structure === structure.id));
  drawOverlay();
}

async function init() {
  const [sceneResponse, metadataResponse] = await Promise.all([
    fetch('./generated/scene.json', { cache: 'no-store' }),
    fetch('./generated/focus-orthophoto-metadata.json', { cache: 'no-store' }),
  ]);
  sceneData = await sceneResponse.json();
  metadata = await metadataResponse.json();
  image = new Image();
  image.src = layerSources.structure;
  await image.decode();
  rawImage.src = image.src;
  document.querySelector('#object-list').innerHTML = sceneData.objects.map((object) =>
    `<button type="button" data-object="${object.id}"><span>${object.id}</span><small>${object.deliveryValidation?.status ?? 'REVIEW'} · ${(object.yaw * 180 / Math.PI).toFixed(1)}°</small></button>`
  ).join('') + allStructures().map((structure) =>
    `<button type="button" data-structure="${structure.id}"><span>${structure.id}</span><small>${structure.validation?.publish ? structure.category : `REVIEW · ${structure.category}`}</small></button>`
  ).join('');
  document.querySelector('#object-list').addEventListener('click', (event) => {
    const button = event.target.closest('[data-object]');
    const structureButton = event.target.closest('[data-structure]');
    if (button) drawDetail(sceneData.objects.find((object) => object.id === button.dataset.object));
    if (structureButton) drawStructureDetail(allStructures().find((structure) => structure.id === structureButton.dataset.structure));
  });
  opacityInput.addEventListener('input', () => { drawOverlay(); if (selected) drawDetail(selected); if (selectedStructure) drawStructureDetail(selectedStructure); });
  document.querySelector('#layer').addEventListener('change', async (event) => {
    activeLayer = event.target.value;
    const next = new Image();
    next.src = layerSources[event.target.value];
    await next.decode();
    image = next;
    rawImage.src = image.src;
    drawOverlay();
    if (selected) drawDetail(selected);
    if (selectedStructure) drawStructureDetail(selectedStructure);
  });
  document.querySelector('#toggle-labels').addEventListener('click', (event) => {
    showLabels = !showLabels;
    event.currentTarget.textContent = showLabels ? '隐藏标签' : '显示标签';
    drawOverlay();
  });
  drawOverlay();
  drawStructureDetail((sceneData.structureCandidates || [])[0] || (sceneData.structures || [])[0]);
  document.querySelector('#status').textContent = `${sceneData.objects.length} 个家具 · ${(sceneData.structures || []).length} 个已接受结构 · ${(sceneData.structureCandidates || []).length} 个候选结构 · 原始点云与位姿照片复核，用户标注不作几何证据`;
  document.documentElement.dataset.reviewReady = 'true';
}

init().catch((error) => {
  document.querySelector('#status').textContent = error.message;
  console.error(error);
});
