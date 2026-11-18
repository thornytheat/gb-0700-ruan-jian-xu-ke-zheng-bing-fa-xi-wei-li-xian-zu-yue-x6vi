"""结算与追溯调整。

已登记的结算是一笔已确认的事实：原结算依据（合同上限、原始超用剖面、
金额与复核版本）写入后永不改写。采购规则追溯更新后，把新合同重算出的
超用与“当前已确认剖面”按生效点比较，只追加一笔与原结算关联的补收或
冲减，而不是重开原结算。

关键约束：
- 当前许可结果始终取自最新批次（新合同），不沿用旧上限少报超用。
- 调整只改 [生效点, ∞) 段；生效点之前保持已确认值，同一段席位不会重复收费。
- 每次调整在单个 SQLite 事务内写入；中断整体回滚，旧版仍可对账。
- 幂等键相同的重试返回已有调整，不新增行、不新增金额。
"""
import hashlib, json
from datetime import datetime, timezone
from decimal import Decimal
from fractions import Fraction
from app.errors import BusinessError
from app import store
from app.report import plain
from app.timeops import instant

HOUR=3600
# 结算金额以 1e-6 席位小时为最小单位。区间秒数用 Fraction 精确累加，
# 只在金额边界量化一次，保证“原确认 + 调整 = 现确认”、产品行合计=总额
# 都能逐位复算。
QUANTUM=Decimal("0.000001")

def _money(value):
    exact=Fraction(value)
    return (Decimal(exact.numerator)/Decimal(exact.denominator)).quantize(QUANTUM)

def _dec(value):
    return Decimal(str(value))

def _hours(a,b,excess):
    return Fraction(b-a)*int(excess)/HOUR

def _segments_from_usage(usage):
    """按产品拆出超用剖面；零超用区间不进入结算剖面。"""
    out={}
    for row in usage or []:
        excess=int(row.get("excess") or 0)
        if excess<=0: continue
        product=row.get("product","")
        a,b=_dec(row["a"]),_dec(row["b"])
        if b<=a: continue
        out.setdefault(product,[]).append({"a":a,"b":b,"excess":excess})
    for rows in out.values():
        rows.sort(key=lambda r:(r["a"],r["b"]))
    return out

def _hours_of(rows):
    return sum((_hours(r["a"],r["b"],r["excess"]) for r in rows),Fraction(0))

def _profile(segments):
    products={}
    for product,rows in segments.items():
        products[product]={"seat_hours":_money(_hours_of(rows)),
                           "peak_excess":max((r["excess"] for r in rows),default=0)}
    total=sum((p["seat_hours"] for p in products.values()),Decimal(0)).quantize(QUANTUM)
    return {"products":products,"total":total}

def _clip(rows,start):
    out=[]
    for r in rows:
        a=max(r["a"],start)
        if r["b"]>a: out.append({"a":a,"b":r["b"],"excess":r["excess"]})
    return out

def _merge_after(old,new,start):
    """保留生效点之前的确认剖面，生效点之后整体换成新合同剖面。"""
    merged={}
    for product,rows in old.items():
        kept=[dict(r) for r in rows if r["b"]<=start]
        kept+=[{"a":r["a"],"b":start,"excess":r["excess"]} for r in rows if r["a"]<start<r["b"]]
        if kept: merged[product]=kept
    for product,rows in new.items():
        rows=_clip(rows,start)
        if not rows: continue
        merged.setdefault(product,[]).extend(rows)
    for rows in merged.values():
        rows.sort(key=lambda r:(r["a"],r["b"]))
    return merged

def _active_excess(rows,t):
    for r in rows:
        if r["a"]<=t<r["b"]: return r["excess"]
    return 0

