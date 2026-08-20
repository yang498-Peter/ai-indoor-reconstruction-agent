import * as THREE from 'three';
import { OrbitControls } from '../../web-uploader/assets/vendor/three/addons/controls/OrbitControls.js';
import { isSceneV2, compileSceneV2 } from '../../scene-core/scene-core.js';

const canvas = document.querySelector('#viewport');
const loading = document.querySelector('#loading');
const renderer = new THREE.WebGLRenderer({ canvas, antialias: true, alpha: true });
renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
renderer.outputColorSpace = THREE.SRGBColorSpace;
renderer.toneMapping = THREE.ACESFilmicToneMapping;
renderer.toneMappingExposure = 1.05;
renderer.shadowMap.enabled = true;
renderer.shadowMap.type = THREE.PCFSoftShadowMap;

const scene3d = new THREE.Scene();
scene3d.background = new THREE.Color(0x0b0f12);
scene3d.fog = new THREE.FogExp2(0x0b0f12, 0.017);
const TOP_VIEW_TILT_RAD = THREE.MathUtils.degToRad(3);
const MIN_CAMERA_HEIGHT_M = 0.28;
const MIN_TARGET_HEIGHT_M = 0.08;
const SOLID_WALL_JOIN_TOLERANCE_M = 0.012;
const SOLID_WALL_JOIN_OVERLAP_M = 0.024;
const perspectiveCamera = new THREE.PerspectiveCamera(50, 1, 0.03, 500);
const orthographicCamera = new THREE.OrthographicCamera(-10, 10, 10, -10, 0.03, 500);
perspectiveCamera.position.set(17, 15, 21);
perspectiveCamera.up.set(0, 1, 0);
orthographicCamera.up.copy(perspectiveCamera.up);
let camera = perspectiveCamera;
let projectionMode = 'perspective';
let orthographicHalfHeight = 12;

function configureControls(instance, target = new THREE.Vector3(0, 1.1, 0)) {
  instance.enableDamping = true;
  instance.dampingFactor = 0.06;
  instance.target.copy(target);
  instance.minDistance = 2;
  instance.maxDistance = 95;
  instance.minZoom = 0.15;
  instance.maxZoom = 12;
  instance.minPolarAngle = TOP_VIEW_TILT_RAD;
  instance.maxPolarAngle = Math.PI * 0.495;
}

let controls = new OrbitControls(camera, canvas);
configureControls(controls);

scene3d.add(new THREE.HemisphereLight(0xddefff, 0x354039, 2.2));
const keyLight = new THREE.DirectionalLight(0xfff3dc, 3.1);
keyLight.position.set(-14, 24, 10);
keyLight.castShadow = true;
keyLight.shadow.mapSize.set(2048, 2048);
keyLight.shadow.camera.left = -35;
keyLight.shadow.camera.right = 35;
keyLight.shadow.camera.top = 35;
keyLight.shadow.camera.bottom = -35;
scene3d.add(keyLight);
const fillLight = new THREE.DirectionalLight(0x9fc8ff, 1.15);
fillLight.position.set(18, 8, -15);
scene3d.add(fillLight);

const pointGroup = new THREE.Group();
const wallGroup = new THREE.Group();
const candidateStructureGroup = new THREE.Group();
const objectGroup = new THREE.Group();
const reviewObjectGroup = new THREE.Group();
const cameraGroup = new THREE.Group();
const photoMarkerGroup = new THREE.Group();
scene3d.add(pointGroup, wallGroup, candidateStructureGroup, objectGroup, reviewObjectGroup, cameraGroup, photoMarkerGroup);

let sceneData;
let pointMaterial;
let selectedGroup = null;
let activeCategory = 'all';
const objectMeshes = new Map();
const floorSurfaceMeshes = [];
let viewportWidth = 0;
let viewportHeight = 0;
let currentLanguage = localStorage.getItem('ai-indoor-language') === 'en' ? 'en' : 'zh-CN';
let selectedPhotoIndex = 0;
const activeNavigationKeys = new Set();
const navigationClock = new THREE.Clock();
// P2-2 photo/panorama alignment view + P2-5 unified review panel state.
// rawSceneV2 keeps the authority graph (statuses, evidence sources) that the
// compiled view model intentionally flattens; null for V1 scenes.
let rawSceneV2 = null;
let sceneBaseUrl = new URL(window.location.href);
let photoAlign = null;
let photoMarkersShowAll = false;
const PHOTO_MARKER_BUDGET = 50;
const PHOTO_BILLBOARD_DISTANCE_M = 4;
let reviewPanelOpen = false;
let reviewFilter = 'all';
let reviewExpandedId = null;
let reviewHighlightedMeshes = [];
const REVIEW_STATUS_COLORS = {
  'accepted-measured': '#23c483',
  'accepted-inferred': '#4c8dff',
  candidate: '#ff9a55',
  rejected: '#ff5f56',
};

const translations = {
  'zh-CN': {
    title: 'AI 室内模型重建', displayMode: '显示模式', language: '语言',
    modeRaw: '原始点云', modeOverlay: '叠加检查', modeModel: '程序化模型',
    refineModel: '结构绘制', evidenceReview: 'Agent 对比图', fitView: '适配视图', topView: '俯视检查', projectionMode: '投影方式', perspectiveProjection: '透视', orthographicProjection: '平行',
    navigationHelp: '键盘视角操作', navMove: '平移', navElevate: '升降', navLook: '转向', navFast: '加速', navProjection: '切换投影', navViews: '俯视 / 适配',
    processingStatus: '处理状态', measuredElements: '场景元素', sceneObjects: '场景对象',
    viewportLabel: 'Three.js 室内重建视图', loadingScene: '正在读取真实点云与 Semantic Scene…',
    legendCloud: '彩色点云', legendAccepted: '展示结构', legendCandidate: '候选/推断结构', legendObject: '程序化元素', legendPath: '相机轨迹',
    confidenceDefault: '实体结构已通过四门；橙色半透明虚线是待复核推断，叠加检查可见、正式模型自动隐藏。自动墙线不会直接进入模型。',
    confidenceResolved: '所有结构建议均已逐项人工接受或拒绝；当前只显示正式结构、程序化家具与真实点云，无待审橙色候选。',
    confidenceWip: '当前是实时重建中的展示假设；结构、开口与家具仍在复核，页面更新不代表权威验收通过。',
    inspector: '检查器', selectElement: '请选择一个元素', selectHint: '点击模型或左侧对象列表，查看点云测量值和本地识别依据。',
    qualityLoop: '复核质量循环', visualEvidence: '本地视觉证据', posedPhotos: '位姿照片', evidencePhotoAlt: '最近相机证据帧',
    evidenceCaptionDefault: '自动选择覆盖范围互补的本地帧，不上传。', previewPoints: '预览点', acceptedStructures: '已接受结构', displayStructures: '展示结构', authorityDisplayStructures: '实测权威 / 展示结构', localElements: '本地元素', estimatedHeight: '估计层高',
    all: '全部', table: '桌', workstation: '双面工位', 'wall-workbench': '沿墙工作台', 'round-table': '圆桌', 'oval-table': '椭圆桌', 'meeting-table': '会议桌', chair: '椅', sofa: '座椅组', cabinet: '柜体', generic: '待确认', 'booth-desk': '卡座工作台', tree: '树木', vehicle: '车辆', 'roof-panel': '雨棚屋面', step: '台阶', tripod: '测量三脚架', signboard: '招牌',
    autoReviewed: '自动建议已完成人工取舍', geometryClosed: '几何闭合', publishedClosed: '正式闭合', shapeFailure: '形状失败', retained: '保留',
    evidenceFrame: '证据帧', originalFrame: '原始帧', localPreview: '本地预览', measuredSize: '测量尺寸', localConfidence: '本地置信度', supportPoints: '支持点数',
    heightEvidence: '桌面高度依据', ruleEvidence: '规则依据', occlusionCompletion: '遮挡补全', materialEvidence: '照片材质判断', status: '状态', noCompletion: '未使用推断补全', unconfirmed: '未确认',
    statusPASS: '通过', statusREVIEW: '待复核', statusFAIL: '未通过', statusADVISORY: '建议项', loadCloudFailed: '点云读取失败', cloudContractFailed: '点云二进制契约不匹配', sceneLoadFailed: 'scene.json 读取失败，请先运行 run-demo.ps1',
    photoAlignEnter: '进入照片对齐视角', photoAlignExit: '退出照片视角 (Esc)', photoOpacity: '照片透明度', photoFrustumsAll: '显示全部帧标记', photoFrustumsSampled: '仅显示抽样帧标记', legendPhotoFrame: '照片帧标记',
    reviewMode: '审查模式', reviewAll: '全部', reviewOnlyCandidate: '仅候选', reviewOnlyWarning: '仅告警', reviewEvidenceSources: '证据来源', reviewNoSources: '无证据来源记录', reviewEmpty: '没有匹配的元素', reviewWarning: '告警',
    stMeasured: '实测接受', stInferred: '推断接受', stCandidate: '候选', stRejected: '已拒绝',
  },
  en: {
    title: 'AI Indoor Model Reconstruction', displayMode: 'Display mode', language: 'Language',
    modeRaw: 'Reality Data', modeOverlay: 'AI Comparison', modeModel: 'Delivery Model',
    refineModel: 'Model Refinement', evidenceReview: 'Evidence Review', fitView: 'Fit View', topView: 'Top View', projectionMode: 'Projection mode', perspectiveProjection: 'Perspective', orthographicProjection: 'Parallel',
    navigationHelp: 'Keyboard navigation', navMove: 'Move', navElevate: 'Elevate', navLook: 'Look', navFast: 'Fast', navProjection: 'Projection', navViews: 'Top / Fit',
    processingStatus: 'Processing Status', measuredElements: 'Scene Elements', sceneObjects: 'Scene Objects',
    viewportLabel: 'Three.js indoor reconstruction viewport', loadingScene: 'Loading the captured point cloud and semantic scene…',
    legendCloud: 'Color Point Cloud', legendAccepted: 'Display Geometry', legendCandidate: 'Inferred / Review', legendObject: 'Parametric Objects', legendPath: 'Camera Path',
    confidenceDefault: 'Accepted structures have passed the evidence gates. Orange dashed geometry remains under review and is hidden from the delivery model.',
    confidenceResolved: 'Every structural proposal has been explicitly accepted or rejected. This view contains only approved structure, parametric furniture and captured point-cloud data.',
    confidenceWip: 'This is a live reconstruction hypothesis. Structure, openings and furniture remain under review; a page update is not authority acceptance.',
    inspector: 'Object Inspector', selectElement: 'Select an object', selectHint: 'Select an object in the model or list to inspect measurements and supporting evidence.',
    qualityLoop: 'Quality Assurance', visualEvidence: 'Spatial Evidence', posedPhotos: 'Registered Photos', evidencePhotoAlt: 'Nearest registered evidence frame',
    evidenceCaptionDefault: 'Locally selected complementary evidence frames. Nothing is uploaded.', previewPoints: 'Preview Points', acceptedStructures: 'Approved Structures', displayStructures: 'Display Geometry', authorityDisplayStructures: 'Measured authority / display geometry', localElements: 'Reconstructed Objects', estimatedHeight: 'Estimated Height',
    all: 'All', table: 'Table', workstation: 'Double Workstation', 'wall-workbench': 'Wall Workbench', 'round-table': 'Round Table', 'oval-table': 'Oval Table', 'meeting-table': 'Meeting Table', chair: 'Chair', sofa: 'Seating', cabinet: 'Cabinet', generic: 'Unclassified', 'booth-desk': 'Built-in Booth Desk', tree: 'Tree', vehicle: 'Vehicle', 'roof-panel': 'Roof Panel', step: 'Step', tripod: 'Survey Tripod', signboard: 'Signboard',
    autoReviewed: 'AI proposals completed human review', geometryClosed: 'Geometry closure', publishedClosed: 'Approved closure', shapeFailure: 'Shape issue', retained: 'Retained',
    evidenceFrame: 'Evidence Frame', originalFrame: 'Source frame', localPreview: 'Local preview', measuredSize: 'Measured Size', localConfidence: 'Local Confidence', supportPoints: 'Supporting Points',
    heightEvidence: 'Height Evidence', ruleEvidence: 'Decision Evidence', occlusionCompletion: 'Occlusion Completion', materialEvidence: 'Photo Material', status: 'Status', noCompletion: 'No inferred completion', unconfirmed: 'Unconfirmed',
    statusPASS: 'Verified', statusREVIEW: 'Review', statusFAIL: 'Failed', statusADVISORY: 'Advisory', loadCloudFailed: 'Failed to load the point cloud', cloudContractFailed: 'Point-cloud binary contract mismatch', sceneLoadFailed: 'Failed to load scene.json. Run run-demo.ps1 first',
    photoAlignEnter: 'Enter photo-aligned view', photoAlignExit: 'Exit photo view (Esc)', photoOpacity: 'Photo opacity', photoFrustumsAll: 'Show all frame markers', photoFrustumsSampled: 'Show sampled frame markers', legendPhotoFrame: 'Photo Frames',
    reviewMode: 'Review', reviewAll: 'All', reviewOnlyCandidate: 'Candidates only', reviewOnlyWarning: 'Warnings only', reviewEvidenceSources: 'Evidence sources', reviewNoSources: 'No evidence sources recorded', reviewEmpty: 'No matching elements', reviewWarning: 'warning',
    stMeasured: 'Accepted (measured)', stInferred: 'Accepted (inferred)', stCandidate: 'Candidate', stRejected: 'Rejected',
  },
};

function t(key) {
  return translations[currentLanguage][key] ?? translations['zh-CN'][key] ?? key;
}

function localizedStatus(status) {
  return translations[currentLanguage][`status${status}`] ?? status;
}

function objectDisplayName(object) {
  const categoryName = t(object.category);
  const suffix = object.id.match(/(\d+)$/)?.[1];
  return suffix ? `${categoryName} ${suffix}` : categoryName;
}

function applyLanguage(language, persist = true) {
  currentLanguage = language === 'en' ? 'en' : 'zh-CN';
  document.documentElement.lang = currentLanguage;
  document.title = t('title');
  document.querySelectorAll('[data-i18n]').forEach((node) => { node.textContent = t(node.dataset.i18n); });
  document.querySelectorAll('[data-i18n-aria]').forEach((node) => { node.setAttribute('aria-label', t(node.dataset.i18nAria)); });
  document.querySelectorAll('[data-i18n-alt]').forEach((node) => { node.setAttribute('alt', t(node.dataset.i18nAlt)); });
  document.querySelectorAll('[data-language]').forEach((button) => button.classList.toggle('active', button.dataset.language === currentLanguage));
  if (persist) localStorage.setItem('ai-indoor-language', currentLanguage);
  if (sceneData) {
    renderPanels(sceneData);
    if (reviewPanelOpen) renderReviewPanel();
    if (selectedGroup) selectObject(selectedGroup.userData.sceneObject.id, false);
    else {
      document.querySelector('#selection-title').textContent = t('selectElement');
      document.querySelector('#selection-details').textContent = t('selectHint');
    }
  }
}

