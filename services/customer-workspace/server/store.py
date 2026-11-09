"""Durable, quota-enforced metadata and chunk storage for manual delivery.

The database and chunks must live outside any public static directory.
Only server-generated identifiers are used as filesystem path components.
"""
import contextlib
import errno
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import sqlite3
import tempfile
import threading
import time
import unicodedata
import uuid
from datetime import datetime, timezone
from pathlib import Path
from .locks import disk_budget, LockBusy

CHUNK_SIZE = 8 * 1024 * 1024
DATASET_LIMIT = 5_000_000_000
USER_LIMIT = 20_000_000_000
MAX_FILES = 100_000
OPTIONS = frozenset(('pointcloud', 'model', 'cad', 'ifc', 'sketchup'))
STATUSES = frozenset(('submitted', 'processing', 'needs_info', 'delivered'))
HEX_SHA = re.compile(r'^[a-f0-9]{64}$')
SAFE_KEY = re.compile(r'^[A-Za-z0-9_-]{16,128}$')


class WorkspaceError(Exception):
    def __init__(self, status, code, message):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def fail(status, code, message):
    raise WorkspaceError(status, code, message)


def now():
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def ident():
    return str(uuid.uuid4())


def digest(value):
    return hashlib.sha256(value).hexdigest()


def sync_dir(path):
    if os.name!='nt':
        descriptor=os.open(str(path),os.O_RDONLY|getattr(os,'O_DIRECTORY',0))
        try:os.fsync(descriptor)
        finally:os.close(descriptor)


def clean_name(value, maximum, label):
    if not isinstance(value, str):
        fail(400, 'INVALID_INPUT', label + '格式不正确')
    value = unicodedata.normalize('NFC', value).strip()
    if not value or len(value) > maximum or any(ord(c) < 32 or ord(c) == 127 for c in value):
        fail(400, 'INVALID_INPUT', label + '不能为空或超过长度限制')
    return value


def user_name(value):
    value = clean_name(value, 48, '账号')
    if len(value) < 3 or any(c.isspace() or c in '/\\:' for c in value):
        fail(400, 'INVALID_USERNAME', '账号需为 3–48 位，不能包含空格、斜杠或冒号')
    return value


def password(value):
    if not isinstance(value, str) or len(value) < 10 or len(value) > 128 or len(value.encode('utf-8')) > 512:
        fail(400, 'INVALID_PASSWORD', '密码长度需为 10–128 个字符')
    return value


def password_hash(value, salt):
    return hashlib.scrypt(value.encode('utf-8'), salt=bytes.fromhex(salt), n=16384, r=8, p=1, dklen=32, maxmem=64 * 1024 * 1024).hex()


def valid_path(value):
    if not isinstance(value, str) or not value or len(value.encode('utf-8')) > 2048:
        fail(400, 'INVALID_PATH', '文件相对路径无效或过长')
    if value.startswith('/') or '\\' in value or any(ord(c) < 32 or ord(c) == 127 for c in value):
        fail(400, 'INVALID_PATH', '文件路径包含不允许的字符')
    parts = value.split('/')
    reserved = re.compile(r'^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?$', re.IGNORECASE)
    if any(not p or p in ('.', '..') or len(p.encode('utf-8')) > 255 or p[-1] in ' .' or
           any(c in '<>:"|?*' for c in p) or reserved.fullmatch(p) for p in parts):
        fail(400, 'INVALID_PATH', '文件路径不安全，请重新选择文件夹')
    return value


def validate_windows_paths(paths):
    seen, prefixes = set(), {}
    for path in paths:
        parts=valid_path(path).split('/')
        canonical='/'.join(unicodedata.normalize('NFC',x).casefold() for x in parts)
        if canonical in seen: fail(400,'DUPLICATE_PATH','文件路径在 Windows 上冲突：'+path[:120])
        for i in range(1,len(parts)+1):
            original='/'.join(parts[:i])
            normalized='/'.join(unicodedata.normalize('NFC',x).casefold() for x in parts[:i])
            if normalized in prefixes and prefixes[normalized]!=original:
                fail(400,'DUPLICATE_PATH','目录或文件路径在 Windows 上冲突：'+path[:120])
            prefixes[normalized]=original
        seen.add(canonical)
    for canonical in seen:
        pieces=canonical.split('/')
        if any('/'.join(pieces[:i]) in seen for i in range(1,len(pieces))):
            fail(400,'INVALID_PATH','文件与目录路径冲突，请检查原始文件夹')


