"""Local durable intents plus crash-released, per-record OS locks.

This serializes cooperating workers on one machine. It is not a distributed
lock and cannot lock a human or another integration out of the CRM.
"""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time
from contextlib import contextmanager

class Busy(RuntimeError):pass
class ContractMismatch(RuntimeError):pass

def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':')).encode()).hexdigest()

class Journal:
    def __init__(self,path):
        self.path=Path(path).resolve();self.path.parent.mkdir(parents=True,exist_ok=True)
        with self.connect() as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS operations(
              operation_id TEXT PRIMARY KEY, contract_hash TEXT NOT NULL, contract TEXT NOT NULL,
              phase TEXT NOT NULL, state TEXT NOT NULL, updated REAL NOT NULL);
              CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY AUTOINCREMENT,
              operation_id TEXT NOT NULL, event TEXT NOT NULL, data TEXT NOT NULL, created REAL NOT NULL);''')

    def connect(self):
        db=sqlite3.connect(self.path,timeout=10)
        db.execute('PRAGMA journal_mode=WAL');db.execute('PRAGMA synchronous=FULL')
        return db

    def ensure(self,contract):
        ident=contract['operation_id'];hashed=digest(contract)
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT contract_hash FROM operations WHERE operation_id=?',(ident,)).fetchone()
            if row and row[0]!=hashed:raise ContractMismatch('Operation ID already belongs to a different immutable contract')
            if not row:db.execute('INSERT INTO operations VALUES(?,?,?,?,?,?)',(ident,hashed,json.dumps(contract),'new','{}',time.time()))
        return self.load(ident)

    def load(self,ident):
        with self.connect() as db:
            row=db.execute('SELECT phase,state FROM operations WHERE operation_id=?',(ident,)).fetchone()
        if not row:raise KeyError(ident)
        return {'phase':row[0],**json.loads(row[1])}

    def record(self,ident,event,data=None,phase=None,**state):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            old_phase,raw=db.execute('SELECT phase,state FROM operations WHERE operation_id=?',(ident,)).fetchone()
            merged={**json.loads(raw),**state}
            # A conflict can change the presentation phase to blocked without
            # resolving an earlier submission. Keep that fence independently.
            if phase=='commit_intent':merged['submission_pending']=True
            elif phase=='confirmed':merged['submission_pending']=False
            db.execute('UPDATE operations SET phase=?,state=?,updated=? WHERE operation_id=?',(phase or old_phase,json.dumps(merged),time.time(),ident))
            db.execute('INSERT INTO events(operation_id,event,data,created) VALUES(?,?,?,?)',(ident,event,json.dumps(data or {}),time.time()))

    def events(self,ident):
        with self.connect() as db:
            return [{'event':e,'data':json.loads(d),'created':t} for e,d,t in db.execute('SELECT event,data,created FROM events WHERE operation_id=? ORDER BY id',(ident,))]

    def pending_for_identity(self,contract):
        # One journal per deployment/tenant. A different operation ID must not
        # bypass an unresolved submission against the same business identity.
        with self.connect() as db:
            rows=db.execute("SELECT operation_id,contract,phase,state FROM operations WHERE operation_id<>?",(contract['operation_id'],)).fetchall()
        return [ident for ident,raw,phase,state in rows if (phase in ('commit_intent','uncertain') or json.loads(state).get('submission_pending')) and (lambda c:c['name']==contract['name'] and c.get('origin',contract.get('origin'))==contract.get('origin'))(json.loads(raw))]

    @contextmanager
    def resource_lock(self,key):
        folder=self.path.parent/'locks';folder.mkdir(exist_ok=True)
        file=folder/(hashlib.sha256(key.encode()).hexdigest()+'.lock')
        f=open(file,'a+b');f.seek(0,2)
        if f.tell()==0:f.write(b'0');f.flush()
        f.seek(0);locked=False
        try:
            try:
                if os.name=='nt':
                    import msvcrt
                    msvcrt.locking(f.fileno(),msvcrt.LK_NBLCK,1)
                else:
                    import fcntl
                    fcntl.flock(f.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
                locked=True
            except OSError as exc:raise Busy('Another local worker owns this record') from exc
            yield
        finally:
            if locked:
                f.seek(0)
                if os.name=='nt':
                    import msvcrt
                    msvcrt.locking(f.fileno(),msvcrt.LK_UNLCK,1)
                else:
                    import fcntl
                    fcntl.flock(f.fileno(),fcntl.LOCK_UN)
            f.close()
