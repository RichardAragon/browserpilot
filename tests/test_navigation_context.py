import pytest
from pilot.setup import navigation

@pytest.mark.parametrize('label',['Remove','Clear','Remove filter','Clear all filters','Reset search'])
def test_observed_search_region_controls_can_change_filters(label):
    assert navigation({'label':label,'search_scope':True},'https://crm.test')=='search_filter'

@pytest.mark.parametrize('control',[
    {'label':'Remove','search_scope':False},
    {'label':'Remove record','search_scope':True},
    {'label':'Remove','search_scope':True,'href':'/delete/42'},
    {'label':'Remove','search_scope':True,'href':'https://other.test'},
    {'label':'Save','search_scope':True},
    {'label':'Confirm order','search_scope':True},
])
def test_search_context_does_not_authorize_unrelated_changes(control):
    with pytest.raises(ValueError):navigation(control,'https://crm.test')


def test_record_type_can_label_its_own_blank_identity_field(tmp_path):
    from test_creation import create_fixture
    from test_general import Page
    from pilot.website import WebsiteBrowser
    from pilot.store import Store
    o,a,p=create_fixture();p['record_type']='Opportunity'
    o['controls'][0].update(name='field_73',label='Opportunity',dom_id='input_8')
    result=WebsiteBrowser(Store(tmp_path)).ready(Page(),o,a,p,creating=True)
    assert result['fields']['Name']['label']=='Opportunity'
    p['record_type']='Contact'
    with pytest.raises(ValueError):WebsiteBrowser(Store(tmp_path)).ready(Page(),o,a,p,creating=True)

@pytest.mark.parametrize('case',['live','closed','moved','unapproved','submitted','created','unknown_failure','no_checkpoint'])
def test_partial_input_resume_requires_original_live_form_and_no_submission(case):
    from types import SimpleNamespace
    from pilot.creation import resumable_input
    saved={'phase':'input_paused','save_unattempted':True};result={};events=[];approved=True
    live=SimpleNamespace(url='https://crm.test/new',is_closed=lambda:case=='closed')
    prepared={'url':'https://crm.test/new'}
    if case=='moved':live.url='https://crm.test/other'
    if case=='unapproved':approved=False
    if case=='submitted':events=[{'event':'submit_intent'}]
    if case=='created':events=[{'event':'create_clicked'}]
    if case=='unknown_failure':saved.update(phase='uncertain',save_unattempted=False)
    if case=='no_checkpoint':saved['save_unattempted']=False
    assert resumable_input(saved,live,prepared,approved,events)==(case=='live')


def test_submission_uses_reviewed_label_when_button_attributes_are_identical():
    from pilot.website import resolve_action,fingerprint
    a={'tag':'button','type':'submit','name':'','label':'','text':'Add'}
    b={**a,'text':'Edit'}
    o={'controls':[a,b]}
    assert resolve_action(o,fingerprint(a),'Add') is a
    o['controls'].append(dict(a))
    with pytest.raises(ValueError):resolve_action(o,fingerprint(a),'Add')
