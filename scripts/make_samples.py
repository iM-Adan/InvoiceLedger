"""Generate the demo invoice set (text PDFs, a scanned image-only PDF, and a phone photo).

Each vendor uses a deliberately different layout and label vocabulary, so the
extraction step has to cope with real-world variety rather than one template.

Run:  python scripts/make_samples.py
"""
from __future__ import annotations

import io
import json
import random
from pathlib import Path

import pypdfium2 as pdfium
from PIL import Image, ImageEnhance, ImageFilter
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, LETTER
from reportlab.pdfgen import canvas

OUT = Path(__file__).resolve().parent.parent / "samples"
OUT.mkdir(exist_ok=True)
random.seed(7)


def money(v: float, sym: str = "$") -> str:
    return f"{sym}{v:,.2f}"


def draw_invoice(path: Path, spec: dict) -> None:
    size = spec.get("pagesize", LETTER)
    W, H = size
    c = canvas.Canvas(str(path), pagesize=size)
    accent = colors.HexColor(spec.get("accent", "#1f3a5f"))
    style = spec.get("style", "classic")
    L = 50
    y = H - 60

    # ---- Header -------------------------------------------------------------
    if style == "classic":
        c.setFillColor(accent)
        c.rect(0, H - 95, W, 95, fill=1, stroke=0)
        c.setFillColor(colors.white)
        c.setFont("Helvetica-Bold", 20)
        c.drawString(L, H - 50, spec["vendor"])
        c.setFont("Helvetica", 9)
        for i, line in enumerate(spec["vendor_addr"]):
            c.drawString(L, H - 66 - i * 11, line)
        c.setFont("Helvetica-Bold", 26)
        c.drawRightString(W - L, H - 55, spec.get("title", "INVOICE"))
        y = H - 130
    elif style == "minimal":
        c.setFillColor(colors.black)
        c.setFont("Helvetica-Bold", 16)
        c.drawString(L, y, spec["vendor"])
        c.setFont("Helvetica", 9)
        for i, line in enumerate(spec["vendor_addr"]):
            c.drawString(L, y - 14 - i * 11, line)
        c.setFont("Helvetica", 22)
        c.drawRightString(W - L, y, spec.get("title", "Tax Invoice"))
        c.setStrokeColor(accent)
        c.setLineWidth(2)
        c.line(L, y - 50, W - L, y - 50)
        y -= 80
    else:  # "letter" style used by consultancies
        c.setFillColor(accent)
        c.setFont("Times-Bold", 22)
        c.drawCentredString(W / 2, y, spec["vendor"])
        c.setFont("Times-Roman", 10)
        c.setFillColor(colors.black)
        c.drawCentredString(W / 2, y - 16, "  |  ".join(spec["vendor_addr"]))
        c.setFont("Times-Bold", 14)
        c.drawCentredString(W / 2, y - 45, spec.get("title", "INVOICE"))
        y -= 80

    if spec.get("banner"):
        c.setFillColor(colors.HexColor("#b42318"))
        c.setFont("Helvetica-Bold", 11)
        c.drawString(L, y + 8, spec["banner"])
        y -= 14

    # ---- Meta block ---------------------------------------------------------
    font = "Times-Roman" if style == "letter" else "Helvetica"
    bold = "Times-Bold" if style == "letter" else "Helvetica-Bold"
    c.setFillColor(colors.black)
    c.setFont(bold, 10)
    c.drawString(L, y, "Bill To:")
    c.setFont(font, 10)
    for i, line in enumerate(["Northgate Manufacturing Inc.", "Accounts Payable", "400 Commerce Way, Columbus, OH 43215"]):
        c.drawString(L, y - 14 - i * 12, line)
    mx = W - 250
    for i, (k, v) in enumerate(spec["meta"]):
        c.setFont(bold, 10)
        c.drawString(mx, y - i * 15, k)
        c.setFont(font, 10)
        c.drawRightString(W - L, y - i * 15, v)
    y -= max(60, 15 * len(spec["meta"]) + 20)

    # ---- Line items ---------------------------------------------------------
    cols = spec.get("columns", ["Description", "Qty", "Unit Price", "Amount"])
    xs = [L, W - 250, W - 170, W - L]
    c.setFillColor(colors.HexColor("#eef2f7") if style != "letter" else colors.white)
    c.rect(L - 5, y - 6, W - 2 * L + 10, 20, fill=1, stroke=0)
    c.setFillColor(colors.black)
    c.setFont(bold, 10)
    if len(cols) == 2:
        c.drawString(xs[0], y, cols[0])
        c.drawRightString(xs[3], y, cols[1])
    else:
        c.drawString(xs[0], y, cols[0])
        c.drawRightString(xs[1] + 30, y, cols[1])
        c.drawRightString(xs[2] + 40, y, cols[2])
        c.drawRightString(xs[3], y, cols[3])
    y -= 22
    c.setFont(font, 10)
    for item in spec["lines"]:
        if len(cols) == 2:
            c.drawString(xs[0], y, item["desc"])
            c.drawRightString(xs[3], y, money(item["amount"], ""))
        else:
            c.drawString(xs[0], y, item["desc"])
            q = item["qty"]
            c.drawRightString(xs[1] + 30, y, f"{q:g}" if isinstance(q, float) else str(q))
            c.drawRightString(xs[2] + 40, y, money(item["unit"], ""))
            c.drawRightString(xs[3], y, money(item["qty"] * item["unit"], ""))
        y -= 18
    c.setStrokeColor(colors.HexColor("#c9d1db"))
    c.line(L - 5, y + 6, W - L + 5, y + 6)
    y -= 12

    # ---- Totals -------------------------------------------------------------
    for k, v, strong in spec["totals"]:
        c.setFont(bold if strong else font, 11 if strong else 10)
        c.drawRightString(W - 150, y, k)
        c.drawRightString(W - L, y, v)
        y -= 17

    # ---- Footer -------------------------------------------------------------
    y -= 20
    c.setFont(font, 9)
    c.setFillColor(colors.HexColor("#444444"))
    for line in spec.get("footer", []):
        c.drawString(L, y, line)
        y -= 12
    c.showPage()
    c.save()