def _delta_lines(before,after,start,targets):
    """按区间对比生效点之后的确认值与新合同值。

    targets 给出每个产品应有的净差额（已量化）。逐行金额先量化，
    残差并入该产品最后一行，确保逐行合计与产品差额、调整总额完全一致。
    """
    lines=[]
    for product in sorted(set(before)|set(after)):
        was_rows=_clip(before.get(product,[]),start)
        now_rows=_clip(after.get(product,[]),start)
        cuts=sorted({start}|{r[k] for r in was_rows for k in ("a","b")}|{r[k] for r in now_rows for k in ("a","b")})
        product_lines=[]
        for a,b in zip(cuts,cuts[1:]):
            was=_active_excess(was_rows,a);now=_active_excess(now_rows,a)
            if was==now: continue
            hours=Fraction(b-a)/HOUR
            delta=_money(hours*now-hours*was)
            product_lines.append({"product":product,"a":plain(a),"b":plain(b),
                                  "before_excess":was,"after_excess":now,
                                  "hours":plain(_money(hours)),"delta_seat_hours":delta,
                                  "direction":"补收" if delta>0 else "冲减" if delta<0 else "无差异"})
        if product_lines:
            target=targets.get(product,Decimal(0))
            residual=target-sum((l["delta_seat_hours"] for l in product_lines),Decimal(0))
            last=product_lines[-1]
            last["delta_seat_hours"]+=residual
            last["direction"]="补收" if last["delta_seat_hours"]>0 else "冲减" if last["delta_seat_hours"]<0 else "无差异"
        lines.extend(product_lines)
    return lines

def _product_summary(before,after):
    out=[]
    for product in sorted(set(before["products"])|set(after["products"])):
        b=before["products"].get(product,{}).get("seat_hours",Decimal(0))
        a=after["products"].get(product,{}).get("seat_hours",Decimal(0))
        delta=a-b
        out.append({"product":product,"before":b,"after":a,"delta":delta,
                    "direction":"补收" if delta>0 else "冲减" if delta<0 else "无差异"})
    return out

def _contract(result):
    return [{"lease_id":r.get("lease_id"),"product":r.get("canonical",r.get("product")),
             "start":r.get("start"),"end":r.get("end"),
             "version":r.get("rev"),"seats":r.get("quantity")}
            for r in result.get("normalized",[]) if r.get("kind")=="capacity"]

def _settlement_id(run_id):
    return "stl"+hashlib.sha256(("settlement\n"+run_id).encode()).hexdigest()[:16]

def _adjustment_id(settlement_id,idem):
    return "adj"+hashlib.sha256((settlement_id+"\n"+idem).encode()).hexdigest()[:16]

def _canonical_instant(value):
    try:
        return datetime.fromtimestamp(float(instant(value)),timezone.utc).isoformat()
    except BusinessError: raise
    except (TypeError,ValueError,OverflowError):
        raise BusinessError("EFFECTIVE_AT","生效点格式错误")

def _fail(message,code="SETTLEMENT",status=422):
    raise BusinessError(code,message,status)

def _fault_point(stage):
    """测试用故障注入钩子；生产环境为空操作。

    调整在写入调整行与更新结算之间调用它，用于验证中断时事务整体回滚，
    旧版已确认结果仍完整可对账。
    """
    return None

def _stored_profile(raw):
    return {p:[{"a":_dec(r["a"]),"b":_dec(r["b"]),"excess":int(r["excess"])} for r in rows]
            for p,rows in json.loads(raw).items()}

def _materialize_adjustment(row):
    item=dict(row)
    for key in ("lines","before","after","contract"):
        try: item[key]=json.loads(item[key])
        except (TypeError,ValueError): pass
    return item

def _settle_payload(run):
    segments=_segments_from_usage(run["result"].get("usage",[]))
    profile=_profile(segments)
    return segments,profile,{
        "run_id":run["id"],"digest":run["digest"],"rule_version":run["rule_version"],
        "review":{"revision":run.get("revision",0),"decision":run.get("decision","待复核")},
        "products":sorted(_run_products(run["result"])),
        "contract":plain(_contract(run["result"])),
        "original":plain(profile),
        "usage":plain([{"product":r.get("product"),"a":r.get("a"),"b":r.get("b"),
                        "concurrent":r.get("concurrent"),"capacity":r.get("capacity"),
                        "excess":r.get("excess"),"status":r.get("status"),
                        "offenders":r.get("offenders",[])} for r in run["result"].get("usage",[])])}

