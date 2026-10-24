"""Rendered-control observations and observation-scoped actuator handles."""
import re
from urllib.parse import urlparse
from .browser import SecurityViolation,redact_url_secrets

FORBIDDEN=re.compile(r'\b(delete|merge|send email|send message|export|password|setup|admin tools|users & groups)\b',re.I)

class ControlCatalog:
    def __init__(self,ui,writes=True):
        self.ui,self.writes=ui,writes
        self.targets={}
        self.generation=0

    def observe(self,shot_name):
        self.ui.page.wait_for_load_state('domcontentloaded',timeout=30000)
        # A search/navigation may begin just after the previous DOM-ready check.
        # Allow its new document to arrive; the shorter action timeout is for
        # individual controls, not a CRM's full page response.
        self.ui.page.locator('body').wait_for(state='attached',timeout=30000)
        # SPA navigation does not produce a load event. Wait for a short DOM quiet
        # window, bounded even when a chat/polling widget keeps updating.
        self.ui.page.evaluate('''()=>new Promise(resolve=>{
            if(!document.body){resolve();return;}
            let timer, finished=false;
            const done=()=>{if(finished)return;finished=true;clearTimeout(timer);observer.disconnect();resolve();};
            const observer=new MutationObserver(()=>{clearTimeout(timer);timer=setTimeout(done,200);});
            observer.observe(document.body,{subtree:true,childList:true,attributes:true,characterData:true});
            timer=setTimeout(done,200);setTimeout(done,1800);
        })''')
        obs=self.ui.observe(shot_name)
        self.observed_url=obs.url
        self.generation+=1; self.targets={}; controls=[]
        elements=self.ui.page.locator('a,input,textarea,select,button,[role="button"],[role="combobox"],[role="tab"],[role="menuitem"],[role="option"],[role="radio"],[role="checkbox"],[contenteditable="true"],[tabindex]:not([tabindex="-1"])')
        # Pin actual nodes. An nth() locator would silently retarget if the DOM
        # reordered while the planner was thinking.
        handles=elements.element_handles()
        # Read metadata together, keeping the original node handles pinned. This
        # avoids thousands of browser round trips on populated CRM boards.
        infos=self.ui.page.evaluate('''elements=>elements.map(e=>{
                const shown=x=>!!(x.getBoundingClientRect().width&&x.getBoundingClientRect().height)&&getComputedStyle(x).visibility!=='hidden';
                if(!shown(e)&&Array.from(e.children).filter(shown).length!==1)return null;
                const td=e.closest('td'), row=e.closest('tr');
                const label=(e.getAttribute('aria-label')||Array.from(e.labels||[]).map(x=>x.innerText).join(' ')||e.getAttribute('placeholder')||(td&&td.previousElementSibling?td.previousElementSibling.innerText:'')||'').trim();
                const role=e.getAttribute('role')||'';
                const visible=x=>!!(x.getBoundingClientRect().width&&x.getBoundingClientRect().height);
                let owned=(e.getAttribute('aria-controls')||e.getAttribute('aria-owns')||'').split(' ').map(id=>document.getElementById(id)).filter(Boolean);
                // Some accessible comboboxes use an adjacent menu without
                // aria-controls. Accept only a unique local menu and combobox.
                if(role==='combobox'&&e.getAttribute('aria-expanded')==='true'&&!owned.length){
                  for(let root=e.parentElement,depth=0;root&&depth<3;root=root.parentElement,depth++){
                    const combos=Array.from(root.querySelectorAll('[role="combobox"]')).filter(visible);
                    const menus=Array.from(root.querySelectorAll('[role="listbox"],[role="menu"]')).filter(x=>visible(x)&&x.querySelector('[role="option"]'));
                    if(combos.length!==1)break;
                    if(menus.length===1){owned=menus;break;}
                  }
                }
                const customOptions=role==='combobox'&&e.getAttribute('aria-expanded')==='true'?owned.flatMap(x=>Array.from(x.querySelectorAll('[role="option"]'))).filter(visible).map(x=>x.innerText.trim()):[];
                return {tag:e.tagName.toLowerCase(),role:role,type:e.type||'',name:e.getAttribute('name')||'',dom_id:e.id||'',heading:!!e.closest('h1,[role="heading"][aria-level="1"]'),
                  expanded:e.getAttribute('aria-expanded')==='true',search_scope:!!e.closest('search,[role="search"]'),
                  text:(e.innerText||e.getAttribute('title')||e.getAttribute('value')||'').trim().slice(0,180),
                  label:label.slice(0,160),context:(row?row.innerText:'').trim().slice(0,260),
                  value:e.type==='password'?'[redacted]':(e.value||(e.isContentEditable||role==='combobox'?e.innerText:'')),checked:['checkbox','radio'].includes(e.type)?e.checked:null,
                  disabled:!!e.disabled||e.getAttribute('aria-disabled')==='true',
                  href:e.tagName==='A'?e.getAttribute('href'):null,
                  selected_text:e.tagName==='SELECT'&&e.selectedOptions.length?e.selectedOptions[0].text.trim():null,
                  options:e.tagName==='SELECT'?Array.from(e.options).map(o=>o.text.trim()):customOptions};
            })''',handles)
        for el,info in zip(handles,infos):
            if info is None:continue
            if info['type'] in ('hidden','password','file'): continue
            # Long option text is redundant with options and dominates model context.
            if info['tag']=='select': info['text']=''
            info['context']=re.sub(r'\s+',' ',info['context'])
            if info.get('href'): info['href']=redact_url_secrets(info['href'])
            ident=f'g{self.generation}t{len(controls)}'
            info={'id':ident,**info}
            controls.append(info); self.targets[ident]=(el,info)
        return {'url':obs.url,'aria':obs.aria[:24000],'controls':controls}

    def act(self,action):
        if action['kind']=='observe':return
        if action['kind']=='batch':
            steps=action.get('steps',[])
            if not 2<=len(steps)<=5:raise ValueError('A batch requires two to five form field actions')
            if len({s['target_id'] for s in steps})!=len(steps):raise ValueError('Duplicate batch target')
            for step in steps:
                if step['kind'] not in ('fill','select','check'):raise ValueError('Batches may only edit observed form fields')
                self._validate(step)
            # Not a transaction: a failure stops remaining steps; feedback includes
            # completed steps so the planner can inspect partial edits safely.
            completed=0
            try:
                for step in steps:self.act(step);completed+=1
            except Exception as exc:raise ValueError(f'Batch stopped after {completed} field edits: {exc}') from exc
            return
        target,info=self._validate(action)
        kind=action['kind']; ident=action['target_id']; value=action['value']
        if kind=='click':
            if not target.is_visible():
                child=[x for x in target.query_selector_all(':scope > *') if x.is_visible()]
                if len(child)!=1: raise ValueError('No unique visible child for target')
                target=child[0]
            # Wait for DOM readiness below; do not wait for every resource in a
            # scheduled navigation (CRM pages may keep background requests open).
            target.click(no_wait_after=True)
        elif kind=='fill': target.fill(value)
        elif kind=='select':
            if value not in (info.get('options') or []): raise ValueError('Option is not in observed options')
            target.select_option(label=value,timeout=30000)
        elif kind=='check':
            if value not in ('true','false'): raise ValueError('Checkbox requires true/false')
            target.set_checked(value=='true')
        elif kind=='press':
            if value not in ('Enter','Tab','Escape','ArrowDown','ArrowUp'): raise ValueError('Unsupported key')
            target.press(value,no_wait_after=True)
        else: raise ValueError('Unsupported UI action')
        self.ui.page.wait_for_load_state('domcontentloaded',timeout=30000)
        self.ui._check_origin()

    def _validate(self,action):
        kind=action['kind'];ident=action['target_id']
        self.ui._check_origin()
        if redact_url_secrets(self.ui.page.url)!=self.observed_url:raise ValueError('Page changed while planning; observe again')
        if kind not in ('click','fill','select','check','press'):raise ValueError('Unsupported UI action')
        if ident not in self.targets: raise ValueError('Unknown/stale target ID; use the latest observation')
        target,info=self.targets[ident]
        current=target.evaluate('''e=>{
            const td=e.closest('td');
            return {expanded:e.getAttribute('aria-expanded')==='true',search_scope:!!e.closest('search,[role="search"]'),connected:e.isConnected,disabled:!!e.disabled||e.getAttribute('aria-disabled')==='true',
              value:e.value||(e.isContentEditable?e.innerText:''),type:e.type||'',name:e.getAttribute('name')||'',
              text:(e.innerText||e.getAttribute('title')||e.getAttribute('value')||'').trim().slice(0,180),
              label:(e.getAttribute('aria-label')||Array.from(e.labels||[]).map(x=>x.innerText).join(' ')||e.getAttribute('placeholder')||(td&&td.previousElementSibling?td.previousElementSibling.innerText:'')||'').trim().slice(0,160),
              href:e.tagName==='A'?e.getAttribute('href'):null};
        }''')
        if current.get('search_scope',False)!=info.get('search_scope',False):raise ValueError('Observed action moved outside its search context; observe again')
        if not current['connected'] or current['disabled']:raise ValueError('Observed control is detached or disabled; observe again')
        if current['type']!=info['type'] or current['name']!=info['name'] or (kind in ('fill','select','check') and current['value']!=info['value']):
            raise ValueError('Observed control changed while planning; observe again')
        if kind=='click' and info['tag']!='select' and current['text']!=info['text']:raise ValueError('Observed action label changed; observe again')
        if current['label']!=info['label'] or (redact_url_secrets(current['href']) if current['href'] else current['href'])!=info['href']:
            raise ValueError('Observed control identity changed; observe again')
        if kind=='select' and action['value'] not in (info.get('options') or []):raise ValueError('Option is not in observed options')
        if kind=='check' and action['value'] not in ('true','false'):raise ValueError('Checkbox requires true/false')
        label=' '.join([info['text'],info['label'],info['name']])
        if FORBIDDEN.search(label): raise SecurityViolation('Action outside benchmark scope: '+label[:120])
        search_field=info['name'].startswith(('search','sall')) or 'search' in info['label'].lower()
        if not self.writes:
            if kind in ('fill','select','press') and not search_field:
                raise SecurityViolation('Read-only policy: only search input is allowed')
            if kind=='check': raise SecurityViolation('Read-only policy blocks checkbox mutation')
            if kind=='click' and re.search(r'\b(save|create|modify|edit|convert|close|new|validate)\b',label,re.I):
                raise SecurityViolation('Read-only policy blocks mutation control')
        return target,info
