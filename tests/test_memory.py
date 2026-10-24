import json
import pytest
from pilot.store import Store
from pilot.memory import PlatformMemory,sanitize

def fixture(tmp_path):
    s=Store(tmp_path);c=s.add_connection({'platform':'website','base_url':'http://localhost:8088'})
    s.connection_status(c['id'],'ready','Ready',{'discovery':{'summary':'Record list with Name search'}})
    return s,c['id']

def test_notes_persist_isolate_and_reject_stale_writes(tmp_path):
    s,i=fixture(tmp_path);other=s.add_connection({'platform':'website','base_url':'http://localhost:8191'})
    saved=s.save_notes(i,0,[],'Prefer all records')
    assert saved['revision']==1 and Store(tmp_path).platform_notes(i)['user_guidance']=='Prefer all records'
    assert s.platform_notes(other['id'])['revision']==0
    with pytest.raises(ValueError,match='changed'):s.save_notes(i,0,[],'stale')
    s.save_notes(i,1,[],'Correction')
    assert s.notes_history(i)[-1]['content']['user_guidance']=='Prefer all records'

def test_learning_preserves_user_guidance_and_has_provenance(tmp_path,monkeypatch):
    s,i=fixture(tmp_path);s.save_notes(i,0,[],'Clear restrictive filters')
    def request(self,system,data,schema):
        assert data['user_corrections']=='Clear restrictive filters'
        return {'notes':[{'category':'search','text':'Name search is available.','confidence':'observed','sources':['setup:'+i]}]}
    monkeypatch.setattr('pilot.memory.StructuredModel.request',request)
    PlatformMemory(s)._learn(i)
    n=s.platform_notes(i)
    assert n['status']=='ready' and n['revision']==2 and n['user_guidance']=='Clear restrictive filters'
    assert n['notes'][0]['sources']==['setup:'+i]

def test_concurrent_user_edit_wins_over_model(tmp_path,monkeypatch):
    s,i=fixture(tmp_path)
    def request(*args):
        s.save_notes(i,0,[],'Human correction during inference')
        return {'notes':[]}
    monkeypatch.setattr('pilot.memory.StructuredModel.request',request)
    PlatformMemory(s)._learn(i)
    assert s.platform_notes(i)['user_guidance']=='Human correction during inference'
    assert s.platform_notes(i)['revision']==1

def test_unknown_sources_cannot_replace_memory(tmp_path,monkeypatch):
    s,i=fixture(tmp_path)
    monkeypatch.setattr('pilot.memory.StructuredModel.request',lambda *a:{'notes':[{'category':'search','text':'Guess','confidence':'tentative','sources':['invented']}]})
    PlatformMemory(s)._learn(i)
    assert s.platform_notes(i)['status']=='error' and s.platform_notes(i)['notes']==[]

def test_learning_sanitizes_record_values_and_does_not_read_secrets(tmp_path):
    s,i=fixture(tmp_path)
    r=s.create_run({'connection_id':i,'kind':'browser_create','name':'Private Company','fields':{'Email':'private@example.invalid','City':'Seattle'}})
    s.event(r['id'],'progress','Search Private Company private@example.invalid Seattle https://host/private')
    evidence,_=PlatformMemory(s).evidence(i);text=json.dumps(evidence)
    assert 'Private Company' not in text and 'private@example.invalid' not in text and 'Seattle' not in text
    assert 'https://host' not in text and '[record value]' in text
    assert sanitize('g12-input-4')=='[control]'

def test_interrupted_learning_keeps_last_revision(tmp_path):
    s,i=fixture(tmp_path);s.save_notes(i,0,[],'Keep this');s.notes_status(i,'learning','Working')
    s.recover_startup()
    assert s.platform_notes(i)['status']=='interrupted' and s.platform_notes(i)['user_guidance']=='Keep this'