def settle(run):
    """以某批次的许可结果登记原结算；同批次重复登记返回原结算。

    已被其他结算当作调整来源的批次不能再单独登记，否则同一段席位会同时
    计入两笔结算而重复收费。
    """
    ident=_settlement_id(run["id"])
    with store.connect() as db:
        row=db.execute("SELECT * FROM settlements WHERE run_id=?",(run["id"],)).fetchone()
        if row is not None: return materialize(row)
        linked=db.execute("SELECT settlement_id FROM settlement_adjustments WHERE source_run_id=? LIMIT 1",(run["id"],)).fetchone()
        if linked is not None:
            raise BusinessError("ALREADY_RECOGNIZED","该批次已作为其他结算的调整来源，不能重复登记",409)
        segments,profile,basis=_settle_payload(run)
        stamp=store.now()
        db.execute("INSERT OR IGNORE INTO settlements(id,run_id,digest,rule_version,basis,recognized,recognized_total,revision,latest_run_id,created,updated) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                   (ident,run["id"],run["digest"],run["rule_version"],
                    json.dumps(plain(basis),ensure_ascii=False),
                    json.dumps(plain(segments),ensure_ascii=False),
                    str(profile["total"]),0,run["id"],stamp,stamp))
        row=db.execute("SELECT * FROM settlements WHERE run_id=?",(run["id"],)).fetchone()
    return materialize(row)

def settlement_for_run(run_id):
    with store.connect() as db:
        row=db.execute("SELECT * FROM settlements WHERE run_id=?",(run_id,)).fetchone()
        if row is None:
            row=db.execute("SELECT s.* FROM settlements s JOIN settlement_adjustments a ON a.settlement_id=s.id WHERE a.source_run_id=? ORDER BY a.rowid DESC LIMIT 1",(run_id,)).fetchone()
    return materialize(row) if row else None

def reference_for(run):
    """导出/展示用的参考结算：优先产品范围相交的最新一笔。"""
    products=_run_products(run["result"])
    with store.connect() as db:
        rows=db.execute("SELECT * FROM settlements ORDER BY created DESC, rowid DESC").fetchall()
    fallback=None
    for row in rows:
        if run["id"]==row["run_id"]: return materialize(row)
        if products & _settlement_products(row):
            return materialize(row)
        if fallback is None: fallback=row
    return materialize(fallback) if fallback is not None else None

def listing():
    with store.connect() as db:
        rows=db.execute("SELECT id,run_id,digest,rule_version,recognized_total,revision,latest_run_id,created,updated FROM settlements ORDER BY created DESC").fetchall()
        counts={r["settlement_id"]:r["n"] for r in db.execute("SELECT settlement_id,COUNT(*) n FROM settlement_adjustments GROUP BY settlement_id")}
    return [dict(r,adjustments=counts.get(r["id"],0)) for r in rows]

def get(settlement_id):
    with store.connect() as db:
        row=db.execute("SELECT * FROM settlements WHERE id=?",(settlement_id,)).fetchone()
    if row is None: _fail("结算不存在","NOT_FOUND",404)
    return materialize(row)

def _run_products(result):
    products=set()
    for key in ("usage","purchase"):
        for row in result.get(key,[]) or []:
            if row.get("product"): products.add(row["product"])
    for row in result.get("normalized",[]) or []:
        if row.get("canonical"): products.add(row["canonical"])
        elif row.get("product"): products.add(row["product"])
    return products

def _settlement_products(item):
    """结算覆盖的产品范围：以登记时的产品为准，与当时是否有超用无关。"""
    basis=json.loads(item["basis"])
    products=set(basis.get("products",[]))
    if not products: products=set(basis.get("original",{}).get("products",{}))
    return products

