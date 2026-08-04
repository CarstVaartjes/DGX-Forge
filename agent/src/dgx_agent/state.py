"""Fenced, durable local attempt state."""
from __future__ import annotations
from dataclasses import dataclass
from datetime import UTC, datetime
import json, os
from pathlib import Path
import sqlite3, stat
from dgx_agent_protocol import AgentClaim, AgentProgress, AgentProtocolError, AgentResult, canonical_message

_DB = "agent-state.sqlite3"; _VERSION = 1; _BUSY = 5000
class AgentStateError(RuntimeError): pass
class AgentStateConflict(AgentStateError): pass
@dataclass(frozen=True)
class AgentAttemptRecord:
    claim: AgentClaim; fence: str; state: str; progress_sequence: int; progress: AgentProgress|None; result: AgentResult|None
    created_at: str; updated_at: str; finished_at: str|None; acknowledged_at: str|None
    canonical_claim: bytes; canonical_progress: bytes|None; canonical_result: bytes|None

class AgentStateStore:
    def __init__(self, root: Path) -> None:
        self._root = Path(root); self._database = self._root / _DB; _root_ready(self._root); self._initialize()
    def begin(self, claim: AgentClaim) -> AgentAttemptRecord:
        raw = _canon(claim, AgentClaim)
        con = self._connection()
        try:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute("SELECT * FROM attempts WHERE node_id=? AND job_id=? AND operation_id=? AND attempt=?", _id(claim)).fetchone()
            if row:
                record = _record(row)
                if record.fence == claim.fence and record.canonical_claim == raw: con.commit(); return record
                raise AgentStateConflict("attempt conflicts with persisted state")
            if con.execute("SELECT 1 FROM attempts WHERE fence=?", (claim.fence,)).fetchone(): raise AgentStateConflict("fence was already used")
            highest = con.execute("SELECT MAX(attempt) FROM attempts WHERE node_id=? AND job_id=? AND operation_id=?", _id(claim)[:3]).fetchone()[0]
            if highest is not None and claim.attempt <= highest: raise AgentStateConflict("attempt is stale")
            if con.execute("SELECT 1 FROM attempts WHERE state='active' OR acknowledged_at IS NULL").fetchone(): raise AgentStateConflict("an attempt delivery is unresolved")
            now = _now(); con.execute("INSERT INTO attempts VALUES(?,?,?,?,?,'active',?,0,NULL,NULL,?,?,NULL,NULL)", (*_id(claim), claim.fence, raw, now, now)); row=con.execute("SELECT * FROM attempts WHERE node_id=? AND job_id=? AND operation_id=? AND attempt=?", _id(claim)).fetchone(); con.commit(); return _record(row)
        except AgentStateConflict: raise
        except (sqlite3.Error, OSError) as e: raise AgentStateError("state operation failed") from e
        finally: con.close()
    def heartbeat(self, progress: AgentProgress) -> AgentAttemptRecord:
        raw=_canon(progress, AgentProgress); con=self._connection()
        try:
            con.execute("BEGIN IMMEDIATE"); row=_match(con,progress); record=_record(row)
            if record.state != 'active': raise AgentStateConflict("attempt is not active")
            now=_now(); n=con.execute("UPDATE attempts SET progress_sequence=progress_sequence+1,progress_json=?,updated_at=? WHERE node_id=? AND job_id=? AND operation_id=? AND attempt=? AND fence=? AND state='active'",(raw,now,*_id(progress),progress.fence)).rowcount
            if n != 1: raise AgentStateConflict("attempt is no longer active")
            row=_match(con,progress); con.commit(); return _record(row)
        except AgentStateConflict: raise
        except (sqlite3.Error,OSError) as e: raise AgentStateError("state operation failed") from e
        finally: con.close()
    def finish(self, result: AgentResult) -> AgentAttemptRecord:
        raw=_canon(result, AgentResult); con=self._connection()
        try:
            con.execute("BEGIN IMMEDIATE"); row=_match(con,result); record=_record(row)
            if record.state != 'active':
                if record.canonical_result == raw: con.commit(); return record
                raise AgentStateConflict("terminal result conflicts with persisted state")
            now=_now(); con.execute("UPDATE attempts SET state=?,result_json=?,finished_at=?,updated_at=? WHERE node_id=? AND job_id=? AND operation_id=? AND attempt=? AND fence=? AND state='active'",(result.state,raw,now,now,*_id(result),result.fence)); row=_match(con,result); con.commit(); return _record(row)
        except AgentStateConflict: raise
        except (sqlite3.Error,OSError) as e: raise AgentStateError("state operation failed") from e
        finally: con.close()
    def acknowledge(self, result: AgentResult) -> AgentAttemptRecord:
        raw=_canon(result, AgentResult); con=self._connection()
        try:
            con.execute("BEGIN IMMEDIATE"); row=_match(con,result); record=_record(row)
            if record.state == 'active' or record.canonical_result != raw: raise AgentStateConflict("terminal result conflicts with persisted state")
            if record.acknowledged_at is None:
                now=_now(); con.execute("UPDATE attempts SET acknowledged_at=?,updated_at=? WHERE node_id=? AND job_id=? AND operation_id=? AND attempt=? AND fence=? AND acknowledged_at IS NULL",(now,now,*_id(result),result.fence)); row=_match(con,result)
            con.commit(); return _record(row)
        except AgentStateConflict: raise
        except (sqlite3.Error,OSError) as e: raise AgentStateError("state operation failed") from e
        finally: con.close()
    def recover_active(self) -> AgentAttemptRecord|None: return self._recover("state='active'")
    def recover_pending(self) -> AgentAttemptRecord|None: return self._recover("state != 'active' AND acknowledged_at IS NULL")
    def _recover(self, where: str) -> AgentAttemptRecord|None:
        con=self._connection()
        try:
            rows=con.execute(f"SELECT * FROM attempts WHERE {where}").fetchall()
            if len(rows)>1: raise AgentStateError("state has multiple unresolved attempts")
            return None if not rows else _record(rows[0])
        except sqlite3.Error as e: raise AgentStateError("state operation failed") from e
        finally: con.close()
    def _initialize(self) -> None:
        _db_ready(self._database, create=True); con=_connect(self._database)
        try:
            con.execute("BEGIN IMMEDIATE")
            version=con.execute("PRAGMA user_version").fetchone()[0]
            exists=con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='attempts'").fetchone()
            if version==0 and not exists:
                con.execute("""CREATE TABLE attempts(node_id TEXT NOT NULL,job_id TEXT NOT NULL,operation_id TEXT NOT NULL,attempt INTEGER NOT NULL,fence TEXT NOT NULL UNIQUE,state TEXT NOT NULL CHECK(state IN ('active','succeeded','failed','waiting-for-operator')),claim_json BLOB NOT NULL,progress_sequence INTEGER NOT NULL CHECK(progress_sequence>=0),progress_json BLOB,result_json BLOB,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,finished_at TEXT,acknowledged_at TEXT,PRIMARY KEY(node_id,job_id,operation_id,attempt),CHECK((progress_sequence=0 AND progress_json IS NULL) OR (progress_sequence>0 AND progress_json IS NOT NULL)),CHECK((state='active' AND result_json IS NULL AND finished_at IS NULL AND acknowledged_at IS NULL) OR (state!='active' AND result_json IS NOT NULL AND finished_at IS NOT NULL)))""")
                con.execute("CREATE UNIQUE INDEX one_unresolved ON attempts((1)) WHERE state='active' OR acknowledged_at IS NULL")
                con.execute("PRAGMA user_version=1")
            elif version != _VERSION: raise AgentStateError("unsupported state schema")
            _schema(con); con.commit()
        except AgentStateError: raise
        except sqlite3.Error as e: raise AgentStateError("state schema is invalid") from e
        finally: con.close()
    def _connection(self) -> sqlite3.Connection:
        _root_ready(self._root); _db_ready(self._database); con=_connect(self._database)
        try:
            if con.execute("PRAGMA user_version").fetchone()[0] != _VERSION: raise AgentStateError("unsupported state schema")
            _schema(con); return con
        except Exception: con.close(); raise

