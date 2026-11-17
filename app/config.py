"""Runtime configuration. Everything business-relevant is tunable via env vars."""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
SAMPLES_DIR = ROOT / "samples"
STATIC_DIR = ROOT / "static"
VERCEL_RUNTIME = os.getenv("VERCEL") == "1"
USE_BLOB_STORAGE = os.getenv("BLOB_STORAGE", "vercel" if VERCEL_RUNTIME else "local").lower() == "vercel"
DEMO_MODE = os.getenv("DEMO_MODE", "true" if VERCEL_RUNTIME else "false").lower() == "true"
DATABASE_URL = (os.getenv("DATABASE_URL") or os.getenv("POSTGRES_URL") or "").strip()
DEFAULT_STORAGE_DIR = Path(os.getenv("TMPDIR", "/tmp")) / "invoiceflow" if VERCEL_RUNTIME else ROOT / "storage"
STORAGE_DIR = Path(os.getenv("STORAGE_DIR", DEFAULT_STORAGE_DIR))
UPLOAD_DIR = STORAGE_DIR / "uploads"
DB_PATH = STORAGE_DIR / "invoiceflow.db"
STORAGE_DIR.mkdir(parents=True, exist_ok=True)
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)


def _f(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except ValueError:
        return default


# --- AI ----------------------------------------------------------------------
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash").strip()
# Tried in order if the primary model is unavailable (404) or rate limited (429).
GEMINI_FALLBACK_MODELS = [m.strip() for m in os.getenv("GEMINI_FALLBACK_MODELS", "gemini-flash-latest,gemini-2.5-flash-lite").split(",") if m.strip()]
GEMINI_TIMEOUT_S = _f("GEMINI_TIMEOUT_S", 60)
# Free tier is ~10 requests/min per project. We pace calls client-side to stay under it.
GEMINI_RPM = _f("GEMINI_RPM", 8)
# On a per-minute 429, wait up to this long (Gemini tells us how long) and retry once.
GEMINI_MAX_WAIT_S = _f("GEMINI_MAX_WAIT_S", 35)
# After a rate-limit failure, skip Gemini for this long so a batch doesn't stall on every invoice.
GEMINI_COOLDOWN_S = _f("GEMINI_COOLDOWN_S", 60)

# --- Business rules ------------------------------------------------------------
# Price / amount tolerance: variance is acceptable if it is within BOTH limits.
TOLERANCE_PCT = _f("TOLERANCE_PCT", 1.0)          # % of expected PO value
TOLERANCE_ABS = _f("TOLERANCE_ABS", 100.0)        # absolute currency amount
ARITHMETIC_TOLERANCE = _f("ARITHMETIC_TOLERANCE", 1.00)  # rounding slack for line/total maths
TAX_TOLERANCE = _f("TAX_TOLERANCE", 1.00)
MAX_INVOICE_AGE_DAYS = int(_f("MAX_INVOICE_AGE_DAYS", 180))
DUPLICATE_WINDOW_DAYS = int(_f("DUPLICATE_WINDOW_DAYS", 10))
MIN_EXTRACTION_CONFIDENCE = _f("MIN_EXTRACTION_CONFIDENCE", 0.70)
LINE_MATCH_THRESHOLD = _f("LINE_MATCH_THRESHOLD", 0.55)
PO_INFERENCE_MIN_SCORE = _f("PO_INFERENCE_MIN_SCORE", 0.80)
PO_INFERENCE_MIN_MARGIN = _f("PO_INFERENCE_MIN_MARGIN", 0.20)
AUTO_APPROVE_INFERRED_PO = os.getenv("AUTO_APPROVE_INFERRED_PO", "true").lower() == "true"

# Small pause between stages so the live run view is readable in a demo.
STAGE_DELAY_S = _f("STAGE_DELAY_S", 0.45)

# The "today" used for date sanity checks; override to make demos deterministic.
TODAY_OVERRIDE = os.getenv("TODAY", "").strip()

MAX_UPLOAD_MB = _f("MAX_UPLOAD_MB", 15)
ALLOWED_EXT = {".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".webp"}


def rules_summary() -> dict:
    return {
        "tolerance_pct": TOLERANCE_PCT,
        "tolerance_abs": TOLERANCE_ABS,
        "arithmetic_tolerance": ARITHMETIC_TOLERANCE,
        "tax_tolerance": TAX_TOLERANCE,
        "max_invoice_age_days": MAX_INVOICE_AGE_DAYS,
        "duplicate_window_days": DUPLICATE_WINDOW_DAYS,
        "min_extraction_confidence": MIN_EXTRACTION_CONFIDENCE,
        "po_inference_min_score": PO_INFERENCE_MIN_SCORE,
        "po_inference_min_margin": PO_INFERENCE_MIN_MARGIN,
        "auto_approve_inferred_po": AUTO_APPROVE_INFERRED_PO,
    }
