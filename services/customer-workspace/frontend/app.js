/* Customer workspace: the server owns quota, authorization and task state. */
(() => {
  'use strict';

  const API = '/workspace/api/';
  const RAW_LIMIT = 5_000_000_000;
  const $ = id => document.getElementById(id);
  const folderSupported = 'webkitdirectory' in $('folder-input');
  const state = { user: null, csrf: '', quota: null, config: null, project: null, detail: null, draft: null, busy: false, controller: null, passwordRequired: false };
  const memoryKeys = new Map();
  const optionNames = {pointcloud:'点云', model:'三维模型', cad:'CAD', ifc:'BIM · IFC', sketchup:'SketchUp'};
  const statusNames = {draft:'草稿', uploading:'上传中', submitted:'待处理', processing:'人工处理中', needs_info:'需补充信息', delivered:'已交付'};

  function node(tag, className, text) {
    const result = document.createElement(tag);
    if (className) result.className = className;
    if (text !== undefined) result.textContent = text;
    return result;
  }
  function bytes(value) {
    const n = Number(value) || 0;
    if (n >= 1e9) return (n / 1e9).toFixed(2).replace(/\.00$/, '') + ' GB';
    if (n >= 1e6) return (n / 1e6).toFixed(1).replace(/\.0$/, '') + ' MB';
    if (n >= 1e3) return (n / 1e3).toFixed(1).replace(/\.0$/, '') + ' KB';
    return n + ' B';
  }
  function date(value) {
    const result = new Date(value);
    return Number.isNaN(result.getTime()) ? '' : new Intl.DateTimeFormat('zh-CN', {year:'numeric',month:'2-digit',day:'2-digit'}).format(result);
  }
  function timestamp(value) {
    const result=new Date(value);
    return Number.isNaN(result.getTime()) ? '' : new Intl.DateTimeFormat('zh-CN',{year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'}).format(result);
  }
  function showMessage(id, message) { const target=$(id); target.textContent=message || ''; target.hidden=!message; }
  function notice(message, error=false) {
    const target=$('workspace-notice'); target.textContent=message || ''; target.hidden=!message; target.classList.toggle('error',error);
  }
  function errorMessage(error) {
    if (error?.name === 'AbortError') return '上传已暂停。可以稍后选择同一文件夹继续。';
    if (error?.message) return error.message;
    return '操作未完成，请检查网络后重试。';
  }
  async function request(path, options={}) {
    const method = options.method || 'GET';
    const headers = new Headers(options.headers || {});
    if (method !== 'GET' && state.csrf) headers.set('X-CSRF-Token', state.csrf);
    let body=options.body;
    if (options.json !== undefined) { headers.set('Content-Type','application/json'); body=JSON.stringify(options.json); }
    let response, data={}, timedOut=false;
    const controller=new AbortController(), caller=options.signal;
    const forward=()=>controller.abort();
    if (caller?.aborted) controller.abort(); else caller?.addEventListener('abort',forward,{once:true});
    const timeout=path.endsWith('/complete') ? 20*60_000 : path.includes('/chunks/') ? 5*60_000 : 30_000;
    const timer=setTimeout(()=>{timedOut=true;controller.abort();},timeout);
    try {
      response=await fetch(API + path, {method,headers,body,credentials:'same-origin',signal:controller.signal,cache:'no-store'});
      try { data=await response.json(); } catch(error) { if (error.name==='AbortError') throw error; /* A server error need not have a JSON body. */ }
    } catch (error) {
      if (timedOut) throw new Error('连接超时，请检查网络后重试。');
      if (error.name === 'AbortError') throw error;
      throw new Error('网络连接中断，请检查网络后重试。');
    } finally { clearTimeout(timer); caller?.removeEventListener('abort',forward); }
    if (!response.ok) {
      const error=new Error(typeof data.error?.message === 'string' ? data.error.message : '操作未完成，请稍后重试。');
      error.status=response.status; error.code=data.error?.code;
      if (response.status===401 && state.user && !path.startsWith('auth/')) {
        state.controller?.abort(); if ($('new-dialog').open) $('new-dialog').close(); showAuth(); showMessage('auth-error','登录已过期，请重新登录。');
      }
      if (error.code==='MUST_CHANGE_PASSWORD' && state.user) {
        state.controller?.abort(); if ($('new-dialog').open) $('new-dialog').close(); if ($('delete-dialog').open) $('delete-dialog').close(); openPassword(true);
      }
      throw error;
    }
    return data;
  }
  async function hash(buffer) {
    if (!globalThis.crypto?.subtle) throw new Error('当前浏览器不支持安全分片上传，请使用新版浏览器并通过 HTTPS 访问。');
    const digest=await crypto.subtle.digest('SHA-256',buffer);
    return [...new Uint8Array(digest)].map(x=>x.toString(16).padStart(2,'0')).join('');
  }
  async function stableKey(purpose, body) {
    const digest=await hash(new TextEncoder().encode(JSON.stringify(body)));
    const name='tersus.workspace.idempotency.' + state.user.id + '.' + purpose + '.' + digest;
    let value=memoryKeys.get(name);
    try { value=localStorage.getItem(name) || value; } catch { /* In-memory retry is still available. */ }
    if (!value) {
      if (!crypto.randomUUID) throw new Error('当前浏览器版本较旧，请更新浏览器后重试。');
      value=crypto.randomUUID(); memoryKeys.set(name,value);
      try { localStorage.setItem(name,value); } catch { /* Storage may be disabled. */ }
    }
    return {name,value};
  }
  function forgetKey(key) { memoryKeys.delete(key.name); try { localStorage.removeItem(key.name); } catch {} }
  async function createIdempotently(path, purpose, body, signal) {
    const key=await stableKey(purpose,body);
    const result=await request(path,{method:'POST',json:body,signal,headers:{'Idempotency-Key':key.value}});
    forgetKey(key);
    return result;
  }
  function setAuthMode(mode) {
    const register=mode==='register';
    $('login-tab').setAttribute('aria-selected',String(!register)); $('register-tab').setAttribute('aria-selected',String(register));
    $('auth-heading').textContent=register ? '创建您的工作空间' : '欢迎回来';
    $('auth-description').textContent=register ? '创建账户后即可上传扫描数据。' : '登录后查看项目和交付成果。';
    $('auth-submit').textContent=register ? '创建账户' : '登录工作台';
    $('confirm-wrap').hidden=!register; $('confirm-password').required=register;
    $('username-hint').hidden=!register; $('username').minLength=register ? 3 : 0;
    $('password').minLength=register ? 10 : 0;
    $('password').placeholder=register ? '至少 10 位密码' : '输入密码';
    $('password').autocomplete=register ? 'new-password' : 'current-password';
    $('auth-form').dataset.mode=mode; showMessage('auth-error','');
  }
  function showAuth() {
    state.user=null; state.csrf=''; state.config=null; $('initial-view').hidden=true; $('workspace-view').hidden=true; $('account-actions').hidden=true; $('auth-view').hidden=false;
    setAuthMode('login');
  }
  async function getConfig(refresh=false,signal) {
    if (state.config && !refresh) return state.config;
    const config=await request('config',{signal});
    if (!Number.isSafeInteger(config.chunk_size) || !Number.isSafeInteger(config.max_chunk_size) || config.chunk_size<1 || config.chunk_size>config.max_chunk_size || config.max_chunk_size>32*1024*1024 || !Number.isSafeInteger(config.dataset_limit) || config.dataset_limit<1) throw new Error('上传配置暂不可用，请稍后重试。');
    state.config=config; return config;
  }
  function renderQuota(quota) {
    if (!quota) return;
    state.quota=quota;
    const used=Math.max(0,Number(quota.used)||0), reserved=Math.max(0,Number(quota.reserved)||0), limit=Number(quota.limit)||20_000_000_000;
    $('storage-used').textContent='已使用 ' + bytes(used) + (reserved ? ' · 上传预留 ' + bytes(reserved) : '');
    $('storage-limit').textContent=bytes(limit);
    const percentage=Math.max(0,Math.min(100,(used+reserved)*100/limit));
    $('storage-progress').setAttribute('aria-valuenow',String(Math.round(percentage)));
    $('storage-progress').setAttribute('aria-valuetext',`已使用 ${bytes(used)}，上传预留 ${bytes(reserved)}，总容量 ${bytes(limit)}`);
    $('storage-progress').firstElementChild.style.width=percentage+'%';
  }
  async function setUser(data) {
    state.user=data.user; state.csrf=data.csrf_token; state.quota=data.quota;
    $('initial-view').hidden=true; $('auth-view').hidden=true; $('workspace-view').hidden=false; $('account-actions').hidden=false;
    $('account-name').textContent=state.user.username; renderQuota(data.quota);
    if (state.user.role==='admin' && !$('admin-link')) {
      const link=node('a','header-link','管理后台 ↗'); link.id='admin-link'; link.href='/workspace/admin/'; $('account-actions').prepend(link);
    }
    $('overview-view').hidden=false; $('detail-view').hidden=true;
    window.scrollTo({top:0,behavior:'instant'});
    if (state.user.must_change_password) { openPassword(true); return; }
    try { await getConfig(); } catch { notice('暂时无法获取上传配置，请稍后刷新重试。',true); }
    await loadProjects();
    if (location.hash==='#password') { history.replaceState(null,'',location.pathname+location.search); openPassword(false); }
  }
  function statusBadge(status) {
    const el=node('span','status',statusNames[status] || '状态待确认');
    if (status==='delivered') el.classList.add('is-ready');
    if (status==='needs_info') el.classList.add('is-error');
    if (status==='uploading' || status==='processing') el.classList.add('is-progress');
    return el;
  }
  async function loadProjects() {
    const list=$('projects-list'); list.replaceChildren(node('div','loading-state','正在读取任务…'));
    try {
      const data=await request('projects'); renderQuota(data.quota); const projects=Array.isArray(data.projects) ? data.projects : [];
      $('project-count').textContent=projects.length ? `· ${projects.length}` : ''; list.replaceChildren();
      if (!projects.length) {
        const empty=node('div','empty-state'); empty.append(node('span','empty-icon','＋'),node('h3','','还没有任务'),node('p','','创建第一个任务，上传 S1 原始数据。'));
        const button=node('button','button primary','创建任务'); button.type='button'; button.addEventListener('click',openNew); empty.append(button); list.append(empty); return;
      }
      for (const project of projects) {
        const button=node('button','project-row'); button.type='button';
        const title=node('span','project-name'); title.append(node('span','project-symbol','◇'),node('strong','',project.name || '未命名任务'));
        const meta=node('span','project-meta',date(project.created_at) ? '创建于 ' + date(project.created_at) : '');
        const badge=statusBadge(project.status); const arrow=node('span','row-arrow','↗'); arrow.setAttribute('aria-hidden','true');
        button.append(title,meta,badge,arrow); button.addEventListener('click',()=>openProject(project.id)); list.append(button);
      }
    } catch (error) { list.replaceChildren(node('div','loading-state',errorMessage(error))); }
  }
  function resultPreview(file, parent) {
    const path=String(file.path || '').toLowerCase();
    const url=API+'files/'+encodeURIComponent(file.id)+'/preview';
    if (/\.(png|jpe?g|webp|gif)$/.test(path)) {
      const image=document.createElement('img'); image.className='result-preview'; image.src=url; image.alt='成果图片预览'; image.loading='lazy';
      image.addEventListener('error',()=>image.remove()); parent.append(image);
    }
    if (/\.pdf$/.test(path)) {
      const preview=node('a','result-preview-link','打开 PDF ↗'); preview.href=url; preview.target='_blank'; preview.rel='noopener noreferrer'; parent.append(preview);
    }
    if (/\.glb$/.test(path)) {
      const preview=node('a','result-model-link','在线查看三维模型 ↗'); preview.href='/workspace/viewer/?file='+encodeURIComponent(file.id); preview.target='_blank'; preview.rel='noopener noreferrer'; parent.append(preview);
    }
  }
  function renderResults(files) {
    const list=$('results-list'); list.replaceChildren();
    if (!files.length) { list.append(node('div','results-empty','成果尚未交付，处理团队完成后会上传至此。')); return; }
    for (const file of files) {
      if (!file?.id) continue;
      const item=node('div','result-card'); resultPreview(file,item);
      const row=node('div','result-item'); const info=node('span');
      info.append(node('strong','',file.path || '交付文件'),node('small','',bytes(file.size)));
      const link=node('a','result-download','下载 ↗'); link.href=API+'files/'+encodeURIComponent(file.id)+'/download'; link.setAttribute('download','');
      row.append(info,link); item.append(row); list.append(item);
    }
  }
  function renderMessages(messages) {
    const list=$('messages-list'); list.replaceChildren();
    if (!messages.length) { list.append(node('div','results-empty','暂无留言。')); return; }
    for (const message of messages) {
      const card=node('article','message-card'+(message.author==='user'?' is-user':''));
      const meta=node('div','message-meta'); meta.append(node('strong','',message.author==='admin'?'处理团队':'我'),node('span','',timestamp(message.created_at)));
      card.append(meta,node('p','message-body',message.text || '')); list.append(card);
    }
  }
  function renderProject(detail, scroll=true) {
    const project=detail.project; state.project=project; state.detail=detail; renderQuota(detail.quota);
    $('overview-view').hidden=true; $('detail-view').hidden=false;
    $('detail-title').textContent=project.name || '未命名任务';
    $('detail-subtitle').textContent=(date(project.created_at) ? '创建于 ' + date(project.created_at) + ' · ' : '') + (project.is_public ? '成果已公开' : '私有任务');
    $('detail-status').replaceWith(Object.assign(statusBadge(project.status),{id:'detail-status'}));
    const tags=$('detail-options'); tags.replaceChildren();
    for (const option of project.options || []) tags.append(node('span','',optionNames[option] || String(option)));
    $('detail-note').textContent=project.note || '没有补充说明。';
    renderResults(Array.isArray(detail.result_files) ? detail.result_files : []);
    const messages=Array.isArray(detail.messages) ? [...detail.messages] : [];
    if (project.message && !messages.some(message=>message.author==='admin' && message.text===project.message)) messages.push({author:'admin',text:project.message,created_at:project.updated_at});
    renderMessages(messages);
    const uploads=Array.isArray(detail.uploads) ? detail.uploads : [];
    const active=uploads.find(upload=>upload.kind==='raw' && ['uploading','open','active','pending'].includes(upload.status));
    const raw=Array.isArray(detail.raw_files) ? detail.raw_files : [], completedRaw=raw.filter(file=>file.state==='complete');
    const target=$('upload-details'); target.replaceChildren();
    if (active) {
      target.append(node('p','muted','数据上传尚未完成，可选择原文件夹继续。'));
      const resume=node('button','button secondary','继续上传'); resume.type='button'; resume.addEventListener('click',()=>openResume(project,active,completedRaw)); target.append(resume);
      const cancel=node('button','text-button cancel-upload','取消上传并释放预留空间'); cancel.type='button'; cancel.addEventListener('click',()=>cancelUpload(active.id)); target.append(cancel);
    } else if (completedRaw.length) {
      target.append(node('p','muted',`已上传 ${completedRaw.length} 个文件 · ${bytes(completedRaw.reduce((n,f)=>n+(Number(f.size)||0),0))}`));
      if (project.status==='needs_info') {
        target.append(node('p','field-hint','请准备仅包含未上传文件的文件夹，不能覆盖已有文件；全部原始数据合计不超过 5 GB。'));
        const extra=node('button','button secondary','补充原始文件夹'); extra.type='button'; extra.addEventListener('click',()=>openResume(project,null,completedRaw)); target.append(extra);
      }
    } else if (project.status==='draft' || project.status==='uploading') {
      target.append(node('p','muted','尚未上传原始扫描数据。'));
      const resume=node('button','button secondary','选择文件夹上传'); resume.type='button'; resume.addEventListener('click',()=>openResume(project,null)); target.append(resume);
    } else target.append(node('p','muted','暂无可显示的原始文件信息。'));
    $('public-toggle').indeterminate=false; $('public-toggle').checked=!!project.is_public;
    $('public-toggle').disabled=project.status!=='delivered' || !detail.result_files?.length;
    $('public-description').textContent=project.is_public ? '已公开，可通过链接查看成果' : '仅自己和处理团队可见';
    const publicUrl=typeof project.public_url==='string' && /^\/workspace\/public\/[A-Za-z0-9_-]+\/?$/.test(project.public_url) ? project.public_url : '';
    $('public-link-wrap').hidden=!project.is_public || !publicUrl;
    if (publicUrl) $('public-link').href=publicUrl; else $('public-link').removeAttribute('href');
    showMessage('public-error','');
    if (project.is_public && !publicUrl) showMessage('public-error','公开链接暂不可用，请刷新或联系处理团队。');
    if (scroll) window.scrollTo({top:0,behavior:'instant'});
  }
  async function openProject(id) {
    notice('');
    if (state.project?.id!==id) { $('message-text').value=''; showMessage('message-error',''); }
    try { renderProject(await request('projects/'+encodeURIComponent(id))); }
    catch(error) { notice(errorMessage(error),true); }
  }
  async function cancelUpload(id) {
    if (!window.confirm('取消后，已上传的分片将被删除，并释放预留空间。确定取消吗？')) return;
    try { const result=await request('uploads/'+encodeURIComponent(id)+'/cancel',{method:'POST'}); await openProject(state.project.id); notice(result.storage_cleanup_pending ? '上传已取消，服务器空间正在清理。' : '上传已取消，预留空间已释放。'); }
    catch(error) { notice(errorMessage(error),true); }
  }
  function lockProjectFields(locked) {
    $('project-name').disabled=locked; $('project-note').disabled=locked;
    $('deliverables-fieldset').hidden=locked; $('project-note-wrap').hidden=locked;
    document.querySelectorAll('input[name="deliverable"]').forEach(input=>{ input.disabled=locked; });
  }
  function resetDialog() {
    $('new-form').reset(); $('folder-summary').textContent=''; $('folder-title').textContent='选择 S1 原始数据文件夹';
    $('folder-description').textContent=folderSupported ? '保留目录结构，无需压缩 · 单次最多 5 GB' : '当前浏览器不支持选择文件夹，请使用新版桌面浏览器。';
    $('upload-progress-box').hidden=true; $('upload-bar').style.width='0%'; $('upload-percent').textContent='0%';
    $('pause-upload').hidden=true; $('submit-project').disabled=false; $('choose-folder').disabled=!folderSupported;
    showMessage('new-error',''); lockProjectFields(false);
  }
  function openNew() {
    resetDialog(); state.draft={project:null,upload:null,files:[],manifest:null,isSupplement:false,existingRaw:[]};
    $('new-title').textContent='创建扫描任务'; $('new-description').textContent='选择 S1 原始数据文件夹和需要的成果。'; $('new-footer-hint').textContent='创建后默认私有，可稍后设置成果公开。'; $('submit-project').textContent='创建并上传 ↗';
    $('new-dialog').showModal(); $('project-name').focus();
  }
  function openResume(project, upload, existingRaw=[]) {
    resetDialog(); state.draft={project,upload,files:[],manifest:null,isSupplement:existingRaw.length>0,existingRaw};
    $('new-title').textContent=upload ? '继续上传扫描数据' : state.draft.isSupplement ? '补充原始文件夹' : '上传扫描数据';
    $('new-description').textContent=state.draft.isSupplement ? '仅选择尚未上传的补充文件，不会覆盖已有数据。' : '选择 S1 原始数据文件夹和需要的成果。';
    $('new-footer-hint').textContent='原始数据仅自己和处理团队可见。';
    if (state.draft.isSupplement) $('folder-title').textContent='选择需要补充的文件夹';
    $('project-name').value=project.name || ''; $('project-note').value=project.note || '';
    document.querySelectorAll('input[name="deliverable"]').forEach(input=>{ input.checked=(project.options||[]).includes(input.value); });
    lockProjectFields(true); $('submit-project').textContent='选择文件夹后继续 ↗';
    $('folder-description').textContent=upload ? '请选择与上次完全相同的原始文件夹' : state.draft.isSupplement ? '仅选择未上传文件 · 项目原始数据合计最多 5 GB' : '选择 S1 原始数据文件夹 · 单次最多 5 GB';
    $('new-dialog').showModal(); $('choose-folder').focus();
  }
  function selectedFolder(files) {
    const rows=Array.from(files).map(file=>({file,path:file.webkitRelativePath || file.name,size:file.size,last_modified:file.lastModified})).sort((a,b)=>a.path<b.path ? -1 : a.path>b.path ? 1 : 0);
    if (!rows.length) throw new Error('请选择包含文件的 S1 原始数据文件夹。');
    if (rows.length>100_000) throw new Error('文件数量超过 100,000 个，请联系处理团队。');
    const encoder=new TextEncoder();
    const invalid=rows.find(row=>!row.path || encoder.encode(row.path).length>2048 || /[\x00-\x1f\x7f\\]/.test(row.path) || row.path.split('/').some(part=>!part || part==='.' || part==='..' || encoder.encode(part).length>255 || /[<>:"|?*]/.test(part) || /[ .]$/.test(part) || /^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?$/i.test(part)));
    if (invalid) throw new Error(`文件夹中包含无法上传的路径：${invalid.path}。请检查后重新选择。`);
    const keys=rows.map(row=>row.path.normalize('NFC').toLowerCase()).sort();
    if (keys.some((value,index)=>index>0 && (value===keys[index-1] || value.startsWith(keys[index-1]+'/')))) throw new Error('文件夹中有重复或冲突的相对路径，请检查后重新选择。');
    const total=rows.reduce((sum,row)=>sum+row.size,0);
    if (total<=0) throw new Error('文件夹中没有可上传的数据。');
    const limit=state.config?.dataset_limit || RAW_LIMIT;
    if (total>limit) throw new Error(`所选文件夹为 ${bytes(total)}，超过单个任务 ${bytes(limit)} 的限制。`);
    return {rows,total};
  }
  function s1Hint(rows) {
    const names=rows.map(row=>row.path.toLowerCase());
    const metadata=names.some(path=>/(^|\/)metadata\.ya?ml$/.test(path));
    const capture=names.some(path=>/(^|\/)(?:data\/)?(?:data_raw|data)\.mcap$/.test(path));
    return metadata && capture ? '已发现常见 S1 关键文件，完整性仍需处理团队确认。' : '未发现常见 S1 文件，请确认目录。';
  }
  function projectBody() {
    return {name:$('project-name').value.trim(),options:[...document.querySelectorAll('input[name="deliverable"]:checked')].map(input=>input.value),note:$('project-note').value.trim()};
  }
  function validateSupplement(selected,existing,limit) {
    const previous=existing.reduce((sum,file)=>sum+(Number(file.size)||0),0);
    if (previous+selected.total>limit) throw new Error(`原始数据合计将达到 ${bytes(previous+selected.total)}，超过项目 ${bytes(limit)} 的限制。`);
    const paths=existing.map(file=>file.path.normalize('NFC').toLowerCase()).sort();
    const chosen=selected.rows.map(row=>({path:row.path,key:row.path.normalize('NFC').toLowerCase()})).sort((a,b)=>a.key<b.key?-1:a.key>b.key?1:0);
    let i=0,j=0;
    while (i<paths.length && j<chosen.length) {
      const a=paths[i],b=chosen[j].key;
      if (a===b || a.startsWith(b+'/') || b.startsWith(a+'/')) throw new Error(`文件路径与已有数据冲突：${chosen[j].path}。请只选择尚未上传的补充文件。`);
      if (a<b) i++; else j++;
    }
  }
  function setUploadProgress(stage,done,total,extra='') {
    const percent=total ? Math.min(100,Math.floor(done*100/total)) : 0;
    $('upload-progress-box').hidden=false; $('upload-stage').textContent=stage; $('upload-percent').textContent=percent+'%';
    $('upload-bar').style.width=percent+'%'; $('upload-progress-text').textContent=bytes(done)+' / '+bytes(total)+(extra ? ' · '+extra : '');
  }
  function validSelection(session, selected) {
    const chosen=new Map(selected.rows.map(row=>[row.path,row]));
    if (chosen.size!==session.files.length) throw new Error('所选文件夹与上次上传的文件数量不同，请重新选择原文件夹。');
    for (const file of session.files) {
      const row=chosen.get(file.path);
      if (!row || row.size!==Number(file.size)) throw new Error(`文件不匹配：${file.path}。请选择上次上传的原文件夹。`);
      const expected=file.expected_hashes;
      if (!Array.isArray(expected) || expected.length!==Math.ceil(row.size/session.chunk_size) || expected.some(item=>typeof item!=='string' || !/^[a-f0-9]{64}$/.test(item))) throw new Error(`无法确认文件校验清单：${file.path}。请联系处理团队。`);
    }
    return chosen;
  }
  async function chunkHash(file, index, chunkSize) {
    const start=index*chunkSize; const buffer=await file.slice(start,Math.min(file.size,start+chunkSize)).arrayBuffer();
    return {buffer,digest:await hash(buffer)};
  }
  async function prepareManifest(selected,signal,chunkSize) {
    const files=selected.rows.map(({path,size,last_modified})=>({path,size,last_modified,expected_hashes:[]}));
    const queue=[];
    for (let i=0;i<files.length;i++) for (let index=0;index<Math.ceil(files[i].size/chunkSize);index++) queue.push({i,index});
    let next=0, checked=0, firstError=null;
    setUploadProgress('正在校验本地数据',0,selected.total,'尚未开始上传 · 可暂停');
    async function worker() {
      try {
        while (next<queue.length && !signal.aborted) {
          const task=queue[next++], row=selected.rows[task.i];
          const result=await chunkHash(row.file,task.index,chunkSize);
          if (signal.aborted) throw new DOMException('Paused','AbortError');
          files[task.i].expected_hashes[task.index]=result.digest; checked+=result.buffer.byteLength;
          setUploadProgress('正在校验本地数据',checked,selected.total,'尚未开始上传 · 可暂停');
        }
      } catch(error) { if (!firstError) firstError=error; state.controller?.abort(); }
    }
    await Promise.all(Array.from({length:Math.min(2,Math.max(queue.length,1))},worker));
    if (firstError) throw firstError;
    if (signal.aborted) throw new DOMException('Paused','AbortError');
    return {kind:'raw',chunk_size:chunkSize,files};
  }
  async function verifyExisting(session,chosen,total,signal) {
    let checked=0, existing=0;
    for (const file of session.files) {
      const row=chosen.get(file.path), indexes=Array.isArray(file.uploaded_chunks) ? file.uploaded_chunks : [];
      if (new Set(indexes).size!==indexes.length) throw new Error('上传会话分片状态异常，请联系处理团队。');
      for (const index of indexes) {
        if (signal.aborted) throw new DOMException('Paused','AbortError');
        if (!Number.isSafeInteger(index) || index<0 || index>=Math.ceil(row.size/session.chunk_size)) throw new Error('上传会话分片状态异常，请联系处理团队。');
        const expected=file.expected_hashes?.[index], uploaded=file.chunk_hashes?.[String(index)];
        if (!expected || !uploaded || uploaded!==expected) throw new Error('无法校验已上传分片，请联系处理团队或取消后重新上传。');
        const result=await chunkHash(row.file,index,session.chunk_size);
        if (result.digest!==String(expected).toLowerCase()) throw new Error(`文件内容与上次上传不一致：${file.path}。请取消该上传并重新选择数据。`);
        checked+=result.buffer.byteLength; existing+=result.buffer.byteLength;
        setUploadProgress('正在核对已上传的数据',checked,total,'请勿关闭页面');
      }
    }
    return existing;
  }
  const wait=(ms,signal)=>new Promise((resolve,reject)=>{
    if (signal.aborted) { reject(new DOMException('Paused','AbortError')); return; }
    const timer=setTimeout(()=>{signal.removeEventListener('abort',stop);resolve();},ms);
    const stop=()=>{clearTimeout(timer);reject(new DOMException('Paused','AbortError'));}; signal.addEventListener('abort',stop,{once:true});
  });
  async function putChunk(path,buffer,digest,signal) {
    for (let attempt=0;attempt<4;attempt++) {
      try { return await request(path,{method:'PUT',body:buffer,signal,headers:{'Content-Type':'application/octet-stream','X-Chunk-SHA256':digest}}); }
      catch(error) {
        if (error.name==='AbortError' || (error.status && error.status!==429 && error.status<500) || attempt===3) throw error;
        await wait(Math.min(8000,500*2**attempt)+Math.random()*250,signal);
      }
    }
  }
  async function uploadSession(session,selected) {
    const chunkSize=Number(session.chunk_size);
    if (!Number.isSafeInteger(chunkSize) || chunkSize<1 || chunkSize>(state.config?.max_chunk_size || 32*1024*1024)) throw new Error('上传会话参数异常，请稍后重试。');
    const chosen=validSelection(session,selected);
    state.controller=new AbortController(); $('pause-upload').hidden=false; const signal=state.controller.signal;
    let done=await verifyExisting(session,chosen,selected.total,signal);
    const queue=[];
    for (const file of session.files) {
      const present=new Set(file.uploaded_chunks || []);
      for (let index=0;index<Math.ceil(Number(file.size)/chunkSize);index++) if (!present.has(index)) queue.push({file,index});
    }
    let next=0, firstError=null; const started=performance.now(), initial=done;
    setUploadProgress('正在上传',done,selected.total,'请保持页面打开');
    async function worker() {
      try {
        while (next<queue.length && !signal.aborted) {
          const task=queue[next++], row=chosen.get(task.file.path);
          const {buffer,digest}=await chunkHash(row.file,task.index,chunkSize);
          if (signal.aborted) throw new DOMException('Paused','AbortError');
          if (digest!==task.file.expected_hashes[task.index]) throw new Error(`文件内容与最初选择时不一致：${task.file.path}。请取消上传后重新选择数据。`);
          const path=`uploads/${encodeURIComponent(session.id)}/files/${encodeURIComponent(task.file.id)}/chunks/${task.index}`;
          await putChunk(path,buffer,digest,signal); done+=buffer.byteLength;
          const seconds=Math.max(1,(performance.now()-started)/1000), rate=(done-initial)/seconds;
          setUploadProgress('正在上传',done,selected.total,rate>0 ? bytes(rate)+'/秒' : '请保持页面打开');
        }
      } catch(error) { if (!firstError) firstError=error; state.controller.abort(); }
    }
    await Promise.all(Array.from({length:Math.min(3,Math.max(queue.length,1))},worker));
    if (firstError) throw firstError;
    if (signal.aborted) throw new DOMException('Paused','AbortError');
    setUploadProgress('正在校验并提交',done,selected.total,'请勿关闭页面');
    await request('uploads/'+encodeURIComponent(session.id)+'/complete',{method:'POST',signal});
  }
  async function submitProject(event) {
    event.preventDefault(); if (state.busy) return;
    try {
      state.busy=true; state.controller=new AbortController(); $('submit-project').disabled=true; $('choose-folder').disabled=true; $('pause-upload').hidden=false; showMessage('new-error','');
      const config=await getConfig(true,state.controller.signal);
      const selected=selectedFolder(state.draft.files); const body=projectBody();
      if (!state.draft.project && (!body.name || !body.options.length)) throw new Error('请填写任务名称并至少选择一种成果。');
      if (!state.draft.upload && state.draft.isSupplement) {
        validateSupplement(selected,state.draft.existingRaw,config.dataset_limit);
      }
      if (!state.draft.upload && state.quota && selected.total>Number(state.quota.available)) throw new Error(`剩余空间不足：可用 ${bytes(state.quota.available)}，所选文件夹 ${bytes(selected.total)}。`);
      showMessage('new-error',''); setUploadProgress('正在准备任务',0,selected.total);
      if (!state.draft.project) {
        const data=await createIdempotently('projects','project',body,state.controller.signal); state.draft.project=data.project; lockProjectFields(true);
      }
      if (!state.draft.upload) {
        if (state.draft.manifest && state.draft.manifest.chunk_size!==config.chunk_size) state.draft.manifest=null;
        if (!state.draft.manifest) state.draft.manifest=await prepareManifest(selected,state.controller.signal,config.chunk_size);
        if (state.controller.signal.aborted) throw new DOMException('Paused','AbortError');
        if (new TextEncoder().encode(JSON.stringify(state.draft.manifest)).byteLength>16*1024*1024) throw new Error('文件清单过大，可能包含过多文件或过长的路径，请联系处理团队。');
        setUploadProgress('正在创建上传会话',0,selected.total,'本地校验已完成，尚未开始上传');
        const data=await createIdempotently('projects/'+encodeURIComponent(state.draft.project.id)+'/uploads','raw-upload-'+state.draft.project.id,state.draft.manifest,state.controller.signal);
        state.draft.upload=data.upload;
      } else {
        const data=await request('uploads/'+encodeURIComponent(state.draft.upload.id),{signal:state.controller.signal}); state.draft.upload=data.upload;
      }
      if (state.controller.signal.aborted) throw new DOMException('Paused','AbortError');
      if (state.draft.upload.status!=='complete') await uploadSession(state.draft.upload,selected);
      const id=state.draft.project.id; state.busy=false; state.controller=null; $('new-dialog').close();
      await openProject(id); notice(state.draft.isSupplement ? '补充数据已提交，处理团队将继续跟进。' : '原始数据已上传并提交，处理团队将跟进任务。');
    } catch(error) {
      if (error.code==='CONFIG_CHANGED') { state.draft.manifest=null; state.config=null; showMessage('new-error','上传配置已更新，请重试并重新校验本地文件。'); }
      else showMessage('new-error',errorMessage(error));
      $('upload-stage').textContent=error.name==='AbortError' ? '上传已暂停' : '上传未完成';
      $('upload-progress-text').textContent='可保留当前任务并稍后继续上传。';
      $('submit-project').textContent=state.draft?.project ? '重试上传 ↗' : '创建并上传 ↗';
    } finally { state.busy=false; state.controller=null; $('submit-project').disabled=false; $('choose-folder').disabled=!folderSupported; $('pause-upload').hidden=true; }
  }
  $('login-tab').addEventListener('click',()=>setAuthMode('login'));
  $('register-tab').addEventListener('click',()=>setAuthMode('register'));
  $('auth-form').addEventListener('submit',async event=>{
    event.preventDefault(); const register=$('auth-form').dataset.mode==='register';
    const username=$('username').value.trim(), password=$('password').value;
    if (register && password!==$('confirm-password').value) { showMessage('auth-error','两次输入的密码不一致。'); return; }
    $('auth-submit').disabled=true; showMessage('auth-error','');
    try { const data=await request('auth/'+(register?'register':'login'),{method:'POST',json:{username,password}}); $('password').value=''; $('confirm-password').value=''; await setUser(data); }
    catch(error) { showMessage('auth-error',errorMessage(error)); }
    finally { $('auth-submit').disabled=false; }
  });
  async function logout() {
    if (state.busy) { notice('请先暂停上传，再退出账户。',true); return; }
    try { await request('auth/logout',{method:'POST'}); $('admin-link')?.remove(); if ($('password-dialog').open) $('password-dialog').close(); notice(''); showAuth(); }
    catch(error) { if ($('password-dialog').open) showMessage('password-error',errorMessage(error)); else notice(errorMessage(error),true); }
  }
  function openPassword(required) {
    state.passwordRequired=!!required; $('password-form').reset(); showMessage('password-error','');
    $('password-hint').textContent=required ? '为保障账户安全，请先修改当前密码。新密码至少 10 位。' : '请使用至少 10 位的新密码。';
    $('password-cancel').textContent=required ? '退出登录' : '取消';
    if (!$('password-dialog').open) $('password-dialog').showModal(); $('current-password').focus();
  }
  $('logout').addEventListener('click',logout);
  $('change-password').addEventListener('click',()=>openPassword(false));
  $('password-cancel').addEventListener('click',()=>{ if (state.passwordRequired) logout(); else $('password-dialog').close(); });
  $('password-dialog').addEventListener('cancel',event=>{ if (state.passwordRequired) event.preventDefault(); });
  $('password-form').addEventListener('submit',async event=>{
    event.preventDefault(); const current=$('current-password').value, next=$('new-password').value;
    if (next!==$('new-password-confirm').value) { showMessage('password-error','两次输入的新密码不一致。'); return; }
    $('password-submit').disabled=true; showMessage('password-error','');
    try {
      await request('auth/password',{method:'POST',json:{current_password:current,new_password:next}});
      $('password-dialog').close(); $('password-form').reset(); state.passwordRequired=false;
      const data=await request('auth/me'); await setUser(data); notice('密码已更新。');
    } catch(error) { showMessage('password-error',errorMessage(error)); }
    finally { $('password-submit').disabled=false; }
  });
  $('delete-project').addEventListener('click',()=>{
    $('delete-name').textContent=state.project.name; $('delete-confirm').value=''; $('delete-submit').disabled=true; showMessage('delete-error','');
    $('delete-dialog').showModal(); $('delete-confirm').focus();
  });
  $('delete-confirm').addEventListener('input',()=>{ $('delete-submit').disabled=$('delete-confirm').value!==state.project?.name; });
  $('delete-cancel').addEventListener('click',()=>$('delete-dialog').close());
  $('delete-form').addEventListener('submit',async event=>{
    event.preventDefault(); if ($('delete-confirm').value!==state.project?.name) return;
    $('delete-submit').disabled=true; showMessage('delete-error','');
    try {
      const result=await request('projects/'+encodeURIComponent(state.project.id),{method:'DELETE'}); $('delete-dialog').close(); state.project=null;
      $('detail-view').hidden=true; $('overview-view').hidden=false; await loadProjects(); notice(result.storage_cleanup_pending ? '任务已删除，服务器空间正在清理。' : '任务及相关数据已删除。');
    } catch(error) { showMessage('delete-error',errorMessage(error)); $('delete-submit').disabled=false; }
  });
  $('new-project').addEventListener('click',openNew); $('refresh-projects').addEventListener('click',loadProjects);
  $('back-to-projects').addEventListener('click',()=>{ $('detail-view').hidden=true; $('overview-view').hidden=false; state.project=null; notice(''); loadProjects(); });
  $('close-dialog').addEventListener('click',()=>{
    if (state.busy) { if (state.controller) { state.controller.abort(); showMessage('new-error','正在暂停上传，请稍候。'); } else showMessage('new-error','正在创建任务，请稍候。'); return; }
    $('new-dialog').close(); if (state.draft?.project) { openProject(state.draft.project.id); }
  });
  $('new-dialog').addEventListener('cancel',event=>{ if (state.busy) { event.preventDefault(); if (state.controller) { state.controller.abort(); showMessage('new-error','正在暂停上传，请稍候。'); } else showMessage('new-error','正在创建任务，请稍候。'); } });
  $('choose-folder').addEventListener('click',()=>{ $('folder-input').value=''; $('folder-input').click(); });
  $('folder-input').addEventListener('change',event=>{
    try { const selected=selectedFolder(event.target.files); state.draft.files=Array.from(event.target.files); state.draft.manifest=null; $('folder-title').textContent=selected.rows[0].path.split('/')[0]; $('folder-summary').textContent=`已选择 ${selected.rows.length} 个文件 · ${bytes(selected.total)}。${state.draft.isSupplement ? '请确认仅包含需要补充的文件。' : s1Hint(selected.rows)}`; showMessage('new-error',''); }
    catch(error) { state.draft.files=[]; $('folder-summary').textContent=''; showMessage('new-error',errorMessage(error)); }
  });
  $('pause-upload').addEventListener('click',()=>{ state.controller?.abort(); showMessage('new-error','正在暂停上传，请稍候。'); });
  $('new-form').addEventListener('submit',submitProject);
  $('message-form').addEventListener('submit',async event=>{
    event.preventDefault(); const text=$('message-text').value.trim(); if (!text) { showMessage('message-error','请输入留言内容。'); return; } if (!state.project) return;
    const projectId=state.project.id; $('message-submit').disabled=true; showMessage('message-error','');
    try {
      const response=await createIdempotently('projects/'+encodeURIComponent(projectId)+'/messages','message-'+projectId,{text});
      if (!response.message) throw new Error('无法确认留言结果，请刷新后查看。');
      $('message-text').value=''; state.detail.messages=[...(state.detail.messages || []).filter(message=>message.id!==response.message.id),response.message]; renderMessages(state.detail.messages);
    } catch(error) { showMessage('message-error',errorMessage(error)); }
    finally { $('message-submit').disabled=false; }
  });
  $('public-toggle').addEventListener('change',async event=>{
    const toggle=event.target, previous=!toggle.checked;
    if (toggle.checked && !window.confirm('公开后，拥有链接的人可以查看和下载已交付成果。原始扫描数据不会公开。确定公开吗？')) { toggle.checked=false; return; }
    let uncertain=false; toggle.disabled=true; showMessage('public-error','');
    try { const data=await request('projects/'+encodeURIComponent(state.project.id),{method:'PATCH',json:{is_public:toggle.checked}}); if (!data.project) throw new Error('无法确认公开状态，请刷新后重试。'); state.detail.project=data.project; renderProject(state.detail,false); }
    catch(error) {
      if (error.status) { toggle.checked=previous; showMessage('public-error',errorMessage(error)); }
      else {
        try { const latest=await request('projects/'+encodeURIComponent(state.project.id)); renderProject(latest,false); showMessage('public-error','操作未确认，已显示服务器当前状态。'); }
        catch { uncertain=true; toggle.indeterminate=true; $('public-description').textContent='状态待确认，请刷新页面'; showMessage('public-error','无法确认公开状态，请刷新页面后再操作。'); }
      }
    }
    finally { toggle.disabled=uncertain || state.project?.status!=='delivered' || !state.detail?.result_files?.length; }
  });
  for (const tab of [$('login-tab'),$('register-tab')]) tab.addEventListener('keydown',event=>{
    if (event.key==='ArrowLeft' || event.key==='ArrowRight') { event.preventDefault(); const next=tab.id==='login-tab' ? $('register-tab') : $('login-tab'); next.click(); next.focus(); }
  });
  if (!folderSupported) { $('choose-folder').disabled=true; $('folder-description').textContent='当前浏览器不支持选择文件夹，请使用新版桌面浏览器。'; }
  window.addEventListener('beforeunload',event=>{ if (state.busy) { event.preventDefault(); event.returnValue=''; } });
  setAuthMode('login');
  request('auth/me').then(setUser).catch(error=>{ showAuth(); if (error.status!==401) showMessage('auth-error',errorMessage(error)); });
})();
