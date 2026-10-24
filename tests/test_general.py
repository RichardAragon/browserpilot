from copy import deepcopy
import pytest
from pilot.store import Store
from pilot.website import WebsiteBrowser,resolve,fingerprint
from pilot.model import validate,object_schema,STRING
from uiagent.journal import Journal

class Page:
    url='http://example.test/record/7'
    def get_by_role(self,*args,**kwargs):return self
    def count(self):return 0

def fixture():
    fields=[{'id':'identity','tag':'input','type':'text','name':'account_name','label':'Account name','value':'Exact Company','disabled':False,'text':'','options':None},
            {'id':'city','tag':'input','type':'text','name':'billing_town','label':'Billing town','value':'Portland','disabled':False,'text':'','options':None},
            {'id':'save','tag':'button','type':'submit','name':'save','label':'','value':'','disabled':False,'text':'Save','options':None}]
    action={'identity_target_id':'identity','bindings':[{'field':'City','target_id':'city'}],'save_id':'save'}
    task={'name':'Exact Company','desired':{'City':'Austin'}}
    return {'controls':fields},action,task

def test_general_mapping_uses_observed_controls_and_preserves_real_labels(tmp_path):
    b=WebsiteBrowser(Store(tmp_path));o,a,p=fixture();r=b.ready(Page(),o,a,p)
    assert r['fields']['City']['label']=='Billing town'
    assert r['fields']['City']['before']=='Portland' and r['fields']['City']['after']=='Austin'

def test_deferred_save_can_be_reviewed_without_editing_to_expose_a_button(tmp_path):
    b=WebsiteBrowser(Store(tmp_path));o,a,p=fixture();o['controls'].pop();a['save_id']=''
    r=b.ready(Page(),o,a,p)
    assert r['save'] is None and r['fields']['City']['before']=='Portland'

@pytest.mark.parametrize('attack',['wrong_identity','invented_control','duplicate_field','password','no_save'])
def test_general_mapping_rejects_unsafe_model_output(tmp_path,attack):
    b=WebsiteBrowser(Store(tmp_path));o,a,p=fixture()
    if attack=='wrong_identity':o['controls'][0]['value']='Other Company'
    elif attack=='invented_control':a['bindings'][0]['target_id']='hidden-hallucination'
    elif attack=='duplicate_field':p['desired']['Phone']='123';a['bindings'].append({'field':'Phone','target_id':'city'})
    elif attack=='password':o['controls'][1]['type']='password'
    else:o['controls'][2]['text']='Delete'
    with pytest.raises(ValueError):b.ready(Page(),o,a,p)

def test_rebind_requires_unique_same_semantic_control():
    o,_,_=fixture();sig=fingerprint(o['controls'][1]);o['controls'][1]['label']='Different field'
    with pytest.raises(ValueError):resolve(o,sig)
    o,_,_=fixture();o['controls'].append(deepcopy(o['controls'][1]))
    with pytest.raises(ValueError):resolve(o,sig)

def test_changing_adapter_does_not_bypass_pending_identity(tmp_path):
    j=Journal(tmp_path/'journal.sqlite')
    first={'operation_id':'first','platform':'dolibarr','name':'Exact Company','origin':'http://example.test'}
    j.ensure(first);j.record('first','commit_intent',phase='commit_intent')
    assert j.pending_for_identity({**first,'operation_id':'second','platform':'website'})==['first']

def test_model_keys_encrypted_and_never_public(tmp_path):
    s=Store(tmp_path);s.save_model({'provider':'openai','model':'test-model','api_key':'test-key-not-real'})
    assert 'api_key' not in s.model_config() and 'secret' not in s.model_config()
    assert s.model_config(True)['api_key']=='test-key-not-real'
    with s.db() as db:assert 'test-key-not-real' not in db.execute("SELECT value FROM settings WHERE key='model'").fetchone()[0]

def test_arbitrary_model_properties_and_tool_actions_rejected():
    schema=object_schema({'message':STRING})
    with pytest.raises(ValueError):validate({'message':'hi','tool':'exec'},schema)

def test_generic_website_task_does_not_require_a_platform_adapter(tmp_path):
    s=Store(tmp_path);c=s.add_connection({'platform':'website','base_url':'https://example.my.salesforce.com/lightning/page/home'})
    r=s.create_run({'connection_id':c['id'],'kind':'browser_update','name':'Exact Company','fields':{'Billing city':'Austin'}})
    assert r['payload']['desired']=={'Billing city':'Austin'}
    with pytest.raises(ValueError):s.create_run({'connection_id':c['id'],'kind':'create','name':'Exact Company','fields':{'city':'Austin'}})
