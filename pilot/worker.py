import json
import sys
from pathlib import Path
from .store import Store
from .runtime import configure_browser

def work(root,ident,check=False):
    configure_browser();store=Store(root)
    if check:
        c=store.connection(ident,private=True)
        from uiagent.browser import BrowserUI
        from uiagent.records import RecordAdapter
        adapter=RecordAdapter(c['platform'],c['config'])
        with BrowserUI(adapter.base,allow_writes=True,artifacts=str(store.root/'checks'/ident),capture=False) as ui:
            adapter.login(ui)
        store.connection_status(ident,'ready','Signed in successfully through the browser')
        return
    run=store.run(ident);c=store.connection(run['connection_id'],private=True)
    from uiagent.records import RecordAdapter
    from uiagent.journal import Journal
    from uiagent.recovery import RecoveryEngine
    out=store.root/'runs'/ident/str(run['attempt'])
    adapter=RecordAdapter(c['platform'],c['config'])
    def progress(event,ui,data):
        if event=='planner_action':store.event(ident,'progress',f"AI decision {data['step']}: {data['kind']}")
    from .model import ModelPlanner
    engine=RecoveryEngine(adapter,Journal(store.root/'operations.sqlite'),out,store.root/'planner-work',hook=progress,planner_factory=lambda *args:ModelPlanner(store,*args))
    result=engine.run(run['payload'])
    store.update_run(ident,result['status'],result['reason'],result)
    store.event(ident,result['status'],result['reason'])

def main(root,ident,check=False):
    try:work(root,ident,check)
    except Exception as exc:
        store=Store(root)
        # Do not expose request bodies, credentials or arbitrary page content.
        reason=f'{type(exc).__name__}: connection or browser step failed. Check sign-in and try Check outcome.'
        if check:store.connection_status(ident,'error',reason)
        else:store.update_run(ident,'error',reason);store.event(ident,'error',reason)
        return 1
    return 0
