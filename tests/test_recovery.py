import json,subprocess,sys
from pathlib import Path
import pytest
from uiagent.journal import Journal,Busy,ContractMismatch
from uiagent.recovery import RecoveryEngine,changed

def contract(kind='create'):
    return {'operation_id':'op-one','platform':'dolibarr','kind':kind,'name':'Exact Company','desired':{'email':'a@example.invalid'},'isolation':'optimistic_ui','origin':'http://local.test'}

def test_operation_id_cannot_be_rebound_to_another_payload(tmp_path):
    j=Journal(tmp_path/'journal.sqlite');c=contract();j.ensure(c)
    with pytest.raises(ContractMismatch):j.ensure({**c,'desired':{'email':'b@example.invalid'}})
    assert j.load(c['operation_id'])['phase']=='new'

def test_commit_intent_survives_abrupt_process_exit(tmp_path):
    path=tmp_path/'journal.sqlite';c=contract()
    code='from uiagent.journal import Journal;import json,os,sys;j=Journal(sys.argv[1]);c=json.loads(sys.argv[2]);j.ensure(c);j.record(c["operation_id"],"commit_intent",phase="commit_intent");os._exit(73)'
    r=subprocess.run([sys.executable,'-c',code,str(path),json.dumps(c)],timeout=10)
    assert r.returncode==73
    assert Journal(path).load(c['operation_id'])['phase']=='commit_intent'
    assert Journal(path).events(c['operation_id'])[0]['event']=='commit_intent'

def test_kernel_lock_blocks_other_process_then_releases_on_crash(tmp_path):
    path=tmp_path/'journal.sqlite'
    code='from uiagent.journal import Journal;import sys,time\nj=Journal(sys.argv[1])\nwith j.resource_lock("same-record"):\n print("locked",flush=True)\n time.sleep(30)'
    p=subprocess.Popen([sys.executable,'-c',code,str(path)],stdout=subprocess.PIPE,text=True)
    try:
        assert p.stdout.readline().strip()=='locked'
        j=Journal(path)
        with pytest.raises(Busy):
            with j.resource_lock('same-record'):pass
        with j.resource_lock('another-record'):pass
    finally:p.kill();p.wait(timeout=10)
    with Journal(path).resource_lock('same-record'):pass

class FakeBrowser:
    def __init__(self,*args,**kwargs):self.network_log=[]
    def __enter__(self):return self
    def __exit__(self,*args):pass

class ReadAdapter:
    base='http://local.test';atomic_compare_and_swap=False
    def __init__(self,live):self.live=live
    def login(self,ui):pass
    def find(self,ui,name):return dict(self.live)

def run_read_only_branch(tmp_path,monkeypatch,c,phase,live,baseline=None):
    import uiagent.recovery as recovery
    monkeypatch.setattr(recovery,'BrowserUI',FakeBrowser)
    def forbidden(*args,**kwargs):raise AssertionError('This recovery branch must not invoke the planner')
    monkeypatch.setattr(recovery,'CodexPlanner',forbidden)
    j=Journal(tmp_path/'journal.sqlite');j.ensure(c);j.record(c['operation_id'],'setup',phase=phase,baseline=baseline)
    return RecoveryEngine(ReadAdapter(live),j,tmp_path/'result',tmp_path/'empty').run(c)

@pytest.mark.parametrize('phase',['commit_intent','uncertain','confirmed'])
def test_missing_uncertain_create_never_replays_submission(tmp_path,monkeypatch,phase):
    r=run_read_only_branch(tmp_path,monkeypatch,contract(),phase,{'count':0,'state':None,'url':None})
    assert r['status']=='uncertain' and r['planner_calls']==[] and r['actions']==[]

def test_verified_retry_does_no_model_or_ui_mutation(tmp_path,monkeypatch):
    state={'name':'Exact Company','email':'a@example.invalid','record_type':'prospect'}
    r=run_read_only_branch(tmp_path,monkeypatch,contract(),'commit_intent',{'count':1,'state':state,'url':'http://local.test/1'})
    assert r['status']=='confirmed' and not r['actions'] and not r['planner_calls']

