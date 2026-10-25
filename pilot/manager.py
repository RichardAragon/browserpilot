import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time
from .store import TERMINAL

class Manager:
    def __init__(self,store):
        self.store=store;self.jobs=queue.Queue();self.lock=threading.RLock();self.active={};self.queued=set();self.closing=False
        store.recover_startup()
        from .memory import PlatformMemory
        self.memory=PlatformMemory(store)
        self.thread=threading.Thread(target=self.loop,daemon=True);self.thread.start()

    def command(self,ident,check=False):
        command=[sys.executable] if getattr(sys,'frozen',False) else [sys.executable,str(Path(__file__).resolve().parents[1]/'app.py')]
        return command+['--worker',ident,'--data',str(self.store.root)]+(['--check'] if check else [])

    def submit(self,ident,retry=False):
        with self.lock:
            r=self.store.run(ident)
            if self.closing:raise ValueError('App is shutting down')
            if ident in self.queued or ident in self.active:raise ValueError('This task is already running')
            if retry and r['status'] not in TERMINAL:raise ValueError('Wait for the current attempt to finish')
            self.store.update_run(ident,'queued','Checking the original operation' if retry else 'Waiting to start')
            self.queued.add(ident);self.jobs.put(('run',ident))

    def connect(self,ident,action='open'):
        with self.lock:
            c=self.store.connection(ident)
            if ident in self.queued or ident in self.active:raise ValueError('Connection check is already running')
            self.queued.add(ident);self.store.connection_status(ident,'checking','Opening browser…')
            self.jobs.put((action if c['platform'] in ('instagram','messenger','website') or action=='inspect' else 'check',ident))

    def approve(self,ident):
        from uiagent.journal import digest
        with self.lock:
            r=self.store.run(ident)
            if r['status']!='awaiting_approval' or not r['result'].get('prepared'):raise ValueError('This task has no current field review to approve')
            result=r['result'];result['approval']=digest(result['prepared'])
            self.store.update_run(ident,r['status'],'Reviewed field changes approved',result)
            self.store.event(ident,'approved','User approved the discovered field changes')
            self.submit(ident,retry=True)

    def stop(self,ident):
        with self.lock:
            run=self.store.run(ident)
            if ident in self.queued:
                self.queued.remove(ident);self.store.update_run(ident,'cancelled','Stopped before execution');self.store.event(ident,'cancelled','Stopped before execution');return
            proc=self.active.get(ident)
            if proc is None:raise ValueError('This task is no longer running')
            self.store.update_run(ident,'stopping','Stopping. A submitted change may still complete.')
            if hasattr(proc,'pid'):
                if proc.poll() is None:subprocess.run(['taskkill','/PID',str(proc.pid),'/T','/F'],capture_output=True,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            else:proc.set()

    def loop(self):
        from .social import SocialBrowser
        from .website import WebsiteBrowser
        social=SocialBrowser(self.store)
        website=WebsiteBrowser(self.store)
        while True:
            kind,ident=self.jobs.get()
            if kind=='shutdown':social.close();website.close();return
            with self.lock:
                if ident not in self.queued:continue
                self.queued.remove(ident)
                run=self.store.run(ident) if kind=='run' else None
                c=self.store.connection(run['connection_id'] if run else ident)
                if run:
                    self.store.update_run(ident,'running','Inspecting current browser state',attempt=run['attempt']+1)
                    self.store.event(ident,'started','Inspecting current state before any change')
                    revision=self.store.platform_notes(c['id'])['revision']
                    if revision:self.store.event(ident,'memory',f'Planner consulting platform notes revision {revision}; live browser checks still required')
                general=c['platform']=='website' or kind=='inspect' or bool(run and run['payload']['kind'] in ('browser_update','browser_create'))
                if os.environ.get('BROWSERPILOT_GENERIC_ONLY')=='1' and not general:
                    if run:self.store.update_run(ident,'blocked','Platform-specific adapters are disabled in this validation session')
                    else:self.store.connection_status(ident,'error','Platform-specific adapters disabled')
                    continue
                if c['platform'] in ('instagram','messenger') or general:
                    stop=threading.Event();self.active[ident]=stop;proc=None
                else:
                    log=self.store.root/'worker-logs';log.mkdir(exist_ok=True)
                    stream=(log/f'{ident}.log').open('a',encoding='utf-8')
                    try:
                        proc=subprocess.Popen(self.command(ident,kind=='check'),stdout=stream,stderr=stream,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
                    except OSError:
                        stream.close()
                        reason='The browser worker could not start. Reopen BrowserPilot and check the original outcome before retrying.'
                        if run:self.store.update_run(ident,'error',reason);self.store.event(ident,'error',reason)
                        else:self.store.connection_status(ident,'error',reason)
                        continue
                    self.active[ident]=proc
            try:
                if general and not getattr(sys,'frozen',False) and os.environ.get('BROWSERPILOT_DEV_RELOAD')=='1':
                    # Source-development only, between jobs on the browser's
                    # owning thread. Keep live forms and all intent journals.
                    import importlib
                    from uiagent import control_catalog
                    from . import setup,references,drafts,creation,website as controller
                    for module in (control_catalog,setup,references,drafts,creation,controller):importlib.reload(module)
                    website.__class__=controller.WebsiteBrowser
                if proc:
                    proc.wait();stream.close()
                    if run and self.store.run(ident)['status'] in ('running','stopping'):
                        self.store.update_run(ident,'interrupted','Execution stopped. Check outcome to inspect safely before any retry.')
                        self.store.event(ident,'interrupted','Worker stopped; original operation retained')
                    elif not run and self.store.connection(ident)['status']=='checking':self.store.connection_status(ident,'error','Connection check stopped')
                elif general and run:website.run(self.store.run(ident),stop)
                elif general and kind=='inspect':website.inspect(ident)
                elif general:website.open(ident)
                elif run:social.run(self.store.run(ident),stop)
                elif kind=='verify':social.verify(ident)
                else:social.open(ident)
            except Exception as exc:
                reason=f'{type(exc).__name__}: browser step could not complete. Reconnect to inspect the session.'
                if isinstance(exc,ValueError):reason=str(exc)
                elif 'net::ERR_' in str(exc):reason='Browser could not load this site. Check the sign-in window and network connection.'
                import re
                diagnostic=re.sub(r'https?://\S+','[site]',str(exc).splitlines()[0])[:400]
                (self.store.root/'last-browser-error.txt').write_text(type(exc).__name__+': '+diagnostic,encoding='utf-8')
                if run:self.store.update_run(ident,'uncertain' if self.store.run(ident)['result'].get('send_intent') else 'error',reason);self.store.event(ident,'error',reason)
                else:self.store.connection_status(ident,'needs_login',reason)
            finally:
                with self.lock:self.active.pop(ident,None)
                if general and not self.closing:
                    if (run and self.store.run(ident)['status'] in ('confirmed','blocked','uncertain','duplicate_detected')) or (not run and kind=='inspect'):
                        self.memory.learn(c['id'])

    def close(self):
        self.closing=True
        with self.lock:
            for ident,proc in list(self.active.items()):
                if hasattr(proc,'pid'):
                    if proc.poll() is None:subprocess.run(['taskkill','/PID',str(proc.pid),'/T','/F'],capture_output=True,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
                else:proc.set()
            self.queued.clear();self.jobs.put(('shutdown',''))
        self.thread.join(timeout=5)