function standardMaterial(color, options = {}) {
  return new THREE.MeshStandardMaterial({
    color,
    roughness: options.roughness ?? 0.72,
    metalness: options.metalness ?? 0.04,
    transparent: options.transparent ?? false,
    opacity: options.opacity ?? 1,
    side: options.side ?? THREE.FrontSide,
    depthWrite: options.depthWrite ?? true,
  });
}

function addBox(parent, size, position, material, options = {}) {
  const geometry = new THREE.BoxGeometry(
    Math.max(size[0], 0.025), Math.max(size[1], 0.025), Math.max(size[2], 0.025),
    options.segments ?? 1, options.segments ?? 1, options.segments ?? 1,
  );
  const mesh = new THREE.Mesh(geometry, material);
  mesh.position.set(...position);
  mesh.castShadow = options.castShadow !== false;
  mesh.receiveShadow = options.receiveShadow !== false;
  parent.add(mesh);
  return mesh;
}

function addCylinder(parent, radius, height, position, material, rotation = null) {
  const mesh = new THREE.Mesh(new THREE.CylinderGeometry(radius, radius, Math.max(height, 0.02), 18), material);
  mesh.position.set(...position);
  if (rotation) mesh.rotation.set(...rotation);
  mesh.castShadow = true;
  mesh.receiveShadow = true;
  parent.add(mesh);
  return mesh;
}

function makePrivacyFilmTexture(repeatX) {
  const pattern = document.createElement('canvas');
  pattern.width = 64;
  pattern.height = 64;
  const context = pattern.getContext('2d');
  context.clearRect(0, 0, 64, 64);
  context.fillStyle = 'rgba(238, 250, 250, 0.92)';
  for (let y = 8; y < 64; y += 16) {
    for (let x = 8; x < 64; x += 16) {
      context.beginPath();
      context.arc(x, y, 2.7, 0, Math.PI * 2);
      context.fill();
    }
  }
  const texture = new THREE.CanvasTexture(pattern);
  texture.colorSpace = THREE.SRGBColorSpace;
  texture.wrapS = THREE.RepeatWrapping;
  texture.wrapT = THREE.RepeatWrapping;
  texture.repeat.set(Math.max(1, repeatX), 2);
  return texture;
}

function addRollerBlind(group, length, base, height, thickness) {
  const blindBottom = Math.max(base + 1.25, base + height * 0.46);
  const blindTop = base + height - 0.04;
  const blindHeight = Math.max(0.5, blindTop - blindBottom);
  addBox(group, [length * 0.96, blindHeight, 0.026], [0, blindBottom + blindHeight / 2, thickness / 2 + 0.028], standardMaterial(0x565e62, { roughness:0.86, transparent:true, opacity:0.88 }), { castShadow:false });
  addBox(group, [length, 0.075, 0.08], [0, blindTop + 0.02, thickness / 2 + 0.02], standardMaterial(0x42494d, { roughness:0.72 }));
}

function buildDerivedGeometry(items = []) {
  for (const item of items) {
    if (item.geometryType !== 'box') continue;
    const material = item.role === 'suspended-light'
      ? standardMaterial(0xf4f2e8, { roughness:0.42 })
      : standardMaterial(0x51585c, { metalness:0.1, roughness:0.76 });
    const mesh = addBox(wallGroup, item.size, item.center, material, { castShadow:false });
    mesh.rotation.y = item.yaw || 0;
    mesh.userData.ceilingDetail = true;
    mesh.userData.parentStructureId = item.parentId;
  }
}

function buildTable(group, object, workstation = false) {
  const [width, measuredHeight, depth] = object.size;
  const height = THREE.MathUtils.clamp(measuredHeight, 0.68, 1.08);
  const topThickness = THREE.MathUtils.clamp(height * 0.085, 0.055, 0.095);
  const topMaterial = standardMaterial(object.color, { roughness: 0.62 });
  const frameMaterial = standardMaterial(workstation ? 0xd9ddda : 0x5a6469, { metalness: 0.48, roughness: 0.42 });
  addBox(group, [width, topThickness, depth], [0, height - topThickness / 2, 0], topMaterial);
  const insetX = Math.max(0.10, width * 0.08);
  const insetZ = Math.max(0.09, depth * 0.11);
  const legHeight = height - topThickness;
  for (const x of [-width / 2 + insetX, width / 2 - insetX]) {
    for (const z of [-depth / 2 + insetZ, depth / 2 - insetZ]) {
      addBox(group, [0.055, legHeight, 0.055], [x, legHeight / 2, z], frameMaterial);
    }
  }
  if (workstation) {
    const dividerHeight = 0.40;
    addBox(group, [width * 0.94, dividerHeight, 0.035], [0, height + dividerHeight / 2, 0], standardMaterial(0x93a1a5, { roughness: 0.85 }));
    const seatCount = Math.max(1, object.layout?.seatsPerSide || Math.round(width / 1.4));
    const monitorSlots = new Set(object.layout?.monitorSlots || []);
    for (let index = 0; index < seatCount; index += 1) {
      const x = -width / 2 + (index + 0.5) * width / seatCount;
      for (const side of [-1, 1]) {
        if (monitorSlots.has(`${index}:${side}`)) {
          const monitor = new THREE.Group();
          monitor.position.set(x, height + 0.19, side * 0.16);
          monitor.rotation.y = side > 0 ? Math.PI : 0;
          addBox(monitor, [Math.min(0.46, width / seatCount * 0.55), 0.28, 0.035], [0, 0, 0], standardMaterial(0x172d39, { metalness: 0.12, roughness: 0.22 }));
          addBox(monitor, [0.035, 0.13, 0.035], [0, -0.20, 0], standardMaterial(0x20282d, { metalness: 0.18, roughness: 0.46 }));
          group.add(monitor);
        }
        const pedestalX = x + (index % 2 ? -0.28 : 0.28);
        addBox(group, [0.32, 0.54, 0.48], [pedestalX, 0.27, side * 0.32], standardMaterial(0xe7e8e4, { roughness: 0.72 }));
        const chair = new THREE.Group();
        const chairClearance = THREE.MathUtils.clamp(object.layout?.chairClearanceM ?? 0.56, 0.42, 0.58);
        chair.position.set(x, 0, side * (depth / 2 + chairClearance));
        chair.rotation.y = side > 0 ? Math.PI : 0;
        buildChair(chair, { size: [0.58, 1.02, 0.58], color: '#aab0b0', frameColor: '#e7e9e5' });
        group.add(chair);
      }
    }
  }
}

function addMeetingChairs(group, width, depth, count, mode = 'rectangle', palette = {}) {
  const chairCount = Math.max(0, count || 0);
  const clearance = THREE.MathUtils.clamp(palette.clearance ?? 0.38, 0.14, 0.42);
  for (let index = 0; index < chairCount; index += 1) {
    const chair = new THREE.Group();
    const offsets = palette.offsets || [];
    if (mode === 'radial') {
      const angle = (index / chairCount) * Math.PI * 2;
      chair.position.set(Math.sin(angle) * (width / 2 + clearance), 0, Math.cos(angle) * (depth / 2 + clearance));
      chair.rotation.y = angle + Math.PI;
    } else {
      const side = index % 2 === 0 ? -1 : 1;
      const slot = Math.floor(index / 2);
      const slots = Math.ceil(chairCount / 2);
      chair.position.set(-width / 2 + (slot + 0.5) * width / slots + (offsets[index] || 0), 0, side * (depth / 2 + clearance));
      chair.rotation.y = side > 0 ? Math.PI : 0;
    }
    buildChair(chair, { size: [0.58, 1.02, 0.58], color: palette.body || '#899398', frameColor: palette.frame || '#59636a' });
    group.add(chair);
  }
}

function buildRoundTable(group, object, oval = false) {
  const [width, measuredHeight, depth] = object.size;
  const height = THREE.MathUtils.clamp(measuredHeight, 0.68, 0.84);
  const topThickness = 0.07;
  const topMaterial = standardMaterial(object.color, { roughness: 0.58 });
  const top = new THREE.Mesh(new THREE.CylinderGeometry(0.5, 0.5, topThickness, 48), topMaterial);
  top.scale.set(width, 1, depth);
  top.position.y = height - topThickness / 2;
  top.castShadow = true;
  top.receiveShadow = true;
  group.add(top);
  const base = standardMaterial(0xe4e6e2, { metalness: 0.12, roughness: 0.5 });
  addCylinder(group, oval ? 0.075 : 0.10, height - topThickness, [0, (height - topThickness) / 2, 0], base);
  const foot = new THREE.Mesh(new THREE.CylinderGeometry(oval ? 0.24 : 0.28, oval ? 0.24 : 0.28, 0.045, 32), base);
  foot.position.y = 0.025;
  group.add(foot);
  const palette = oval ? { body:'#b7b9b5', frame:'#eceeea' } : { body:'#bdc98d', frame:'#eceeea' };
  addMeetingChairs(group, width, depth, object.layout?.seatCount || (oval ? 6 : 4), 'radial', { ...palette, clearance:object.layout?.chairClearanceM });
}

function buildMeetingTable(group, object) {
  buildTable(group, object, false);
  addMeetingChairs(group, object.size[0], object.size[2], object.layout?.seatCount || 6, 'rectangle', { body:'#565f63', frame:'#353d42', offsets:object.layout?.chairOffsetsM || [], clearance:object.layout?.chairClearanceM });
}

function buildBoothDesk(group, object) {
  buildTable(group, object, false);
  const [width, , depth] = object.size;
  const seat = standardMaterial(0x646968, { roughness:0.9 });
  for (const side of [-1, 1]) {
    addBox(group, [width * 0.88, 0.42, 0.42], [0, 0.21, side * (depth / 2 + 0.38)], seat);
    addBox(group, [width * 0.88, 0.52, 0.10], [0, 0.60, side * (depth / 2 + 0.54)], seat);
  }
}

function buildWallWorkbench(group, object) {
  buildTable(group, object, false);
  const count = object.layout?.seatCount || Math.max(1, Math.round(object.size[0] / 1.4));
  const side = object.layout?.seatSide || 1;
  const offsets = object.layout?.chairOffsetsM || [];
  for (let index = 0; index < count; index += 1) {
    const chair = new THREE.Group();
    const chairClearance = THREE.MathUtils.clamp(object.layout?.chairClearanceM ?? 0.56, 0.42, 0.58);
    chair.position.set(-object.size[0] / 2 + (index + 0.5) * object.size[0] / count + (offsets[index] || 0), 0, side * (object.size[2] / 2 + chairClearance));
    chair.rotation.y = side > 0 ? Math.PI : 0;
    buildChair(chair, { size: [0.58, 1.02, 0.58], color: '#969e9e', frameColor: '#e7e9e5' });
    group.add(chair);
  }
  const cabinetCount = Math.max(1, Math.floor(object.size[0] / 2.2));
  for (let index = 0; index < cabinetCount; index += 1) {
    const x = -object.size[0] / 2 + (index + 0.5) * object.size[0] / cabinetCount;
    addBox(group, [0.34, 0.55, 0.46], [x, 0.275, -side * 0.16], standardMaterial(0xe7e8e4, { roughness:0.72 }));
  }
}

function buildChair(group, object) {
  const [rawWidth, rawHeight, rawDepth] = object.size;
  const width = THREE.MathUtils.clamp(rawWidth, 0.42, 0.82);
  const depth = THREE.MathUtils.clamp(rawDepth, 0.42, 0.78);
  const height = THREE.MathUtils.clamp(rawHeight, 0.76, 1.28);
  const seatY = THREE.MathUtils.clamp(height * 0.46, 0.42, 0.54);
  const body = standardMaterial(object.color, { roughness: 0.79 });
  const frameColor = object.frameColor ? Number.parseInt(object.frameColor.replace('#', ''), 16) : 0x4b565c;
  const frame = standardMaterial(frameColor, { metalness: 0.42, roughness: 0.42 });
  addBox(group, [width, 0.085, depth], [0, seatY, 0], body);
  addBox(group, [width * 0.92, Math.max(0.28, height - seatY), 0.075], [0, seatY + (height - seatY) / 2, -depth / 2 + 0.04], body);
  addCylinder(group, 0.035, seatY - 0.05, [0, (seatY - 0.05) / 2, 0], frame);
  for (let index = 0; index < 5; index += 1) {
    const angle = (index / 5) * Math.PI * 2;
    const length = Math.min(width, depth) * 0.42;
    const spoke = addBox(group, [0.035, 0.025, length], [Math.sin(angle) * length * 0.25, 0.055, Math.cos(angle) * length * 0.25], frame);
    spoke.rotation.y = angle;
    addCylinder(group, 0.035, 0.04, [Math.sin(angle) * length * 0.72, 0.035, Math.cos(angle) * length * 0.72], frame, [Math.PI / 2, 0, 0]);
  }
}

function buildSofa(group, object) {
  const [rawWidth, rawHeight, rawDepth] = object.size;
  const width = THREE.MathUtils.clamp(rawWidth, 1.05, 3.1);
  const depth = THREE.MathUtils.clamp(rawDepth, 0.62, 1.25);
  const height = THREE.MathUtils.clamp(rawHeight, 0.68, 1.25);
  const upholstery = standardMaterial(object.color, { roughness: 0.92 });
  const dark = standardMaterial(new THREE.Color(object.color).multiplyScalar(0.68), { roughness: 0.9 });
  addBox(group, [width, 0.22, depth * 0.9], [0, 0.20, 0], dark);
  addBox(group, [width * 0.88, 0.16, depth * 0.72], [0, 0.39, 0.03], upholstery);
  addBox(group, [width * 0.88, height - 0.36, 0.16], [0, 0.36 + (height - 0.36) / 2, -depth / 2 + 0.10], upholstery);
  for (const x of [-width / 2 + 0.09, width / 2 - 0.09]) {
    addBox(group, [0.18, 0.45, depth * 0.82], [x, 0.36, 0], upholstery);
  }
}

