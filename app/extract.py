"""Document reading (text layer / OCR) and field extraction (Gemini or offline parser)."""
from __future__ import annotations

import base64
import json
import re
import shutil
import threading
import time
from pathlib import Path

import httpx
import pdfplumber
import pypdfium2 as pdfium
from PIL import Image, ImageFilter, ImageOps

from . import config
from .normalize import DATE_RE, norm_po, parse_date, parse_money

MIME = {".pdf": "application/pdf", ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".tif": "image/tiff", ".tiff": "image/tiff", ".webp": "image/webp"}


class ExtractionError(Exception):
    pass


class GeminiError(Exception):
    def __init__(self, msg: str, status: int | None = None, retry_after: float | None = None,
                 per_day: bool = False, detail: str = ""):
        super().__init__(msg)
        self.status, self.retry_after, self.per_day, self.detail = status, retry_after, per_day, detail


# ----------------------------------------------------------------------------
# Stage 2: get text out of the document
# ----------------------------------------------------------------------------

def tesseract_available() -> bool:
    return shutil.which("tesseract") is not None


OCR_VARIANTS = [("despeckle+psm6", True, "--psm 6"), ("raw+psm6", False, "--psm 6"), ("raw+psm4", False, "--psm 4")]


def _ocr_image(img: Image.Image, despeckle: bool = True, cfg: str = "--psm 6") -> tuple[str, float]:
    import pytesseract

    g = ImageOps.autocontrast(img.convert("L"))
    if despeckle:
        g = g.filter(ImageFilter.MedianFilter(3))  # removes scanner speckle before OCR
    if g.width < 1600:
        g = g.resize((g.width * 2, g.height * 2), Image.LANCZOS)
    data = pytesseract.image_to_data(g, output_type=pytesseract.Output.DICT, config=cfg)
    confs = [float(c) for c, w in zip(data["conf"], data["text"]) if w.strip() and float(c) >= 0]
    # rebuild text line by line from the same pass
    lines: dict[tuple, list[str]] = {}
    for i, w in enumerate(data["text"]):
        if w.strip():
            lines.setdefault((data["block_num"][i], data["par_num"][i], data["line_num"][i]), []).append(w)
    text = "\n".join(" ".join(ws) for ws in lines.values())
    return text, (sum(confs) / len(confs) / 100.0) if confs else 0.0


def _ocr_variants(images: list[Image.Image]) -> list[dict]:
    """Run several OCR passes. The parser later keeps the one whose numbers reconcile."""
    out = []
    for name, desp, cfg in OCR_VARIANTS:
        texts, confs = [], []
        for img in images:
            t, c = _ocr_image(img, desp, cfg)
            texts.append(t)
            confs.append(c)
        out.append({"variant": name, "text": "\n".join(texts), "confidence": round(sum(confs) / len(confs), 3)})
    return out


def read_document(path: Path) -> dict:
    """Return the document's text and how it was obtained."""
    ext = path.suffix.lower()
    out = {"mime": MIME.get(ext, "application/octet-stream"), "pages": 1, "text": "", "method": None,
           "has_text_layer": False, "ocr_confidence": None, "ocr_available": tesseract_available()}
    if ext == ".pdf":
        try:
            with pdfplumber.open(path) as pdf:
                out["pages"] = len(pdf.pages)
                text = "\n".join((p.extract_text() or "") for p in pdf.pages)
        except Exception as e:  # corrupt / encrypted
            raise ExtractionError(f"Could not open PDF: {e}") from e
        if len(text.strip()) >= 40 * max(1, out["pages"]) // 2:
            out.update(text=text, method="pdf_text_layer", has_text_layer=True)
            return out
        out["method"] = "scanned_pdf"
        if not out["ocr_available"]:
            return out
        doc = pdfium.PdfDocument(str(path))
        images = [doc[i].render(scale=2.0).to_pil() for i in range(min(len(doc), 5))]
        doc.close()
        variants = _ocr_variants(images)
        out.update(text=variants[0]["text"], method="ocr_tesseract", ocr_confidence=variants[0]["confidence"], ocr_variants=variants)
        return out
    # images
    out["method"] = "image"
    if not out["ocr_available"]:
        return out
    try:
        img = Image.open(path)
        img = ImageOps.exif_transpose(img)
    except Exception as e:
        raise ExtractionError(f"Could not open image: {e}") from e
    variants = _ocr_variants([img])
    out.update(text=variants[0]["text"], method="ocr_tesseract", ocr_confidence=variants[0]["confidence"], ocr_variants=variants)
    return out


