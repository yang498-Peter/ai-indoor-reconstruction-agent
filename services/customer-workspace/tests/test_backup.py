"""Exercise same-machine backup/restore with synthetic data only."""
import hashlib
import json
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch
from server.backup import backup,restore
from server.store import Store,CHUNK_SIZE


class BackupTest(unittest.TestCase):
    def test_backup_restore_revokes_sessions_and_publication(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);data=root/'data';copies=root/'copies';recovered=root/'recovered'
            store=Store(data,min_free=0);uid=store.create_user('synthetic-user','example-password')
            token,_=store.new_session(uid);actor=store.session(token)
            pid=store.create_project(actor,{'name':'private synthetic','options':['pointcloud'],'note':'contact'},str(uuid.uuid4()),'a')['project']['id']
            content=b'synthetic chunk';sha=hashlib.sha256(content).hexdigest()
            manifest={'kind':'raw','chunk_size':CHUNK_SIZE,'files':[{'path':'S1/data/data_raw.mcap','size':len(content),'last_modified':0,'expected_hashes':[sha]}]}
            up=store.create_upload(actor,pid,manifest,str(uuid.uuid4()),'b')['upload'];fid=up['files'][0]['id']
            store.put_chunk(actor,up['id'],fid,0,content,sha);store.complete_upload(actor,up['id'])
            admin_id=store.create_user('backup-admin','admin-password','admin');admin_token,_=store.new_session(admin_id);admin=store.session(admin_token)
            result_content=b'result';result_sha=hashlib.sha256(result_content).hexdigest()
            result_manifest={'kind':'result','chunk_size':CHUNK_SIZE,'files':[{'path':'result.ifc','size':len(result_content),'last_modified':0,'expected_hashes':[result_sha]}]}
            result_up=store.create_upload(admin,pid,result_manifest,str(uuid.uuid4()),'c')['upload']
            store.put_chunk(admin,result_up['id'],result_up['files'][0]['id'],0,result_content,result_sha);store.complete_upload(admin,result_up['id'])
            store.update_project(admin,pid,{'status':'delivered','message':''},admin=True)
            share=store.update_project(actor,pid,{'is_public':True})['project']['public_url']
            result=backup(data,copies,min_free=0);self.assertEqual(result['status'],'ok')
            source=store.chunk_path(up['id'],fid,0);saved=copies/'chunks'/up['id']/fid/'00000000.chunk'
            self.assertEqual(saved.read_bytes(),content)
            source.write_bytes(b'X'*len(content));self.assertEqual(saved.read_bytes(),content)
            store.update_project(actor,pid,{'is_public':False})
            outcome=restore(copies,recovered);self.assertTrue(outcome['sessionsRevoked']);self.assertTrue(outcome['publicLinksDisabled'])
            restored=Store(recovered,min_free=0);self.assertIsNone(restored.session(token))
            self.assertEqual(restored.chunk_path(up['id'],fid,0).read_bytes(),content)
            with restored.db() as c:
                project=c.execute('SELECT is_public,public_token FROM projects WHERE id=?',(pid,)).fetchone()
                self.assertEqual(project['is_public'],0);self.assertIsNone(project['public_token'])
            with self.assertRaises(ValueError):restore(copies,recovered)

    def test_failed_backup_retains_last_snapshot_and_deleted_chunks_are_cleaned(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);data=root/'data';copies=root/'copies';store=Store(data,min_free=0)
            uid=store.create_user('synthetic-user','example-password');token,_=store.new_session(uid);actor=store.session(token)
            pid=store.create_project(actor,{'name':'example','options':['model'],'note':''},str(uuid.uuid4()),'a')['project']['id']
            content=b'backup-test';sha=hashlib.sha256(content).hexdigest()
            manifest={'kind':'raw','chunk_size':CHUNK_SIZE,'files':[{'path':'S1/raw.mcap','size':len(content),'last_modified':0,'expected_hashes':[sha]}]}
            up=store.create_upload(actor,pid,manifest,str(uuid.uuid4()),'b')['upload'];fid=up['files'][0]['id']
            store.put_chunk(actor,up['id'],fid,0,content,sha);store.complete_upload(actor,up['id'])
            backup(data,copies,min_free=0);old=(copies/'workspace.sqlite3').read_bytes()
            usage=type('Disk',(),{'free':0})()
            with patch('server.backup.shutil.disk_usage',return_value=usage):
                with self.assertRaises(RuntimeError):backup(data,copies,min_free=0)
            self.assertEqual((copies/'workspace.sqlite3').read_bytes(),old)
            self.assertEqual(json.loads((data/'backup-status.json').read_text(encoding='utf-8'))['status'],'failed')
            store.delete_project(actor,pid)
            backup(data,copies,min_free=0)
            self.assertFalse((copies/'chunks'/up['id']).exists())


if __name__=='__main__':unittest.main()
