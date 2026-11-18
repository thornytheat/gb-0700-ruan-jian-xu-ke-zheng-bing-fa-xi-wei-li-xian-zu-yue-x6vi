# 接口

`POST /api/run`：{"text":"CSV内容"}。同规则版本与输入摘要只产生一条批次。

`GET/POST /api/1` 至 `/api/7`：独立样例或自定义文件计算。各模块返回 report、quarantine、balance。

`POST /api/review`：id、整数revision、decision取通过或退回。旧版本409；阻断项不能通过。

`GET /api/export/{id}`：唯一证据CSV，含运行摘要、结果与隔离记录；若该批次关联结算，同份证据追加 `settlement-original`、`settlement-recognized`、`settlement-current` 与全部 `settlement-adjustment` 行。`GET /health`：存活检查。

# 结算追溯

`POST /api/settlements`：{"run_id":"批次"}。以该批次登记原结算并冻结依据；同批次重复登记返回原结算。

`GET /api/settlements`：结算列表（含调整笔数）。`GET /api/settlements/{id}`：原结算依据、当前许可结果、期间确认金额、调整记录。

`POST /api/settlements/{id}/adjustments`：{"effective_at":"带时区ISO","run_id":"新合同批次","idempotency_key":"可选"}。按生效点追加补收或冲减；省略run_id取该结算当前批次。

`POST /api/settlements/{id}/adjustments/preview`：同字段只读试算，不落库。

`GET /api/export/settlement/{id}`：结算证据CSV（original、current、adjustment 行，含调整前后金额与逐区间差额）。

调整的幂等键在结算内唯一：相同键与相同载荷返回已有调整，不新增金额；键相同但载荷不同返回409。生效点缺时区返回422 TIMEZONE。结算或批次不存在返回404。已作为其他结算调整来源的批次再次登记原结算返回409 ALREADY_RECOGNIZED。

错误：结构/数值422，版本冲突409，不存在404，文件过大413。逐行异常进入quarantine。