# ----------------------------------------------------------------------------
# Stage 3a: offline rule-based parser
# ----------------------------------------------------------------------------
CUR_SYM = r"(?:US\$|\$|₹|€|£|Rs\.?|INR|USD|EUR|GBP)"
NUM = r"\d{1,3}(?:,\d{2,3})+(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?"   # 1,04,500.00 · 12,000 · 640 · 4.20
NUM_RE = re.compile(rf"(?<![\w.]){CUR_SYM}?\s?({NUM})(?![\w%])")
MONEY = rf"\(?-?{CUR_SYM}?\s?({NUM})\)?"
TOTAL_LABELS = ["Grand Total", "Total Invoice Value", "Invoice Value", "Net Amount Payable", "Net Payable", "Amount Payable",
                "Total Amount Payable", "Balance Due", "Total Due", "Amount Due", "Invoice Total", "Total Amount", "Total"]
SUBTOTAL_LABELS = ["Subtotal", "Sub-total", "Sub Total", "Taxable Value", "Taxable Amount", "Total Taxable Value",
                   "Total Before Tax", "Net Amount(?!\\s*Payable)", "Net Total"]
INDIAN_TAX = re.compile(r"^\W*(CGST|SGST|IGST|UTGST|Cess)\b", re.I)
STOP_LINE = re.compile(r"^\s*(sub-?\s?total|taxable (value|amount)|net amount|net total|total|grand total|amount payable|"
                       r"net payable|balance due|amount due|sales tax|vat|gst|cgst|sgst|igst|utgst|tax\b|amount in words|rupees)", re.I)
TABLE_HEAD = re.compile(r"^\W*((s\.?\s?no\.?|sr\.?\s?no\.?|#)\s+)?(item\s+)?(description|particulars|item|product|services?|details)\b", re.I)
NON_INVOICE = [("credit_note", r"credit\s*note|credit\s*memo"), ("statement", r"\bstatement\b"),
               ("proforma", r"pro[\s-]?forma"), ("quote", r"\bquot(e|ation)\b"), ("purchase_order", r"^\s*purchase order\s*$")]
UNITS = r"(?:nos?|pcs|pc|units?|ea|each|rolls?|kgs?|gms?|mtrs?|m|ltrs?|l|boxes|box|sets?|pairs?|pkts?|bags?|hrs?|hours?|days?|months?|lots?|jobs?|sq\.?\s?ft|sqm)\.?"
PCT = re.compile(r"\(?@?\s*\d{1,2}(?:\.\d+)?\s?%\s*\)?")
NOT_AMOUNT_LINE = re.compile(r"acct|account|a/c|routing|iban|swift|ifsc|tax id|gstin|pan\b|phone|tel|mob|pin\s*code|\bzip\b", re.I)


def _amounts(line: str) -> list[float]:
    return [v for v in (parse_money(m.group(1)) for m in NUM_RE.finditer(PCT.sub(" ", line))) if v is not None]


def _labelled_amounts(text: str, labels: list[str]) -> list[tuple[float, str]]:
    """All (amount, label) pairs where a label line ends in an amount. The LAST number on the
    line is used: totals rows often carry a quantity total or a rate before the amount."""
    out = []
    for lab in labels:
        pat = re.compile(rf"(?<![A-Za-z\-]){lab}\b", re.I)
        for line in text.splitlines():
            m = pat.search(line)
            if not m or NOT_AMOUNT_LINE.search(line):
                continue
            if lab == "Total" and re.search(r"sub\s*-?\s*total|total\s+(qty|quantity|items?|units?)", line, re.I):
                continue
            nums = _amounts(line[m.end():])
            if nums:
                out.append((nums[-1], lab.split("(")[0]))
    return out


