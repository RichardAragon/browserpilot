from copy import deepcopy
import pytest
from pilot.store import Store
from pilot.references import grounded_value, reference_changed
from pilot.website import WebsiteBrowser
from pilot.assistant import AssistantService, SCHEMA
from test_creation import create_fixture
from test_general import Page

REF={'field':'Account','source_name':'Previous opportunity','source_record_type':'Opportunity','source_field':'Account'}

def test_rendered_record_scopes_exclude_unrelated_records_and_search():
    from playwright.sync_api import sync_playwright
    from pilot.references import record_scopes,source_target
    with sync_playwright() as pw:
        try:browser=pw.chromium.launch(headless=True)
        except Exception as exc:
            if "Executable doesn't exist" in str(exc):pytest.skip('Install Playwright Chromium to run rendered-scope validation')
            raise
        try:
            page=browser.new_page()
            page.set_content('<div role="search"><input type="search" value="Previous opportunity"><span>Previous opportunity</span></div><article><b>Previous opportunity</b><span>Account A</span></article><article><b>Other opportunity</b><span>Account B</span></article>')
            scopes=record_scopes(page,'Previous opportunity')
            assert len(scopes)==1 and 'Account A' in scopes[0] and 'Account B' not in scopes[0]
            assert source_target(page,'Previous opportunity').count()==1
            page.set_content('<header>Account B</header><div role="search"><input value="Previous opportunity"></div><div><h1>Previous opportunity</h1><label>Title<input value="Previous opportunity"></label><label>Contact<input role="combobox" value="Account A"></label></div>')
            scopes=record_scopes(page,'Previous opportunity')
            assert scopes and all('Account B' not in s for s in scopes)
            assert any('Contact' in s and 'Account A' in s for s in scopes)
            page.set_content('<input type="search" value="Previous opportunity"><h1>Other opportunity</h1><input value="Account B">')
            assert record_scopes(page,'Previous opportunity')==[]
            with pytest.raises(ValueError):source_target(page,'Previous opportunity')
        finally:browser.close()

def task(tmp_path,**overrides):
    s=Store(tmp_path);c=s.add_connection({'platform':'website','base_url':'http://example.test'})
    data={'connection_id':c['id'],'kind':'browser_create','name':'Follow-up','record_type':'Opportunity','fields':{},'references':[dict(REF)]}
    data.update(overrides)
    return s,s.create_run(data)

def test_reference_contract_does_not_invent_account_value(tmp_path):
    s,r=task(tmp_path)
    assert r['payload']['desired']=={'Name':'Follow-up'}
    assert r['payload']['references']==[REF]
    assert s.run(r['id'])['payload']==r['payload']

@pytest.mark.parametrize('changes',[
    {'fields':{'Account':'Guessed account'}}, {'choose_existing':['Account']},
    {'references':[{**REF,'field':'Name'}]}, {'references':[{**REF,'source_name':'Follow-up'}]},
    {'references':[REF,REF]}, {'references':[{**REF,'source_field':''}]},
    {'references':[{**REF,'source_url':'http://other.test'}]},
    {'kind':'browser_update','fields':{'City':'Austin'}},
])
def test_reference_contract_rejects_ambiguous_or_unsupported_dependencies(tmp_path,changes):
    with pytest.raises(ValueError):task(tmp_path,**changes)

def test_scoped_live_evidence_required():
    response={'scope_index':0,'value':'Exact Company','quote':'Customer: Exact Company','actual_label':'Customer'}
    assert grounded_value(response,['Previous opportunity\nCustomer: Exact Company'])=='Exact Company'
    for scopes in ([],['Unrelated Company'],['Previous opportunity\nCustomer: Different Company']):
        with pytest.raises(ValueError):grounded_value(response,scopes)
    with pytest.raises(ValueError):grounded_value({**response,'scope_index':-1},['Customer: Exact Company'])

def test_source_drift_invalidates_snapshot_but_cosmetic_changes_do_not():
    binding={**REF,'value':'Company A','quote':'Customer Company A','url':'http://example.test/record'}
    reviewed={'references':[binding]}
    assert not reference_changed([{**binding,'quote':'Company A','url':'http://example.test/list'}],reviewed)
    assert reference_changed([{**binding,'value':'Company B'}],reviewed)
    assert reference_changed([{**binding,'source_name':'Different opportunity'}],reviewed)
    assert reference_changed([],reviewed)

