import json
import threading
import urllib.request
import urllib.error
import pytest
from pilot.store import Store,protect,unprotect,origin,identifier
from pilot.social import conversation_url

def crm(s):return s.add_connection({'platform':'dolibarr','base_url':'http://127.0.0.1:8088','login':'tester','password':'private-test-password'})
def task(s,c):return s.create_run({'connection_id':c['id'],'kind':'update','name':'Test Company','fields':{'city':'Portland'}})

def test_credentials_roundtrip_and_public_redaction(tmp_path):
    s=Store(tmp_path);c=crm(s)
    assert 'private-test-password' not in json.dumps(s.connections())
    assert 'password' not in c['config']
    assert s.connection(c['id'],True)['config']['password']=='private-test-password'
    with s.db() as db:secret=db.execute('SELECT secret FROM connections').fetchone()[0]
    assert 'private-test-password' not in secret
    assert unprotect(secret)=='private-test-password'

@pytest.mark.parametrize('url',['file:///C:/secret','https://user:password@example.com','http://example.com/path','https://example.com/?token=secret','http://169.254.169.254','javascript:alert(1)'])
def test_connection_rejects_non_origin_and_credentials(url):
    with pytest.raises(ValueError):origin(url)

def test_restart_preserves_original_intent_and_contract(tmp_path):
    s=Store(tmp_path);c=crm(s);r=task(s,c)
    s.update_run(r['id'],'running',result={'send_intent':True,'phase':'send_intent'})
    Store(tmp_path).recover_startup();saved=s.run(r['id'])
    assert saved['status']=='interrupted'
    assert saved['payload']==r['payload'] and saved['result']['send_intent']
    assert saved['attempt']==0

def test_submission_fence_is_not_limited_to_recent_100_tasks(tmp_path):
    s=Store(tmp_path);c=crm(s);r=task(s,c)
    s.update_run(r['id'],'uncertain',result={'send_intent':True})
    for _ in range(102):task(s,c)
    assert r['id'] not in {x['id'] for x in s.runs()}
    assert s.pending_messages(c['id'],'other')

def test_self_message_requires_verified_conversation_and_immutable_marker(tmp_path):
    s=Store(tmp_path);c=s.add_connection({'platform':'instagram','handle':'own.account'})
    data={'connection_id':c['id'],'kind':'self_message','message':'Test only'}
    with pytest.raises(ValueError):s.create_run(data)
    s.connection_status(c['id'],'ready','Verified',{'conversation':'https://www.instagram.com/direct/t/123'})
    r=s.create_run(data)
    assert r['payload']['sender']=='own.account'
    assert r['payload']['marker'].startswith('UIAGENT-IG-')
    assert s.run(r['id'])['payload']==r['payload']
    with pytest.raises(ValueError):s.create_run({**data,'kind':'message_others'})

@pytest.mark.parametrize('platform,url',[('instagram','https://evil.test/direct/t/123'),('messenger','https://www.messenger.com/marketplace/t/123'),('instagram','https://www.instagram.com/direct/inbox/'),('messenger','https://www.messenger.com@evil.test/t/123')])
def test_conversation_binding_rejects_wrong_origin_or_nonthread(platform,url):
    with pytest.raises(ValueError):conversation_url(platform,url)

def test_patch_rejects_unbounded_action_and_explicit_clear_works(tmp_path):
    s=Store(tmp_path);c=crm(s)
    with pytest.raises(ValueError):s.create_run({'connection_id':c['id'],'kind':'delete','name':'Test','fields':{'city':'X'}})
    with pytest.raises(ValueError):s.create_run({'connection_id':c['id'],'kind':'update','name':'Test','fields':{'password':'X'}})
    r=s.create_run({'connection_id':c['id'],'kind':'update','name':'Test','fields':{'city':''}})
    assert r['payload']['desired']=={'city':''}

def test_http_boundary_csrf_host_path_and_credential_redaction(tmp_path,monkeypatch):
    import app
    monkeypatch.setattr(app,'health',lambda:{'browser':True,'planner':True})
    class Manager:
        def submit(self,*args,**kwargs):pass
    s=Store(tmp_path);crm(s);server=app.Server(('127.0.0.1',0),s,Manager())
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    def request(path,body=None,headers=None):
        req=urllib.request.Request(server.origin+path,data=json.dumps(body).encode() if body is not None else None,headers=headers or {})
        try:
            with urllib.request.urlopen(req) as response:return response.status,response.read()
        except urllib.error.HTTPError as e:return e.code,e.read()
    try:
        status,body=request('/api/state');assert status==200 and b'private-test-password' not in body
        token=json.loads(body)['token'];headers={'Content-Type':'application/json','Origin':server.origin,'X-BrowserPilot-Token':token}
        assert request('/api/connections',{},headers={'Content-Type':'application/json'})[0]==403
        assert request('/api/connections',{},headers={**headers,'Origin':'https://evil.test'})[0]==403
        assert request('/api/state',headers={'Host':'evil.test'})[0]==403
        assert request('/../app.py')[0]==404
        assert request('/evidence/../../app.py')[0]==404
        assert request('/api/connections',{'platform':'instagram','handle':'own.test'},headers)[0]==201
        assert len(s.connections())==2
    finally:server.shutdown();server.server_close();thread.join()