def _find_amount(text: str, labels: list[str]) -> tuple[float | None, str | None]:
    c = _labelled_amounts(text, labels)
    return (c[0][0], c[0][1]) if c else (None, None)


def _fuzzy_total(text: str) -> float | None:
    """OCR-tolerant fallback: 'TotaiDue', 'T0tal' etc. Takes the last such line."""
    best = None
    for line in text.splitlines():
        if re.search(r"t[o0]ta[il1|]", line, re.I) and not re.search(r"sub", line, re.I):
            nums = _amounts(line)
            if nums:
                best = nums[-1]
    return best


def _parse_row(line: str) -> dict | None:
    """Generic table row: description, then numeric columns (HSN, qty, rate, tax %, amount...).
    Finds qty x rate = amount among the trailing numbers, whatever the column layout."""
    tokens = line.split()
    # trailing numeric-ish tokens
    i = len(tokens)
    while i > 0 and re.fullmatch(rf"{CUR_SYM}?\(?-?[\d,]+(\.\d+)?\)?%?|{CUR_SYM}|[xX@]|{UNITS}", tokens[i - 1], re.I):
        i -= 1
    nums_tok = [(k, t) for k, t in enumerate(tokens[i:], start=i) if re.search(r"\d", t) and not t.endswith("%")]
    nums = [(k, parse_money(t)) for k, t in nums_tok]
    nums = [(k, v) for k, v in nums if v is not None]
    if not nums or i == 0:
        return None
    desc_end = i
    amount = nums[-1][1]
    found = None
    # try qty * rate == some later number (amount may be followed by tax columns)
    for a_idx in range(len(nums) - 1, 0, -1):
        a = nums[a_idx][1]
        for x in range(a_idx):
            for y in range(x + 1, a_idx):
                q, r_ = nums[x][1], nums[y][1]
                if q and r_ and a and abs(q * r_ - a) <= max(0.05, 0.005 * a):
                    found = (x, q, r_, a)
                    break
            if found:
                break
        if found:
            break
    if found:
        x, q, r_, a = found
        desc_end = nums[x][0]
        amount = a
    desc_toks = tokens[:desc_end]
    # drop a trailing HSN/SAC code (4-8 digit integer) and a leading serial number
    if len(desc_toks) > 1 and re.fullmatch(r"\d{4,8}", desc_toks[-1]):
        desc_toks = desc_toks[:-1]
    if len(desc_toks) > 1 and re.fullmatch(r"\d{1,3}[.)]?", desc_toks[0]):
        desc_toks = desc_toks[1:]
    desc = " ".join(desc_toks).strip(" -:|")
    if not re.search(r"[A-Za-z]{2,}", desc):
        return None
    if found:
        return {"description": desc, "quantity": q, "unit_price": r_, "amount": a}
    if len(nums) == 1 or (len(nums) >= 1 and nums[-1][1] >= max(v for _, v in nums)):
        return {"description": desc, "quantity": 1.0, "unit_price": amount, "amount": amount}
    return None


def _parse_lines(text: str) -> list[dict]:
    lines = [l.strip() for l in text.splitlines()]
    start = next((i for i, l in enumerate(lines) if TABLE_HEAD.match(l)), None)
    region = lines[start + 1:] if start is not None else lines
    items = []
    for l in region:
        if not l:
            continue
        if STOP_LINE.match(l) or re.match(r"^\W*total\b", l, re.I):
            if start is not None:
                break
            continue
        if NOT_AMOUNT_LINE.search(l):
            continue
        row = _parse_row(l)
        if not row:
            continue
        if start is None and row["quantity"] == 1.0 and row["unit_price"] == row["amount"]:
            continue  # without a table header, only trust rows where qty x rate = amount
        items.append(row)
    return items


