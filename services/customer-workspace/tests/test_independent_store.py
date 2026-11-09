"""Independent synthetic checks of workspace authorization and data boundaries."""
import hashlib
import http.client
import importlib.util
import io
import json
import sys
import tempfile
import threading
import unittest
from unittest import mock
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from server import store as mod
from server import app as http_app


def key():
    return str(uuid.uuid4())


def entry(path, contents):
    hashes=[hashlib.sha256(contents[i:i+mod.CHUNK_SIZE]).hexdigest() for i in range(0,len(contents),mod.CHUNK_SIZE)]
    return {'path':path,'size':len(contents),'last_modified':0,'expected_hashes':hashes}


class StoreReview(unittest.TestCase):
    def setUp(self):
        self.old_limits = mod.DATASET_LIMIT, mod.USER_LIMIT, mod.CHUNK_SIZE
        mod.DATASET_LIMIT, mod.USER_LIMIT, mod.CHUNK_SIZE = 8, 10, 4
        self.tmp = tempfile.TemporaryDirectory(prefix='workspace-independent-review-')
        self.s = mod.Store(self.tmp.name, min_free=0)
        self.owner = {'id': self.s.create_user('owner-review', 'test-password-123'), 'role': 'user'}
        self.other = {'id': self.s.create_user('other-review', 'test-password-123'), 'role': 'user'}
        self.admin = {'id': self.s.create_user('admin-review', 'test-password-123', role='admin'), 'role': 'admin'}

    def tearDown(self):
        mod.DATASET_LIMIT, mod.USER_LIMIT, mod.CHUNK_SIZE = self.old_limits
        self.tmp.cleanup()

    def project(self):
        return self.s.create_project(self.owner, {'name':'fixture', 'note':'', 'options':['pointcloud']}, key(), key())['project']['id']

    def upload(self, actor, pid, kind, contents=b'abc', filename='folder/data/data_raw.mcap'):
        body = {'kind':kind, 'chunk_size':mod.CHUNK_SIZE, 'files':[entry(filename,contents)]}
        upload = self.s.create_upload(actor, pid, body, key(), key())['upload']
        file = upload['files'][0]
        for idx in range((len(contents)+mod.CHUNK_SIZE-1)//mod.CHUNK_SIZE):
            data = contents[idx*mod.CHUNK_SIZE:(idx+1)*mod.CHUNK_SIZE]
            self.s.put_chunk(actor,upload['id'],file['id'],idx,data,hashlib.sha256(data).hexdigest())
        return upload, file

    def test_owner_isolation_and_raw_public_boundary(self):
        pid=self.project(); up,raw=self.upload(self.owner,pid,'raw'); self.s.complete_upload(self.owner,up['id'])
        for action in (lambda:self.s.detail(self.other,pid), lambda:self.s.get_upload(self.other,up['id']),
                       lambda:self.s.file_info(raw['id'],actor=self.other),
                       lambda:self.s.file_info(raw['id'],public_token='invalid')):
            with self.assertRaises(mod.WorkspaceError) as caught: action()
            self.assertEqual(caught.exception.status,404)
        with self.assertRaises(mod.WorkspaceError):
            self.s.create_upload(self.other,pid,{'kind':'result','chunk_size':mod.CHUNK_SIZE,'files':[entry('x',b'x')]},key(),key())

    def test_concurrent_quota_reservations_do_not_oversubscribe(self):
        projects=[self.project(),self.project()]; ready=threading.Barrier(2)
        def reserve(pid):
            ready.wait(timeout=10)
            try:
                self.s.create_upload(self.owner,pid,{'kind':'raw','chunk_size':mod.CHUNK_SIZE,'files':[entry('x',b'xxxxxx')]},key(),key())
                return 'ok'
            except mod.WorkspaceError as exc:
                return exc.code
        with ThreadPoolExecutor(max_workers=2) as pool:
            result=list(pool.map(reserve,projects))
        self.assertEqual(sorted(result),['QUOTA_EXCEEDED','ok'])
        self.assertEqual(self.s.quota(self.owner['id'])['reserved'],6)

    def test_publication_revocation_and_future_result_approval(self):
        pid=self.project(); raw,_=self.upload(self.owner,pid,'raw',b'a');self.s.complete_upload(self.owner,raw['id'])
        result,rf=self.upload(self.admin,pid,'result',b'b',filename='model.glb');self.s.complete_upload(self.admin,result['id'])
        self.s.update_project(self.admin,pid,{'status':'delivered','message':''},admin=True)
        project=self.s.update_project(self.owner,pid,{'is_public':True})['project']; token=project['public_url'].split('/')[-1]
        self.assertEqual(self.s.public_project(token)['result_files'][0]['id'],rf['id'])
        with self.assertRaises(mod.WorkspaceError):
            self.s.create_upload(self.admin,pid,{'kind':'result','chunk_size':mod.CHUNK_SIZE,'files':[entry('new.glb',b'x')]},key(),key())
        self.s.update_project(self.owner,pid,{'is_public':False})
        with self.assertRaises(mod.WorkspaceError): self.s.public_project(token)
        with self.assertRaises(mod.WorkspaceError): self.s.file_info(rf['id'],public_token=token)

    def test_reject_same_length_chunk_corruption_at_completion(self):
        pid=self.project(); up,file=self.upload(self.owner,pid,'raw',b'abc')
        self.s.chunk_path(up['id'],file['id'],0).write_bytes(b'xyz')
        with self.assertRaises(mod.WorkspaceError): self.s.complete_upload(self.owner,up['id'])
        data=b'abc'
        self.s.put_chunk(self.owner,up['id'],file['id'],0,data,hashlib.sha256(data).hexdigest())
        self.assertEqual(self.s.complete_upload(self.owner,up['id'])['upload']['status'],'complete')

    def test_cannot_publish_during_new_result_upload(self):
        pid=self.project(); raw,_=self.upload(self.owner,pid,'raw',b'a');self.s.complete_upload(self.owner,raw['id'])
        result,_=self.upload(self.admin,pid,'result',b'b',filename='model.glb');self.s.complete_upload(self.admin,result['id'])
        self.s.update_project(self.admin,pid,{'status':'delivered','message':''},admin=True)
        self.s.create_upload(self.admin,pid,{'kind':'result','chunk_size':mod.CHUNK_SIZE,'files':[entry('updated.glb',b'x')]},key(),key())
        with self.assertRaises(mod.WorkspaceError): self.s.update_project(self.owner,pid,{'is_public':True})
        with self.assertRaises(mod.WorkspaceError): self.s.update_project(self.admin,pid,{'status':'delivered','message':''},admin=True)

    def test_in_progress_upload_uses_persisted_chunk_size_after_upgrade(self):
        pid=self.project();up,_=self.upload(self.owner,pid,'raw',b'abcdef')
        mod.CHUNK_SIZE=8
        self.assertEqual(self.s.complete_upload(self.owner,up['id'])['upload']['status'],'complete')

    def test_changed_not_yet_uploaded_chunk_cannot_be_mixed_on_resume(self):
        pid=self.project();original=b'abcdef'
        up=self.s.create_upload(self.owner,pid,{'kind':'raw','chunk_size':mod.CHUNK_SIZE,'files':[entry('raw.mcap',original)]},key(),key())['upload']
        fid=up['files'][0]['id']; first=original[:4]
        self.s.put_chunk(self.owner,up['id'],fid,0,first,hashlib.sha256(first).hexdigest())
        changed=b'XY'
        with self.assertRaises(mod.WorkspaceError):
            self.s.put_chunk(self.owner,up['id'],fid,1,changed,hashlib.sha256(changed).hexdigest())
        with self.assertRaises(mod.WorkspaceError): self.s.complete_upload(self.owner,up['id'])
        final=original[4:]
        self.s.put_chunk(self.owner,up['id'],fid,1,final,hashlib.sha256(final).hexdigest())
        self.assertEqual(self.s.complete_upload(self.owner,up['id'])['upload']['status'],'complete')

    def test_undelivered_result_is_private_until_explicit_delivery(self):
        pid=self.project();raw,_=self.upload(self.owner,pid,'raw',b'a');self.s.complete_upload(self.owner,raw['id'])
        first,firstfile=self.upload(self.admin,pid,'result',b'b',filename='first.glb');self.s.complete_upload(self.admin,first['id'])
        self.s.update_project(self.admin,pid,{'status':'delivered','message':''},admin=True)
        self.s.update_project(self.admin,pid,{'status':'processing','message':''},admin=True)
        second,secondfile=self.upload(self.admin,pid,'result',b'c',filename='second.glb');self.s.complete_upload(self.admin,second['id'])
        visible={f['id'] for f in self.s.detail(self.owner,pid)['result_files']}
        self.assertEqual(visible,{firstfile['id']})
        with self.assertRaises(mod.WorkspaceError): self.s.file_info(secondfile['id'],actor=self.owner)
        self.s.update_project(self.admin,pid,{'status':'delivered','message':''},admin=True)
        visible={f['id'] for f in self.s.detail(self.owner,pid)['result_files']}
        self.assertEqual(visible,{firstfile['id'],secondfile['id']})

    def test_complete_but_undelivered_addition_blocks_new_publication(self):
        pid=self.project();raw,_=self.upload(self.owner,pid,'raw',b'a');self.s.complete_upload(self.owner,raw['id'])
        result,_=self.upload(self.admin,pid,'result',b'b',filename='first.glb');self.s.complete_upload(self.admin,result['id'])
        self.s.update_project(self.admin,pid,{'status':'delivered','message':''},admin=True)
        new,newfile=self.upload(self.admin,pid,'result',b'c',filename='second.glb');self.s.complete_upload(self.admin,new['id'])
        with self.assertRaises(mod.WorkspaceError): self.s.update_project(self.owner,pid,{'is_public':True})
        self.s.update_project(self.admin,pid,{'status':'delivered','message':''},admin=True)
        public=self.s.update_project(self.owner,pid,{'is_public':True})['project']
        token=public['public_url'].split('/')[-1]
        self.assertIn(newfile['id'],{f['id'] for f in self.s.public_project(token)['result_files']})

    def test_cancelled_result_does_not_permanently_block_publication(self):
        pid=self.project();raw,_=self.upload(self.owner,pid,'raw',b'a');self.s.complete_upload(self.owner,raw['id'])
        result,_=self.upload(self.admin,pid,'result',b'b',filename='first.glb');self.s.complete_upload(self.admin,result['id'])
        self.s.update_project(self.admin,pid,{'status':'delivered','message':''},admin=True)
        pending=self.s.create_upload(self.admin,pid,{'kind':'result','chunk_size':mod.CHUNK_SIZE,'files':[entry('cancelled.glb',b'c')]},key(),key())['upload']
        self.s.cancel_upload(self.admin,pending['id'])
        public=self.s.update_project(self.owner,pid,{'is_public':True})['project']
        self.assertTrue(public['is_public'])

    def test_reject_cross_platform_name_and_file_directory_conflicts(self):
        for paths in (('Folder/A','folder/a'),('folder/a','folder/a/b')):
            data={'chunk_size':mod.CHUNK_SIZE,'files':[entry(p,b'x') for p in paths]}
            with self.subTest(paths=paths), self.assertRaises(mod.WorkspaceError): self.s.manifest(data)

    def test_reject_path_traversal(self):
        for candidate in ('../outside','/outside','x/../out','x\\out','C:/outside','x/./out',
                          'folder/NUL.txt','folder/AUX','folder/name.','folder/name ','folder/a?b','folder/a|b'):
            with self.subTest(path=candidate), self.assertRaises(mod.WorkspaceError): mod.valid_path(candidate)

    def test_retry_cancelled_storage_cleanup_without_touching_live_data(self):
        live_project=self.project(); live,live_file=self.upload(self.owner,live_project,'raw',b'a');self.s.complete_upload(self.owner,live['id'])
        other_project=self.project(); cancelled,_=self.upload(self.owner,other_project,'raw',b'b')
        with mock.patch.object(self.s,'remove_chunks',side_effect=OSError('synthetic I/O failure')):
            self.assertTrue(self.s.cancel_upload(self.owner,cancelled['id'])['storage_cleanup_pending'])
        self.assertTrue((self.s.chunk_root/cancelled['id']).exists())
        self.assertEqual(self.s.recover_cleanup(),0)
        self.assertFalse((self.s.chunk_root/cancelled['id']).exists())
        self.assertTrue(self.s.chunk_path(live['id'],live_file['id'],0).exists())

    def test_supplemental_raw_keeps_limit_and_cannot_overwrite(self):
        pid=self.project();first,_=self.upload(self.owner,pid,'raw',b'abcd',filename='S1/data/a')
        self.s.complete_upload(self.owner,first['id']);self.s.update_project(self.admin,pid,{'status':'needs_info','message':''},admin=True)
        with self.assertRaises(mod.WorkspaceError):
            self.s.create_upload(self.owner,pid,{'kind':'raw','chunk_size':mod.CHUNK_SIZE,'files':[entry('S1/data/a',b'b')]},key(),key())
        with self.assertRaises(mod.WorkspaceError):
            self.s.create_upload(self.owner,pid,{'kind':'raw','chunk_size':mod.CHUNK_SIZE,'files':[entry('S1/data/b',b'12345')]},key(),key())
        nextup,_=self.upload(self.owner,pid,'raw',b'xyz',filename='S1/data/b')
        self.s.complete_upload(self.owner,nextup['id'])
        self.assertEqual(len(self.s.detail(self.owner,pid)['raw_files']),2)


class HttpReview(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='workspace-independent-http-')
        self.origin='http://127.0.0.1:0'
        self.httpd=http_app.serve(self.tmp.name,self.origin,port=0,min_free=0)
        self.thread=threading.Thread(target=self.httpd.serve_forever,daemon=True);self.thread.start()
        self.conn=http.client.HTTPConnection('127.0.0.1',self.httpd.server_port,timeout=3)

    def tearDown(self):
        self.conn.close();self.httpd.shutdown();self.httpd.server_close();self.thread.join(timeout=3);self.tmp.cleanup()

    def test_error_response_on_reused_http_connection(self):
        self.conn.request('GET','/workspace/')
        r=self.conn.getresponse();self.assertEqual(r.status,200);r.read()
        self.conn.request('GET','/workspace/api/auth/me')
        r=self.conn.getresponse();self.assertEqual(r.status,401);self.assertEqual(json.loads(r.read())['error']['code'],'LOGIN_REQUIRED')

    def test_single_instance_lock_is_released_after_shutdown(self):
        with self.assertRaises(RuntimeError): http_app.serve(self.tmp.name,self.origin,port=0,min_free=0)
        self.httpd.shutdown();self.httpd.server_close();self.thread.join(timeout=3)
        again=http_app.serve(self.tmp.name,self.origin,port=0,min_free=0)
        try:
            self.assertTrue(again.instance_lock)
        finally:
            again.server_close()

    def test_user_cannot_call_administrator_route(self):
        data=json.dumps({'username':'ordinary-review','password':'test-password-123'}).encode('utf-8')
        self.conn.request('POST','/workspace/api/auth/register',body=data,headers={'Origin':self.origin,'Content-Type':'application/json'})
        r=self.conn.getresponse();self.assertEqual(r.status,200);cookie=r.getheader('Set-Cookie').split(';',1)[0];r.read()
        self.conn.request('GET','/workspace/api/admin/projects',headers={'Cookie':cookie})
        r=self.conn.getresponse();self.assertEqual(r.status,403);r.read()

    def test_mutation_requires_matching_origin_and_session_csrf(self):
        s=self.httpd.RequestHandlerClass.store
        uid=s.create_user('csrf-review','test-password-123');token,csrf=s.new_session(uid)
        body=json.dumps({'name':'fixture','note':'','options':['pointcloud']}).encode('utf-8')
        headers={'Content-Type':'application/json','Cookie':'ws_session='+token,'Idempotency-Key':key()}
        self.conn.request('POST','/workspace/api/projects',body=body,headers={**headers,'Origin':'https://untrusted.invalid','X-CSRF-Token':csrf})
        r=self.conn.getresponse();self.assertEqual(r.status,403);r.read()
        self.conn.request('POST','/workspace/api/projects',body=body,headers={**headers,'Origin':self.origin})
        r=self.conn.getresponse();self.assertEqual(r.status,403);r.read()
        self.assertEqual(s.projects({'id':uid,'role':'user'})['projects'],[])

    def test_admin_zip_and_cross_user_http_isolation(self):
        s=self.httpd.RequestHandlerClass.store
        aid=s.create_user('owner-http','test-password-123');bid=s.create_user('other-http','test-password-123')
        mid=s.create_user('admin-http','test-password-123',role='admin')
        owner={'id':aid,'role':'user'}
        pid=s.create_project(owner,{'name':'fixture','note':'','options':['pointcloud']},key(),key())['project']['id']
        data=b'synthetic mcap bytes';name='folder/data/data_raw.mcap'
        up=s.create_upload(owner,pid,{'kind':'raw','chunk_size':mod.CHUNK_SIZE,'files':[entry(name,data)]},key(),key())['upload'];fid=up['files'][0]['id']
        s.put_chunk(owner,up['id'],fid,0,data,hashlib.sha256(data).hexdigest());s.complete_upload(owner,up['id'])
        other_token,_=s.new_session(bid)
        self.conn.request('GET','/workspace/api/files/'+fid+'/download',headers={'Cookie':'ws_session='+other_token})
        r=self.conn.getresponse();self.assertEqual(r.status,404);r.read()
        admin_token,_=s.new_session(mid)
        self.conn.request('GET','/workspace/api/admin/projects/'+pid+'/raw.zip',headers={'Cookie':'ws_session='+admin_token})
        r=self.conn.getresponse();self.assertEqual(r.status,200);archive=r.read()
        with zipfile.ZipFile(io.BytesIO(archive)) as z:
            self.assertEqual(z.namelist(),[name]);self.assertEqual(z.read(name),data)

    def test_old_file_range_uses_persisted_chunk_size_after_upgrade(self):
        saved=mod.CHUNK_SIZE,http_app.CHUNK_SIZE
        try:
            mod.CHUNK_SIZE=http_app.CHUNK_SIZE=4
            s=self.httpd.RequestHandlerClass.store
            uid=s.create_user('range-review','test-password-123');actor={'id':uid,'role':'user'}
            pid=s.create_project(actor,{'name':'fixture','note':'','options':['pointcloud']},key(),key())['project']['id']
            data=b'abcdef';up=s.create_upload(actor,pid,{'kind':'raw','chunk_size':mod.CHUNK_SIZE,'files':[entry('raw.mcap',data)]},key(),key())['upload']
            fid=up['files'][0]['id']
            for idx in (0,1):
                part=data[idx*4:(idx+1)*4]
                s.put_chunk(actor,up['id'],fid,idx,part,hashlib.sha256(part).hexdigest())
            s.complete_upload(actor,up['id']);token,_=s.new_session(uid)
            mod.CHUNK_SIZE=http_app.CHUNK_SIZE=8
            self.conn.request('GET','/workspace/api/files/'+fid+'/download',headers={'Cookie':'ws_session='+token,'Range':'bytes=4-5'})
            r=self.conn.getresponse();self.assertEqual(r.status,206);self.assertEqual(r.read(),b'ef')
        finally:
            mod.CHUNK_SIZE,http_app.CHUNK_SIZE=saved


if __name__ == '__main__':
    unittest.main(verbosity=2)
