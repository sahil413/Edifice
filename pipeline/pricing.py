"""Asking-price parsing for Kansas site listings.

Only prices the listing itself states are used: LOIS pricing.salePrice / pricingNote / leaseRateNote,
plus an explicit "Sale/Asking/List Price: $X" in the listing description. Nothing is estimated.
$/acre is either taken from the listing (stated per-acre price, or $/sq ft x 43,560) or computed as
total price / listed acres. Values that look wrong are flagged, never silently "fixed".
"""
import re

SQFT_PER_ACRE = 43560
PPA_LOW, PPA_HIGH = 1000, 500000   # outside this range a $/acre figure is flagged for checking

def _num(s):
    try: return float(str(s).replace(",", "").replace("$", "").strip())
    except (TypeError, ValueError): return None

def _clean(s): return re.sub(r"\s+", " ", str(s or "")).strip()

MONEY = r"\$\s?([\d][\d,]*(?:\.\d+)?)"
PER_ACRE_RX = re.compile(r"\$?\s?([\d][\d,]*(?:\.\d+)?)" + r"\s*(?:/|per|an|a)\s*(?:ac\b|acre)", re.I)
PSF_RX = re.compile(MONEY + r"\s*(?:-\s*" + MONEY + r"\s*)?(?:psf|/\s*sq\.?\s*ft|per\s+(?:sq(?:uare)?\.?\s*f(?:oo)?t))", re.I)
TOTAL_RX = re.compile(r"^\s*" + MONEY + r"\s*$")
DESC_TOTAL_RX = re.compile(r"(?:sale|asking|list(?:ing)?)\s+price\s*(?:is|of)?\s*[:\-]?\s*" + MONEY, re.I)
PER_ACRE_WORDS = re.compile(r"^\s*(?:price\s+)?per\s+acre(?:\s+price)?\s*$", re.I)
CALL_RX = re.compile(r"call|contact|broker|realtor|see flyer|inquire|request|upon request", re.I)
NEG_RX = re.compile(r"negotiab", re.I)
AUCTION_RX = re.compile(r"auction", re.I)

def price_info(pricing, acres, description="", acres_suspect=False):
    p = pricing if isinstance(pricing, dict) else {}
    sale = _num(p.get("salePrice"))
    note = _clean(p.get("pricingNote"))
    raw = {"sale": sale, "note": note}   # kept so the change-detection hash matches pre-pricing snapshots
    if note.lower() in ("n/a", "na", "none", "-", "0"): note = ""
    lease = _clean(p.get("leaseRateNote"))
    if lease.lower() in ("n/a", "na", "none", "unknown", "for sale only", "not available for lease", "-"): lease = ""
    flags, basis = [], ""
    total = ppa = None; ppa_low = ppa_high = None; total_computed = ppa_computed = False
    if sale is not None and sale <= 1:          # LOIS uses 0 / 1 as "no price" placeholders
        if sale == 1: flags.append("Sale price entered as $1 (placeholder) — ignored")
        sale = None

    psf = PSF_RX.findall(note)
    psf_vals = [v for a, b in psf for v in (_num(a), _num(b)) if v]
    per_acre = [] if psf_vals else [v for v in (_num(x) for x in PER_ACRE_RX.findall(note)) if v]

    if psf_vals and not per_acre:
        ppa_low, ppa_high = min(psf_vals) * SQFT_PER_ACRE, max(psf_vals) * SQFT_PER_ACRE
        ppa = ppa_low; basis = "listed $/sq ft × 43,560"
        if sale and abs(sale - ppa_low) / ppa_low < 0.02: sale = None   # salePrice repeats the per-acre floor
    if per_acre:
        listed = per_acre[0]
        if len(per_acre) > 1: ppa_low, ppa_high = min(per_acre), max(per_acre)
        if sale and abs(sale - listed) / listed < 0.02:
            ppa = listed; basis = "listed $/acre"
        elif sale and acres and not acres_suspect:
            comp = sale / acres
            if ppa_low is not None and ppa_low * 0.98 <= comp <= ppa_high * 1.02:
                total, ppa, total_computed = sale, comp, False; basis = "total ÷ acres (within listed $/acre range)"
            elif abs(comp - listed) / listed <= 0.15:
                total, ppa = sale, listed; basis = "listed $/acre (total matches)"
            else:
                total, ppa = sale, listed; basis = "listed $/acre"
                flags.append(f"Listed ${listed:,.0f}/acre but total ${sale:,.0f} ÷ {acres:,.0f} ac = ${comp:,.0f}/acre — the price may cover only part of the site")
        else:
            ppa = listed; basis = "listed $/acre"
            if sale: total = sale
    elif sale and PER_ACRE_WORDS.match(note or "x"):
        ppa = sale; basis = "listed $/acre (sale price field marked per acre)"
    elif sale and not psf_vals:
        if acres and not acres_suspect and sale / acres < 300 and sale >= 1000:
            ppa = sale; basis = "sale price field read as $/acre"
            flags.append(f"Sale price ${sale:,.0f} would be ${sale/acres:,.0f}/acre as a total, so it is read as a per-acre price — confirm with the contact")
        else:
            total = sale; basis = "total ÷ acres"
    if total is None and ppa is None and not sale:
        m = TOTAL_RX.match(note) or DESC_TOTAL_RX.search(description or "")
        if m:
            total = _num(m.group(1)); basis = "total ÷ acres"
            if not TOTAL_RX.match(note): basis += " (price from listing description)"
    if ppa is None and total and acres:
        if acres_suspect:
            flags.append(f"Acreage {acres:,.0f} looks like a listing error (possibly square feet), so $/acre is not computed")
        else:
            ppa, ppa_computed = total / acres, True
    if total is None and ppa and acres and not acres_suspect and ppa_low is None:
        total, total_computed = ppa * acres, True
    if ppa is not None and not (PPA_LOW <= ppa <= PPA_HIGH):
        flags.append(f"Price is below ${PPA_LOW:,}/acre, which is under typical Kansas farmland values. Verify with the listing contact." if ppa < PPA_LOW
                     else f"Price is above ${PPA_HIGH:,}/acre, which is unusually high for raw land. Verify with the listing contact.")
    if ppa is not None and acres_suspect and not any("Acreage" in f for f in flags):
        flags.append(f"Acreage {acres:,.0f} looks like a listing error, so totals derived from it are unreliable")

    status = "priced" if ppa is not None or total is not None else ""
    label = ""
    if not status:
        if AUCTION_RX.search(note): label = "Auction"
        elif re.search(r"lease only|for lease", note, re.I): label = "Lease only"
        elif NEG_RX.search(note) or NEG_RX.search(_clean(p.get("leaseTerms"))): label = "Negotiable"
        elif CALL_RX.search(note): label = "Call for price"
        elif lease and not note: label = "Lease only"
    elif NEG_RX.search(note): label = "Negotiable"
    r = lambda v: round(v) if v is not None else None
    return {"raw": raw, "sale": r(sale), "note": note, "lease_note": lease, "total": r(total), "total_computed": total_computed,
            "per_acre": r(ppa), "per_acre_low": r(ppa_low), "per_acre_high": r(ppa_high), "per_acre_computed": ppa_computed,
            "basis": basis, "label": label, "flags": flags, "flagged": bool(flags),
            "source": "LOIS listing pricing fields" + (" / description" if "description" in basis else "")}