def offline_parse(text: str, ocr_conf: float | None = None) -> dict:
    """Deterministic parser for common invoice vocab (US/EU and Indian GST layouts).
    Each field carries a confidence."""
    base = 0.92 if ocr_conf is None else max(0.4, min(0.9, 0.55 + 0.4 * ocr_conf))
    conf: dict[str, float] = {}
    notes: list[str] = []
    lines = [l.strip() for l in text.splitlines() if l.strip()]

    currency = "USD"
    if "€" in text or re.search(r"\bEUR\b", text):
        currency = "EUR"
    elif "£" in text or re.search(r"\bGBP\b", text):
        currency = "GBP"
    elif "₹" in text or re.search(r"\bINR\b|\bRs\.?\s?\d|\bGSTIN\b|\bCGST\b|\bIGST\b|\brupees\b", text, re.I):
        currency = "INR"
    dayfirst = currency != "USD"

    # vendor = first meaningful line minus the document title
    vendor = None
    for l in lines[:5]:
        cand = re.sub(r"\b(original\s+for\s+recipient|tax\s+invoice|invoice|statement|receipt|bill(\s+of\s+supply)?)\b\s*$", "", l, flags=re.I)
        cand = re.sub(r"[^A-Za-z0-9&.,'() ]+$", "", cand).strip(" -|:.,")
        if len(cand) >= 3 and not re.search(r"\d{3,}|@|bill to|^(tax\s+)?invoice$|gstin|original|duplicate|triplicate", cand, re.I):
            vendor = cand
            break
    conf["vendor_name"] = round(base * 0.85, 2) if vendor else 0.0

    tax_id = re.search(r"Tax\s*ID\s*[:#]?\s*([A-Z]{2}-[\d\-]{6,})", text, re.I)
    gstin = re.search(r"\b(\d{2}[A-Z]{5}\d{4}[A-Z][1-9A-Z]Z[0-9A-Z])\b", text)

    inv = re.search(r"\b(?:Tax\s+)?(?:Invoice|Bill)\s*(?:No\.?|Number|#)?\s*[:#.\-]\s*#?\s*([A-Z0-9][A-Z0-9\-/]*\d[A-Z0-9\-/]*)", text, re.I)
    if not inv:
        inv = re.search(r"\b(?:Inv(?:oice)?|Bill)\.?\s*(?:No\.?|#)\s*([A-Z0-9\-/]*\d[A-Z0-9\-/]*)", text, re.I)
    conf["invoice_number"] = base if inv else 0.0

    d_raw, ambiguous = None, False
    m = re.search(r"(?:Invoice\s+Date|Date\s+of\s+(?:Issue|Invoice)|Bill\s+Date|Issued|Dated|\bDate)\s*[:\-]?\s*" + DATE_RE.pattern, text, re.I)
    if m:
        d_raw = m.group(1)
        conf["invoice_date"] = base
    inv_date, ambiguous = parse_date(d_raw, dayfirst=dayfirst)
    if not inv_date:
        conf["invoice_date"] = 0.0
    elif ambiguous and dayfirst:
        conf["invoice_date"] = round(base * 0.92, 2)
        notes.append(f"Date '{d_raw}' read as DD/MM ({currency} invoice convention).")
    elif ambiguous:
        conf["invoice_date"] = 0.6
        notes.append(f"Date '{d_raw}' is ambiguous (MM/DD vs DD/MM); assumed US MM/DD format.")

    po = None
    m = re.search(r"(?:P\.?O\.?\s*(?:Number|No\.?|#|Ref(?:erence)?)?|Purchase\s+Order(?:\s+(?:No\.?|Number|#))?|Order\s+Ref(?:erence)?)\s*[:#]?\s*(PO[\s\-]?\d{3,}|\d{4,})", text, re.I)
    if m:
        po = norm_po(m.group(1))
        conf["po_number"] = base
    else:
        m = re.search(r"\bPO[\-\s]?(\d{4,})\b", text)
        if m:
            po = f"PO-{m.group(1)}"
            conf["po_number"] = base * 0.8
        else:
            conf["po_number"] = 0.0

    items = _parse_lines(text)
    conf["line_items"] = base if items else 0.0
    lines_sum = round(sum(li["amount"] for li in items), 2) if items else None

    subtotal, _ = _find_amount(text, SUBTOTAL_LABELS)

    # tax: Indian split taxes are summed; otherwise a single tax line
    tax = None
    split = [(INDIAN_TAX.match(l).group(1).upper(), _amounts(l[INDIAN_TAX.match(l).end():])) for l in text.splitlines() if INDIAN_TAX.match(l)]
    split = [(k, a[-1]) for k, a in split if a]
    if split:
        tax = round(sum(a for _, a in split), 2)
        notes.append("Tax components: " + ", ".join(f"{k} {a:,.2f}" for k, a in split))
    else:
        for line in text.splitlines():
            mt = re.search(r"(?<![A-Za-z])(Sales\s+Tax|VAT|GST|Tax)\b(?!\s*(ID|IN|Invoice))", line, re.I)
            if mt and not re.search(r"tax\s+invoice|gstin", line, re.I):
                nums = _amounts(line[mt.end():])
                if nums:
                    tax = nums[-1]
                    break

    # total: among labelled candidates prefer one consistent with subtotal + tax, never one below the subtotal
    cands = _labelled_amounts(text, TOTAL_LABELS)
    floor_ = max(x for x in (subtotal, lines_sum, 0) if x is not None)
    total, tlab = None, None
    if cands:
        expect = (subtotal if subtotal is not None else lines_sum)
        exp_total = round(expect + (tax or 0), 2) if expect is not None else None
        exact = [c for c in cands if exp_total is not None and abs(c[0] - exp_total) <= 1.0]
        plausible = [c for c in cands if c[0] >= floor_ - 0.01]
        pick = exact[0] if exact else (plausible[0] if plausible else None)
        if pick:
            total, tlab = pick
        elif cands:
            notes.append(f"Total candidates {[c[0] for c in cands]} are all below the line-item sum; ignored.")
    conf["total"] = base if total is not None else 0.0
    if total is None:
        ft = _fuzzy_total(text)
        if ft is not None and ft >= floor_ - 0.01:
            total, tlab, conf["total"] = ft, "fuzzy 'total' line", round(base * 0.8, 2)
    if total is None and lines_sum and subtotal is None:
        pass  # leave missing: better a HOLD than a guessed total
    if subtotal is None and items and total is not None and tax is not None and abs(lines_sum + tax - total) <= 1.0:
        subtotal = lines_sum

    doc_type = "invoice"
    head = "\n".join(lines[:6])
    for t, pat in NON_INVOICE:
        if re.search(pat, head, re.I | re.M):
            doc_type = t
            break
    if re.search(r"payment reminder|second notice|overdue", text, re.I):
        notes.append("Document is marked as a payment reminder / resend.")

    return {
        "vendor_name": vendor,
        "vendor_tax_id": (tax_id.group(1) if tax_id else (gstin.group(1) if gstin else None)),
        "invoice_number": inv.group(1).strip() if inv else None,
        "invoice_date": inv_date.isoformat() if inv_date else None,
        "invoice_date_raw": d_raw,
        "po_number": po,
        "currency": currency,
        "subtotal": subtotal,
        "tax_amount": tax,
        "tax_inclusive": bool(re.search(r"incl(\.|usive|uding)\s+(of\s+)?(all\s+)?(tax|taxes|vat|gst)", text, re.I)),
        "total": total,
        "total_label": tlab,
        "document_type": doc_type,
        "line_items": items,
        "confidence": {k: round(v, 2) for k, v in conf.items()},
        "notes": notes,
    }