function buildCabinet(group, object) {
  const [rawWidth, rawHeight, rawDepth] = object.size;
  const width = THREE.MathUtils.clamp(rawWidth, 0.45, 2.35);
  const depth = THREE.MathUtils.clamp(rawDepth, 0.24, 0.92);
  const height = THREE.MathUtils.clamp(rawHeight, 0.65, 2.45);
  const body = standardMaterial(object.color, { roughness: 0.74 });
  const front = standardMaterial(new THREE.Color(object.color).offsetHSL(0, -0.04, 0.06), { roughness: 0.65 });
  const handle = standardMaterial(0x889397, { metalness: 0.76, roughness: 0.25 });
  addBox(group, [width, height, depth], [0, height / 2, 0], body);
  const doorGap = 0.018;
  for (const x of [-width * 0.25, width * 0.25]) {
    addBox(group, [width * 0.49 - doorGap, height * 0.91, 0.025], [x, height * 0.51, depth / 2 + 0.014], front);
    addBox(group, [0.025, Math.min(0.22, height * 0.18), 0.035], [x + Math.sign(-x || 1) * width * 0.12, height * 0.55, depth / 2 + 0.045], handle);
  }
}

function buildTree(group, object) {
  // Parametric site tree: size = [canopy width, total height, canopy depth].
  // layout.form 'conifer' stacks cones; default is a broadleaf blob cluster.
  const [width, height, depth] = object.size;
  const canopyColor = new THREE.Color(object.color || '#4c7a3d');
  const trunkHeight = Math.max(0.5, height * (object.layout?.trunkRatio ?? 0.32));
  const trunkRadius = THREE.MathUtils.clamp(Math.min(width, depth) * 0.03, 0.08, 0.32);
  const trunk = standardMaterial(0x6d5236, { roughness: 0.92 });
  // The visible trunk continues INTO the canopy - stopping it at the canopy
  // base leaves a floating-blob gap.
  const trunkReach = trunkHeight + (height - trunkHeight) * 0.55;
  addCylinder(group, trunkRadius, trunkReach, [0, trunkReach / 2, 0], trunk);
  const foliage = standardMaterial(canopyColor, { roughness: 0.94 });
  if (object.layout?.form === 'conifer') {
    const layers = 3;
    for (let index = 0; index < layers; index += 1) {
      const t = index / layers;
      const radius = (Math.max(width, depth) / 2) * (1 - t * 0.55);
      const coneHeight = (height - trunkHeight) * 0.5;
      const cone = new THREE.Mesh(new THREE.ConeGeometry(radius, coneHeight, 10), foliage);
      cone.position.y = trunkHeight + coneHeight * (0.35 + t * 0.85);
      cone.castShadow = true;
      group.add(cone);
    }
  } else {
    const canopyHeight = height - trunkHeight;
    const blobs = [
      [0, 0.42, 0, 0.52], [width * 0.22, 0.62, depth * 0.12, 0.38],
      [-width * 0.2, 0.58, -depth * 0.16, 0.4], [0, 0.82, 0, 0.34],
    ];
    for (const [bx, by, bz, scale] of blobs) {
      const blob = new THREE.Mesh(new THREE.SphereGeometry(0.5, 12, 9), foliage);
      blob.scale.set(width * scale, canopyHeight * scale, depth * scale);
      blob.position.set(bx, trunkHeight + canopyHeight * by, bz);
      blob.castShadow = true;
      group.add(blob);
    }
  }
}

function buildVehicle(group, object) {
  // Simple site vehicle: body slab + cabin + wheels. size = [length, height, width].
  const [length, height, width] = object.size;
  const body = standardMaterial(object.color || '#d8d8d8', { roughness: 0.42, metalness: 0.35 });
  const bodyHeight = height * 0.42;
  const wheelRadius = Math.min(0.38, height * 0.24);
  addBox(group, [length, bodyHeight, width], [0, wheelRadius + bodyHeight / 2, 0], body);
  const cabinLength = length * (object.layout?.kind === 'pickup' ? 0.4 : 0.62);
  const cabinOffset = object.layout?.kind === 'pickup' ? -length * 0.16 : 0;
  const cabin = standardMaterial(object.color || '#d8d8d8', { roughness: 0.3, metalness: 0.25, transparent: true, opacity: 0.92 });
  addBox(group, [cabinLength, height * 0.4, width * 0.9], [cabinOffset, wheelRadius + bodyHeight + height * 0.18, 0], cabin);
  if (object.layout?.kind === 'pickup') {
    // Open cargo bed walls behind the cabin.
    const bed = standardMaterial(object.color || '#d8d8d8', { roughness: 0.5, metalness: 0.3 });
    addBox(group, [length * 0.42, height * 0.16, 0.04], [length * 0.26, wheelRadius + bodyHeight + height * 0.07, width * 0.44], bed);
    addBox(group, [length * 0.42, height * 0.16, 0.04], [length * 0.26, wheelRadius + bodyHeight + height * 0.07, -width * 0.44], bed);
    addBox(group, [0.04, height * 0.16, width * 0.86], [length * 0.465, wheelRadius + bodyHeight + height * 0.07, 0], bed);
  }
  const tire = standardMaterial(0x24272a, { roughness: 0.9 });
  for (const x of [-length * 0.32, length * 0.32]) {
    for (const z of [-width / 2, width / 2]) {
      addCylinder(group, wheelRadius, 0.24, [x, wheelRadius, z], tire, [Math.PI / 2, 0, 0]);
    }
  }
}

function buildRoofPanel(group, object) {
  // Flat tilted roof sheet: size = [span, thickness, depth]; layout.pitchDeg
  // tilts about the local x axis (positive lifts the -z edge).
  const [span, thickness, depth] = object.size;
  const material = standardMaterial(object.color || '#8a8f93', { roughness: 0.6, metalness: 0.25, side: THREE.DoubleSide });
  const panel = addBox(group, [span, thickness, depth / Math.max(0.2, Math.cos(THREE.MathUtils.degToRad(object.layout?.pitchDeg || 0)))], [0, 0, 0], material);
  panel.rotation.x = THREE.MathUtils.degToRad(object.layout?.pitchDeg || 0);
  panel.position.y = 0;
}

function buildTripod(group, object) {
  // Surveying tripod: three legs meeting at the head. size = [spread, height, spread].
  // Each leg is aligned apex->foot with a quaternion; compound Euler angles
  // do NOT make the legs converge.
  const [spread, height] = object.size;
  const leg = standardMaterial(object.color || '#c8a23c', { roughness: 0.55, metalness: 0.3 });
  const legRadius = 0.022;
  const up = new THREE.Vector3(0, 1, 0);
  for (let index = 0; index < 3; index += 1) {
    const angle = (index / 3) * Math.PI * 2;
    const foot = new THREE.Vector3(Math.sin(angle) * spread / 2, 0, Math.cos(angle) * spread / 2);
    const apex = new THREE.Vector3(0, height * 0.96, 0);
    const axis = apex.clone().sub(foot);
    const mesh = new THREE.Mesh(new THREE.CylinderGeometry(legRadius, legRadius, axis.length(), 8), leg);
    mesh.position.copy(foot.clone().add(apex).multiplyScalar(0.5));
    mesh.quaternion.setFromUnitVectors(up, axis.normalize());
    mesh.castShadow = true;
    group.add(mesh);
  }
  addCylinder(group, 0.09, 0.06, [0, height * 0.98, 0], standardMaterial(0x3a4046, { metalness: 0.5, roughness: 0.4 }));
}

function buildSignboard(group, object) {
  // Wall-mounted sign: size = [width, height, thickness]; face slightly proud.
  const [width, height, thickness] = object.size;
  addBox(group, [width, height, thickness], [0, 0, 0], standardMaterial(0xf2efe8, { roughness: 0.5 }));
  addBox(group, [width * 0.96, height * 0.85, 0.006], [0, 0, thickness / 2 + 0.004],
    standardMaterial(object.color || '#c8442c', { roughness: 0.45 }));
}

function buildStep(group, object) {
  // Two-tread masonry step block: size = [width, total height, depth].
  const [width, height, depth] = object.size;
  const concrete = standardMaterial(object.color || '#b9b2a6', { roughness: 0.9 });
  addBox(group, [width, height / 2, depth], [0, height / 4, 0], concrete);
  addBox(group, [width, height / 2, depth / 2], [0, height * 0.75, -depth / 4], concrete);
}

function buildGeneric(group, object) {
  const [width, height, depth] = object.size;
  const material = standardMaterial(object.color, { roughness: 0.72, transparent: true, opacity: 0.72 });
  const mesh = addBox(group, [width, height, depth], [0, height / 2, 0], material);
  const edges = new THREE.LineSegments(new THREE.EdgesGeometry(mesh.geometry), new THREE.LineBasicMaterial({ color: 0xb9f5dc, transparent: true, opacity: 0.6 }));
  edges.position.copy(mesh.position);
  group.add(edges);
}

function buildObject(object) {
  const accepted = object.deliveryValidation?.status === 'PASS';
  const group = new THREE.Group();
  group.name = object.id;
  group.position.set(...object.center);
  if (Array.isArray(object.renderAdjustments?.centerDisplayDelta) && object.renderAdjustments.centerDisplayDelta.length === 3) {
    group.position.add(new THREE.Vector3(...object.renderAdjustments.centerDisplayDelta));
  }
  group.rotation.y = object.yaw;
  if (Number.isFinite(object.renderAdjustments?.widthDeltaM)) object.size[0] += object.renderAdjustments.widthDeltaM;
  group.userData.sceneObject = object;
  switch (object.category) {
    case 'table': buildTable(group, object, false); break;
    case 'workstation': buildTable(group, object, true); break;
    case 'wall-workbench': buildWallWorkbench(group, object); break;
    case 'round-table': buildRoundTable(group, object, false); break;
    case 'oval-table': buildRoundTable(group, object, true); break;
    case 'meeting-table': buildMeetingTable(group, object); break;
    case 'booth-desk': buildBoothDesk(group, object); break;
    case 'chair': buildChair(group, object); break;
    case 'sofa': buildSofa(group, object); break;
    case 'cabinet': buildCabinet(group, object); break;
    case 'tree': buildTree(group, object); break;
    case 'vehicle': buildVehicle(group, object); break;
    case 'roof-panel': buildRoofPanel(group, object); break;
    case 'step': buildStep(group, object); break;
    case 'tripod': buildTripod(group, object); break;
    case 'signboard': buildSignboard(group, object); break;
    default: buildGeneric(group, object); break;
  }
  const reviewEdges = [];
  group.traverse((child) => {
    if (!child.isMesh) return;
    child.userData.owner = group;
    if (!accepted) {
      child.material = child.material.clone();
      child.material.color.set(0xff9a55);
      child.material.transparent = true;
      child.material.opacity = 0.18;
      child.material.depthWrite = false;
      child.castShadow = false;
      child.receiveShadow = false;
      const edges = new THREE.LineSegments(
        new THREE.EdgesGeometry(child.geometry),
        new THREE.LineDashedMaterial({ color:0xffb071, dashSize:0.12, gapSize:0.08, transparent:true, opacity:0.88 }),
      );
      edges.position.copy(child.position);
      edges.rotation.copy(child.rotation);
      edges.scale.copy(child.scale);
      edges.computeLineDistances();
      reviewEdges.push(edges);
    }
  });
  reviewEdges.forEach((edges) => group.add(edges));
  (accepted ? objectGroup : reviewObjectGroup).add(group);
  objectMeshes.set(object.id, group);
}

function buildWalls(walls) {
  const material = standardMaterial(0xc7d0cc, { roughness: 0.9, transparent: true, opacity: 0.73, side: THREE.DoubleSide });
  const edgeMaterial = new THREE.LineBasicMaterial({ color: 0xef8354, transparent: true, opacity: 0.72 });
  for (const wall of walls) {
    const start = new THREE.Vector3(...wall.start);
    const end = new THREE.Vector3(...wall.end);
    const delta = end.clone().sub(start);
    const length = Math.hypot(delta.x, delta.z);
    const geometry = new THREE.BoxGeometry(length, wall.height, wall.thickness);
    const mesh = new THREE.Mesh(geometry, material.clone());
    mesh.position.copy(start.clone().add(end).multiplyScalar(0.5));
    mesh.position.y = wall.height / 2;
    mesh.rotation.y = -Math.atan2(delta.z, delta.x);
    mesh.castShadow = true;
    mesh.receiveShadow = true;
    mesh.userData.wall = wall;
    wallGroup.add(mesh);
    const edges = new THREE.LineSegments(new THREE.EdgesGeometry(geometry), edgeMaterial);
    edges.position.copy(mesh.position);
    edges.rotation.copy(mesh.rotation);
    wallGroup.add(edges);
  }
}

function computeSolidWallJoinExtensions(structures) {
  const solids = structures.filter((structure) => (
    structure.geometryType === 'segment'
    && structure.category === 'wall'
    && structure.decision?.status?.startsWith('accepted')
  ));
  const materialKey = (structure) => `${structure.material?.color || ''}|${structure.material?.description || ''}`.toLowerCase();
  const endpoint = (structure, index) => new THREE.Vector3(...(index === 0 ? structure.start : structure.end));
  const pointSegmentDistance = (point, start, end) => {
    const segment = end.clone().sub(start);
    const denominator = segment.lengthSq();
    if (denominator < 1e-10) return point.distanceTo(start);
    const parameter = THREE.MathUtils.clamp(point.clone().sub(start).dot(segment) / denominator, 0, 1);
    return point.distanceTo(start.clone().add(segment.multiplyScalar(parameter)));
  };
  const extensions = new Map(solids.map((structure) => [structure.id, [0, 0]]));
  const visualJoints = [];

  for (let leftIndex = 0; leftIndex < solids.length; leftIndex += 1) {
    const left = solids[leftIndex];
    for (let rightIndex = leftIndex + 1; rightIndex < solids.length; rightIndex += 1) {
      const right = solids[rightIndex];
      if (materialKey(left) !== materialKey(right)) continue;
      const leftBase = left.baseHeight || 0;
      const rightBase = right.baseHeight || 0;
      const verticalOverlap = Math.min(leftBase + left.height, rightBase + right.height) - Math.max(leftBase, rightBase);
      if (verticalOverlap < 0.05) continue;

      const overlap = Math.min(
        SOLID_WALL_JOIN_OVERLAP_M,
        Math.max(0.012, Math.min(left.thickness || 0.12, right.thickness || 0.12) * 0.25),
      );
      for (const [source, target] of [[left, right], [right, left]]) {
        const targetStart = endpoint(target, 0);
        const targetEnd = endpoint(target, 1);
        for (let sourceEnd = 0; sourceEnd < 2; sourceEnd += 1) {
          const separation = pointSegmentDistance(endpoint(source, sourceEnd), targetStart, targetEnd);
          if (separation > SOLID_WALL_JOIN_TOLERANCE_M) continue;
          extensions.get(source.id)[sourceEnd] = Math.max(extensions.get(source.id)[sourceEnd], overlap);
          visualJoints.push({ sourceId:source.id, targetId:target.id, sourceEnd, separation, overlap });
        }
      }
    }
  }
  return { extensions, visualJoints };
}

