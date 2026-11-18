"""结算追溯调整：按生效点补收/冲减，原结算依据与调整前后金额可对账。"""
import pytest
from decimal import Decimal
from app import config
from app.pipeline import run, sample_text
from app import store, settlement
from app.errors import BusinessError

@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    return tmp_path

def _saved(text):
    return store.save(text, run(text))

def _tightened(seats=8):
    return sample_text().replace("capacity,1,12", f"capacity,1,{seats}")

@pytest.fixture
def base():
    return _saved(sample_text())

@pytest.fixture
def record(base):
    return settlement.settle(base)

def test_settle_freezes_original_basis(base, record):
    assert record["recognized"]["total"] == "40.000000"
    assert record["original"]["run_id"] == base["id"]
    assert record["original"]["digest"] == base["digest"]
    assert record["original"]["review"] == {"revision": 0, "decision": "待复核"}
    assert record["adjustments"] == []

def test_settle_is_idempotent_per_run(base):
    first = settlement.settle(base)
    second = settlement.settle(base)
    assert first["id"] == second["id"]
    assert len(settlement.listing()) == 1

def test_current_uses_new_contract_not_old_cap(record):
    tightened = _saved(_tightened())
    detail = settlement.get(record["id"])
    assert detail["current"]["run_id"] == tightened["id"]
    assert detail["current"]["pending"] is True
    # 新合同下超用更高，不能被旧上限掩盖
    assert Decimal(detail["current"]["purchase"][0]["excess_seat_hours"]) > Decimal("40")

def test_rerating_uses_new_contract_cap(record):
    tightened = _saved(_tightened())
    preview = settlement.preview(record["id"], "2026-11-18T00:00:00+00:00", tightened)
    # 旧上限 12 之下原值 40；新上限 8 必须按新合同重算，不能沿用旧上限
    assert Decimal(preview["total_after"]) == Decimal(tightened["result"]["purchase"][0]["excess_seat_hours"])
    assert preview["total_after"] != record["recognized"]["total"]

def test_adjustment_recomputes_under_new_contract(record):
    tightened = _saved(_tightened())
    preview = settlement.preview(record["id"], "2026-11-18T00:00:00+00:00", tightened)
    assert preview["direction"] == "补收"
    assert preview["total_before"] == "40.000000"
    assert preview["total_after"] == "64.000000"
    assert preview["total_delta"] == "24.000000"

def test_adjustment_keeps_original_and_links(record):
    tightened = _saved(_tightened())
    adjustment = settlement.adjust(record["id"], "2026-11-18T00:00:00+00:00", tightened)
    detail = settlement.get(record["id"])
    assert adjustment["settlement_id"] == record["id"]
    assert adjustment["source_run_id"] == tightened["id"]
    assert detail["original"]["run_id"] == record["original"]["run_id"]
    assert detail["original"]["original"]["total"] == "40.000000"
    assert Decimal(detail["original"]["original"]["total"]) + Decimal(detail["adjustment_total"]) == Decimal(detail["recognized"]["total"])

def test_effective_point_leaves_earlier_basis(record):
    tightened = _saved(_tightened())
    midnight = settlement.preview(record["id"], "2026-11-18T00:00:00+00:00", tightened)
    noon_only = settlement.preview(record["id"], "2026-11-18T12:00:00+00:00", tightened)
    # 生效点之前已确认部分不再重算：全天差额大于正午之后差额
    assert Decimal(noon_only["total_delta"]) < Decimal(midnight["total_delta"])
    assert all(Decimal(line["a"]) >= Decimal(str(settlement.instant("2026-11-18T12:00:00+00:00")))
               for line in noon_only["lines"]["segments"])

def test_adjustment_does_not_rebill_confirmed_period(record):
    tightened = _saved(_tightened())
    adjustment = settlement.adjust(record["id"], "2026-11-18T06:00:00+00:00", tightened)
    detail = settlement.get(record["id"])
    cut = Decimal(str(settlement.instant("2026-11-18T06:00:00+00:00")))
    # 06:00 之前的确认段一字未改；只有之后被新合同替换
    kept = [s for s in detail["recognized"]["segments"]["CAD"] if Decimal(s["b"]) <= cut]
    assert kept and all(int(s["excess"]) > 0 for s in kept)
    assert Decimal(detail["recognized"]["total"]) > Decimal(detail["original"]["original"]["total"])
    # 差额行只出现在生效点之后
    assert all(Decimal(line["a"]) >= cut for line in adjustment["lines"]["segments"])