def consistency_score(f: dict) -> float:
    """How internally consistent a parse is: fields found + maths that ties out."""
    score = sum(1 for k in ("vendor_name", "invoice_number", "invoice_date", "po_number", "total") if f.get(k))
    lines_sum = round(sum(li["amount"] or 0 for li in f.get("line_items") or []), 2)
    sub, tax, total = f.get("subtotal"), f.get("tax_amount") or 0, f.get("total")
    if f.get("line_items") and sub is not None and abs(lines_sum - sub) <= 0.05:
        score += 2
    if total is not None and abs((sub if sub is not None else lines_sum) + tax - total) <= 0.05:
        score += 3
    return score


def best_offline_parse(doc: dict) -> tuple[dict, dict | None]:
    """Parse the text layer, or every OCR variant and keep the most consistent one."""
    variants = doc.get("ocr_variants")
    if not variants:
        return offline_parse(doc["text"], doc.get("ocr_confidence")), None
    scored = []
    for v in variants:
        f = offline_parse(v["text"], v["confidence"])
        scored.append((consistency_score(f), v["confidence"], f, v))
    scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
    sc, _, f, v = scored[0]
    info = {"chosen": v["variant"], "scores": {x[3]["variant"]: x[0] for x in scored}}
    if len(variants) > 1 and scored[0][0] > scored[-1][0]:
        f["notes"].append(f"OCR passes disagreed; kept '{v['variant']}' because its amounts reconcile.")
    return f, info