function buildStructures(structures) {
  const solidWallJoints = computeSolidWallJoinExtensions(structures);
  let prismPartCount = 0;
  const renderedVerticalFrames = new Set();
  const claimVerticalFrame = (point) => {
    const key = point.map((value) => Math.round(value * 1000)).join(':');
    if (renderedVerticalFrames.has(key)) return false;
    renderedVerticalFrames.add(key);
    return true;
  };
  for (const structure of structures) {
    const category = structure.category;
    if (structure.geometryType === 'segment') {
      const start = new THREE.Vector3(...structure.start);
      const end = new THREE.Vector3(...structure.end);
      const delta = end.clone().sub(start);
      const length = Math.hypot(delta.x, delta.z);
      const height = structure.height || (category === 'door' ? 2.1 : 3.05);
      const base = structure.baseHeight || 0;
      const thickness = structure.thickness || (category === 'glass' || category === 'window' ? 0.045 : 0.12);
      const center = start.clone().add(end).multiplyScalar(0.5);
      const yaw = -Math.atan2(delta.z, delta.x);
      const group = new THREE.Group();
      group.name = structure.id;
      group.position.set(center.x, 0, center.z);
      group.rotation.y = yaw;
      if (category === 'glass' || category === 'window') {
        const glass = standardMaterial(category === 'glass' ? 0x8bd5df : 0x9acfe8, { transparent:true, opacity:0.25, roughness:0.12, metalness:0.06, side:THREE.DoubleSide });
        addBox(group, [length, height, thickness], [0, base + height / 2, 0], glass);
        const frame = standardMaterial(0x67767d, { metalness:0.55, roughness:0.34 });
        addBox(group, [length + 0.05, 0.045, thickness + 0.035], [0, base, 0], frame);
        addBox(group, [length + 0.05, 0.045, thickness + 0.035], [0, base + height, 0], frame);
        [[-length / 2, structure.start], [length / 2, structure.end]].forEach(([x, point]) => {
          if (claimVerticalFrame(point)) addBox(group, [0.045, height, thickness + 0.035], [x, base + height / 2, 0], frame);
        });
        if (category === 'glass') {
          const filmHeight = Math.min(1.22, height * 0.52);
          const film = new THREE.Mesh(
            new THREE.PlaneGeometry(length, filmHeight),
            new THREE.MeshStandardMaterial({
              map: makePrivacyFilmTexture(length / 0.55),
              transparent: true,
              opacity: 0.68,
              roughness: 0.58,
              metalness: 0,
              side: THREE.DoubleSide,
              depthWrite: false,
            }),
          );
          film.position.set(0, base + filmHeight / 2, thickness / 2 + 0.006);
          film.castShadow = false;
          group.add(film);
        } else {
          const mullionCount = Math.max(0, Math.round(length / 1.35) - 1);
          for (let index = 1; index <= mullionCount; index += 1) {
            const x = -length / 2 + (length * index) / (mullionCount + 1);
            addBox(group, [0.038, height, thickness + 0.03], [x, base + height / 2, 0], frame);
          }
          if (/roller blind|卷帘/i.test(structure.material?.description || '')) addRollerBlind(group, length, base, height, thickness);
        }
      } else if (category === 'door') {
        const isGlassDoor = /glass|玻璃/i.test(structure.material?.description || '');
        const panel = isGlassDoor
          ? standardMaterial(0x9edce5, { transparent:true, opacity:0.28, roughness:0.12, metalness:0.04, side:THREE.DoubleSide })
          : standardMaterial(0x9b7658, { roughness:0.62 });
        addBox(group, [length * 0.94, height * 0.97, thickness], [0, base + height * 0.485, 0], panel);
        const frame = standardMaterial(0x4f5b60, { metalness:0.28, roughness:0.48 });
        [[-length / 2, structure.start], [length / 2, structure.end]].forEach(([x, point]) => {
          if (claimVerticalFrame(point)) addBox(group, [0.055, height + 0.06, thickness + 0.04], [x, base + height / 2, 0], frame);
        });
        addBox(group, [length + 0.055, 0.055, thickness + 0.04], [0, base + height, 0], frame);
        if (isGlassDoor) {
          const filmHeight = Math.min(1.22, height * 0.52);
          const film = new THREE.Mesh(
            new THREE.PlaneGeometry(length * 0.91, filmHeight),
            new THREE.MeshStandardMaterial({ map: makePrivacyFilmTexture(length / 0.32), transparent:true, opacity:0.68, roughness:0.58, side:THREE.DoubleSide, depthWrite:false }),
          );
          film.position.set(0, base + filmHeight / 2, thickness / 2 + 0.006);
          group.add(film);
        }
        addCylinder(group, 0.028, 0.06, [length * 0.32, base + height * 0.5, thickness / 2 + 0.04], standardMaterial(0xd0b168, { metalness:0.8, roughness:0.25 }), [Math.PI / 2, 0, 0]);
      } else {
        const [startOverlap, endOverlap] = solidWallJoints.extensions.get(structure.id) || [0, 0];
        const renderLength = length + startOverlap + endOverlap;
        const renderOffset = (endOverlap - startOverlap) / 2;
        const bands = structure.material?.bands;
        if (Array.isArray(bands) && bands.length) {
          for (const band of bands) {
            addBox(
              group,
              [renderLength, band.height, thickness],
              [renderOffset, base + band.base + band.height / 2, 0],
              standardMaterial(band.color, { roughness:0.84 }),
            );
          }
        } else {
          const description = structure.material?.description || '';
          const explicitColor = structure.material?.color ? Number.parseInt(structure.material.color.replace('#', ''), 16) : null;
          const color = Number.isFinite(explicitColor) ? explicitColor
            : /oak|wood|木/i.test(description) ? 0xc2a179
            : /dark gray|charcoal|深灰/i.test(description) ? 0x596066
              : /warm-gray|暖灰/i.test(description) ? 0xc7c0b6 : 0xd8d6ce;
          const wall = standardMaterial(color, { roughness:0.9 });
          addBox(group, [renderLength, height, thickness], [renderOffset, base + height / 2, 0], wall);
        }
        group.userData.visualJointOverlap = { start:startOverlap, end:endOverlap };
      }
      group.userData.structure = structure;
      wallGroup.add(group);
    } else if (structure.geometryType === 'prism' && Array.isArray(structure.footprint) && structure.footprint.length >= 3) {
      // Scene V2 wall solids: plan footprints already carry mitered corners
      // and T-embed extensions from scene-core joinery; openings are real
      // holes, so no render-only overlap is applied here.
      const shape = new THREE.Shape();
      structure.footprint.forEach((point, index) => index ? shape.lineTo(point[0], -point[1]) : shape.moveTo(point[0], -point[1]));
      shape.closePath();
      const description = structure.material?.description || '';
      const explicitColor = structure.material?.color ? Number.parseInt(structure.material.color.replace('#', ''), 16) : null;
      const color = Number.isFinite(explicitColor) ? explicitColor
        : /oak|wood|木/i.test(description) ? 0xc2a179
        : /dark gray|charcoal|深灰/i.test(description) ? 0x596066
          : /warm-gray|暖灰/i.test(description) ? 0xc7c0b6 : 0xd8d6ce;
      const geometry = new THREE.ExtrudeGeometry(shape, { depth: structure.height, bevelEnabled: false, curveSegments: 1 });
      const mesh = new THREE.Mesh(geometry, standardMaterial(color, { roughness: 0.9 }));
      mesh.rotation.x = -Math.PI / 2;
      mesh.position.y = structure.baseHeight || 0;
      mesh.castShadow = true;
      mesh.receiveShadow = true;
      mesh.userData.structure = structure;
      wallGroup.add(mesh);
      prismPartCount += 1;
    } else if (structure.geometryType === 'rectangle') {
      const group = new THREE.Group();
      group.position.set(...structure.center);
      group.rotation.y = structure.yaw || 0;
      const height = structure.height || 3.05;
      addBox(group, [structure.size[0], height, structure.size[1]], [0, (structure.baseHeight || 0) + height / 2, 0], standardMaterial(0xc8c6bd, { roughness:0.86 }));
      group.userData.structure = structure;
      wallGroup.add(group);
    } else if (structure.geometryType === 'polygon' && structure.points.length >= 3) {
      const shape = new THREE.Shape();
      structure.points.forEach((point, index) => index ? shape.lineTo(point[0], -point[2]) : shape.moveTo(point[0], -point[2]));
      shape.closePath();
      const isFloor = structure.category === 'floor-zone';
      const isCeiling = structure.category === 'ceiling-zone';
      const explicitColor = structure.material?.color ? Number.parseInt(structure.material.color.replace('#', ''), 16) : null;
      const surfaceColor = Number.isFinite(explicitColor) ? explicitColor : (isCeiling ? 0xe9e6dc : 0x5b6260);
      const slabThickness = THREE.MathUtils.clamp(structure.height || 0.05, 0.035, 0.10);
      const geometry = isFloor
        ? new THREE.ExtrudeGeometry(shape, { depth:slabThickness, bevelEnabled:false, curveSegments:1 })
        : new THREE.ShapeGeometry(shape);
      const mesh = new THREE.Mesh(geometry, standardMaterial(surfaceColor, {
        roughness:structure.material?.roughness ?? 0.94,
        side:THREE.DoubleSide,
        transparent:isCeiling,
        opacity:isCeiling ? (structure.material?.opacity ?? 0.9) : 1,
      }));
      mesh.rotation.x = -Math.PI / 2;
      mesh.position.y = isCeiling ? (structure.baseHeight || structure.height || 3.05) : (structure.baseHeight || 0);
      mesh.castShadow = false;
      mesh.receiveShadow = true;
      mesh.userData.structure = structure;
      mesh.userData.horizontalSurface = true;
      mesh.userData.floorSurface = isFloor;
      mesh.userData.slabThickness = isFloor ? slabThickness : 0;
      if (isFloor) floorSurfaceMeshes.push(mesh);
      wallGroup.add(mesh);
    }
  }
  canvas.dataset.floorSurfaceCount = String(floorSurfaceMeshes.length);
  canvas.dataset.floorRenderContract = floorSurfaceMeshes.length ? 'solid-slab' : 'missing';
  canvas.dataset.solidWallJointCount = String(solidWallJoints.visualJoints.length);
  canvas.dataset.solidWallJointContract = prismPartCount ? 'derived-joinery-v2' : 'same-material-overlap-v1';
  canvas.dataset.prismPartCount = String(prismPartCount);
}

function buildCandidateStructures(structures) {
  const fill = standardMaterial(0xff9b5f, {
    transparent: true,
    opacity: 0.075,
    roughness: 0.7,
    depthWrite: false,
    side: THREE.DoubleSide,
  });
  const edge = new THREE.LineDashedMaterial({ color: 0xffa066, dashSize: 0.18, gapSize: 0.1, transparent: true, opacity: 0.92 });
  for (const structure of structures) {
    const height = structure.height || 3.05;
    const base = structure.baseHeight || 0;
    let geometry;
    let position;
    let yaw = 0;
    if (structure.geometryType === 'segment') {
      const start = new THREE.Vector3(...structure.start);
      const end = new THREE.Vector3(...structure.end);
      const delta = end.clone().sub(start);
      const length = Math.hypot(delta.x, delta.z);
      const thickness = Math.max(structure.thickness || 0.08, 0.055);
      geometry = new THREE.BoxGeometry(length, height, thickness);
      position = start.clone().add(end).multiplyScalar(0.5);
      yaw = -Math.atan2(delta.z, delta.x);
    } else if (structure.geometryType === 'rectangle') {
      geometry = new THREE.BoxGeometry(structure.size[0], height, structure.size[1]);
      position = new THREE.Vector3(...structure.center);
      yaw = structure.yaw || 0;
    } else {
      continue;
    }
    const mesh = new THREE.Mesh(geometry, fill.clone());
    mesh.position.copy(position);
    mesh.position.y = base + height / 2;
    mesh.rotation.y = yaw;
    mesh.renderOrder = 3;
    mesh.userData.structureCandidate = structure;
    candidateStructureGroup.add(mesh);
    const edges = new THREE.LineSegments(new THREE.EdgesGeometry(geometry), edge.clone());
    edges.computeLineDistances();
    edges.position.copy(mesh.position);
    edges.rotation.copy(mesh.rotation);
    edges.renderOrder = 4;
    candidateStructureGroup.add(edges);
  }
}

function buildGround(envelope) {
  // Accepted floor-zone polygons are the finished floor. A world-axis GridHelper
  // is a diagnostic overlay, not a scanned material, and must not enter model mode.
  void envelope;
}

async function buildPointCloud(url) {
  const response = await fetch(url);
  if (!response.ok) throw new Error(`${t('loadCloudFailed')} (${response.status})`);
  const buffer = await response.arrayBuffer();
  const view = new DataView(buffer);
  const magic = String.fromCharCode(view.getUint8(0), view.getUint8(1), view.getUint8(2), view.getUint8(3));
  const version = view.getUint32(4, true);
  const count = view.getUint32(8, true);
  const stride = view.getUint32(12, true);
  if (magic !== 'LRPC' || version !== 1 || stride !== 16 || buffer.byteLength !== 16 + count * stride) {
    throw new Error(t('cloudContractFailed'));
  }
  const positions = new Float32Array(count * 3);
  const colors = new Uint8Array(count * 3);
  let offset = 16;
  for (let index = 0; index < count; index += 1, offset += stride) {
    positions[index * 3] = view.getFloat32(offset, true);
    positions[index * 3 + 1] = view.getFloat32(offset + 4, true);
    positions[index * 3 + 2] = view.getFloat32(offset + 8, true);
    colors[index * 3] = view.getUint8(offset + 12);
    colors[index * 3 + 1] = view.getUint8(offset + 13);
    colors[index * 3 + 2] = view.getUint8(offset + 14);
  }
  const geometry = new THREE.BufferGeometry();
  geometry.setAttribute('position', new THREE.BufferAttribute(positions, 3));
  geometry.setAttribute('color', new THREE.BufferAttribute(colors, 3, true));
  geometry.computeBoundingSphere();
  pointMaterial = new THREE.PointsMaterial({ size: 0.035, vertexColors: true, sizeAttenuation: true, transparent: true, opacity: 0.78 });
  const points = new THREE.Points(geometry, pointMaterial);
  pointGroup.add(points);
}

