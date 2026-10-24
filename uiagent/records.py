"""Small browser-only verification adapters; no CRM APIs or database reads.

Scope: active company contacts in Odoo; third parties across statuses in Dolibarr.
Identity is an exact, immutable name in these synthetic trials. Field coverage is
explicit; the adapter does not claim full-record or atomic concurrency protection.
"""
from contextlib import contextmanager
import json
from pathlib import Path
import re
from urllib.parse import urljoin,urlparse
from .browser import BrowserUI,Action
from .control_catalog import ControlCatalog

def normalize(key,value):
    value=value or ''
    return re.sub(r'[ ()-]','',value) if key=='phone' else value

def matches(state,desired):
    return all(normalize(k,state.get(k))==normalize(k,v) for k,v in desired.items())

@contextmanager
def fresh_session(parent,artifacts):
    ui=BrowserUI(parent.base,allow_writes=True,artifacts=str(artifacts))
    ui.artifacts.mkdir(parents=True,exist_ok=True)
    ui._browser=parent._browser
    ui._context=parent._browser.new_context(viewport={'width':1180,'height':800},accept_downloads=False)
    ui._context.set_default_timeout(5000);ui._context.route('**/*',ui._guard_route)
    ui.page=ui._context.new_page();ui.page.on('request',ui._on_request)
    ui.page.on('requestfinished',ui._on_request_done);ui.page.on('requestfailed',ui._on_request_done)
    try:
        ui.page.goto(ui.base,wait_until='domcontentloaded')
        yield ui
    finally:
        (Path(artifacts)/'browser-network.json').write_text(json.dumps(ui.network_log,indent=2),encoding='utf-8')
        ui._context.close()

