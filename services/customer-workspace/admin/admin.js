/* 管理端只调用同源工作空间接口；所有权限和配额以服务器校验为准。 */
(() => {
  'use strict';

  const API = '/workspace/api/';
  const $ = id => document.getElementById(id);
  const statusNames = { draft: '待上传', uploading: '上传中', submitted: '待处理', processing: '处理中', needs_info: '待补充', delivered: '已交付' };
  const optionNames = { pointcloud: '点云', model: '三维模型', cad: 'CAD', ifc: 'BIM IFC', sketchup: 'SketchUp' };
  const state = { csrf: '', config: null, projects: [], selected: null, detail: null, files: [], busy: false, resetting: false, messageSending: false, pendingRequest: null, abort: null, createKey: null };

  function showNotice(message, error = false) {
    const box = $('notice');
    box.textContent = message;
    box.classList.toggle('error', error);
    box.hidden = !message;
  }

  function make(tag, text, className) {
    const el = document.createElement(tag);
    if (text !== undefined && text !== null) el.textContent = String(text);
    if (className) el.className = className;
    return el;
  }

  function sizeText(value) {
    const size = Number(value) || 0;
    if (size >= 1e9) return (size / 1e9).toFixed(2) + ' GB';
    if (size >= 1e6) return (size / 1e6).toFixed(1) + ' MB';
    if (size >= 1e3) return (size / 1e3).toFixed(1) + ' KB';
    return size + ' B';
  }

  function timeText(value) {
    if (!value) return '时间未提供';
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? String(value) : new Intl.DateTimeFormat('zh-CN', { dateStyle: 'medium', timeStyle: 'short' }).format(date);
  }

  async function request(path, options = {}) {
    const method = options.method || 'GET';
    const headers = new Headers(options.headers || {});
    if (method !== 'GET') headers.set('X-CSRF-Token', state.csrf);
    let body = options.body;
    if (body !== undefined && !(body instanceof ArrayBuffer) && !(body instanceof Blob)) {
      headers.set('Content-Type', 'application/json');
      body = JSON.stringify(body);
    }
    const controller = new AbortController();
    const outer = options.signal;
    const onAbort = () => controller.abort();
    if (outer?.aborted) controller.abort(); else outer?.addEventListener('abort', onAbort, { once: true });
    const timeout = setTimeout(() => controller.abort(), options.timeoutMs || 180000);
    try {
      const response = await fetch(API + path, { method, headers, body, signal: controller.signal, credentials: 'same-origin', cache: 'no-store' });
      let result = null;
      const contentType = response.headers.get('content-type') || '';
      if (contentType.includes('json')) {
        try { result = await response.json(); } catch { result = null; }
      }
      if (!response.ok) {
        const error = new Error(result?.error?.message || result?.message || (typeof result?.error === 'string' ? result.error : '') || `请求失败（${response.status}）`);
        error.status = response.status;
        error.code = result?.error?.code || result?.code || '';
        throw error;
      }
      if (!result || typeof result !== 'object') throw new Error('服务器响应格式异常，请刷新后重试。');
      return result;
    } catch (error) {
      if (controller.signal.aborted && !outer?.aborted) { const timeoutError = new Error('网络响应超时，请稍后继续上传。'); timeoutError.name = 'TimeoutError'; throw timeoutError; }
      throw error;
    } finally {
      clearTimeout(timeout); outer?.removeEventListener('abort', onAbort);
    }
  }

  function userError(error) {
    if (error?.code === 'MUST_CHANGE_PASSWORD') return '首次登录需要先修改密码，请返回用户工作空间完成修改。';
    if (error?.status === 401) return '登录已失效，请返回用户工作空间重新登录。';
    if (error?.status === 403) return '当前账号没有管理员权限。';
    if (error?.name === 'AbortError') return '上传已暂停，已确认的分片会保留；重新选择相同文件可继续。';
    if (error?.name === 'TimeoutError') return '网络响应超时，已确认的分片会保留；请检查网络后继续上传。';
    if (error?.name === 'TypeError') return '网络连接中断，请检查网络后重试；已确认的分片会保留。';
    return error?.message || '操作未完成，请稍后重试。';
  }

  function showError(error) {
    showNotice(userError(error), true);
    if (error?.code === 'MUST_CHANGE_PASSWORD') {
      const link = make('a', '前往修改密码'); link.href = '/workspace/#password'; link.className = 'notice-link'; $('notice').append(' ', link);
    }
  }

  function statusBadge(value) {
    const badge = make('span', statusNames[value] || '状态未知', 'status');
    if (['processing', 'needs_info', 'delivered'].includes(value)) badge.classList.add(value === 'delivered' ? 'completed' : value);
    return badge;
  }

  function canUpdateStatus() {
    return ['submitted', 'processing', 'needs_info', 'delivered'].includes(state.detail?.project?.status);
  }

  function setBusy(value) {
    state.busy = value;
    const locked = value || state.messageSending;
    $('refresh').disabled = locked;
    $('search').disabled = locked;
    $('filter').disabled = locked;
    $('result-input').disabled = locked;
    $('result-folder').disabled = locked;
    $('save-status').disabled = locked || !canUpdateStatus();
    $('status-input').disabled = locked || !canUpdateStatus();
    $('status-message').disabled = locked || !canUpdateStatus();
    $('message-input').disabled = locked;
    $('send-message').disabled = locked;
    $('reset-password').disabled = locked || !state.detail?.project?.user?.id;
    $('pause-upload').hidden = !value;
    document.querySelectorAll('.project-item, .pending-row button').forEach(el => { el.disabled = locked; });
    updateSelection();
  }

  function drawProjects() {
    const query = $('search').value.trim().toLocaleLowerCase();
    const filter = $('filter').value;
    const projects = state.projects.filter(project => {
      const name = `${project.name || ''} ${project.user?.username || ''}`.toLocaleLowerCase();
      return name.includes(query) && (filter === 'all' || project.status === filter);
    });
    $('count-all').textContent = String(state.projects.length);
    $('count-pending').textContent = String(state.projects.filter(p => p.status === 'submitted').length);
    $('count-processing').textContent = String(state.projects.filter(p => p.status === 'processing').length);
    $('count-complete').textContent = String(state.projects.filter(p => p.status === 'delivered').length);
    $('visible-count').textContent = `${projects.length} 个项目`;
    const list = $('project-list');
    list.replaceChildren();
    if (!projects.length) { list.append(make('p', state.projects.length ? (state.selected ? '没有匹配的项目，仍显示当前项目。' : '没有匹配的项目。') : '当前还没有项目。', 'empty')); return; }
    for (const project of projects) {
      const button = make('button', null, 'project-item' + (project.id === state.selected ? ' selected' : ''));
      button.type = 'button';
      button.disabled = state.busy || state.messageSending;
      button.setAttribute('aria-pressed', String(project.id === state.selected));
      const top = make('div', null, 'project-item-top');
      top.append(make('strong', project.name || '未命名项目'), statusBadge(project.status));
      button.append(top, make('p', `${project.user?.username || '用户'} · ${timeText(project.created_at)}`));
      button.addEventListener('click', () => selectProject(project.id));
      list.append(button);
    }
  }

  async function loadProjects(preserve = true) {
    if (state.busy || state.messageSending) return;
    try {
      const data = await request('admin/projects');
      state.projects = Array.isArray(data?.projects) ? data.projects : [];
      drawProjects();
      const id = preserve && state.projects.some(p => p.id === state.selected) ? state.selected : state.projects[0]?.id;
      if (id) await selectProject(id, preserve && id === state.selected);
      else { state.selected = null; state.detail = null; $('detail').hidden = true; $('detail-empty').hidden = false; $('detail-panel').setAttribute('aria-labelledby', 'detail-heading'); }
    } catch (error) { showError(error); $('project-list').replaceChildren(make('p', '项目列表暂不可用。', 'empty')); }
  }

  async function loadSystem() {
    const badge = $('system-badge');
    try {
      const result = await request('admin/system');
      const storage = result?.storage || {}, backup = result?.backup || {};
      const free = storage.free, total = storage.total;
      $('system-storage').textContent = typeof free === 'number' && typeof total === 'number' && Number.isFinite(free) && Number.isFinite(total) && free >= 0 && total >= free
        ? `可用 ${sizeText(free)} / 总计 ${sizeText(total)}` : '存储信息暂不可用';
      $('system-backup').textContent = backup.last_success ? `${timeText(backup.last_success)}${typeof backup.bytes === 'number' && Number.isFinite(backup.bytes) ? ' · ' + sizeText(backup.bytes) : ''}` : '尚无成功记录';
      const ok = backup.status === 'ok' && !!backup.last_success;
      badge.classList.toggle('warning', !ok);
      badge.textContent = ok ? '最近备份成功' : backup.status === 'failed' ? '备份失败，请检查' : backup.status === 'never' ? '备份尚未运行' : '备份状态待确认';
      $('system-message').textContent = typeof backup.message === 'string' ? backup.message.slice(0, 500) : '';
    } catch {
      badge.classList.add('warning'); badge.textContent = '备份状态不可用';
      $('system-storage').textContent = '存储信息暂不可用'; $('system-backup').textContent = '备份信息暂不可用';
      $('system-message').textContent = '请稍后刷新状态或联系管理员。';
    }
  }

  async function selectProject(id, preserveFiles = false, force = false) {
    if ((state.busy || state.messageSending) && !force) return;
    state.selected = id;
    if (!preserveFiles) { state.files = []; state.createKey = null; $('result-input').value = ''; $('result-folder').value = ''; $('upload-progress').hidden = true; }
    drawProjects();
    try {
      const result = await request(`admin/projects/${encodeURIComponent(id)}`);
      if (state.selected !== id) return;
      state.detail = result;
      renderDetail(result);
    } catch (error) { if (state.selected === id) { $('detail').hidden = true; $('detail-empty').hidden = false; $('detail-panel').setAttribute('aria-labelledby', 'detail-heading'); showError(error); } }
  }

  function renderFiles(target, files) {
    const root = $(target);
    const items = Array.isArray(files) ? files : [];
    root.replaceChildren();
    if (!items.length) { root.append(make('p', '暂无文件', 'empty')); return; }
    let limit = 50;
    const render = () => {
      root.replaceChildren();
      for (const file of items.slice(0, limit)) {
        const row = make('div', null, 'file-row');
        const name = make('div', file.path || '未命名文件', 'file-name');
        let info = sizeText(file.size);
        if (file.state !== 'complete') info += ' · 上传中';
        else if (target === 'result-files' && typeof file.is_delivered === 'boolean') info += file.is_delivered ? ' · 已交付' : ' · 待确认交付';
        name.append(make('small', info));
        row.append(name);
        if (file.state === 'complete') {
          const link = make('a', '下载', 'file-download');
          link.href = API + 'admin/files/' + encodeURIComponent(file.id) + '/download';
          link.download = '';
          link.setAttribute('aria-label', `下载 ${file.path || '文件'}`);
          row.append(link);
        }
        root.append(row);
      }
      if (items.length > limit) {
        const more = make('button', `显示更多（剩余 ${items.length - limit} 个）`, 'file-more');
        more.type = 'button';
        more.addEventListener('click', () => { limit += 100; render(); });
        root.append(more);
      }
    };
    render();
  }

  function renderMessages(messages) {
    const root = $('messages-list');
    const items = Array.isArray(messages) ? messages : [];
    $('message-count').textContent = `${items.length} 条记录`;
    let limit = 50;
    const render = () => {
      root.replaceChildren();
      if (!items.length) { root.append(make('p', '暂无留言。', 'empty')); return; }
      if (items.length > limit) {
        const more = make('button', `显示更早的记录（剩余 ${items.length - limit} 条）`, 'file-more');
        more.type = 'button'; more.addEventListener('click', () => { limit += 50; render(); }); root.append(more);
      }
      for (const message of items.slice(-limit)) {
        const row = make('div', null, 'message-row');
        const meta = make('div', null, 'message-meta');
        meta.append(make('strong', message.author === 'user' ? '客户' : message.author === 'admin' ? '处理团队' : '系统'), make('time', timeText(message.created_at)));
        row.append(meta, make('p', message.text || '')); root.append(row);
      }
    };
    render();
  }

  function matchingUpload() {
    const files = state.files;
    if (!files.length) return null;
    const selected = new Map(files.map(file => [file.path, file.file.size]));
    return (state.detail?.uploads || []).find(upload => upload.kind === 'result' && upload.status === 'uploading' &&
      Array.isArray(upload.files) && upload.files.length === selected.size && upload.files.every(file => selected.has(file.path) && selected.get(file.path) === file.size)) || null;
  }

  function renderPending() {
    const container = $('pending-uploads');
    container.replaceChildren();
    for (const upload of (state.detail?.uploads || []).filter(item => item.kind === 'result' && item.status === 'uploading')) {
      const row = make('div', null, 'pending-row');
      const description = `有未完成的成果上传（${upload.files?.length || 0} 个文件${upload.total_size != null ? '，' + sizeText(upload.total_size) : ''}${upload.created_at ? '，' + timeText(upload.created_at) : ''}）。`;
      row.append(make('span', description +
        (state.detail?.project?.is_public ? '项目已公开，请先联系客户关闭公开后继续。' : '重选相同文件可继续。')));
      const cancel = make('button', '取消上传');
      cancel.type = 'button'; cancel.disabled = state.busy || state.messageSending;
      cancel.addEventListener('click', async () => {
        if (state.busy || !window.confirm('确定取消这次未完成的成果上传？取消后需要重新上传。')) return;
        cancel.disabled = true;
        try {
          await request(`uploads/${encodeURIComponent(upload.id)}/cancel`, { method: 'POST' });
          showNotice('未完成的成果上传已取消。');
          await selectProject(state.selected, true);
        } catch (error) { showError(error); cancel.disabled = false; }
      });
      row.append(cancel); container.append(row);
    }
  }

  function renderDetail(data) {
    const project = data.project || {};
    const raw = Array.isArray(data.raw_files) ? data.raw_files : [];
    const results = Array.isArray(data.result_files) ? data.result_files : [];
    $('detail-empty').hidden = true; $('detail').hidden = false;
    $('detail-panel').setAttribute('aria-labelledby', 'project-title');
    $('project-title').textContent = project.name || '未命名项目';
    $('project-subtitle').textContent = `${project.user?.username || '用户'} · ${timeText(project.created_at)}`;
    const status = $('project-status'); status.replaceWith(statusBadge(project.status));
    const renderedStatus = document.querySelector('.detail-top .status'); renderedStatus.id = 'project-status';
    const meta = $('project-meta'); meta.replaceChildren();
    meta.append(make('span', `原始数据 ${sizeText(project.raw_bytes)}`), make('span', `成果 ${sizeText(project.result_bytes)}`), make('span', project.is_public ? '用户已设为公开' : '仅本人可见'));
    if (data.quota && Number.isFinite(Number(data.quota.available))) meta.append(make('span', `用户可用空间 ${sizeText(data.quota.available)}`));
    const tags = $('requirements'); tags.replaceChildren();
    for (const key of Array.isArray(project.options) ? project.options : []) tags.append(make('span', optionNames[key] || key, 'tag'));
    if (!tags.children.length) tags.append(make('span', '未指定', 'tag'));
    $('customer-note').textContent = project.note || '';
    $('customer-note').hidden = !project.note;
    renderMessages(data.messages);
    $('raw-count').textContent = `${raw.length} 个文件`;
    $('result-count').textContent = `${results.length} 个文件`;
    renderFiles('raw-files', raw); renderFiles('result-files', results);
    const archive = $('raw-archive');
    const manifest = $('raw-manifest');
    archive.hidden = !raw.length || raw.some(file => file.state !== 'complete'); manifest.hidden = archive.hidden; $('raw-hint').hidden = archive.hidden;
    archive.href = API + 'admin/projects/' + encodeURIComponent(project.id) + '/raw.zip';
    manifest.href = API + 'admin/projects/' + encodeURIComponent(project.id) + '/raw.sha256';
    archive.download = '';
    manifest.download = '';
    $('status-input').value = ['submitted', 'processing', 'needs_info', 'delivered'].includes(project.status) ? project.status : 'submitted';
    $('status-message').value = project.message || project.status_message || '';
    $('status-unavailable').hidden = canUpdateStatus();
    $('status-input').disabled = state.busy || state.messageSending || !canUpdateStatus();
    $('status-message').disabled = state.busy || state.messageSending || !canUpdateStatus();
    $('save-status').disabled = state.busy || state.messageSending || !canUpdateStatus();
    $('reset-password').disabled = state.busy || state.messageSending || !project.user?.id;
    $('privacy-note').textContent = project.is_public ? '该项目已公开。为保护数据，请先联系项目所有者关闭公开，再上传新的成果。' : '公开展示由项目所有者控制，请交付前核对状态。';
    renderPending(); updateSelection();
  }

  function selectionFrom(input) {
    if (state.busy) return;
    const files = Array.from(input.files || []).map(file => ({ file, path: file.webkitRelativePath || file.name }));
    const limit = Number(state.config?.dataset_limit) || 5000000000;
    if (files.length > 100000 || files.reduce((sum, item) => sum + item.file.size, 0) > limit) {
      state.files = []; input.value = ''; state.createKey = null;
      showNotice(`每次最多上传 ${sizeText(limit)}、100000 个文件；请分批选择成果。`, true); updateSelection(); return;
    }
    const names = new Set();
    const encoder = new TextEncoder();
    for (const item of files) {
      const parts = item.path.split('/');
      const canonical = item.path.normalize('NFC').toLowerCase();
      if (!item.path || item.path.startsWith('/') || item.path.includes('\\') || encoder.encode(item.path).length > 2048 ||
          parts.some(part => !part || part === '.' || part === '..' || /[<>:"|?*]/.test(part) || /[ .]$/.test(part) ||
            /^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?$/i.test(part) || encoder.encode(part).length > 255) ||
          /[\u0000-\u001f\u007f]/.test(item.path) || names.has(canonical)) {
        state.files = []; input.value = ''; state.createKey = null;
        showNotice('文件路径重复或包含 Windows 不支持的名称，请重新选择。', true); updateSelection(); return;
      }
      names.add(canonical);
    }
    state.files = files; state.createKey = null;
    $('upload-progress').hidden = true;
    showNotice(''); updateSelection();
  }

  function updateSelection() {
    const files = state.files;
    const text = $('selected-files');
    if (!files.length) text.textContent = '尚未选择成果文件。';
    else {
      const size = files.reduce((sum, item) => sum + item.file.size, 0);
      text.textContent = `已选择 ${files.length} 个文件，共 ${sizeText(size)}。` + (matchingUpload() && !state.detail?.project?.is_public ? ' 匹配到未完成上传，可以继续。' : '');
    }
    if (state.detail?.project?.is_public) text.textContent += ' 项目已公开，需客户先关闭公开后才能上传成果。';
    $('result-input').disabled = state.busy || state.messageSending || !!state.detail?.project?.is_public;
    $('result-folder').disabled = state.busy || state.messageSending || !!state.detail?.project?.is_public;
    $('upload').disabled = state.busy || state.messageSending || !files.length || !state.detail || !!state.detail?.project?.is_public;
    $('upload').textContent = matchingUpload() ? '继续上传' : '上传成果';
  }

  function updateProgress(done, total, label) {
    const percent = total ? Math.min(100, Math.floor(done / total * 100)) : 100;
    $('upload-progress').hidden = false;
    $('progress').value = percent;
    $('progress').setAttribute('aria-label', label);
    $('progress-percent').textContent = `${percent}%`;
    $('progress-label').textContent = label;
  }

  function digest(buffer) {
    return crypto.subtle.digest('SHA-256', buffer).then(value => Array.from(new Uint8Array(value), byte => byte.toString(16).padStart(2, '0')).join(''));
  }

  function randomKey() {
    const data = crypto.getRandomValues(new Uint8Array(24));
    return Array.from(data, value => value.toString(16).padStart(2, '0')).join('');
  }

  async function operationKey(scope, projectId, content) {
    if (!crypto?.subtle || !crypto?.getRandomValues) throw new Error('当前浏览器不支持安全操作，请使用 HTTPS 或更新浏览器。');
    const fingerprint = await digest(new TextEncoder().encode(scope + '\u0000' + projectId + '\u0000' + content));
    const storageKey = 'workspace-operation-' + fingerprint;
    let key = state.pendingRequest?.fingerprint === fingerprint ? state.pendingRequest.key : null;
    try { key ||= sessionStorage.getItem(storageKey); } catch { /* In-memory recovery remains available. */ }
    if (!key) {
      if (typeof crypto.randomUUID === 'function') key = crypto.randomUUID();
      else {
        const bytes = crypto.getRandomValues(new Uint8Array(16)); bytes[6] = (bytes[6] & 15) | 64; bytes[8] = (bytes[8] & 63) | 128;
        const hex = Array.from(bytes, value => value.toString(16).padStart(2, '0')).join('');
        key = `${hex.slice(0,8)}-${hex.slice(8,12)}-${hex.slice(12,16)}-${hex.slice(16,20)}-${hex.slice(20)}`;
      }
      try { sessionStorage.setItem(storageKey, key); } catch { /* In-memory recovery remains available. */ }
    }
    state.pendingRequest = { fingerprint, key, storageKey };
    return state.pendingRequest;
  }

  function clearOperationKey(item) {
    try { sessionStorage.removeItem(item.storageKey); } catch { /* Nothing sensitive is stored. */ }
    if (state.pendingRequest?.fingerprint === item.fingerprint) state.pendingRequest = null;
  }

  function sleep(ms, signal) {
    return new Promise((resolve, reject) => {
      if (signal.aborted) return reject(new DOMException('已暂停', 'AbortError'));
      const timer = setTimeout(done, ms);
      function done() { signal.removeEventListener('abort', stop); resolve(); }
      function stop() { clearTimeout(timer); signal.removeEventListener('abort', stop); reject(new DOMException('已暂停', 'AbortError')); }
      signal.addEventListener('abort', stop, { once: true });
    });
  }

  async function retry(action, signal) {
    for (let attempt = 0; ; attempt++) {
      if (signal.aborted) throw new DOMException('已暂停', 'AbortError');
      try { return await action(); }
      catch (error) {
        if (signal.aborted) throw new DOMException('已暂停', 'AbortError');
        if (attempt >= 3 || (error.status && error.status < 500 && error.status !== 429)) throw error;
        await sleep(Math.min(1000 * 2 ** attempt, 6000), signal);
      }
    }
  }

  async function workers(tasks, callback, abort) {
    let next = 0, firstError = null;
    const work = async () => {
      while (!firstError && !abort.signal.aborted) {
        const index = next++;
        if (index >= tasks.length) return;
        try { await callback(tasks[index]); }
        catch (error) { if (!firstError) firstError = error; abort.abort(); return; }
      }
    };
    await Promise.all(Array.from({ length: Math.min(3, tasks.length) }, work));
    if (firstError) throw firstError;
    if (abort.signal.aborted) throw new DOMException('已暂停', 'AbortError');
  }

  async function hashManifest(abort, total, chunkBytes) {
    const count = state.files.reduce((sum, item) => sum + Math.ceil(item.file.size / chunkBytes), 0);
    if (count > 100000) throw new Error('文件分片过多，当前浏览器无法安全处理；请联系管理员。');
    const manifest = state.files.map(item => ({ path: item.path, size: item.file.size, last_modified: item.file.lastModified,
      expected_hashes: Array(Math.ceil(item.file.size / chunkBytes)) }));
    const tasks = [];
    for (let i = 0; i < state.files.length; i++) {
      const file = state.files[i].file;
      for (let index = 0; index < manifest[i].expected_hashes.length; index++) {
        const start = index * chunkBytes, end = Math.min(start + chunkBytes, file.size);
        tasks.push({ file, index, start, end, entry: manifest[i] });
      }
    }
    let checked = 0;
    updateProgress(0, total, '正在本机计算文件校验值');
    await workers(tasks, async task => {
      task.entry.expected_hashes[task.index] = await digest(await task.file.slice(task.start, task.end).arrayBuffer());
      checked += task.end - task.start; updateProgress(checked, total, '正在本机计算文件校验值');
    }, abort);
    return manifest;
  }

  async function beginUpload(signal, manifest, chunkBytes) {
    if (!state.createKey) state.createKey = randomKey();
    const response = await retry(() => request(`projects/${encodeURIComponent(state.selected)}/uploads`, {
      method: 'POST', signal, headers: { 'Idempotency-Key': state.createKey },
      body: { kind: 'result', chunk_size: chunkBytes, files: manifest }
    }), signal);
    return response.upload;
  }

  async function uploadResults() {
    if (state.busy || state.messageSending || !state.files.length || !state.detail || state.detail.project?.is_public) return;
    if (!window.crypto?.subtle || !window.crypto?.getRandomValues) { showNotice('当前浏览器无法计算上传校验值，请使用 HTTPS 或本机地址打开，并更新浏览器。', true); return; }
    const projectId = state.selected;
    const abort = new AbortController(); state.abort = abort; setBusy(true); showNotice('');
    let done = 0;
    const total = state.files.reduce((sum, item) => sum + item.file.size, 0);
    try {
      const config = await retry(() => request('config', { signal: abort.signal }), abort.signal);
      if (!Number.isSafeInteger(config?.chunk_size) || !Number.isSafeInteger(config?.max_chunk_size) ||
          !Number.isSafeInteger(config?.dataset_limit) || config.chunk_size < 1 || config.chunk_size > config.max_chunk_size ||
          config.max_chunk_size > 64 * 1024 * 1024 || config.dataset_limit < 1) throw new Error('上传配置无效，请联系管理员。');
      state.config = config;
      if (total > config.dataset_limit) throw new Error(`本次成果超过 ${sizeText(config.dataset_limit)}，请分批上传。`);
      const available = Number(state.detail?.quota?.available);
      const existing = matchingUpload();
      if (!existing && Number.isFinite(available) && total > available) throw new Error(`用户空间不足：本次成果 ${sizeText(total)}，当前可用 ${sizeText(available)}。`);
      const prior = existing ? (await retry(() => request(`uploads/${encodeURIComponent(existing.id)}`, { signal: abort.signal }), abort.signal)).upload : null;
      if (existing && (!prior || prior.status !== 'uploading')) throw new Error('这次上传已结束，请刷新项目后重新选择文件。');
      const chunkBytes = prior ? prior.chunk_size : config.chunk_size;
      if (!Number.isSafeInteger(chunkBytes) || chunkBytes < 1 || chunkBytes > config.max_chunk_size) throw new Error('上传会话的分片大小无法处理，请联系管理员。');
      const manifest = await hashManifest(abort, total, chunkBytes);
      updateProgress(0, total, '正在准备上传');
      const upload = prior || await beginUpload(abort.signal, manifest, chunkBytes);
      if (!upload || upload.status !== 'uploading' || upload.chunk_size !== chunkBytes || !Array.isArray(upload.files)) throw new Error('上传会话信息不完整，请刷新后重试。');
      const selected = new Map(state.files.map(item => [item.path, item.file]));
      const hashes = new Map(manifest.map(item => [item.path, item.expected_hashes]));
      if (upload.files.length !== selected.size || upload.files.some(item => !selected.has(item.path) || selected.get(item.path).size !== item.size)) throw new Error('所选文件与未完成上传不一致，请重新选择原文件。');
      const pending = [];
      for (const entry of upload.files) {
        const file = selected.get(entry.path);
        const count = Math.ceil(file.size / upload.chunk_size);
        const expected = hashes.get(entry.path);
        if (!Array.isArray(entry.expected_hashes) || entry.expected_hashes.length !== count || entry.expected_hashes.some((hash, index) => hash !== expected[index]))
          throw new Error(`文件“${entry.path}”与最初选择的内容不一致。请选择原文件，或取消旧上传后重新上传。`);
        const uploaded = new Set(entry.uploaded_chunks || []);
        if ([...uploaded].some(index => !Number.isInteger(index) || index < 0 || index >= count)) throw new Error('服务器返回的上传进度无效，请联系管理员。');
        for (let index = 0; index < count; index++) {
          const start = index * upload.chunk_size, end = Math.min(start + upload.chunk_size, file.size);
          if (uploaded.has(index)) {
            if (entry.chunk_hashes?.[String(index)] !== expected[index]) throw new Error('服务器已上传分片的校验信息不一致，请联系管理员。');
            done += end - start;
          } else pending.push({ entry, file, index, start, end, expected: expected[index] });
        }
      }
      updateProgress(done, total, '正在上传成果');
      await workers(pending, async task => {
        const bytes = await task.file.slice(task.start, task.end).arrayBuffer();
        const hash = await digest(bytes);
        if (hash !== task.expected) throw new Error(`文件“${task.entry.path}”在上传期间发生变化，请重新选择原文件。`);
        await retry(() => request(`uploads/${encodeURIComponent(upload.id)}/files/${encodeURIComponent(task.entry.id)}/chunks/${task.index}`, {
          method: 'PUT', body: bytes, signal: abort.signal,
          headers: { 'Content-Type': 'application/octet-stream', 'X-Chunk-SHA256': hash }
        }), abort.signal);
        done += task.end - task.start; updateProgress(done, total, '正在上传成果');
      }, abort);
      updateProgress(done, total, '正在确认成果');
      await retry(() => request(`uploads/${encodeURIComponent(upload.id)}/complete`, { method: 'POST', signal: abort.signal }), abort.signal);
      updateProgress(total, total, '成果上传完成');
      state.files = []; state.createKey = null; $('result-input').value = ''; $('result-folder').value = '';
      showNotice('成果已上传。请确认内容后，将项目状态保存为“已交付”，用户才能看到新成果。');
    } catch (error) { if (error?.code === 'CONFIG_CHANGED') state.createKey = null; showError(error); }
    finally {
      state.abort = null; setBusy(false);
      if (state.selected === projectId) await selectProject(projectId, true);
    }
  }

  $('search').addEventListener('input', drawProjects);
  $('filter').addEventListener('change', drawProjects);
  $('system-panel').addEventListener('toggle', () => { $('system-toggle').textContent = $('system-panel').open ? '收起' : '查看详情'; });
  $('refresh').addEventListener('click', () => { showNotice(''); loadProjects(); loadSystem(); });
  $('result-input').addEventListener('change', event => { $('result-folder').value = ''; selectionFrom(event.target); });
  $('result-folder').addEventListener('change', event => { $('result-input').value = ''; selectionFrom(event.target); });
  $('upload').addEventListener('click', uploadResults);
  $('pause-upload').addEventListener('click', () => state.abort?.abort());
  $('message-form').addEventListener('submit', async event => {
    event.preventDefault();
    if (state.busy || state.messageSending || !state.selected) return;
    const id = state.selected, value = $('message-input').value.trim();
    if (!value) { showNotice('请输入需要告知客户的内容。', true); return; }
    state.messageSending = true; setBusy(false);
    try {
      const key = await operationKey('message', id, value);
      await request(`admin/projects/${encodeURIComponent(id)}/messages`, { method: 'POST', headers: { 'Idempotency-Key': key.key }, body: { text: value } });
      clearOperationKey(key);
      if (state.selected === id) { $('message-input').value = ''; await selectProject(id, true, true); }
      showNotice('留言已发送。');
    } catch (error) {
      if (error?.name === 'TypeError' || error?.name === 'TimeoutError') showNotice('网络异常，留言状态尚未确认；请刷新记录后重试相同内容。', true);
      else showError(error);
    }
    finally { state.messageSending = false; setBusy(false); }
  });
  $('reset-password').addEventListener('click', () => {
    const user = state.detail?.project?.user;
    if (state.busy || state.messageSending || !user?.id) return;
    $('reset-user').textContent = user.username || '未知账户';
    $('reset-form').reset(); $('reset-error').hidden = true;
    if (typeof $('reset-dialog').showModal !== 'function') { showNotice('当前浏览器不支持安全对话框，请更新浏览器。', true); return; }
    $('reset-dialog').showModal();
  });
  function closeReset() { if (!state.resetting) $('reset-dialog').close(); }
  $('reset-close').addEventListener('click', closeReset);
  $('reset-cancel').addEventListener('click', closeReset);
  $('reset-dialog').addEventListener('cancel', event => { if (state.resetting) event.preventDefault(); });
  $('reset-dialog').addEventListener('close', () => { $('reset-form').reset(); $('reset-error').textContent = ''; $('reset-error').hidden = true; });
  $('reset-form').addEventListener('submit', async event => {
    event.preventDefault();
    const id = state.detail?.project?.user?.id;
    if (state.resetting || !id) return;
    const password = $('reset-secret').value;
    const confirmation = $('reset-confirm').value;
    const problem = password.length < 10 || password.length > 128 || new TextEncoder().encode(password).length > 512
      ? '临时密码需为 10–128 个字符。' : password !== confirmation ? '两次输入的临时密码不一致。' : '';
    if (problem) { $('reset-error').textContent = problem; $('reset-error').hidden = false; return; }
    state.resetting = true;
    for (const id of ['reset-secret', 'reset-confirm', 'reset-submit', 'reset-cancel', 'reset-close']) $(id).disabled = true;
    $('reset-error').hidden = true;
    try {
      const result = await request(`admin/users/${encodeURIComponent(id)}/reset-password`, { method: 'POST', body: { new_password: password } });
      if (result.ok !== true) throw new Error('服务器未确认密码重置，请重试。');
      state.resetting = false; $('reset-dialog').close();
      showNotice('临时密码已重置，请通过安全渠道告知用户；用户下次登录时需要修改密码。');
    } catch (error) {
      $('reset-error').textContent = userError(error); $('reset-error').hidden = false;
    } finally {
      $('reset-secret').value = ''; $('reset-confirm').value = '';
      state.resetting = false;
      for (const id of ['reset-secret', 'reset-confirm', 'reset-submit', 'reset-cancel', 'reset-close']) $(id).disabled = false;
    }
  });
  $('status-form').addEventListener('submit', async event => {
    event.preventDefault();
    if (state.busy || state.messageSending || !state.detail || !canUpdateStatus()) return;
    const status = $('status-input').value;
    if (status === 'delivered' && !(state.detail.result_files || []).some(file => file.state === 'complete')) { showNotice('请先上传成果文件，再将项目设为已交付。', true); return; }
    if (status === 'delivered' && (state.detail.result_files || []).some(file => file.state !== 'complete')) { showNotice('请先完成或取消所有成果上传，再将项目设为已交付。', true); return; }
    const id = state.selected, payload = { status, message: $('status-message').value.trim() };
    state.messageSending = true; setBusy(false);
    try {
      const key = await operationKey('status', id, JSON.stringify(payload));
      await request(`admin/projects/${encodeURIComponent(id)}`, { method: 'PATCH', headers: { 'Idempotency-Key': key.key }, body: payload });
      clearOperationKey(key);
      state.messageSending = false; setBusy(false); await loadProjects(true); showNotice('项目状态已更新。');
    } catch (error) {
      if (error?.name === 'TypeError' || error?.name === 'TimeoutError') showNotice('网络异常，状态更新是否保存尚未确认；请刷新项目后再操作。', true);
      else showError(error);
    }
    finally { state.messageSending = false; setBusy(false); }
  });
  $('logout').addEventListener('click', async () => {
    if (state.busy || state.messageSending) { showNotice('请等待当前操作完成，再退出登录。', true); return; }
    try { await request('auth/logout', { method: 'POST' }); window.location.assign('/workspace/'); }
    catch (error) { showError(error); }
  });
  window.addEventListener('beforeunload', event => { if (state.busy || state.messageSending || state.resetting) { event.preventDefault(); event.returnValue = ''; } });

  (async () => {
    try {
      const result = await request('auth/me');
      if (!result?.csrf_token || !result?.user) throw new Error('登录信息不完整，请重新登录。');
      state.csrf = result.csrf_token;
      if (result.user.must_change_password) {
        showNotice('首次登录需要先修改密码，请返回用户工作空间完成修改。', true);
        const link = make('a', '前往修改密码'); link.href = '/workspace/#password'; link.className = 'notice-link'; $('notice').append(' ', link);
        $('project-list').replaceChildren(make('p', '修改密码后即可管理项目。', 'empty'));
        return;
      }
      await Promise.all([loadProjects(false), loadSystem()]);
    } catch (error) { showError(error); $('project-list').replaceChildren(make('p', '请先以管理员账号登录。', 'empty')); }
  })();
})();
