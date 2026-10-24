"""BrowserPilot local application. Platform writes occur only through browser UI."""
import argparse
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import json
import mimetypes
import os
from pathlib import Path
import secrets
import sys
import threading
from urllib.parse import urlsplit,unquote
import webbrowser

from pilot.runtime import resource_root,configure_browser,health
from pilot.store import Store,identifier

class Server(ThreadingHTTPServer):
    daemon_threads=True
    def __init__(self,address,store,manager):
        self.store=store;self.manager=manager;self.token=secrets.token_urlsafe(32);self.health=health()
        from pilot.assistant import AssistantService
        self.assistant=AssistantService(store)
        super().__init__(address,Handler)
    @property
    def origin(self):return 'http://127.0.0.1:'+str(self.server_port)

class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args):pass
    def reply(self,status,value,kind='application/json; charset=utf-8'):
        body=json.dumps(value,ensure_ascii=False).encode() if kind.startswith('application/json') else value
        self.send_response(status);self.send_header('Content-Type',kind);self.send_header('Content-Length',str(len(body)))
        self.send_header('Cache-Control','no-store');self.send_header('X-Content-Type-Options','nosniff')
        self.send_header('Referrer-Policy','no-referrer')
        self.send_header('Content-Security-Policy',"default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        self.end_headers();self.wfile.write(body)
    def valid_host(self):return self.headers.get('Host')==urlsplit(self.server.origin).netloc
    def do_GET(self):
        if not self.valid_host():return self.reply(403,{'error':'Invalid host'})
        path=urlsplit(self.path).path
        try:
            if path=='/api/state':
                return self.reply(200,{'token':self.server.token,'health':self.server.health,'connections':self.server.store.connections(),'runs':self.server.store.runs(),'model':self.server.store.model_config(),'chat':self.server.store.chat_messages()})
            if path.startswith('/api/runs/'):
                ident=identifier(path.split('/')[3]);r=self.server.store.run(ident)
                return self.reply(200,{'run':r,'events':self.server.store.events(ident),'screenshots':self.screenshots(ident)})
            if path.startswith('/api/notes/'):
                ident=identifier(path.split('/')[3]);self.server.store.connection(ident)
                return self.reply(200,{'memory':self.server.store.platform_notes(ident),'history':self.server.store.notes_history(ident)})
            if path.startswith('/evidence/'):
                parts=path.split('/');ident=identifier(parts[2]);name=unquote('/'.join(parts[3:]))
                allowed=self.screenshots(ident)
                if name not in allowed:raise ValueError('Evidence not found')
                return self.reply(200,(self.server.store.root/'runs'/ident/name).read_bytes(),'image/png')
            files={'/':'index.html','/app.js':'app.js','/style.css':'style.css'}
            if path not in files:return self.reply(404,{'error':'Not found'})
            name=files[path];kind={'html':'text/html','js':'application/javascript','css':'text/css'}[name.split('.')[-1]]
            return self.reply(200,(resource_root()/'web'/name).read_bytes(),kind+'; charset=utf-8')
        except (ValueError,IndexError):return self.reply(404,{'error':'Not found'})
    def screenshots(self,ident):
        root=self.server.store.root/'runs'/ident
        return [p.relative_to(root).as_posix() for p in sorted(root.rglob('*.png'))][-20:] if root.exists() else []
    def do_POST(self):
        if not self.valid_host() or self.headers.get('Origin')!=self.server.origin or not secrets.compare_digest(self.headers.get('X-BrowserPilot-Token',''),self.server.token):
            return self.reply(403,{'error':'Open BrowserPilot locally to continue'})
        if self.headers.get('Content-Type','').split(';')[0]!='application/json':return self.reply(415,{'error':'JSON required'})
        try:
            length=int(self.headers.get('Content-Length','0'))
            if not 0<length<=16000:raise ValueError('Request is too large or empty')
            data=json.loads(self.rfile.read(length))
            if not isinstance(data,dict):raise ValueError('Invalid request')
            path=urlsplit(self.path).path;s=self.server.store;m=self.server.manager
            if path=='/api/connections':return self.reply(201,s.add_connection(data))
            if path=='/api/model':return self.reply(200,s.save_model(data))
            if path=='/api/notes/learn':m.memory.learn(identifier(data['id']));return self.reply(202,{'ok':True})
            if path=='/api/notes/save':
                ident=identifier(data['id']);old=s.platform_notes(ident)
                return self.reply(200,s.save_notes(ident,data['revision'],old['notes'],data.get('guidance','')))
            if path=='/api/notes/restore':
                ident=identifier(data['id']);version=next((h for h in s.notes_history(ident) if h['revision']==data['restore_revision']),None)
                if not version:raise ValueError('That revision is no longer available')
                return self.reply(200,s.save_notes(ident,data['revision'],version['content']['notes'],version['content']['user_guidance'],'restored'))
            if path=='/api/assistant':return self.reply(202,self.server.assistant.ask(data.get('message')))
            if path=='/api/connect':m.connect(identifier(data['id']),data.get('action','open'));return self.reply(202,{'ok':True})
            if path=='/api/runs':
                r=s.create_run(data);m.submit(r['id']);return self.reply(201,r)
            if path=='/api/retry':m.submit(identifier(data['id']),retry=True);return self.reply(202,{'ok':True})
            if path=='/api/approve':m.approve(identifier(data['id']));return self.reply(202,{'ok':True})
            if path=='/api/stop':m.stop(identifier(data['id']));return self.reply(202,{'ok':True})
            if path=='/api/shutdown':
                self.reply(200,{'ok':True});threading.Thread(target=self.server.shutdown,daemon=True).start();return
            self.reply(404,{'error':'Not found'})
        except (ValueError,KeyError,TypeError) as exc:self.reply(400,{'error':str(exc) if isinstance(exc,ValueError) else 'Missing or invalid fields'})
        except Exception:self.reply(500,{'error':'The local operation failed. No automatic retry was made.'})

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--data',default=str(Path(os.environ.get('LOCALAPPDATA',Path.home()))/'BrowserPilot'))
    parser.add_argument('--port',type=int,default=8765);parser.add_argument('--no-open',action='store_true');parser.add_argument('--worker');parser.add_argument('--check',action='store_true')
    args=parser.parse_args();configure_browser()
    if args.worker:
        from pilot.worker import main as worker
        return worker(args.data,args.worker,args.check)
    # A process-owned lock prevents two app instances from sharing a queue/profile.
    from uiagent.journal import Journal,Busy
    store=Store(args.data);lock=Journal(store.root/'instance.sqlite').resource_lock('BrowserPilot singleton')
    try:lock.__enter__()
    except Busy:
        address=store.root/'address.txt'
        if address.exists() and not args.no_open:webbrowser.open(address.read_text())
        return 0
    from pilot.manager import Manager
    manager=Manager(store)
    try:
        try:server=Server(('127.0.0.1',args.port),store,manager)
        except OSError:server=Server(('127.0.0.1',0),store,manager)
        (store.root/'address.txt').write_text(server.origin)
        if not args.no_open:webbrowser.open(server.origin)
        server.serve_forever(poll_interval=.2)
    finally:
        manager.close();lock.__exit__(None,None,None)
    return 0

if __name__=='__main__':raise SystemExit(main())
