"""Single-node HTTP service; bind to loopback behind a TLS reverse proxy."""
import argparse
import calendar
import hashlib
import hmac
import json
import mimetypes
import os
import re
import socket
import socketserver
import shutil
import sys
import threading
import time
import urllib.parse
import zipfile
from http import cookies
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from datetime import datetime

from .store import Store, WorkspaceError, CHUNK_SIZE, DATASET_LIMIT, USER_LIMIT, digest, fail

PREFIX = '/workspace/'
API = PREFIX + 'api/'
ROOT = Path(__file__).resolve().parent.parent
UUID = r'[0-9a-f-]{36}'
TOKEN = r'[A-Za-z0-9_-]{20,128}'
STATIC = {
    '/workspace/': ('frontend/index.html','text/html; charset=utf-8'),
    '/workspace/app.js': ('frontend/app.js','application/javascript; charset=utf-8'),
    '/workspace/styles.css': ('frontend/styles.css','text/css; charset=utf-8'),
    '/workspace/assets/tersus-logo.png': ('frontend/assets/tersus-logo.png','image/png'),
    '/workspace/assets/auth-space-generic.svg': ('frontend/assets/auth-space-generic.svg','image/svg+xml'),
    '/workspace/public.js': ('frontend/public.js','application/javascript; charset=utf-8'),
    '/workspace/admin/': ('admin/index.html','text/html; charset=utf-8'),
    '/workspace/admin/admin.js': ('admin/admin.js','application/javascript; charset=utf-8'),
    '/workspace/admin/admin.css': ('admin/admin.css','text/css; charset=utf-8'),
    '/workspace/viewer/': ('viewer/index.html','text/html; charset=utf-8'),
    '/workspace/viewer/viewer.js': ('viewer/viewer.js','application/javascript; charset=utf-8'),
    '/workspace/viewer/viewer.css': ('viewer/viewer.css','text/css; charset=utf-8'),
    '/workspace/viewer/vendor/three.module.js': ('viewer/vendor/three.module.js','application/javascript; charset=utf-8'),
    '/workspace/viewer/vendor/three.core.js': ('viewer/vendor/three.core.js','application/javascript; charset=utf-8'),
    '/workspace/viewer/vendor/addons/controls/OrbitControls.js': ('viewer/vendor/addons/controls/OrbitControls.js','application/javascript; charset=utf-8'),
    '/workspace/viewer/vendor/addons/loaders/GLTFLoader.js': ('viewer/vendor/addons/loaders/GLTFLoader.js','application/javascript; charset=utf-8'),
    '/workspace/viewer/vendor/addons/utils/BufferGeometryUtils.js': ('viewer/vendor/addons/utils/BufferGeometryUtils.js','application/javascript; charset=utf-8'),
}
CSP = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' blob: data:; connect-src 'self'; frame-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'"