def test_drift_after_confirmed_create_is_not_silently_overwritten(tmp_path,monkeypatch):
    state={'name':'Exact Company','email':'changed@example.invalid','record_type':'prospect'}
    r=run_read_only_branch(tmp_path,monkeypatch,contract(),'confirmed',{'count':1,'state':state,'url':'http://local.test/1'})
    assert r['status']=='blocked' and not r['actions']

def test_pending_update_conflict_blocks_before_replanning(tmp_path,monkeypatch):
    original={'name':'Exact Company','email':'old@example.invalid','record_type':'prospect'}
    live={**original,'email':'concurrent@example.invalid'}
    r=run_read_only_branch(tmp_path,monkeypatch,contract('update'),'commit_intent',{'count':1,'state':live,'url':'http://local.test/1'},original)
    assert r['status']=='blocked' and not r['actions']

def test_unconfirmed_update_is_not_replayed_even_when_old_values_remain(tmp_path,monkeypatch):
    state={'name':'Exact Company','email':'old@example.invalid','record_type':'prospect'}
    r=run_read_only_branch(tmp_path,monkeypatch,contract('update'),'commit_intent',{'count':1,'state':state,'url':'http://local.test/1'},state)
    assert r['status']=='uncertain' and not r['actions']

def test_replay_does_not_overwrite_a_human_revert_after_confirmation(tmp_path,monkeypatch):
    state={'name':'Exact Company','email':'old@example.invalid','record_type':'prospect'}
    r=run_read_only_branch(tmp_path,monkeypatch,contract('update'),'confirmed',{'count':1,'state':state,'url':'http://local.test/1'},state)
    assert r['status']=='blocked' and not r['actions']

def test_strict_isolation_fails_closed_without_ui_capability(tmp_path,monkeypatch):
    c={**contract('update'),'isolation':'serializable'}
    r=run_read_only_branch(tmp_path,monkeypatch,c,'new',{'count':0,'state':None,'url':None})
    assert r['status']=='blocked' and r['ui_probes']==0 and not r['actions']

def test_new_operation_id_cannot_bypass_unresolved_submission(tmp_path,monkeypatch):
    prior=contract();j=Journal(tmp_path/'journal.sqlite');j.ensure(prior)
    j.record(prior['operation_id'],'commit_intent',phase='commit_intent')
    current={**prior,'operation_id':'new-op'}
    r=run_read_only_branch(tmp_path,monkeypatch,current,'new',{'count':0,'state':None,'url':None})
    assert r['status']=='waiting' and r['planner_calls']==[] and r['actions']==[]

def test_retry_cannot_silently_switch_crm_deployments(tmp_path):
    c=contract();j=Journal(tmp_path/'journal.sqlite');j.ensure(c)
    adapter=ReadAdapter({'count':0,'state':None,'url':None});adapter.base='http://different.test'
    incoming={k:v for k,v in c.items() if k!='origin'}
    r=RecoveryEngine(adapter,j,tmp_path/'out',tmp_path/'empty').run(incoming)
    assert r['status']=='contract_mismatch' and not r['actions']

def test_concurrent_phone_formatting_does_not_hide_digit_changes():
    assert changed({'phone':'+12025550188'},{'phone':'+1 202-555-0188'})==set()
    assert changed({'phone':'+12025550188'},{'phone':'+12025550199'})=={'phone'}

def test_conflict_does_not_release_an_unresolved_submission_fence(tmp_path,monkeypatch):
    c=contract('update');original={'name':c['name'],'email':'old@example.invalid','record_type':'prospect'}
    live={'count':1,'state':{**original,'email':'concurrent@example.invalid'},'url':'http://local.test/1'}
    first=run_read_only_branch(tmp_path,monkeypatch,c,'commit_intent',live,original)
    assert first['status']=='blocked'
    second=run_read_only_branch(tmp_path,monkeypatch,{**c,'operation_id':'new-operation'},'new',live)
    assert second['status']=='waiting' and not second['actions'] and not second['planner_calls']
