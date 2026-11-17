"""Orchestrates one invoice run through all stages, emitting live events."""
from __future__ import annotations

import hashlib
import json
import time
import traceback
from dataclasses import asdict
from pathlib import Path

from . import config, db, extract, masterdata, rules, storage
from .normalize import norm_invoice_number

STAGES = [
    ("intake", "Intake", "Receive file, fingerprint it, detect type"),
    ("read", "Read document", "Text layer or OCR for scans and photos"),
    ("extract", "Extract fields", "Gemini (multimodal) or offline parser"),
    ("validate", "Validate", "Completeness, arithmetic, dates, confidence"),
    ("vendor", "Vendor check", "Match against vendor master"),
    ("duplicate", "Duplicate check", "Fingerprint against history"),
    ("po_match", "PO match", "Explicit reference or inferred PO"),
    ("amount", "Amount & tolerance", "Line-level 2-way match and PO balance"),
    ("decision", "Decision", "Approve / Hold / Reject with reasons"),
]


def engine_available() -> dict:
    return {"gemini": bool(config.GEMINI_API_KEY), "ocr": extract.tesseract_available(), "model": config.GEMINI_MODEL}


class Run:
    def __init__(self, run_id: str):
        self.id = run_id
        self.t0 = time.time()

    def start(self, stage: str):
        title = next(s[1] for s in STAGES if s[0] == stage)
        db.add_event(self.id, stage, "start", "running", title)
        self._st = time.time()

    def log(self, stage: str, msg: str, payload=None):
        db.add_event(self.id, stage, "log", None, msg, payload)

    def end(self, stage: str, status: str, summary: str, findings: list | None = None, data: dict | None = None):
        db.add_event(self.id, stage, "end", status, summary, {
            "findings": [asdict(f) if isinstance(f, rules.Finding) else f for f in (findings or [])],
            "data": data or {}, "ms": int((time.time() - self._st) * 1000)})
        time.sleep(config.STAGE_DELAY_S)

    def skip(self, stage: str, why: str):
        self._st = time.time()
        db.add_event(self.id, stage, "end", "skip", why, {"findings": [], "data": {}, "ms": 0})


def _crosscheck(ai: dict, parsed: dict, authoritative_text: bool) -> dict:
    compared, dis = [], []
    for k in ("invoice_number", "total", "po_number", "invoice_date"):
        a, b = ai.get(k), parsed.get(k)
        if a is None or b is None:
            continue
        compared.append(k)
        same = (abs(a - b) < 0.01) if k == "total" else (norm_invoice_number(str(a)) == norm_invoice_number(str(b)) if k == "invoice_number" else str(a) == str(b))
        if not same:
            dis.append({"field": k, "gemini": a, "parser": b})
    return {"compared": compared, "disagreements": dis, "authoritative_text": authoritative_text}


