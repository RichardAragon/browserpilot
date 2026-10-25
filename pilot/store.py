from __future__ import annotations
import base64
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import re
import sqlite3
import time
from urllib.parse import urlsplit
import uuid

PLATFORMS = {'dolibarr','odoo','messenger','instagram','website'}
TERMINAL = {'confirmed','visible','blocked','uncertain','error','cancelled','interrupted','waiting','needs_login','needs_user','duplicate_detected','busy','contract_mismatch','awaiting_approval'}

def protect(value: str) -> str:
    if not value:return ''
    if os.name != 'nt':raise ValueError('Saved credentials require Windows encryption')
    class Blob(ctypes.Structure):
        _fields_=[('size',wintypes.DWORD),('data',ctypes.POINTER(ctypes.c_ubyte))]
    raw=value.encode();buf=ctypes.create_string_buffer(raw)
    src=Blob(len(raw),ctypes.cast(buf,ctypes.POINTER(ctypes.c_ubyte)));dest=Blob()
    if not ctypes.windll.crypt32.CryptProtectData(ctypes.byref(src),'BrowserPilot',None,None,None,1,ctypes.byref(dest)):
        raise ctypes.WinError()
    try:return base64.b64encode(ctypes.string_at(dest.data,dest.size)).decode()
    finally:ctypes.windll.kernel32.LocalFree(dest.data)

def unprotect(value: str) -> str:
    if not value:return ''
    class Blob(ctypes.Structure):
        _fields_=[('size',wintypes.DWORD),('data',ctypes.POINTER(ctypes.c_ubyte))]
    raw=base64.b64decode(value);buf=ctypes.create_string_buffer(raw)
    src=Blob(len(raw),ctypes.cast(buf,ctypes.POINTER(ctypes.c_ubyte)));dest=Blob()
    if not ctypes.windll.crypt32.CryptUnprotectData(ctypes.byref(src),None,None,None,None,1,ctypes.byref(dest)):
        raise ctypes.WinError()
    try:return ctypes.string_at(dest.data,dest.size).decode()
    finally:ctypes.windll.kernel32.LocalFree(dest.data)

def origin(value):
    p=urlsplit(str(value).strip())
    if p.scheme not in ('http','https') or not p.hostname or p.username or p.password or p.query or p.fragment or p.path not in ('','/'):
        raise ValueError('Enter the website address, for example http://127.0.0.1:8088, without a path or sign-in details')
    if p.hostname.lower() in ('0.0.0.0','169.254.169.254'):raise ValueError('This address is not supported')
    return f'{p.scheme}://{p.netloc}'.rstrip('/')

def identifier(value):
    if not re.fullmatch('[a-f0-9]{32}',str(value)):raise ValueError('Invalid identifier')
    return value