def _connect(path: Path) -> sqlite3.Connection:
    con: sqlite3.Connection | None = None
    try:
        con=sqlite3.connect(path,timeout=_BUSY/1000,isolation_level=None); con.row_factory=sqlite3.Row
        con.execute(f"PRAGMA busy_timeout={_BUSY}"); con.execute("PRAGMA journal_mode=WAL"); con.execute("PRAGMA foreign_keys=ON"); con.execute("PRAGMA synchronous=FULL"); return con
    except sqlite3.Error as e:
        if con is not None: con.close()
        raise AgentStateError("state database cannot be opened") from e
def _schema(con: sqlite3.Connection) -> None:
    required={'node_id','job_id','operation_id','attempt','fence','state','claim_json','progress_sequence','progress_json','result_json','created_at','updated_at','finished_at','acknowledged_at'}
    if {r[1] for r in con.execute("PRAGMA table_info(attempts)")} != required: raise AgentStateError("state schema is invalid")
    table = con.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='attempts'").fetchone()[0].lower()
    index = con.execute("SELECT sql FROM sqlite_master WHERE type='index' AND name='one_unresolved'").fetchone()
    if not index or "fence text not null unique" not in table or "check((progress_sequence=0" not in table or "check((state='active'" not in table or "where state='active' or acknowledged_at is null" not in index[0].lower(): raise AgentStateError("state schema is invalid")