class RecordAdapter:
    def __init__(self,platform,config=None):
        self.platform=platform;self.config=config or {}
        self.base=self.config.get('base_url','http://127.0.0.1:8088')
        if platform not in ('dolibarr','odoo'):raise ValueError(platform)
        self.atomic_compare_and_swap=False

    def login(self,ui):
        if self.platform=='dolibarr':
            ui.page.get_by_placeholder('Login',exact=True).fill(self.config['login'])
            ui.page.get_by_placeholder('Password',exact=True).fill(self.config['password'])
            ui.act(Action('click',role='button',name='Login'))
            if ui.page.get_by_placeholder('Password',exact=True).is_visible():raise RuntimeError('Sign-in failed; check this connection')
        else:
            ui.page.get_by_label('Email',exact=True).fill(self.config['login'])
            ui.page.locator('input[type="password"]').fill(self.config['password'])
            ui.act(Action('click',role='button',name='Log in'))
            ui.page.get_by_role('navigation').first.wait_for(state='visible',timeout=20000)

    def field_key(self,control):
        if control.get('tag') not in ('input','textarea'):return None
        if self.platform=='dolibarr':
            return {'name':'name','email':'email','phone':'phone','town':'city','address':'address'}.get(control['name'])
        return {'e.g. Lumber Inc':'name','Email':'email','Phone':'phone','City':'city','Street...':'address'}.get(control['label'])

    def state(self,observation):
        state={}
        for c in observation['controls']:
            key=self.field_key(c)
            if key:
                if key in state:raise ValueError('Ambiguous observed field: '+key)
                state[key]=normalize(key,c['value'])
        if set(state)!={'name','email','phone','city','address'}:raise ValueError('Incomplete visible record fields')
        checked={c['label']:c['checked'] for c in observation['controls'] if c['type'] in ('checkbox','radio')}
        if self.platform=='dolibarr':state['record_type']='prospect' if checked.get('Prospect') and not checked.get('Customer') else 'other'
        else:state['record_type']='company' if checked.get('Company') else 'person'
        return state

    def find(self,ui,name):
        if self.platform=='dolibarr':
            ui.page.goto(self.base+'/societe/list.php',wait_until='domcontentloaded')
            ui.page.locator('[name="search_nom"]').fill(name)
            ui.page.locator('[name="search_status"]').select_option(label='')
            ui.page.locator('[name="button_search_x"]').click()
            ui.page.wait_for_load_state('domcontentloaded')
            ui.wait_for_ui_ready()
            ControlCatalog(ui).observe('search-settled')
            links=ui.page.locator('a[href*="/societe/card.php?"]')
            hrefs=sorted(set(links.nth(i).get_attribute('href') for i in range(links.count()) if links.nth(i).inner_text().strip()==name))
            if not hrefs and 'No record found' not in ui.page.locator('body').inner_text():raise ValueError('No reliable empty-result evidence')
            if len(hrefs)!=1:return {'count':len(hrefs),'state':None,'url':None}
            url=urljoin(self.base,hrefs[0]);self.open_edit(ui,url,name)
        else:
            ui.page.goto(self.base+'/odoo/contacts',wait_until='domcontentloaded')
            ui.page.get_by_role('searchbox',name='Search...',exact=True).fill(name)
            ui.page.get_by_role('link',name='Search Name for: '+name,exact=True).click()
            # Wait for the applied filter and for the search menu to disappear.
            ui.page.get_by_role('link',name='Search Name for: '+name,exact=True).wait_for(state='hidden')
            # Observed Odoo messaging traffic can remain open independently of
            # contact results. Only its lifecycle is ignored, never record reads.
            ui.wait_for_ui_ready(ignore_paths=('/mail/data',))
            ControlCatalog(ui).observe('search-settled')
            cards=ui.page.get_by_role('article')
            next_page=ui.page.get_by_role('button',name='Next',exact=True)
            if next_page.count() and next_page.is_enabled():raise ValueError('Search is paginated; uniqueness has not been established')
            matches=[cards.nth(i) for i in range(cards.count()) if cards.nth(i).locator('main > div > span').first.inner_text().strip()==name]
            count=len(matches)
            if not count and 'Create a Contact in your address book' not in ui.page.locator('body').inner_text():
                # Non-exact substring matches are visible, but no exact identity.
                if cards.count()==0:raise ValueError('No reliable empty-result evidence')
            if count!=1:return {'count':count,'state':None,'url':None}
            matches[0].click();ui.page.get_by_text(name,exact=True).first.wait_for(state='visible')
            ui.page.get_by_role('textbox',name='Email',exact=True).wait_for(state='visible')
            url=ui.page.url
        observation=ControlCatalog(ui).observe('record-probe')
        state=self.state(observation)
        if state['name']!=name:raise ValueError('Record identity mismatch')
        return {'count':1,'state':state,'url':url,'observation':observation}

    def open_edit(self,ui,url,name):
        if urlparse(url).netloc!=urlparse(self.base).netloc:raise ValueError('Record URL outside origin')
        ui.page.goto(url,wait_until='domcontentloaded')
        if self.platform=='dolibarr':
            ui.page.get_by_role('link',name='Modify',exact=True).click()
            ui.page.locator('[name="town"]').wait_for(state='visible')
        else:
            ui.page.get_by_role('textbox',name='Email',exact=True).wait_for(state='visible')
            edit=ui.page.get_by_role('button',name='Edit',exact=True)
            if edit.is_visible():edit.click()
        observation=ControlCatalog(ui).observe('edit-open')
        if self.state(observation)['name']!=name:raise ValueError('Record identity mismatch')
        return observation

    def is_save(self,control):
        return control.get('name')=='save' if self.platform=='dolibarr' else control.get('label')=='Save manually'

    def apply_patch(self,ui,catalog,patch):
        obs=catalog.observe('patch-before')
        for key,value in patch.items():
            controls=[c for c in obs['controls'] if self.field_key(c)==key]
            if len(controls)!=1:raise ValueError('No unique observed field for '+key)
            catalog.act({'kind':'fill','target_id':controls[0]['id'],'value':value})
        return catalog.observe('patch-after')

    def save(self,ui,catalog):
        obs=catalog.observe('save-before');saves=[c for c in obs['controls'] if self.is_save(c)]
        if len(saves)!=1:raise ValueError('No unique observed save control')
        catalog.act({'kind':'click','target_id':saves[0]['id'],'value':''})
        if self.platform=='odoo':ui.page.get_by_role('button',name='Save manually',exact=True).wait_for(state='hidden',timeout=15000)
        else:ui.page.get_by_role('link',name='Modify',exact=True).wait_for(state='visible',timeout=15000)
        catalog.observe('save-after')

    def probe(self,parent,name,artifacts):
        with fresh_session(parent,artifacts) as other:
            self.login(other);result=self.find(other,name)
            other.observe('verified')
            # No current control IDs survive this session.
            result.pop('observation',None)
            return result
