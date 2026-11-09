"""Same-machine verified backup and non-destructive isolated restore.

Backups are independent byte copies on the same filesystem, not offsite disaster
recovery. Only explicit cancelled/deleted upload IDs are cleaned after a newer
consistent snapshot is committed.
"""
import argparse
import contextlib
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
import uuid
from pathlib import Path
from .store import now, sync_dir
from .locks import disk_budget, LockBusy


def _uuid(value):
    parsed=uuid.UUID(value)
    if str(parsed)!=value:raise ValueError('invalid internal id')
    return value


def _chunk(root,uid,fid,idx):
    _uuid(uid);_uuid(fid)
    if not isinstance(idx,int) or idx<0:raise ValueError('invalid chunk')
    return root/'chunks'/uid/fid/('%08d.chunk' % idx)


def _hash_file(path):
    h=hashlib.sha256();size=0
    with path.open('rb') as source:
        while True:
            data=source.read(1024*1024)
            if not data:break
            h.update(data);size+=len(data)
    return size,h.hexdigest()


def _atomic_json(path,value):
    data=json.dumps(value,ensure_ascii=False,indent=2).encode('utf-8')
    path.parent.mkdir(parents=True,exist_ok=True)
    temp=None
    try:
        with tempfile.NamedTemporaryFile(dir=str(path.parent),prefix='.backup-status-',delete=False) as target:
            temp=target.name;target.write(data);target.flush();os.fsync(target.fileno())
        os.replace(temp,str(path));temp=None;sync_dir(path.parent)
    finally:
        if temp and os.path.exists(temp):os.unlink(temp)


def _snapshot(source,destination):
    if not source.is_file() or source.is_symlink():raise ValueError('source database is missing or unsafe')
    if destination.exists():raise FileExistsError(str(destination))
    if hasattr(sqlite3.Connection,'backup'):
        with contextlib.closing(sqlite3.connect(source.as_uri()+'?mode=ro',timeout=30,uri=True)) as src:
            with contextlib.closing(sqlite3.connect(str(destination),timeout=30)) as dst:src.backup(dst)
    else:
        # Python 3.6 lacks Connection.backup; SQLite's CLI uses the same online backup API.
        if any(ch in str(destination) for ch in ('"','\n','\r')):raise ValueError('unsupported backup path')
        command='.backup "'+str(destination).replace('\\','\\\\')+'"'
        result=subprocess.run(['sqlite3','-readonly','-batch',str(source),command],stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=600)
        if result.returncode:raise RuntimeError('sqlite online backup failed')
    os.chmod(str(destination),0o600)


def _rows(database):
    if not database.is_file() or database.is_symlink():raise ValueError('snapshot database is missing or unsafe')
    with contextlib.closing(sqlite3.connect(str(database),timeout=30)) as c:
        c.row_factory=sqlite3.Row
        if c.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise RuntimeError('database integrity check failed')
        if c.execute('PRAGMA foreign_key_check').fetchone():raise RuntimeError('database foreign key check failed')
        rows=c.execute('''SELECT u.id uid,f.id fid,ch.idx,ch.size,ch.sha256 FROM chunks ch
            JOIN files f ON f.id=ch.file_id JOIN uploads u ON u.id=f.upload_id JOIN projects p ON p.id=u.project_id
            WHERE u.status IN ('uploading','complete') AND p.deleted=0 ORDER BY u.id,f.id,ch.idx''').fetchall()
        files=c.execute('''SELECT f.id,f.size,f.sha256,u.chunk_size,u.status FROM files f
            JOIN uploads u ON u.id=f.upload_id JOIN projects p ON p.id=f.project_id
            WHERE u.status IN ('uploading','complete') AND p.deleted=0''').fetchall()
        expected=c.execute('''SELECT e.file_id,e.idx,e.sha256 FROM expected_chunks e JOIN files f ON f.id=e.file_id
            JOIN uploads u ON u.id=f.upload_id JOIN projects p ON p.id=f.project_id
            WHERE u.status IN ('uploading','complete') AND p.deleted=0 ORDER BY e.file_id,e.idx''').fetchall()
        cancelled=[r[0] for r in c.execute("SELECT id FROM uploads WHERE status='cancelled'")]
    actual_by_file={};expected_by_file={}
    for row in rows:actual_by_file.setdefault(row['fid'],[]).append(row)
    for row in expected:expected_by_file.setdefault(row['file_id'],[]).append(row)
    for file in files:
        if file['chunk_size']<=0 or file['size']<0:raise RuntimeError('invalid backup file size')
        count=(file['size']+file['chunk_size']-1)//file['chunk_size']
        plan=expected_by_file.get(file['id'],[]);present=actual_by_file.get(file['id'],[])
        if len(plan)!=count or any(row['idx']!=i for i,row in enumerate(plan)):
            raise RuntimeError('incomplete backup chunk manifest')
        if file['status']=='complete' and (len(present)!=count or not file['sha256']):
            raise RuntimeError('completed backup file has missing chunks')
        for row in present:
            idx=row['idx']
            if idx<0 or idx>=count or row['size']!=min(file['chunk_size'],file['size']-idx*file['chunk_size']) or row['sha256']!=plan[idx]['sha256']:
                raise RuntimeError('backup chunk metadata is inconsistent')
    return [dict(r) for r in rows],cancelled