# ----------------------------------------------------------------------------
# Stage 3b: Gemini extraction (multimodal: reads text PDFs, scans and photos)
# ----------------------------------------------------------------------------
GEMINI_PROMPT = """You are an accounts-payable extraction engine. Read the attached vendor document and return ONLY a JSON object.

Rules:
- Extract exactly what is printed. Never guess or invent values. Use null when a field is not present.
- invoice_date and due_date as ISO YYYY-MM-DD. If a numeric date is ambiguous (e.g. 03/04/2026) set date_ambiguous=true.
- po_number: the purchase order reference exactly as printed (e.g. "PO-4501"), or null if there is none. Do not treat customer account numbers as PO numbers.
- Amounts are plain numbers without currency symbols or thousands separators.
- subtotal is pre-tax. tax_amount is the tax charged (0 if explicitly zero, null if not shown). tax_inclusive=true only if prices are stated as including tax.
- line_items: every billable line. If a line has only an amount, set quantity=1 and unit_price=amount.
- document_type: one of invoice, credit_note, statement, proforma, quote, purchase_order, other.
- confidence: your 0..1 confidence for each listed key field (lower it for blurry, handwritten or partially cut-off values).
- notes: short observations an AP clerk should know (e.g. "marked as payment reminder", "new bank details printed", "handwritten correction on total").

JSON shape:
{"vendor_name": str|null, "vendor_tax_id": str|null, "invoice_number": str|null, "invoice_date": str|null, "date_ambiguous": bool,
 "due_date": str|null, "po_number": str|null, "currency": str|null, "subtotal": number|null, "tax_amount": number|null,
 "tax_inclusive": bool, "total": number|null, "document_type": str,
 "line_items": [{"description": str, "quantity": number|null, "unit_price": number|null, "amount": number|null}],
 "confidence": {"vendor_name": number, "invoice_number": number, "invoice_date": number, "po_number": number, "total": number, "line_items": number},
 "notes": [str]}
"""


_gem_lock = threading.Lock()
_gem_last_call = 0.0
_gem_cooldown_until = 0.0


def gemini_cooldown_remaining() -> int:
    return max(0, int(_gem_cooldown_until - time.time()))


def _throttle(log) -> None:
    """Client-side pacing so we stay under the free-tier requests-per-minute limit."""
    global _gem_last_call
    with _gem_lock:
        gap = 60.0 / max(config.GEMINI_RPM, 1)
        wait = _gem_last_call + gap - time.time()
        if wait > 0:
            if wait >= 1:
                log(f"Pacing Gemini calls ({config.GEMINI_RPM:g}/min): waiting {wait:.1f}s")
            time.sleep(wait)
        _gem_last_call = time.time()