def test_adjustment_basis_segments_unchanged_before_effective(record):
    tightened = _saved(_tightened())
    before_segments = settlement.get(record["id"])["recognized"]["segments"]
    cut = "2026-11-18T06:00:00+00:00"
    settlement.adjust(record["id"], cut, tightened)
    kept = [s for s in before_segments["CAD"] if Decimal(s["b"]) <= Decimal(str(settlement.instant(cut)))]
    after = settlement.get(record["id"])["recognized"]["segments"]["CAD"]
    for segment in kept:
        assert {"a": segment["a"], "b": segment["b"], "excess": segment["excess"]} in after

def test_retry_same_adjustment_adds_no_money(record):
    tightened = _saved(_tightened())
    first = settlement.adjust(record["id"], "2026-11-18T03:00:00+00:00", tightened)
    total = settlement.get(record["id"])["recognized"]["total"]
    second = settlement.adjust(record["id"], "2026-11-18T03:00:00+00:00", tightened)
    again = settlement.get(record["id"])
    assert first["id"] == second["id"]
    assert len(again["adjustments"]) == 1
    assert again["recognized"]["total"] == total

def test_idempotency_key_conflict_rejected(record):
    tightened = _saved(_tightened())
    settlement.adjust(record["id"], "2026-11-18T03:00:00+00:00", tightened, "fixed")
    with pytest.raises(BusinessError) as caught:
        settlement.adjust(record["id"], "2026-11-18T04:00:00+00:00", tightened, "fixed")
    assert caught.value.code == "IDEMPOTENCY_CONFLICT"
    assert caught.value.status == 409

def test_interrupted_adjustment_keeps_previous_version(record, monkeypatch):
    tightened = _saved(_tightened())
    before = settlement.get(record["id"])
    def explode(stage):
        if stage == "after-ledger-insert": raise RuntimeError("interrupted")
    monkeypatch.setattr(settlement, "_fault_point", explode)
    with pytest.raises(RuntimeError):
        settlement.adjust(record["id"], "2026-11-18T03:00:00+00:00", tightened)
    after = settlement.get(record["id"])
    assert after["adjustments"] == []
    assert after["recognized"]["total"] == before["recognized"]["total"]
    assert settlement.listing()[0]["adjustments"] == 0
    # 中断后重试同一调整，只产生一笔
    monkeypatch.setattr(settlement, "_fault_point", lambda stage: None)
    settlement.adjust(record["id"], "2026-11-18T03:00:00+00:00", tightened)
    settlement.adjust(record["id"], "2026-11-18T03:00:00+00:00", tightened)
    assert len(settlement.get(record["id"])["adjustments"]) == 1

def test_adjustment_requires_effective_point(record):
    tightened = _saved(_tightened())
    for bad in ("", "2026-11-18T03:00:00", "not-a-time"):
        with pytest.raises(BusinessError):
            settlement.adjust(record["id"], bad, tightened)

def test_adjustment_rejects_unknown_settlement(base):
    tightened = _saved(_tightened())
    with pytest.raises(BusinessError) as caught:
        settlement.adjust("stlmissing", "2026-11-18T00:00:00+00:00", tightened)
    assert caught.value.status == 404

def test_line_and_product_totals_reconcile(record):
    tightened = _saved(_tightened())
    adjustment = settlement.adjust(record["id"], "2026-11-18T00:00:00+00:00", tightened)
    line_total = sum((Decimal(line["delta_seat_hours"]) for line in adjustment["lines"]["segments"]), Decimal(0))
    product_total = sum((Decimal(p["delta"]) for p in adjustment["lines"]["products"]), Decimal(0))
    assert line_total == Decimal(adjustment["total_delta"])
    assert product_total == Decimal(adjustment["total_delta"])

