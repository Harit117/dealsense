"""W1 enrichment: turn a messy enquiry ("hi sir 2bhk hsr ~85L loan done") into structured fields.

Uses Claude with structured outputs when an API key is configured, and a rule-based
parser otherwise (or if the API call fails), so the pipeline always runs.
"""
import logging
import re
from typing import Literal, Optional

from pydantic import BaseModel, Field

from . import config
from .market import ALIASES, LOCALITIES, canonical_locality

log = logging.getLogger("dealsense.extract")

LAKH = 100_000
CRORE = 10_000_000


class LeadFields(BaseModel):
    name: Optional[str] = Field(None, description="Buyer's name if stated")
    phone: Optional[str] = Field(None, description="10-digit Indian mobile number, digits only")
    email: Optional[str] = None
    budget_min: Optional[float] = Field(None, description="Lower budget bound in INR (absolute rupees)")
    budget_max: Optional[float] = Field(None, description="Upper budget bound in INR (absolute rupees)")
    localities: list[str] = Field(default_factory=list, description="Preferred Bengaluru localities")
    bhk: Optional[int] = Field(None, description="Bedrooms wanted, e.g. 3 for 3BHK")
    property_type: Optional[Literal["apartment", "villa", "plot"]] = None
    urgency_days: Optional[int] = Field(None, description="Wants to buy/move within this many days")
    loan_preapproved: bool = Field(False, description="Home loan sanctioned/pre-approved or paying cash")
    notes: Optional[str] = Field(None, description="Other requirements in a short phrase")


# --------------------------------------------------------------------------- rules

_UNIT = r"(cr|crs|crore|crores|l|lakh|lakhs|lac|lacs|k)"
_NUM = r"(\d+(?:\.\d+)?)"


def _to_inr(value: float, unit: Optional[str]) -> float:
    unit = (unit or "").lower()
    if unit.startswith("cr"):
        return value * CRORE
    if unit in {"l", "lakh", "lakhs", "lac", "lacs"}:
        return value * LAKH
    if unit == "k":
        return value * 1000
    # Bare numbers: interpret small ones the way Indian buyers write them.
    if value < 10:
        return value * CRORE
    if value < 1000:
        return value * LAKH
    return value


def _budget(text: str):
    t = text.lower().replace(",", "")
    rng = re.search(rf"{_NUM}\s*{_UNIT}?\s*(?:-|to|–)\s*{_NUM}\s*{_UNIT}\b", t)
    if rng:
        lo_v, lo_u, hi_v, hi_u = rng.groups()
        hi = _to_inr(float(hi_v), hi_u)
        lo = _to_inr(float(lo_v), lo_u or hi_u)
        if lo > hi:  # "90L - 1.1Cr" style mixed units are handled above; guard anyway
            lo, hi = hi, lo
        return lo, hi
    single = re.search(rf"(under|below|upto|up to|max|within|around|about|approx|~)?\s*(?:rs\.?|inr|₹)?\s*{_NUM}\s*{_UNIT}\b", t)
    if single:
        qualifier, v, u = single.groups()
        amount = _to_inr(float(v), u)
        if qualifier in {"under", "below", "upto", "up to", "max", "within"}:
            return amount * 0.75, amount
        return amount * 0.9, amount * 1.05
    rupees = re.search(r"(?:rs\.?|inr|₹)\s*(\d{6,9})", t)
    if rupees:
        amount = float(rupees.group(1))
        return amount * 0.9, amount * 1.05
    return None, None


def _localities(text: str) -> list:
    t = " " + re.sub(r"[^a-z ]", " ", text.lower()) + " "
    hits = {}
    for alias in sorted(ALIASES, key=len, reverse=True):
        pos = t.find(f" {alias} ")
        if pos >= 0:
            name = ALIASES[alias]
            hits[name] = min(pos, hits.get(name, pos))
            t = t.replace(f" {alias} ", " " * (len(alias) + 2))
    return sorted(hits, key=hits.get)