def simple_totals(lines, tax_rate=0.0, tax_label="Sales Tax", total_label="Total Due", extra=None):
    sub = round(sum(l.get("amount", l.get("qty", 0) * l.get("unit", 0)) for l in lines), 2)
    tax = round(sub * tax_rate, 2)
    rows = [("Subtotal", money(sub), False)]
    if tax_rate:
        rows.append((f"{tax_label} ({tax_rate*100:g}%)", money(tax), False))
    rows.append((total_label, money(sub + tax), True))
    return rows


APEX = dict(vendor="Apex Industrial Supply", accent="#0b4f6c", style="classic",
            vendor_addr=["1180 Foundry Road, Dayton, OH 45402", "ar@apexindustrial.example  |  Tax ID US-84-2219031"])
BRIGHT = dict(vendor="Brightline Components Ltd", accent="#7a3e9d", style="minimal",
              vendor_addr=["Unit 4, Riverside Park, Toledo, OH 43604", "billing@brightline.example"])
MERIDIAN = dict(vendor="Meridian Consulting Group", accent="#8a5a00", style="letter",
                vendor_addr=["55 Lakeview Plaza, Chicago, IL 60601", "finance@meridiancg.example"])
COBALT = dict(vendor="Cobalt Office Solutions Inc.", accent="#1d4ed8", style="classic",
              vendor_addr=["900 Market St, Suite 300, Indianapolis, IN 46204", "invoices@cobaltoffice.example"])
HARBOR = dict(vendor="Harbor Facility Services", accent="#14532d", style="minimal",
              vendor_addr=["17 Wharf Street, Cleveland, OH 44113", "accounts@harborfs.example"])
TIDEWATER = dict(vendor="Tidewater Logistics LLC", accent="#0f766e", style="classic",
                 vendor_addr=["2 Port Access Rd, Norfolk, VA 23510", "billing@tidewaterlog.example"])
QUILLON = dict(vendor="Quillon Print Co.", accent="#9f1239", style="minimal",
               vendor_addr=["310 Press Ave, Akron, OH 44308", "billing@quillonprint.example"])

SAMPLES: list[dict] = []


def add(file, title, category, expected, story, spec):
    SAMPLES.append(dict(file=file, title=title, category=category, expected=expected, story=story, spec=spec))


