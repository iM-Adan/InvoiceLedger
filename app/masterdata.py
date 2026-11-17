"""Procurement master data (the 'PO spreadsheet') + ledger-derived balances."""
from __future__ import annotations

import csv
import json
from functools import lru_cache

from . import config, db
from .normalize import norm_vendor, text_similarity


@lru_cache(maxsize=1)
def vendors() -> list[dict]:
    rows = list(csv.DictReader(open(config.DATA_DIR / "vendors.csv")))
    for r in rows:
        r["aliases"] = [a for a in r["aliases"].split("|") if a]
        r["payment_terms_days"] = int(r["payment_terms_days"])
    return rows


@lru_cache(maxsize=1)
def purchase_orders() -> dict[str, dict]:
    pos = {r["po_number"]: r for r in csv.DictReader(open(config.DATA_DIR / "purchase_orders.csv"))}
    for po in pos.values():
        po["tax_rate"] = float(po["tax_rate"])
        po["total_amount"] = float(po["total_amount"])
        po["lines"] = []
    for ln in csv.DictReader(open(config.DATA_DIR / "po_lines.csv")):
        ln["line_no"] = int(ln["line_no"])
        ln["quantity"] = float(ln["quantity"])
        ln["unit_price"] = float(ln["unit_price"])
        ln["line_total"] = round(ln["quantity"] * ln["unit_price"], 2)
        if ln["po_number"] in pos:
            pos[ln["po_number"]]["lines"].append(ln)
    return pos


def vendor_by_id(vid: str | None) -> dict | None:
    return next((v for v in vendors() if v["vendor_id"] == vid), None)


def match_vendor(name: str | None, full_text: str = "") -> tuple[dict | None, float, str]:
    """Return (vendor, score, method). Name match first, then scan the document text for aliases."""
    best, best_score, method = None, 0.0, "none"
    n = norm_vendor(name)
    if n:
        for v in vendors():
            for cand in [v["name"], *v["aliases"]]:
                c = norm_vendor(cand)
                s = 1.0 if c == n else text_similarity(c, n)
                if s > best_score:
                    best, best_score, method = v, s, f"name ~ '{cand}'"
    if best_score < 0.85 and full_text:
        head = norm_vendor(full_text[:400])
        for v in vendors():
            for cand in [v["name"], *v["aliases"]]:
                c = norm_vendor(cand)
                if len(c) > 5 and c in head and 0.9 > best_score:
                    best, best_score, method = v, 0.9, f"alias '{cand}' found in document header"
    if best_score < 0.85:
        return None, best_score, method
    return best, best_score, method


def ledger_for_po(po_number: str) -> list[dict]:
    rows = db.query("SELECT * FROM ledger WHERE po_number=? ORDER BY id", (po_number,))
    for r in rows:
        r["lines"] = json.loads(r.pop("lines_json") or "[]")
    return rows


def po_balance(po_number: str) -> dict:
    po = purchase_orders()[po_number]
    entries = ledger_for_po(po_number)
    billed = round(sum(e["subtotal"] or 0 for e in entries), 2)
    line_billed: dict[int, dict] = {}
    for e in entries:
        for l in e["lines"]:
            d = line_billed.setdefault(l["po_line"], {"qty": 0.0, "amount": 0.0})
            d["qty"] += l.get("qty") or 0
            d["amount"] += l.get("amount") or 0
    return {
        "po_total": po["total_amount"],
        "billed": billed,
        "remaining": round(po["total_amount"] - billed, 2),
        "invoices": [{"id": e["id"], "invoice_number": e["invoice_number"], "subtotal": e["subtotal"],
                      "date": e["invoice_date"], "source": e["source"], "payment_status": e["payment_status"]} for e in entries],
        "line_billed": line_billed,
    }


def po_overview() -> list[dict]:
    out = []
    for po in purchase_orders().values():
        b = po_balance(po["po_number"])
        v = vendor_by_id(po["vendor_id"])
        out.append({**{k: po[k] for k in ("po_number", "vendor_id", "issue_date", "status", "currency", "tax_rate", "description", "total_amount")},
                    "vendor_name": v["name"] if v else po["vendor_id"], "lines": po["lines"],
                    "settled": bool(b["invoices"]) and all(i["payment_status"] == "paid" for i in b["invoices"]),
                    **{k: b[k] for k in ("billed", "remaining", "invoices")}})
    return out
