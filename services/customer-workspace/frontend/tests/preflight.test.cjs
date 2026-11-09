/* Isolated synthetic tests of the actual browser preflight and resume helpers. */
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {webcrypto} = require('node:crypto');

const source = fs.readFileSync(path.join(__dirname,'../app.js'),'utf8');
const end = source.indexOf("  $('login-tab').addEventListener");
assert.ok(end > 0, 'application helper boundary');
const elements = new Map();
const document = {getElementById(id) {
  if (!elements.has(id)) elements.set(id,{webkitdirectory:true,style:{},setAttribute(){},textContent:'',hidden:false});
  return elements.get(id);
}};
const context = {document,window:{},crypto:webcrypto,TextEncoder,DOMException,AbortController,performance,console};
vm.runInNewContext(source.slice(0,end)+"window.helpers={selectedFolder,s1Hint,validSelection,verifyExisting,uploadSession,validateSupplement};})();",context);
const {selectedFolder,s1Hint,validSelection,verifyExisting,uploadSession,validateSupplement}=context.window.helpers;
const fake=(name,size)=>({name:name.split('/').at(-1),webkitRelativePath:name,size,lastModified:1});

test('5 GB is accepted exactly, overage and combined overage are rejected',()=>{
  assert.equal(selectedFolder([fake('scan/a',5_000_000_000)]).total,5_000_000_000);
  assert.throws(()=>selectedFolder([fake('scan/a',5_000_000_001)]),/超过单个任务 5 GB/);
  assert.throws(()=>selectedFolder([fake('scan/a',4_999_999_999),fake('scan/b',2)]),/超过单个任务 5 GB/);
});
test('empty and duplicate paths fail before creating an upload',()=>{
  assert.throws(()=>selectedFolder([]),/包含文件/);
  assert.throws(()=>selectedFolder([fake('scan/a',0)]),/没有可上传/);
  assert.throws(()=>selectedFolder([fake('scan/a',1),fake('scan/a',1)]),/重复/);
  assert.throws(()=>selectedFolder([fake('scan/../a',1)]),/路径/);
  assert.throws(()=>selectedFolder([fake('scan/CON.txt',1)]),/路径/);
  assert.throws(()=>selectedFolder([fake('scan/file:name',1)]),/路径/);
  assert.throws(()=>selectedFolder([fake('scan/A',1),fake('scan/a',1)]),/冲突/);
});
test('S1 hint accepts common case-insensitive metadata and nested capture variants without claiming completeness',()=>{
  let files=selectedFolder([fake('scan/metadata.yaml',1),fake('scan/data/data_raw.mcap',1)]);
  assert.match(s1Hint(files.rows),/关键文件.*完整性仍需/);
  files=selectedFolder([fake('scan/METADATA.YML',1),fake('scan/data.mcap',1)]);
  assert.match(s1Hint(files.rows),/关键文件.*完整性仍需/);
  files=selectedFolder([fake('scan/metadata.yaml',1)]);
  assert.match(s1Hint(files.rows),/未发现常见 S1 文件/);
});
test('resuming rejects paths or sizes that do not match',()=>{
  const session={chunk_size:4,files:[{path:'scan/a',size:4,expected_hashes:['0'.repeat(64)]}]};
  assert.doesNotThrow(()=>validSelection(session,selectedFolder([fake('scan/a',4)])));
  assert.throws(()=>validSelection(session,selectedFolder([fake('scan/a',5)])),/文件不匹配/);
  assert.throws(()=>validSelection(session,selectedFolder([fake('scan/b',4)])),/文件不匹配/);
  session.files[0].expected_hashes=[];
  assert.throws(()=>validSelection(session,selectedFolder([fake('scan/a',4)])),/无法确认文件校验清单/);
});
test('supplementary data respects the whole project limit and cannot overwrite a path',()=>{
  const selected=selectedFolder([fake('scan/new',1)]);
  assert.doesNotThrow(()=>validateSupplement(selected,[{path:'scan/old',size:4_999_999_999}],5_000_000_000));
  assert.throws(()=>validateSupplement(selected,[{path:'scan/old',size:5_000_000_000}],5_000_000_000),/合计.*超过项目/);
  assert.throws(()=>validateSupplement(selected,[{path:'SCAN/NEW',size:1}],5_000_000_000),/路径.*冲突/);
  assert.throws(()=>validateSupplement(selected,[{path:'scan/new/child',size:1}],5_000_000_000),/路径.*冲突/);
});
test('resuming compares existing chunk hashes and stops on changed content',async()=>{
  const blob=new Blob(['abcd']);
  const row={file:blob,path:'scan/a',size:4};
  const chosen=new Map([['scan/a',row]]);
  const good='88d4266fd4e6338d13b845fcf289579d209c897823b9217da3e161936f031589';
  const session={chunk_size:4,files:[{path:'scan/a',size:4,uploaded_chunks:[0],expected_hashes:[good],chunk_hashes:{'0':good}}]};
  assert.equal(await verifyExisting(session,chosen,4,new AbortController().signal),4);
  const changed=new Map([['scan/a',{file:new Blob(['abce']),path:'scan/a',size:4}]]);
  await assert.rejects(verifyExisting(session,changed,4,new AbortController().signal),/文件内容与上次上传不一致/);
  session.files[0].chunk_hashes={};
  await assert.rejects(verifyExisting(session,chosen,4,new AbortController().signal),/无法校验/);
});
test('resuming rejects changes in a previously unuploaded chunk before sending it',async()=>{
  const original='88d4266fd4e6338d13b845fcf289579d209c897823b9217da3e161936f031589';
  const changed=new Blob(['abce']);
  const session={id:'u',chunk_size:8*1024*1024,files:[{id:'f',path:'scan/a',size:4,uploaded_chunks:[],chunk_hashes:{},expected_hashes:[original]}]};
  const selected={rows:[{file:changed,path:'scan/a',size:4,last_modified:1}],total:4};
  await assert.rejects(uploadSession(session,selected),/文件内容与最初选择时不一致/);
});
