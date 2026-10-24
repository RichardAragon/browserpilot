"""Browser-only observer and actuator. No direct CRM APIs used by the agent.

An actual web app may issue its OWN AJAX/XHR calls while operating its UI.
Those are logged by this adapter but aren't agent-authored API calls.
"""
from __future__ import annotations
from dataclasses import dataclass, asdict
from urllib.parse import urlparse
from pathlib import Path
import re
import time
import shutil
from playwright.sync_api import sync_playwright, Error as PlaywrightError

WRITE_INDICATORS = re.compile(r'\b(save|submit|add|delete|approve|create|update|send|close|convert|confirm|publish|remove)\b',re.I)

def redact_url_secrets(text):
    return re.sub(r'([?&](?:token|csrf|password|access_token|auth|username)=)[^&\s"\']+',r'\1[redacted]',text,flags=re.I)

@dataclass
class Action:
    kind: str                   # click, fill, select, ocr_click, stop
    role: str = ''               # button, link, etc.
    name: str = ''               # visible accessible name / screenshot text
    value: str = ''              # form value (redacted from logs)
    reason: str = ''
    by: str = 'role'             # role, placeholder, field (observed visible DOM name)
    scope_role: str = ''
    scope_name: str = ''

@dataclass
class Observation:
    url: str
    aria: str
    screenshot: bytes

class SecurityViolation(RuntimeError):
    pass