def _urgency(text: str):
    t = text.lower()
    if re.search(r"\b(urgent|urgently|asap|immediately|this month)\b", t):
        return 20
    m = re.search(r"(?:within|in|next|by)\s*(\d+)\s*(day|days|week|weeks|month|months|mo)\b", t)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        return n if unit.startswith("day") else n * 7 if unit.startswith("week") else n * 30
    if "next month" in t:
        return 45
    if re.search(r"\b(no hurry|just exploring|not urgent|next year)\b", t):
        return 240
    return None


def extract_rules(text: str) -> LeadFields:
    t = text.lower()
    lo, hi = _budget(text)

    bhk = None
    m = re.search(r"(\d)\s*-?\s*(bhk|bed|bedroom|bhks)", t)
    if m:
        bhk = int(m.group(1))

    ptype = None
    if re.search(r"\b(plot|site|land)\b", t):
        ptype = "plot"
    elif re.search(r"\b(villa|independent house|row house)\b", t):
        ptype = "villa"
    elif bhk or re.search(r"\b(flat|apartment|apt)\b", t):
        ptype = "apartment"

    phone = None
    m = re.search(r"(?:\+?91[\s-]?)?([6-9]\d{4}[\s-]?\d{5})", text)
    if m:
        phone = re.sub(r"\D", "", m.group(1))

    email = None
    m = re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", text)
    if m:
        email = m.group(0)

    name = None
    m = re.search(r"\b(?i:i am|i'm|this is|name is|name:)\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]*\.?)?)", text)
    if m:
        name = m.group(1).strip()

    loan = bool(re.search(r"(loan (is )?(approved|sanctioned|done|ready)|pre-?approved|preapproved|full cash|cash buyer|paying cash)", t))

    return LeadFields(
        name=name, phone=phone, email=email, budget_min=lo, budget_max=hi,
        localities=_localities(text), bhk=bhk, property_type=ptype,
        urgency_days=_urgency(text), loan_preapproved=loan,
    )


# --------------------------------------------------------------------------- LLM

SYSTEM_PROMPT = f"""You extract structured buyer requirements from Indian real-estate enquiries \
(portal leads, WhatsApp chats, emails, call notes). Messages are often short, informal, \
Hinglish, and full of abbreviations.

Rules:
- Money is in INR. 1 lakh (L, lac) = 100,000; 1 crore (Cr) = 10,000,000. Return absolute rupees.
- "around X" means roughly 0.9X to 1.05X. "under X" means up to X.
- Map localities to these canonical Bengaluru names when they match: {", ".join(LOCALITIES)}.
  Keep any other locality as written.
- urgency_days: "urgent"/"asap" ~20, "next month" ~45, "in 2 months" 60, "just exploring" ~240.
- loan_preapproved is true for a sanctioned/pre-approved loan or a cash purchase.
- Leave a field null when the message does not say. Do not guess names or phone numbers."""


def extract_llm(text: str) -> LeadFields:
    import anthropic

    client = anthropic.Anthropic()
    kwargs = dict(
        model=config.LLM_MODEL,
        max_tokens=4000,
        system=SYSTEM_PROMPT,
        output_config={"effort": "low"},
        messages=[{"role": "user", "content": text}],
        output_format=LeadFields,
    )
    try:
        # Server-side fallback: if a request is ever declined, the API retries on a fallback model.
        resp = client.beta.messages.parse(
            betas=["server-side-fallback-2026-07-01"], extra_body={"fallbacks": "default"}, **kwargs
        )
    except anthropic.BadRequestError:
        resp = client.beta.messages.parse(**kwargs)
    if resp.stop_reason == "refusal" or resp.parsed_output is None:
        raise RuntimeError(f"LLM extraction unavailable (stop_reason={resp.stop_reason})")
    out = resp.parsed_output
    out.localities = [canonical_locality(l) or l for l in out.localities]
    return out


def extract(text: str) -> tuple[LeadFields, str]:
    """Returns (fields, method) where method is 'llm' or 'rules'."""
    if config.llm_enabled():
        try:
            fields = extract_llm(text)
            # The rules parser is reliable for phone/email; fill any gaps the model left.
            rules = extract_rules(text)
            fields.phone = fields.phone or rules.phone
            fields.email = fields.email or rules.email
            return fields, "llm"
        except Exception as exc:  # network, auth, rate limit ... never block intake
            log.warning("LLM extraction failed, using rules: %s", exc)
    return extract_rules(text), "rules"
