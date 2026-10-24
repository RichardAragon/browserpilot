"""Browser UI adapter for narrowly scoped, explicitly authorized self-messages.

No HTTP client, Graph API, hidden application state, cookie export or network
response parsing. A visible account/recipient proof precedes every send.
"""
from pathlib import Path
import os
import re
import time
from urllib.parse import urlsplit
from .message_policy import Scope,Contract,SendGrant,Receipt,Observation,Prior,decide
from .runtime import configure_browser

def conversation_url(platform,url):
    p=urlsplit(url)
    host='www.instagram.com' if platform=='instagram' else 'www.messenger.com'
    pattern=r'/direct/t/\d+/?' if platform=='instagram' else r'/(?:e2ee/)?t/\d+/?'
    if p.scheme!='https' or p.netloc!=host or not re.fullmatch(pattern,p.path):raise ValueError('Open your own conversation first')
    return f'https://{host}{p.path}'.rstrip('/')

class SocialBrowser:
    def __init__(self,store):self.store=store;self.pw=None;self.sessions={}
    def session(self,ident):
        configure_browser()
        if self.pw is None:
            from playwright.sync_api import sync_playwright
            self.pw=sync_playwright().start()
        if ident in self.sessions:
            context,page=self.sessions[ident]
            if not page.is_closed():return context,page
            try:context.close()
            except Exception:pass
        c=self.store.connection(ident)
        options={}
        edge=Path(os.environ.get('PROGRAMFILES(X86)','C:/Program Files (x86)'))/'Microsoft/Edge/Application/msedge.exe'
        if edge.exists():options['channel']='msedge'
        context=self.pw.chromium.launch_persistent_context(str(self.store.root/'profiles'/ident),headless=False,viewport={'width':1180,'height':850},accept_downloads=False,**options)
        context.set_default_timeout(7000)
        page=context.pages[0] if context.pages else context.new_page()
        page.on('dialog',lambda d:d.dismiss())
        self.sessions[ident]=(context,page)
        page.goto(c['config']['base_url'],wait_until='domcontentloaded')
        return context,page
    def open(self,ident):
        _,page=self.session(ident);page.bring_to_front()
        platform=self.store.connection(ident)['platform']
        detail='Sign in in the browser, then click Verify self-chat here.'
        if platform=='messenger':detail='Sign in, restore chats if prompted, and open a conversation with yourself. Then click Verify self-chat.'
        self.store.connection_status(ident,'login_open',detail)
    def editor(self,page):return page.locator('[contenteditable="true"][role="textbox"]:visible')
    def draft(self,page):
        editor=self.editor(page)
        if editor.count()>1:raise ValueError('Multiple message composers')
        return editor.inner_text().replace('\r\n','\n').strip('\n') if editor.count() else ''
    def own_account(self,page,c):
        h=c['config']['handle']
        if c['platform']=='instagram':
            own=page.get_by_role('link',name=f"{h}'s profile picture",exact=True)
            own.wait_for(state='visible',timeout=15000)
            if own.count()!=1 or not own.is_visible():raise ValueError('Signed-in account is not verified')
            if urlsplit(own.get_attribute('href')).path.rstrip('/')!='/'+h:raise ValueError('Account profile does not match')
        else:
            own=page.get_by_role('button',name=re.compile(r'@'+re.escape(h)+r' Settings, help and more$'))
            own.wait_for(state='visible',timeout=15000)
            if own.count()!=1 or not own.is_visible():raise ValueError('Signed-in account is not verified')
    def messenger_identity(self,page,c):
        self.own_account(page,c)
        page.get_by_role('button',name='Conversation information',exact=True).click()
        h=c['config']['handle']
        page.get_by_text(h,exact=True).wait_for(state='visible')
        # Direct one-person profile link plus exact username in conversation details.
        links=page.get_by_role('heading',level=3).get_by_role('link')
        profiles=[links.nth(i).get_attribute('href') for i in range(links.count()) if re.fullmatch(r'https://(?:www\.)?facebook\.com/(?:\d+|[A-Za-z0-9.]+)/?',links.nth(i).get_attribute('href') or '')]
        if len(profiles)!=1:raise ValueError('A unique participant profile was not exposed')
        if c['config'].get('profile') and c['config']['profile']!=profiles[0]:raise ValueError('Participant profile changed')
        page.get_by_role('button',name='Back',exact=True).click()
        self.editor(page).wait_for(state='visible')
        return profiles[0]
    def instagram_self(self,page,c):
        self.own_account(page,c)
        if self.draft(page):raise ValueError('An existing draft must be resolved before opening a new chat')
        page.get_by_role('button',name='New message',exact=True).click()
        page.get_by_role('textbox',name='To:',exact=True).fill(c['config']['handle'])
        candidates=page.get_by_role('checkbox',name='Radio selection',exact=True)
        deadline=time.monotonic()+10
        while time.monotonic()<deadline:
            lists=page.get_by_role('list').filter(has=candidates)
            if candidates.count()==1 and lists.count()==1:
                tokens=lists.inner_text().strip().split()
                if tokens and tokens[-1].lower()==c['config']['handle']:break
            page.wait_for_timeout(150)
        else:raise ValueError('A unique exact self-recipient was not found')
        if candidates.is_checked():raise ValueError('Unexpected preselected recipient')
        candidates.check()
        if candidates.count()!=1 or not candidates.is_checked():raise ValueError('Recipient selection changed')
        page.get_by_role('button',name='Chat',exact=True).click()
        self.editor(page).wait_for(state='visible')
        self.own_account(page,c)
        return conversation_url('instagram',page.url)
    def verify(self,ident):
        c=self.store.connection(ident);_,page=self.session(ident)
        if self.draft(page):raise ValueError('Clear or send your existing draft manually before verification')
        if c['platform']=='instagram':
            if not urlsplit(page.url).path.startswith('/direct/'):
                page.goto(c['config']['base_url']+'/direct/inbox/',wait_until='domcontentloaded')
            url=self.instagram_self(page,c);patch={'conversation':url}
        else:
            url=conversation_url('messenger',page.url);profile=self.messenger_identity(page,c);patch={'conversation':url,'profile':profile}
        if c['config'].get('conversation') and c['config']['conversation']!=url:raise ValueError('This connection is bound to a different conversation; add a new connection')
        self.store.connection_status(ident,'ready','Own account and self-recipient verified through the browser',patch)
    def receipts(self,page,c,payload,fresh):
        marker='['+payload['marker']+']'
        if c['platform']=='messenger':
            labels=page.locator('[role="button"][aria-label]').evaluate_all('(els, marker)=>els.filter(e=>e.getBoundingClientRect().width && e.getAttribute("aria-label").includes(marker)).map(e=>e.getAttribute("aria-label"))',marker)
            return tuple(Receipt(v[v.index(marker):],True,'sent' if fresh else 'local') for v in labels if re.match(r'Enter, Message sent .+ by You:',v))
        # Read rendered text, including alt text of visible emoji images.
        texts=page.locator('div[dir="auto"]').evaluate_all('''(els,marker)=>{
          const read=n=>n.nodeType===3?n.textContent:n.nodeName==='IMG'?(n.getAttribute('alt')||''):n.nodeName==='BR'?'\n':Array.from(n.childNodes).map(read).join('');
          return els.filter(e=>e.getBoundingClientRect().width && !e.closest('[contenteditable="true"]') && Array.from(e.childNodes).some(n=>n.nodeType===3 && n.textContent.startsWith(marker))).map(read);
        }'''.replace("?'\n':","?'\\n':"),marker)
        return tuple(Receipt(v,True,'persisted' if fresh else 'local') for v in texts)
    def finish(self,run,status,reason,result=None):
        self.store.update_run(run['id'],status,reason,result);self.store.event(run['id'],status,reason)
    def run(self,run,stop):
        ident=run['id'];c=self.store.connection(run['connection_id']);p=run['payload'];context,page=self.session(c['id'])
        scope=Scope(c['platform'],p['sender'],p['conversation'],(p['sender'],));contract=Contract(ident,scope,p['body'],p['marker']);grant=SendGrant(scope,True,True)
        previous=run['result'];phase=previous.get('phase','new')
        if p['sender']!=c['config']['handle'] or p['conversation']!=c['config']['conversation']:return self.finish(run,'blocked','Connection no longer matches this immutable operation')
        # Never overwrite a human draft in the owned browser, including another chat.
        current_draft=self.draft(page)
        if current_draft and current_draft!=contract.text:return self.finish(run,'blocked','Existing draft differs. Resolve it in the browser, then check outcome.',previous)
        if current_draft:
            if conversation_url(c['platform'],page.url)!=p['conversation']:return self.finish(run,'blocked','Draft is in a different conversation',previous)
            # Inspect a fresh tab without discarding this known draft.
            fresh=context.new_page();fresh.goto(p['conversation'],wait_until='domcontentloaded')
        else:fresh=page;fresh.goto(p['conversation'],wait_until='domcontentloaded')
        fresh.on('dialog',lambda d:d.dismiss())
        self.own_account(fresh,c)
        if c['platform']=='instagram':
            if self.instagram_self(fresh,c)!=p['conversation']:raise ValueError('Self conversation changed')
        else:
            if not self.editor(fresh).count():
                # Deep links can open the inbox; only an observed matching thread link is acceptable.
                links=fresh.locator('a[href]');match=[links.nth(i) for i in range(links.count()) if urlsplit(links.nth(i).get_attribute('href') or '').path.rstrip('/')==urlsplit(p['conversation']).path]
                if len(match)!=1:raise ValueError('Open your verified self-chat in the browser, then check again')
                match[0].click()
            self.messenger_identity(fresh,c)
        self.editor(fresh).wait_for(state='visible')
        receipts=self.receipts(fresh,c,p,True)
        peers=self.store.pending_messages(c['id'],ident)
        obs=Observation(scope,draft=current_draft,receipts=receipts,freshly_reopened=True)
        decision=decide(contract,obs,Prior(phase,peers),grant)
        if decision.status not in ('ready_to_compose','ready_to_send'):
            if fresh!=page:fresh.close()
            if decision.status in ('confirmed','visible'):previous={**previous,'phase':decision.status,'send_intent':False}
            return self.finish(run,decision.status,decision.reason,previous)
        if stop.is_set():return self.finish(run,'cancelled','Stopped before sending',previous)
        if fresh!=page:
            fresh.close()
            self.own_account(page,c)
            if c['platform']=='messenger':self.messenger_identity(page,c)
            # Instagram original draft is not selected through a fresh recipient picker;
            # refuse rather than rely on a stale conversation binding.
            else:return self.finish(run,'blocked','Existing Instagram draft preserved. Clear it manually, then check outcome.',previous)
        else:page=fresh
        if conversation_url(c['platform'],page.url)!=p['conversation']:raise ValueError('Conversation changed')
        editor=self.editor(page)
        if not self.draft(page):editor.fill(contract.text)
        if self.draft(page)!=contract.text:return self.finish(run,'blocked','Composer did not retain the exact message. Nothing was sent.',previous)
        self.own_account(page,c)
        if stop.is_set():return self.finish(run,'cancelled','Stopped with the exact draft preserved',previous)
        # Durable intent before the one send click. Never automatically repeat it.
        result={'phase':'send_intent','send_intent':True,'marker':p['marker']}
        self.store.update_run(ident,'running','Sending once, then reopening to verify',result)
        self.store.event(ident,'send_intent','Send intent saved before clicking Send')
        if self.draft(page)!=contract.text: return self.finish(run,'uncertain','Draft changed at the send boundary; intent retained',result)
        page.get_by_role('button',name='Send' if c['platform']=='instagram' else 'Press enter to send',exact=True).click()
        deadline=time.monotonic()+10
        while time.monotonic()<deadline and self.draft(page):page.wait_for_timeout(150)
        if self.draft(page):return self.finish(run,'uncertain','Send may have been submitted; the draft did not clear',result)
        page.reload(wait_until='domcontentloaded');self.editor(page).wait_for(state='visible')
        self.own_account(page,c)
        receipts=self.receipts(page,c,p,True)
        decision=decide(contract,Observation(scope,receipts=receipts,freshly_reopened=True),Prior('send_intent'),grant)
        result['phase']=decision.status if decision.status in ('confirmed','visible') else 'uncertain'
        result['send_intent']=result['phase']=='uncertain'
        out=self.store.root/'runs'/ident/str(run['attempt']);out.mkdir(parents=True,exist_ok=True)
        page.screenshot(path=str(out/'message-proof.png'))
        self.finish(run,decision.status,decision.reason,result)
    def close(self):
        for context,page in self.sessions.values():
            try:context.close()
            except Exception:pass
        if self.pw:self.pw.stop()