def test_explicit_input_combobox_uses_real_selection_not_plain_text(tmp_path):
    o,a,p=create_fixture()
    o['controls'].append({'id':'account','tag':'input','role':'combobox','type':'text','name':'partner','label':'Contact?','value':'','options':['Exact Company'],'disabled':False,'text':''})
    p['desired']['Account']='Exact Company';a['bindings'].append({'field':'Account','target_id':'account'})
    browser=WebsiteBrowser(Store(tmp_path))
    assert browser.ready(Page(),o,a,p,creating=True)['fields']['Account']['custom_select']
    o['controls'][-1]['options']=[]
    with pytest.raises(ValueError,match='observe the requested'):browser.ready(Page(),o,a,p,creating=True)

def test_assistant_receives_verified_history_and_latest_request(tmp_path,monkeypatch):
    s,r=task(tmp_path,references=[])
    s.update_run(r['id'],'confirmed',result={'prepared':{'fields':{'Account':{'label':'Customer'}}},'proof':{'state':{'Name':'Follow-up','Account':'Company A'}}})
    captured={}
    class Model:
        def __init__(self,*args):pass
        def request(self,system,data,schema):
            captured.update(data)
            return {'message':'Proposed','proposal_kind':'none'}
    monkeypatch.setattr('pilot.assistant.StructuredModel',Model)
    ident=s.chat_add('assistant','Thinking','thinking')
    AssistantService(s).respond(ident,'Create another opportunity with the same Account')
    assert captured['current_request']=='Create another opportunity with the same Account'
    previous=captured['previous_verified_tasks'][0]
    assert previous['name']=='Follow-up' and previous['connection_id']==r['connection_id']
    assert previous['verified_fields'][1]['value']=='Company A'
    assert 'references' in SCHEMA['properties']

def test_context_excludes_unverified_and_other_connection_results(tmp_path):
    s,r=task(tmp_path)
    assert s.task_context()==[]
    s.update_run(r['id'],'confirmed',result={'proof':{'state':{'Name':'Follow-up'}}})
    assert s.task_context('different-connection')==[]

@pytest.mark.parametrize('scenario',['changed_source','lost_session_after_submit'])
def test_creation_dependency_guard_and_recovery(tmp_path,monkeypatch,scenario):
    from types import SimpleNamespace
    from threading import Event
    from uiagent.journal import Journal,digest
    s,r=task(tmp_path)
    binding={**REF,'value':'Company A','quote':'Account Company A','url':'http://example.test/source'}
    prepared={'identity':'Follow-up','url':'http://example.test/new','fields':{'Name':{'after':'Follow-up'},'Account':{'after':'Company A'}},'references':[binding]}
    s.update_run(r['id'],'running',result={'prepared':prepared,'approval':digest(prepared)})
    journal=Journal(s.root/'operations.sqlite');journal.ensure(r['payload'])
    if scenario=='lost_session_after_submit':journal.record(r['id'],'submit_intent',phase='commit_intent')
    else:journal.record(r['id'],'opening_form',phase='form_open')
    class BrowserPage:
        url='http://example.test/new'
        def goto(self,*a,**k):pass
        def close(self):pass
        def is_closed(self):return False
        def screenshot(self,*a,**k):pass
    page=BrowserPage();browser=WebsiteBrowser(s)
    browser.session=lambda cid:(SimpleNamespace(new_page=BrowserPage),page)
    browser.ui=lambda pg,*args:SimpleNamespace(page=pg)
    browser.drafts={} if scenario=='lost_session_after_submit' else {r['id']:page}
    browser.find_record=lambda *a,**k:{'found':scenario=='lost_session_after_submit','matches':1,'url':'http://example.test/list'}
    calls=[]
    def resolve(*a,**k):calls.append('read');return [{**binding,'value':'Company B'}]
    browser.resolve_references=resolve
    def prepare(ui,p,*args):
        assert p['desired']['Account']=='Company B'
        return {**prepared,'fields':{'Name':{'after':'Follow-up'},'Account':{'after':'Company B'}}},None
    browser.creation_form=prepare
    browser.verify_document=lambda *a,**k:{'Name':'Follow-up','Account':'Company A'}
    monkeypatch.setattr('pilot.creation.StructuredModel',lambda *a:SimpleNamespace(calls=[]))
    browser.create(s.run(r['id']),Event())
    result=s.run(r['id'])
    assert not any(e['event']=='create_clicked' for e in journal.events(r['id']))
    if scenario=='changed_source':
        assert result['status']=='awaiting_approval' and result['result']['approval'] is None
        assert result['result']['prepared']['fields']['Account']['after']=='Company B'
        assert calls==['read']
        assert not any(e['event']=='submit_intent' for e in journal.events(r['id']))
    else:
        assert result['status']=='confirmed'
        assert result['result']['proof']['state']['Account']=='Company A'
        assert calls==[] # recovery verifies the approved snapshot, not today's source