def _parse_error(r: httpx.Response) -> GeminiError:
    status = r.status_code
    msg, retry, per_day, raw = r.text[:300], None, False, r.text[:2000]
    try:
        err = r.json().get("error", {})
        msg = err.get("message", msg)
        for d in err.get("details", []) or []:
            t = d.get("@type", "")
            if t.endswith("RetryInfo") and d.get("retryDelay"):
                retry = float(str(d["retryDelay"]).rstrip("s") or 0)
            if t.endswith("QuotaFailure"):
                for v in d.get("violations", []) or []:
                    if "PerDay" in (v.get("quotaId") or "") or "per day" in (v.get("quotaMetric") or "").lower():
                        per_day = True
    except Exception:
        pass
    if retry is None:
        m = re.search(r"retry in ([\d.]+)s", msg, re.I)
        if m:
            retry = float(m.group(1))
    if status == 429 and re.search(r"per day|PerDay|daily", msg):
        per_day = True
    if status == 429:
        short = "rate limited - " + ("daily quota exhausted" if per_day else "per-minute quota exceeded")
    elif status in (400, 401, 403) and re.search(r"api key|API_KEY|permission|unauthori", msg, re.I):
        short = "API key rejected - check GEMINI_API_KEY"
    elif status == 404:
        short = "model not available"
    else:
        short = msg[:140]
    return GeminiError(f"HTTP {status}: {short}", status=status, retry_after=retry, per_day=per_day, detail=raw)


def _gemini_call(model: str, parts: list[dict]) -> dict:
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    body = {"contents": [{"role": "user", "parts": parts}],
            "generationConfig": {"temperature": 0, "responseMimeType": "application/json"}}
    r = httpx.post(url, json=body, headers={"x-goog-api-key": config.GEMINI_API_KEY},
                   timeout=config.GEMINI_TIMEOUT_S)
    if r.status_code != 200:
        raise _parse_error(r)
    data = r.json()
    try:
        text = "".join(p.get("text", "") for p in data["candidates"][0]["content"]["parts"])
    except (KeyError, IndexError) as e:
        raise GeminiError(f"Empty response ({data.get('promptFeedback') or e})") from e
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.M).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise GeminiError(f"Model returned non-JSON output: {text[:120]}") from e


def gemini_extract(path: Path, mime: str, text_hint: str = "", log=lambda m: None) -> tuple[dict, str]:
    """Send the original file to Gemini. Returns (fields, model_used).

    Rate limits: per-minute quotas are shared by every model in the project, so on a
    per-minute 429 we wait the delay Gemini asks for and retry the SAME model once
    (switching models would hit the same wall). Daily quotas are per model, so on a
    daily 429 we move to the next model. Anything else non-retryable fails fast."""
    global _gem_cooldown_until
    if not config.GEMINI_API_KEY:
        raise GeminiError("GEMINI_API_KEY is not set")
    if gemini_cooldown_remaining():
        raise GeminiError(f"paused for {gemini_cooldown_remaining()}s after a recent rate limit")
    raw = path.read_bytes()
    parts = [{"text": GEMINI_PROMPT},
             {"inline_data": {"mime_type": mime, "data": base64.b64encode(raw).decode()}}]
    if text_hint.strip():
        parts.append({"text": "Text layer extracted locally (may be imperfect, use as a hint only):\n" + text_hint[:6000]})
    errors = []
    models = [config.GEMINI_MODEL, *[m for m in config.GEMINI_FALLBACK_MODELS if m != config.GEMINI_MODEL]]
    for model in models:
        for attempt in range(2):
            _throttle(log)
            try:
                out = _gemini_call(model, parts)
                return _normalise_gemini(out), model
            except httpx.HTTPError as e:
                errors.append(f"{model}: network error ({type(e).__name__})")
                break
            except GeminiError as e:
                errors.append(f"{model}: {e}")
                if e.status == 429 and e.per_day:
                    log(f"{model}: daily quota exhausted - trying next model")
                    break
                if e.status == 429:
                    wait = e.retry_after if e.retry_after is not None else 20.0
                    if attempt == 0 and wait <= config.GEMINI_MAX_WAIT_S:
                        log(f"{model}: per-minute quota hit - Gemini asks to retry in {wait:.0f}s, waiting")
                        time.sleep(wait + 0.5)
                        continue
                    _gem_cooldown_until = time.time() + config.GEMINI_COOLDOWN_S
                    raise GeminiError(f"{model}: rate limited (per-minute quota, project-wide); "
                                      f"Gemini paused {config.GEMINI_COOLDOWN_S:g}s", status=429, detail=e.detail) from e
                if e.status in (500, 502, 503, 504) and attempt == 0:
                    log(f"{model}: server busy ({e.status}) - retrying in 2s")
                    time.sleep(2)
                    continue
                if e.status in (400, 401, 403) and "API key" in str(e):
                    raise
                break  # 404 / other 4xx -> next model
    if any("429" in x for x in errors):
        _gem_cooldown_until = time.time() + config.GEMINI_COOLDOWN_S
    raise GeminiError("; ".join(errors[-3:]))