def test_second_adjustment_only_covers_new_delta(record):
    tightened = _saved(_tightened(8))
    looser = _saved(_tightened(10))
    settlement.adjust(record["id"], "2026-11-18T00:00:00+00:00", tightened)
    after_first = Decimal(settlement.get(record["id"])["recognized"]["total"])
    second = settlement.adjust(record["id"], "2026-11-18T00:00:00+00:00", looser)
    detail = settlement.get(record["id"])
    assert second["total_before"] == f"{after_first:f}"
    assert Decimal(detail["recognized"]["total"]) == Decimal(second["total_after"])
    assert detail["adjustments"][0]["total_after"] == second["total_before"]

def test_preview_is_read_only(record):
    tightened = _saved(_tightened())
    before = settlement.get(record["id"])
    settlement.preview(record["id"], "2026-11-18T03:00:00+00:00", tightened)
    after = settlement.get(record["id"])
    assert after["adjustments"] == []
    assert after["recognized"]["total"] == before["recognized"]["total"]
    assert after["revision"] == before["revision"]

def test_concurrent_adjustments_serialize(record):
    import threading
    tightened = _saved(_tightened())
    errors = []
    def worker(hour):
        try:
            settlement.adjust(record["id"], f"2026-11-18T0{hour}:00:00+00:00", tightened, f"key-{hour}")
        except Exception as error:
            errors.append(error)
    threads = [threading.Thread(target=worker, args=(hour,)) for hour in range(1, 6)]
    for thread in threads: thread.start()
    for thread in threads: thread.join()
    detail = settlement.get(record["id"])
    assert errors == []
    assert len(detail["adjustments"]) == 5
    assert Decimal(detail["original"]["original"]["total"]) + Decimal(detail["adjustment_total"]) == Decimal(detail["recognized"]["total"])

def test_concurrent_retry_of_same_key_is_single_charge(record):
    import threading
    tightened = _saved(_tightened())
    errors = []
    def worker():
        try:
            settlement.adjust(record["id"], "2026-11-18T03:00:00+00:00", tightened, "same-key")
        except Exception as error:
            errors.append(error)
    threads = [threading.Thread(target=worker) for _ in range(5)]
    for thread in threads: thread.start()
    for thread in threads: thread.join()
    detail = settlement.get(record["id"])
    assert errors == [] and len(detail["adjustments"]) == 1

def test_loosened_contract_credits_without_double_charge(record):
    looser = _saved(_tightened(20))
    preview = settlement.preview(record["id"], "2026-11-18T00:00:00+00:00", looser)
    assert preview["direction"] == "冲减"
    assert Decimal(preview["total_after"]) == Decimal(looser["result"]["purchase"][0]["excess_seat_hours"]).quantize(Decimal("0.000001"))
    adjustment = settlement.adjust(record["id"], "2026-11-18T00:00:00+00:00", looser)
    detail = settlement.get(record["id"])
    assert Decimal(adjustment["total_delta"]) < 0
    assert Decimal(detail["recognized"]["total"]) == Decimal(adjustment["total_after"])

def test_source_run_cannot_be_settled_twice(record):
    tightened = _saved(_tightened())
    settlement.adjust(record["id"], "2026-11-18T03:00:00+00:00", tightened)
    with pytest.raises(BusinessError) as caught:
        settlement.settle(tightened)
    assert caught.value.code == "ALREADY_RECOGNIZED" and caught.value.status == 409

def test_zero_overuse_settlement_scopes_to_its_products():
    text = sample_text().replace("capacity,1,12", "capacity,1,1000")
    base = _saved(text)
    record = settlement.settle(base)
    assert record["recognized"]["total"] == "0.000000"
    assert record["original"]["products"] == ["CAD"]
    other = text.replace("CAD-Pro", "Render").replace(",cad,", ",Render,").replace(",CAD,", ",Render,")
    _saved(other)
    detail = settlement.get(record["id"])
    assert detail["current"]["run_id"] == base["id"] and detail["current"]["pending"] is False
    tightened = _saved(_tightened())
    adjustment = settlement.adjust(record["id"], "2026-11-18T00:00:00+00:00", tightened)
    assert adjustment["total_before"] == "0.000000" and adjustment["direction"] == "补收"