class Store:
    def __init__(self,root):
        self.root=Path(root).resolve();self.root.mkdir(parents=True,exist_ok=True)
        self.path=self.root/'app.sqlite'
        with self.db() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS connections(id TEXT PRIMARY KEY, label TEXT, platform TEXT, config TEXT, secret TEXT, status TEXT, detail TEXT, created REAL);
            CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY, connection_id TEXT, title TEXT, payload TEXT, status TEXT, reason TEXT, result TEXT, attempt INTEGER, created REAL, updated REAL);
            CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, kind TEXT, message TEXT, created REAL);
            CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT);
            CREATE TABLE IF NOT EXISTS chat(id TEXT PRIMARY KEY,role TEXT,content TEXT,status TEXT,proposal TEXT,created REAL);
            CREATE TABLE IF NOT EXISTS platform_notes(connection_id TEXT PRIMARY KEY,revision INTEGER,content TEXT,updated REAL,status TEXT,detail TEXT);
            CREATE TABLE IF NOT EXISTS note_history(connection_id TEXT,revision INTEGER,content TEXT,updated REAL,author TEXT,PRIMARY KEY(connection_id,revision));
            ''')
    def db(self):
        db=sqlite3.connect(self.path,timeout=15);db.row_factory=sqlite3.Row
        db.execute('PRAGMA journal_mode=WAL');db.execute('PRAGMA synchronous=FULL')
        return db
    def connections(self):
        with self.db() as db:rows=db.execute('SELECT * FROM connections ORDER BY created').fetchall()
        return [self.public_connection(dict(r)) for r in rows]
    def public_connection(self,r):
        r['config']=json.loads(r['config']);r['has_password']=bool(r.pop('secret'))
        r['memory']=self.platform_notes(r['id'])
        return r

    def platform_notes(self,ident):
        with self.db() as db:r=db.execute('SELECT * FROM platform_notes WHERE connection_id=?',(identifier(ident),)).fetchone()
        if not r:return {'revision':0,'notes':[],'user_guidance':'','updated':0,'status':'empty','detail':'Learns after AI setup and browser tasks'}
        return {**json.loads(r['content']),'revision':r['revision'],'updated':r['updated'],'status':r['status'],'detail':r['detail']}

    def notes_context(self,ident):
        from .memory import ADVISORY
        n=self.platform_notes(ident)
        return {'policy':ADVISORY,'revision':n['revision'],'notes':n['notes'],'user_guidance':n['user_guidance']}

    def notes_status(self,ident,status,detail):
        with self.db() as db:
            db.execute("INSERT OR IGNORE INTO platform_notes VALUES(?,0,?,0,'empty','')",(identifier(ident),json.dumps({'notes':[],'user_guidance':''})))
            db.execute('UPDATE platform_notes SET status=?,detail=? WHERE connection_id=?',(status,detail,ident))

    def save_notes(self,ident,revision,notes,guidance,author='user'):
        if not isinstance(guidance,str) or len(guidance)>4000:raise ValueError('Keep guidance under 4,000 characters')
        self.connection(ident)
        content=json.dumps({'notes':notes,'user_guidance':guidance});now=time.time()
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT revision FROM platform_notes WHERE connection_id=?',(ident,)).fetchone()
            current=row[0] if row else 0
            if current!=revision:raise ValueError('Notes changed while this edit was being prepared. Reopen them to use the latest revision.')
            db.execute('INSERT OR REPLACE INTO platform_notes VALUES(?,?,?,?,?,?)',(ident,current+1,content,now,'ready','Notes saved'))
            db.execute('INSERT INTO note_history VALUES(?,?,?,?,?)',(ident,current+1,content,now,author))
        return self.platform_notes(ident)

    def notes_history(self,ident):
        with self.db() as db:rows=db.execute('SELECT * FROM note_history WHERE connection_id=? ORDER BY revision DESC LIMIT 20',(identifier(ident),)).fetchall()
        return [{**dict(r),'content':json.loads(r['content'])} for r in rows]
    def connection(self,ident,private=False):
        with self.db() as db:r=db.execute('SELECT * FROM connections WHERE id=?',(identifier(ident),)).fetchone()
        if not r:raise ValueError('Connection not found')
        result=self.public_connection(dict(r))
        if private:result['config']['password']=unprotect(r['secret'])
        return result
    def add_connection(self,data):
        platform=data.get('platform')
        if platform not in PLATFORMS:raise ValueError('Choose a supported platform')
        label=str(data.get('label','')).strip()[:80] or platform.title()
        if platform in ('dolibarr','odoo'):
            config={'base_url':origin(data.get('base_url','')),'login':str(data.get('login','')).strip()[:200]}
            if not config['login'] or not data.get('password'):raise ValueError('Enter your CRM username and password')
            if len(str(data['password']))>1000:raise ValueError('Password is too long')
            secret=protect(data['password'])
        elif platform=='website':
            url=str(data.get('base_url','')).strip();parsed=urlsplit(url)
            base=origin(f'{parsed.scheme}://{parsed.netloc}')
            if parsed.query or parsed.fragment:raise ValueError('Use a website address without secret query parameters')
            config={'base_url':base,'start_url':base+(parsed.path or '/'),'site_name':'Website','capabilities':[],'login':str(data.get('login','')).strip()[:200]};secret=protect(str(data.get('password','')))
            if bool(config['login'])!=bool(secret):raise ValueError('Provide both sign-in fields, or leave both empty for manual sign-in')
        else:
            handle=str(data.get('handle','')).strip().lstrip('@')
            if not re.fullmatch('[A-Za-z0-9._]{1,80}',handle):raise ValueError('Enter your own account username, without @')
            config={'base_url':'https://www.messenger.com' if platform=='messenger' else 'https://www.instagram.com','handle':handle.lower(),'conversation':''}
            secret=''
        ident=uuid.uuid4().hex
        with self.db() as db:db.execute('INSERT INTO connections VALUES(?,?,?,?,?,?,?,?)',(ident,label,platform,json.dumps(config),secret,'not_checked','Connect to check this account',time.time()))
        return self.connection(ident)
    def connection_status(self,ident,status,detail,patch=None):
        with self.db() as db:
            row=db.execute('SELECT config FROM connections WHERE id=?',(ident,)).fetchone()
            config=json.loads(row['config']);config.update(patch or {})
            db.execute('UPDATE connections SET status=?,detail=?,config=? WHERE id=?',(status,detail,json.dumps(config),ident))
    def create_run(self,data):
        connection=self.connection(data.get('connection_id',''));p=connection['platform'];kind=data.get('kind')
        ident=uuid.uuid4().hex
        if kind in ('browser_update','browser_create'):
            name=str(data.get('name','')).strip();fields=data.get('fields',{})
            if not name or len(name)>160 or not isinstance(fields,dict) or not (0 if kind=='browser_create' else 1)<=len(fields)<=8:raise ValueError('Specify one exact record and up to eight fields; updates need at least one field')
            if any(not isinstance(k,str) or not k.strip() or len(k)>100 or not isinstance(v,str) or len(v)>1000 for k,v in fields.items()):raise ValueError('Invalid field change')
            if p in ('messenger','instagram'):raise ValueError('Use the self-messaging workflow for this connection')
            payload={'operation_id':ident,'platform':p,'kind':kind,'name':name,'desired':fields,'origin':connection['config']['base_url']}
            record_type=str(data.get('record_type','')).strip()
            choices=data.get('choose_existing',[])
            if len(record_type)>100 or not isinstance(choices,list) or len(choices)>4 or any(not isinstance(x,str) or not x.strip() or len(x)>100 for x in choices):raise ValueError('Specify a record type and up to four delegated existing-record choices')
            from .references import validate_references
            refs=validate_references(data.get('references',[]),name,fields,choices,kind)
            if refs:payload['references']=refs
            payload.update(record_type=record_type,choose_existing=choices,draft_only=bool(re.search(r'order|invoice|quot|proposal|contract',record_type,re.I)))
            if kind=='browser_create':
                if any(k.casefold()=='name' and v!=name for k,v in fields.items()):raise ValueError('Name must match the record identity')
                # The assistant may repeat identity under the visible CRM label.
                # Canonicalize that repetition instead of mapping two fields to one node.
                identity_label=re.compile(r'^(?:(?:(?:third[ -]?party|company|contact|account|record|organization)\s+)?name|title|(?:external|customer)\s+ref(?:erence)?\.?)$',re.I)
                payload['desired']={'Name':name,**{k:v for k,v in fields.items() if not (identity_label.fullmatch(k.strip()) and v==name)}}
                # Draft is a verified outcome, not necessarily an editable input.
                if payload['draft_only']:
                    payload['desired']={k:v for k,v in payload['desired'].items() if not (k.strip().casefold() in ('status','state','document status','order status') and v.strip().casefold()=='draft')}

            title=('Create ' if kind=='browser_create' else 'Update ')+name
        elif p in ('dolibarr','odoo'):
            if kind not in ('create','update'):raise ValueError('Choose create or update')
            name=str(data.get('name','')).strip()
            if not name or len(name)>160:raise ValueError('Enter an exact company name, up to 160 characters')
            fields=data.get('fields',{})
            if not isinstance(fields,dict) or set(fields)-{'email','phone','city','address'}:raise ValueError('Unsupported field')
            if not fields or any(not isinstance(v,str) or len(v)>1000 for v in fields.values()):raise ValueError('Choose at least one valid field to set')
            isolation=data.get('isolation','optimistic_ui')
            if isolation not in ('optimistic_ui','serializable'):raise ValueError('Invalid isolation mode')
            desired={**fields,**({'name':name} if kind=='create' else {})}
            payload={'operation_id':ident,'platform':p,'kind':kind,'name':name,'desired':desired,'isolation':isolation,'origin':connection['config']['base_url'],'account':connection['config']['login']}
            title=('Create ' if kind=='create' else 'Update ')+name
        elif p=='website':raise ValueError('Choose a general browser create or update task')
        else:
            if kind!='self_message':raise ValueError('Messaging currently supports your own account only')
            if connection['status']!='ready' or not connection['config'].get('conversation'):raise ValueError('Connect and verify your own conversation first')
            body=str(data.get('message',''))
            if not body.strip() or len(body)>2000:raise ValueError('Enter a message between 1 and 2,000 characters')
            from .message_policy import make_marker
            marker=make_marker('IG' if p=='instagram' else 'MSG')
            payload={'operation_id':ident,'platform':p,'kind':kind,'body':body,'marker':marker,'sender':connection['config']['handle'],'conversation':connection['config']['conversation']}
            title='Message to yourself'
        now=time.time()
        with self.db() as db:db.execute('INSERT INTO runs VALUES(?,?,?,?,?,?,?,?,?,?)',(ident,connection['id'],title,json.dumps(payload),'queued','Waiting to start','{}',0,now,now))
        self.event(ident,'queued','Task queued')
        return self.run(ident)
    def run(self,ident):
        with self.db() as db:r=db.execute('SELECT * FROM runs WHERE id=?',(identifier(ident),)).fetchone()
        if not r:raise ValueError('Task not found')
        r=dict(r);r['payload']=json.loads(r['payload']);r['result']=json.loads(r['result'])
        return r
    def runs(self):
        with self.db() as db:ids=[r[0] for r in db.execute('SELECT id FROM runs ORDER BY created DESC LIMIT 100')]
        return [self.run(i) for i in ids]
    def task_context(self,connection_id=None):
        context=[]
        for run in self.runs():
            if run['status']!='confirmed' or (connection_id and run['connection_id']!=connection_id):continue
            p,r=run['payload'],run['result']
            if p['kind'] not in ('browser_create','browser_update','create','update'):continue
            fields=r.get('prepared',{}).get('fields',{})
            verified=r.get('proof',{}).get('state',{})
            if not verified:continue
            context.append({'run_id':run['id'],'connection_id':run['connection_id'],'name':p.get('name',''),
                'record_type':p.get('record_type',''),'operation':p['kind'],
                'verified_fields':[{ 'field':k,'value':v,'website_label':fields.get(k,{}).get('label',k),
                  'requested_field':fields.get(k,{}).get('task_field',k)} for k,v in verified.items()],
                'delegated_fields':p.get('choose_existing',[]),'verified_at':run['updated']})
            if len(context)>=12:break
        return context

    def update_run(self,ident,status,reason='',result=None,attempt=None):
        with self.db() as db:
            db.execute('UPDATE runs SET status=?,reason=?,updated=? WHERE id=?',(status,reason,time.time(),ident))
            if result is not None:db.execute('UPDATE runs SET result=? WHERE id=?',(json.dumps(result),ident))
            if attempt is not None:db.execute('UPDATE runs SET attempt=? WHERE id=?',(attempt,ident))
    def event(self,ident,kind,message):
        with self.db() as db:
            db.execute('INSERT INTO events(run_id,kind,message,created) VALUES(?,?,?,?)',(ident,kind,str(message)[:1200],time.time()))
            db.execute('UPDATE runs SET updated=? WHERE id=?',(time.time(),ident))
    def events(self,ident):
        with self.db() as db:return [dict(r) for r in db.execute('SELECT * FROM events WHERE run_id=? ORDER BY id',(identifier(ident),))]
    def pending_messages(self,connection_id,exclude):
        c=self.connection(connection_id)
        with self.db() as db:rows=db.execute("SELECT connection_id,payload,result FROM runs WHERE id<>? AND status NOT IN ('confirmed','visible')",(exclude,)).fetchall()
        for row in rows:
            p=json.loads(row['payload']);pending=json.loads(row['result']).get('send_intent')
            same=row['connection_id']==connection_id or (p.get('platform')==c['platform'] and p.get('sender')==c['config'].get('handle') and p.get('conversation')==c['config'].get('conversation'))
            if pending and same:return True
        return False
    def recover_startup(self):
        with self.db() as db:
            ids=[r[0] for r in db.execute("SELECT id FROM runs WHERE status IN ('running','queued','stopping')")]
        for ident in ids:
            self.update_run(ident,'interrupted','App closed during this task. Check the outcome before continuing.')
            self.event(ident,'interrupted','Recovered an unfinished task; no automatic resubmission')
        with self.db() as db:db.execute("UPDATE connections SET status='not_checked',detail='Reconnect to inspect the current session' WHERE status IN ('opening','checking','login_open')")
        with self.db() as db:db.execute("UPDATE chat SET status='error',content='The app closed during this model request. Send your request again.' WHERE status='thinking'")
        with self.db() as db:db.execute("UPDATE platform_notes SET status='interrupted',detail='Learning was interrupted; saved notes were kept' WHERE status='learning'")

    def model_config(self,private=False):
        with self.db() as db:r=db.execute("SELECT value FROM settings WHERE key='model'").fetchone()
        value=json.loads(r[0]) if r else {'provider':'codex','model':'gpt-6.1-sol','secret':''}
        secret=value.pop('secret','');value['has_key']=bool(secret)
        if private:value['api_key']=unprotect(secret)
        return value
    def save_model(self,data):
        provider=data.get('provider');model=str(data.get('model','')).strip()
        if provider not in ('codex','openai') or not re.fullmatch(r'[A-Za-z0-9._:-]{1,100}',model):raise ValueError('Choose a provider and enter its model ID')
        old=self.model_config(private=True);key=data.get('api_key') or (old['api_key'] if old['provider']==provider else '')
        if provider=='openai' and (not isinstance(key,str) or not key or len(key)>1000):raise ValueError('Enter an OpenAI API key directly in this form')
        value={'provider':provider,'model':model,'secret':protect(key) if provider=='openai' else ''}
        with self.db() as db:db.execute("INSERT OR REPLACE INTO settings VALUES('model',?)",(json.dumps(value),))
        return self.model_config()
    def chat_add(self,role,content,status='done',proposal=None):
        ident=uuid.uuid4().hex
        with self.db() as db:db.execute('INSERT INTO chat VALUES(?,?,?,?,?,?)',(ident,role,content,status,json.dumps(proposal or {}),time.time()))
        return ident
    def chat_update(self,ident,content,status='done',proposal=None):
        with self.db() as db:db.execute('UPDATE chat SET content=?,status=?,proposal=? WHERE id=?',(content,status,json.dumps(proposal or {}),ident))
    def chat_messages(self):
        with self.db() as db:rows=db.execute('SELECT * FROM chat ORDER BY created DESC LIMIT 40').fetchall()
        return [{**dict(r),'proposal':json.loads(r['proposal'])} for r in reversed(rows)]