def process(run_id: str) -> None:
    run = db.get_run(run_id)
    r = Run(run_id)
    db.update_run(run_id, status="running")
    findings: list[rules.Finding] = []
    path = None
    temporary = False
    try:
        path, temporary = storage.materialize_path(run["stored_path"])

        # 1. Intake ------------------------------------------------------------
        r.start("intake")
        raw = path.read_bytes()
        sha = hashlib.sha256(raw).hexdigest()
        db.update_run(run_id, sha256=sha)
        r.end("intake", "pass", f"{run['filename']} · {len(raw) / 1024:.0f} KB · {path.suffix.lower().lstrip('.').upper()}",
              data={"filename": run["filename"], "bytes": len(raw), "sha256": sha, "source": run["source"]})

        # 2. Read --------------------------------------------------------------
        r.start("read")
        try:
            doc = extract.read_document(path)
        except extract.ExtractionError as e:
            r.end("read", "fail", str(e))
            return _finish_unreadable(r, run_id, str(e))
        method_label = {"pdf_text_layer": "Digital PDF - text layer extracted", "ocr_tesseract": "Scanned/image - OCR applied (Tesseract)",
                        "scanned_pdf": "Scanned PDF - no text layer", "image": "Image - no text layer"}[doc["method"]]
        if doc["method"] == "ocr_tesseract":
            r.log("read", f"No text layer found; ran {len(doc.get('ocr_variants', []))} OCR passes")
        rstatus = "pass" if doc["text"].strip() else ("warn" if run["engine_used"] == "gemini" else "fail")
        r.end("read", rstatus, method_label + (f" · OCR confidence {doc['ocr_confidence']:.0%}" if doc.get("ocr_confidence") else ""),
              data={"method": doc["method"], "pages": doc["pages"], "chars": len(doc["text"]), "ocr_confidence": doc.get("ocr_confidence"),
                    "text_preview": doc["text"][:3000], "ocr_variants": [{k: v[k] for k in ("variant", "confidence")} for v in doc.get("ocr_variants", [])]})

        # 3. Extract -----------------------------------------------------------
        r.start("extract")
        engine = run["engine_used"]
        parsed, ocr_pick = (extract.best_offline_parse(doc) if doc["text"].strip() else (None, None))
        fields, crosscheck, model_used = None, None, None
        if engine == "gemini":
            r.log("extract", f"Sending original document to {config.GEMINI_MODEL} (multimodal)")
            try:
                fields, model_used = extract.gemini_extract(path, doc["mime"], doc["text"] if doc["has_text_layer"] else "",
                                                            log=lambda m: r.log("extract", m))
                if parsed:
                    crosscheck = _crosscheck(fields, parsed, doc["has_text_layer"])
            except extract.GeminiError as e:
                r.log("extract", f"Gemini unavailable ({e}) - falling back to offline parser", {"detail": getattr(e, "detail", "")[:1500]})
                engine = "offline (fallback)"
                db.update_run(run_id, engine_used=engine)
        if fields is None:
            if parsed is None:
                r.end("extract", "fail", "No readable text and no AI engine available - cannot extract")
                return _finish_unreadable(r, run_id, "Document has no text layer and OCR is unavailable" if not doc["ocr_available"] else "OCR produced no text")
            fields = parsed
        f = fields
        conf = f.get("confidence") or {}
        r.end("extract", "pass" if f.get("total") is not None else "warn",
              f"{'Gemini ' + model_used if model_used else 'Offline parser'} · {len(f.get('line_items') or [])} line item(s) · total {rules.money(f.get('total'), f.get('currency') or 'USD')}",
              data={"engine": engine, "model": model_used, "fields": {k: f.get(k) for k in ("vendor_name", "vendor_tax_id", "invoice_number", "invoice_date", "po_number", "currency", "subtotal", "tax_amount", "tax_inclusive", "total", "document_type")},
                    "line_items": f.get("line_items"), "confidence": conf, "notes": f.get("notes"), "crosscheck": crosscheck, "ocr_pick": ocr_pick})
        db.update_run(run_id, vendor_name=f.get("vendor_name"), invoice_number=f.get("invoice_number"),
                      invoice_number_norm=norm_invoice_number(f.get("invoice_number")), invoice_date=f.get("invoice_date"),
                      total=f.get("total"), currency=f.get("currency"), po_number=f.get("po_number"))

        # 4. Validate ----------------------------------------------------------
        r.start("validate")
        vf = rules.validate(f, doc, crosscheck)
        findings += vf
        r.end("validate", rules.stage_status(vf), _summ(vf, "All document checks passed"), vf)

        # 5. Vendor ------------------------------------------------------------
        r.start("vendor")
        vendor, venf, vinfo = rules.vendor_check(f, doc["text"])
        findings += venf
        r.end("vendor", rules.stage_status(venf), venf[0].title, venf, vinfo)
        if vendor:
            db.update_run(run_id, vendor_id=vendor["vendor_id"], vendor_name=vendor["name"])

        # 6. Duplicates --------------------------------------------------------
        r.start("duplicate")
        df, matches = rules.duplicate_check(f, vendor, sha, run_id)
        findings += df
        r.end("duplicate", rules.stage_status(df), df[0].title, df, {"matches": matches})

        # 7. PO match ----------------------------------------------------------
        po, amount = None, None
        hard_stop = None
        if vendor and vendor["status"] == "blocked":
            hard_stop = "vendor is blocked"
        elif any(x.severity == "reject" for x in df):
            hard_stop = "invoice is a duplicate"
        if hard_stop:
            r.skip("po_match", f"Skipped - {hard_stop}")
            r.skip("amount", f"Skipped - {hard_stop}")
        else:
            r.start("po_match")
            po, pf, pinfo = rules.po_match(f, vendor)
            findings += pf
            if po:
                pinfo["po"] = {k: po[k] for k in ("po_number", "vendor_id", "status", "description", "total_amount", "tax_rate", "currency")}
                pinfo["po"]["lines"] = po["lines"]
                db.update_run(run_id, po_number=po["po_number"])
            r.end("po_match", rules.stage_status(pf), pf[0].title, pf, pinfo)

            # 8. Amounts -------------------------------------------------------
            if po:
                r.start("amount")
                af, amount = rules.amount_check(f, po)
                findings += af
                lead = next((x for x in af if x.severity in ("reject", "hold")), af[0])
                r.end("amount", rules.stage_status(af), lead.title, af, amount)
            else:
                r.skip("amount", "Skipped - no PO to compare against")

        # 9. Decision ----------------------------------------------------------
        r.start("decision")
        result = rules.decide(findings, f, vendor, po, amount)
        if result["decision"] == "APPROVE" and po and amount:
            result["ledger_posted"] = True
        status = {"APPROVE": "pass", "HOLD": "warn", "REJECT": "fail"}[result["decision"]]
        r.end("decision", status, result["headline"], data=result)
        full = {"fields": f, "document": {k: doc.get(k) for k in ("method", "pages", "ocr_confidence", "has_text_layer")},
                "vendor": vinfo, "po": po["po_number"] if po else None, "amount": amount, "crosscheck": crosscheck, **result}
        with db.transaction():
            if result.get("ledger_posted"):
                db.execute("INSERT INTO ledger(run_id, source, vendor_id, invoice_number, invoice_number_norm, invoice_date, po_number, subtotal, total, lines_json, note, created_at)"
                           " VALUES (?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(run_id) WHERE run_id IS NOT NULL DO NOTHING",
                           (run_id, "run", vendor["vendor_id"], f.get("invoice_number"), norm_invoice_number(f.get("invoice_number")), f.get("invoice_date"),
                            po["po_number"], amount["invoice_subtotal"], f.get("total"), json.dumps(amount["ledger_lines"]), "Approved by InvoiceFlow", db.now_iso()))
            db.update_run(run_id, status="done", decision=result["decision"], headline=result["headline"],
                          duration_ms=int((time.time() - r.t0) * 1000), result_json=json.dumps(full, default=str))
    except Exception as e:  # never leave a run hanging
        tb = traceback.format_exc()
        db.add_event(run_id, "decision", "end", "fail", f"Pipeline error: {e}", {"findings": [], "data": {"traceback": tb}, "ms": 0})
        db.update_run(run_id, status="error", decision="HOLD", headline=f"Processing error - routed to manual review ({e})",
                      duration_ms=int((time.time() - r.t0) * 1000), result_json=json.dumps({"error": str(e), "traceback": tb}))
    finally:
        if temporary and path is not None:
            path.unlink(missing_ok=True)


