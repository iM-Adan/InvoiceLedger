"""End-to-end tests: every demo invoice must reach its expected decision (offline engine).

Run:  STAGE_DELAY_S=0 TODAY=2026-11-17 python -m pytest -q
"""
import json
import os
import time
import uuid

os.environ.setdefault("STAGE_DELAY_S", "0")
os.environ.setdefault("TODAY", "2026-11-17")
os.environ.setdefault("STORAGE_DIR", "/tmp/invoiceflow-test")

from fastapi.testclient import TestClient  # noqa: E402

from app import config, db  # noqa: E402
from app.main import app  # noqa: E402

client = TestClient(app)


def _wait(ids, timeout=120):
    t = time.time()
    while time.time() - t < timeout:
        runs = {r["id"]: r for r in client.get("/api/runs").json()}
        if all(runs.get(i, {}).get("status") in ("done", "error") for i in ids):
            return runs
        time.sleep(0.2)
    raise TimeoutError


def test_demo_library_decisions():
    client.post("/api/reset")
    catalog = json.loads((config.SAMPLES_DIR / "catalog.json").read_text())
    ids = client.post("/api/batch", data={"engine": "offline"}).json()["run_ids"]
    runs = _wait(ids)
    failures = [(s["file"], s["expected"], runs[i]["decision"], runs[i]["headline"])
                for s, i in zip(catalog, ids) if runs[i]["decision"] != s["expected"]]
    assert not failures, failures


def test_rerun_is_caught_as_duplicate():
    client.post("/api/reset")
    a = client.post("/api/runs", data={"sample": "01_happy_apex_INV-10482.pdf", "engine": "offline"}).json()["run_id"]
    _wait([a])
    b = client.post("/api/runs", data={"sample": "01_happy_apex_INV-10482.pdf", "engine": "offline"}).json()["run_id"]
    runs = _wait([b])
    assert runs[a]["decision"] == "APPROVE"
    assert runs[b]["decision"] == "REJECT"


def test_out_of_scope_line_is_held_even_when_po_has_balance():
    client.post("/api/reset")
    # run before milestone 2: the $9k fits the $18k remaining balance...
    a = client.post("/api/runs", data={"sample": "06_split_po_overbill_meridian_MCG-2026-052.pdf", "engine": "offline"}).json()["run_id"]
    _wait([a])
    # ...but its out-of-scope line is still flagged by the variance rule
    r = client.get(f"/api/runs/{a}").json()
    assert r["decision"] == "HOLD"
    assert any(x["rule"] == "R-AMT-01" for x in r["result"]["reasons"])


def test_claim_next_run_processes_queued_runs_in_order_once():
    first, second = f"queue-{uuid.uuid4().hex}", f"queue-{uuid.uuid4().hex}"
    with db.transaction():
        db.execute("INSERT INTO runs(id, created_at, status) VALUES (?,?,?)",
                   (first, "2000-01-01T00:00:00.000+00:00", "queued"))
        db.enqueue_run(first)
        db.execute("INSERT INTO runs(id, created_at, status) VALUES (?,?,?)",
                   (second, "2000-01-02T00:00:00.000+00:00", "queued"))
        db.enqueue_run(second)
    try:
        assert db.claim_next_run() == first
        assert db.claim_next_run() == second
        assert db.claim_next_run() is None
        assert db.get_run(first)["status"] == "running"
        assert db.get_run(second)["status"] == "running"
    finally:
        db.execute("DELETE FROM run_jobs WHERE run_id IN (?,?)", (first, second))
        db.execute("DELETE FROM runs WHERE id IN (?,?)", (first, second))


def test_demo_mode_rejects_arbitrary_uploads(monkeypatch):
    monkeypatch.setattr(config, "DEMO_MODE", True)
    response = client.post("/api/runs", data={"engine": "offline"},
                           files={"file": ("invoice.pdf", b"not a real PDF", "application/pdf")})
    assert response.status_code == 403


