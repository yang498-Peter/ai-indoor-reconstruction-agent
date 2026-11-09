"""Independent synthetic backup/restore fault-injection checks."""
import hashlib
import contextlib
import json
import os
import sqlite3
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT))
from server import backup as backup_mod
from server import store as mod
from server.locks import LockBusy


def key():return str(uuid.uuid4())


class BackupReview(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='workspace-backup-review-')
        self.root=Path(self.temp.name);self.data=self.root/'data';self.backup=self.root/'backup'
        self.s=mod.Store(self.data,min_free=0)
        uid=self.s.create_user('backup-review','test-password-123')
        self.owner={'id':uid,'role':'user'}

    def tearDown(self):self.temp.cleanup()

    def add_data(self, value):
        pid=self.s.create_project(self.owner,{'name':'synthetic','note':'','options':['pointcloud']},key(),key())['project']['id']
        file={'path':'folder/data/data_raw.mcap','size':len(value),'last_modified':0,'expected_hashes':[hashlib.sha256(value).hexdigest()]}
        up=self.s.create_upload(self.owner,pid,{'kind':'raw','chunk_size':mod.CHUNK_SIZE,'files':[file]},key(),key())['upload']
        self.s.put_chunk(self.owner,up['id'],up['files'][0]['id'],0,value,hashlib.sha256(value).hexdigest())
        self.s.complete_upload(self.owner,up['id'])

    def test_restore_is_consistent_if_backup_switches_during_restore(self):
        self.add_data(b'first synthetic file');backup_mod.backup(self.data,self.backup,min_free=0)
        self.add_data(b'second synthetic file')
        original=backup_mod.shutil.copyfile;switched=[]
        def interleaved(source,target,*args,**kwargs):
            if Path(source).resolve()==(self.backup/'workspace.sqlite3').resolve() and not switched:
                switched.append(True)
                try:backup_mod.backup(self.data,self.backup,min_free=0)
                except LockBusy:pass
            return original(source,target,*args,**kwargs)
        with mock.patch.object(backup_mod.shutil,'copyfile',side_effect=interleaved):
            backup_mod.restore(self.backup,self.root/'restored')
        self.assertTrue(switched, 'race injection did not run')
        rows,_=backup_mod._rows(self.root/'restored'/'workspace.sqlite3')
        for row in rows:
            path=backup_mod._chunk(self.root/'restored',row['uid'],row['fid'],row['idx'])
            self.assertEqual(backup_mod._hash_file(path),(row['size'],row['sha256']))

    def test_failed_new_snapshot_retains_old_backup(self):
        self.add_data(b'first synthetic file');backup_mod.backup(self.data,self.backup,min_free=0)
        before=backup_mod._hash_file(self.backup/'workspace.sqlite3')
        self.add_data(b'second synthetic file')
        with mock.patch.object(backup_mod,'_copy_verified',side_effect=OSError('synthetic copy failure')):
            with self.assertRaises(OSError):backup_mod.backup(self.data,self.backup,min_free=0)
        self.assertEqual(backup_mod._hash_file(self.backup/'workspace.sqlite3'),before)
        rows,_=backup_mod._rows(self.backup/'workspace.sqlite3')
        for row in rows:
            self.assertEqual(backup_mod._hash_file(backup_mod._chunk(self.backup,row['uid'],row['fid'],row['idx'])),(row['size'],row['sha256']))

    def test_destination_corruption_is_detected_before_commit(self):
        source=self.root/'source';target=self.root/'target';payload=b'synthetic bytes';source.write_bytes(payload)
        real_fsync=os.fsync
        def corrupt_after_sync(fd):
            real_fsync(fd);position=os.lseek(fd,0,os.SEEK_CUR);os.lseek(fd,0,os.SEEK_SET);os.write(fd,b'!');os.lseek(fd,position,os.SEEK_SET)
        with mock.patch.object(backup_mod.os,'fsync',side_effect=corrupt_after_sync):
            with self.assertRaises(RuntimeError):backup_mod._copy_verified(source,target,len(payload),hashlib.sha256(payload).hexdigest())
        self.assertFalse(target.exists())

    def test_missing_source_does_not_create_database(self):
        missing=self.root/'missing';missing.mkdir()
        with self.assertRaises(Exception):backup_mod.backup(missing,self.root/'missing-backup',min_free=0)
        self.assertFalse((missing/'workspace.sqlite3').exists())

    def test_busy_backup_is_reported_without_losing_last_success(self):
        self.add_data(b'first synthetic file');before=backup_mod.backup(self.data,self.backup,min_free=0)
        with backup_mod.disk_budget(self.data,blocking=False):
            with self.assertRaises(LockBusy):backup_mod.backup(self.data,self.backup,min_free=0)
        status=json.loads((self.data/'backup-status.json').read_text(encoding='utf-8'))
        self.assertEqual(status['status'],'failed');self.assertEqual(status['last_success'],before['last_success'])

    def test_missing_record_for_completed_file_cannot_be_a_successful_backup(self):
        self.add_data(b'first synthetic file')
        with contextlib.closing(sqlite3.connect(str(self.s.database))) as db:
            db.execute('DELETE FROM chunks');db.commit()
        with self.assertRaises(Exception):backup_mod.backup(self.data,self.backup,min_free=0)


if __name__=='__main__':unittest.main(verbosity=2)