# 1. Happy path ---------------------------------------------------------------
apex_lines = [
    dict(desc="Nitrile safety gloves (box of 100)", qty=120, unit=18.50),
    dict(desc="Hard hat ANSI Type II", qty=60, unit=32.00),
    dict(desc="Hi-visibility vest Class 2", qty=90, unit=14.00),
    dict(desc="Safety glasses anti-fog", qty=150, unit=6.60),
]
add("01_happy_apex_INV-10482.pdf", "Clean invoice, exact PO match", "Happy path", "APPROVE",
    "Text PDF, explicit PO reference, 4 itemised lines, separate 8% tax. Everything ties out.",
    {**APEX, "meta": [("Invoice No:", "INV-10482"), ("Invoice Date:", "November 10, 2026"),
                      ("PO Number:", "PO-4501"), ("Terms:", "Net 30")],
     "lines": apex_lines, "totals": simple_totals(apex_lines, 0.08),
     "footer": ["Remit to: First Ohio Bank  |  Acct ****4417  |  Routing 041000124", "Thank you for your business."]})

# 2. Tolerance - within --------------------------------------------------------
bl_lines = [dict(desc="Sensor housing SH-11 aluminium", qty=2000, unit=4.20),
            dict(desc="Freight & handling", qty=1, unit=12.00)]
add("02_tolerance_ok_brightline_BL-8120.pdf", "Small freight charge within tolerance", "Amount mismatch + tolerance", "APPROVE",
    "Invoice is $12 over the PO because of an unplanned freight line. Within the 1% / $100 tolerance, so it auto-approves with a note.",
    {**BRIGHT, "title": "Tax Invoice",
     "meta": [("Invoice #", "BL-8120"), ("Date", "12 Nov 2026"), ("Your Order Ref", "PO-4502"), ("Payment", "45 days")],
     "lines": bl_lines, "totals": [("Net Amount", money(8412), False), ("VAT", money(0), False), ("Amount Payable", money(8412), True)],
     "footer": ["Goods shipped 08 Nov 2026 via GroundLine. Bank: Midwest Commerce Bank, Acct ****2290."]})

# 3. Tolerance - outside -------------------------------------------------------
bl2 = [dict(desc="Sensor housing SH-12 aluminium", qty=1500, unit=4.65)]
add("03_tolerance_breach_brightline_BL-8164.pdf", "Unit price above PO price", "Amount mismatch + tolerance", "HOLD",
    "Vendor billed $4.65/unit against a PO price of $4.20 - a $675 (10.7%) variance. Outside tolerance, so it is held with a line-level breakdown.",
    {**BRIGHT, "title": "Tax Invoice",
     "meta": [("Invoice #", "BL-8164"), ("Date", "14 Nov 2026"), ("Your Order Ref", "PO-4503"), ("Payment", "45 days")],
     "lines": bl2, "totals": [("Net Amount", money(6975), False), ("VAT", money(0), False), ("Amount Payable", money(6975), True)],
     "footer": ["Price reflects aluminium surcharge effective 01 Nov 2026."]})

# 4. Duplicate (re-sent with different number format & date format) ------------
bl3 = [dict(desc="Connector kit CK-220", qty=250, unit=17.25)]
add("04_duplicate_brightline_BL7781.pdf", "Resubmitted invoice (already paid)", "Duplicate / near-duplicate", "REJECT",
    "Sent again as a 'payment reminder' with the number written BL7781 and a European date. Already paid as BL-7781 - normalised fingerprint catches it.",
    {**BRIGHT, "title": "Tax Invoice", "banner": "PAYMENT REMINDER - PLEASE REMIT",
     "meta": [("Invoice #", "BL7781"), ("Date", "28.10.2026"), ("Your Order Ref", "PO-4499"), ("Payment", "45 days")],
     "lines": bl3, "totals": [("Net Amount", money(4312.50), False), ("VAT", money(0), False), ("Amount Payable", money(4312.50), True)],
     "footer": ["Our records show this invoice as outstanding. Please remit at your earliest convenience."]})

# 5. Split PO - part 2 -----------------------------------------------------------
add("05_split_po_meridian_MCG-2026-044.pdf", "Milestone 2 of 3 on a split PO", "Split PO / partial billing", "APPROVE",
    "Second milestone against a $30,000 PO that already has $12,000 billed. Fits the remaining balance, so it approves and the PO drops to $6,000 remaining.",
    {**MERIDIAN, "columns": ["Description", "Amount"],
     "meta": [("Invoice Number:", "MCG-2026-044"), ("Date:", "2026-11-09"), ("Purchase Order:", "PO-4504")],
     "lines": [dict(desc="ERP rollout advisory - Phase 2 of 3 (milestone)", amount=12000.00)],
     "totals": [("Total", money(12000), True)],
     "footer": ["Milestone: Phase 2 sign-off received 06 Nov 2026.", "Payment terms: Net 30. Wire to Lakeshore Trust, Acct ****8830."]})

