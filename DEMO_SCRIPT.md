# Demo script

## Before recording / before the interview
1. Start the app (`./run.sh` or the deployed URL). Click **Reset demo data** in the sidebar.
2. Check the engine pill in the sidebar. It shows "Gemini connected" if the key is set and "Offline mode" if not. Both work; say which one you're using.
3. Have `samples/` open in a file browser so you can **drag a file in** (this looks more real than clicking Run).
4. Optional: have one invoice of your own (a real PDF) ready as a "surprise" input.

---

## 5-minute video (≈ timings)

**0:00 – 0:30 · Problem and approach**
> "AP teams open each invoice, find the PO, check the numbers and decide. I built InvoiceFlow to do that end to end. One principle drove the design: the AI *reads* the invoice, and deterministic rules *decide*. Every decision traces back to a rule ID, so it's auditable."

**0:30 – 1:30 · Happy path: drag in `01_happy_apex_INV-10482.pdf`**
- Point at the stages lighting up: intake, text layer, extraction.
- Expand **Extract fields**: per-field confidence and line items.
- Expand **Amount & tolerance**: four lines matched to the PO at exact prices, 8% tax matches the PO's tax rate.
- Decision card: **APPROVE**, payment scheduled at Net 30, PO ledger updated.

**1:30 – 2:20 · Edge case 1, tolerance: run `02` then `03`**
- `02`: "A $12 freight line isn't on the PO. Tolerance is the lesser of 1% and $100, so it approves, *with a note* the buyer can see."
- `03`: "The unit price went from $4.20 to $4.65. The line table shows exactly which line drives the $675 variance. It's on **HOLD**, routed to the Buyer, and there's a drafted email to the vendor ready to review. Nothing goes out automatically."

**2:20 – 3:10 · Edge case 2, split PO: run `05` then `06`**
- `05`: "This is the second milestone on a $30k PO. $12k was already billed in the ERP, this invoice adds $12k, so $6k is left. Approved."
- `06`: "The final invoice adds an out-of-scope line. The PO-balance bar goes red: it's $3k over. **HOLD**, routed to Procurement for a change order." Open the **PO ledger** page to show the balance.

**3:10 – 4:00 · Edge case 3, duplicate: drag `01` in again, then run `04`**
- Rerunning `01`: "Same invoice twice: **REJECT**. The identical file hash was caught."
- `04`: "This one is sneakier. A 'payment reminder' with the number written BL7781 and a European date format. The number normalises to the same key as BL-7781, which the ERP already paid, so it's **REJECTED** before any PO logic runs."

**4:00 – 4:45 · Edge case 4, missing PO / unknown vendor: `07`, `08`**
- `07`: "There's no PO number on this invoice. The vendor has two open POs, so the process scores both on line coverage and amount fit. PO-4505 wins 1.00 to 0.00, so it's inferred and approved, with a note asking the vendor to quote POs in future."
- `08`: "This vendor has never been onboarded and the PO number doesn't exist. **HOLD** to the vendor-master team, with an onboarding request drafted."

**4:45 – 5:00 · Dashboard**
- "Every run is here with its decision, value, owner queue and duration. A scanned PDF and a phone photo also run through OCR and land in the same rules. Thanks."

---

## Live interview run order (rehearsed)
1. Reset → `01` happy path (drag and drop) → expand Extract + Amount.
2. `02` → `03` (tolerance).
3. `05` → `06` → PO ledger page (split billing).
4. Drag `01` again → `04` (duplicates).
5. `07` → `08` → `09` (missing PO, unknown vendor, blocked vendor).
6. `13` Indian GST invoice: different layout and currency, CGST+SGST summed, DD/MM date.
7. `11` scanned PDF: expand **Read document** to show 3 OCR passes and why one was kept.
8. Dashboard → filter "Hold" → open a held invoice.
9. If time allows: drop in a real invoice of your own.

## Likely questions and short answers
- **Why not let the LLM decide?** You can't audit a probabilistic decision about money. The LLM handles the messy part (reading). Rules handle the part that has to be exact, repeatable and explainable.
- **What if Gemini is down or rate-limited?** Calls are paced under the free-tier limit. On a per-minute 429 it waits the delay Gemini asks for and retries the same model, because every model shares the project quota. On a daily 429 it switches models. If it's still limited, Gemini is paused for 60 s and invoices use the offline parser. Every step is logged in the trace, and no run fails silently.
- **How do you know extraction is right?** Per-field confidence plus arithmetic reconciliation. On digital PDFs, a second independent parser has to agree on the total. Low confidence → HOLD, not a guess.
- **Why a single worker?** Split billing depends on order: each approval changes the remaining PO balance. At scale I'd move to a queue partitioned by PO.
- **Why is an inferred PO auto-approved?** Only when the match is clearly best (score ≥ 0.80, margin ≥ 0.20). It's one env var (`AUTO_APPROVE_INFERRED_PO=false`) to send these to HOLD instead, if the client's policy says so.
- **Tolerance policy?** Lesser of 1% / $100, both configurable. Under-billing is a favourable variance and gets a note. Over-billing beyond tolerance is held.