def _summ(fs: list[rules.Finding], ok: str) -> str:
    bad = [x for x in fs if x.severity in ("reject", "hold")]
    if bad:
        return bad[0].title + (f" (+{len(bad) - 1} more)" if len(bad) > 1 else "")
    notes = [x for x in fs if x.severity == "note"]
    return ok + (f" · {len(notes)} note(s)" if notes else "")


def _finish_unreadable(r: Run, run_id: str, why: str) -> None:
    for st in ("extract", "validate", "vendor", "duplicate", "po_match", "amount"):
        if not db.query("SELECT 1 FROM events WHERE run_id=? AND stage=? AND kind='end'", (run_id, st)):
            r.skip(st, "Skipped - document could not be read")
    r.start("decision")
    result = {"decision": "HOLD", "headline": "On hold - document could not be read automatically", "confidence": 0,
              "reasons": [{"rule": "R-DOC-05", "severity": "hold", "title": "Unreadable document", "detail": why, "data": {}}],
              "passed": [], "actions": [{"owner": "AP Clerk", "action": "Key the invoice manually or request a digital copy"}],
              "queue": "AP Clerk", "message": None}
    r.end("decision", "warn", result["headline"], data=result)
    db.update_run(run_id, status="done", decision="HOLD", headline=result["headline"],
                  duration_ms=int((time.time() - r.t0) * 1000), result_json=json.dumps(result))