# 6. Split PO - over-billing -----------------------------------------------------
add("06_split_po_overbill_meridian_MCG-2026-052.pdf", "Final milestone exceeds PO balance", "Split PO / partial billing", "HOLD",
    "Phase 3 plus an out-of-scope 'data migration' line. Cumulative billing would exceed the $30,000 PO by $3,000 - held for a PO change order. (Run after sample 05.)",
    {**MERIDIAN, "columns": ["Description", "Amount"],
     "meta": [("Invoice Number:", "MCG-2026-052"), ("Date:", "2026-11-16"), ("Purchase Order:", "PO-4504")],
     "lines": [dict(desc="ERP rollout advisory - Phase 3 of 3 (milestone)", amount=6000.00),
               dict(desc="Additional data migration support", amount=3000.00)],
     "totals": [("Total", money(9000), True)],
     "footer": ["Payment terms: Net 30. Wire to Lakeshore Trust, Acct ****8830."]})

# 7. Missing PO - inferable --------------------------------------------------------
cob = [dict(desc="Height-adjustable desk 60in", qty=12, unit=545.00),
       dict(desc="Ergonomic task chair", qty=12, unit=100.00)]
add("07_missing_po_cobalt_CO-55190.pdf", "No PO reference - inferred from lines", "Missing PO / unknown vendor", "APPROVE",
    "No PO number anywhere on the invoice. The vendor has two open POs; line items and amount match PO-4505 uniquely, so the PO is inferred with high confidence.",
    {**COBALT, "meta": [("Invoice:", "CO-55190"), ("Issued:", "Nov 13, 2026"), ("Account:", "NGM-0042")],
     "lines": cob, "totals": simple_totals(cob, 0.0725, "Tax", "Balance Due"),
     "footer": ["Delivered and installed 12 Nov 2026."]})

# 8. Unknown vendor --------------------------------------------------------------
tw = [dict(desc="Expedited LTL freight - Columbus to Norfolk", qty=1, unit=2150.00)]
add("08_unknown_vendor_tidewater_TWL-0091.pdf", "Vendor not in vendor master", "Missing PO / unknown vendor", "HOLD",
    "Looks legitimate, but Tidewater Logistics has never been onboarded and PO-4509 does not exist. Routed to procurement for vendor onboarding before any payment.",
    {**TIDEWATER, "meta": [("Invoice No:", "TWL-0091"), ("Invoice Date:", "November 12, 2026"), ("PO Number:", "PO-4509"), ("Terms:", "Due on receipt")],
     "lines": tw, "totals": simple_totals(tw, 0, total_label="Total Due"),
     "footer": ["NEW REMITTANCE DETAILS: Coastal Federal, Acct ****6612"]})

# 9. Blocked vendor ----------------------------------------------------------------
qp = [dict(desc="Tri-fold brochure printing", qty=5000, unit=0.42)]
add("09_blocked_vendor_quillon_QP-3310.pdf", "Vendor is blocked in master data", "Missing PO / unknown vendor", "REJECT",
    "Quillon Print Co. is on the vendor master but blocked (failed bank verification). No PO. Rejected outright.",
    {**QUILLON, "title": "Invoice", "meta": [("Invoice #", "QP-3310"), ("Date", "10 Nov 2026")],
     "lines": qp, "totals": [("Subtotal", money(2100), False), ("Total", money(2100), True)],
     "footer": ["Please pay to our new account: Acct ****0093"]})

# 10. Missing critical fields ----------------------------------------------------
ap2 = [dict(desc="Steel shelving unit 72in", qty=20, unit=149.00)]
add("10_missing_fields_apex.pdf", "No invoice number or date", "Missing data", "HOLD",
    "Pro-forma style document: PO matches perfectly but there is no invoice number and no date, so it cannot be booked. Held with a request to the vendor.",
    {**APEX, "title": "STATEMENT", "meta": [("PO Number:", "PO-4510"), ("Terms:", "Net 30")],
     "lines": ap2, "totals": simple_totals(ap2, 0.08),
     "footer": ["Remit to: First Ohio Bank  |  Acct ****4417"]})