def _copy_verified(source,target,expected_size,expected_hash):
    target.parent.mkdir(parents=True,exist_ok=True)
    temp=None;h=hashlib.sha256();size=0
    try:
        with source.open('rb') as src:
            with tempfile.NamedTemporaryFile(dir=str(target.parent),prefix='.backup-',delete=False) as dst:
                temp=dst.name
                while True:
                    data=src.read(1024*1024)
                    if not data:break
                    h.update(data);size+=len(data);dst.write(data)
                dst.flush();os.fsync(dst.fileno())
        if size!=expected_size or h.hexdigest()!=expected_hash:raise RuntimeError('backup source integrity check failed')
        if _hash_file(Path(temp))!=(expected_size,expected_hash):raise RuntimeError('backup copy integrity check failed')
        os.chmod(temp,0o600);os.replace(temp,str(target));temp=None
        sync_dir(target.parent)
    finally:
        if temp and os.path.exists(temp):os.unlink(temp)


def _backup_unlocked(data_root,backup_root,min_free=20_000_000_000):
    data=Path(data_root).resolve();backup=Path(backup_root).resolve()
    if data==backup or data in backup.parents or backup in data.parents:
        raise ValueError('backup and data roots must be separate')
    backup.mkdir(parents=True,exist_ok=True);os.chmod(str(backup),0o700)
    status_path=data/'backup-status.json';snapshot=backup/('.snapshot-'+uuid.uuid4().hex+'.sqlite')
    previous={}
    committed=False
    try:
        if status_path.is_file():previous=json.loads(status_path.read_text(encoding='utf-8'))
    except (OSError,ValueError):pass
    try:
        source_db=data/'workspace.sqlite3'
        if not source_db.is_file() or source_db.is_symlink():raise ValueError('source database is missing or unsafe')
        outstanding=0
        if os.stat(str(data)).st_dev==os.stat(str(backup)).st_dev:
            with contextlib.closing(sqlite3.connect(source_db.as_uri()+'?mode=ro',timeout=30,uri=True)) as live:
                reserved=live.execute("SELECT COALESCE(SUM(u.total_size),0) FROM uploads u JOIN projects p ON p.id=u.project_id WHERE u.status='uploading' AND p.deleted=0").fetchone()[0]
                written=live.execute("SELECT COALESCE(SUM(ch.size),0) FROM chunks ch JOIN files f ON f.id=ch.file_id JOIN uploads u ON u.id=f.upload_id JOIN projects p ON p.id=u.project_id WHERE u.status='uploading' AND p.deleted=0").fetchone()[0]
                outstanding=max(0,reserved-written)
        wal=Path(str(source_db)+'-wal')
        db_estimate=source_db.stat().st_size+(wal.stat().st_size if wal.is_file() else 0)
        if shutil.disk_usage(str(backup)).free < min_free+outstanding+db_estimate*2+128*1024*1024:
            raise RuntimeError('备份空间不足，已保留上一次成功备份')
        _snapshot(source_db,snapshot)
        rows,cancelled=_rows(snapshot);needed=[];total=0
        for row in rows:
            target=_chunk(backup,row['uid'],row['fid'],row['idx']);total+=row['size']
            try: valid=_hash_file(target)==(row['size'],row['sha256'])
            except FileNotFoundError:valid=False
            if not valid:needed.append(row)
        required=sum(row['size'] for row in needed)
        outstanding=0
        if os.stat(str(data)).st_dev==os.stat(str(backup)).st_dev:
            with contextlib.closing(sqlite3.connect(str(data/'workspace.sqlite3'),timeout=30)) as live:
                reserved=live.execute("SELECT COALESCE(SUM(u.total_size),0) FROM uploads u JOIN projects p ON p.id=u.project_id WHERE u.status='uploading' AND p.deleted=0").fetchone()[0]
                written=live.execute("SELECT COALESCE(SUM(ch.size),0) FROM chunks ch JOIN files f ON f.id=ch.file_id JOIN uploads u ON u.id=f.upload_id JOIN projects p ON p.id=u.project_id WHERE u.status='uploading' AND p.deleted=0").fetchone()[0]
                outstanding=max(0,reserved-written)
        if shutil.disk_usage(str(backup)).free < min_free+outstanding+required+128*1024*1024:
            raise RuntimeError('备份空间不足，已保留上一次成功备份')
        for row in needed:
            _copy_verified(_chunk(data,row['uid'],row['fid'],row['idx']),_chunk(backup,row['uid'],row['fid'],row['idx']),row['size'],row['sha256'])
        os.replace(str(snapshot),str(backup/'workspace.sqlite3'))
        committed=True
        sync_dir(backup)
        pending=0
        for uid in cancelled:
            path=backup/'chunks'/_uuid(uid)
            # This exact upload was explicitly cancelled or deleted by its owner.
            if path.exists():
                try:shutil.rmtree(str(path))
                except OSError:pending+=1
        result={'status':'ok','last_success':now(),'bytes':total,'files':len(rows),
                'message':'同机备份已校验；不能防止整机或磁盘损坏。' if not pending else '备份完成，部分已删除数据仍在等待清理。',
                'cleanup_pending':pending}
        _atomic_json(status_path,result)
        return result
    except Exception as exc:
        message=('备份数据已更新，但状态记录未完成，请管理员检查。' if committed else
                 (str(exc) if str(exc).startswith('备份空间不足') else '备份未完成，上一次成功备份仍保留。'))
        result={'status':'failed','last_success':previous.get('last_success'), 'bytes':previous.get('bytes',0),'message':message}
        _atomic_json(status_path,result)
        raise
    finally:
        if snapshot.exists():snapshot.unlink()


