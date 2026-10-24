from copy import deepcopy
import pytest
from pilot.store import Store
from pilot.website import WebsiteBrowser,save_control
from pilot.setup import navigation
from test_creation import create_fixture
from test_general import Page

def test_sales_order_contract_preserves_object_and_delegated_customer(tmp_path):
    s=Store(tmp_path);c=s.add_connection({'platform':'website','base_url':'http://example.test'})
    r=s.create_run({'connection_id':c['id'],'kind':'browser_create','name':'YouTube test Opportunity','record_type':'Sales Order','choose_existing':['Customer'],'fields':{}})
    assert r['payload']['record_type']=='Sales Order'
    assert r['payload']['draft_only'] and r['payload']['choose_existing']==['Customer']
    assert r['payload']['desired']=={'Name':'YouTube test Opportunity'}
    alias=s.create_run({'connection_id':c['id'],'kind':'browser_create','name':'YouTube test Opportunity','record_type':'Sales Order','fields':{'External reference':'YouTube test Opportunity'}})
    assert alias['payload']['desired']=={'Name':'YouTube test Opportunity'}

def test_draft_document_maps_customer_reference_and_requires_draft_evidence(tmp_path):
    o,a,p=create_fixture();o['controls'][0].update(name='customer_ref',label='Customer reference')
    p.update(record_type='Sales Order',draft_only=True);o['aria']='Create draft';a['draft_evidence']='Create draft';o['controls'][2]['text']='Create draft'
    prepared=WebsiteBrowser(Store(tmp_path)).ready(Page(),o,a,p,creating=True)
    assert prepared['draft_only'] and prepared['fields']['Name']['label']=='Customer reference'
    a['draft_evidence']='Draft not on this page'
    with pytest.raises(ValueError,match='draft creation'):WebsiteBrowser(Store(tmp_path)).ready(Page(),o,a,p,creating=True)

def test_custom_existing_customer_must_be_observed_and_delegated(tmp_path):
    o,a,p=create_fixture();p['choose_existing']=['Customer']
    o['controls'].append({'id':'customer','tag':'span','role':'combobox','name':'','type':'','label':'Customer','value':'Choose a customer','options':['Existing Test Customer'],'disabled':False,'text':'Choose a customer'})
    a['defaults']=[{'field':'Customer','target_id':'customer','value':'Existing Test Customer','reason':'User authorized any existing customer'}]
    prepared=WebsiteBrowser(Store(tmp_path)).ready(Page(),o,a,p,creating=True)
    assert prepared['fields']['Suggested: Customer']['custom_select']
    p['choose_existing']=[]
    with pytest.raises(ValueError,match='not delegated'):WebsiteBrowser(Store(tmp_path)).ready(Page(),o,a,p,creating=True)
    p['choose_existing']=['Customer'];a['defaults'][0]['value']='Invented Customer'
    with pytest.raises(ValueError,match='observed select option'):WebsiteBrowser(Store(tmp_path)).ready(Page(),o,a,p,creating=True)

@pytest.mark.parametrize('label',['Create and confirm','Create and send','Validate','Issue invoice','Pay','Charge card'])
def test_creation_cannot_silently_advance_to_consequential_action(label):
    assert not save_control({'label':label,'text':label,'tag':'button'},creating=True)
    with pytest.raises(ValueError):navigation({'text':label},'http://example.test',create=True)

def test_assistant_policy_and_schema_preserve_general_objects():
    from pilot.assistant import SCHEMA,SYSTEM
    assert 'record_type' in SCHEMA['properties'] and 'choose_existing' in SCHEMA['properties']
    assert 'financial changes, account permissions' not in SYSTEM
    assert 'sales orders' in SYSTEM and 'draft' in SYSTEM


def test_assistant_uses_general_workflow_for_document_even_if_model_says_create(tmp_path,monkeypatch):
    from pilot import assistant
    from test_app import crm
    s=Store(tmp_path);c=crm(s)
    response={'message':'Prepare the draft for review','proposal_kind':'task','operation':'create','connection_id':c['id'],'record_type':'Sales Order','choose_existing':['Customer'],'fields':[{'label':'External reference','value':'Trial'}]}
    class Model:
        def __init__(self,*a):pass
        def request(self,*a):return response
    monkeypatch.setattr(assistant,'StructuredModel',Model)
    ident=s.chat_add('assistant','Thinking','thinking');assistant.AssistantService(s).respond(ident,'Create a sales order')
    message=s.chat_messages()[-1]
    assert message['status']=='done' and message['proposal']['operation']=='browser_create'
    assert message['proposal']['fields'][0]['label']=='External reference'