def _root_ready(root: Path) -> None:
    if not root.is_absolute(): raise AgentStateError("state root must be absolute")
    descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for part in root.parts[1:]:
            try:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=descriptor)
            except FileNotFoundError:
                try: os.mkdir(part, 0o700, dir_fd=descriptor)
                except FileExistsError: pass
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=descriptor)
            os.close(descriptor); descriptor = child
        meta = os.fstat(descriptor)
        if meta.st_uid != os.geteuid() or stat.S_IMODE(meta.st_mode) != 0o700: raise AgentStateError("state root is unsafe")
    except OSError as e: raise AgentStateError("state root unavailable") from e
    finally: os.close(descriptor)
def _db_ready(path: Path, create: bool=False) -> None:
    root = path.parent
    directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC
        if create: flags |= os.O_CREAT
        try: descriptor = os.open(path.name, flags, 0o600, dir_fd=directory)
        except OSError as e: raise AgentStateError("state database unavailable") from e
        try:
            meta = os.fstat(descriptor)
            if not stat.S_ISREG(meta.st_mode) or meta.st_uid != os.geteuid() or stat.S_IMODE(meta.st_mode) != 0o600: raise AgentStateError("state database is unsafe")
        finally: os.close(descriptor)
    finally: os.close(directory)
def _id(m: AgentClaim|AgentProgress|AgentResult)->tuple[str,str,str,int]: return m.node_id,m.job_id,m.operation_id,m.attempt
def _match(con:sqlite3.Connection,m:AgentProgress|AgentResult)->sqlite3.Row:
    row=con.execute("SELECT * FROM attempts WHERE node_id=? AND job_id=? AND operation_id=? AND attempt=? AND fence=?",(*_id(m),m.fence)).fetchone()
    if row is None: raise AgentStateConflict("attempt identity or fence does not match")
    return row
def _canon(value: object, typ:type)->bytes:
    if not isinstance(value,typ): raise AgentStateError("message is invalid")
    return canonical_message(value)
def _time(value:object)->datetime:
    if not isinstance(value,str): raise AgentStateError("stored timestamp is invalid")
    try: parsed=datetime.fromisoformat(value)
    except ValueError as e: raise AgentStateError("stored timestamp is invalid") from e
    if parsed.tzinfo is None or parsed.utcoffset()!=UTC.utcoffset(parsed) or parsed.isoformat()!=value: raise AgentStateError("stored timestamp is invalid")
    return parsed
def _record(row:sqlite3.Row)->AgentAttemptRecord:
    try:
        cb=bytes(row['claim_json']); claim=AgentClaim.parse(json.loads(cb)); pb=None if row['progress_json'] is None else bytes(row['progress_json']); progress=None if pb is None else AgentProgress.parse(json.loads(pb)); rb=None if row['result_json'] is None else bytes(row['result_json']); result=None if rb is None else AgentResult.parse(json.loads(rb))
        if canonical_message(claim)!=cb or (progress and canonical_message(progress)!=pb) or (result and canonical_message(result)!=rb): raise AgentStateError("stored record is not canonical")
        if not all((_id(claim)==tuple(row[k] for k in ('node_id','job_id','operation_id','attempt')), claim.fence==row['fence'], progress is None or (_id(progress)==_id(claim) and progress.fence==claim.fence), result is None or (_id(result)==_id(claim) and result.fence==claim.fence))): raise AgentStateError("stored identity is invalid")
        created,updated=_time(row['created_at']),_time(row['updated_at']); finished=None if row['finished_at'] is None else _time(row['finished_at']); ack=None if row['acknowledged_at'] is None else _time(row['acknowledged_at'])
        seq=row['progress_sequence']; state=row['state']
        if not isinstance(seq,int) or isinstance(seq,bool) or seq<0 or (seq==0)!=(progress is None) or created>updated or (finished and (finished<created or finished>updated)) or (ack and (not finished or ack<finished or ack>updated)): raise AgentStateError("stored record coherence is invalid")
        if state=='active' and (result or finished or ack) or state!='active' and (result is None or finished is None or result.state!=state): raise AgentStateError("stored record coherence is invalid")
        return AgentAttemptRecord(claim,row['fence'],state,seq,progress,result,row['created_at'],row['updated_at'],row['finished_at'],row['acknowledged_at'],cb,pb,rb)
    except (AgentProtocolError,ValueError,TypeError,KeyError,json.JSONDecodeError) as e: raise AgentStateError("stored state is invalid") from e
def _now()->str: return datetime.now(UTC).isoformat()
