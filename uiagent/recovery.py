"""Durable, bounded browser-only operation execution.

The planner proposes UI actions. This controller owns replay protection,
verification, commit intent, and conflict decisions. No saved locator is replayed.
"""
import json
from pathlib import Path
import time
from .browser import BrowserUI
from .control_catalog import ControlCatalog
from .codex_planner import CodexPlanner
from .journal import Journal,Busy,ContractMismatch
from .records import matches,normalize

def changed(old,new):return {k for k in old if normalize(k,old[k])!=normalize(k,new.get(k))}

class RecoveryEngine:
    def __init__(self,adapter,journal,out,empty_cwd,hook=None,planner_factory=None):
        self.adapter=adapter;self.journal=journal;self.out=Path(out);self.out.mkdir(parents=True,exist_ok=True)
        self.empty_cwd=empty_cwd;self.hook=hook or (lambda event,ui,c:None)
        self.planner=None;self.actions=[];self.probe_count=0;self.rebases=0
        self.network=[]
        self.planner_factory=planner_factory or CodexPlanner

    def probe(self,ui,c):
        self.probe_count+=1
        p=self.adapter.probe(ui,c['name'],self.out/f'probe-{self.probe_count:02d}')
        self.journal.record(c['operation_id'],'ui_probe',p)
        return p

    def result(self,c,status,reason,proof=None):
        return {'operation_id':c['operation_id'],'status':status,'reason':reason,'proof':proof or {},
                'planner_calls':self.planner.calls if self.planner else [],'actions':self.actions,
                'ui_probes':self.probe_count,'rebases':self.rebases,'isolation':c['isolation'],
                'verification_scope':'UI-visible requested fields only; no atomic concurrency guarantee'}

    def stop(self,c,status,reason,proof=None):
        self.journal.record(c['operation_id'],status,proof,phase=status,reason=reason)
        return self.result(c,status,reason,proof)

    def verify(self,ui,c):
        p=self.probe(ui,c)
        if p['count']==1 and matches(p['state'],self.desired(c)):
            return self.stop(c,'confirmed','Fresh UI shows exactly one matching record with all requested values',p)
        return self.stop(c,'uncertain','Save outcome is not confirmed; no automatic replay of the submission',p)

    def desired(self,c):
        return {**c['desired'],'name':c['name'],'record_type':'prospect' if c['platform']=='dolibarr' else 'company'}

    def run(self,c):
        start=time.monotonic()
        try:
            if c.get('origin',self.adapter.base)!=self.adapter.base:raise ValueError('Contract origin differs from configured deployment')
            c={**c,'origin':self.adapter.base}
            if c['kind'] not in ('create','update') or c['isolation'] not in ('optimistic_ui','serializable'):raise ValueError('Unsupported operation contract')
            if set(c['desired'])-{'name','email','phone','city','address'}:raise ValueError('Unsupported desired field')
            if c['desired'].get('name',c['name'])!=c['name']:raise ValueError('Renaming identity is outside this contract')
            self.journal.ensure(c)
            key=self.adapter.base+'|'+c['name']
            with self.journal.resource_lock(key):
                r=self._run(c)
        except Busy as exc:r=self.result(c,'busy',str(exc))
        except ContractMismatch as exc:r=self.result(c,'contract_mismatch',str(exc))
        except Exception as exc:
            # In particular, never rewrite a durable commit intent as safe-to-retry.
            reason=f'{type(exc).__name__}: {exc}'
            password=getattr(self.adapter,'config',{}).get('password')
            if password:reason=reason.replace(password,'[redacted]')
            r=self.result(c,'error',reason)
        r['seconds']=round(time.monotonic()-start,3)
        (self.out/'browser-network.json').write_text(json.dumps(self.network,indent=2),encoding='utf-8')
        (self.out/'result.json').write_text(json.dumps(r,indent=2,ensure_ascii=False),encoding='utf-8')
        return r

    def _run(self,c):
        a=self.adapter;j=self.journal;ident=c['operation_id'];saved=j.load(ident)
        j.record(ident,'attempt_started',{'prior_phase':saved['phase']})
        if c['isolation']=='serializable' and not a.atomic_compare_and_swap:
            return self.stop(c,'blocked','Serializable update requested, but this UI adapter has no atomic compare-and-save or server lock')
        with BrowserUI(a.base,allow_writes=True,artifacts=str(self.out/'browser')) as ui:
            self.network=ui.network_log
            a.login(ui)
            live=a.find(ui,c['name']);live.pop('observation',None)
            self.probe_count+=1;j.record(ident,'initial_ui_probe',live)
            if live['count']>1:return self.stop(c,'blocked','Record identity is ambiguous',live)
            if live['count']==1 and matches(live['state'],self.desired(c)):
                return self.stop(c,'confirmed','Fresh UI confirms operation already satisfied; no write or model call needed',live)
            if saved['phase']=='confirmed' and live['count']==1:
                return self.stop(c,'blocked','Previously confirmed operation has drifted; replay must not overrule a later edit',live)
            pending=j.pending_for_identity(c)
            if pending:return self.stop(c,'waiting','Another operation has an unresolved submission for this identity',{'pending_operation_ids':pending,'live':live})
            if saved['phase']=='blocked':return self.stop(c,'blocked','Operation remains blocked; use a new reviewed contract to change it',live)
            if c['kind']=='create':
                if live['count']==1:return self.stop(c,'blocked','Existing identity has different values; refusing duplicate or replacement',live)
                if saved['phase'] in ('commit_intent','uncertain','confirmed'):
                    return self.stop(c,'uncertain','Prior create may have committed but is not visible; refusing a second submission',live)
            else:
                if live['count']!=1:return self.stop(c,'blocked','Requested record is missing',live)
                prior=saved.get('baseline')
                if prior and changed(prior,live['state']) & set(c['desired']):
                    return self.stop(c,'blocked','Requested field changed since the durable baseline',live)
                if saved['phase'] in ('commit_intent','uncertain'):
                    return self.stop(c,'uncertain','Prior update may still commit or have side effects; requested state is not confirmed, so submission is not replayed',live)
                if saved['phase']=='blocked':return self.stop(c,'blocked','Operation remains blocked; use a new reviewed contract to change it',live)
                a.open_edit(ui,live['url'],c['name'])
            baseline=live['state'] if live['count']==1 else None
            j.record(ident,'ready',{'live':live},phase='editing',baseline=baseline)
            goal=(f'Create exactly one {"prospect, not a customer" if c["platform"]=="dolibarr" else "company contact"} named {c["name"]}. ' if c['kind']=='create' else f'Update the open company record {c["name"]}. ')
            goal+='Set only these fields to the exact values: '+json.dumps(c['desired'])+'. Save. Preserve all other fields. Do not navigate away after editing. A controller will independently verify the save.'
            self.planner=self.planner_factory(goal,self.out/'planner',self.empty_cwd,'batch')
            cat=ControlCatalog(ui);dirty=False;errors=0
            for step in range(18):
                obs=cat.observe(f'step-{step:02d}')
                (self.out/f'observation-{step:02d}.json').write_text(json.dumps(obs,indent=2,ensure_ascii=False),encoding='utf-8')
                action=self.planner.next_action(obs);self.actions.append(action)
                self.hook('planner_action',ui,{'operation_id':ident,'kind':action['kind'],'step':step+1})
                print(f'{c["platform"]}/{ident[-8:]} {step}: {action["kind"]} {action["reason"][:95]}',flush=True)
                if action['kind']=='finish':
                    if action['outcome']=='succeeded':return self.verify(ui,c)
                    return self.stop(c,'blocked',action['answer'])
                info=cat.targets.get(action['target_id'],(None,{}))[1]
                if action['kind']=='click' and a.is_save(info):
                    if not matches(a.state(obs),self.desired(c)):
                        errors+=1
                        self.planner.feedback(action,'REJECTED: visible form does not yet match every requested field and record type. Complete the exact contract before Save.')
                        j.record(ident,'incomplete_form_rejected',{})
                        if errors>=3:return self.stop(c,'blocked','Form does not satisfy the immutable contract')
                        continue
                    return self.commit(ui,cat,c,live,baseline)
                try:
                    steps=action['steps'] if action['kind']=='batch' else [action]
                    mutation=False
                    for s in steps:
                        target=cat.targets.get(s['target_id'],(None,{}))[1]
                        if s['kind'] in ('fill','select','check'):
                            key=a.field_key(target)
                            search=target.get('name','').startswith('search') or 'search' in target.get('label','').lower()
                            if search and not dirty and s['value']==c['name']:continue
                            type_check=c['kind']=='create' and s['kind']=='check' and (
                                (c['platform']=='dolibarr' and (target.get('label'),s['value']) in (('Prospect','true'),('Customer','false'))) or
                                (c['platform']=='odoo' and (target.get('label'),s['value'])==('Company','true')))
                            if type_check:mutation=True;continue
                            if key not in c['desired'] or s['kind']!='fill' or normalize(key,s['value'])!=normalize(key,c['desired'][key]):
                                raise ValueError('Field edit is outside the immutable requested patch')
                            mutation=True
                        elif s['kind']=='press':raise ValueError('Keyboard submissions are disabled; use an observed Save control')
                        elif dirty and s['kind']!='observe':raise ValueError('Navigation while dirty is blocked to prevent implicit saves')
                    j.record(ident,'action_intent',{'action':action})
                    cat.act(action)
                    j.record(ident,'action_executed',{'action':action})
                    if mutation:
                        dirty=True;self.hook('after_fields',ui,c)
                    self.planner.feedback(action,'Executed. Inspect current UI; Save is independently guarded.')
                except Exception as exc:
                    errors+=1;j.record(ident,'action_rejected',{'error':str(exc)})
                    self.planner.feedback(action,'REJECTED: '+str(exc))
                    if errors>=3:return self.stop(c,'blocked','Action rejection budget exceeded')
            return self.stop(c,'blocked','Planner step budget exceeded')

    def commit(self,ui,cat,c,live,baseline):
        a=self.adapter;j=self.journal;ident=c['operation_id']
        self.hook('before_guard',ui,c)
        for attempt in range(3):
            fresh=self.probe(ui,c)
            if c['kind']=='create':
                if fresh['count']==1 and matches(fresh['state'],self.desired(c)):
                    return self.stop(c,'confirmed','Another local attempt already satisfied this creation',fresh)
                if fresh['count']!=0:return self.stop(c,'blocked','Identity appeared before create; refusing duplicate',fresh)
                break
            if fresh['count']!=1:return self.stop(c,'blocked','Identity became missing or ambiguous before save',fresh)
            changes=changed(baseline,fresh['state'])
            overlap=changes & set(c['desired'])
            if overlap:
                if matches(fresh['state'],self.desired(c)):return self.stop(c,'confirmed','Concurrent writer already supplied requested values',fresh)
                return self.stop(c,'blocked','Concurrent edit conflicts with requested fields: '+','.join(sorted(overlap)),fresh)
            if not changes:break
            if attempt==2:return self.stop(c,'blocked','Record kept changing; bounded rebase budget exhausted',fresh)
            self.rebases+=1;baseline=fresh['state']
            j.record(ident,'rebase',{'changed_fields':sorted(changes),'fresh':fresh},baseline=baseline)
            a.open_edit(ui,fresh['url'],c['name']);cat=ControlCatalog(ui)
            a.apply_patch(ui,cat,c['desired'])
        # SQLite commit completes before the possible CRM mutation.
        if not matches(a.state(cat.observe('commit-form')),self.desired(c)):
            return self.stop(c,'blocked','Visible form drifted before submission; nothing was submitted')
        j.record(ident,'commit_intent',{'desired':self.desired(c)},phase='commit_intent',baseline=baseline)
        self.hook('after_intent',ui,c)
        self.hook('after_guard',ui,c)
        try:
            a.save(ui,cat)
            self.hook('after_save',ui,c)
        except Exception as exc:
            j.record(ident,'save_uncertain',{'error':str(exc)})
            return self.verify(ui,c)
        j.record(ident,'save_returned',{})
        return self.verify(ui,c)