def _current_run(db,item):
    """该结算产品范围内最新的一批许可结果。

    “当前许可结果”必须体现最新超用：取产品范围相交的最新批次，而不是
    只取最后一次被调整的批次，否则新合同批次算完但未追补时会少报。
    属于其他结算基准的批次不抢用；incorporated_run_id 保留最后一次纳入
    调整的批次，便于追溯差额。产品范围完整命中的批次优先，只有部分相交
    时退回部分匹配，避免跨产品误绑。
    """
    products=_settlement_products(item)
    owned={r["run_id"] for r in db.execute("SELECT run_id FROM settlements WHERE id<>?",(item["id"],))}
    rows=db.execute("SELECT * FROM runs ORDER BY created DESC, rowid DESC LIMIT 500").fetchall()
    candidate=None;partial=None
    for row in rows:
        if row["id"] in owned: continue
        result=json.loads(row["result"])
        seen=_run_products(result)
        if not products or products<=seen:
            candidate=dict(row,result=result);break
        if partial is None and products & seen:
            partial=dict(row,result=result)
    chosen=candidate or partial
    if chosen is None:
        fallback=db.execute("SELECT * FROM runs WHERE id=?",(item["latest_run_id"] or item["run_id"],)).fetchone()
        chosen=dict(fallback);chosen["result"]=json.loads(chosen["result"])
    return chosen

def materialize(row):
    item=dict(row)
    basis=json.loads(item["basis"])
    segments=_stored_profile(item["recognized"])
    profile=_profile(segments)
    with store.connect() as db:
        adjustments=[_materialize_adjustment(r) for r in
                     db.execute("SELECT * FROM settlement_adjustments WHERE settlement_id=? ORDER BY rowid",(item["id"],))]
        current_run=_current_run(db,item)
    current_rule=current_run["result"].get("rule_version",item["rule_version"])
    incorporated=item["latest_run_id"] or item["run_id"]
    result={"id":item["id"],"run_id":item["run_id"],"digest":item["digest"],"rule_version":item["rule_version"],
            "revision":item["revision"],"created":item["created"],"updated":item["updated"],
            "original":basis,
            "recognized":{"products":plain(profile["products"]),"total":plain(profile["total"]),"segments":plain(segments)},
            "current":{"run_id":current_run["id"],"digest":current_run["digest"],"rule_version":current_rule,
                       "decision":current_run["decision"],"review_revision":current_run["revision"],
                       "incorporated_run_id":incorporated,"pending":current_run["id"]!=incorporated,
                       "purchase":current_run["result"].get("purchase",[]),
                       "usage":current_run["result"].get("usage",[]),
                       "contract":plain(_contract(current_run["result"]))},
            "adjustments":adjustments}
    result["adjustment_total"]=plain(sum((_dec(a["total_delta"]) for a in adjustments),Decimal(0)))
    return result

def _prepare_adjustment(recognized,effective_at,run):
    canonical=_canonical_instant(effective_at.strip())
    effective=_dec(instant(canonical))
    before_segments=_stored_profile(recognized)
    new_segments=_segments_from_usage(run["result"].get("usage",[]))
    after_segments=_merge_after(before_segments,new_segments,effective)
    before_profile=_profile(before_segments);after_profile=_profile(after_segments)
    delta=after_profile["total"]-before_profile["total"]
    products=_product_summary(before_profile,after_profile)
    targets={p["product"]:p["delta"] for p in products}
    return {"effective_at":canonical,"effective":effective,"run":run,
            "before_segments":before_segments,"new_segments":new_segments,"after_segments":after_segments,
            "before_profile":before_profile,"after_profile":after_profile,
            "delta":delta,"direction":"补收" if delta>0 else "冲减" if delta<0 else "无差异",
            "lines":{"products":products,
                     "segments":_delta_lines(before_segments,new_segments,effective,targets)}}

def _require(settlement_id):
    with store.connect() as db:
        row=db.execute("SELECT * FROM settlements WHERE id=?",(settlement_id,)).fetchone()
    if row is None: _fail("结算不存在","NOT_FOUND",404)
    return dict(row)