@pytest.mark.parametrize('empty_evidence',[True,False])
def test_search_navigation_timeout_requires_fresh_absence_evidence(tmp_path,monkeypatch,empty_evidence):
    import threading
    from types import SimpleNamespace
    from pilot import creation
    from playwright.sync_api import TimeoutError
    s=Store(tmp_path);b=WebsiteBrowser(s)
    field={'id':'search','name':'search_ref','label':'External ref','value':'','text':''}
    class Catalog:
        def __init__(self,ui):self.targets={'search':(None,field)}
        def observe(self,tag):
            return {'aria':'Trial\n'+('No records found' if empty_evidence else 'Loading'), 'controls':[field]}
        def act(self,a):
            if a['kind']=='fill':field['value']=a['value']
            else:raise TimeoutError('navigation response timed out')
    class Model:
        def __init__(self):self.steps=iter(['search','press_search','absent'])
        def request(self,*args):return {'kind':next(self.steps),'target_id':'search','reason':'test','absence_quote':'No records found','scope_quote':'Sales Orders'}
    page=SimpleNamespace(url='http://example.test',title=lambda:'Sales Orders',evaluate=lambda _:[],locator=lambda _:SimpleNamespace(all_text_contents=lambda:[]))
    monkeypatch.setattr(creation,'ControlCatalog',Catalog)
    monkeypatch.setattr(s,'event',lambda *a:None)
    monkeypatch.setattr(s,'notes_context',lambda *a:{})
    p={'name':'Trial','record_type':'Sales Order'};run={'id':'test','connection_id':'test'}
    if empty_evidence:assert not b.find_record(SimpleNamespace(page=page),p,Model(),run,threading.Event(),'test')['found']
    else:
        with pytest.raises(ValueError,match='zero-results'):b.find_record(SimpleNamespace(page=page),p,Model(),run,threading.Event(),'test')


@pytest.mark.parametrize('case',['authorized','not_delegated','different_field','placeholder','no_draft_action','already_chosen'])
def test_preparation_choices_require_delegation_observation_and_once_only_intent(tmp_path,monkeypatch,case):
    import threading
    from types import SimpleNamespace
    from pilot import creation
    from uiagent.journal import Journal
    s=Store(tmp_path);conn=s.add_connection({'platform':'website','base_url':'https://crm.test'})
    run=s.create_run({'connection_id':conn['id'],'kind':'browser_create','name':'Trial','record_type':'Sales Order','choose_existing':[] if case=='not_delegated' else ['Customer'],'fields':{}})
    j=Journal(tmp_path/'operations.sqlite');j.ensure(run['payload']);j.record(run['id'],'open_form_intent',phase='commit_intent');j.record(run['id'],'opening_form',phase='form_open')
    if case=='already_chosen':j.record(run['id'],'prepared_existing_record',preparation_choices={'Customer':{'after':'Other Customer'}})
    label='Source' if case=='different_field' else 'Customer'
    option='Create new customer' if case=='placeholder' else 'Existing Customer'
    field={'id':'customer','tag':'select','role':'','name':'customer','type':'select-one','label':label,'context':label,'text':'','value':'','selected_text':'Choose','options':[option],'disabled':False}
    save={'id':'save','tag':'button','label':'','text':'Create draft' if case!='no_draft_action' else 'Confirm order'}
    actions=[]
    class Catalog:
        def __init__(self,ui):self.targets={'customer':(None,field)}
        def observe(self,tag):return {'controls':[field,save],'aria':'Create draft'}
        def act(self,a):
            assert j.load(run['id'])['phase']=='commit_intent'
            actions.append(a)
    class Model:
        def __init__(self):self.n=0
        def request(self,*args):
            self.n+=1
            return {'kind':'choose_existing' if self.n==1 else 'blocked','delegated_field':'Customer','target_id':'customer','value':option,'reason':'stop after preparation'}
    monkeypatch.setattr(creation,'ControlCatalog',Catalog)
    with pytest.raises(ValueError):WebsiteBrowser(s).creation_form(SimpleNamespace(page=None),run['payload'],Model(),run,threading.Event(),j)
    if case=='authorized':
        assert len(actions)==1 and j.load(run['id'])['phase']=='form_open'
        assert j.load(run['id'])['preparation_choices']['Customer']['after']==option
    else:assert not actions


@pytest.mark.parametrize('kind,record_type,fields,expected',[
    ('browser_create','Sales Order',{'Status':'Draft'},{'Name':'Trial'}),
    ('browser_create','Sales Order',{'Status':'Confirmed'},{'Name':'Trial','Status':'Confirmed'}),
    ('browser_update','Sales Order',{'Status':'Draft'},{'Status':'Draft'}),
    ('browser_create','Contact',{'Status':'Draft'},{'Name':'Trial','Status':'Draft'}),
])
def test_draft_creation_status_is_an_outcome_not_a_missing_form_input(tmp_path,kind,record_type,fields,expected):
    s=Store(tmp_path);c=s.add_connection({'platform':'website','base_url':'http://example.test'})
    r=s.create_run({'connection_id':c['id'],'kind':kind,'name':'Trial','record_type':record_type,'fields':fields})
    assert r['payload']['desired']==expected