def _norm_currency(c) -> str:
    t = str(c or "USD").strip().upper()
    for sym, code in (("₹", "INR"), ("RS", "INR"), ("RUPEE", "INR"), ("€", "EUR"), ("EURO", "EUR"), ("£", "GBP"), ("US$", "USD"), ("$", "USD")):
        if t.startswith(sym):
            return code
    return t[:3] if re.fullmatch(r"[A-Z]{3}.*", t) else "USD"


def _normalise_gemini(d: dict) -> dict:
    inv_date, amb = parse_date(d.get("invoice_date")) if d.get("invoice_date") else (None, False)
    items = []
    for li in d.get("line_items") or []:
        amt = parse_money(li.get("amount"))
        q = parse_money(li.get("quantity"))
        u = parse_money(li.get("unit_price"))
        if amt is None and q is not None and u is not None:
            amt = round(q * u, 2)
        if q is None:
            q = 1.0
        if u is None and amt is not None:
            u = amt / q if q else amt
        items.append({"description": (li.get("description") or "").strip(), "quantity": q, "unit_price": u, "amount": amt})
    conf = {k: float(v) for k, v in (d.get("confidence") or {}).items() if isinstance(v, (int, float))}
    for k in ("vendor_name", "invoice_number", "invoice_date", "po_number", "total", "line_items"):
        conf.setdefault(k, 0.9 if d.get(k) else 0.0)
        if not d.get(k) and k != "line_items":
            conf[k] = 0.0
    notes = [n for n in (d.get("notes") or []) if isinstance(n, str)]
    if d.get("date_ambiguous") or amb:
        conf["invoice_date"] = min(conf.get("invoice_date", 0.6), 0.6)
        notes.append("Invoice date is ambiguous (MM/DD vs DD/MM).")
    return {
        "vendor_name": d.get("vendor_name"),
        "vendor_tax_id": d.get("vendor_tax_id"),
        "invoice_number": (str(d["invoice_number"]).strip() if d.get("invoice_number") else None),
        "invoice_date": inv_date.isoformat() if inv_date else None,
        "invoice_date_raw": d.get("invoice_date"),
        "po_number": norm_po(d.get("po_number")) if d.get("po_number") else None,
        "currency": _norm_currency(d.get("currency")),
        "subtotal": parse_money(d.get("subtotal")),
        "tax_amount": parse_money(d.get("tax_amount")),
        "tax_inclusive": bool(d.get("tax_inclusive")),
        "total": parse_money(d.get("total")),
        "document_type": (d.get("document_type") or "invoice").lower(),
        "line_items": items,
        "confidence": {k: round(min(1.0, max(0.0, v)), 2) for k, v in conf.items()},
        "notes": notes,
    }
