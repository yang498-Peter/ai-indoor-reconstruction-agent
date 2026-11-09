"""Synthetic end-to-end tests. No customer captures or production accounts are used."""
import hashlib
import base64
import http.client
import io
import json
import tempfile
import threading
import unittest
import uuid
import zipfile
from pathlib import Path
from unittest.mock import patch

from server.app import serve
from server.store import CHUNK_SIZE


def key(): return str(uuid.uuid4())


class Client:
    def __init__(self, port):
        self.port=port;self.cookie='';self.csrf='';self.origin='http://127.0.0.1:'+str(port)

    def call(self,method,path,data=None,headers=None):
        con=http.client.HTTPConnection('127.0.0.1',self.port,timeout=30)
        h={'Origin':self.origin}
        if self.cookie:h['Cookie']=self.cookie
        if self.csrf:h['X-CSRF-Token']=self.csrf
        if headers:h.update(headers)
        if isinstance(data,dict):
            h['Content-Type']='application/json';data=json.dumps(data,ensure_ascii=False,separators=(',',':')).encode('utf-8')
        con.request(method,'/workspace/api/'+path,body=data,headers=h)
        response=con.getresponse();body=response.read();status=response.status;result_headers=dict(response.getheaders())
        if response.getheader('Set-Cookie'):
            self.cookie=response.getheader('Set-Cookie').split(';',1)[0]
        con.close()
        if result_headers.get('Content-Type','').startswith('application/json'):
            body=json.loads(body.decode('utf-8'))
        if isinstance(body,dict) and body.get('csrf_token'):self.csrf=body['csrf_token']
        return status,body,result_headers

    def register(self,name):
        status,body,_=self.call('POST','auth/register',{'username':name,'password':'test-password-123'})
        assert status==200,(status,body)
        return body

    def login(self,name,secret):
        return self.call('POST','auth/login',{'username':name,'password':secret})

    def project(self,name='演示项目'):
        return self.call('POST','projects',{'name':name,'options':['pointcloud','ifc'],'note':''},{'Idempotency-Key':key()})

    def upload(self,pid,kind,files,token=None):
        files=[dict(f,expected_hashes=f.get('expected_hashes',['0'*64]*((f['size']+CHUNK_SIZE-1)//CHUNK_SIZE))) for f in files]
        return self.call('POST','projects/'+pid+'/uploads',{'kind':kind,'chunk_size':CHUNK_SIZE,'files':files},{'Idempotency-Key':token or key()})

    def put(self,up,fid,index,data,hash_value=None):
        return self.call('PUT','uploads/'+up+'/files/'+fid+'/chunks/'+str(index),data,
                         {'X-Chunk-SHA256':hash_value or hashlib.sha256(data).hexdigest(),'Content-Type':'application/octet-stream'})


class WorkspaceE2E(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.httpd=serve(self.tmp.name,'http://127.0.0.1:1',port=0,min_free=0)
        self.port=self.httpd.server_address[1]
        self.httpd.RequestHandlerClass.origin='http://127.0.0.1:'+str(self.port)
        self.httpd.RequestHandlerClass.store.create_user('admin','initial-admin-pass','admin',True)
        self.thread=threading.Thread(target=self.httpd.serve_forever,daemon=True);self.thread.start()
        self.owner=Client(self.port);self.owner.register('customer')
        self.other=Client(self.port);self.other.register('other-customer')
        self.admin=Client(self.port)
        status,body,_=self.admin.login('admin','initial-admin-pass');self.assertEqual(status,200)
        self.assertTrue(body['user']['must_change_password'])
        self.assertEqual(self.admin.call('GET','admin/projects')[0],403)
        self.assertEqual(self.admin.call('POST','auth/password',{'current_password':'initial-admin-pass','new_password':'changed-admin-pass'})[0],200)

    def tearDown(self):
        self.httpd.shutdown();self.httpd.server_close();self.thread.join(timeout=3);self.tmp.cleanup()

    def test_full_manual_delivery_privacy_and_resume(self):
        body={'name':'项目一','options':['pointcloud','ifc'],'note':'人工处理'}
        idem=key()
        status,first,_=self.owner.call('POST','projects',body,{'Idempotency-Key':idem});self.assertEqual(status,200)
        status,again,_=self.owner.call('POST','projects',body,{'Idempotency-Key':idem});self.assertEqual(first,again)
        self.assertEqual(self.owner.call('POST','projects',dict(body,name='不同'),{'Idempotency-Key':idem})[0],409)
        pid=first['project']['id']
        self.assertEqual(self.other.call('GET','projects/'+pid)[0],404)
        self.assertEqual(self.admin.call('PATCH','admin/projects/'+pid,{'status':'processing','message':''})[0],409)
        meta=b'metadata: synthetic\n';raw=b'R'*(CHUNK_SIZE+19)
        files=[{'path':'sample/metadata.yaml','size':len(meta),'last_modified':0,'expected_hashes':[hashlib.sha256(meta).hexdigest()]},
               {'path':'sample/data/data_raw.mcap','size':len(raw),'last_modified':0,
                'expected_hashes':[hashlib.sha256(raw[:CHUNK_SIZE]).hexdigest(),hashlib.sha256(raw[CHUNK_SIZE:]).hexdigest()]}]
        status,created,_=self.owner.upload(pid,'raw',files);self.assertEqual(status,200)
        up=created['upload'];fid={f['path']:f['id'] for f in up['files']}
        self.assertEqual(self.owner.call('GET','auth/me')[1]['quota']['reserved'],len(meta)+len(raw))
        self.assertEqual(self.other.call('GET','uploads/'+up['id'])[0],404)
        self.assertEqual(self.owner.put(up['id'],fid['sample/metadata.yaml'],0,meta)[0],200)
        self.assertEqual(self.owner.put(up['id'],fid['sample/data/data_raw.mcap'],0,raw[:CHUNK_SIZE])[0],200)
        self.assertEqual(self.owner.call('POST','uploads/'+up['id']+'/complete')[0],409)
        state=self.owner.call('GET','uploads/'+up['id'])[1]['upload']
        resumed=next(f for f in state['files'] if f['id']==fid['sample/data/data_raw.mcap'])
        self.assertEqual(resumed['uploaded_chunks'],[0]);self.assertEqual(resumed['chunk_hashes']['0'],hashlib.sha256(raw[:CHUNK_SIZE]).hexdigest())
        self.assertTrue(self.owner.put(up['id'],fid['sample/data/data_raw.mcap'],0,raw[:CHUNK_SIZE])[1]['already_present'])
        self.assertEqual(self.owner.put(up['id'],fid['sample/data/data_raw.mcap'],0,b'X'*CHUNK_SIZE)[0],409)
        self.assertEqual(self.owner.put(up['id'],fid['sample/data/data_raw.mcap'],1,b'X'*19)[1]['error']['code'],'SOURCE_CHANGED')
        self.assertEqual(self.owner.put(up['id'],fid['sample/data/data_raw.mcap'],1,raw[CHUNK_SIZE:])[0],200)
        self.assertEqual(self.owner.call('POST','uploads/'+up['id']+'/complete')[0],200)
        self.assertEqual(self.owner.call('POST','uploads/'+up['id']+'/complete')[0],200)
        self.assertEqual(self.owner.call('GET','projects/'+pid)[1]['project']['status'],'submitted')
        self.assertEqual(self.owner.call('GET','auth/me')[1]['quota']['used'],len(meta)+len(raw))
        status,data,headers=self.admin.call('GET','admin/files/'+fid['sample/data/data_raw.mcap']+'/download',headers={'Range':'bytes=7-15'})
        self.assertEqual((status,data),(206,raw[7:16]));self.assertEqual(headers['Content-Range'],'bytes 7-15/'+str(len(raw)))
        status,zipped,_=self.admin.call('GET','admin/projects/'+pid+'/raw.zip');self.assertEqual(status,200)
        with zipfile.ZipFile(io.BytesIO(zipped)) as z:
            self.assertEqual(z.read('sample/metadata.yaml'),meta);self.assertEqual(z.read('sample/data/data_raw.mcap'),raw)
        self.assertEqual(self.admin.call('PATCH','admin/projects/'+pid,{'status':'processing','message':'正在处理'})[0],200)
        pdf=b'%PDF-1.4\nsynthetic result\n'
        png=base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aKX0AAAAASUVORK5CYII=')
        fake=b'<script>not an image</script>'
        outputs=[{'path':'result/report.pdf','size':len(pdf),'last_modified':0,'expected_hashes':[hashlib.sha256(pdf).hexdigest()]},
                 {'path':'result/photo.png','size':len(png),'last_modified':0,'expected_hashes':[hashlib.sha256(png).hexdigest()]},
                 {'path':'result/fake.png','size':len(fake),'last_modified':0,'expected_hashes':[hashlib.sha256(fake).hexdigest()]}]
        status,result,_=self.admin.upload(pid,'result',outputs);self.assertEqual(status,200)
        r=result['upload'];result_ids={f['path']:f['id'] for f in r['files']};rfid=result_ids['result/report.pdf']
        self.assertEqual(self.admin.put(r['id'],rfid,0,pdf)[0],200)
        self.assertEqual(self.admin.put(r['id'],result_ids['result/photo.png'],0,png)[0],200)
        self.assertEqual(self.admin.put(r['id'],result_ids['result/fake.png'],0,fake)[0],200)
        self.assertEqual(self.owner.call('PATCH','projects/'+pid,{'is_public':True})[0],409)
        self.assertEqual(self.admin.call('POST','uploads/'+r['id']+'/complete')[0],200)
        self.assertEqual(self.owner.call('GET','files/'+rfid+'/download')[0],404)
        self.assertEqual(self.admin.call('PATCH','admin/projects/'+pid,{'status':'delivered','message':'成果已交付'})[0],200)
        status,published,_=self.owner.call('PATCH','projects/'+pid,{'is_public':True});self.assertEqual(status,200)
        token=published['project']['public_url'].split('/')[-1]
        anonymous=Client(self.port)
        self.assertEqual(anonymous.call('GET','public/'+token)[0],200)
        self.assertEqual(anonymous.call('GET','public/'+token+'/files/'+rfid+'/download')[1],pdf)
        status,body,headers=anonymous.call('GET','public/'+token+'/files/'+result_ids['result/photo.png']+'/preview')
        self.assertEqual((status,body),(200,png));self.assertEqual(headers['Content-Type'],'image/png')
        self.assertEqual(anonymous.call('GET','public/'+token+'/files/'+result_ids['result/fake.png']+'/preview')[0],415)
        self.assertEqual(anonymous.call('GET','public/'+token+'/files/'+rfid+'/preview')[2]['Content-Type'],'application/pdf')
        self.assertEqual(anonymous.call('GET','public/'+token+'/files/'+fid['sample/metadata.yaml']+'/download')[0],404)
        self.assertEqual(self.other.call('GET','files/'+rfid+'/download')[0],404)
        self.assertEqual(self.admin.upload(pid,'result',[{'path':'new.ifc','size':10,'last_modified':0}])[0],409)
        self.assertEqual(self.owner.call('PATCH','projects/'+pid,{'is_public':False})[0],200)
        self.assertEqual(anonymous.call('GET','public/'+token)[0],404)
        token2=self.owner.call('PATCH','projects/'+pid,{'is_public':True})[1]['project']['public_url'].split('/')[-1]
        self.assertNotEqual(token,token2)
        self.assertEqual(self.owner.call('DELETE','projects/'+pid)[0],200)
        self.assertEqual(anonymous.call('GET','public/'+token2)[0],404)
        self.assertEqual(self.owner.call('GET','auth/me')[1]['quota']['used'],0)
        self.assertFalse((Path(self.tmp.name)/'chunks'/up['id']).exists())

    def test_messages_and_supplementary_folder(self):
        pid=self.owner.project()[1]['project']['id'];raw=b'raw'
        initial=[{'path':'S1/data/data_raw.mcap','size':len(raw),'last_modified':0,'expected_hashes':[hashlib.sha256(raw).hexdigest()]}]
        up=self.owner.upload(pid,'raw',initial)[1]['upload'];self.owner.put(up['id'],up['files'][0]['id'],0,raw)
        self.assertEqual(self.owner.call('POST','uploads/'+up['id']+'/complete')[0],200)
        self.assertEqual(self.admin.call('PATCH','admin/projects/'+pid,{'status':'needs_info','message':'请补充数据'})[0],200)
        payload={'text':'会补充一个文件夹'};idem=key()
        sent=self.owner.call('POST','projects/'+pid+'/messages',payload,{'Idempotency-Key':idem})
        self.assertEqual(sent[0],200);self.assertEqual(self.owner.call('POST','projects/'+pid+'/messages',payload,{'Idempotency-Key':idem})[1],sent[1])
        self.assertEqual(len(self.admin.call('GET','admin/projects/'+pid)[1]['messages']),2)
        self.assertEqual(self.other.call('POST','projects/'+pid+'/messages',payload,{'Idempotency-Key':key()})[0],404)
        collision=[{'path':'s1/DATA/data_raw.mcap','size':1,'last_modified':0,'expected_hashes':[hashlib.sha256(b'x').hexdigest()]}]
        self.assertEqual(self.owner.upload(pid,'raw',collision)[1]['error']['code'],'DUPLICATE_PATH')
        extra=b'extra';supp=[{'path':'S1/info/readme.txt','size':len(extra),'last_modified':0,'expected_hashes':[hashlib.sha256(extra).hexdigest()]}]
        up2=self.owner.upload(pid,'raw',supp)[1]['upload']
        self.assertEqual(self.admin.call('PATCH','admin/projects/'+pid,{'status':'processing','message':''})[0],409)
        self.owner.put(up2['id'],up2['files'][0]['id'],0,extra)
        self.assertEqual(self.owner.call('POST','uploads/'+up2['id']+'/complete')[0],200)
        self.assertEqual(self.owner.call('GET','projects/'+pid)[1]['project']['status'],'submitted')
        status,zipped,_=self.admin.call('GET','admin/projects/'+pid+'/raw.zip');self.assertEqual(status,200)
        with zipfile.ZipFile(io.BytesIO(zipped)) as z:
            self.assertEqual(z.read('S1/data/data_raw.mcap'),raw);self.assertEqual(z.read('S1/info/readme.txt'),extra)

    def test_limit_reservation_and_cross_origin(self):
        self.assertEqual(self.owner.call('POST','projects',{'name':'x','options':['model']},
                         {'Origin':'https://untrusted.example','Idempotency-Key':key()})[0],403)
        old=self.owner.csrf;self.owner.csrf='wrong'
        self.assertEqual(self.owner.call('POST','projects',{'name':'x','options':['model']},{'Idempotency-Key':key()})[0],403)
        self.owner.csrf=old
        pid=self.owner.project()[1]['project']['id']
        self.assertEqual(self.owner.upload(pid,'raw',[{'path':'folder/big.mcap','size':5_000_000_001,'last_modified':0}])[0],400)
        self.assertEqual(self.owner.upload(pid,'raw',[{'path':'../secret','size':1,'last_modified':0}])[0],400)
        usage=type('Disk',(),{'free':100_000_000_000})()
        with patch('server.store.shutil.disk_usage',return_value=usage):
            for i in range(4):
                project=pid if i==0 else self.owner.project('p'+str(i))[1]['project']['id']
                status,_,_=self.owner.upload(project,'raw',[{'path':'folder/raw.mcap','size':5_000_000_000,'last_modified':0}])
                self.assertEqual(status,200)
            fifth=self.owner.project('p5')[1]['project']['id']
            self.assertEqual(self.owner.upload(fifth,'raw',[{'path':'folder/raw.mcap','size':1,'last_modified':0}])[0],413)
            self.assertEqual(self.owner.call('GET','auth/me')[1]['quota']['reserved'],20_000_000_000)

    def test_keepalive_error_does_not_hang(self):
        conn=http.client.HTTPConnection('127.0.0.1',self.port,timeout=5)
        conn.request('GET','/workspace/api/auth/me',headers={'Cookie':self.owner.cookie})
        response=conn.getresponse();response.read();self.assertEqual(response.status,200)
        conn.request('GET','/workspace/api/projects/00000000-0000-0000-0000-000000000000',headers={'Cookie':self.owner.cookie})
        response=conn.getresponse();response.read();self.assertEqual(response.status,404);conn.close()

    def test_admin_reset_forces_password_change_and_revokes_sessions(self):
        uid=self.owner.call('GET','auth/me')[1]['user']['id']
        self.assertEqual(self.admin.call('POST','admin/users/'+uid+'/reset-password',{'new_password':'temporary-pass-123'})[0],200)
        self.assertEqual(self.owner.call('GET','auth/me')[0],401)
        status,body,_=self.owner.login('customer','temporary-pass-123')
        self.assertEqual(status,200);self.assertTrue(body['user']['must_change_password'])
        self.assertEqual(self.owner.call('GET','projects')[1]['error']['code'],'MUST_CHANGE_PASSWORD')
        self.assertEqual(self.owner.call('POST','auth/password',{'current_password':'temporary-pass-123','new_password':'permanent-pass-123'})[0],200)
        self.assertEqual(self.owner.call('GET','projects')[0],200)


if __name__=='__main__': unittest.main()
