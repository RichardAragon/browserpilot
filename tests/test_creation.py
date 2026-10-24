import inspect
from copy import deepcopy
import pytest
from pilot.store import Store
from pilot.website import WebsiteBrowser,value_of
from pilot.creation import search_absent
from pilot.setup import navigation,GenericSetup
from test_general import fixture,Page
from uiagent.journal import Journal

def create_fixture():
    o,a,p=fixture();o['controls'][0]['value']=''
    p['desired']['Name']=p['name'];a['bindings'].append({'field':'Name','target_id':'identity'})
    a['defaults']=[];o['controls'][2]['text']='Create record'
    return o,a,p

def test_creation_maps_blank_identity_and_explicit_name(tmp_path):
    o,a,p=create_fixture();r=WebsiteBrowser(Store(tmp_path)).ready(Page(),o,a,p,creating=True)
    assert r['fields']['Name']['before']=='' and r['fields']['Name']['after']==p['name']

def test_title_input_can_ground_name_without_platform_field_attributes(tmp_path):
    o,a,p=create_fixture();o['controls'][0].update(name='',label='e.g. Example Inc',dom_id='autocomplete_0',heading=True)
    assert WebsiteBrowser(Store(tmp_path)).ready(Page(),o,a,p,creating=True)['fields']['Name']['after']==p['name']
    o['controls'][0]['heading']=False
    with pytest.raises(ValueError):WebsiteBrowser(Store(tmp_path)).ready(Page(),o,a,p,creating=True)

@pytest.mark.parametrize('attack',['populated','name_mismatch','duplicate_binding','security_default'])
def test_creation_rejects_unsafe_mapping(tmp_path,attack):
    o,a,p=create_fixture()
    if attack=='populated':o['controls'][0]['value']='Some existing record'
    elif attack=='name_mismatch':a['bindings'][-1]['target_id']='city'
    elif attack=='duplicate_binding':a['defaults']=[{'target_id':'city','value':'x','reason':'required'}]
    else:
        o['controls'].append({'id':'role','tag':'select','type':'select-one','name':'role','label':'Permission role','value':'','options':['Admin'],'disabled':False,'text':''})
        a['defaults']=[{'target_id':'role','value':'Admin','reason':'required'}]
    with pytest.raises(ValueError):WebsiteBrowser(Store(tmp_path)).ready(Page(),o,a,p,creating=True)

def test_suggested_required_select_is_separate_reviewed_addition(tmp_path):
    o,a,p=create_fixture()
    o['controls'].append({'id':'status','tag':'select','type':'select-one','name':'category','label':'Classification','value':'0','selected_text':'Choose','options':['Choose','Prospect'],'disabled':False,'text':''})
    a['defaults']=[{'target_id':'status','value':'Prospect','reason':'Required classification; prospect makes no customer commitment'}]
    r=WebsiteBrowser(Store(tmp_path)).ready(Page(),o,a,p,creating=True)
    assert r['fields']['Suggested: Classification']['before']=='Choose'
    assert 'reason' in r['fields']['Suggested: Classification']

def test_absence_requires_submitted_visible_query_and_zero_result_evidence():
    o={'aria':'Search: Exact Company\nNo records found','controls':[]}
    assert search_absent(o,'Exact Company','No records found',True)
    assert not search_absent(o,'Exact Company','No records found',False)
    assert not search_absent(o,'Different Company','No records found',True)
    assert not search_absent(o,'Exact Company','Loading',True)
    assert not search_absent(o,'Exact Company','No contacts found',True)
    prompt='Create a Contact in your address book'
    observed={'aria':'Exact Company\n'+prompt,'controls':[],'empty_state_quotes':[prompt]}
    assert search_absent(observed,'Exact Company',prompt,True)
    observed['empty_state_quotes']=[]
    assert not search_absent(observed,'Exact Company',prompt,True)

def test_opening_new_form_fences_other_operations_even_before_field_entry(tmp_path):
    j=Journal(tmp_path/'journal.sqlite');p={'operation_id':'one','name':'Exact Company','origin':'https://crm.test'}
    j.ensure(p);j.record('one','open_form_intent',phase='commit_intent');j.record('one','opening_form',phase='form_open')
    assert j.load('one')['submission_pending']
    assert j.pending_for_identity({**p,'operation_id':'two'})==['one']

def test_generic_create_contract_and_name_conflict(tmp_path):
    s=Store(tmp_path);c=s.add_connection({'platform':'website','base_url':'https://crm.test'})
    r=s.create_run({'connection_id':c['id'],'kind':'browser_create','name':'Exact Company','fields':{'City':'Austin'}})
    assert r['payload']['desired']=={'Name':'Exact Company','City':'Austin'}
    alias=s.create_run({'connection_id':c['id'],'kind':'browser_create','name':'Exact Company','fields':{'Third-party name':'Exact Company','City':'Austin'}})
    assert alias['payload']['desired']==r['payload']['desired']
    with pytest.raises(ValueError):s.create_run({'connection_id':c['id'],'kind':'browser_create','name':'Exact Company','fields':{'Name':'Other'}})

