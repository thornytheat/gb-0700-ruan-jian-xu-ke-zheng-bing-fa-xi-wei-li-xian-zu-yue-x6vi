import csv,io,json

def _writer(out):
    return csv.writer(out)

def _manifest(writer,run,extra):
    writer.writerow([run["id"],run["digest"],run["rule_version"],"manifest","","",
                     json.dumps(dict({"balance":run["result"]["balance"],"decision":run["decision"],
                                      "revision":run["revision"]},**extra),ensure_ascii=False)])

def _result_rows(writer,run):
    for row in run["result"].get("final",[]):
        writer.writerow([run["id"],run["digest"],run["rule_version"],"result",row.get("line",""),
                         row.get("status",""),json.dumps(row,ensure_ascii=False)])
    for row in run["result"].get("quarantine",[]):
        writer.writerow([run["id"],run["digest"],run["rule_version"],"quarantine",row.get("line",""),
                         row["reason"],json.dumps(row,ensure_ascii=False)])

def evidence_csv(run):
    out=io.StringIO();writer=_writer(out)
    writer.writerow(["run_id","sha256","rule_version","kind","line","reason","evidence"])
    settlement_record=None;link="none"
    try:
        from app.settlement import settlement_for_run,reference_for
        settlement_record=settlement_for_run(run["id"])
        if settlement_record:
            link="linked"
        else:
            # 新合同批次尚未生成调整时，仍把相关已确认结算作为对照依据导出，
            # 保证同一份证据里能看到原结算与调整记录。
            settlement_record=reference_for(run)
            if settlement_record: link="reference"
    except Exception:
        settlement_record=None
    _manifest(writer,run,{"settlement":settlement_record["id"] if settlement_record else None,
                          "settlement_link":link})
    _result_rows(writer,run)
    if settlement_record:
        _settlement_rows(writer,settlement_record,run["id"],run["digest"],link)
    return "﻿"+out.getvalue()

def _settlement_rows(writer,record,run_id,digest,link):
    original=record["original"];recognized=record["recognized"];current=record["current"]
    writer.writerow([run_id,digest,record["rule_version"],"settlement-original","","",
                     json.dumps({"settlement":record["id"],"link":link,"basis_run":original["run_id"],
                                 "basis_digest":original["digest"],"basis_rule_version":original["rule_version"],
                                 "review":original["review"],"contract":original["contract"],
                                 "total_seat_hours":original["original"]["total"],
                                 "products":original["original"]["products"]},ensure_ascii=False)])
    writer.writerow([run_id,digest,record["rule_version"],"settlement-recognized","","",
                     json.dumps({"settlement":record["id"],"recognized_total":recognized["total"],
                                 "adjustment_total":record["adjustment_total"],
                                 "products":recognized["products"],
                                 "segments":recognized["segments"]},ensure_ascii=False)])
    writer.writerow([run_id,digest,current["rule_version"],"settlement-current","","",
                     json.dumps({"settlement":record["id"],"current_run":current["run_id"],
                                 "current_digest":current["digest"],"current_rule_version":current["rule_version"],
                                 "decision":current["decision"],"review_revision":current["review_revision"],
                                 "purchase":current["purchase"],"contract":current["contract"],
                                 "overuse":[{k:r.get(k) for k in ("product","a","b","concurrent","capacity","excess","status")}
                                            for r in current["usage"] if r.get("excess")]},ensure_ascii=False)])
    for adjustment in record["adjustments"]:
        writer.writerow([run_id,digest,adjustment["after_rule_version"],"settlement-adjustment","","",
                         json.dumps({"settlement":adjustment["settlement_id"],"adjustment":adjustment["id"],
                                     "effective_at":adjustment["effective_at"],
                                     "before_rule_version":adjustment["before_rule_version"],
                                     "after_rule_version":adjustment["after_rule_version"],
                                     "source_run":adjustment["source_run_id"],
                                     "source_digest":adjustment["source_digest"],
                                     "direction":adjustment["direction"],
                                     "total_before":adjustment["total_before"],
                                     "total_after":adjustment["total_after"],
                                     "total_delta":adjustment["total_delta"],
                                     "lines":adjustment["lines"]},ensure_ascii=False)])

def settlement_csv(record):
    """结算证据：原结算依据、当前超用与全部调整记录在同一份导出中。"""
    out=io.StringIO();writer=_writer(out)
    writer.writerow(["settlement_id","kind","effective_at","rule_version","before_total","after_total","delta","direction","evidence"])
    writer.writerow([record["id"],"original","",record["original"]["rule_version"],
                     record["original"]["original"]["total"],record["original"]["original"]["total"],"0","基准",
                     json.dumps({"basis":record["original"],"recognized":record["recognized"],
                                 "adjustment_total":record["adjustment_total"]},ensure_ascii=False)])
    writer.writerow([record["id"],"current","",record["current"]["rule_version"],"","","","",
                     json.dumps(record["current"],ensure_ascii=False)])
    for adjustment in record["adjustments"]:
        writer.writerow([record["id"],"adjustment",adjustment["effective_at"],
                         adjustment["after_rule_version"],adjustment["total_before"],
                         adjustment["total_after"],adjustment["total_delta"],adjustment["direction"],
                         json.dumps({k:adjustment[k] for k in ("id","source_run_id","source_digest","lines","contract")},ensure_ascii=False)])
    return "﻿"+out.getvalue()
