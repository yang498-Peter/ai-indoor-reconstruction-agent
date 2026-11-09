import * as THREE from './vendor/three.module.js';
import { OrbitControls } from './vendor/addons/controls/OrbitControls.js';
import { GLTFLoader } from './vendor/addons/loaders/GLTFLoader.js';

const params=new URLSearchParams(location.search);
const file=params.get('file')||'',share=params.get('share')||'';
const status=document.getElementById('status'),viewport=document.getElementById('viewport');
const reset=document.getElementById('reset'),download=document.getElementById('download'),back=document.getElementById('back');
const validFile=/^[0-9a-f-]{36}$/.test(file),validShare=!share||/^[A-Za-z0-9_-]{20,128}$/.test(share);
const base=share?'/workspace/api/public/'+encodeURIComponent(share)+'/files/':'/workspace/api/files/';
if (share) { back.href='/workspace/public/'+encodeURIComponent(share);back.textContent='返回分享页'; }
if (validFile && validShare) download.href=base+encodeURIComponent(file)+'/download';
else { download.removeAttribute('href');status.textContent='查看链接无效，请返回工作空间。'; }

const limit=150*1000*1000;
let renderer,camera,controls,initialPosition,initialTarget;
function resize(){ if(!renderer)return;const width=viewport.clientWidth,height=viewport.clientHeight;renderer.setSize(width,height,false);camera.aspect=width/Math.max(height,1);camera.updateProjectionMatrix(); }
function embeddedOnly(buffer){
  if(buffer.byteLength<20)throw new Error('文件不是有效的 GLB 模型。');
  const view=new DataView(buffer);
  if(view.getUint32(0,true)!==0x46546c67||view.getUint32(4,true)!==2||view.getUint32(8,true)!==buffer.byteLength||view.getUint32(16,true)!==0x4e4f534a)
    throw new Error('文件不是有效的 GLB 模型。');
  const length=view.getUint32(12,true);
  if(length>10*1024*1024||20+length>buffer.byteLength)throw new Error('模型结构无法读取，请下载后查看。');
  let metadata;try{metadata=JSON.parse(new TextDecoder().decode(new Uint8Array(buffer,20,length)).trim());}
  catch{throw new Error('模型结构无法读取，请下载后查看。');}
  const pending=[metadata];
  while(pending.length){const item=pending.pop();if(!item||typeof item!=='object')continue;
    if(Object.prototype.hasOwnProperty.call(item,'uri'))throw new Error('模型引用了外部资源，在线预览暂不支持，请下载后查看。');
    for(const value of Object.values(item))if(value&&typeof value==='object')pending.push(value);
  }
}
async function readModel(){
  const response=await fetch(base+encodeURIComponent(file)+'/preview',{credentials:share?'omit':'same-origin',cache:'no-store'});
  if(!response.ok){let message='无法打开模型，请返回后重试。';try{const data=await response.json();message=data.error?.message||message;}catch{}throw new Error(message);}
  const length=Number(response.headers.get('Content-Length'));
  if(!Number.isSafeInteger(length)||length<=0||length>limit){response.body?.cancel();throw new Error('在线预览支持 150 MB 以内的 GLB 模型，请下载完整文件查看。');}
  const bytes=new Uint8Array(length);let position=0;
  if(response.body?.getReader){const reader=response.body.getReader();while(true){const {done,value}=await reader.read();if(done)break;if(position+value.length>length)throw new Error('模型数据不完整，请重试。');bytes.set(value,position);position+=value.length;status.textContent='正在读取模型 '+Math.floor(position/length*100)+'%';}}
  else {const value=new Uint8Array(await response.arrayBuffer());bytes.set(value);position=value.length;}
  if(position!==length)throw new Error('模型数据不完整，请重试。');
  embeddedOnly(bytes.buffer);return bytes.buffer;
}
async function start(){
  try{
    const buffer=await readModel();status.textContent='正在打开三维模型…';
    const gltf=await new Promise((resolve,reject)=>new GLTFLoader().parse(buffer,'',resolve,reject));
    const box=new THREE.Box3().setFromObject(gltf.scene);
    if(box.isEmpty())throw new Error('模型中没有可显示的三维内容。');
    const center=box.getCenter(new THREE.Vector3()),size=box.getSize(new THREE.Vector3());
    const extent=Math.max(size.x,size.y,size.z,0.01);
    gltf.scene.position.sub(center);
    const scene=new THREE.Scene();scene.background=new THREE.Color(0xe8eeea);scene.add(gltf.scene);
    scene.add(new THREE.HemisphereLight(0xffffff,0x98a99d,2.2));
    const light=new THREE.DirectionalLight(0xffffff,2);light.position.set(2,4,3);scene.add(light);
    renderer=new THREE.WebGLRenderer({antialias:true});renderer.setPixelRatio(Math.min(devicePixelRatio||1,2));
    renderer.outputColorSpace=THREE.SRGBColorSpace;viewport.append(renderer.domElement);
    camera=new THREE.PerspectiveCamera(45,1,Math.max(extent/10000,0.001),Math.max(extent*100,100));
    camera.position.set(extent*1.3,extent*.9,extent*1.4);
    controls=new OrbitControls(camera,renderer.domElement);controls.target.set(0,0,0);controls.enableDamping=true;controls.update();
    initialPosition=camera.position.clone();initialTarget=controls.target.clone();
    reset.disabled=false;resize();new ResizeObserver(resize).observe(viewport);
    renderer.setAnimationLoop(()=>{controls.update();renderer.render(scene,camera);});
    status.hidden=true;
  }catch(error){status.textContent=error?.message||'模型暂时无法预览，请下载后查看。';}
}
reset.addEventListener('click',()=>{if(!controls)return;camera.position.copy(initialPosition);controls.target.copy(initialTarget);controls.update();});
if(validFile&&validShare)start();