def backup(data_root,backup_root,min_free=20_000_000_000):
    data=Path(data_root).resolve();backup_root=Path(backup_root).resolve()
    if data==backup_root or data in backup_root.parents or backup_root in data.parents:
        raise ValueError('backup and data roots must be separate')
    backup_root.mkdir(parents=True,exist_ok=True)
    try:
        with disk_budget(data,blocking=False,timeout=3),disk_budget(backup_root,blocking=False,timeout=3):
            return _backup_unlocked(data,backup_root,min_free)
    except LockBusy:
        status_path=data/'backup-status.json';previous={}
        try:previous=json.loads(status_path.read_text(encoding='utf-8'))
        except (OSError,ValueError):pass
        _atomic_json(status_path,{'status':'failed','last_success':previous.get('last_success'),
                     'bytes':previous.get('bytes',0),'message':'备份未能开始，其他存储任务正在进行；请稍后重试。'})
        raise


def _restore_unlocked(backup_root,target_root):
    backup=Path(backup_root).resolve();target=Path(target_root).resolve()
    if target.exists() or target==backup or backup in target.parents or target in backup.parents:
        raise ValueError('Restore target must be a new, separate directory')
    target.mkdir(parents=True,mode=0o700)
    marker=target/'RESTORE_IN_PROGRESS';marker.write_text('incomplete',encoding='utf-8')
    source_db=backup/'workspace.sqlite3'
    shutil.copyfile(str(source_db),str(target/'workspace.sqlite3'));os.chmod(str(target/'workspace.sqlite3'),0o600)
    rows,_=_rows(target/'workspace.sqlite3')
    for row in rows:
        _copy_verified(_chunk(backup,row['uid'],row['fid'],row['idx']),_chunk(target,row['uid'],row['fid'],row['idx']),row['size'],row['sha256'])
    with contextlib.closing(sqlite3.connect(str(target/'workspace.sqlite3'))) as c:
        c.execute('DELETE FROM sessions')
        c.execute('UPDATE projects SET is_public=0,public_token=NULL')
        c.commit()
    marker.unlink()
    return {'files':len(rows),'bytes':sum(r['size'] for r in rows),'sessionsRevoked':True,'publicLinksDisabled':True}


def restore(backup_root,target_root):
    backup_root=Path(backup_root).resolve()
    if not backup_root.is_dir():raise ValueError('Backup directory does not exist')
    with disk_budget(backup_root,blocking=False):
        return _restore_unlocked(backup_root,target_root)


def main():
    parser=argparse.ArgumentParser();sub=parser.add_subparsers(dest='action')
    b=sub.add_parser('backup');b.add_argument('--data',required=True);b.add_argument('--backup',required=True);b.add_argument('--min-free',type=int,default=20_000_000_000)
    r=sub.add_parser('restore');r.add_argument('--backup',required=True);r.add_argument('--target',required=True)
    args=parser.parse_args()
    if args.action=='backup':result=backup(args.data,args.backup,args.min_free)
    elif args.action=='restore':result=restore(args.backup,args.target)
    else:parser.error('choose backup or restore')
    print(json.dumps(result,ensure_ascii=False))


if __name__=='__main__':main()
