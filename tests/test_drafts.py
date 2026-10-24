from types import SimpleNamespace
import threading
import pytest
from pilot.store import Store
from pilot.website import WebsiteBrowser
from uiagent.journal import Journal


def draft_fixture(tmp_path):
    store=Store(tmp_path);connection=store.add_connection({'platform':'website','base_url':'https://crm.test'})
    run=store.create_run({'connection_id':connection['id'],'kind':'browser_create','name':'Trial','record_type':'Sales Order','choose_existing':['Customer'],'fields':{}})
    journal=Journal(tmp_path/'operations.sqlite');journal.ensure(run['payload']);journal.record(run['id'],'open_form_intent',phase='commit_intent');journal.record(run['id'],'prepared_existing_record',phase='form_open',preparation_choices={'Customer':{'after':'Existing Customer (Customer)'}})
    obs={'aria':'Customer Existing Customer\nCreate draft','controls':[{'id':'save','tag':'button','type':'submit','name':'create','label':'','text':'Create draft','disabled':False}]}
    action={'save_id':'save','relationships':[{'field':'Customer','value':'Existing Customer','quote':'Customer Existing Customer'}]}
    ui=SimpleNamespace(page=SimpleNamespace(url='https://crm.test/new'))
    return store,run,journal,obs,action,ui


def test_draft_first_review_exposes_deferred_reference_and_real_customer(tmp_path):
    s,r,j,o,a,ui=draft_fixture(tmp_path)
    prepared=WebsiteBrowser(s).ready_shell(ui,o,a,r['payload'],j)
    assert prepared['stage']=='draft_shell'
    assert prepared['fields']['Name']['deferred']
    assert prepared['fields']['Customer']['readonly'] and prepared['fields']['Customer']['after']=='Existing Customer'
    assert j.load(r['id'])['phase']=='form_open'


@pytest.mark.parametrize('case',['confirm','no_customer','wrong_customer','unobserved_quote','additional_fields','not_draft'])
def test_draft_first_rejects_unsupported_or_ungrounded_shell(tmp_path,case):
    s,r,j,o,a,ui=draft_fixture(tmp_path)
    if case=='confirm':o['controls'][0]['text']='Create and confirm'
    elif case=='no_customer':a['relationships']=[]
    elif case=='wrong_customer':a['relationships'][0]['value']='Different Customer'
    elif case=='unobserved_quote':a['relationships'][0]['quote']='Customer Missing'
    elif case=='additional_fields':r['payload']['desired']['Amount']='100'
    else:r['payload']['draft_only']=False
    with pytest.raises(ValueError):WebsiteBrowser(s).ready_shell(ui,o,a,r['payload'],j)


def test_created_draft_pointer_survives_later_field_discovery_failure(tmp_path,monkeypatch):
    from pilot import drafts
    s,r,j,o,a,ui=draft_fixture(tmp_path);browser=WebsiteBrowser(s)
    shell=browser.ready_shell(ui,o,a,r['payload'],j)
    ui.page.url='https://crm.test/document/42'
    j.record(r['id'],'create_clicked',phase='commit_intent')
    class Catalog:
        def __init__(self,ui):pass
        def observe(self,tag):return {'aria':'Sales Order TMP0042\nDraft\nCustomer Existing Customer','controls':[]}
    class Model:
        count=0
        def request(self,*args):
            self.count+=1
            if self.count==1:return {'identity':'TMP0042','identity_quote':'Sales Order TMP0042','draft_quote':'Draft'}
            return {'kind':'blocked','reason':'Reference edit temporarily unavailable'}
    monkeypatch.setattr(drafts,'ControlCatalog',Catalog);model=Model()
    with pytest.raises(ValueError,match='temporarily'):browser.prepare_draft_completion(ui,r['payload'],shell,model,r,threading.Event(),j)
    state=j.load(r['id'])
    assert state['draft_pointer']['identity']=='TMP0042' and state['draft_pointer']['url']==ui.page.url
    assert state['phase']=='draft_open' and state['submission_pending']
    with pytest.raises(ValueError,match='temporarily'):browser.prepare_draft_completion(ui,r['payload'],shell,model,r,threading.Event(),j)
    assert len([e for e in j.events(r['id']) if e['event']=='draft_created_pointer'])==1


@pytest.mark.parametrize('matches',[True,False])
def test_uncertain_draft_reopens_exact_pointer_without_search_or_write_replay(tmp_path,monkeypatch,matches):
    from pilot import creation
    from uiagent.journal import digest
    s,r,j,o,a,ui=draft_fixture(tmp_path)
    pointer={'url':'https://crm.test/document/42','identity':'TMP0042'}
    prepared={'stage':'complete_draft','draft_pointer':pointer,'fields':{'Name':{'after':'Trial'}}}
    s.update_run(r['id'],'uncertain',result={'prepared':prepared,'approval':digest(prepared)})
    j.record(r['id'],'create_clicked',phase='commit_intent');j.record(r['id'],'draft_created_pointer',draft_pointer=pointer,phase='uncertain')
    visits=[]
    class Page:
        url='https://crm.test'
        def goto(self,url,**kwargs):visits.append(url);self.url=url
        def is_closed(self):return False
        def close(self):pass
        def screenshot(self,**kwargs):pass
    class Context:
        def new_page(self):return Page()
    class Model:
        def __init__(self,*a):self.calls=[]
    monkeypatch.setattr(creation,'StructuredModel',Model)
    b=WebsiteBrowser(s);monkeypatch.setattr(b,'session',lambda _:(Context(),Page()))
    monkeypatch.setattr(b,'ui',lambda page,*a:SimpleNamespace(page=page))
    monkeypatch.setattr(b,'find_record',lambda *a:pytest.fail('A known draft was searched using an unrelated reference filter'))
    monkeypatch.setattr(b,'creation_form',lambda *a:pytest.fail('A known draft was created again'))
    def verify(*args):
        if not matches:raise ValueError('Saved reference differs')
        return {'Name':'Trial','Status':'Draft'}
    monkeypatch.setattr(b,'verify_document',verify)
    b.create(s.run(r['id']),threading.Event())
    assert visits==[pointer['url']]
    assert s.run(r['id'])['status']==('confirmed' if matches else 'uncertain')
    assert len([e for e in j.events(r['id']) if e['event']=='create_clicked'])==1
