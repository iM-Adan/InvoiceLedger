"""InvoiceFlow API + static UI."""
from __future__ import annotations

import asyncio
import json
import uuid
from contextlib import asynccontextmanager
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import config, db, masterdata, pipeline, rules, storage


@asynccontextmanager
async def lifespan(_app):
    db.conn()
    if not config.VERCEL_RUNTIME:
        db.mark_interrupted_runs(include_queued=True)
    yield


app = FastAPI(title="InvoiceFlow", version="1.0", lifespan=lifespan)
# One worker = invoices are processed strictly in arrival order, which matters
# for split-PO billing (each approval changes the PO's remaining balance).
EXECUTOR = ThreadPoolExecutor(max_workers=1)


def _catalog() -> list[dict]:
    p = config.SAMPLES_DIR / "catalog.json"
    return json.loads(p.read_text()) if p.exists() else []


def _resolve_engine(requested: str) -> str:
    avail = pipeline.engine_available()
    if requested == "offline":
        return "offline"
    if requested == "gemini":
        if not avail["gemini"]:
            raise HTTPException(400, "Gemini selected but GEMINI_API_KEY is not configured on the server.")
        return "gemini"
    return "gemini" if avail["gemini"] else "offline"


def _create_run(src: Path, filename: str, source: str, engine: str) -> str:
    run_id = uuid.uuid4().hex[:12]
    stored = storage.store_file(src, run_id)
    with db.transaction():
        db.execute("INSERT INTO runs(id, created_at, filename, stored_path, mime, source, engine_requested, engine_used, status) VALUES (?,?,?,?,?,?,?,?,?)",
                   (run_id, db.now_iso(), filename, str(stored), src.suffix.lower(), source, engine, _resolve_engine(engine), "queued"))
        if config.VERCEL_RUNTIME:
            db.enqueue_run(run_id)
    if not config.VERCEL_RUNTIME:
        EXECUTOR.submit(pipeline.process, run_id)
    return run_id


@app.get("/api/config")
def get_config():
    return {"engines": pipeline.engine_available(), "demo_mode": config.DEMO_MODE, "rules": config.rules_summary(), "rule_catalog": [
        {"id": r[0], "stage": r[1], "title": r[2], "outcome": r[3]} for r in rules.RULES],
        "stages": [{"id": s[0], "title": s[1], "desc": s[2]} for s in pipeline.STAGES], "samples": _catalog(),
        "today": rules.today().isoformat()}


@app.post("/api/runs")
async def create_run(file: UploadFile | None = File(None), sample: str | None = Form(None), engine: str = Form("auto")):
    if sample:
        src = (config.SAMPLES_DIR / Path(sample).name)
        if not src.exists() or src.suffix.lower() not in config.ALLOWED_EXT:
            raise HTTPException(404, "Unknown sample")
        return {"run_id": _create_run(src, src.name, "sample", engine)}
    if config.DEMO_MODE:
        raise HTTPException(403, "File uploads are disabled on this public demo.")
    if not file or not file.filename:
        raise HTTPException(400, "Upload a file or choose a sample")
    ext = Path(file.filename).suffix.lower()
    if ext not in config.ALLOWED_EXT:
        raise HTTPException(415, f"Unsupported file type '{ext}'. Use PDF, PNG, JPG, TIFF or WEBP.")
    data = await file.read()
    if len(data) > config.MAX_UPLOAD_MB * 1024 * 1024:
        raise HTTPException(413, f"File larger than {config.MAX_UPLOAD_MB:g} MB")
    if not data:
        raise HTTPException(400, "Empty file")
    tmp = config.UPLOAD_DIR / f"tmp_{uuid.uuid4().hex}{ext}"
    tmp.write_bytes(data)
    try:
        rid = _create_run(tmp, Path(file.filename).name, "upload", engine)
    finally:
        tmp.unlink(missing_ok=True)
    return {"run_id": rid}


@app.post("/api/batch")
def run_batch(engine: str = Form("auto")):
    ids = []
    for s in _catalog():
        src = config.SAMPLES_DIR / s["file"]
        ids.append(_create_run(src, src.name, "sample", engine))
    return {"run_ids": ids}


@app.get("/api/runs")
def list_runs(limit: int = 200):
    return db.query("SELECT id, created_at, filename, source, engine_used, status, decision, headline, vendor_id, vendor_name, invoice_number, "
                    "invoice_date, total, currency, po_number, duration_ms FROM runs ORDER BY created_at DESC LIMIT ?", (limit,))


@app.get("/api/runs/{run_id}")
def get_run(run_id: str):
    r = db.get_run(run_id)
    if not r:
        raise HTTPException(404, "Run not found")
    r["events"] = db.events_after(run_id, 0)
    r.pop("stored_path", None)
    return r


