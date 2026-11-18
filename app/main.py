from fastapi import FastAPI, Body, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from app.errors import BusinessError, object_payload
from app.pipeline import run, sample_text
from app import store, settlement
from app.exporter import evidence_csv
from app.timeops import display_full
from app.numbers import display_number
from pathlib import Path
import importlib
app=FastAPI(title='软件许可证并发席位与离线租约')
app.mount("/static",StaticFiles(directory=Path(__file__).parent/"static"),name="static")
templates=Jinja2Templates(directory=Path(__file__).parent/"templates")
templates.env.filters["utc_instant"]=display_full
templates.env.filters["display_number"]=display_number
MODULES=['会话接收', '产品映射', '租约裁决', '并发扫描', '超用归属', '采购对照', '补传审计']
for n,slug in enumerate(['sessions', 'products', 'leases', 'concurrency', 'excess', 'purchase', 'audit'],1):
    app.include_router(importlib.import_module(f"app.modules.m{n}_{slug}.handler").router)
@app.exception_handler(BusinessError)
async def domain_error(request,exc):
    return JSONResponse({"error":exc.code,"message":str(exc)},status_code=exc.status)
@app.get("/health")
def health(): return {"status":"ok"}
@app.get("/",response_class=HTMLResponse)
def home(request:Request): return page(request,1)
@app.get("/module/{module}",response_class=HTMLResponse)
def page(request:Request,module:int):
    if module not in range(1,8): raise BusinessError("MODULE","模块不存在",404)
    ident=request.query_params.get("run_id")
    item=store.get(ident) if ident else store.save(sample_text(),run(sample_text()))
    record=settlement.settlement_for_run(item["id"])
    link="linked"
    if record is None:
        record=settlement.reference_for(item);link="reference" if record else "none"
    return templates.TemplateResponse(request=request,name="workbench.html",context={"title":'软件许可证并发席位与离线租约',"modules":MODULES,"active":module,"run":item,"report":item["result"]["reports"][module-1],"settlement":record,"settlement_link":link})
@app.post("/api/run")
def calculate(payload:object=Body(default=None)):
    payload=object_payload(payload)
    text=payload.get("text",sample_text())
    if not isinstance(text,str): raise BusinessError("TEXT","输入须为文本")
    return store.save(text,run(text))
@app.get("/api/runs")
def list_runs(): return store.history()
@app.get("/api/runs/{ident}")
def run_detail(ident:str): return store.get(ident)
@app.post("/api/review")
def review(payload:object=Body(...)):
    payload=object_payload(payload)
    if not isinstance(payload.get("id"),str) or not isinstance(payload.get("decision"),str): raise BusinessError("REVIEW","复核编号与决定须为文本")
    if type(payload.get("revision")) is not int: raise BusinessError("REVISION","版本须为整数")
    return store.review(str(payload.get("id","")),payload["revision"],payload.get("decision"))
@app.get("/api/export/{ident}")
def export(ident:str):
    return Response(evidence_csv(store.get(ident)),media_type="text/csv",headers={"Content-Disposition":"attachment; filename=evidence.csv"})
@app.get("/api/settlements")
def settlements(): return {"settlements":settlement.listing()}
@app.get("/api/settlements/{ident}")
def settlement_detail(ident:str): return settlement.get(ident)
@app.post("/api/settlements")
def create_settlement(payload:object=Body(...)):
    item=store.get(_run_id(payload))
    return settlement.settle(item)
@app.post("/api/settlements/{ident}/adjustments")
def create_adjustment(ident:str,payload:object=Body(...)):
    payload=object_payload(payload)
    if not isinstance(payload.get("effective_at"),str): raise BusinessError("EFFECTIVE_AT","生效点须为带时区的ISO时间")
    run_id=payload.get("run_id")
    if run_id is not None and not isinstance(run_id,str): raise BusinessError("RUN_ID","批次编号须为文本")
    source=store.get(run_id) if run_id else store.get(settlement.get(ident)["current"]["run_id"])
    idem=payload.get("idempotency_key",payload.get("idem"))
    if idem is not None and not isinstance(idem,str): raise BusinessError("IDEMPOTENCY","幂等键须为文本")
    return settlement.adjust(ident,payload["effective_at"],source,idem)
@app.get("/api/export/settlement/{ident}")
def export_settlement(ident:str):
    from app.exporter import settlement_csv
    return Response(settlement_csv(settlement.get(ident)),media_type="text/csv",headers={"Content-Disposition":"attachment; filename=settlement-evidence.csv"})
@app.post("/api/settlements/{ident}/adjustments/preview")
def preview_adjustment(ident:str,payload:object=Body(...)):
    payload=object_payload(payload)
    if not isinstance(payload.get("effective_at"),str): raise BusinessError("EFFECTIVE_AT","生效点须为带时区的ISO时间")
    run_id=payload.get("run_id")
    if run_id is not None and not isinstance(run_id,str): raise BusinessError("RUN_ID","批次编号须为文本")
    source=store.get(run_id) if run_id else store.get(settlement.get(ident)["current"]["run_id"])
    return settlement.preview(ident,payload["effective_at"],source)

def _run_id(payload):
    payload=object_payload(payload)
    ident=payload.get("run_id",payload.get("id"))
    if not isinstance(ident,str) or not ident: raise BusinessError("RUN_ID","批次编号须为文本")
    return ident
@app.get("/api/sample")
def sample(): return {"text":sample_text()}