def test_navigation_cannot_create_during_setup_or_search():
    with pytest.raises(ValueError):navigation({'text':'New','href':'/new'},'https://crm.test')
    navigation({'text':'New','href':'/new'},'https://crm.test',create=True)
    with pytest.raises(ValueError):navigation({'text':'Delete','href':'/delete'},'https://crm.test',create=True)
    with pytest.raises(ValueError):navigation({'text':'Contacts','href':'https://other.test'},'https://crm.test')

def test_general_setup_has_no_platform_adapter_dependency():
    import pilot.website,pilot.creation,pilot.setup
    for module in (pilot.website,pilot.creation,pilot.setup):
        text=inspect.getsource(module)
        assert 'RecordAdapter' not in text and 'uiagent.records' not in text

def test_generic_credentials_are_encrypted_and_not_public(tmp_path):
    s=Store(tmp_path);c=s.add_connection({'platform':'website','base_url':'https://crm.test','login':'test@example.invalid','password':'fake-test-secret'})
    assert c['has_password'] and 'password' not in c['config']
    assert s.connection(c['id'],True)['config']['password']=='fake-test-secret'

def test_lost_creation_form_never_reopens_new_or_replays_fields(tmp_path,monkeypatch):
    import threading
    import pilot.creation
    s=Store(tmp_path);c=s.add_connection({'platform':'website','base_url':'https://crm.test'})
    run=s.create_run({'connection_id':c['id'],'kind':'browser_create','name':'Crash Trial','fields':{}})
    j=Journal(tmp_path/'operations.sqlite');j.ensure(run['payload'])
    j.record(run['id'],'open_form_intent',phase='commit_intent');j.record(run['id'],'opening_form',phase='form_open')
    class StubPage:
        url='https://crm.test/'
        def goto(self,*a,**k):pass
        def is_closed(self):return False
        def close(self):pass
    class Context:
        def new_page(self):return StubPage()
    class Model:
        def __init__(self,*a):self.calls=[]
    monkeypatch.setattr(pilot.creation,'StructuredModel',Model)
    b=WebsiteBrowser(s)
    monkeypatch.setattr(b,'session',lambda ident:(Context(),StubPage()))
    monkeypatch.setattr(b,'ui',lambda *a:None)
    monkeypatch.setattr(b,'find_record',lambda *a:{'found':False,'url':'https://crm.test/','absence_quote':'No records found'})
    monkeypatch.setattr(b,'creation_form',lambda *a:pytest.fail('Reopened New after a lost creation session'))
    b.create(run,threading.Event())
    assert s.run(run['id'])['status']=='uncertain'
    assert s.run(run['id'])['result']['send_intent'] is True
    assert j.load(run['id'])['submission_pending'] is True


def test_discovery_error_can_resume_same_live_untouched_form(tmp_path,monkeypatch):
    import threading
    import pilot.creation
    s=Store(tmp_path);c=s.add_connection({'platform':'website','base_url':'https://crm.test'})
    run=s.create_run({'connection_id':c['id'],'kind':'browser_create','name':'Live Draft Trial','fields':{}})
    j=Journal(tmp_path/'operations.sqlite');opened=[]
    class StubPage:
        url='https://crm.test/new';closed=False
        def goto(self,*a,**k):pass
        def is_closed(self):return self.closed
        def close(self):self.closed=True
    class Context:
        def new_page(self):return StubPage()
    class Model:
        def __init__(self,*a):self.calls=[]
    monkeypatch.setattr(pilot.creation,'StructuredModel',Model)
    b=WebsiteBrowser(s)
    monkeypatch.setattr(b,'session',lambda ident:(Context(),StubPage()))
    monkeypatch.setattr(b,'ui',lambda page,*a:page)
    monkeypatch.setattr(b,'find_record',lambda *a:{'found':False,'url':'https://crm.test','absence_quote':'No records found'})
    def discovery(ui,p,client,run,stop,journal):
        if not opened:
            opened.append(ui);journal.record(run['id'],'open_form_intent',phase='commit_intent');journal.record(run['id'],'opening_form',phase='form_open')
            raise ValueError('Dropdown needs a fresh observation')
        assert ui is opened[0] and not ui.is_closed()
        return {'fields':{},'url':ui.url},None
    monkeypatch.setattr(b,'creation_form',discovery)
    b.create(run,threading.Event())
    assert s.run(run['id'])['status']=='blocked'
    assert j.load(run['id'])['phase']=='form_open' and j.load(run['id'])['submission_pending']
    b.create(s.run(run['id']),threading.Event())
    assert s.run(run['id'])['status']=='awaiting_approval'
    assert len([e for e in j.events(run['id']) if e['event']=='open_form_intent'])==1