function buildCameraPath(points) {
  const positions = [];
  for (const point of points) positions.push(...point.position);
  const geometry = new THREE.BufferGeometry();
  geometry.setAttribute('position', new THREE.Float32BufferAttribute(positions, 3));
  const line = new THREE.Line(geometry, new THREE.LineBasicMaterial({ color: 0x4c8dff, transparent: true, opacity: 0.88 }));
  cameraGroup.add(line);
  for (let index = 0; index < points.length; index += Math.max(1, Math.floor(points.length / 18))) {
    const marker = new THREE.Mesh(new THREE.SphereGeometry(0.055, 12, 8), standardMaterial(0x4c8dff, { metalness: 0.15, roughness: 0.45 }));
    marker.position.set(...points[index].position);
    cameraGroup.add(marker);
  }
}

function stageClass(status) {
  return status === 'PASS' ? '' : ' fail';
}

function renderPanels(data) {
  const hasStructureCandidates = (data.structureCandidates || []).length > 0;
  const hasIncompleteStage = (data.pipeline || []).some((stage) => stage.status !== 'PASS');
  const displayCount = (data.structures || data.walls || []).length;
  const reportedAuthorityCount = data.freshAuthorityReview?.acceptedMeasuredWallCount;
  const hasSeparatedAuthority = Number.isInteger(reportedAuthorityCount);
  const authorityCount = hasSeparatedAuthority ? reportedAuthorityCount : displayCount;
  const pipeline = data.pipeline.map((stage) => stage.id === 'seed' && !hasStructureCandidates
    ? { ...stage, label: t('autoReviewed'), status: 'PASS' }
    : stage);
  document.querySelector('#pipeline').innerHTML = pipeline.map((stage) => `
    <div class="stage${stageClass(stage.status)}"><span>${currentLanguage === 'en' ? ({ingest:'Point cloud and camera registration',seed:'AI proposal review',objects:'Full-plan visual inspection',structures:'Structural evidence gates',assets:'Parametric scene generation',author:'3D completion and final review'}[stage.id] || stage.label) : stage.label}</span><em>${localizedStatus(stage.status)}</em></div>
  `).join('');
  const candidateLegend = document.querySelector('.viewport-hud .candidate')?.parentElement;
  if (candidateLegend) candidateLegend.hidden = !hasStructureCandidates;
  document.querySelector('.confidence-note').textContent = hasStructureCandidates
    ? t('confidenceDefault')
    : t(hasIncompleteStage ? 'confidenceWip' : 'confidenceResolved');
  document.querySelector('#scene-stats').innerHTML = `
    <div class="metric"><strong>${data.source.samplePointCount.toLocaleString(currentLanguage)}</strong><span>${t('previewPoints')}</span></div>
    <div class="metric"><strong>${hasSeparatedAuthority ? `${authorityCount} / ${displayCount}` : displayCount}</strong><span>${t(hasSeparatedAuthority ? 'authorityDisplayStructures' : (hasIncompleteStage ? 'displayStructures' : 'acceptedStructures'))}</span></div>
    <div class="metric"><strong>${data.objects.length}</strong><span>${t('localElements')}</span></div>
    <div class="metric"><strong>${data.levels[0].height.toFixed(2)} m</strong><span>${t('estimatedHeight')}</span></div>
  `;
  document.querySelector('#object-count').textContent = String(data.objects.length);
  const categories = ['all', ...new Set(data.objects.map((object) => object.category))];
  document.querySelector('#category-filters').innerHTML = categories.map((category) => `
    <button type="button" data-category="${category}" class="${category === activeCategory ? 'active' : ''}">${t(category)}</button>
  `).join('');
  document.querySelector('#quality-loops').innerHTML = data.qualityLoops.map((loop) => {
    const displayStatus = loop.status === 'REVIEW' && loop.blocking === false ? 'ADVISORY' : loop.status;
    if (Array.isArray(loop.spaces)) {
      const violations = loop.spaces.flatMap((space) => (space.regularityViolations || []).map((item) => `${space.id} ${item.angleDeltaDeg ?? '—'}°`));
      const topology = ` · ${t('geometryClosed')} ${loop.spaces.filter((space) => space.geometryClosure === 'PASS').length}/${loop.spaces.length} · ${t('publishedClosed')} ${loop.spaces.filter((space) => space.status === 'PASS').length}/${loop.spaces.length}`;
      const violationText = violations.length ? `<small>${t('shapeFailure')}: ${violations.join(' / ')}</small>` : '';
      return `<div class="quality-item"><b>${loop.iteration}. ${loop.name}</b><span>${localizedStatus(displayStatus)}${topology}${violationText}</span></div>`;
    }
    const remaining = ` · ${t('retained')} ${loop.remainingCount ?? loop.groundedCount ?? loop.measuredCandidateCount ?? '—'}`;
    return `<div class="quality-item"><b>${loop.iteration}. ${loop.name}</b><span>${localizedStatus(displayStatus)}${remaining}</span></div>`;
  }).join('');
  renderObjectList();
  renderPhotos(data.photos);
}

function renderObjectList() {
  const rows = sceneData.objects.filter((object) => activeCategory === 'all' || object.category === activeCategory);
  document.querySelector('#object-list').innerHTML = rows.map((object) => `
    <button type="button" class="object-row${object.deliveryValidation?.status === 'PASS' ? '' : ' review'}" data-object-id="${object.id}" style="--item-color:${object.deliveryValidation?.status === 'PASS' ? object.color : '#ff9a55'}">
      <i></i><span><b>${objectDisplayName(object)}</b><small>${object.size.map((value) => value.toFixed(2)).join(' × ')} m · ${object.id}</small></span><span>${object.deliveryValidation?.status === 'PASS' ? (object.furnitureValidation?.evidenceClass === 'accepted-inferred' ? 'INFERRED' : `${Math.round(object.confidence * 100)}%`) : 'REVIEW'}</span>
    </button>
  `).join('');
}

// Photo URLs resolve against the scene JSON location, exactly like the LRPC
// point-cloud artifact, so a scene directory stays portable as one unit.
function resolveSceneAsset(path) {
  if (!path) return path;
  try { return new URL(path, sceneBaseUrl).toString(); } catch { return path; }
}

function renderPhotos(photos) {
  const strip = document.querySelector('#photo-strip');
  strip.innerHTML = photos.map((photo, index) => `
    <button type="button" data-photo-index="${index}" class="${index === selectedPhotoIndex ? 'active' : ''}"><img src="${resolveSceneAsset(photo.path)}" alt="${t('evidenceFrame')} ${index + 1}"></button>
  `).join('');
  if (photos.length) showPhoto(Math.min(selectedPhotoIndex, photos.length - 1));
  else document.querySelector('#photo-align-enter').hidden = true;
  updateFrustumToggle();
}

function showPhoto(index) {
  const photo = sceneData.photos[index];
  if (!photo) return;
  selectedPhotoIndex = index;
  document.querySelector('#evidence-photo').src = resolveSceneAsset(photo.path);
  document.querySelector('#evidence-caption').textContent = `${photo.id} · ${t('originalFrame')} ${photo.sourceFile || photo.path || photo.id} · ${t('localPreview')}`;
  document.querySelectorAll('[data-photo-index]').forEach((button) => button.classList.toggle('active', Number(button.dataset.photoIndex) === index));
  const alignButton = document.querySelector('#photo-align-enter');
  alignButton.hidden = !Array.isArray(photo.transformMatrix);
  alignButton.dataset.photoIndex = String(index);
}

function findClosestPreview(object) {
  const cameraNumber = Number(object.evidence?.nearestCameraId?.replace(/\D/g, '') ?? NaN) - 1;
  let best = 0;
  let bestDistance = Infinity;
  sceneData.photos.forEach((photo, index) => {
    const distance = Math.abs(photo.frameIndex - cameraNumber);
    if (Number.isFinite(distance) && distance < bestDistance) { best = index; bestDistance = distance; }
  });
  return best;
}

// --- P2-2: photo / panorama alignment view -------------------------------
// meta.photos entries may carry a scene-source-frame camera-to-world matrix
// (OpenGL convention: camera looks down -Z, +Y up) plus pinhole intrinsics.
// Source frame -> display frame follows coordinateFrame.sourceToDisplay:
// display = [x - offset[0], elevation, -y - offset[1]].
const SOURCE_TO_DISPLAY = new THREE.Matrix4().set(
  1, 0, 0, 0,
  0, 0, 1, 0,
  0, -1, 0, 0,
  0, 0, 0, 1,
);
// Equirect texture centre lands on the sphere's local -X in three.js after the
// inside-out scale(-1,1,1); rotate -90 deg about Y so it faces the camera -Z.
const PANO_CENTER_FIX = new THREE.Quaternion().setFromAxisAngle(new THREE.Vector3(0, 1, 0), -Math.PI / 2);

function displayOffsetOf() {
  return sceneData?.focusEnvelope?.displayOffset || rawSceneV2?.meta?.displayOffset || [0, 0];
}

function photoPoseMatrix(photo) {
  const m = photo?.transformMatrix;
  if (!Array.isArray(m) || m.length < 3) return null;
  const source = new THREE.Matrix4().set(
    m[0][0], m[0][1], m[0][2], m[0][3],
    m[1][0], m[1][1], m[1][2], m[1][3],
    m[2][0], m[2][1], m[2][2], m[2][3],
    0, 0, 0, 1,
  );
  const pose = new THREE.Matrix4().multiplyMatrices(SOURCE_TO_DISPLAY, source);
  const offset = displayOffsetOf();
  pose.elements[12] -= offset[0];
  pose.elements[14] -= offset[1];
  return pose;
}

function photoVerticalFovDeg(photo) {
  const intrinsics = photo?.intrinsics;
  if (intrinsics?.flY && intrinsics?.height) {
    return THREE.MathUtils.radToDeg(2 * Math.atan((intrinsics.height / 2) / intrinsics.flY));
  }
  return 75;
}

function updateFrustumToggle() {
  const button = document.querySelector('#frustum-toggle');
  if (!button) return;
  const posedCount = (sceneData?.photos || []).filter((photo) => Array.isArray(photo.transformMatrix)).length;
  button.hidden = posedCount <= PHOTO_MARKER_BUDGET;
  button.textContent = t(photoMarkersShowAll ? 'photoFrustumsSampled' : 'photoFrustumsAll');
}

function buildPhotoMarkers() {
  disposeGroupChildren(photoMarkerGroup);
  const posed = (sceneData?.photos || [])
    .map((photo, index) => ({ photo, index }))
    .filter((entry) => Array.isArray(entry.photo.transformMatrix));
  const stride = (!photoMarkersShowAll && posed.length > PHOTO_MARKER_BUDGET)
    ? Math.ceil(posed.length / PHOTO_MARKER_BUDGET)
    : 1;
  posed.forEach((entry, order) => {
    if (order % stride) return;
    const pose = photoPoseMatrix(entry.photo);
    if (!pose) return;
    const marker = new THREE.Group();
    const depth = 0.42;
    const halfY = Math.tan(THREE.MathUtils.degToRad(photoVerticalFovDeg(entry.photo)) / 2) * depth;
    const aspect = entry.photo.intrinsics
      ? entry.photo.intrinsics.width / entry.photo.intrinsics.height
      : 1;
    const halfX = halfY * aspect;
    const corners = [[-halfX, -halfY], [halfX, -halfY], [halfX, halfY], [-halfX, halfY]];
    const points = [];
    for (let index = 0; index < 4; index += 1) {
      const [x0, y0] = corners[index];
      const [x1, y1] = corners[(index + 1) % 4];
      points.push(0, 0, 0, x0, y0, -depth, x0, y0, -depth, x1, y1, -depth);
    }
    const geometry = new THREE.BufferGeometry();
    geometry.setAttribute('position', new THREE.Float32BufferAttribute(points, 3));
    marker.add(new THREE.LineSegments(geometry, new THREE.LineBasicMaterial({ color: 0xffc857, transparent: true, opacity: 0.9 })));
    if (entry.photo.projection === 'equirectangular') {
      const ring = new THREE.Mesh(new THREE.TorusGeometry(0.16, 0.018, 8, 26), standardMaterial(0xffc857, { roughness: 0.5 }));
      ring.rotation.x = Math.PI / 2;
      marker.add(ring);
    }
    marker.add(new THREE.Mesh(
      new THREE.SphereGeometry(0.13, 10, 8),
      standardMaterial(0xffc857, { roughness: 0.4, transparent: true, opacity: 0.85 }),
    ));
    marker.traverse((child) => { child.userData.photoIndex = entry.index; });
    pose.decompose(marker.position, marker.quaternion, new THREE.Vector3());
    photoMarkerGroup.add(marker);
  });
  canvas.dataset.photoMarkerCount = String(photoMarkerGroup.children.length);
  canvas.dataset.photoMarkerStride = String(stride);
}

function buildAlignWireframe() {
  // Bake the current semantic model into an always-on-top wireframe so the
  // photo underlay never occludes the geometry being verified.
  const group = new THREE.Group();
  const material = new THREE.LineBasicMaterial({ color: 0x3af0a4, transparent: true, opacity: 0.85, depthTest: false, fog: false });
  for (const root of [wallGroup, candidateStructureGroup, objectGroup, reviewObjectGroup]) {
    root.updateWorldMatrix(true, true);
    root.traverse((child) => {
      if (!child.isMesh || !child.geometry) return;
      const edges = new THREE.LineSegments(new THREE.EdgesGeometry(child.geometry, 18), material);
      edges.applyMatrix4(child.matrixWorld);
      edges.renderOrder = 30;
      group.add(edges);
    });
  }
  return group;
}

function applyPanoOrientation() {
  if (!photoAlign?.sphere) return;
  const photo = sceneData.photos[photoAlign.index];
  const yawOffset = THREE.MathUtils.degToRad(photo.yawOffsetDeg || 0);
  if (photo.panoOrientation === 'camera') {
    // Pano stitched in the raw camera frame: apply the full pose rotation.
    const worldYaw = new THREE.Quaternion().setFromAxisAngle(new THREE.Vector3(0, 1, 0), yawOffset);
    photoAlign.sphere.quaternion.copy(worldYaw).multiply(photoAlign.quaternion).multiply(PANO_CENTER_FIX);
    return;
  }
  // Default: the stitcher outputs gravity-levelled panoramas (verticals are
  // straight pixel columns), so only the heading of the camera forward axis
  // orients the sphere; pitch/roll of the pose must NOT tilt it.
  const forward = new THREE.Vector3(0, 0, -1).applyQuaternion(photoAlign.quaternion);
  const heading = Math.atan2(-forward.x, -forward.z);
  photoAlign.sphere.quaternion
    .setFromAxisAngle(new THREE.Vector3(0, 1, 0), heading + yawOffset)
    .multiply(PANO_CENTER_FIX);
}