class BrowserUI:
    def __init__(self,base_url:str='about:blank',headless:bool=True,allow_writes:bool=False,viewport:tuple[int,int]=(1180,800),artifacts:str='artifacts',capture:bool=True,html:str|None=None):
        p=urlparse(base_url)
        if base_url!='about:blank' and (p.scheme not in ('http','https') or not p.netloc):
            raise ValueError('Base URL must be about:blank or an absolute http(s) URL')
        self.base=base_url.rstrip('/') if base_url!='about:blank' else 'about:blank'
        self.html=html
        self.origin=(p.scheme,p.netloc)
        self.headless=headless
        self.allow_writes=allow_writes
        self.viewport=viewport
        self.artifacts=Path(artifacts)
        self.capture=capture
        self.network_log=[]
        self._pending_ui_requests=set()
        self.action_log=[]
        self._p=self._browser=self._context=self.page=None

    def __enter__(self):
        self.artifacts.mkdir(parents=True,exist_ok=True)
        self._p=sync_playwright().start()
        try:
            chrome_path=next((shutil.which(k) for k in ('chromium','chromium-browser','google-chrome') if shutil.which(k)),None)
            launch={'headless':self.headless,'args':['--no-sandbox']}
            if chrome_path:launch['executable_path']=chrome_path
            self._browser=self._p.chromium.launch(**launch)
            self._context=self._browser.new_context(viewport={'width':self.viewport[0],'height':self.viewport[1]},device_scale_factor=1,accept_downloads=False)
            self._context.set_default_timeout(4000)
            self._context.route('**/*',self._guard_route)
            self.page=self._context.new_page()
            self.page.on('request',self._on_request)
            self.page.on('requestfinished',self._on_request_done)
            self.page.on('requestfailed',self._on_request_done)
            self.page.goto(self.base,wait_until='domcontentloaded',timeout=10000)
            if self.html is not None:
                if self.base!='about:blank':raise ValueError('Embedded HTML requires about:blank')
                self.page.set_content(self.html,wait_until='domcontentloaded')
            self._check_origin()
            return self
        except BaseException:
            if self._browser:self._browser.close()
            self._p.stop()
            raise

    def _guard_route(self,route):
        u=urlparse(route.request.url)
        if u.scheme in ('data','blob','about') or (u.scheme,u.netloc)==self.origin:
            route.continue_()
        else:
            route.abort('blockedbyclient')

    def _on_request(self,request):
        u=urlparse(request.url)
        if request.is_navigation_request() and request.frame==self.page.main_frame:
            # Requests from a discarded document may never complete on this Page.
            # They are not a readiness barrier for the replacement document.
            self._pending_ui_requests.clear()
        if request.resource_type in ('xhr','fetch'):self._pending_ui_requests.add(request)
        self.network_log.append({'resource_type':request.resource_type,'method':request.method,'path':u.path,'host':u.netloc})

    def _on_request_done(self,request):
        self._pending_ui_requests.discard(request)

    def wait_for_ui_ready(self,timeout=10,ignore_paths=()):
        # Observe only the browser's request lifecycle, never response bodies.
        # Unsettled UI is an error, not evidence that a record is absent.
        deadline=time.monotonic()+timeout;quiet=None
        while time.monotonic()<deadline:
            self.page.wait_for_timeout(50)
            pending=[r for r in self._pending_ui_requests if urlparse(r.url).path not in ignore_paths]
            if pending:quiet=None
            elif quiet is None:quiet=time.monotonic()
            elif time.monotonic()-quiet>=.25:return
        pending=sorted({urlparse(r.url).path for r in self._pending_ui_requests if urlparse(r.url).path not in ignore_paths})
        raise TimeoutError('UI requests did not settle; refusing to infer missing data: '+repr(pending))

    def _check_origin(self):
        u=urlparse(self.page.url)
        if self.base=='about:blank':
            if self.page.url!='about:blank':raise SecurityViolation('Navigated away from in-memory test document')
            return
        if (u.scheme,u.netloc)!=self.origin:
            raise SecurityViolation(f'Navigated outside permitted origin: {u.scheme}://{u.netloc}')

    def observe(self,shot_name:str|None=None)->Observation:
        self._check_origin()
        aria=redact_url_secrets(self.page.locator('body').aria_snapshot(timeout=4000))
        screenshot=self.page.screenshot(full_page=False,animations='disabled')
        if self.capture and shot_name:
            (self.artifacts / f'{shot_name}.png').write_bytes(screenshot)
            (self.artifacts / f'{shot_name}.aria.txt').write_text(aria,encoding='utf-8')
        return Observation(redact_url_secrets(self.page.url),aria,screenshot)

    def act(self,act:Action,ocr=None):
        self._check_origin()
        if act.kind not in ('click','hover','fill','select','check','ocr_click','stop','abort'):
            raise SecurityViolation(f'Action kind not allowed: {act.kind}')
        if act.kind in ('stop','abort'): return
        if act.by not in ('role','placeholder','field'):
            raise SecurityViolation('Unsupported locator strategy')
        # Intent guard isn't a full authorization model; sandbox experiments allow writes.
        if not self.allow_writes and (act.kind in ('fill','select','check','ocr_click') or (act.kind=='click' and WRITE_INDICATORS.search(act.name))):
            raise SecurityViolation('Mutation-type UI action blocked. Explicit allow_writes required.')
        if act.kind in ('click','hover'):
            if not act.role or not act.name:
                raise ValueError('Click must include explicit role and name')
            scope=self.page
            if act.scope_role:
                scope=self.page.get_by_role(act.scope_role,name=act.scope_name,exact=True)
                if scope.count()!=1: raise ValueError('Scope requires one exact match')
            target=scope.get_by_role(act.role,name=act.name,exact=True)
            if target.count()==1 and not target.is_visible():
                child=target.locator(':scope > *:visible')
                if child.count()==1: target=child
            if target.count()!=1 or not target.is_visible():
                raise ValueError(f'Click requires one visible match: {act.role}/{act.name!r}, found {target.count()}')
            if act.kind=='hover': target.hover()
            else: target.click()
        elif act.kind=='fill':
            if not act.name: raise ValueError('Fill requires accessible label')
            target=self._field(act, 'textbox')
            if target.count()!=1 or not target.is_visible():
                raise ValueError(f'Fill requires one visible textbox: {act.name}')
            target.fill(act.value)
        elif act.kind=='select':
            target=self._field(act, 'combobox')
            if target.count()!=1 or not target.is_visible():
                raise ValueError(f'Select requires one visible combobox: {act.name}')
            target.select_option(label=act.value)
        elif act.kind=='check':
            if act.value not in ('true','false'): raise ValueError('Check requires true or false')
            target=self.page.get_by_role('checkbox',name=act.name,exact=True)
            if target.count()!=1 or not target.is_visible(): raise ValueError('Check requires one visible checkbox')
            target.set_checked(act.value=='true')
        elif act.kind=='ocr_click':
            if ocr is None:raise ValueError('OCR engine required')
            shot=self.page.screenshot(full_page=False,animations='disabled')
            detection=ocr.find(shot,act.name)
            if detection is None:raise ValueError(f'OCR could not ground clickable text {act.name!r}')
            x,y=detection.center
            w,h=self.viewport
            if not (0<=x<w and 0<=y<h):raise ValueError('OCR target outside viewport')
            self.page.mouse.click(x,y)
        self.action_log.append({'kind':act.kind,'role':act.role,'name':act.name,'by':act.by,'scope_role':act.scope_role,'scope_name':act.scope_name,'value':'[redacted]' if act.value else '', 'reason':act.reason})
        self.page.wait_for_load_state('domcontentloaded',timeout=5000)
        time.sleep(.07)
        self._check_origin()

    def _field(self, act, role):
        if act.by == 'placeholder':
            return self.page.get_by_placeholder(act.name, exact=True)
        if act.by == 'field':
            # Only exact field names from observed DOM; never arbitrary selectors.
            if not re.fullmatch(r'[A-Za-z0-9_\[\]-]+', act.name):
                raise ValueError('Invalid field name')
            return self.page.locator(f'input[name="{act.name}"]:visible,textarea[name="{act.name}"]:visible,select[name="{act.name}"]:visible')
        return self.page.get_by_role(role,name=act.name,exact=True)

    def __exit__(self,*exc):
        try:
            if self._browser:self._browser.close()
        finally:
            if self._p:self._p.stop()