# 11/12. Scanned invoices --------------------------------------------------------
add("11_scanned_harbor_HFS-11-2026.pdf", "Scanned paper invoice (image-only PDF)", "Scanned input", "APPROVE",
    "No text layer at all - a photocopied page. Text is recovered with OCR (Gemini vision online, Tesseract offline) and then flows through the same rules.",
    {**HARBOR, "title": "Invoice", "meta": [("Invoice No:", "HFS-11-2026"), ("Invoice Date:", "Nov 3, 2026"), ("PO Number:", "PO-4507")],
     "lines": [dict(desc="Janitorial services - monthly (November)", qty=1, unit=3250.00)],
     "totals": [("Subtotal", money(3250), False), ("Total Due", money(3250), True)],
     "footer": ["Net 15. Thank you!"], "_scan": "pdf"})
add("12_photo_harbor_HFS-PC-0412.png", "Phone photo of an invoice (PNG)", "Scanned input", "APPROVE",
    "A slightly skewed phone photo emailed in as a PNG. Same OCR path; PO-4508 matches exactly.",
    {**HARBOR, "title": "Invoice", "meta": [("Invoice No:", "HFS-PC-0412"), ("Invoice Date:", "Nov 4, 2026"), ("PO Number:", "PO-4508")],
     "lines": [dict(desc="Pest control service - quarterly", qty=1, unit=640.00)],
     "totals": [("Subtotal", money(640), False), ("Total Due", money(640), True)],
     "footer": ["Net 15. Thank you!"], "_scan": "photo"})


def draw_gst_invoice(path: Path) -> None:
    """Indian GST tax invoice: HSN column, CGST/SGST split, Indian digit grouping,
    a 'Total' row that carries a quantity before the amount, and a grand total without paise."""
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    fdir = Path(__file__).resolve().parent / "fonts"
    pdfmetrics.registerFont(TTFont("DV", str(fdir / "DejaVuSans.ttf")))
    pdfmetrics.registerFont(TTFont("DVB", str(fdir / "DejaVuSans-Bold.ttf")))
    W, H = A4
    c = canvas.Canvas(str(path), pagesize=A4)
    L, R = 40, W - 40
    c.setFont("DVB", 9); c.drawRightString(R, H - 30, "ORIGINAL FOR RECIPIENT")
    c.setFont("DVB", 16); c.drawCentredString(W / 2, H - 55, "TAX INVOICE")
    c.setFont("DVB", 13); c.drawString(L, H - 85, "Sahyadri Packaging Pvt Ltd")
    c.setFont("DV", 8.5)
    for i, t in enumerate(["Plot 14, MIDC Bhosari, Pune, Maharashtra 411026", "GSTIN: 27AAHCS4821K1Z6   |   PAN: AAHCS4821K",
                           "accounts@sahyadripack.example   |   +91 20 2712 4400"]):
        c.drawString(L, H - 99 - i * 11, t)
    y = H - 150
    c.rect(L, y - 62, R - L, 72, stroke=1, fill=0)
    c.setFont("DVB", 9); c.drawString(L + 6, y, "Buyer (Bill to):")
    c.setFont("DV", 9)
    for i, t in enumerate(["Northgate Manufacturing India Pvt Ltd", "Chakan Industrial Area, Pune 410501", "GSTIN: 27AACCN7710Q1Z2"]):
        c.drawString(L + 6, y - 13 - i * 12, t)
    meta = [("Invoice No.:", "SPP/26-27/0418"), ("Dated:", "05/11/2026"), ("PO No.:", "PO-4511"), ("Place of Supply:", "27-Maharashtra")]
    for i, (k, v) in enumerate(meta):
        c.setFont("DVB", 9); c.drawString(W / 2 + 30, y - i * 13, k)
        c.setFont("DV", 9); c.drawRightString(R - 6, y - i * 13, v)
    y -= 90
    cols = [(L + 4, "S.No", "l"), (L + 40, "Description of Goods", "l"), (L + 270, "HSN", "l"), (L + 345, "Qty", "r"),
            (L + 375, "Unit", "l"), (L + 440, "Rate", "r"), (R - 4, "Amount", "r")]
    c.setFillColor(colors.HexColor("#eeeeee")); c.rect(L, y - 5, R - L, 18, fill=1, stroke=0); c.setFillColor(colors.black)
    c.setFont("DVB", 9)
    for x, t, a in cols:
        (c.drawRightString if a == "r" else c.drawString)(x, y, t)
    rows = [("1", "Corrugated carton 5-ply 18x12x12in", "4819", "2,000", "Nos", "42.50", "85,000.00"),
            ("2", "BOPP packing tape 48mm x 65m", "3919", "300", "Rolls", "65.00", "19,500.00")]
    c.setFont("DV", 9)
    for r_ in rows:
        y -= 18
        for (x, _, a), t in zip(cols, r_):
            (c.drawRightString if a == "r" else c.drawString)(x, y, t)
    y -= 14; c.line(L, y + 8, R, y + 8)
    c.setFont("DVB", 9); c.drawString(L + 40, y - 4, "Total"); c.drawRightString(L + 345, y - 4, "2,300"); c.drawRightString(R - 4, y - 4, "1,04,500.00")
    y -= 30
    tot = [("Taxable Value", "1,04,500.00"), ("CGST @ 9%", "9,405.00"), ("SGST @ 9%", "9,405.00"), ("Round Off", "0.00")]
    c.setFont("DV", 9.5)
    for k, v in tot:
        c.drawRightString(R - 120, y, k); c.drawRightString(R - 4, y, v); y -= 15
    c.setFont("DVB", 11); c.drawRightString(R - 120, y - 2, "Grand Total"); c.drawRightString(R - 4, y - 2, "₹ 1,23,310")
    y -= 30
    c.setFont("DV", 9)
    c.drawString(L, y, "Amount Chargeable (in words): Indian Rupees One Lakh Twenty Three Thousand Three Hundred Ten Only")
    c.drawString(L, y - 14, "Bank: HDFC Bank, A/c No. 50200012345678, IFSC HDFC0000123")
    c.drawString(L, y - 28, "Terms: Payment within 30 days. Interest @ 18% p.a. on delayed payments.")
    c.setFont("DVB", 9); c.drawRightString(R, y - 70, "for Sahyadri Packaging Pvt Ltd"); c.setFont("DV", 8); c.drawRightString(R, y - 100, "Authorised Signatory")
    c.showPage(); c.save()