class Store:
    def __init__(self, root, min_free=20_000_000_000, max_capacity=80_000_000_000):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.chunk_root = self.root / 'chunks'
        self.chunk_root.mkdir(exist_ok=True)
        self.database = self.root / 'workspace.sqlite3'
        self.min_free = int(min_free)
        self.max_capacity = int(max_capacity)
        self._locks = {}
        self._locks_guard = threading.Lock()
        with self.db() as c:
            version=c.execute('PRAGMA user_version').fetchone()[0]
            if version>1: raise RuntimeError('Workspace database schema is newer than this service')
            c.execute('PRAGMA journal_mode=WAL')
            c.executescript('''
                CREATE TABLE IF NOT EXISTS users (
                    id TEXT PRIMARY KEY, username TEXT NOT NULL, username_key TEXT NOT NULL UNIQUE,
                    salt TEXT NOT NULL, password_hash TEXT NOT NULL, role TEXT NOT NULL,
                    must_change INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sessions (
                    token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id),
                    csrf TEXT NOT NULL, expires INTEGER NOT NULL, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS auth_attempts (
                    ip TEXT NOT NULL, username_key TEXT NOT NULL, attempted INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS auth_attempts_time ON auth_attempts(attempted);
                CREATE TABLE IF NOT EXISTS projects (
                    id TEXT PRIMARY KEY, owner_id TEXT NOT NULL REFERENCES users(id), name TEXT NOT NULL,
                    options TEXT NOT NULL, note TEXT NOT NULL, status TEXT NOT NULL, message TEXT NOT NULL,
                    is_public INTEGER NOT NULL DEFAULT 0, public_token TEXT UNIQUE,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL, deleted INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS project_owner ON projects(owner_id, deleted);
                CREATE TABLE IF NOT EXISTS uploads (
                    id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
                    created_by TEXT NOT NULL REFERENCES users(id), kind TEXT NOT NULL, status TEXT NOT NULL,
                    chunk_size INTEGER NOT NULL, total_size INTEGER NOT NULL, created_at TEXT NOT NULL,
                    cleanup_done INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS files (
                    id TEXT PRIMARY KEY, upload_id TEXT NOT NULL REFERENCES uploads(id),
                    project_id TEXT NOT NULL REFERENCES projects(id), kind TEXT NOT NULL,
                    path TEXT NOT NULL, size INTEGER NOT NULL, last_modified INTEGER NOT NULL DEFAULT 0,
                    sha256 TEXT, released INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS files_upload ON files(upload_id);
                CREATE TABLE IF NOT EXISTS expected_chunks (
                    file_id TEXT NOT NULL REFERENCES files(id), idx INTEGER NOT NULL, sha256 TEXT NOT NULL,
                    PRIMARY KEY(file_id, idx)
                );
                CREATE TABLE IF NOT EXISTS chunks (
                    file_id TEXT NOT NULL REFERENCES files(id), idx INTEGER NOT NULL, size INTEGER NOT NULL,
                    sha256 TEXT NOT NULL, PRIMARY KEY(file_id, idx)
                );
                CREATE TABLE IF NOT EXISTS idempotency (
                    user_id TEXT NOT NULL, scope TEXT NOT NULL, key TEXT NOT NULL, request_hash TEXT NOT NULL,
                    response TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(user_id, scope, key)
                );
                CREATE TABLE IF NOT EXISTS audit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, actor_id TEXT, project_id TEXT,
                    action TEXT NOT NULL, detail TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS messages (
                    id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
                    author_id TEXT NOT NULL REFERENCES users(id), text TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS messages_project ON messages(project_id,created_at);
            ''')
            required={'files':{'sha256','released'},'uploads':{'chunk_size','cleanup_done'},
                      'projects':{'public_token','deleted'},'expected_chunks':{'file_id','idx','sha256'}}
            for table,columns in required.items():
                present={row['name'] for row in c.execute('PRAGMA table_info('+table+')')}
                if not columns.issubset(present):
                    raise RuntimeError('Workspace database requires an explicit schema migration')
            c.execute('PRAGMA user_version=1')

    @contextlib.contextmanager
    def db(self):
        conn = sqlite3.connect(str(self.database), timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA foreign_keys=ON')
        conn.execute('PRAGMA busy_timeout=30000')
        conn.execute('PRAGMA synchronous=FULL')
        try:
            yield conn
        finally:
            conn.close()

    @contextlib.contextmanager
    def tx(self):
        with self.db() as conn:
            conn.execute('BEGIN IMMEDIATE')
            try:
                yield conn
                conn.execute('COMMIT')
            except BaseException:
                conn.execute('ROLLBACK')
                raise

    def lock(self, key):
        with self._locks_guard:
            if key not in self._locks:
                self._locks[key] = threading.RLock()
            return self._locks[key]

    @contextlib.contextmanager
    def budget_lock(self):
        try:
            with disk_budget(self.root,blocking=False,timeout=3):yield
        except LockBusy:
            fail(503,'BACKUP_BUSY','服务器正在备份，新上传暂不可创建，请稍后重试；已有上传可以继续')

    def record(self, conn, actor, project, action, detail=''):
        conn.execute('INSERT INTO audit(actor_id,project_id,action,detail,created_at) VALUES(?,?,?,?,?)',
                     (actor, project, action, detail[:1000], now()))

    def create_user(self, username, secret, role='user', must_change=False):
        username = user_name(username)
        secret = password(secret)
        if role not in ('user', 'admin'):
            fail(400, 'INVALID_ROLE', '账号类型无效')
        salt, uid = secrets.token_hex(16), ident()
        hashed = password_hash(secret, salt)
        try:
            with self.tx() as c:
                c.execute('INSERT INTO users VALUES(?,?,?,?,?,?,?,?)',
                          (uid, username, username.casefold(), salt, hashed, role, int(must_change), now()))
                self.record(c, uid, None, 'register' if role == 'user' else 'admin_bootstrap')
        except sqlite3.IntegrityError:
            fail(409, 'USERNAME_TAKEN', '这个账号已存在，请直接登录或更换账号')
        return uid

    def authenticate(self, username, secret, ip):
        name = username if isinstance(username, str) else ''
        name = unicodedata.normalize('NFC', name.strip())[:48].casefold()
        instant = int(time.time())
        with self.tx() as c:
            c.execute('DELETE FROM auth_attempts WHERE attempted < ?', (instant - 3600,))
            total = c.execute('SELECT count(*) FROM auth_attempts WHERE ip=? AND attempted>?', (ip, instant - 900)).fetchone()[0]
            target = c.execute('SELECT count(*) FROM auth_attempts WHERE username_key=? AND attempted>?', (name, instant - 900)).fetchone()[0]
            if total >= 20 or target >= 12:
                fail(429, 'TOO_MANY_ATTEMPTS', '尝试次数过多，请 15 分钟后再试')
            row = c.execute('SELECT * FROM users WHERE username_key=?', (name,)).fetchone()
            valid = False
            if row and isinstance(secret, str) and len(secret) <= 128:
                try:
                    valid = hmac.compare_digest(password_hash(secret, row['salt']), row['password_hash'])
                except (ValueError, UnicodeError):
                    valid = False
            else:
                # Avoid a cheap username-existence timing oracle.
                password_hash('invalid-password', '00' * 16)
            if not valid:
                c.execute('INSERT INTO auth_attempts VALUES(?,?,?)', (ip, name, instant))
            else:
                c.execute('DELETE FROM auth_attempts WHERE ip=? AND username_key=?', (ip, name))
        if not valid:
            fail(401, 'INVALID_CREDENTIALS', '账号或密码不正确')
        return row['id']

    def guard_registration(self, ip):
        instant = int(time.time())
        with self.tx() as c:
            c.execute('DELETE FROM auth_attempts WHERE attempted < ?', (instant-3600,))
            count = c.execute("SELECT count(*) FROM auth_attempts WHERE ip=? AND username_key='*register*' AND attempted>?", (ip,instant-3600)).fetchone()[0]
            if count >= 8:
                fail(429, 'TOO_MANY_ATTEMPTS', '注册次数过多，请稍后再试')
            c.execute("INSERT INTO auth_attempts VALUES(?,'*register*',?)",(ip,instant))

    def new_session(self, uid):
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        with self.tx() as c:
            c.execute('DELETE FROM sessions WHERE expires<?', (int(time.time()),))
            c.execute('INSERT INTO sessions VALUES(?,?,?,?,?)', (digest(token.encode('ascii')), uid, csrf, int(time.time()) + 7 * 86400, now()))
        return token, csrf

    def session(self, token):
        if not isinstance(token, str) or len(token) > 128:
            return None
        with self.db() as c:
            row = c.execute('SELECT users.id,username,role,must_change,csrf,token_hash FROM sessions JOIN users ON users.id=sessions.user_id WHERE token_hash=? AND expires>?',
                            (digest(token.encode('utf-8')), int(time.time()))).fetchone()
            return dict(row) if row else None

    def logout(self, session):
        with self.tx() as c:
            c.execute('DELETE FROM sessions WHERE token_hash=?', (session['token_hash'],))

    def change_password(self, session, current, new):
        new = password(new)
        with self.tx() as c:
            row = c.execute('SELECT * FROM users WHERE id=?', (session['id'],)).fetchone()
            if not isinstance(current, str) or len(current)>128 or len(current.encode('utf-8'))>512 or not hmac.compare_digest(password_hash(current, row['salt']), row['password_hash']):
                fail(401, 'INVALID_CREDENTIALS', '当前密码不正确')
            salt = secrets.token_hex(16)
            c.execute('UPDATE users SET salt=?,password_hash=?,must_change=0 WHERE id=?', (salt, password_hash(new, salt), session['id']))
            c.execute('DELETE FROM sessions WHERE user_id=? AND token_hash<>?', (session['id'], session['token_hash']))
            self.record(c, session['id'], None, 'password_changed')

    def reset_user_password(self, actor, uid, new):
        new=password(new);salt=secrets.token_hex(16);hashed=password_hash(new,salt)
        with self.tx() as c:
            target=c.execute('SELECT id,role FROM users WHERE id=?',(uid,)).fetchone()
            if not target or target['role']!='user':
                fail(404,'NOT_FOUND','用户不存在或不能通过此入口重置')
            c.execute('UPDATE users SET salt=?,password_hash=?,must_change=1 WHERE id=?',(salt,hashed,uid))
            c.execute('DELETE FROM sessions WHERE user_id=?',(uid,))
            self.record(c,actor['id'],None,'user_password_reset',uid)
        return {'ok':True}

    def quota(self, uid, c=None):
        if c is None:
            with self.db() as conn:
                return self.quota(uid, conn)
        rows = c.execute('''SELECT u.status,COALESCE(SUM(f.size),0) amount FROM files f
            JOIN uploads u ON u.id=f.upload_id JOIN projects p ON p.id=f.project_id
            WHERE p.owner_id=? AND p.deleted=0 AND u.status IN ('complete','uploading') GROUP BY u.status''', (uid,)).fetchall()
        amounts = {r['status']: r['amount'] for r in rows}
        used, reserved = amounts.get('complete', 0), amounts.get('uploading', 0)
        return {'limit': USER_LIMIT, 'used': used, 'reserved': reserved, 'available': max(0, USER_LIMIT-used-reserved)}

    def get_project(self, c, pid, actor, admin=False):
        row = c.execute('SELECT p.*, u.username owner_name FROM projects p JOIN users u ON u.id=p.owner_id WHERE p.id=? AND p.deleted=0', (pid,)).fetchone()
        if not row or (not admin and row['owner_id'] != actor['id']):
            fail(404, 'NOT_FOUND', '项目不存在或无权访问')
        return row

    def project_view(self, c, row):
        raw, result = c.execute('''SELECT COALESCE(SUM(CASE WHEN f.kind='raw' THEN f.size ELSE 0 END),0),
            COALESCE(SUM(CASE WHEN f.kind='result' THEN f.size ELSE 0 END),0) FROM files f
            JOIN uploads u ON u.id=f.upload_id WHERE f.project_id=? AND u.status='complete' ''', (row['id'],)).fetchone()
        return {'id': row['id'], 'name': row['name'], 'options': json.loads(row['options']), 'note': row['note'],
                'status': row['status'], 'message': row['message'], 'is_public': bool(row['is_public']),
                'public_url': '/workspace/public/' + row['public_token'] if row['is_public'] and row['public_token'] else None,
                'created_at': row['created_at'], 'updated_at': row['updated_at'],
                'user': {'id': row['owner_id'], 'username': row['owner_name']}, 'raw_bytes': raw, 'result_bytes': result}

    def projects(self, actor, admin=False):
        with self.db() as c:
            if admin:
                rows = c.execute('SELECT p.*,u.username owner_name FROM projects p JOIN users u ON u.id=p.owner_id WHERE p.deleted=0 ORDER BY p.updated_at DESC').fetchall()
            else:
                rows = c.execute('SELECT p.*,u.username owner_name FROM projects p JOIN users u ON u.id=p.owner_id WHERE p.owner_id=? AND p.deleted=0 ORDER BY p.updated_at DESC', (actor['id'],)).fetchall()
            return {'projects': [self.project_view(c, r) for r in rows], 'quota': self.quota(actor['id'], c)}

    def replay(self, c, actor, scope, key, payload_hash):
        if not isinstance(key, str) or not SAFE_KEY.fullmatch(key):
            fail(400, 'IDEMPOTENCY_REQUIRED', '请刷新页面后重试，创建请求缺少安全标识')
        row = c.execute('SELECT request_hash,response FROM idempotency WHERE user_id=? AND scope=? AND key=?', (actor['id'], scope, key)).fetchone()
        if row:
            if row['request_hash'] != payload_hash:
                fail(409, 'IDEMPOTENCY_CONFLICT', '请求标识已用于不同内容，请重新开始')
            return json.loads(row['response'])
        return None

    def save_replay(self, c, actor, scope, key, payload_hash, response):
        c.execute('INSERT INTO idempotency VALUES(?,?,?,?,?,?)',
                  (actor['id'], scope, key, payload_hash, json.dumps(response, ensure_ascii=False), now()))

    def create_project(self, actor, data, key, payload_hash):
        name = clean_name(data.get('name'), 120, '项目名称')
        opts = data.get('options')
        if not isinstance(opts, list) or not opts or len(opts) > len(OPTIONS) or any(not isinstance(x, str) or x not in OPTIONS for x in opts) or len(set(opts)) != len(opts):
            fail(400, 'INVALID_OPTIONS', '请选择至少一种需要交付的数据')
        note = data.get('note', '')
        if not isinstance(note, str) or len(note) > 2000:
            fail(400, 'INVALID_NOTE', '备注不能超过 2000 个字符')
        with self.tx() as c:
            replay = self.replay(c, actor, 'project', key, payload_hash)
            if replay is not None:
                return replay
            pid, stamp = ident(), now()
            c.execute('INSERT INTO projects VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                      (pid, actor['id'], name, json.dumps(opts), note, 'draft', '', 0, None, stamp, stamp, 0))
            row = self.get_project(c, pid, actor)
            response = {'project': self.project_view(c, row)}
            self.save_replay(c, actor, 'project', key, payload_hash, response)
            self.record(c, actor['id'], pid, 'project_created')
            return response

    def detail(self, actor, pid, admin=False):
        with self.db() as c:
            row = self.get_project(c, pid, actor, admin)
            entries = c.execute('''SELECT f.id,f.path,f.size,f.sha256,f.kind,f.released,u.status state FROM files f JOIN uploads u ON u.id=f.upload_id
                WHERE f.project_id=? AND u.status IN ('uploading','complete') ORDER BY f.path''', (pid,)).fetchall()
            uploads = c.execute('SELECT id,kind,status,total_size,created_at FROM uploads WHERE project_id=? AND status IN (\'uploading\',\'complete\') ORDER BY created_at', (pid,)).fetchall()
            def file_view(r): return {'id': r['id'], 'path': r['path'], 'size': r['size'], 'state': r['state'], 'sha256':r['sha256'], 'is_delivered':bool(r['released'])}
            upload_views=[]
            for up in uploads:
                if not admin and up['kind']=='result': continue
                info=dict(up)
                info['files']=[{'id':f['id'],'path':f['path'],'size':f['size']} for f in c.execute('SELECT id,path,size FROM files WHERE upload_id=? ORDER BY path',(up['id'],))]
                upload_views.append(info)
            messages=c.execute('''SELECT m.id,m.text,m.created_at,u.role FROM messages m JOIN users u ON u.id=m.author_id
                WHERE m.project_id=? ORDER BY m.created_at,m.rowid''',(pid,)).fetchall()
            return {'project': self.project_view(c, row), 'raw_files': [file_view(r) for r in entries if r['kind']=='raw'],
                    'result_files': [file_view(r) for r in entries if r['kind']=='result' and (admin or (r['state']=='complete' and r['released']))],
                    'uploads': upload_views, 'messages':[{'id':m['id'],'author':m['role'],'text':m['text'],'created_at':m['created_at']} for m in messages],
                    'quota': self.quota(row['owner_id'], c)}

    def manifest(self, data):
        if data.get('chunk_size') != CHUNK_SIZE:
            fail(409,'CONFIG_CHANGED','上传配置已变化，请刷新页面并重新校验文件')
        rows = data.get('files')
        if not isinstance(rows, list) or not rows or len(rows) > MAX_FILES:
            fail(400, 'INVALID_MANIFEST', '请选择包含文件的文件夹，最多支持 100000 个文件')
        files, total = [], 0
        for row in rows:
            if not isinstance(row, dict):
                fail(400, 'INVALID_MANIFEST', '文件清单格式不正确')
            path, size, modified = valid_path(row.get('path')), row.get('size'), row.get('last_modified', 0)
            if isinstance(size, bool) or not isinstance(size, int) or size < 0 or size > DATASET_LIMIT:
                fail(400, 'INVALID_SIZE', '文件大小无效或超过 5 GB')
            if isinstance(modified, bool) or not isinstance(modified, int) or modified < 0 or modified > 2**53:
                fail(400, 'INVALID_MANIFEST', '文件时间格式不正确')
            expected=row.get('expected_hashes')
            if not isinstance(expected,list) or len(expected)!=(size+CHUNK_SIZE-1)//CHUNK_SIZE or any(not isinstance(x,str) or not HEX_SHA.fullmatch(x) for x in expected):
                fail(400,'INVALID_MANIFEST','文件初始校验清单不完整，请重新选择文件夹并等待校验')
            total += size; files.append((ident(), path, size, modified, expected))
        validate_windows_paths([f[1] for f in files])
        if total == 0:
            fail(400,'INVALID_SIZE','所选数据为空，请选择包含实际内容的文件夹')
        if total > DATASET_LIMIT:
            fail(413, 'DATASET_TOO_LARGE', '本次数据超过 5 GB 上限，请选择更小的数据')
        return files, total

    def create_upload(self, actor, pid, data, key, payload_hash):
        kind = data.get('kind')
        if kind not in ('raw', 'result'):
            fail(400, 'INVALID_KIND', '上传类型无效')
        files, total = self.manifest(data)
        with self.lock('project:'+pid), self.budget_lock():
            with self.tx() as c:
                scope = 'upload:'+pid
                replay = self.replay(c, actor, scope, key, payload_hash)
                if replay is not None:
                    return replay
                admin = actor['role'] == 'admin'
                project = self.get_project(c, pid, actor, admin=admin)
                if kind == 'result' and not admin:
                    fail(403, 'FORBIDDEN', '只有管理员可以上传交付成果')
                if kind == 'result' and project['is_public']:
                    fail(409, 'PUBLIC_PROJECT', '项目正在公开，请先由客户关闭公开后再上传新成果')
                if kind == 'raw' and (admin or project['owner_id'] != actor['id']):
                    fail(403, 'FORBIDDEN', '只有项目所有者可以上传原始数据')
                if kind == 'raw':
                    if c.execute("SELECT 1 FROM uploads WHERE project_id=? AND kind='raw' AND status='uploading'", (pid,)).fetchone():
                        fail(409, 'RAW_ALREADY_EXISTS', '已有原始数据上传，请继续或取消已有上传')
                    previous=c.execute("SELECT COALESCE(SUM(total_size),0) FROM uploads WHERE project_id=? AND kind='raw' AND status='complete'",(pid,)).fetchone()[0]
                    if previous and project['status']!='needs_info':
                        fail(409,'RAW_ALREADY_EXISTS','原始数据已提交；需要补充时请联系处理团队')
                    if previous+total>DATASET_LIMIT:
                        fail(413,'DATASET_TOO_LARGE','项目原始数据合计超过 5 GB，无法继续补充')
                    paths=[r['path'] for r in c.execute("SELECT f.path FROM files f JOIN uploads u ON u.id=f.upload_id WHERE f.project_id=? AND f.kind='raw' AND u.status='complete'",(pid,))]
                    validate_windows_paths(paths+[f[1] for f in files])
                if kind == 'result' and not c.execute("SELECT 1 FROM uploads WHERE project_id=? AND kind='raw' AND status='complete'", (pid,)).fetchone():
                    fail(409, 'RAW_NOT_READY', '原始数据尚未完整提交')
                quota = self.quota(project['owner_id'], c)
                if total > quota['available']:
                    fail(413, 'QUOTA_EXCEEDED', '用户空间不足，请删除不需要的项目后重试')
                global_used=c.execute("SELECT COALESCE(SUM(u.total_size),0) FROM uploads u JOIN projects p ON p.id=u.project_id WHERE u.status IN ('uploading','complete') AND p.deleted=0").fetchone()[0]
                if global_used+total>self.max_capacity:
                    fail(507,'CAPACITY_FULL','工作空间整体容量暂时不足，请联系处理团队')
                outstanding = c.execute("""SELECT COALESCE(SUM(u.total_size),0) FROM uploads u JOIN projects p ON p.id=u.project_id WHERE u.status='uploading' AND p.deleted=0""").fetchone()[0]
                uploaded = c.execute("""SELECT COALESCE(SUM(ch.size),0) FROM chunks ch JOIN files f ON f.id=ch.file_id JOIN uploads u ON u.id=f.upload_id JOIN projects p ON p.id=u.project_id WHERE u.status='uploading' AND p.deleted=0""").fetchone()[0]
                if shutil.disk_usage(str(self.root)).free < self.min_free + max(0, outstanding-uploaded) + total + CHUNK_SIZE:
                    fail(507, 'SERVER_SPACE_LOW', '服务器可用空间不足，请稍后联系管理员')
                uid = ident()
                c.execute('INSERT INTO uploads(id,project_id,created_by,kind,status,chunk_size,total_size,created_at) VALUES(?,?,?,?,?,?,?,?)', (uid, pid, actor['id'], kind, 'uploading', CHUNK_SIZE, total, now()))
                c.executemany('INSERT INTO files(id,upload_id,project_id,kind,path,size,last_modified) VALUES(?,?,?,?,?,?,?)', [(fid, uid, pid, kind, path, size, modified) for fid,path,size,modified,expected in files])
                c.executemany('INSERT INTO expected_chunks(file_id,idx,sha256) VALUES(?,?,?)',
                              [(fid,idx,value) for fid,path,size,modified,expected in files for idx,value in enumerate(expected)])
                if kind == 'raw':
                    c.execute("UPDATE projects SET status='uploading',updated_at=? WHERE id=?", (now(),pid))
                response = {'upload': self.upload_view(c, uid)}
                self.save_replay(c, actor, scope, key, payload_hash, response)
                self.record(c, actor['id'], pid, 'upload_created', kind)
                return response

    def upload_view(self, c, uid):
        up = c.execute('SELECT * FROM uploads WHERE id=?', (uid,)).fetchone()
        if not up:
            fail(404, 'NOT_FOUND', '上传任务不存在')
        files = c.execute('SELECT id,path,size,last_modified FROM files WHERE upload_id=? ORDER BY path', (uid,)).fetchall()
        chunks = c.execute('SELECT ch.file_id,ch.idx,ch.sha256 FROM chunks ch JOIN files f ON f.id=ch.file_id WHERE f.upload_id=? ORDER BY ch.idx', (uid,)).fetchall()
        expected = c.execute('SELECT ch.file_id,ch.idx,ch.sha256 FROM expected_chunks ch JOIN files f ON f.id=ch.file_id WHERE f.upload_id=? ORDER BY ch.idx', (uid,)).fetchall()
        by_file = {}
        for chunk in chunks:
            by_file.setdefault(chunk['file_id'], {})[str(chunk['idx'])] = chunk['sha256']
        planned = {}
        for chunk in expected:
            planned.setdefault(chunk['file_id'], []).append(chunk['sha256'])
        result = []
        for row in files:
            hashes = by_file.get(row['id'], {})
            result.append({'id':row['id'],'path':row['path'],'size':row['size'],'last_modified':row['last_modified'],
                           'uploaded_chunks': sorted(int(x) for x in hashes), 'chunk_hashes': hashes,
                           'expected_hashes': planned.get(row['id'],[])})
        return {'id':uid,'kind':up['kind'],'status':up['status'],'chunk_size':up['chunk_size'],'total_size':up['total_size'],'files':result}

    def authorized_upload(self, c, actor, uid):
        row = c.execute('SELECT u.*,p.owner_id,p.deleted FROM uploads u JOIN projects p ON p.id=u.project_id WHERE u.id=?', (uid,)).fetchone()
        if not row or row['deleted'] or (actor['role'] != 'admin' and row['owner_id'] != actor['id']):
            fail(404, 'NOT_FOUND', '上传任务不存在或无权访问')
        if row['kind']=='result' and actor['role']!='admin':
            fail(404,'NOT_FOUND','上传任务不存在或无权访问')
        if row['kind'] == 'raw' and actor['role'] == 'admin' and row['created_by'] != actor['id']:
            # Administrators can inspect a completed upload, but cannot change its source chunks.
            return row
        return row

    def get_upload(self, actor, uid):
        with self.db() as c:
            self.authorized_upload(c, actor, uid)
            return {'upload': self.upload_view(c, uid)}

    def chunk_path(self, uid, fid, idx):
        return self.chunk_root / uid / fid / ('%08d.chunk' % idx)

    def put_chunk(self, actor, uid, fid, idx, data, expected_hash):
        if not isinstance(idx, int) or idx < 0 or not isinstance(expected_hash, str) or not HEX_SHA.fullmatch(expected_hash):
            fail(400, 'INVALID_CHUNK', '分片编号或校验值无效')
        actual = digest(data)
        if not hmac.compare_digest(actual, expected_hash):
            fail(422, 'CHECKSUM_MISMATCH', '分片校验失败，请重新上传')
        with self.lock('upload:'+uid):
            with self.db() as c:
                up = self.authorized_upload(c, actor, uid)
                if up['created_by'] != actor['id']:
                    fail(403, 'FORBIDDEN', '只有上传发起人可以写入分片')
                f = c.execute('SELECT * FROM files WHERE id=? AND upload_id=?', (fid,uid)).fetchone()
                if not f:
                    fail(404, 'NOT_FOUND', '上传文件不存在')
                chunk_size=up['chunk_size']
                count = (f['size'] + chunk_size-1)//chunk_size
                if idx >= count or len(data) != min(chunk_size, f['size']-idx*chunk_size):
                    fail(400, 'INVALID_CHUNK', '分片大小或编号不正确')
                planned=c.execute('SELECT sha256 FROM expected_chunks WHERE file_id=? AND idx=?',(fid,idx)).fetchone()
                if not planned or not hmac.compare_digest(planned['sha256'],actual):
                    fail(409,'SOURCE_CHANGED','文件内容与最初选择时不一致，请重新选择原文件或取消后重新上传')
                old = c.execute('SELECT * FROM chunks WHERE file_id=? AND idx=?', (fid,idx)).fetchone()
                if old:
                    if old['size'] != len(data) or not hmac.compare_digest(old['sha256'], actual):
                        fail(409, 'CHUNK_CONFLICT', '已上传分片与当前文件不同，请取消后重新上传')
                    try:
                        on_disk=self.chunk_path(uid,fid,idx).read_bytes()
                        if len(on_disk)==len(data) and hmac.compare_digest(digest(on_disk),actual):
                            return {'received':True,'already_present':True}
                    except FileNotFoundError:
                        pass
                    except OSError:
                        fail(503,'STORAGE_UNAVAILABLE','服务器暂时无法读取上传数据，请稍后重试')
                if up['status'] != 'uploading' and not old:
                    fail(409, 'UPLOAD_CLOSED', '上传任务已结束')
            path = self.chunk_path(uid,fid,idx)
            temp = None
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                # Network and fsync I/O happen outside the database write transaction.
                with tempfile.NamedTemporaryFile(dir=str(path.parent), prefix='.incoming-', delete=False) as handle:
                    temp = handle.name; handle.write(data); handle.flush(); os.fsync(handle.fileno())
                with self.tx() as c:
                    up = self.authorized_upload(c, actor, uid)
                    if up['created_by'] != actor['id']: fail(403,'FORBIDDEN','只有上传发起人可以写入分片')
                    existing=c.execute('SELECT * FROM chunks WHERE file_id=? AND idx=?',(fid,idx)).fetchone()
                    if not existing and up['status']!='uploading': fail(409,'UPLOAD_CLOSED','上传任务已结束')
                    if existing and (existing['size']!=len(data) or not hmac.compare_digest(existing['sha256'],actual)):
                        fail(409,'CHUNK_CONFLICT','已上传分片与当前文件不同，请取消后重新上传')
                    os.replace(temp, str(path)); temp = None
                    sync_dir(path.parent)
                    if not existing: c.execute('INSERT INTO chunks VALUES(?,?,?,?)', (fid,idx,len(data),actual))
            except OSError as exc:
                if exc.errno in (errno.ENOSPC, getattr(errno,'EDQUOT',errno.ENOSPC)):
                    fail(507,'SERVER_SPACE_LOW','服务器空间不足，上传已保留，请稍后继续')
                raise
            except sqlite3.OperationalError as exc:
                if 'full' in str(exc).lower(): fail(507,'SERVER_SPACE_LOW','服务器空间不足，上传已保留，请稍后继续')
                raise
            finally:
                if temp and os.path.exists(temp): os.unlink(temp)
            return {'received':True,'already_present':False}

    def complete_upload(self, actor, uid):
        with self.lock('upload:'+uid):
            with self.db() as c:
                up = self.authorized_upload(c, actor, uid)
                if up['created_by'] != actor['id']:
                    fail(403, 'FORBIDDEN', '只有上传发起人可以完成上传')
                if up['status'] == 'complete':
                    return {'upload': self.upload_view(c, uid)}
                if up['status'] != 'uploading':
                    fail(409, 'UPLOAD_CLOSED', '上传任务已取消')
                if up['kind']=='result' and c.execute('SELECT is_public FROM projects WHERE id=?',(up['project_id'],)).fetchone()['is_public']:
                    fail(409, 'PUBLIC_PROJECT', '项目正在公开，请先由客户关闭公开后再完成新成果')
                files = c.execute('SELECT id,size FROM files WHERE upload_id=?', (uid,)).fetchall()
                counts = {r['file_id']:(r['n'],r['total']) for r in c.execute('''SELECT ch.file_id,count(*) n,COALESCE(sum(ch.size),0) total
                    FROM chunks ch JOIN files f ON f.id=ch.file_id WHERE f.upload_id=? GROUP BY ch.file_id''', (uid,))}
                for f in files:
                    required = (f['size'] + up['chunk_size']-1)//up['chunk_size']
                    count, total = counts.get(f['id'],(0,0))
                    if count != required or total != f['size']:
                        fail(409, 'UPLOAD_INCOMPLETE', '仍有文件未上传完成，请继续上传')
                chunks = [dict(r) for r in c.execute('SELECT ch.file_id,ch.idx,ch.size,ch.sha256 FROM chunks ch JOIN files f ON f.id=ch.file_id WHERE f.upload_id=? ORDER BY ch.file_id,ch.idx', (uid,))]
            file_hashes = {f['id']: hashlib.sha256() for f in files}
            for ch in chunks:
                p = self.chunk_path(uid,ch['file_id'],ch['idx'])
                chunk_hash = hashlib.sha256(); actual_size = 0
                try:
                    with p.open('rb') as stream:
                        while True:
                            block = stream.read(1024*1024)
                            if not block: break
                            chunk_hash.update(block); file_hashes[ch['file_id']].update(block); actual_size += len(block)
                except FileNotFoundError:
                    actual_size = -1
                except OSError:
                    fail(503,'STORAGE_UNAVAILABLE','服务器暂时无法读取上传数据，请稍后重试')
                if actual_size != ch['size'] or not hmac.compare_digest(chunk_hash.hexdigest(),ch['sha256']):
                    # Keep the damaged on-disk bytes until the user explicitly retries or cancels.
                    # Removing only the confirmation causes a resumed client to retransmit this chunk.
                    with self.tx() as c:
                        c.execute('DELETE FROM chunks WHERE file_id=? AND idx=? AND sha256=?', (ch['file_id'],ch['idx'],ch['sha256']))
                    fail(409,'CHUNK_CORRUPT','服务器校验发现分片不完整，请重新选择原文件夹继续，系统会重传受影响的部分')
            with self.tx() as c:
                up = self.authorized_upload(c, actor, uid)
                if up['status'] != 'uploading': fail(409,'UPLOAD_CLOSED','上传任务已结束')
                if up['kind']=='result' and c.execute('SELECT is_public FROM projects WHERE id=?',(up['project_id'],)).fetchone()['is_public']:
                    fail(409, 'PUBLIC_PROJECT', '项目正在公开，请先由客户关闭公开后再完成新成果')
                c.executemany('UPDATE files SET sha256=? WHERE id=?', [(file_hashes[f['id']].hexdigest(),f['id']) for f in files])
                c.execute("UPDATE uploads SET status='complete' WHERE id=?", (uid,))
                if up['kind'] == 'raw':
                    c.execute("UPDATE projects SET status='submitted',updated_at=? WHERE id=?", (now(),up['project_id']))
                else:
                    c.execute('UPDATE projects SET updated_at=? WHERE id=?', (now(),up['project_id']))
                self.record(c, actor['id'], up['project_id'], 'upload_completed', up['kind'])
                return {'upload': self.upload_view(c, uid)}

    def remove_chunks(self, uid):
        # Only internally generated UUID paths are passed here; never use a customer filename.
        try:
            uuid.UUID(uid)
        except ValueError:
            raise ValueError('invalid internal upload identifier')
        path = self.chunk_root / uid
        if path.exists():
            shutil.rmtree(str(path))

    def cleanup_upload(self, uid):
        with self.lock('upload:'+uid):
            with self.db() as c:
                row=c.execute("SELECT status,cleanup_done FROM uploads WHERE id=?",(uid,)).fetchone()
                if not row or row['status']!='cancelled' or row['cleanup_done']: return True
            try:
                self.remove_chunks(uid)
            except OSError:
                return False
            with self.tx() as c:
                c.execute("UPDATE uploads SET cleanup_done=1 WHERE id=? AND status='cancelled'",(uid,))
            return True

    def recover_cleanup(self):
        # Only explicit user cancellations/deletions enter this queue.
        with self.db() as c:
            ids=[r['id'] for r in c.execute("SELECT id FROM uploads WHERE status='cancelled' AND cleanup_done=0")]
        return sum(not self.cleanup_upload(uid) for uid in ids)

    def erase_upload_metadata(self, c, uid, pid):
        c.execute('DELETE FROM chunks WHERE file_id IN (SELECT id FROM files WHERE upload_id=?)',(uid,))
        c.execute('DELETE FROM expected_chunks WHERE file_id IN (SELECT id FROM files WHERE upload_id=?)',(uid,))
        c.execute('DELETE FROM files WHERE upload_id=?',(uid,))
        rows=c.execute('SELECT user_id,scope,key,response FROM idempotency WHERE scope=?',('upload:'+pid,)).fetchall()
        for row in rows:
            if json.loads(row['response']).get('upload',{}).get('id')==uid:
                c.execute('DELETE FROM idempotency WHERE user_id=? AND scope=? AND key=?',(row['user_id'],row['scope'],row['key']))

    def cancel_upload(self, actor, uid):
        with self.lock('upload:'+uid):
            with self.tx() as c:
                up = self.authorized_upload(c, actor, uid)
                if up['created_by'] != actor['id']:
                    fail(403, 'FORBIDDEN', '只有上传发起人可以取消上传')
                if up['status'] == 'complete':
                    fail(409, 'UPLOAD_COMPLETE', '上传已完成，如需删除请删除整个项目')
                if up['status'] == 'uploading':
                    c.execute("UPDATE uploads SET status='cancelled' WHERE id=?", (uid,))
                    self.erase_upload_metadata(c,uid,up['project_id'])
                    if up['kind'] == 'raw':
                        previous=c.execute("SELECT 1 FROM uploads WHERE project_id=? AND kind='raw' AND status='complete'",(up['project_id'],)).fetchone()
                        c.execute('UPDATE projects SET status=?,updated_at=? WHERE id=?',('needs_info' if previous else 'draft',now(),up['project_id']))
                    self.record(c, actor['id'], up['project_id'], 'upload_cancelled', up['kind'])
            return {'ok':True,'storage_cleanup_pending':not self.cleanup_upload(uid)}

    def update_project(self, actor, pid, data, admin=False, key=None, payload_hash=None):
        with self.lock('project:'+pid):
            with self.tx() as c:
                p = self.get_project(c, pid, actor, admin=admin)
                if admin:
                    scope='status:'+pid
                    if key:
                        replay=self.replay(c,actor,scope,key,payload_hash)
                        if replay is not None:return replay
                    status, message = data.get('status'), data.get('message','')
                    if status not in STATUSES or not isinstance(message,str) or len(message)>2000:
                        fail(400, 'INVALID_STATUS', '项目状态或说明无效')
                    if not c.execute("SELECT 1 FROM uploads WHERE project_id=? AND kind='raw' AND status='complete'", (pid,)).fetchone():
                        fail(409, 'RAW_NOT_READY', '原始数据尚未完整提交')
                    if c.execute("SELECT 1 FROM uploads WHERE project_id=? AND kind='raw' AND status='uploading'",(pid,)).fetchone():
                        fail(409,'UPLOAD_IN_PROGRESS','原始数据仍在上传，请等待完成或取消')
                    if status == 'delivered' and not c.execute("SELECT 1 FROM uploads WHERE project_id=? AND kind='result' AND status='complete'", (pid,)).fetchone():
                        fail(409, 'RESULT_NOT_READY', '请先上传并完成至少一份成果')
                    if status == 'delivered' and c.execute("SELECT 1 FROM uploads WHERE project_id=? AND kind='result' AND status='uploading'", (pid,)).fetchone():
                        fail(409, 'UPLOAD_IN_PROGRESS', '请先完成或取消正在上传的成果，再确认交付')
                    if status == 'delivered' and p['is_public'] and c.execute("SELECT 1 FROM files WHERE project_id=? AND kind='result' AND released=0",(pid,)).fetchone():
                        fail(409,'PUBLIC_PROJECT','项目正在公开，请先由客户关闭公开后再交付新成果')
                    if status != 'delivered' and p['is_public']:
                        fail(409, 'PUBLIC_PROJECT', '项目正在公开，请先由客户关闭公开')
                    c.execute('UPDATE projects SET status=?,message=?,updated_at=? WHERE id=?', (status,message,now(),pid))
                    if message.strip() and (p['status']!=status or p['message']!=message):
                        c.execute('INSERT INTO messages VALUES(?,?,?,?,?)',(ident(),pid,actor['id'],message.strip(),now()))
                    if status=='delivered':
                        c.execute("UPDATE files SET released=1 WHERE project_id=? AND kind='result' AND upload_id IN (SELECT id FROM uploads WHERE project_id=? AND status='complete')",(pid,pid))
                    self.record(c,actor['id'],pid,'status_changed',status)
                else:
                    value = data.get('is_public')
                    if not isinstance(value,bool):
                        fail(400, 'INVALID_PUBLIC', '请选择是否公开成果')
                    if value and (p['status']!='delivered' or not c.execute("SELECT 1 FROM files WHERE project_id=? AND kind='result' AND released=1", (pid,)).fetchone()):
                        fail(409, 'RESULT_NOT_READY', '交付完成后才能公开成果')
                    if value and c.execute("SELECT 1 FROM uploads WHERE project_id=? AND kind='result' AND status='uploading'",(pid,)).fetchone():
                        fail(409, 'UPLOAD_IN_PROGRESS', '请等待管理员完成或取消正在上传的成果后再公开')
                    if value and c.execute("SELECT 1 FROM files WHERE project_id=? AND kind='result' AND released=0",(pid,)).fetchone():
                        fail(409, 'RESULT_NOT_READY', '存在未交付的新成果，请等待处理团队确认交付后再公开')
                    token = p['public_token'] if value and p['is_public'] else (secrets.token_urlsafe(24) if value else None)
                    c.execute('UPDATE projects SET is_public=?,public_token=?,updated_at=? WHERE id=?', (int(value),token,now(),pid))
                    self.record(c,actor['id'],pid,'public_enabled' if value else 'public_disabled')
                response={'project': self.project_view(c,self.get_project(c,pid,actor,admin=admin))}
                if admin and key:self.save_replay(c,actor,scope,key,payload_hash,response)
                return response

    def post_message(self,actor,pid,data,key,payload_hash,admin=False):
        text=data.get('text')
        if not isinstance(text,str) or not text.strip() or len(text)>2000 or any(ord(ch)<32 and ch not in '\n\r\t' for ch in text):
            fail(400,'INVALID_MESSAGE','留言不能为空或超过 2000 个字符')
        text=text.strip()
        with self.lock('project:'+pid):
            with self.tx() as c:
                self.get_project(c,pid,actor,admin=admin)
                scope='message:'+pid
                replay=self.replay(c,actor,scope,key,payload_hash)
                if replay is not None:return replay
                message={'id':ident(),'author':'admin' if admin else 'user','text':text,'created_at':now()}
                c.execute('INSERT INTO messages VALUES(?,?,?,?,?)',(message['id'],pid,actor['id'],text,message['created_at']))
                c.execute('UPDATE projects SET updated_at=? WHERE id=?',(now(),pid))
                self.record(c,actor['id'],pid,'message_posted')
                response={'message':message}
                self.save_replay(c,actor,scope,key,payload_hash,response)
                return response

    def delete_project(self, actor, pid):
        with self.lock('project:'+pid):
            with self.db() as c:
                self.get_project(c,pid,actor)
                ids = [r['id'] for r in c.execute('SELECT id FROM uploads WHERE project_id=? ORDER BY id', (pid,))]
            with contextlib.ExitStack() as stack:
                for uid in ids: stack.enter_context(self.lock('upload:'+uid))
                with self.tx() as c:
                    self.get_project(c,pid,actor)
                    c.execute("UPDATE uploads SET status='cancelled' WHERE project_id=?", (pid,))
                    for uid in ids:self.erase_upload_metadata(c,uid,pid)
                    c.execute('DELETE FROM messages WHERE project_id=?',(pid,))
                    c.execute('DELETE FROM idempotency WHERE scope=?',('message:'+pid,))
                    c.execute('DELETE FROM idempotency WHERE scope=?',('status:'+pid,))
                    rows=c.execute("SELECT user_id,scope,key,response FROM idempotency WHERE user_id=? AND scope='project'",(actor['id'],)).fetchall()
                    for row in rows:
                        if json.loads(row['response']).get('project',{}).get('id')==pid:
                            c.execute('DELETE FROM idempotency WHERE user_id=? AND scope=? AND key=?',(row['user_id'],row['scope'],row['key']))
                    c.execute("UPDATE projects SET deleted=1,is_public=0,public_token=NULL,name='',note='',message='',options='[]',updated_at=? WHERE id=?", (now(),pid))
                    self.record(c,actor['id'],pid,'project_deleted')
                pending=sum(not self.cleanup_upload(uid) for uid in ids)
            return {'ok':True,'storage_cleanup_pending':bool(pending)}

    def file_info(self, fid, actor=None, public_token=None, admin=False):
        with self.db() as c:
            row = c.execute('''SELECT f.*,u.status,u.chunk_size,p.owner_id,p.deleted,p.is_public,p.public_token,p.status project_status
                FROM files f JOIN uploads u ON u.id=f.upload_id JOIN projects p ON p.id=f.project_id WHERE f.id=?''', (fid,)).fetchone()
            if not row or row['deleted'] or row['status'] != 'complete':
                fail(404,'NOT_FOUND','文件不存在或尚未完成上传')
            if public_token is not None:
                if row['kind'] != 'result' or not row['released'] or not row['is_public'] or row['project_status'] != 'delivered' or not row['public_token'] or not hmac.compare_digest(row['public_token'],public_token):
                    fail(404,'NOT_FOUND','文件不存在或未公开')
            elif not actor or (not admin and row['owner_id'] != actor['id']):
                fail(404,'NOT_FOUND','文件不存在或无权访问')
            elif not admin and row['kind']=='result' and not row['released']:
                fail(404,'NOT_FOUND','成果尚未交付')
            chunks = [dict(r) for r in c.execute('SELECT idx,size,sha256 FROM chunks WHERE file_id=? ORDER BY idx',(fid,))]
            result = dict(row); result['chunks'] = chunks
            return result

    def public_project(self, token):
        if not isinstance(token,str) or len(token)>128:
            fail(404,'NOT_FOUND','公开项目不存在')
        with self.db() as c:
            p = c.execute("SELECT p.*,u.username owner_name FROM projects p JOIN users u ON u.id=p.owner_id WHERE p.public_token=? AND p.is_public=1 AND p.deleted=0 AND p.status='delivered'",(token,)).fetchone()
            if not p: fail(404,'NOT_FOUND','公开项目不存在或已关闭')
            entries = c.execute("SELECT f.id,f.path,f.size FROM files f JOIN uploads u ON u.id=f.upload_id WHERE f.project_id=? AND f.kind='result' AND f.released=1 AND u.status='complete' ORDER BY f.path",(p['id'],)).fetchall()
            return {'project':{'name':p['name'],'created_at':p['created_at'],'updated_at':p['updated_at']},'result_files':[dict(r) for r in entries]}
