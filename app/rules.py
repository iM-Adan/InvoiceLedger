"""Deterministic AP rules. The LLM reads the document; these rules make the decision.

Every check returns Findings with a severity:
  reject  -> invoice must not be paid (decision REJECT)
  hold    -> a human must look at it (decision HOLD)
  note    -> approvable, but worth recording (approve-with-note)
  pass    -> check passed
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta

from . import config, db, masterdata
from .normalize import norm_invoice_number, norm_vendor, text_similarity

RULES = [
    # id, stage, title, outcome-if-triggered
    ("R-DOC-01", "validate", "Document must be an invoice (not a statement, quote or credit note)", "HOLD"),
    ("R-DOC-02", "validate", "Invoice number, invoice date, vendor and total must be present", "HOLD"),
    ("R-DOC-03", "validate", "Line items must add up to the subtotal, and subtotal + tax to the total", "HOLD"),
    ("R-DOC-04", "validate", "Invoice date may not be in the future or older than the max age", "HOLD"),
    ("R-DOC-05", "validate", "Key fields must be extracted with sufficient confidence", "HOLD"),
    ("R-DOC-06", "validate", "Independent extractions (AI vs text parser) must agree on key fields", "HOLD"),
    ("R-VEN-01", "vendor", "Vendor must exist in the vendor master", "HOLD"),
    ("R-VEN-02", "vendor", "Vendor must not be blocked", "REJECT"),
    ("R-DUP-01", "duplicate", "Same vendor + same normalised invoice number already processed", "REJECT"),
    ("R-DUP-02", "duplicate", "Identical file already processed", "REJECT"),
    ("R-DUP-03", "duplicate", "Same vendor + same amount within the duplicate window, or near-identical number", "HOLD"),
    ("R-PO-01", "po_match", "Referenced PO must exist", "HOLD"),
    ("R-PO-02", "po_match", "Referenced PO must belong to the invoicing vendor", "HOLD"),
    ("R-PO-03", "po_match", "Referenced PO must be open", "HOLD"),
    ("R-PO-04", "po_match", "Without a PO reference, a single PO must be inferable with high confidence", "HOLD"),
    ("R-AMT-01", "amount", "Invoice vs PO variance must be within tolerance (lesser of % and absolute)", "HOLD"),
    ("R-AMT-02", "amount", "Cumulative billing may not exceed the PO total (split / partial billing)", "HOLD"),
    ("R-AMT-03", "amount", "Billed quantity may not exceed remaining PO quantity", "HOLD"),
    ("R-AMT-04", "amount", "PO must have remaining balance", "REJECT"),
    ("R-AMT-05", "amount", "Tax must match the PO tax rate", "HOLD"),
    ("R-AMT-06", "amount", "Invoice currency must match PO currency", "HOLD"),
]
RULE_TITLES = {r[0]: r[2] for r in RULES}


@dataclass
class Finding:
    rule: str
    severity: str  # reject | hold | note | pass
    title: str
    detail: str = ""
    data: dict = field(default_factory=dict)


def today() -> date:
    if config.TODAY_OVERRIDE:
        return date.fromisoformat(config.TODAY_OVERRIDE)
    return date.today()


def money(v: float | None, cur: str = "USD") -> str:
    if v is None:
        return "—"
    sym = {"USD": "$", "EUR": "€", "GBP": "£", "INR": "₹"}.get(cur, cur + " ")
    body = f"{abs(v):,.2f}"
    if cur == "INR":  # lakh grouping: 1,23,310.00
        whole, frac = f"{abs(v):.2f}".split(".")
        head, tail = whole[:-3], whole[-3:]
        head = ",".join([head[max(0, i - 2):i] for i in range(len(head), 0, -2)][::-1]) if head else ""
        body = (head + "," if head else "") + tail + "." + frac
    return f"{'-' if v < 0 else ''}{sym}{body}"


def stage_status(findings: list[Finding]) -> str:
    sev = {f.severity for f in findings}
    if "reject" in sev:
        return "fail"
    if "hold" in sev:
        return "warn"
    if "note" in sev:
        return "note"
    return "pass"


# ----------------------------------------------------------------------------
# Validation
# ----------------------------------------------------------------------------

def validate(f: dict, doc: dict, crosscheck: dict | None) -> list[Finding]:
    out: list[Finding] = []
    cur = f.get("currency") or "USD"
    dt = f.get("document_type") or "invoice"
    if dt != "invoice":
        out.append(Finding("R-DOC-01", "hold", f"Document looks like a {dt.replace('_', ' ')}, not an invoice",
                           "Statements, quotes and pro-formas cannot be booked for payment. Ask the vendor for a tax invoice."))

    missing = [k for k, label in (("invoice_number", "invoice number"), ("invoice_date", "invoice date"),
                                  ("vendor_name", "vendor name"), ("total", "total amount")) if not f.get(k)]
    if missing:
        nice = {"invoice_number": "invoice number", "invoice_date": "invoice date", "vendor_name": "vendor name", "total": "total amount"}
        out.append(Finding("R-DOC-02", "hold", "Missing required field(s): " + ", ".join(nice[m] for m in missing),
                           "Cannot post an invoice without these. A corrected invoice is needed from the vendor.",
                           {"missing": missing}))
    else:
        out.append(Finding("R-DOC-02", "pass", "All required header fields present"))

    items = f.get("line_items") or []
    lines_sum = round(sum(li.get("amount") or 0 for li in items), 2)
    sub, tax, total = f.get("subtotal"), f.get("tax_amount"), f.get("total")
    maths_ok = True
    if items and sub is not None and abs(lines_sum - sub) > config.ARITHMETIC_TOLERANCE:
        maths_ok = False
        out.append(Finding("R-DOC-03", "hold", f"Line items sum to {money(lines_sum, cur)} but subtotal says {money(sub, cur)}",
                           "Possible extraction error, hidden line, or vendor arithmetic error.", {"lines_sum": lines_sum, "subtotal": sub}))
    if total is not None:
        base = sub if sub is not None else (lines_sum if items else None)
        if base is not None:
            expected_total = base if f.get("tax_inclusive") else round(base + (tax or 0), 2)
            if abs(expected_total - total) > config.ARITHMETIC_TOLERANCE:
                maths_ok = False
                out.append(Finding("R-DOC-03", "hold", f"Subtotal + tax = {money(expected_total, cur)} but total says {money(total, cur)}",
                                   "Totals do not reconcile.", {"expected_total": expected_total, "total": total}))
    if maths_ok and total is not None:
        out.append(Finding("R-DOC-03", "pass", "Line items, subtotal, tax and total reconcile",
                           data={"lines_sum": lines_sum, "subtotal": sub, "tax": tax, "total": total}))

    if f.get("invoice_date"):
        d = date.fromisoformat(f["invoice_date"])
        t = today()
        if d > t + timedelta(days=1):
            out.append(Finding("R-DOC-04", "hold", f"Invoice date {d.isoformat()} is in the future", "Likely a typo or a pre-billed invoice."))
        elif (t - d).days > config.MAX_INVOICE_AGE_DAYS:
            out.append(Finding("R-DOC-04", "hold", f"Invoice is {(t - d).days} days old",
                               f"Older than {config.MAX_INVOICE_AGE_DAYS} days - check whether it was already paid or is stale."))
        else:
            out.append(Finding("R-DOC-04", "pass", f"Invoice date {d.isoformat()} is plausible"))

    conf = f.get("confidence") or {}
    low = [k for k in ("invoice_number", "invoice_date", "total", "po_number", "vendor_name")
           if f.get(k) and conf.get(k, 1) < config.MIN_EXTRACTION_CONFIDENCE]
    if low:
        out.append(Finding("R-DOC-05", "hold", "Low extraction confidence on: " + ", ".join(low),
                           "A human should verify these values against the document.", {"fields": {k: conf.get(k) for k in low}}))

    if crosscheck and crosscheck.get("disagreements"):
        sev = "hold" if crosscheck.get("authoritative_text") and any(d["field"] == "total" for d in crosscheck["disagreements"]) else "note"
        out.append(Finding("R-DOC-06", sev, "AI extraction and text parser disagree on: " + ", ".join(d["field"] for d in crosscheck["disagreements"]),
                           "Gemini value used." if sev == "note" else "Total differs between two independent reads of a digital PDF - verify.",
                           crosscheck))
    elif crosscheck and crosscheck.get("compared"):
        out.append(Finding("R-DOC-06", "pass", "AI extraction confirmed by independent text parser on " + ", ".join(crosscheck["compared"])))

    for n in f.get("notes") or []:
        out.append(Finding("NOTE", "note", n))
    return out


# ----------------------------------------------------------------------------
# Vendor
# ----------------------------------------------------------------------------

def vendor_check(f: dict, text: str) -> tuple[dict | None, list[Finding], dict]:
    v, score, method = masterdata.match_vendor(f.get("vendor_name"), text)
    info = {"extracted_name": f.get("vendor_name"), "match_score": round(score, 2), "method": method}
    if not v:
        return None, [Finding("R-VEN-01", "hold", f"Vendor '{f.get('vendor_name') or 'unknown'}' is not in the vendor master",
                              "No payment can be made to a vendor that has not been onboarded (tax ID, bank verification). Route to procurement for onboarding.",
                              info)], info
    info.update(vendor_id=v["vendor_id"], vendor_name=v["name"], status=v["status"], terms=v["payment_terms_days"])
    if v["status"] == "blocked":
        return v, [Finding("R-VEN-02", "reject", f"{v['name']} is BLOCKED in the vendor master",
                           "Vendor failed verification. Invoices from blocked vendors are rejected and escalated to compliance.", info)], info
    out = [Finding("R-VEN-01", "pass", f"Matched to {v['name']} ({v['vendor_id']}) - approved vendor", f"Match via {method}, score {score:.2f}", info)]
    norm = lambda x: re.sub(r"[^A-Z0-9]", "", (x or "").upper())
    if f.get("vendor_tax_id") and v.get("tax_id") and norm(f["vendor_tax_id"]) != norm(v["tax_id"]) \
            and norm(v["tax_id"]) not in norm(text):
        out.append(Finding("R-VEN-01", "hold", f"Tax ID on invoice ({f['vendor_tax_id']}) differs from vendor master ({v['tax_id']})",
                           "Possible impersonation or outdated master data."))
    return v, out, info


# ----------------------------------------------------------------------------
# Duplicates
# ----------------------------------------------------------------------------

def _lev1(a: str, b: str) -> bool:
    if a == b or abs(len(a) - len(b)) > 1:
        return False
    if len(a) == len(b):
        return sum(x != y for x, y in zip(a, b)) == 1
    s, l = (a, b) if len(a) < len(b) else (b, a)
    return any(l[:i] + l[i + 1:] == s for i in range(len(l)))


def duplicate_check(f: dict, vendor: dict | None, sha256: str, run_id: str) -> tuple[list[Finding], list[dict]]:
    num = norm_invoice_number(f.get("invoice_number"))
    total = f.get("total")
    vname = norm_vendor(vendor["name"] if vendor else f.get("vendor_name"))
    vid = vendor["vendor_id"] if vendor else None

    history = []
    for r in db.query("SELECT id, created_at, vendor_id, vendor_name, invoice_number, invoice_number_norm, invoice_date, total, decision, sha256 "
                      "FROM runs WHERE id<>? AND status='done' AND decision IN ('APPROVE','HOLD')", (run_id,)):
        history.append({**r, "source": f"run {r['id'][:8]} ({r['decision'].lower()})"})
    for r in db.query("SELECT vendor_id, invoice_number, invoice_number_norm, invoice_date, total, note FROM ledger WHERE source='seed'"):
        history.append({**r, "vendor_name": (masterdata.vendor_by_id(r["vendor_id"]) or {}).get("name"), "sha256": None,
                        "source": f"ERP history ({r['note']})"})

    def same_vendor(h):
        return (vid and h.get("vendor_id") == vid) or (vname and norm_vendor(h.get("vendor_name")) == vname)

    checked = len(history)
    for h in history:
        if sha256 and h.get("sha256") == sha256:
            return [Finding("R-DUP-02", "reject", f"Identical file already processed ({h['source']})",
                            f"Byte-for-byte the same document as invoice {h['invoice_number'] or '?'}.", {"match": h})], [h]
    for h in history:
        if num and same_vendor(h) and h.get("invoice_number_norm") == num:
            same_amt = total is not None and h.get("total") is not None and abs(h["total"] - total) < 0.01
            return [Finding("R-DUP-01", "reject",
                            f"Duplicate of invoice {h['invoice_number']} already in the system - {h['source']}",
                            f"'{f.get('invoice_number')}' normalises to '{num}', same as '{h['invoice_number']}'"
                            + (f"; amount {money(total)} also identical." if same_amt else "; amount differs - may be a re-issue, still blocked."),
                            {"match": h, "normalised_number": num})], [h]
    suspects = []
    for h in history:
        if not same_vendor(h) or total is None or h.get("total") is None:
            continue
        same_amt = abs(h["total"] - total) < 0.01
        close_num = num and h.get("invoice_number_norm") and _lev1(num, h["invoice_number_norm"])
        close_date = False
        if f.get("invoice_date") and h.get("invoice_date"):
            close_date = abs((date.fromisoformat(f["invoice_date"]) - date.fromisoformat(h["invoice_date"])).days) <= config.DUPLICATE_WINDOW_DAYS
        if same_amt and (close_date or close_num):
            suspects.append(h)
    if suspects:
        h = suspects[0]
        return [Finding("R-DUP-03", "hold", f"Possible duplicate of {h['invoice_number']} ({h['source']})",
                        f"Same vendor and same amount {money(total)}" + (" with a near-identical invoice number." if num and h.get("invoice_number_norm") and _lev1(num, h["invoice_number_norm"]) else f" within {config.DUPLICATE_WINDOW_DAYS} days."),
                        {"match": h})], suspects
    return [Finding("R-DUP-01", "pass", f"No duplicate found across {checked} prior invoices",
                    f"Fingerprint: vendor + normalised number '{num or '—'}' + amount + file hash.")], []


# ----------------------------------------------------------------------------
# PO matching
# ----------------------------------------------------------------------------

def _match_lines(items: list[dict], po: dict) -> list[dict]:
    rows = []
    for li in items:
        best, best_s = None, 0.0
        for pl in po["lines"]:
            s = text_similarity(li.get("description", ""), pl["description"])
            if s > best_s:
                best, best_s = pl, s
        rows.append({"invoice_line": li, "po_line": best if best_s >= config.LINE_MATCH_THRESHOLD else None, "similarity": round(best_s, 2)})
    return rows


def _invoice_subtotal(f: dict) -> float | None:
    if f.get("subtotal") is not None:
        return f["subtotal"]
    if f.get("line_items"):
        return round(sum(li.get("amount") or 0 for li in f["line_items"]), 2)
    if f.get("total") is not None:
        return round(f["total"] - (f.get("tax_amount") or 0), 2)
    return None


def score_po(f: dict, po: dict) -> dict:
    bal = masterdata.po_balance(po["po_number"])
    sub = _invoice_subtotal(f) or 0
    items = f.get("line_items") or []
    rows = _match_lines(items, po) if items else []
    covered = 0.0
    for r in rows:
        pl, li = r["po_line"], r["invoice_line"]
        if pl is None or not li.get("amount"):
            continue
        if pl["billing_type"] == "amount" or (li.get("unit_price") and abs(li["unit_price"] - pl["unit_price"]) <= 0.02 * pl["unit_price"]):
            covered += li["amount"] * r["similarity"]
    coverage = (covered / sub) if (sub and rows) else 0.0
    rem = bal["remaining"]
    amount_fit = 0.0 if rem <= 0 else max(0.0, 1 - abs(sub - rem) / rem) if sub > rem else max(0.0, min(1.0, sub / rem) * 0.6 + 0.4)
    score = 0.65 * coverage + 0.35 * amount_fit if rows else amount_fit * 0.7
    return {"po_number": po["po_number"], "description": po["description"], "remaining": rem, "line_coverage": round(coverage, 2),
            "amount_fit": round(amount_fit, 2), "score": round(min(score, 1.0), 2)}


def po_match(f: dict, vendor: dict | None) -> tuple[dict | None, list[Finding], dict]:
    pos = masterdata.purchase_orders()
    ref = f.get("po_number")
    info: dict = {"reference_on_invoice": ref, "method": None, "candidates": []}
    if ref:
        po = pos.get(ref)
        if not po:
            fnd = [Finding("R-PO-01", "hold", f"{ref} printed on the invoice does not exist",
                           "Could be a typo, a PO from another entity, or a fabricated reference.", info)]
            if vendor:
                cands = [score_po(f, p) for p in pos.values() if p["vendor_id"] == vendor["vendor_id"] and p["status"] == "open"]
                cands.sort(key=lambda c: c["score"], reverse=True)
                info["candidates"] = cands
                if cands and cands[0]["score"] >= config.PO_INFERENCE_MIN_SCORE:
                    fnd[0].detail += f" Closest open PO for this vendor is {cands[0]['po_number']} (score {cands[0]['score']})."
            return None, fnd, info
        info["method"] = "explicit"
        out = []
        if vendor and po["vendor_id"] != vendor["vendor_id"]:
            owner = masterdata.vendor_by_id(po["vendor_id"])
            out.append(Finding("R-PO-02", "hold", f"{ref} belongs to {owner['name'] if owner else po['vendor_id']}, not {vendor['name']}",
                               "Vendor/PO mismatch - possible misdirected or fraudulent invoice."))
        if po["status"] != "open":
            out.append(Finding("R-PO-03", "hold", f"{ref} is {po['status'].upper()}", "Billing against a closed PO needs procurement to reopen it."))
        if not out:
            out.append(Finding("R-PO-01", "pass", f"Explicit reference {ref} found - open PO for this vendor", po["description"]))
        return po, out, info

    # No PO reference -> try to infer it.
    if not vendor:
        return None, [Finding("R-PO-04", "hold", "No PO reference and vendor is unknown - cannot match",
                              "Nothing to match against.", info)], info
    cands = [score_po(f, p) for p in pos.values() if p["vendor_id"] == vendor["vendor_id"] and p["status"] == "open"]
    cands.sort(key=lambda c: c["score"], reverse=True)
    info["candidates"] = cands
    if not cands:
        return None, [Finding("R-PO-04", "hold", f"No PO reference and {vendor['name']} has no open POs",
                              "Treat as a non-PO invoice: needs budget-owner approval.", info)], info
    best = cands[0]
    second = cands[1]["score"] if len(cands) > 1 else 0.0
    if best["score"] >= config.PO_INFERENCE_MIN_SCORE and best["score"] - second >= config.PO_INFERENCE_MIN_MARGIN:
        info["method"] = "inferred"
        po = pos[best["po_number"]]
        sev = "note" if config.AUTO_APPROVE_INFERRED_PO else "hold"
        return po, [Finding("R-PO-04", sev, f"No PO on invoice - inferred {po['po_number']} with confidence {best['score']:.2f}",
                            f"Chosen from {len(cands)} open PO(s) for {vendor['name']}: line coverage {best['line_coverage']:.0%}, amount fit {best['amount_fit']:.0%}. "
                            f"Runner-up scored {second:.2f}. Ask the vendor to quote PO numbers on future invoices.", info)], info
    return None, [Finding("R-PO-04", "hold", "No PO reference and no single PO matches confidently",
                          f"Best candidate {best['po_number']} scored {best['score']:.2f}"
                          + (f" vs runner-up {second:.2f}" if len(cands) > 1 else "") + ". Needs a human to assign the PO.", info)], info


# ----------------------------------------------------------------------------
# Amounts, tolerance, PO balance (split billing)
# ----------------------------------------------------------------------------

def amount_check(f: dict, po: dict) -> tuple[list[Finding], dict]:
    cur = f.get("currency") or "USD"
    bal = masterdata.po_balance(po["po_number"])
    out: list[Finding] = []
    sub = _invoice_subtotal(f) or 0.0

    if f.get("currency") and f["currency"] != po["currency"]:
        out.append(Finding("R-AMT-06", "hold", f"Invoice currency {f['currency']} differs from PO currency {po['currency']}", "FX handling needed."))

    if bal["remaining"] <= 0.01:
        out.append(Finding("R-AMT-04", "reject", f"{po['po_number']} is already fully billed ({money(bal['billed'], cur)} of {money(bal['po_total'], cur)})",
                           "Nothing left to pay on this PO.", {"balance": bal}))

    rows = _match_lines(f.get("line_items") or [], po)
    table = []
    expected_total = 0.0
    variance_total = 0.0
    qty_issues = []
    ledger_lines = []
    for r in rows:
        li, pl = r["invoice_line"], r["po_line"]
        amt = li.get("amount") or 0.0
        if pl is None:
            table.append({"description": li["description"], "qty": li.get("quantity"), "unit_price": li.get("unit_price"), "amount": amt,
                          "po_line": None, "po_description": None, "po_unit_price": None, "expected": 0.0, "variance": amt,
                          "similarity": r["similarity"], "status": "unmatched"})
            variance_total += amt
            continue
        if pl["billing_type"] == "amount":
            exp, var = amt, 0.0
            prior = bal["line_billed"].get(pl["line_no"], {}).get("amount", 0.0)
            ledger_lines.append({"po_line": pl["line_no"], "qty": 0, "amount": amt})
        else:
            q = li.get("quantity") or 0
            exp = round(q * pl["unit_price"], 2)
            var = round(amt - exp, 2)
            prior_q = bal["line_billed"].get(pl["line_no"], {}).get("qty", 0.0)
            if q + prior_q > pl["quantity"] + 1e-6:
                qty_issues.append(f"{pl['description']}: billing {q:g} but only {pl['quantity'] - prior_q:g} of {pl['quantity']:g} remain")
            ledger_lines.append({"po_line": pl["line_no"], "qty": q, "amount": amt})
        expected_total += exp
        variance_total += var
        table.append({"description": li["description"], "qty": li.get("quantity"), "unit_price": li.get("unit_price"), "amount": amt,
                      "po_line": pl["line_no"], "po_description": pl["description"], "po_unit_price": pl["unit_price"],
                      "billing_type": pl["billing_type"], "expected": exp, "variance": var, "similarity": r["similarity"],
                      "status": "ok" if abs(var) < 0.01 else ("over" if var > 0 else "under")})

    if not rows:  # header-level match only
        expected_total = min(sub, bal["remaining"]) if bal["remaining"] > 0 else sub
        variance_total = round(sub - expected_total, 2)
        out.append(Finding("R-AMT-01", "note", "No line detail extracted - header-level match only", "Subtotal compared against remaining PO balance."))

    variance_total = round(variance_total, 2)
    expected_total = round(expected_total, 2)
    tol = round(min(config.TOLERANCE_PCT / 100 * (expected_total or sub), config.TOLERANCE_ABS), 2)
    summary = {"currency": cur, "invoice_subtotal": sub, "expected_from_po": expected_total, "variance": variance_total, "tolerance": tol,
               "variance_pct": round(100 * variance_total / expected_total, 2) if expected_total else None,
               "po_total": bal["po_total"], "billed_before": bal["billed"], "remaining_before": bal["remaining"],
               "remaining_after": round(bal["remaining"] - sub, 2), "prior_invoices": bal["invoices"], "lines": table, "ledger_lines": ledger_lines}

    if variance_total > tol:
        drivers = [f"{t['description']}: {money(t['variance'], cur)}" + (f" ({money(t['unit_price'], cur)} vs PO {money(t['po_unit_price'], cur)}/unit)" if t.get("po_unit_price") and t["status"] == "over" and t.get("billing_type") == "quantity" else " (not on PO)" if t["status"] == "unmatched" else "")
                   for t in table if abs(t["variance"]) >= 0.01]
        out.append(Finding("R-AMT-01", "hold", f"Over PO by {money(variance_total, cur)} ({summary['variance_pct']}%) - tolerance is {money(tol, cur)}",
                           "Drivers: " + "; ".join(drivers), {"drivers": drivers}))
    elif variance_total > 0.01:
        out.append(Finding("R-AMT-01", "note", f"Over PO by {money(variance_total, cur)} - within tolerance of {money(tol, cur)}",
                           "Auto-approved; variance logged for the buyer."))
    elif variance_total < -0.01:
        out.append(Finding("R-AMT-01", "note", f"Billed {money(-variance_total, cur)} below PO pricing", "Favourable variance."))
    else:
        out.append(Finding("R-AMT-01", "pass", "Invoice lines match PO prices exactly"))

    variance_held = any(x.rule == "R-AMT-01" and x.severity == "hold" for x in out)
    if bal["remaining"] > 0.01:
        if sub > bal["remaining"] + tol and variance_held and bal["billed"] <= 0.01:
            pass  # first invoice on the PO: the over-run is fully explained by the price variance above
        elif sub > bal["remaining"] + tol:
            out.append(Finding("R-AMT-02", "hold", f"Cumulative billing would exceed {po['po_number']} by {money(sub - bal['remaining'], cur)}",
                               f"PO {money(bal['po_total'], cur)}, already billed {money(bal['billed'], cur)} across {len(bal['invoices'])} invoice(s), "
                               f"remaining {money(bal['remaining'], cur)}, this invoice {money(sub, cur)}. Needs a PO change order.", summary))
        else:
            label = "Partial billing" if bal["billed"] > 0 or sub < bal["po_total"] - 0.01 else "Full billing"
            out.append(Finding("R-AMT-02", "pass", f"{label}: fits remaining balance - {money(max(bal['remaining'] - sub, 0), cur)} left on PO after this invoice",
                               f"PO {money(bal['po_total'], cur)}; previously billed {money(bal['billed'], cur)}."))

    if qty_issues:
        out.append(Finding("R-AMT-03", "hold", "Billed quantity exceeds remaining PO quantity", "; ".join(qty_issues)))

    tax = f.get("tax_amount")
    rate = po["tax_rate"]
    if not f.get("tax_inclusive"):
        if rate > 0 and tax is not None:
            exp_tax = round(sub * rate, 2)
            if abs(exp_tax - tax) > config.TAX_TOLERANCE:
                out.append(Finding("R-AMT-05", "hold", f"Tax {money(tax, cur)} differs from expected {money(exp_tax, cur)} ({rate * 100:g}% per PO)", ""))
            else:
                out.append(Finding("R-AMT-05", "pass", f"Tax {money(tax, cur)} matches PO rate {rate * 100:g}%"))
        elif rate > 0 and tax is None:
            out.append(Finding("R-AMT-05", "note", f"No tax shown; PO expects {rate * 100:g}%", "May be tax-exempt or embedded - check."))
        elif rate == 0 and tax and tax > 0.01:
            out.append(Finding("R-AMT-05", "hold", f"Tax {money(tax, cur)} charged on a tax-exempt PO", ""))
    return out, summary


# ----------------------------------------------------------------------------
# Decision
# ----------------------------------------------------------------------------
ROUTES = {
    "R-DOC-01": ("AP Clerk", "Request a proper tax invoice from the vendor"),
    "R-DOC-02": ("AP Clerk", "Request a corrected invoice with the missing fields"),
    "R-DOC-03": ("AP Clerk", "Verify the arithmetic against the document"),
    "R-DOC-04": ("AP Clerk", "Confirm the invoice date with the vendor"),
    "R-DOC-05": ("AP Clerk", "Verify low-confidence fields against the original"),
    "R-DOC-06": ("AP Clerk", "Verify the total against the original"),
    "R-VEN-01": ("Vendor Master", "Start vendor onboarding (W-9, bank verification) before payment"),
    "R-VEN-02": ("Compliance", "Escalate - invoice from blocked vendor"),
    "R-DUP-01": ("AP Clerk", "Return to vendor - already processed"),
    "R-DUP-02": ("AP Clerk", "Discard - same file already processed"),
    "R-DUP-03": ("AP Clerk", "Compare with the earlier invoice before paying"),
    "R-PO-01": ("Requester", "Confirm the correct PO with the requester"),
    "R-PO-02": ("Procurement", "Investigate vendor/PO mismatch"),
    "R-PO-03": ("Procurement", "Reopen the PO or reject"),
    "R-PO-04": ("Requester", "Assign the PO manually"),
    "R-AMT-01": ("Buyer", "Approve the price variance or request a credit note"),
    "R-AMT-02": ("Procurement", "Raise a PO change order or dispute the excess"),
    "R-AMT-03": ("Buyer", "Confirm receipt of the extra quantity"),
    "R-AMT-04": ("AP Clerk", "Return to vendor - PO fully billed"),
    "R-AMT-05": ("AP Clerk", "Check tax treatment with the vendor"),
    "R-AMT-06": ("Treasury", "Confirm FX handling"),
}


def decide(findings: list[Finding], f: dict, vendor: dict | None, po: dict | None, amount: dict | None) -> dict:
    rejects = [x for x in findings if x.severity == "reject"]
    holds = [x for x in findings if x.severity == "hold"]
    notes = [x for x in findings if x.severity == "note"]
    passes = [x for x in findings if x.severity == "pass"]
    cur = f.get("currency") or "USD"
    if rejects:
        decision, lead = "REJECT", rejects
    elif holds:
        decision, lead = "HOLD", holds
    else:
        decision, lead = "APPROVE", notes
    conf = f.get("confidence") or {}
    keyconf = [conf.get(k, 0) for k in ("vendor_name", "invoice_number", "invoice_date", "total") if f.get(k)]
    confidence = round(sum(keyconf) / len(keyconf), 2) if keyconf else 0.0

    if decision == "APPROVE":
        terms = vendor["payment_terms_days"] if vendor else 30
        due = (date.fromisoformat(f["invoice_date"]) + timedelta(days=terms)).isoformat() if f.get("invoice_date") else None
        headline = f"Approved for payment - {money(f.get('total'), cur)} to {vendor['name'] if vendor else f.get('vendor_name')}"
        if notes:
            headline += f" (with {len(notes)} note{'s' if len(notes) > 1 else ''})"
        actions = [{"owner": "AP System", "action": f"Post to ERP against {po['po_number'] if po else '—'} and schedule payment for {due} (Net {terms})"}]
    else:
        headline = ("Rejected - " if decision == "REJECT" else "On hold - ") + lead[0].title
        seen, actions = set(), []
        for x in lead:
            owner, act = ROUTES.get(x.rule, ("AP Clerk", "Review"))
            if (owner, act) not in seen:
                seen.add((owner, act))
                actions.append({"owner": owner, "action": act, "rule": x.rule})

    return {
        "decision": decision,
        "headline": headline,
        "confidence": confidence,
        "reasons": [asdict(x) for x in (rejects + holds + notes)],
        "passed": [asdict(x) for x in passes],
        "actions": actions,
        "queue": actions[0]["owner"] if actions else None,
        "message": draft_message(decision, lead, f, vendor, po, amount),
    }


def draft_message(decision: str, lead: list[Finding], f: dict, vendor: dict | None, po: dict | None, amount: dict | None) -> dict | None:
    vname = vendor["name"] if vendor else (f.get("vendor_name") or "Vendor")
    inv = f.get("invoice_number") or "(no number)"
    cur = f.get("currency") or "USD"
    rules = {x.rule for x in lead}
    if decision == "APPROVE":
        return None
    internal = rules <= {"R-DOC-05", "R-DOC-06", "R-PO-02", "R-PO-03", "R-AMT-03", "R-VEN-02", "R-AMT-06", "R-DUP-03", "R-PO-04"}
    if "R-VEN-02" in rules:
        return {"to": "compliance@northgate.example", "subject": f"Blocked vendor invoice received - {vname} {inv}",
                "body": f"An invoice for {money(f.get('total'), cur)} was received from {vname}, which is blocked in the vendor master. "
                        f"It has been rejected and not entered for payment. Please review whether further action is needed."}
    if internal:
        return {"to": "ap-team@northgate.example", "subject": f"Review needed: {vname} invoice {inv}",
                "body": "\n".join(f"- {x.title}. {x.detail}" for x in lead)}
    bullets = []
    for x in lead:
        if x.rule == "R-DOC-02":
            bullets.append("The invoice is missing: " + ", ".join(m.replace("_", " ") for m in x.data.get("missing", [])) + ".")
        elif x.rule == "R-DOC-01":
            bullets.append("We received a statement / non-invoice document. Please send a tax invoice.")
        elif x.rule in ("R-DUP-01", "R-DUP-02"):
            m = x.data.get("match", {})
            bullets.append(f"This invoice matches {m.get('invoice_number')} which is already in our system ({m.get('source', 'processed')}). No further payment is due.")
        elif x.rule == "R-AMT-01":
            bullets.append(f"Pricing differs from {po['po_number'] if po else 'the PO'}: " + "; ".join(x.data.get("drivers", [])) + ". Please issue a corrected invoice or credit note, or confirm an agreed price change.")
        elif x.rule == "R-AMT-02":
            bullets.append(f"This invoice exceeds the remaining balance on {po['po_number'] if po else 'the PO'} ({money(amount['remaining_before'], cur) if amount else ''} remaining). We are checking internally whether a PO amendment is approved.")
        elif x.rule == "R-AMT-04":
            bullets.append(f"{po['po_number'] if po else 'The PO'} has already been fully invoiced.")
        elif x.rule == "R-VEN-01":
            bullets.append("You are not yet set up as a supplier. Please complete our supplier onboarding (W-9, bank verification letter, contact details) before we can process payment.")
        elif x.rule == "R-PO-01":
            bullets.append(f"The PO number on the invoice ({f.get('po_number')}) is not recognised. Please confirm the correct PO with your contact at Northgate.")
        elif x.rule == "R-AMT-05":
            bullets.append(x.title + ". Please confirm the tax treatment.")
        elif x.rule in ("R-DOC-03", "R-DOC-04"):
            bullets.append(x.title + ".")
    if not bullets:
        return None
    to = vendor["contact_email"] if vendor else "the vendor (address from invoice)"
    verb = "cannot be processed" if decision == "REJECT" else "is on hold"
    body = (f"Hello {vname} team,\n\nYour invoice {inv} dated {f.get('invoice_date') or '(undated)'} for {money(f.get('total'), cur)} {verb}:\n\n"
            + "\n".join(f"  • {b}" for b in bullets)
            + "\n\nPlease reply to this email with the requested information and we will process it promptly.\n\nThanks,\nAccounts Payable - Northgate Manufacturing")
    return {"to": to, "subject": f"Invoice {inv} - action needed", "body": body}
