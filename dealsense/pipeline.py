"""The four workflows wired together (what the n8n workflows call over HTTP)."""
import json
import logging

from . import actions, db, learning, scoring
from .extract import extract

log = logging.getLogger("dealsense.pipeline")

RETRAIN_EVERY = 5  # broker outcomes between automatic retrains

OUTCOME_LABELS = {"visited": 1, "closed": 1, "override_up": 1, "lost": 0, "override_down": 0}
OUTCOME_EVENTS = {"called": "call", "visited": "visit"}


def _normalise_phone(phone):
    if not phone:
        return None
    digits = "".join(c for c in phone if c.isdigit())
    return digits[-10:] if len(digits) >= 10 else None


# --------------------------------------------------------------------------- W1

def ingest(conn, text: str, source: str = "manual", name=None, phone=None, email=None,
           property_id=None, at=None, act=True) -> dict:
    """W1 intake & enrichment -> W2 scoring -> W3 actions for one incoming message."""
    fields, method = extract(text)
    phone = _normalise_phone(phone) or _normalise_phone(fields.phone)
    email = email or fields.email
    at = at or db.iso(db.now())

    existing = None
    if phone:
        existing = conn.execute("SELECT * FROM leads WHERE phone = ?", (phone,)).fetchone()
    if not existing and email:
        existing = conn.execute("SELECT * FROM leads WHERE email = ?", (email,)).fetchone()

    new_values = {
        "budget_min": fields.budget_min, "budget_max": fields.budget_max, "bhk": fields.bhk,
        "property_type": fields.property_type, "urgency_days": fields.urgency_days, "notes": fields.notes,
    }
    if existing:
        lead = db.lead_from_row(existing)
        merged = {k: v if v is not None else lead[k] for k, v in new_values.items()}
        localities = list(dict.fromkeys(fields.localities + lead["localities"])) if fields.localities else lead["localities"]
        msgs = lead["raw_messages"] + [{"at": at, "text": text, "source": source}]
        conn.execute(
            """UPDATE leads SET budget_min=?, budget_max=?, bhk=?, property_type=?, urgency_days=?, notes=?,
               localities=?, loan_preapproved=?, raw_messages=?, last_activity_at=?, email=COALESCE(email, ?),
               extraction=? WHERE id=?""",
            (merged["budget_min"], merged["budget_max"], merged["bhk"], merged["property_type"],
             merged["urgency_days"], merged["notes"], json.dumps(localities),
             int(lead["loan_preapproved"] or fields.loan_preapproved), json.dumps(msgs), at, email, method, lead["id"]))
        lead_id, deduped = lead["id"], True
    else:
        cur = conn.execute(
            """INSERT INTO leads(name, phone, email, source, raw_messages, budget_min, budget_max, localities, bhk,
               property_type, urgency_days, loan_preapproved, notes, extraction, created_at, last_activity_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (name or fields.name or (f"{source} buyer ···{phone[-4:]}" if phone else f"{source} buyer"), phone, email, source,
             json.dumps([{"at": at, "text": text, "source": source}]),
             fields.budget_min, fields.budget_max, json.dumps(fields.localities), fields.bhk, fields.property_type,
             fields.urgency_days, int(fields.loan_preapproved), fields.notes, method, at, at))
        lead_id, deduped = cur.lastrowid, False

    # An inbound message is a reply; a portal enquiry on a listing also counts as a view.
    conn.execute("INSERT INTO engagements(lead_id, kind, at) VALUES (?, 'reply', ?)", (lead_id, at))
    if property_id:
        conn.execute("INSERT INTO engagements(lead_id, property_id, kind, at) VALUES (?, ?, 'view', ?)",
                     (lead_id, property_id, at))

    deals = scoring.rescore(conn, [lead_id]) if act else []
    dispatched = actions.dispatch(conn, deals) if act else {}
    lead = db.lead_from_row(conn.execute("SELECT * FROM leads WHERE id = ?", (lead_id,)).fetchone())
    top = sorted(deals, key=lambda d: d["score"], reverse=True)[:5]
    for d in top:
        p = dict(conn.execute("SELECT * FROM properties WHERE id = ?", (d["property_id"],)).fetchone())
        d.update(title=p["title"], price=p["price"], property_label=actions.property_label(p))
    return {"lead": lead, "deduplicated": deduped, "extraction": method,
            "extracted": fields.model_dump(), "top_matches": top, "actions": dispatched}


# --------------------------------------------------------------------------- engagement & outcomes

def log_event(conn, lead_id: int, kind: str, property_id=None) -> dict:
    at = db.iso(db.now())
    conn.execute("INSERT INTO engagements(lead_id, property_id, kind, at) VALUES (?,?,?,?)",
                 (lead_id, property_id, kind, at))
    if kind != "message_sent":
        conn.execute("UPDATE leads SET last_activity_at = ? WHERE id = ?", (at, lead_id))
    deals = scoring.rescore(conn, [lead_id])
    return {"actions": actions.dispatch(conn, deals, nurture=False),
            "deal": next((d for d in deals if d["property_id"] == property_id), None)}


def log_outcome(conn, lead_id: int, property_id: int, outcome: str) -> dict:
    """Broker feedback: called / visited / closed / lost / override_up / override_down."""
    row = conn.execute("SELECT d.features, p.locality FROM deals d JOIN properties p ON p.id = d.property_id "
                       "WHERE d.lead_id=? AND d.property_id=?", (lead_id, property_id)).fetchone()
    if not row:
        raise KeyError("deal not found")
    at = db.iso(db.now())
    conn.execute("UPDATE deals SET outcome=?, outcome_at=? WHERE lead_id=? AND property_id=?",
                 (outcome, at, lead_id, property_id))
    if outcome in OUTCOME_EVENTS:
        conn.execute("INSERT INTO engagements(lead_id, property_id, kind, at) VALUES (?,?,?,?)",
                     (lead_id, property_id, OUTCOME_EVENTS[outcome], at))
        conn.execute("UPDATE leads SET last_activity_at=? WHERE id=?", (at, lead_id))
    if outcome in OUTCOME_LABELS:
        conn.execute("INSERT INTO outcomes(lead_id, property_id, locality, features, label, kind, at) "
                     "VALUES (?,?,?,?,?,'broker',?)",
                     (lead_id, property_id, row["locality"], row["features"], OUTCOME_LABELS[outcome], at))
    if outcome == "closed":
        conn.execute("UPDATE leads SET status='closed' WHERE id=?", (lead_id,))
        conn.execute("UPDATE properties SET status='sold' WHERE id=?", (property_id,))
    elif outcome == "lost":
        pass  # the buyer stays active for other properties

    retrained = None
    n = conn.execute("SELECT COUNT(*) FROM outcomes WHERE kind='broker'").fetchone()[0]
    if outcome in OUTCOME_LABELS and n % RETRAIN_EVERY == 0:
        retrained = learning.retrain(conn)
        scoring.rescore(conn)
    elif outcome == "closed":
        scoring.rescore(conn)  # property is gone for everyone
    else:
        scoring.rescore(conn, [lead_id])
    return {"ok": True, "broker_outcomes": n, "retrained": retrained}


# --------------------------------------------------------------------------- W4

def daily_refresh(conn) -> dict:
    """Daily cron: re-score every deal with time decay, act on changes, send the Top-10 digest."""
    deals = scoring.rescore(conn)
    acted = actions.dispatch(conn, deals)
    dig = actions.digest(conn)
    db.set_setting(conn, "last_refresh", db.iso(db.now()))
    return {"rescored": len(deals), "actions": acted, "digest_title": dig["title"], "digest": dig["body"]}