function applyPhotoOpacity(value) {
  if (!photoAlign) return;
  photoAlign.opacity = value;
  if (photoAlign.photoMaterial) photoAlign.photoMaterial.opacity = value;
  // Slider contract: 0 = model only, 1 = photo only (wireframe fades out).
  photoAlign.wireframe.visible = value < 0.995;
  canvas.dataset.photoOpacity = value.toFixed(2);
}

function configureAlignControls(kind, position, forward) {
  clearControlMomentum();
  camera.position.copy(position);
  if (kind === 'equirect') {
    // Free look-around: orbit a point just in front of the optical centre.
    controls.enabled = true;
    controls.enablePan = false;
    controls.enableZoom = false;
    controls.minDistance = 0.05;
    controls.maxDistance = 0.2;
    controls.minPolarAngle = 0.03;
    controls.maxPolarAngle = Math.PI - 0.03;
    controls.target.copy(position).addScaledVector(forward, 0.12);
    controls.update();
  } else {
    // Pinhole alignment: the camera must not move at all.
    controls.enabled = false;
    controls.target.copy(position).addScaledVector(forward, PHOTO_BILLBOARD_DISTANCE_M);
    camera.quaternion.copy(photoAlign.quaternion);
  }
}

function enterPhotoAlign(index) {
  const photo = sceneData?.photos?.[index];
  if (!photo || !Array.isArray(photo.transformMatrix)) return;
  if (photoAlign) exitPhotoAlign();
  const wasOrthographic = projectionMode === 'orthographic';
  if (wasOrthographic) setProjection('perspective');
  const pose = photoPoseMatrix(photo);
  if (!pose) return;
  const position = new THREE.Vector3();
  const quaternion = new THREE.Quaternion();
  pose.decompose(position, quaternion, new THREE.Vector3());
  const forward = new THREE.Vector3(0, 0, -1).applyQuaternion(quaternion);
  photoAlign = {
    index,
    kind: photo.projection === 'equirectangular' ? 'equirect' : 'pinhole',
    position,
    quaternion,
    saved: {
      wasOrthographic,
      fov: perspectiveCamera.fov,
      cameraPosition: camera.position.clone(),
      target: controls.target.clone(),
      fogDensity: scene3d.fog.density,
      markersVisible: photoMarkerGroup.visible,
    },
    overlay: new THREE.Group(),
    wireframe: buildAlignWireframe(),
    photoMaterial: null,
    sphere: null,
    opacity: Number(document.querySelector('#photo-opacity').value),
  };
  scene3d.add(photoAlign.overlay, photoAlign.wireframe);
  scene3d.fog.density = 0;
  photoMarkerGroup.visible = false;
  configureAlignControls(photoAlign.kind, position, forward);
  perspectiveCamera.fov = photoAlign.kind === 'equirect' ? 70 : photoVerticalFovDeg(photo);
  perspectiveCamera.updateProjectionMatrix();
  new THREE.TextureLoader().load(resolveSceneAsset(photo.path), (texture) => {
    if (!photoAlign || photoAlign.index !== index) { texture.dispose(); return; }
    texture.colorSpace = THREE.SRGBColorSpace;
    const aspect = texture.image.width / texture.image.height;
    const equirect = photo.projection === 'equirectangular' || Math.abs(aspect - 2) < 0.2;
    const material = new THREE.MeshBasicMaterial({ map: texture, transparent: true, opacity: photoAlign.opacity, depthTest: false, depthWrite: false, fog: false });
    photoAlign.photoMaterial = material;
    if (equirect) {
      if (photoAlign.kind !== 'equirect') {
        photoAlign.kind = 'equirect';
        configureAlignControls('equirect', position, forward);
      }
      const geometry = new THREE.SphereGeometry(24, 72, 36);
      geometry.scale(-1, 1, 1);
      const sphere = new THREE.Mesh(geometry, material);
      sphere.renderOrder = 20;
      sphere.position.copy(position);
      photoAlign.sphere = sphere;
      applyPanoOrientation();
      photoAlign.overlay.add(sphere);
    } else {
      const fovRad = THREE.MathUtils.degToRad(photoVerticalFovDeg(photo));
      const height = 2 * PHOTO_BILLBOARD_DISTANCE_M * Math.tan(fovRad / 2);
      const width = height * (photo.intrinsics ? photo.intrinsics.width / photo.intrinsics.height : aspect);
      const plane = new THREE.Mesh(new THREE.PlaneGeometry(width, height), material);
      plane.renderOrder = 20;
      plane.position.copy(position).addScaledVector(forward, PHOTO_BILLBOARD_DISTANCE_M);
      plane.quaternion.copy(quaternion);
      photoAlign.overlay.add(plane);
    }
    applyPhotoOpacity(photoAlign.opacity);
    canvas.dataset.photoAlignMode = photoAlign.kind;
  });
  applyPhotoOpacity(photoAlign.opacity);
  document.querySelector('#photo-align-label').textContent = photo.id || photo.path;
  document.querySelector('#photo-align-hud').hidden = false;
  canvas.dataset.photoAlignMode = photoAlign.kind;
  canvas.dataset.photoAlignIndex = String(index);
}

function exitPhotoAlign() {
  if (!photoAlign) return;
  const { saved } = photoAlign;
  for (const node of [photoAlign.overlay, photoAlign.wireframe]) {
    scene3d.remove(node);
    node.traverse((child) => {
      child.geometry?.dispose?.();
      const materials = Array.isArray(child.material) ? child.material : (child.material ? [child.material] : []);
      materials.forEach((material) => { material.map?.dispose?.(); material.dispose?.(); });
    });
  }
  photoAlign = null;
  scene3d.fog.density = saved.fogDensity;
  perspectiveCamera.fov = saved.fov;
  perspectiveCamera.updateProjectionMatrix();
  photoMarkerGroup.visible = saved.markersVisible;
  controls.enabled = true;
  configureControls(controls, saved.target);
  controls.enablePan = true;
  controls.enableZoom = true;
  camera.position.copy(saved.cameraPosition);
  camera.lookAt(saved.target);
  clearControlMomentum();
  if (saved.wasOrthographic) setProjection('orthographic');
  document.querySelector('#photo-align-hud').hidden = true;
  delete canvas.dataset.photoAlignMode;
  delete canvas.dataset.photoAlignIndex;
}

// Debug hook for calibrating pano yaw against real geometry; the accepted
// value is then baked into meta.photos[].yawOffsetDeg by the inject script.
window.__photoAlignDebug = {
  setYawOffset(deg) {
    const photo = sceneData?.photos?.[photoAlign?.index];
    if (!photo) return null;
    photo.yawOffsetDeg = deg;
    applyPanoOrientation();
    return deg;
  },
  state: () => (photoAlign ? { index: photoAlign.index, kind: photoAlign.kind } : null),
};

// --- P2-5: unified review panel ------------------------------------------

function reviewWarningsFor(nodeId) {
  const issues = rawSceneV2?.review?.issues;
  if (!Array.isArray(issues)) return [];
  return issues.filter((issue) => issue && (issue.nodeId === nodeId
    || (Array.isArray(issue.nodeIds) && issue.nodeIds.includes(nodeId))
    || (Array.isArray(issue.targetNodeIds) && issue.targetNodeIds.includes(nodeId))));
}

function reviewElements() {
  // Prefer the raw Scene V2 authority graph: it keeps per-node statuses and
  // evidence sources that the compiled view model flattens away.
  if (rawSceneV2?.nodes) {
    return Object.values(rawSceneV2.nodes)
      .filter((node) => node && node.type !== 'level')
      .map((node) => {
        const entry = rawSceneV2.evidence?.[node.id] || {};
        const gates = entry.gates || node.gates || {};
        return {
          id: node.id,
          type: node.type || 'unknown',
          status: entry.status || 'candidate',
          support: gates.support ?? gates.supportPoints ?? node.support,
          residualP90: gates.residualP90 ?? gates.residual_p90 ?? node.residualP90,
          confidence: node.confidence ?? gates.confidence ?? entry.confidence,
          sources: Array.isArray(entry.sources) ? entry.sources : [],
          warnings: reviewWarningsFor(node.id),
          node,
        };
      });
  }
  const rows = [];
  for (const structure of sceneData?.structures || []) {
    rows.push({ id: structure.id, type: structure.category || 'structure', status: structure.decision?.status || 'accepted-measured', support: undefined, residualP90: undefined, confidence: undefined, sources: [], warnings: [], node: structure });
  }
  for (const structure of sceneData?.structureCandidates || []) {
    rows.push({ id: structure.id, type: structure.category || 'structure', status: structure.decision?.status || 'candidate', support: undefined, residualP90: undefined, confidence: undefined, sources: [], warnings: [], node: structure });
  }
  for (const object of sceneData?.objects || []) {
    const status = ['accepted-measured', 'accepted-inferred', 'candidate', 'rejected'].includes(object.furnitureValidation?.evidenceClass)
      ? object.furnitureValidation.evidenceClass
      : (object.deliveryValidation?.status === 'PASS' ? 'accepted-measured' : 'candidate');
    rows.push({ id: object.id, type: object.category || 'item', status, support: object.furnitureValidation?.pointCount, residualP90: undefined, confidence: object.confidence, sources: [], warnings: [], node: null });
  }
  return rows;
}

function reviewStatusKey(status) {
  if (status === 'accepted-measured') return 'stMeasured';
  if (status === 'accepted-inferred') return 'stInferred';
  if (status === 'rejected') return 'stRejected';
  return 'stCandidate';
}

function reviewSourcesHtml(row) {
  const items = row.sources.length ? row.sources.map((source) => {
    const path = source.path || source.file || '';
    const isImage = /\.(png|jpe?g|webp|gif|bmp)$/i.test(path);
    const label = `${source.type || 'source'} · ${path}${source.note ? ` · ${source.note}` : ''}`;
    return isImage
      ? `<button type="button" data-review-preview="${encodeURIComponent(path)}">${label}</button>`
      : `<span class="src">${label}</span>`;
  }).join('') : `<span class="src">${t('reviewNoSources')}</span>`;
  return `<div class="review-sources"><p>${t('reviewEvidenceSources')}</p>${items}</div>`;
}

function reviewRowHtml(row) {
  const color = REVIEW_STATUS_COLORS[row.status] || '#8a949c';
  const metrics = [
    row.support !== undefined && row.support !== null ? `support ${row.support}` : '',
    Number.isFinite(row.residualP90) ? `P90 ${Number(row.residualP90).toFixed(3)}` : '',
    Number.isFinite(row.confidence) ? `conf ${Math.round(row.confidence * 100)}%` : '',
  ].filter(Boolean).join(' · ');
  const warn = row.warnings.length ? `<span class="review-warn">⚠ ${row.warnings.length} ${t('reviewWarning')}</span>` : '<span></span>';
  const expanded = reviewExpandedId === row.id ? reviewSourcesHtml(row) : '';
  return `
    <button type="button" class="review-row${reviewExpandedId === row.id ? ' active' : ''}" data-review-id="${row.id}" style="--status-color:${color}">
      <i></i><span><b>${row.id}</b><small>${metrics || '—'}</small></span>${warn}
    </button>${expanded}`;
}

function renderReviewPanel() {
  const panel = document.querySelector('#review-panel');
  if (!reviewPanelOpen || !sceneData) { panel.hidden = true; return; }
  const rows = reviewElements();
  const counts = {};
  rows.forEach((row) => { counts[row.status] = (counts[row.status] || 0) + 1; });
  const filtered = rows.filter((row) => (reviewFilter === 'candidate' ? row.status === 'candidate'
    : reviewFilter === 'warning' ? row.warnings.length > 0 : true));
  const groups = new Map();
  filtered.forEach((row) => {
    if (!groups.has(row.type)) groups.set(row.type, []);
    groups.get(row.type).push(row);
  });
  const stats = Object.keys(REVIEW_STATUS_COLORS)
    .map((status) => `<span><i style="background:${REVIEW_STATUS_COLORS[status]}"></i>${t(reviewStatusKey(status))} ${counts[status] || 0}</span>`)
    .join('');
  const filters = [['all', 'reviewAll'], ['candidate', 'reviewOnlyCandidate'], ['warning', 'reviewOnlyWarning']]
    .map(([key, label]) => `<button type="button" data-review-filter="${key}" class="${reviewFilter === key ? 'active' : ''}">${t(label)}</button>`)
    .join('');
  const body = [...groups.entries()]
    .sort((a, b) => a[0].localeCompare(b[0]))
    .map(([type, members]) => `
      <p class="review-group-title">${t(type) !== type ? t(type) : type} · ${members.length}</p>
      ${members.map((row) => reviewRowHtml(row)).join('')}
    `).join('') || `<p class="review-group-title">${t('reviewEmpty')}</p>`;
  panel.innerHTML = `
    <header>
      <p class="section-kicker">${t('reviewMode')}</p>
      <div class="review-stats">${stats}</div>
      <div class="review-filters">${filters}</div>
    </header>
    <div class="review-body">${body}</div>
  `;
  panel.hidden = false;
  canvas.dataset.reviewPanelRows = String(filtered.length);
  panel.querySelector('.review-row.active')?.scrollIntoView({ block: 'center' });
}

function clearReviewHighlight() {
  for (const node of reviewHighlightedMeshes) {
    if (node.material?.emissive && node.userData.reviewOriginalEmissive !== undefined) {
      node.material.emissive.setHex(node.userData.reviewOriginalEmissive);
      node.material.emissiveIntensity = node.userData.reviewOriginalEmissiveIntensity;
      if (node.userData.reviewOriginalOpacity !== undefined) node.material.opacity = node.userData.reviewOriginalOpacity;
      delete node.userData.reviewOriginalEmissive;
      delete node.userData.reviewOriginalOpacity;
    }
  }
  reviewHighlightedMeshes = [];
}

function highlightStructureMeshes(id) {
  clearReviewHighlight();
  for (const root of [wallGroup, candidateStructureGroup]) {
    root.traverse((child) => {
      const structure = child.userData.structure || child.userData.structureCandidate;
      const match = structure && (structure.id === id || structure.sourceId === id || String(structure.id).startsWith(`${id}::`));
      if (!match) return;
      child.traverse((node) => {
        if (!node.isMesh || !node.material?.emissive) return;
        if (node.userData.reviewOriginalEmissive === undefined) {
          node.userData.reviewOriginalEmissive = node.material.emissive.getHex();
          node.userData.reviewOriginalEmissiveIntensity = node.material.emissiveIntensity;
          node.userData.reviewOriginalOpacity = node.material.opacity;
        }
        node.material.emissive.setHex(0x23c483);
        node.material.emissiveIntensity = 0.85;
        // Candidate fills are nearly invisible (opacity 0.075); lift them so
        // the focused element actually reads as highlighted.
        if (node.material.transparent && node.material.opacity < 0.3) node.material.opacity = 0.35;
        reviewHighlightedMeshes.push(node);
      });
    });
  }
  return reviewHighlightedMeshes.length > 0;
}