def preview(settlement_id,effective_at,run):
    """只读试算：不写任何调整，也不改动已确认结算。"""
    if not isinstance(effective_at,str) or not effective_at.strip():
        _fail("生效点须为带时区的ISO时间","EFFECTIVE_AT")
    plan=_prepare_adjustment(_require(settlement_id)["recognized"],effective_at,run)
    return {"settlement":settlement_id,"source_run":run["id"],"source_digest":run["digest"],
            "source_rule_version":run["rule_version"],"effective_at":plan["effective_at"],
            "direction":plan["direction"],
            "total_before":plain(plan["before_profile"]["total"]),
            "total_after":plain(plan["after_profile"]["total"]),
            "total_delta":plain(plan["delta"]),"lines":plain(plan["lines"])}

def adjust(settlement_id,effective_at,run,idem=None):
    """按生效点追加一笔与原结算关联的补收/冲减。"""
    if not isinstance(effective_at,str) or not effective_at.strip():
        _fail("生效点须为带时区的ISO时间","EFFECTIVE_AT")
    canonical=_canonical_instant(effective_at.strip())
    if not (isinstance(idem,str) and idem.strip()):
        raw="\n".join([settlement_id,canonical,run["id"],run["digest"],run["rule_version"]])
        idem=hashlib.sha256(raw.encode()).hexdigest()
    else:
        idem=idem.strip()
    payload=json.dumps({"settlement":settlement_id,"run":run["id"],"digest":run["digest"],
                        "rule_version":run["rule_version"],"effective_at":canonical},
                       sort_keys=True,ensure_ascii=False)
    payload_digest=hashlib.sha256(payload.encode()).hexdigest()
    db=store.connect()
    try:
        db.execute("BEGIN IMMEDIATE")
        row=db.execute("SELECT * FROM settlements WHERE id=?",(settlement_id,)).fetchone()
        if row is None: _fail("结算不存在","NOT_FOUND",404)
        existing=db.execute("SELECT * FROM settlement_adjustments WHERE settlement_id=? AND idem=?",(settlement_id,idem)).fetchone()
        if existing is not None:
            if existing["payload_digest"]!=payload_digest:
                raise BusinessError("IDEMPOTENCY_CONFLICT","幂等键已用于另一笔调整",409)
            db.execute("ROLLBACK")
            return _materialize_adjustment(existing)
        plan=_prepare_adjustment(row["recognized"],canonical,run)
        stamp=store.now()
        adjustment_id=_adjustment_id(settlement_id,idem)
        db.execute("INSERT INTO settlement_adjustments(id,settlement_id,idem,payload_digest,effective_at,before_rule_version,after_rule_version,source_run_id,source_digest,contract,before,after,lines,total_before,total_after,total_delta,direction,created) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   (adjustment_id,settlement_id,idem,payload_digest,plan["effective_at"],row["rule_version"],run["rule_version"],
                    run["id"],run["digest"],json.dumps(plain(_contract(run["result"])),ensure_ascii=False),
                    json.dumps(plain({"products":plan["before_profile"]["products"],"segments":plain(plan["before_segments"])}),ensure_ascii=False),
                    json.dumps(plain({"products":plan["after_profile"]["products"],"segments":plain(plan["after_segments"])}),ensure_ascii=False),
                    json.dumps(plain(plan["lines"]),ensure_ascii=False),
                    str(plan["before_profile"]["total"]),str(plan["after_profile"]["total"]),
                    str(plan["delta"]),plan["direction"],stamp))
        _fault_point("after-ledger-insert")
        changed=db.execute("UPDATE settlements SET recognized=?,recognized_total=?,revision=revision+1,latest_run_id=?,updated=? WHERE id=? AND revision=?",
                           (json.dumps(plain(plan["after_segments"]),ensure_ascii=False),str(plan["after_profile"]["total"]),
                            run["id"],stamp,settlement_id,row["revision"])).rowcount
        if not changed: raise BusinessError("STALE","结算已被更新，请刷新后重试",409)
        db.execute("COMMIT")
    except Exception:
        try: db.execute("ROLLBACK")
        except Exception: pass
        raise
    finally:
        db.close()
    with store.connect() as conn:
        return _materialize_adjustment(conn.execute("SELECT * FROM settlement_adjustments WHERE id=?",(adjustment_id,)).fetchone())
