"""结算追溯的接口与证据导出：当前超用、原结算与调整记录必须同屏可追溯。"""
import csv, io, json
from decimal import Decimal
import pytest
from app import config
from app.pipeline import sample_text

@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    return tmp_path

@pytest.fixture
def client():
    from fastapi.testclient import TestClient
    from app.main import app
    return TestClient(app, raise_server_exceptions=False)

def _run(client, text=None):
    payload = {} if text is None else {"text": text}
    return client.post("/api/run", json=payload).json()

def _tight(client):
    text = client.get("/api/sample").json()["text"].replace("capacity,1,12", "capacity,1,8")
    return _run(client, text)

def test_settlement_api_round_trip(client):
    base = _run(client)
    created = client.post("/api/settlements", json={"run_id": base["id"]})
    assert created.status_code == 200 and created.json()["recognized"]["total"] == "40.000000"
    listing = client.get("/api/settlements").json()["settlements"]
    assert len(listing) == 1 and listing[0]["id"] == created.json()["id"]

def test_adjustment_api_links_original(client):
    base = _run(client); tightened = _tight(client)
    record = client.post("/api/settlements", json={"run_id": base["id"]}).json()
    preview = client.post(f"/api/settlements/{record['id']}/adjustments/preview",
                          json={"run_id": tightened["id"], "effective_at": "2026-11-18T00:00:00+00:00"}).json()
    assert preview["total_before"] == "40.000000" and preview["total_delta"] == "24.000000"
    adjusted = client.post(f"/api/settlements/{record['id']}/adjustments",
                           json={"run_id": tightened["id"], "effective_at": "2026-11-18T00:00:00+00:00"}).json()
    detail = client.get(f"/api/settlements/{record['id']}").json()
    assert adjusted["settlement_id"] == record["id"]
    assert detail["original"]["run_id"] == base["id"]
    assert detail["current"]["run_id"] == tightened["id"]
    assert len(detail["adjustments"]) == 1

def test_adjustment_api_rejects_bad_effective(client):
    base = _run(client)
    record = client.post("/api/settlements", json={"run_id": base["id"]}).json()
    response = client.post(f"/api/settlements/{record['id']}/adjustments",
                           json={"run_id": base["id"], "effective_at": "2026-11-18T00:00:00"})
    assert response.status_code == 422 and response.json()["error"] == "TIMEZONE"

def test_adjustment_api_requires_effective(client):
    base = _run(client)
    record = client.post("/api/settlements", json={"run_id": base["id"]}).json()
    response = client.post(f"/api/settlements/{record['id']}/adjustments", json={"run_id": base["id"]})
    assert response.status_code == 422 and response.json()["error"] == "EFFECTIVE_AT"

def test_settlement_unknown_is_404(client):
    assert client.get("/api/settlements/stlnope").status_code == 404

def test_evidence_export_shows_current_original_and_adjustments(client):
    base = _run(client); tightened = _tight(client)
    record = client.post("/api/settlements", json={"run_id": base["id"]}).json()
    client.post(f"/api/settlements/{record['id']}/adjustments",
                json={"run_id": tightened["id"], "effective_at": "2026-11-18T03:00:00+00:00"})
    rows = list(csv.reader(io.StringIO(client.get(f"/api/export/{tightened['id']}").text.lstrip("﻿"))))
    header, body = rows[0], rows[1:]
    kinds = {row[3] for row in body}
    assert {"manifest", "settlement-original", "settlement-current", "settlement-adjustment"} <= kinds
    manifest = json.loads(next(row[6] for row in body if row[3] == "manifest"))
    assert manifest["settlement"] == record["id"] and manifest["settlement_link"] == "linked"
    original = json.loads(next(row[6] for row in body if row[3] == "settlement-original"))
    current = json.loads(next(row[6] for row in body if row[3] == "settlement-current"))
    adjustment = json.loads(next(row[6] for row in body if row[3] == "settlement-adjustment"))
    assert original["basis_run"] == base["id"] and original["total_seat_hours"] == "40.000000"
    assert current["current_run"] == tightened["id"]
    assert any(row["excess"] for row in current["overuse"])
    assert adjustment["effective_at"] == "2026-11-18T03:00:00+00:00"
    assert adjustment["total_before"] and adjustment["total_after"] and adjustment["total_delta"]
    assert adjustment["direction"] in {"补收", "冲减", "无差异"}
    assert header[:4] == ["run_id", "sha256", "rule_version", "kind"]

def test_settlement_export_contains_before_after(client):
    base = _run(client); tightened = _tight(client)
    record = client.post("/api/settlements", json={"run_id": base["id"]}).json()
    client.post(f"/api/settlements/{record['id']}/adjustments",
                json={"run_id": tightened["id"], "effective_at": "2026-11-18T03:00:00+00:00"})
    rows = list(csv.reader(io.StringIO(client.get(f"/api/export/settlement/{record['id']}").text.lstrip("﻿"))))
    kinds = [row[1] for row in rows[1:]]
    assert kinds[0] == "original" and "current" in kinds and "adjustment" in kinds
    adjustment = next(row for row in rows[1:] if row[1] == "adjustment")
    evidence = json.loads(adjustment[8])
    assert evidence["source_run_id"] == tightened["id"]
    assert Decimal(adjustment[4]) + Decimal(adjustment[6]) == Decimal(adjustment[5])
    assert evidence["lines"]["products"]

def test_workbench_shows_settlement_columns(client):
    base = _run(client); tightened = _tight(client)
    record = client.post("/api/settlements", json={"run_id": base["id"]}).json()
    client.post(f"/api/settlements/{record['id']}/adjustments",
                json={"run_id": tightened["id"], "effective_at": "2026-11-18T03:00:00+00:00"})
    page = client.get(f"/module/6?run_id={tightened['id']}").text
    assert "结算追溯" in page and "当前许可结果" in page and "原结算依据" in page and "调整记录" in page
    assert record["id"] in page and "导出结算证据" in page
    assert "本批次已关联" in page

def test_workbench_reference_settlement_when_unsettled(client):
    base = _run(client)
    record = client.post("/api/settlements", json={"run_id": base["id"]}).json()
    new = _run(client, sample_text().replace("capacity,1,10", "capacity,1,12") + "\n")
    page = client.get(f"/module/6?run_id={new['id']}").text
    assert record["id"] in page and "对照最近结算" in page