function nodeDisplayAnchor(node) {
  if (!node) return null;
  const offset = displayOffsetOf();
  const toDisplay = (x, y, elevation) => new THREE.Vector3(x - offset[0], elevation, -y - offset[1]);
  if (node.hostOffsetM !== undefined && rawSceneV2) {
    const host = rawSceneV2.nodes?.[node.parentId];
    if (Array.isArray(host?.start) && Array.isArray(host?.end)) {
      const dx = host.end[0] - host.start[0];
      const dy = host.end[1] - host.start[1];
      const length = Math.hypot(dx, dy) || 1;
      const x = host.start[0] + (dx / length) * node.hostOffsetM;
      const y = host.start[1] + (dy / length) * node.hostOffsetM;
      return toDisplay(x, y, (host.baseHeight || 0) + (node.sillHeight || 0) + (node.height || 1) / 2);
    }
  }
  if (Array.isArray(node.start) && Array.isArray(node.end)) {
    // Raw V2 nodes carry plan [x, y]; compiled V1 segments carry display
    // [x, elevation, z] and must not be re-transformed.
    if (node.start.length === 3) {
      return new THREE.Vector3(
        (node.start[0] + node.end[0]) / 2,
        (node.baseHeight || 0) + (node.height || 2) / 2,
        (node.start[2] + node.end[2]) / 2,
      );
    }
    const base = (node.baseHeight || 0) + (node.sillHeight || 0);
    return toDisplay((node.start[0] + node.end[0]) / 2, (node.start[1] + node.end[1]) / 2, base + (node.height || 2) / 2);
  }
  if (Array.isArray(node.center)) {
    if (node.center.length === 3) {
      return new THREE.Vector3(node.center[0], node.center[1] + (node.size?.[1] || node.height || 1) / 2, node.center[2]);
    }
    return toDisplay(node.center[0], node.center[1], (node.elevation || node.baseHeight || 0) + (node.size?.[1] || node.height || 1) / 2);
  }
  if (Array.isArray(node.polygon) && node.polygon.length) {
    const cx = node.polygon.reduce((sum, point) => sum + point[0], 0) / node.polygon.length;
    const cy = node.polygon.reduce((sum, point) => sum + point[1], 0) / node.polygon.length;
    return toDisplay(cx, cy, node.elevation || 0);
  }
  return null;
}

function focusDisplayPoint(anchor) {
  if (photoAlign) exitPhotoAlign();
  clearControlMomentum();
  controls.target.copy(anchor);
  camera.position.copy(anchor.clone().add(new THREE.Vector3(3.6, 2.6, 4.0)));
  if (projectionMode === 'orthographic') {
    orthographicHalfHeight = 6;
    orthographicCamera.zoom = 1;
    applyOrthographicFrustum();
  }
  camera.lookAt(controls.target);
  controls.update();
  canvas.dataset.cameraPosition = camera.position.toArray().map((value) => value.toFixed(3)).join(',');
  canvas.dataset.cameraTarget = controls.target.toArray().map((value) => value.toFixed(3)).join(',');
}

function focusReviewElement(row) {
  reviewExpandedId = row.id;
  if (objectMeshes.has(row.id)) {
    clearReviewHighlight();
    if (photoAlign) exitPhotoAlign();
    selectObject(row.id, true);
  } else {
    const highlighted = highlightStructureMeshes(row.id);
    let anchor = null;
    if (highlighted) {
      const box = new THREE.Box3();
      for (const mesh of reviewHighlightedMeshes) box.expandByObject(mesh);
      anchor = box.getCenter(new THREE.Vector3());
    } else {
      anchor = nodeDisplayAnchor(row.node);
    }
    if (anchor) focusDisplayPoint(anchor);
  }
  renderReviewPanel();
}

function toggleReviewPanel(force) {
  reviewPanelOpen = force !== undefined ? force : !reviewPanelOpen;
  document.querySelector('#review-mode-toggle').classList.toggle('active', reviewPanelOpen);
  document.documentElement.dataset.reviewMode = reviewPanelOpen ? 'on' : 'off';
  if (!reviewPanelOpen) {
    clearReviewHighlight();
    reviewExpandedId = null;
  }
  renderReviewPanel();
}

function openReviewPreview(path) {
  const overlay = document.querySelector('#review-preview');
  overlay.innerHTML = `<figure style="margin:0"><img src="${resolveSceneAsset(path)}" alt="${path}"><figcaption>${path}</figcaption></figure>`;
  overlay.hidden = false;
}

function clearSelectionHighlight() {
  if (!selectedGroup) return;
  selectedGroup.traverse((child) => {
    if (child.isMesh && child.material?.emissive && child.userData.originalEmissive !== undefined) {
      child.material.emissive.setHex(child.userData.originalEmissive);
      child.material.emissiveIntensity = child.userData.originalEmissiveIntensity;
    }
  });
}

function selectObject(objectId, moveCamera = false) {
  const group = objectMeshes.get(objectId);
  if (!group) return;
  clearSelectionHighlight();
  selectedGroup = group;
  group.traverse((child) => {
    if (child.isMesh && child.material?.emissive) {
      child.userData.originalEmissive = child.material.emissive.getHex();
      child.userData.originalEmissiveIntensity = child.material.emissiveIntensity;
      child.material.emissive.setHex(0x23c483);
      child.material.emissiveIntensity = 0.36;
    }
  });
  const object = group.userData.sceneObject;
  document.querySelector('#selection-title').textContent = objectDisplayName(object);
  document.querySelector('#selection-details').innerHTML = `
    <div>${t('measuredSize')}<b>${object.size.map((value) => `${value.toFixed(2)} m`).join(' × ')}</b></div>
    <div>${t('localConfidence')}<b>${Math.round(object.confidence * 100)}%</b></div>
    <div>${t('supportPoints')}<b>${(object.furnitureValidation?.pointCount ?? 0).toLocaleString(currentLanguage)}</b></div>
    <div>${t('heightEvidence')}<b>${object.evidence?.heightSource ?? '—'}</b></div>
    <div>${t('evidenceFrame')}<b>${object.evidence?.nearestCameraId ?? '—'}</b></div>
    <div class="wide">${currentLanguage === 'en' ? 'Delivery Status' : '交付状态'}<b>${object.deliveryValidation?.status ?? 'REVIEW'} · pose ${object.deliveryValidation?.poseStatus ?? 'MISSING'} · clearance ${object.deliveryValidation?.clearanceStatus ?? 'MISSING'}</b><small>${object.deliveryValidation?.clearanceIssues?.map((item) => `${item.componentId} × ${item.structureId}`).join(' · ') || object.furnitureValidation?.blockers?.join(' · ') || 'evidence gates passed'}</small></div>
    <div class="wide">${currentLanguage === 'en' ? 'Raw Pose Refit' : '原始点云姿态复算'}<b>${object.furnitureValidation?.status ?? 'REVIEW'} · raw Δ ${object.furnitureValidation?.yawResidualDeg?.toFixed(1) ?? '—'}° · leave-one-out family Δ ${object.furnitureValidation?.localFamilyYawResidualDeg?.toFixed(1) ?? '—'}°</b><small>${object.furnitureValidation?.blockers?.join(' · ') || object.furnitureValidation?.independentSource || 'missing receipt'}</small></div>
    <div class="wide">${t('ruleEvidence')}<b>${object.evidence?.reason ?? object.furnitureValidation?.evidenceClass ?? '—'}</b></div>
    <div class="wide">${t('occlusionCompletion')}<b>${object.evidence?.completion || t('noCompletion')}</b></div>
    <div class="wide">${t('materialEvidence')}<b>${Object.values(object.material || {}).join(' · ') || t('unconfirmed')}</b></div>
    <div class="wide">${t('status')}<b>${localizedStatus(object.reviewState ?? object.deliveryValidation?.status ?? 'REVIEW')}</b><small>${object.id}</small></div>
  `;
  document.querySelectorAll('[data-object-id]').forEach((row) => row.classList.toggle('active', row.dataset.objectId === objectId));
  if (sceneData.photos.length) showPhoto(findClosestPreview(object));
  if (moveCamera) {
    const target = group.position.clone().add(new THREE.Vector3(0, Math.min(object.size[1] * 0.45, 1), 0));
    controls.target.copy(target);
    camera.position.copy(target.clone().add(new THREE.Vector3(3.8, 2.8, 4.2)));
  }
}

function setMode(mode) {
  if (photoAlign) exitPhotoAlign();
  document.querySelectorAll('[data-mode]').forEach((button) => button.classList.toggle('active', button.dataset.mode === mode));
  pointGroup.visible = mode !== 'model';
  photoMarkerGroup.visible = mode !== 'model';
  wallGroup.visible = mode !== 'raw';
  candidateStructureGroup.visible = mode === 'overlay';
  objectGroup.visible = mode !== 'raw';
  reviewObjectGroup.visible = mode === 'overlay';
  cameraGroup.visible = mode !== 'model';
  if (pointMaterial) pointMaterial.opacity = mode === 'overlay' ? 0.38 : 0.86;
  wallGroup.traverse((child) => {
    if (child.userData.ceilingDetail) child.visible = mode === 'model';
    if (!child.isMesh) return;
    if (child.userData.horizontalSurface) {
      const isCeiling = child.userData.structure?.category === 'ceiling-zone';
      const isFloor = child.userData.floorSurface === true;
      child.visible = mode !== 'raw' && !isCeiling;
      child.material.transparent = mode === 'overlay';
      child.material.opacity = isFloor ? (mode === 'overlay' ? 0.46 : 1) : (mode === 'overlay' ? 0.12 : 0.9);
      child.material.depthWrite = isFloor || mode !== 'overlay';
      child.material.needsUpdate = true;
    } else if (child.material.transparent) {
      child.material.opacity = mode === 'overlay' ? Math.min(child.material.opacity, 0.58) : 0.82;
    }
  });
}

function clearControlMomentum() {
  const damping = controls.enableDamping;
  controls.enableDamping = false;
  controls.update();
  controls.enableDamping = damping;
}

function updateProjectionButtons() {
  document.querySelectorAll('[data-projection]').forEach((button) => {
    button.classList.toggle('active', button.dataset.projection === projectionMode);
    button.setAttribute('aria-pressed', button.dataset.projection === projectionMode ? 'true' : 'false');
  });
  document.documentElement.dataset.currentProjection = projectionMode;
}

function applyOrthographicFrustum() {
  const aspect = Math.max(viewportWidth, 1) / Math.max(viewportHeight, 1);
  orthographicCamera.left = -orthographicHalfHeight * aspect;
  orthographicCamera.right = orthographicHalfHeight * aspect;
  orthographicCamera.top = orthographicHalfHeight;
  orthographicCamera.bottom = -orthographicHalfHeight;
  orthographicCamera.updateProjectionMatrix();
}

function replaceCamera(nextCamera, target, position) {
  const up = camera.up.clone();
  controls.dispose();
  camera = nextCamera;
  camera.up.copy(up);
  camera.position.copy(position);
  camera.lookAt(target);
  controls = new OrbitControls(camera, canvas);
  configureControls(controls, target);
  controls.update();
}

function setProjection(mode) {
  const nextMode = mode === 'orthographic' ? 'orthographic' : 'perspective';
  if (nextMode === projectionMode) return;
  if (photoAlign) return; // projection is dictated by the photo while aligned
  clearControlMomentum();
  const target = controls.target.clone();
  const direction = camera.position.clone().sub(target).normalize();
  if (nextMode === 'orthographic') {
    const distance = Math.max(0.5, camera.position.distanceTo(target));
    orthographicHalfHeight = Math.max(0.4, Math.tan(THREE.MathUtils.degToRad(perspectiveCamera.fov * 0.5)) * distance);
    orthographicCamera.zoom = 1;
    applyOrthographicFrustum();
    replaceCamera(orthographicCamera, target, camera.position.clone());
  } else {
    const visibleHalfHeight = orthographicHalfHeight / Math.max(orthographicCamera.zoom, 0.001);
    const distance = visibleHalfHeight / Math.tan(THREE.MathUtils.degToRad(perspectiveCamera.fov * 0.5));
    replaceCamera(perspectiveCamera, target, target.clone().add(direction.multiplyScalar(distance)));
  }
  projectionMode = nextMode;
  updateProjectionButtons();
}

function fitView(top = false) {
  if (photoAlign) exitPhotoAlign();
  const width = sceneData.focusEnvelope.width;
  const depth = sceneData.focusEnvelope.depth;
  const distance = Math.max(width, depth) * (top ? 0.95 : 0.78);
  clearControlMomentum();
  controls.target.set(0, 0.9, 0);
  if (top) {
    const verticalOffset = Math.cos(TOP_VIEW_TILT_RAD) * distance;
    const northOffset = Math.sin(TOP_VIEW_TILT_RAD) * distance;
    camera.position.copy(controls.target).add(new THREE.Vector3(0, verticalOffset, northOffset));
  } else {
    camera.position.set(distance * 0.58, distance * 0.42, distance * 0.68);
  }
  if (projectionMode === 'orthographic') {
    orthographicHalfHeight = Math.max(width, depth) * (top ? 0.54 : 0.48);
    orthographicCamera.zoom = 1;
    applyOrthographicFrustum();
  }
  camera.lookAt(controls.target);
  controls.update();
}

function isTypingTarget(target) {
  // Range sliders (photo opacity) don't capture text; Esc must keep working
  // right after dragging one.
  return target instanceof HTMLElement && Boolean(target.closest('input:not([type="range"]), textarea, select, [contenteditable="true"]'));
}

