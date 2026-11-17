"""Normalisation helpers shared by extraction and rules."""
from __future__ import annotations

import re
from datetime import date, datetime
from difflib import SequenceMatcher

MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}

STOPWORDS = {"the", "of", "and", "for", "a", "an", "to", "in", "with", "per", "-", "x"}


def norm_invoice_number(s: str | None) -> str:
    """'INV-0010482', 'inv 10482', 'INV#10482' -> '10482'; 'BL-7781' / 'BL7781' -> 'BL7781'."""
    if not s:
        return ""
    t = re.sub(r"[^A-Za-z0-9]", "", s).upper()
    t = re.sub(r"^(INVOICE|INV)(NO|NUM|NUMBER)?(?=[A-Z]*\d)", "", t)
    # strip leading zeros of the numeric tail
    m = re.match(r"^([A-Z]*)(0*)(\d.*)$", t)
    if m:
        t = m.group(1) + m.group(3)
    return t


def norm_vendor(s: str | None) -> str:
    if not s:
        return ""
    t = s.lower()
    t = re.sub(r"[^a-z0-9 ]", " ", t)
    t = re.sub(r"\b(inc|llc|ltd|limited|co|corp|corporation|company|plc|gmbh)\b", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def tokens(s: str) -> set[str]:
    t = re.sub(r"[^a-z0-9 ]", " ", (s or "").lower())
    return {w for w in t.split() if w not in STOPWORDS and len(w) > 1}


def text_similarity(a: str, b: str) -> float:
    """Blend of character similarity and token containment (robust to extra words)."""
    if not a or not b:
        return 0.0
    seq = SequenceMatcher(None, a.lower(), b.lower()).ratio()
    ta, tb = tokens(a), tokens(b)
    if not ta or not tb:
        return seq
    contain = len(ta & tb) / min(len(ta), len(tb))
    return round(max(seq, 0.85 * contain + 0.15 * seq), 3)


def parse_money(s) -> float | None:
    if s is None:
        return None
    if isinstance(s, (int, float)):
        return float(s)
    t = re.sub(r"(?i)US\$|Rs\.?|INR|USD|EUR|GBP|[$₹€£]", "", str(s)).strip().replace(" ", "")
    neg = t.startswith("-") or (t.startswith("(") and t.endswith(")"))
    t = re.sub(r"[^\d.,]", "", t)
    if not t:
        return None
    if "," in t and "." in t and t.rfind(",") > t.rfind("."):  # 1.234,56
        t = t.replace(".", "").replace(",", ".")
    else:
        t = t.replace(",", "")
    try:
        v = float(t)
    except ValueError:
        return None
    return -v if neg else v


DATE_PATTERNS = [
    r"\d{4}-\d{1,2}-\d{1,2}",
    r"\d{4}[/.]\d{1,2}[/.]\d{1,2}",
    r"\d{1,2}[.\-]\d{1,2}[.\-]\d{4}",
    r"\d{1,2}/\d{1,2}/\d{2,4}",
    r"\d{1,2}[ -](?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*[- ,]+\d{4}",
    r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.? ?\d{1,2}(?:st|nd|rd|th)?,? \d{4}",
]
DATE_RE = re.compile(r"(?<!\d)(" + "|".join(DATE_PATTERNS) + r")(?!\d)", re.I)


def parse_date(s: str | None, dayfirst: bool = False) -> tuple[date | None, bool]:
    """Returns (date, ambiguous). dayfirst=True reads 05/11/2026 as 5 Nov (India/EU)."""
    if not s:
        return None, False
    t = s.strip().lower().replace(",", " ")
    t = re.sub(r"\s+", " ", t)
    t = re.sub(r"(?<=\d)(?:st|nd|rd|th)\b", "", t)
    try:
        if re.fullmatch(r"\d{4}-\d{1,2}-\d{1,2}", t):
            return datetime.strptime(t, "%Y-%m-%d").date(), False
        m = re.fullmatch(r"(\d{4})[/.](\d{1,2})[/.](\d{1,2})", t)
        if m:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3))), False
        m = re.fullmatch(r"(\d{1,2})[.\-](\d{1,2})[.\-](\d{4})", t)
        if m:
            a, b, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
            if a > 12:
                return date(y, b, a), False
            if b > 12:
                return date(y, a, b), False
            return date(y, b, a), a != b
        m = re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{2,4})", t)
        if m:
            a, b, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
            y = y + 2000 if y < 100 else y
            if a > 12:
                return date(y, b, a), False
            if b > 12:
                return date(y, a, b), False
            if dayfirst:
                return date(y, b, a), a != b
            return date(y, a, b), a != b  # US default, flag ambiguity
        m = re.fullmatch(r"(\d{1,2})[ -]([a-z]+)[ -]+(\d{4})", t)
        if m:
            return date(int(m.group(3)), MONTHS[m.group(2)[:3]], int(m.group(1))), False
        m = re.fullmatch(r"([a-z]+)\.? ?(\d{1,2}) (\d{4})", t)
        if m:
            return date(int(m.group(3)), MONTHS[m.group(1)[:3]], int(m.group(2))), False
    except (ValueError, KeyError):
        return None, False
    return None, False


def norm_po(s: str | None) -> str | None:
    if not s:
        return None
    m = re.search(r"(?:PO)?[\s\-#:]*(\d{3,})", s.upper())
    return f"PO-{m.group(1)}" if m else None