def test_purchase_order_settlement_tracks_invoice_payment_status():
    client.post("/api/reset")
    pos = {p["po_number"]: p for p in client.get("/api/pos").json()}
    settled_po = pos["PO-4499"]
    unsettled_po = pos["PO-4501"]
    invoice = settled_po["invoices"][0]

    assert settled_po["settled"] is True
    assert unsettled_po["settled"] is False
    assert invoice["payment_status"] == "paid"

    with db.transaction():
        db.execute(
            "INSERT INTO ledger(source, po_number, subtotal, total, lines_json, payment_status, created_at) "
            "VALUES ('test', 'PO-4499', 10, 10, '[]', 'unpaid', ?)",
            (db.now_iso(),),
        )
    pos = {p["po_number"]: p for p in client.get("/api/pos").json()}
    extra_invoice = next(i for i in pos["PO-4499"]["invoices"] if i["payment_status"] == "unpaid")
    assert pos["PO-4499"]["settled"] is False

    response = client.put(f"/api/ledger/{extra_invoice['id']}/payment", params={"paid": "true"})
    assert response.status_code == 200
    pos = {p["po_number"]: p for p in client.get("/api/pos").json()}
    assert pos["PO-4499"]["settled"] is True

    response = client.put(f"/api/ledger/{invoice['id']}/payment", params={"paid": "false"})
    assert response.status_code == 200
    pos = {p["po_number"]: p for p in client.get("/api/pos").json()}
    assert pos["PO-4499"]["settled"] is False
    response = client.put(f"/api/ledger/{invoice['id']}/payment", params={"paid": "true"})
    assert response.status_code == 200
    pos = {p["po_number"]: p for p in client.get("/api/pos").json()}
    assert pos["PO-4499"]["settled"] is True
    assert client.put("/api/ledger/999999/payment", params={"paid": "true"}).status_code == 404
    client.post("/api/reset")


def test_corrupt_file_goes_to_manual_review(tmp_path):
    p = tmp_path / "broken.pdf"
    p.write_bytes(b"%PDF-1.4 this is not really a pdf")
    with open(p, "rb") as fh:
        rid = client.post("/api/runs", files={"file": ("broken.pdf", fh, "application/pdf")}, data={"engine": "offline"}).json()["run_id"]
    runs = _wait([rid])
    assert runs[rid]["decision"] == "HOLD"


def test_rejects_unsupported_type():
    r = client.post("/api/runs", files={"file": ("x.docx", b"abc", "application/octet-stream")}, data={"engine": "offline"})
    assert r.status_code == 415


def test_indian_gst_invoice_parsed_offline():
    from pathlib import Path
    from app.extract import best_offline_parse, read_document
    f, _ = best_offline_parse(read_document(Path(config.SAMPLES_DIR / "13_india_gst_sahyadri_SPP-0418.pdf")))
    assert f["currency"] == "INR"
    assert f["invoice_date"] == "2026-11-05"          # DD/MM, not 11 May
    assert f["total"] == 123310.0                      # 'Grand Total ₹ 1,23,310' (no paise), not the qty in the Total row
    assert f["tax_amount"] == 18810.0                  # CGST + SGST summed
    assert [li["quantity"] for li in f["line_items"]] == [2000.0, 300.0]


def test_offline_parser_recognizes_common_printed_date_formats():
    from app.extract import offline_parse

    for printed_date in ("17-Nov-2026", "November 17th, 2026", "2026/11/17"):
        parsed = offline_parse(f"Invoice Date: {printed_date}", 0.8)
        assert parsed["invoice_date"] == "2026-11-17", printed_date


def test_gemini_rate_limit_waits_then_succeeds_without_model_hopping(monkeypatch):
    import httpx
    from pathlib import Path
    from app import extract
    monkeypatch.setattr(config, "GEMINI_API_KEY", "k")
    monkeypatch.setattr(config, "GEMINI_RPM", 6000)
    extract._gem_cooldown_until = 0
    per_min = {"error": {"code": 429, "message": "Quota exceeded for quota metric 'Generate Content API requests per minute'",
                         "details": [{"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "1s"}]}}
    ok = {"candidates": [{"content": {"parts": [{"text": json.dumps({"invoice_number": "X1", "total": 10, "currency": "USD"})}]}}]}
    seq, models = [(429, per_min), (200, ok)], []

    def fake_post(url, **kw):
        models.append(url.split("/models/")[1].split(":")[0])
        st, body = seq.pop(0)
        return httpx.Response(st, json=body, request=httpx.Request("POST", url))
    monkeypatch.setattr(extract.httpx, "post", fake_post)
    f, model = extract.gemini_extract(Path(config.SAMPLES_DIR / "01_happy_apex_INV-10482.pdf"), "application/pdf")
    assert f["invoice_number"] == "X1"
    assert models == [config.GEMINI_MODEL, config.GEMINI_MODEL]