GST_FILE = "13_india_gst_sahyadri_SPP-0418.pdf"
GST_ENTRY = dict(file=GST_FILE, title="Indian GST invoice (INR, CGST/SGST)", category="Other formats", expected="APPROVE",
                 story="Different country, different layout: HSN column, CGST + SGST instead of one tax line, lakh digit grouping, "
                       "DD/MM date, a 'Total' row with a quantity before the amount, and a grand total with no paise.")


def rasterize(pdf_path: Path, scale: float) -> Image.Image:
    doc = pdfium.PdfDocument(str(pdf_path))
    img = doc[0].render(scale=scale).to_pil().convert("RGB")
    doc.close()
    return img


def scanify(img: Image.Image, kind: str) -> Image.Image:
    g = img.convert("L")
    if kind == "pdf":
        g = g.rotate(0.6, expand=True, fillcolor=255)
        g = ImageEnhance.Contrast(g).enhance(1.3).filter(ImageFilter.GaussianBlur(0.6))
        px = g.load()
        w, h = g.size
        for _ in range(int(w * h * 0.0004)):
            x, y = random.randrange(w), random.randrange(h)
            px[x, y] = random.choice([0, 90, 200])
        return g
    # photo: warm paper tint, skew, vignette-ish brightness
    g = g.rotate(-1.4, expand=True, fillcolor=235)
    rgb = Image.merge("RGB", (g.point(lambda v: min(255, v + 8)), g.point(lambda v: min(255, v + 2)), g.point(lambda v: max(0, v - 18))))
    rgb = ImageEnhance.Brightness(rgb).enhance(0.95).filter(ImageFilter.GaussianBlur(0.5))
    return rgb


def main():
    for p in OUT.glob("*"):
        if p.suffix in {".pdf", ".png", ".json"}:
            p.unlink()
    catalog = []
    for s in SAMPLES:
        spec = dict(s["spec"])
        scan = spec.pop("_scan", None)
        target = OUT / s["file"]
        if scan:
            tmp = OUT / "_tmp.pdf"
            draw_invoice(tmp, spec)
            img = scanify(rasterize(tmp, 2.0 if scan == "pdf" else 1.8), scan)
            tmp.unlink()
            if scan == "pdf":
                img.save(target, "PDF", resolution=144)
            else:
                img.save(target, "PNG")
        else:
            draw_invoice(target, spec)
        catalog.append({k: s[k] for k in ("file", "title", "category", "expected", "story")})
    draw_gst_invoice(OUT / GST_FILE)
    catalog.append(GST_ENTRY)
    (OUT / "catalog.json").write_text(json.dumps(catalog, indent=2))
    print(f"Wrote {len(catalog)} samples to {OUT}")


if __name__ == "__main__":
    main()