function updateKeyboardNavigation(deltaSeconds) {
  if (!activeNavigationKeys.size) return;
  const delta = Math.min(deltaSeconds, 0.05);
  const speed = activeNavigationKeys.has('ShiftLeft') || activeNavigationKeys.has('ShiftRight') ? 14 : 4.5;
  const viewDirection = controls.target.clone().sub(camera.position);
  const viewDistance = Math.max(viewDirection.length(), 0.5);
  const forward = viewDirection.clone();
  forward.y = 0;
  if (forward.lengthSq() < 1e-7) forward.set(0, 0, -1);
  forward.normalize();
  const right = new THREE.Vector3().crossVectors(forward, camera.up).normalize();
  const movement = new THREE.Vector3();
  if (activeNavigationKeys.has('KeyW')) movement.add(forward);
  if (activeNavigationKeys.has('KeyS')) movement.sub(forward);
  if (activeNavigationKeys.has('KeyD')) movement.add(right);
  if (activeNavigationKeys.has('KeyA')) movement.sub(right);
  if (activeNavigationKeys.has('KeyE') || activeNavigationKeys.has('PageUp')) movement.y += 1;
  if (activeNavigationKeys.has('KeyQ') || activeNavigationKeys.has('PageDown')) movement.y -= 1;
  if (movement.lengthSq() > 0) {
    movement.normalize().multiplyScalar(speed * delta);
    camera.position.add(movement);
    controls.target.add(movement);
  }

  const lookDirection = controls.target.clone().sub(camera.position);
  const yaw = (activeNavigationKeys.has('ArrowLeft') ? 1 : 0) - (activeNavigationKeys.has('ArrowRight') ? 1 : 0);
  const pitch = (activeNavigationKeys.has('ArrowUp') ? 1 : 0) - (activeNavigationKeys.has('ArrowDown') ? 1 : 0);
  const angularSpeed = (activeNavigationKeys.has('ShiftLeft') || activeNavigationKeys.has('ShiftRight')) ? 1.8 : 0.92;
  if (yaw) lookDirection.applyAxisAngle(camera.up, yaw * angularSpeed * delta);
  if (pitch) {
    const lookRight = new THREE.Vector3().crossVectors(lookDirection, camera.up).normalize();
    const candidate = lookDirection.clone().applyAxisAngle(lookRight, pitch * angularSpeed * delta);
    if (Math.abs(candidate.clone().normalize().y) < 0.96) lookDirection.copy(candidate);
  }
  if (yaw || pitch) controls.target.copy(camera.position).add(lookDirection.setLength(viewDistance));
  enforceCameraFloorClearance();
  controls.update();
  canvas.dataset.cameraPosition = camera.position.toArray().map((value) => value.toFixed(3)).join(',');
  canvas.dataset.cameraTarget = controls.target.toArray().map((value) => value.toFixed(3)).join(',');
}

function enforceCameraFloorClearance() {
  camera.position.y = Math.max(camera.position.y, MIN_CAMERA_HEIGHT_M);
  controls.target.y = Math.max(controls.target.y, MIN_TARGET_HEIGHT_M);
  canvas.dataset.cameraFloorClearance = (camera.position.y - MIN_CAMERA_HEIGHT_M).toFixed(3);
  canvas.dataset.cameraAboveFloor = camera.position.y >= MIN_CAMERA_HEIGHT_M ? 'true' : 'false';
}

const raycaster = new THREE.Raycaster();
const pointer = new THREE.Vector2();
let pointerDownAt = null;
canvas.addEventListener('pointerdown', (event) => { pointerDownAt = [event.clientX, event.clientY]; });
// Single click (not a drag) on a frame marker enters the photo-aligned view.
canvas.addEventListener('click', (event) => {
  if (photoAlign || !pointerDownAt) return;
  if (Math.hypot(event.clientX - pointerDownAt[0], event.clientY - pointerDownAt[1]) > 6) return;
  const rect = canvas.getBoundingClientRect();
  pointer.x = ((event.clientX - rect.left) / rect.width) * 2 - 1;
  pointer.y = -((event.clientY - rect.top) / rect.height) * 2 + 1;
  raycaster.setFromCamera(pointer, camera);
  const hit = raycaster.intersectObjects(photoMarkerGroup.children, true)
    .find((entry) => entry.object.isMesh && entry.object.userData.photoIndex !== undefined);
  if (hit) {
    showPhoto(hit.object.userData.photoIndex);
    enterPhotoAlign(hit.object.userData.photoIndex);
  }
});
canvas.addEventListener('dblclick', (event) => {
  const rect = canvas.getBoundingClientRect();
  pointer.x = ((event.clientX - rect.left) / rect.width) * 2 - 1;
  pointer.y = -((event.clientY - rect.top) / rect.height) * 2 + 1;
  raycaster.setFromCamera(pointer, camera);
  const hit = raycaster.intersectObjects([...objectGroup.children, ...reviewObjectGroup.children], true).find((entry) => entry.object.userData.owner);
  if (hit) selectObject(hit.object.userData.owner.userData.sceneObject.id, false);
});

document.addEventListener('click', (event) => {
  const language = event.target.closest('[data-language]');
  if (language) applyLanguage(language.dataset.language);
  const mode = event.target.closest('[data-mode]');
  if (mode) setMode(mode.dataset.mode);
  const projection = event.target.closest('[data-projection]');
  if (projection) setProjection(projection.dataset.projection);
  const category = event.target.closest('[data-category]');
  if (category) {
    activeCategory = category.dataset.category;
    document.querySelectorAll('[data-category]').forEach((button) => button.classList.toggle('active', button === category));
    [...objectGroup.children, ...reviewObjectGroup.children].forEach((group) => { group.visible = activeCategory === 'all' || group.userData.sceneObject.category === activeCategory; });
    renderObjectList();
  }
  const row = event.target.closest('[data-object-id]');
  if (row) selectObject(row.dataset.objectId, true);
  const photo = event.target.closest('[data-photo-index]');
  if (photo) showPhoto(Number(photo.dataset.photoIndex));
  const reviewToggle = event.target.closest('#review-mode-toggle');
  if (reviewToggle) toggleReviewPanel();
  const filterButton = event.target.closest('[data-review-filter]');
  if (filterButton) {
    reviewFilter = filterButton.dataset.reviewFilter;
    renderReviewPanel();
  }
  const previewSource = event.target.closest('[data-review-preview]');
  if (previewSource) {
    openReviewPreview(decodeURIComponent(previewSource.dataset.reviewPreview));
    return;
  }
  const reviewRow = event.target.closest('[data-review-id]');
  if (reviewRow) {
    const match = reviewElements().find((element) => element.id === reviewRow.dataset.reviewId);
    if (match) focusReviewElement(match);
  }
  const previewOverlay = event.target.closest('#review-preview');
  if (previewOverlay) previewOverlay.hidden = true;
  const alignEnter = event.target.closest('#photo-align-enter');
  if (alignEnter) enterPhotoAlign(Number(alignEnter.dataset.photoIndex));
  const alignExit = event.target.closest('#photo-align-exit');
  if (alignExit) exitPhotoAlign();
  const frustumToggle = event.target.closest('#frustum-toggle');
  if (frustumToggle) {
    photoMarkersShowAll = !photoMarkersShowAll;
    buildPhotoMarkers();
    updateFrustumToggle();
  }
});
document.querySelector('#photo-opacity').addEventListener('input', (event) => {
  applyPhotoOpacity(Number(event.target.value));
});
document.querySelector('#fit-view').addEventListener('click', () => fitView(false));
document.querySelector('#top-view').addEventListener('click', () => fitView(true));
canvas.addEventListener('pointerdown', () => canvas.focus({ preventScroll: true }));
window.addEventListener('keydown', (event) => {
  if (isTypingTarget(event.target)) return;
  if (photoAlign) {
    // Keyboard navigation would break the pose alignment; only Esc acts here.
    if (!event.repeat && event.code === 'Escape') exitPhotoAlign();
    return;
  }
  if (!event.repeat && event.code === 'KeyP') setProjection(projectionMode === 'perspective' ? 'orthographic' : 'perspective');
  if (!event.repeat && event.code === 'Home') fitView(false);
  if (!event.repeat && event.code === 'KeyT') fitView(true);
  if (!event.repeat && event.code === 'Digit1') setMode('raw');
  if (!event.repeat && event.code === 'Digit2') setMode('overlay');
  if (!event.repeat && event.code === 'Digit3') setMode('model');
  const navigationCodes = new Set(['KeyW', 'KeyA', 'KeyS', 'KeyD', 'KeyQ', 'KeyE', 'PageUp', 'PageDown', 'ArrowUp', 'ArrowDown', 'ArrowLeft', 'ArrowRight', 'ShiftLeft', 'ShiftRight']);
  if (navigationCodes.has(event.code)) {
    event.preventDefault();
    activeNavigationKeys.add(event.code);
    if (!event.repeat) updateKeyboardNavigation(1 / 30);
  }
});
window.addEventListener('keyup', (event) => activeNavigationKeys.delete(event.code));
window.addEventListener('blur', () => activeNavigationKeys.clear());

function resize() {
  const rect = canvas.getBoundingClientRect();
  const width = Math.max(1, Math.floor(rect.width));
  const height = Math.max(1, Math.floor(rect.height));
  if (viewportWidth !== width || viewportHeight !== height) {
    viewportWidth = width;
    viewportHeight = height;
    renderer.setSize(width, height, false);
    perspectiveCamera.aspect = width / height;
    perspectiveCamera.updateProjectionMatrix();
    applyOrthographicFrustum();
  }
}

function animate() {
  resize();
  if (!photoAlign) {
    updateKeyboardNavigation(navigationClock.getDelta());
    controls.update();
    enforceCameraFloorClearance();
  } else {
    navigationClock.getDelta();
    // Pinhole alignment keeps the camera pinned to the photo pose; the pano
    // sphere allows free look-around (and may sit below the walk floor limit).
    if (photoAlign.kind === 'equirect') controls.update();
  }
  renderer.render(scene3d, camera);
  requestAnimationFrame(animate);
}

function disposeGroupChildren(group) {
  for (const child of [...group.children]) {
    child.traverse?.((node) => {
      node.geometry?.dispose?.();
      const materials = Array.isArray(node.material) ? node.material : (node.material ? [node.material] : []);
      materials.forEach((material) => material.dispose?.());
    });
    group.remove(child);
  }
}

// The default fog density suits ~20 m interiors; large outdoor sites would
// otherwise disappear at their own fit distance.
function adaptFogToScene(data) {
  const span = Math.max(data.focusEnvelope?.width || 20, data.focusEnvelope?.depth || 20, 20);
  scene3d.fog.density = Math.min(0.017, 0.77 / span);
}

// Rebuilds the semantic model from a fresh view-model without touching the
// camera - the live-watch mode uses this so an agent editing the scene file
// gets an in-place updating view.
function rebuildScene(data) {
  const activePhotoIndex = photoAlign ? photoAlign.index : null;
  if (photoAlign) exitPhotoAlign();
  clearSelectionHighlight();
  reviewHighlightedMeshes = [];
  selectedGroup = null;
  for (const group of [wallGroup, candidateStructureGroup, objectGroup, reviewObjectGroup, cameraGroup]) {
    disposeGroupChildren(group);
  }
  objectMeshes.clear();
  floorSurfaceMeshes.length = 0;
  adaptFogToScene(data);
  buildGround(data.focusEnvelope);
  buildWalls(data.walls || []);
  buildStructures(data.structures || []);
  buildDerivedGeometry(data.derivedGeometry || []);
  buildCandidateStructures(data.structureCandidates || []);
  (data.objects || []).forEach(buildObject);
  buildCameraPath(data.cameraPath || []);
  renderPanels(data);
  buildPhotoMarkers();
  if (reviewPanelOpen) renderReviewPanel();
  if (activePhotoIndex !== null && data.photos?.[activePhotoIndex]?.transformMatrix) {
    enterPhotoAlign(activePhotoIndex);
  }
}

async function init() {
  try {
    applyLanguage(currentLanguage, false);
    const startup = new URLSearchParams(window.location.search);
    // ?scene=/outputs/foo/scene.json points the viewer at any scene file the
    // local server exposes; ?watch=2 polls it and hot-rebuilds on change.
    const scenePath = startup.get('scene') || './generated/scene.json';
    sceneBaseUrl = new URL(scenePath, window.location.href);
    const response = await fetch(scenePath, { cache: 'no-store' });
    if (!response.ok) throw new Error(`${t('sceneLoadFailed')} (${response.status})`);
    let sceneText = await response.text();
    sceneData = JSON.parse(sceneText);
    // Scene V2 authority graphs compile into the V1 view-model here; the
    // compile layer owns joinery, hosted-opening splits and frame mapping.
    // The raw graph is kept for the review panel (statuses + evidence).
    rawSceneV2 = isSceneV2(sceneData) ? sceneData : null;
    if (rawSceneV2) sceneData = compileSceneV2(rawSceneV2);
    adaptFogToScene(sceneData);
    buildGround(sceneData.focusEnvelope);
    buildWalls(sceneData.walls);
    buildStructures(sceneData.structures || []);
    buildDerivedGeometry(sceneData.derivedGeometry || []);
    buildCandidateStructures(sceneData.structureCandidates || []);
    sceneData.objects.forEach(buildObject);
    buildCameraPath(sceneData.cameraPath);
    const watchSeconds = Number(startup.get('watch') || 0);
    if (watchSeconds > 0) {
      setInterval(async () => {
        try {
          const poll = await fetch(scenePath, { cache: 'no-store' });
          if (!poll.ok) return;
          const text = await poll.text();
          if (text === sceneText) return;
          sceneText = text;
          let next = JSON.parse(text);
          rawSceneV2 = isSceneV2(next) ? next : null;
          if (rawSceneV2) next = compileSceneV2(rawSceneV2);
          sceneData = next;
          rebuildScene(sceneData);
          canvas.dataset.sceneRebuildAt = String(Date.now());
        } catch (error) {
          console.warn('watch rebuild skipped:', error.message);
        }
      }, Math.max(1, watchSeconds) * 1000);
    }
    const pointCloudArtifact = sceneData.artifacts?.pointCloud;
    if (pointCloudArtifact) {
      try {
        const sceneUrl = new URL(scenePath, window.location.href);
        const pointCloudUrl = new URL(pointCloudArtifact, sceneUrl).toString();
        await buildPointCloud(pointCloudUrl);
      } catch (error) {
        console.warn('Point-cloud artifact is unavailable; continuing in semantic-model mode.', error);
        sceneData.portableModelOnly = true;
      }
    } else {
      sceneData.portableModelOnly = true;
    }
    renderPanels(sceneData);
    buildPhotoMarkers();
    applyLanguage(currentLanguage, false);
    fitView(startup.get('view') === 'top');
    setMode(['raw', 'overlay', 'model'].includes(startup.get('mode')) ? startup.get('mode') : 'overlay');
    setProjection(startup.get('projection') === 'orthographic' ? 'orthographic' : 'perspective');
    updateProjectionButtons();
    loading.hidden = true;
    document.documentElement.dataset.demoReady = 'true';
    document.documentElement.dataset.keyboardNavigation = 'ready';
    canvas.dataset.cameraPosition = camera.position.toArray().map((value) => value.toFixed(3)).join(',');
    canvas.dataset.cameraTarget = controls.target.toArray().map((value) => value.toFixed(3)).join(',');
  } catch (error) {
    loading.textContent = error.message;
    loading.classList.add('error');
    console.error(error);
  }
}

animate();
init();