@app.get("/api/runs/{run_id}/file")
def run_file(run_id: str):
    r = db.get_run(run_id)
    if not r:
        raise HTTPException(404)
    media = {".pdf": "application/pdf", ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp", ".tif": "image/tiff", ".tiff": "image/tiff"}
    suffix = Path(r["filename"]).suffix.lower()
    media_type = media.get(suffix, "application/octet-stream")
    headers = {"Content-Disposition": f"inline; filename=\"{r['filename']}\""}
    if not storage.is_blob_path(r["stored_path"]):
        return FileResponse(Path(r["stored_path"]), media_type=media_type, headers=headers)
    return Response(storage.read_file(r["stored_path"]), media_type=media_type, headers=headers)


@app.get("/api/runs/{run_id}/preview.png")
def run_preview(run_id: str):
    """First page rendered to PNG so every browser shows the same preview (PDF or image)."""
    import io

    import pypdfium2 as pdfium
    from fastapi.responses import Response
    from PIL import Image

    r = db.get_run(run_id)
    if not r:
        raise HTTPException(404)
    p = None
    temporary = False
    try:
        p, temporary = storage.materialize_path(r["stored_path"])
        if p.suffix.lower() == ".pdf":
            doc = pdfium.PdfDocument(str(p))
            img = doc[0].render(scale=1.6).to_pil()
            doc.close()
        else:
            img = Image.open(p)
            img.thumbnail((1400, 1800))
        buf = io.BytesIO()
        img.convert("RGB").save(buf, "PNG", optimize=True)
    except Exception as e:
        raise HTTPException(422, f"Cannot render preview: {e}")
    finally:
        if temporary and p is not None:
            p.unlink(missing_ok=True)
    return Response(buf.getvalue(), media_type="image/png", headers={"Cache-Control": "max-age=3600"})


@app.get("/api/runs/{run_id}/stream")
async def stream(run_id: str):
    if not db.get_run(run_id):
        raise HTTPException(404)

    async def gen():
        last = 0
        idle = 0
        while True:
            evs = db.events_after(run_id, last)
            for e in evs:
                last = e["id"]
                yield f"data: {json.dumps(e, default=str)}\n\n"
            run = db.get_run(run_id)
            if run["status"] in ("done", "error") and not evs:
                yield f"event: done\ndata: {json.dumps({'status': run['status'], 'decision': run['decision']})}\n\n"
                return
            idle = 0 if evs else idle + 1
            if idle and idle % 50 == 0:
                yield ": keepalive\n\n"
            await asyncio.sleep(0.15)

    return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/stats")
def stats():
    rows = db.query("SELECT decision, status, total, currency, duration_ms, headline FROM runs")

    def by_cur(rs):  # never add rupees to dollars
        out: dict[str, float] = {}
        for r in rs:
            out[r["currency"] or "USD"] = round(out.get(r["currency"] or "USD", 0) + (r["total"] or 0), 2)
        return out

    done = [r for r in rows if r["status"] in ("done", "error")]
    by = {d: [r for r in done if r["decision"] == d] for d in ("APPROVE", "HOLD", "REJECT")}
    return {
        "total_runs": len(rows),
        "in_progress": len([r for r in rows if r["status"] in ("queued", "running")]),
        "approved": len(by["APPROVE"]), "held": len(by["HOLD"]), "rejected": len(by["REJECT"]),
        "value_approved": by_cur(by["APPROVE"]),
        "value_held": by_cur(by["HOLD"]),
        "value_rejected": by_cur(by["REJECT"]),
        "avg_duration_ms": int(sum(r["duration_ms"] or 0 for r in done) / len(done)) if done else 0,
        "touchless_rate": round(len(by["APPROVE"]) / len(done), 3) if done else 0,
    }


@app.get("/api/pos")
def pos():
    return masterdata.po_overview()


@app.put("/api/ledger/{ledger_id}/payment")
def update_invoice_payment(ledger_id: int, paid: bool):
    payment_status = "paid" if paid else "unpaid"
    with db.transaction():
        result = db.execute("UPDATE ledger SET payment_status=? WHERE id=?", (payment_status, ledger_id))
        if result.rowcount == 0:
            raise HTTPException(404, "Invoice not found")
    return {"id": ledger_id, "payment_status": payment_status}


@app.get("/api/vendors")
def vendors():
    return masterdata.vendors()


@app.post("/api/reset")
def reset():
    if config.USE_BLOB_STORAGE:
        for run in db.query("SELECT stored_path FROM runs WHERE stored_path IS NOT NULL"):
            storage.delete_file(run["stored_path"])
    db.reset()
    return {"ok": True}


@app.get("/samples/{name}")
def sample_file(name: str):
    p = config.SAMPLES_DIR / Path(name).name
    if not p.exists():
        raise HTTPException(404)
    return FileResponse(p)


@app.get("/api/health")
def health():
    return {"ok": True, **pipeline.engine_available()}


app.mount("/", StaticFiles(directory=config.STATIC_DIR, html=True), name="static")
