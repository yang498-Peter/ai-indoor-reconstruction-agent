(() => {
  'use strict';
  const $=id=>document.getElementById(id);
  const node=(tag,className,text)=>{ const el=document.createElement(tag); if(className)el.className=className; if(text!==undefined)el.textContent=text; return el; };
  const bytes=value=>{const n=Number(value)||0;if(n>=1e9)return (n/1e9).toFixed(2)+' GB';if(n>=1e6)return (n/1e6).toFixed(1)+' MB';if(n>=1e3)return (n/1e3).toFixed(1)+' KB';return n+' B';};
  const match=location.pathname.match(/^\/workspace\/public\/([A-Za-z0-9_-]+)\/?$/);
  async function start() {
    if (!match) throw new Error('分享链接无效。');
    const token=match[1], base='/workspace/api/public/'+encodeURIComponent(token);
    let response;
    try { response=await fetch(base,{credentials:'omit',cache:'no-store'}); }
    catch { throw new Error('网络连接失败，请稍后重试。'); }
    if (response.status===404 || response.status===403) throw new Error('分享不存在或已被取消公开。');
    if (!response.ok) throw new Error('暂时无法读取分享成果，请稍后重试。');
    const data=await response.json(); $('public-title').textContent=data.project?.name || '空间成果';
    document.title=$('public-title').textContent+' · 公开成果';
    const list=$('public-results'), files=Array.isArray(data.result_files) ? data.result_files : [];
    if (!files.length) list.append(node('div','results-empty','分享者尚未公开可用成果。'));
    for (const file of files) {
      if (!file?.id) continue;
      const name=String(file.path || '交付文件'), lower=name.toLowerCase();
      const baseFile=base+'/files/'+encodeURIComponent(file.id), card=node('article','result-card public-result');
      if (/\.(png|jpe?g|webp|gif)$/.test(lower)) {
        const image=document.createElement('img'); image.className='result-preview'; image.src=baseFile+'/preview'; image.alt='分享成果图片预览'; image.loading='lazy'; image.addEventListener('error',()=>image.remove()); card.append(image);
      }
      if (/\.pdf$/.test(lower)) {
        const preview=node('a','result-preview-link','打开 PDF ↗'); preview.href=baseFile+'/preview'; preview.target='_blank'; preview.rel='noopener noreferrer'; card.append(preview);
      }
      if (/\.glb$/.test(lower)) {
        const preview=node('a','result-model-link','在线查看三维模型 ↗'); preview.href='/workspace/viewer/?share='+encodeURIComponent(token)+'&file='+encodeURIComponent(file.id); preview.target='_blank'; preview.rel='noopener noreferrer'; card.append(preview);
      }
      const row=node('div','result-item'), info=node('span'); info.append(node('strong','',name),node('small','',bytes(file.size)));
      const download=node('a','result-download','下载 ↗'); download.href=baseFile+'/download'; download.setAttribute('download',''); row.append(info,download); card.append(row); list.append(card);
    }
    $('public-loading').hidden=true; $('public-content').hidden=false;
  }
  start().catch(error=>{ $('public-loading').textContent=error.message || '暂时无法读取分享成果，请稍后重试。'; });
})();
