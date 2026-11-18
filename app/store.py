import sqlite3, json, hashlib
from datetime import datetime, timezone
from app import config
from app.errors import BusinessError

def connect():
    config.DATA_DIR.mkdir(parents=True,exist_ok=True)
    # Autocommit mode: every write is a single atomic statement unless the
    # caller opens an explicit BEGIN ... COMMIT block (settlement adjustments).
    db=sqlite3.connect(config.DATA_DIR/"runs.sqlite", timeout=15, isolation_level=None)
    db.row_factory=sqlite3.Row
    db.execute("CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, digest TEXT NOT NULL, result TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 0, decision TEXT NOT NULL DEFAULT '待复核', created TEXT NOT NULL)")
    db.execute("CREATE TABLE IF NOT EXISTS settlements (id TEXT PRIMARY KEY, run_id TEXT NOT NULL UNIQUE, digest TEXT NOT NULL, rule_version TEXT NOT NULL, basis TEXT NOT NULL, recognized TEXT NOT NULL, recognized_total TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 0, latest_run_id TEXT, created TEXT NOT NULL, updated TEXT NOT NULL)")
    db.execute("CREATE TABLE IF NOT EXISTS settlement_adjustments (id TEXT PRIMARY KEY, settlement_id TEXT NOT NULL, idem TEXT NOT NULL, payload_digest TEXT NOT NULL, effective_at TEXT NOT NULL, before_rule_version TEXT NOT NULL, after_rule_version TEXT NOT NULL, source_run_id TEXT NOT NULL, source_digest TEXT NOT NULL, contract TEXT NOT NULL, before TEXT NOT NULL, after TEXT NOT NULL, lines TEXT NOT NULL, total_before TEXT NOT NULL, total_after TEXT NOT NULL, total_delta TEXT NOT NULL, direction TEXT NOT NULL, created TEXT NOT NULL)")
    db.execute("CREATE UNIQUE INDEX IF NOT EXISTS settlement_adjustment_idem ON settlement_adjustments(settlement_id, idem)")
    db.execute("CREATE INDEX IF NOT EXISTS settlement_adjustment_order ON settlement_adjustments(settlement_id, created)")
    db.execute("CREATE INDEX IF NOT EXISTS settlement_adjustment_source ON settlement_adjustments(source_run_id)")
    return db

def now():
    return datetime.now(timezone.utc).isoformat()

def save(text, result):
    digest=hashlib.sha256((config.RULE_VERSION+"\n"+text).encode()).hexdigest()
    ident=digest[:16]
    with connect() as db:
        db.execute("INSERT OR IGNORE INTO runs(id,digest,result,created) VALUES(?,?,?,?)",(ident,digest,json.dumps(dict(result,rule_version=config.RULE_VERSION),ensure_ascii=False),now()))
    return get(ident)

def get(ident):
    with connect() as db:
        row=db.execute("SELECT * FROM runs WHERE id=?",(ident,)).fetchone()
    if row is None: raise BusinessError("NOT_FOUND","批次不存在",404)
    item=dict(row);item["result"]=json.loads(item["result"]);item["rule_version"]=item["result"].get("rule_version","2026.11.18.1")
    return item

def history():
    with connect() as db:
        return [dict(r) for r in db.execute("SELECT id,revision,decision,created FROM runs ORDER BY created DESC LIMIT 40")]

def review(ident,revision,decision):
    if not isinstance(decision,str) or decision not in {"通过","退回"}: raise BusinessError("DECISION","复核决定无效")
    current=get(ident)
    if decision=="通过" and current["result"].get("blocked"):
        raise BusinessError("BLOCKED","存在阻断项，请先修正数据",409)
    with connect() as db:
        changed=db.execute("UPDATE runs SET revision=revision+1,decision=? WHERE id=? AND revision=?",(decision,ident,revision)).rowcount
        if not changed: raise BusinessError("STALE","版本已更新，请刷新",409)
    return get(ident)