class ThreadedServer(socketserver.ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 128

    def server_close(self):
        if getattr(self,'cleanup_stop',None): self.cleanup_stop.set()
        super().server_close()
        if getattr(self,'instance_lock',None): self.instance_lock.close()


class ChunkWriter:
    def __init__(self, target): self.target, self.position = target, 0
    def write(self, data):
        if data:
            self.target.write(('%x\r\n' % len(data)).encode('ascii'))
            self.target.write(data); self.target.write(b'\r\n'); self.position += len(data)
        return len(data)
    def tell(self): return self.position
    def flush(self): self.target.flush()
    def close(self): pass
    def seekable(self): return False


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    server_version = 'Workspace'
    sys_version = ''
    store = None
    origin = None
    secure = True
    trusted_proxy = False
    ingress = threading.BoundedSemaphore(12)
    egress = threading.BoundedSemaphore(6)

    def setup(self):
        super().setup()
        self.connection.settimeout(120)
        self._sent = False

    def log_message(self, format, *args):
        # Paths may include private share tokens, so do not write request URLs to logs.
        return

    def send_response(self, code, message=None):
        self._sent = True
        super().send_response(code, message)

    def headers_common(self):
        self.send_header('X-Content-Type-Options','nosniff')
        self.send_header('Referrer-Policy','no-referrer')
        self.send_header('X-Robots-Tag','noindex, nofollow')
        self.send_header('X-Frame-Options','DENY')
        self.send_header('Cache-Control','private, no-store')
        if self.close_connection: self.send_header('Connection','close')

    def respond(self, status, body, content_type='application/json; charset=utf-8', extra=None):
        if not isinstance(body, bytes):
            body = json.dumps(body,ensure_ascii=False,separators=(',',':')).encode('utf-8')
        self.send_response(status)
        self.headers_common()
        self.send_header('Content-Type',content_type)
        self.send_header('Content-Length',str(len(body)))
        if extra:
            for key,value in extra.items(): self.send_header(key,value)
        self.end_headers()
        if self.command != 'HEAD': self.wfile.write(body)

    def error(self, status, code, message):
        self.respond(status,{'error':{'code':code,'message':message}})

    def body(self, limit):
        if self.headers.get('Transfer-Encoding'):
            fail(400,'INVALID_REQUEST','不支持此请求编码')
        raw = self.headers.get('Content-Length','0')
        if not re.fullmatch(r'\d{1,12}',raw):
            fail(400,'INVALID_REQUEST','请求长度无效')
        length = int(raw)
        if length > limit:
            fail(413,'REQUEST_TOO_LARGE','请求数据过大')
        data = self.rfile.read(length)
        if len(data)!=length:
            fail(400,'INCOMPLETE_REQUEST','网络中断，请重试')
        return data

    def json_body(self, limit=65536):
        if self.headers.get('Content-Type','').split(';',1)[0].strip().lower()!='application/json':
            fail(415,'INVALID_CONTENT_TYPE','请求格式不正确')
        raw = self.body(limit)
        try: value=json.loads(raw.decode('utf-8'))
        except (ValueError,UnicodeError): fail(400,'INVALID_JSON','请求内容不是有效的 JSON')
        if not isinstance(value,dict): fail(400,'INVALID_JSON','请求内容必须是对象')
        return value,digest(raw)

    def cookie_session(self):
        jar = cookies.SimpleCookie()
        try: jar.load(self.headers.get('Cookie',''))
        except cookies.CookieError: return None
        return self.store.session(jar['ws_session'].value) if 'ws_session' in jar else None

    def require_session(self, mutation=False, admin=False, allow_change=False):
        session = self.cookie_session()
        if not session: fail(401,'LOGIN_REQUIRED','请先登录')
        if mutation:
            token = self.headers.get('X-CSRF-Token','')
            if not hmac.compare_digest(token,session['csrf']): fail(403,'CSRF_INVALID','登录状态已变化，请刷新后重试')
        if admin and session['role']!='admin': fail(403,'FORBIDDEN','需要管理员权限')
        if session['must_change'] and not allow_change:
            fail(403,'MUST_CHANGE_PASSWORD','请先修改初始或重置后的密码')
        return session

    def check_origin(self):
        if self.headers.get('Origin','') != self.origin:
            fail(403,'ORIGIN_INVALID','请求来源不受信任，请从工作空间页面重试')

    def ip_key(self):
        address = self.headers.get('X-Real-IP','') if self.trusted_proxy else self.client_address[0]
        return digest(address.encode('utf-8'))

    def auth_result(self, uid):
        token, csrf = self.store.new_session(uid)
        session = self.store.session(token)
        user = {'id':session['id'],'username':session['username'],'role':session['role'],'must_change_password':bool(session['must_change'])}
        cookie = 'ws_session='+token+'; Path=/workspace/; HttpOnly; SameSite=Strict; Max-Age=604800'
        if self.secure: cookie += '; Secure'
        return {'user':user,'csrf_token':csrf,'quota':self.store.quota(uid)},cookie

    def me(self, session):
        return {'user':{'id':session['id'],'username':session['username'],'role':session['role'],
                        'must_change_password':bool(session['must_change'])},'csrf_token':session['csrf'],'quota':self.store.quota(session['id'])}

    def static(self, path):
        item = STATIC.get(path)
        if re.fullmatch('/workspace/public/'+TOKEN+'/?',path): item=('frontend/public.html','text/html; charset=utf-8')
        if not item: fail(404,'NOT_FOUND','页面不存在')
        file = ROOT / item[0]
        if not file.is_file(): fail(404,'NOT_FOUND','页面尚未安装')
        self.respond(200,file.read_bytes(),item[1],{'Content-Security-Policy':CSP})

    def stream_file(self, info, preview=False):
        chunks = info['chunks']; size = info['size']; start, end = 0, size-1
        if preview:
            first = b''
            if chunks:
                with self.store.chunk_path(info['upload_id'],info['id'],0).open('rb') as f: first=f.read(16)
            if first.startswith(b'\x89PNG\r\n\x1a\n'): kind='image/png'
            elif first.startswith(b'\xff\xd8\xff'): kind='image/jpeg'
            elif first[:6] in (b'GIF87a',b'GIF89a'): kind='image/gif'
            elif first.startswith(b'RIFF') and first[8:12]==b'WEBP': kind='image/webp'
            elif first.startswith(b'%PDF-'): kind='application/pdf'
            elif first.startswith(b'glTF') and info['path'].lower().endswith('.glb'): kind='model/gltf-binary'
            else: fail(415,'PREVIEW_UNAVAILABLE','此文件暂不支持在线预览，请下载后查看')
        else: kind='application/octet-stream'
        etag='"'+info['sha256']+'"' if info.get('sha256') else None
        requested = self.headers.get('Range')
        if requested and self.headers.get('If-Range') and self.headers.get('If-Range')!=etag: requested=None
        status = 200
        if requested:
            match = re.fullmatch(r'bytes=(\d*)-(\d*)', requested.strip())
            if not match or size==0 or not (match.group(1) or match.group(2)):
                self.respond(416,b'',kind,{'Content-Range':'bytes */'+str(size)});return
            if match.group(1):
                start=int(match.group(1));end=min(size-1,int(match.group(2))) if match.group(2) else size-1
            else:
                suffix=int(match.group(2));start=max(0,size-suffix);end=size-1
            if start>=size or end<start:
                self.respond(416,b'',kind,{'Content-Range':'bytes */'+str(size)});return
            status=206
        filename = info['path'].split('/')[-1]
        disposition = ('inline' if preview else 'attachment') + "; filename=\"download\"; filename*=UTF-8''" + urllib.parse.quote(filename,safe='')
        self.send_response(status); self.headers_common()
        self.send_header('Content-Type',kind)
        self.send_header('Content-Length',str(max(0,end-start+1)))
        self.send_header('Accept-Ranges','bytes')
        if etag:self.send_header('ETag',etag)
        self.send_header('Content-Disposition',disposition)
        if preview: self.send_header('Content-Security-Policy',"sandbox; default-src 'none'")
        if status==206: self.send_header('Content-Range','bytes %d-%d/%d' % (start,end,size))
        self.end_headers()
        if self.command=='HEAD' or size==0: return
        for ch in chunks:
            offset=ch['idx']*info['chunk_size']
            if offset>end or offset+ch['size']<=start: continue
            p=self.store.chunk_path(info['upload_id'],info['id'],ch['idx'])
            data=p.read_bytes()
            if len(data)!=ch['size'] or not hmac.compare_digest(hashlib.sha256(data).hexdigest(),ch['sha256']):
                raise IOError('file integrity check failed')
            local_start=max(0,start-offset);local_end=min(ch['size'],end-offset+1)
            self.wfile.write(data[local_start:local_end])

    def zip_raw(self, actor, pid):
        detail = self.store.detail(actor,pid,admin=True)
        files = [x for x in detail['raw_files'] if x['state']=='complete']
        if not files: fail(409,'RAW_NOT_READY','原始数据尚未完整提交')
        infos=[self.store.file_info(f['id'],actor=actor,admin=True) for f in files]
        self.send_response(200);self.headers_common()
        self.send_header('Content-Type','application/zip')
        self.send_header('Content-Disposition','attachment; filename="s1-'+pid+'.zip"')
        self.send_header('Transfer-Encoding','chunked')
        self.end_headers()
        if self.command=='HEAD': return
        writer=ChunkWriter(self.wfile)
        with zipfile.ZipFile(writer,'w',compression=zipfile.ZIP_STORED,allowZip64=True) as archive:
            for f in infos:
                with archive.open(f['path'],'w',force_zip64=True) as target:
                    for ch in f['chunks']:
                        data=self.store.chunk_path(f['upload_id'],f['id'],ch['idx']).read_bytes()
                        if len(data)!=ch['size'] or not hmac.compare_digest(hashlib.sha256(data).hexdigest(),ch['sha256']):
                            raise IOError('file integrity check failed')
                        target.write(data)
        self.wfile.write(b'0\r\n\r\n')

    def run_route(self):
        path=urllib.parse.urlsplit(self.path).path
        method=self.command
        if method in ('GET','HEAD'): self.body(0)
        if method in ('GET','HEAD') and not path.startswith(API):
            if path in ('/workspace','/workspace/admin','/workspace/viewer'):
                self.respond(308,b'', 'text/plain', {'Location':path+'/'});return
            self.static(path);return
        if not path.startswith(API): fail(404,'NOT_FOUND','接口不存在')
        rel=path[len(API):]
        if method in ('GET','HEAD'):
            if rel=='config':
                self.respond(200,{'chunk_size':CHUNK_SIZE,'max_chunk_size':16*1024*1024,'dataset_limit':DATASET_LIMIT,'user_limit':USER_LIMIT});return
            match=re.fullmatch('public/('+TOKEN+')',rel)
            if match: self.respond(200,self.store.public_project(match.group(1)));return
            match=re.fullmatch('public/('+TOKEN+')/files/('+UUID+')/(download|preview)',rel)
            if match:
                info=self.store.file_info(match.group(2),public_token=match.group(1))
                with self.egress:self.stream_file(info,preview=match.group(3)=='preview')
                return
        if method not in ('GET','HEAD'):
            self.check_origin()
        if method=='POST' and rel in ('auth/register','auth/login'):
            data,_=self.json_body()
            if rel.endswith('register'):
                self.store.guard_registration(self.ip_key())
                uid=self.store.create_user(data.get('username'),data.get('password'))
            else: uid=self.store.authenticate(data.get('username'),data.get('password'),self.ip_key())
            response,cookie=self.auth_result(uid);self.respond(200,response,extra={'Set-Cookie':cookie});return
        if method in ('GET','HEAD') and rel=='auth/me':
            self.respond(200,self.me(self.require_session(allow_change=True)));return
        if method=='POST' and rel=='auth/logout':
            self.body(0)
            actor=self.require_session(mutation=True,allow_change=True);self.store.logout(actor)
            cookie='ws_session=; Path=/workspace/; HttpOnly; SameSite=Strict; Max-Age=0'
            if self.secure:cookie+='; Secure'
            self.respond(200,{'ok':True},extra={'Set-Cookie':cookie});return
        if method=='POST' and rel=='auth/password':
            actor=self.require_session(mutation=True,allow_change=True);data,_=self.json_body()
            self.store.change_password(actor,data.get('current_password'),data.get('new_password'))
            self.respond(200,{'ok':True});return
        admin = rel.startswith('admin/')
        actor=self.require_session(mutation=method not in ('GET','HEAD'),admin=admin)
        if method in ('GET','HEAD') and rel=='projects': self.respond(200,self.store.projects(actor));return
        if method=='POST' and rel=='projects':
            data,body_hash=self.json_body()
            self.respond(200,self.store.create_project(actor,data,self.headers.get('Idempotency-Key',''),body_hash));return
        if method in ('GET','HEAD') and rel=='admin/projects': self.respond(200,self.store.projects(actor,admin=True));return
        if method in ('GET','HEAD') and rel=='admin/system':
            usage=shutil.disk_usage(str(self.store.root));backup={'status':'never','last_success':None,'bytes':0,'message':'同机备份尚未运行。'}
            try:
                status_path=self.store.root/'backup-status.json'
                if status_path.is_file():
                    value=json.loads(status_path.read_text(encoding='utf-8'))
                    if value.get('status') in ('ok','failed'):
                        backup=value
                        if backup['status']=='ok':
                            stamp=calendar.timegm(datetime.strptime(backup['last_success'],'%Y-%m-%dT%H:%M:%SZ').timetuple())
                            if time.time()-stamp>36*3600:
                                backup=dict(backup,status='failed',message='超过 36 小时没有成功备份，请检查服务器。')
            except (OSError,ValueError,AttributeError,KeyError,TypeError):
                backup={'status':'failed','last_success':None,'bytes':0,'message':'备份状态无法读取，请检查服务器。'}
            with self.store.db() as c:
                logical=c.execute("SELECT COALESCE(SUM(u.total_size),0) FROM uploads u JOIN projects p ON p.id=u.project_id WHERE u.status IN ('uploading','complete') AND p.deleted=0").fetchone()[0]
            self.respond(200,{'storage':{'free':usage.free,'total':usage.total,'workspace_used':logical,'workspace_limit':self.store.max_capacity},'backup':backup});return
        match=re.fullmatch('admin/users/('+UUID+')/reset-password',rel)
        if match and method=='POST':
            data,_=self.json_body();self.respond(200,self.store.reset_user_password(actor,match.group(1),data.get('new_password')));return
        match=re.fullmatch('(admin/)?projects/('+UUID+')',rel)
        if match:
            adm,pid=bool(match.group(1)),match.group(2)
            if method in ('GET','HEAD'): self.respond(200,self.store.detail(actor,pid,admin=adm));return
            if method=='PATCH':
                data,body_hash=self.json_body();self.respond(200,self.store.update_project(actor,pid,data,admin=adm,key=self.headers.get('Idempotency-Key'),payload_hash=body_hash));return
            if method=='DELETE' and not adm: self.body(0);self.respond(200,self.store.delete_project(actor,pid));return
        match=re.fullmatch('(admin/)?projects/('+UUID+')/messages',rel)
        if match and method=='POST':
            data,body_hash=self.json_body()
            self.respond(200,self.store.post_message(actor,match.group(2),data,self.headers.get('Idempotency-Key',''),body_hash,admin=bool(match.group(1))));return
        match=re.fullmatch('projects/('+UUID+')/uploads',rel)
        if match and method=='POST':
            data,body_hash=self.json_body(16*1024*1024)
            self.respond(200,self.store.create_upload(actor,match.group(1),data,self.headers.get('Idempotency-Key',''),body_hash));return
        match=re.fullmatch('uploads/('+UUID+')',rel)
        if match and method in ('GET','HEAD'): self.respond(200,self.store.get_upload(actor,match.group(1)));return
        match=re.fullmatch('uploads/('+UUID+')/(complete|cancel)',rel)
        if match and method=='POST':
            self.body(0)
            uid,action=match.groups()
            self.respond(200,self.store.complete_upload(actor,uid) if action=='complete' else self.store.cancel_upload(actor,uid));return
        match=re.fullmatch(r'uploads/('+UUID+r')/files/('+UUID+r')/chunks/(\d{1,8})',rel)
        if match and method=='PUT':
            with self.ingress:
                data=self.body(16*1024*1024)
                self.respond(200,self.store.put_chunk(actor,match.group(1),match.group(2),int(match.group(3)),data,self.headers.get('X-Chunk-SHA256','').lower()))
            return
        match=re.fullmatch('(admin/)?files/('+UUID+')/(download|preview)',rel)
        if match and method in ('GET','HEAD'):
            adm=bool(match.group(1))
            info=self.store.file_info(match.group(2),actor=actor,admin=adm)
            with self.egress:self.stream_file(info,preview=match.group(3)=='preview')
            return
        match=re.fullmatch(r'admin/projects/('+UUID+r')/raw\.zip',rel)
        if match and method=='GET':
            with self.egress:self.zip_raw(actor,match.group(1))
            return
        match=re.fullmatch(r'admin/projects/('+UUID+r')/raw\.sha256',rel)
        if match and method=='GET':
            detail=self.store.detail(actor,match.group(1),admin=True)
            files=[f for f in detail['raw_files'] if f['state']=='complete']
            if not files or any(not f['sha256'] for f in files): fail(409,'RAW_NOT_READY','原始数据尚未完整提交')
            payload=''.join(f['sha256']+'  '+f['path']+'\n' for f in files).encode('utf-8')
            self.respond(200,payload,'text/plain; charset=utf-8',{'Content-Disposition':'attachment; filename="s1-checksums.sha256"'})
            return
        fail(404,'NOT_FOUND','接口不存在')

    def dispatch(self):
        self._sent=False
        try: self.run_route()
        except WorkspaceError as exc:
            self.close_connection=True
            if not self._sent:
                try: self.error(exc.status,exc.code,exc.message)
                except (BrokenPipeError,ConnectionResetError): pass
        except (BrokenPipeError,ConnectionResetError,socket.timeout): self.close_connection=True
        except Exception:
            self.close_connection=True
            if not self._sent:
                try: self.error(500,'SERVER_ERROR','服务暂时不可用，请稍后重试')
                except (BrokenPipeError,ConnectionResetError): pass
            # No request paths, names, tokens, or secrets in logs.
            print('workspace request failed',file=sys.stderr,flush=True)

    do_GET=dispatch
    do_HEAD=dispatch
    do_POST=dispatch
    do_PUT=dispatch
    do_PATCH=dispatch
    do_DELETE=dispatch


def instance_lock(root):
    Path(root).mkdir(parents=True,exist_ok=True)
    lock=(Path(root)/'.workspace-service.lock').open('a+b')
    try:
        if os.name=='nt':
            import msvcrt
            lock.seek(0,os.SEEK_END)
            if lock.tell()==0: lock.write(b'0');lock.flush()
            lock.seek(0);msvcrt.locking(lock.fileno(),msvcrt.LK_NBLCK,1)
        else:
            import fcntl
            fcntl.flock(lock.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
    except (OSError,IOError):
        lock.close();raise RuntimeError('Workspace data directory already has a running service')
    return lock


def serve(root, origin, host='127.0.0.1', port=8840, min_free=20_000_000_000, trusted_proxy=False, max_capacity=80_000_000_000):
    if host not in ('127.0.0.1','::1','localhost'):
        raise ValueError('Workspace service must bind to loopback')
    if not origin.startswith(('https://','http://127.0.0.1:','http://localhost:')):
        raise ValueError('A trusted HTTPS or local development origin is required')
    if min_free < 0 or max_capacity < 1: raise ValueError('invalid storage limits')
    lock=instance_lock(root)
    try:
        handler=type('WorkspaceHandler',(Handler,),{})
        handler.store=Store(root,min_free=min_free,max_capacity=max_capacity);handler.origin=origin.rstrip('/')
        handler.secure=handler.origin.startswith('https://');handler.trusted_proxy=trusted_proxy
        handler.store.recover_cleanup()
        server=ThreadedServer((host,port),handler);server.instance_lock=lock;server.cleanup_stop=threading.Event()
        def maintenance():
            while not server.cleanup_stop.wait(60):
                try: handler.store.recover_cleanup()
                except Exception: print('workspace cleanup will retry',file=sys.stderr,flush=True)
        threading.Thread(target=maintenance,daemon=True).start()
        return server
    except BaseException:
        lock.close();raise


def main():
    parser=argparse.ArgumentParser(description='Isolated S1 customer workspace')
    parser.add_argument('--data',required=True)
    parser.add_argument('--origin',required=True)
    parser.add_argument('--port',type=int,default=8840)
    parser.add_argument('--min-free',type=int,default=20_000_000_000)
    parser.add_argument('--max-capacity',type=int,default=80_000_000_000)
    parser.add_argument('--trusted-proxy',action='store_true')
    args=parser.parse_args()
    httpd=serve(args.data,args.origin,port=args.port,min_free=args.min_free,trusted_proxy=args.trusted_proxy,max_capacity=args.max_capacity)
    print('Workspace service listening on loopback:%d' % args.port,flush=True)
    httpd.serve_forever()


if __name__=='__main__': main()
