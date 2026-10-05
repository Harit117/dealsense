"""W3 decide & act: route each scored deal.

  score 80+   -> instant alert to the broker (WhatsApp-style card, Telegram/Slack if configured)
  score 50-79 -> daily Top-10 digest
  score < 50  -> auto-nurture message queued for the lead
"""
import json
import logging
from datetime import timedelta

import httpx

from . import config, db

log = logging.getLogger("dealsense.actions")


def _first_name_initial(name: str) -> str:
    parts = name.split()
    return f"{parts[0]} {parts[1][0]}." if len(parts) == 2 and parts[1][:1].isupper() else name


def property_label(p) -> str:
    kind = f"{p['bhk']}BHK" if p["bhk"] else p["property_type"].capitalize()
    return f"{kind}, {p['locality']}"


def _deliver(title: str, body: str) -> str:
    """Push to external channels when configured. Returns the channel used."""
    text = f"{title}\n{body}"
    channel = "whatsapp-sim"
    try:
        if config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_CHAT_ID:
            httpx.post(f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage",
                       json={"chat_id": config.TELEGRAM_CHAT_ID, "text": text}, timeout=10).raise_for_status()
            channel = "telegram"
        if config.SLACK_WEBHOOK_URL:
            httpx.post(config.SLACK_WEBHOOK_URL, json={"text": text}, timeout=10).raise_for_status()
            channel = "slack" if channel == "whatsapp-sim" else channel + "+slack"
    except httpx.HTTPError as exc:
        log.warning("alert delivery failed: %s", exc)
    return channel


def _queue(conn, kind, channel, title, body, lead_id=None, property_id=None, score=None):
    conn.execute(
        "INSERT INTO outbox(kind, channel, lead_id, property_id, score, title, body, status, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (kind, channel, lead_id, property_id, score, title, body,
         "sent" if channel != "whatsapp-sim" else "queued", db.iso(db.now())),
    )


def alert_text(lead, prop, deal) -> tuple[str, str]:
    reasons = [r["text"] for r in deal["reasons"] if r["sign"] == "+"][:3]
    ist = db.now() + timedelta(hours=5, minutes=30)
    if 8 <= ist.hour < 19:
        when = "call before " + (ist + timedelta(hours=2)).strftime("%I %p").lstrip("0")
    else:
        when = "call first thing tomorrow, before 11 AM"
    title = f"HOT DEAL · Score {deal['score']:.0f}"
    body = (f"{_first_name_initial(lead['name'])} is a strong match for {property_label(prop)}.\n"
            f"{'; '.join(reasons)}.\nSuggested: {when}.")
    return title, body


def nurture_text(lead, prop=None) -> str:
    first = lead["name"].split()[0]
    where = lead["localities"][0] if lead["localities"] else "your preferred area"
    what = f"{lead['bhk']}BHK homes" if lead["bhk"] else f"{lead['property_type'] or 'property'} options"
    tail = f" One that stood out: {property_label(prop)}." if prop else ""
    return (f"Hi {first}, a few new {what} have come up around {where}.{tail} "
            f"Would you like me to share a shortlist or set up a weekend visit?")


def dispatch(conn, deals: list, nurture=True) -> dict:
    """Act on freshly scored deals. Idempotent: no duplicate alerts within 24h, nurture within 7 days."""
    counts = {"alerts": 0, "nurtures": 0}
    if not deals:
        return counts
    now = db.now()
    leads = {r["id"]: db.lead_from_row(r) for r in conn.execute(
        f"SELECT * FROM leads WHERE id IN ({','.join('?' * len({d['lead_id'] for d in deals}))})",
        tuple({d["lead_id"] for d in deals}))}
    props = {r["id"]: dict(r) for r in conn.execute("SELECT * FROM properties")}

    best_by_lead = {}
    for d in deals:
        if d["lead_id"] not in best_by_lead or d["score"] > best_by_lead[d["lead_id"]]["score"]:
            best_by_lead[d["lead_id"]] = d
        if d["route"] != "alert":
            continue
        recent = conn.execute(
            "SELECT 1 FROM outbox WHERE kind='alert' AND lead_id=? AND property_id=? AND created_at >= ?",
            (d["lead_id"], d["property_id"], db.iso(now - timedelta(hours=24)))).fetchone()
        if recent:
            continue
        title, body = alert_text(leads[d["lead_id"]], props[d["property_id"]], d)
        _queue(conn, "alert", _deliver(title, body), title, body, d["lead_id"], d["property_id"], d["score"])
        counts["alerts"] += 1

    if nurture:
        for lead_id, d in best_by_lead.items():
            if d["route"] != "nurture":
                continue
            recent = conn.execute(
                "SELECT 1 FROM outbox WHERE kind='nurture' AND lead_id=? AND created_at >= ?",
                (lead_id, db.iso(now - timedelta(days=7)))).fetchone()
            if recent:
                continue
            lead = leads[lead_id]
            _queue(conn, "nurture", "whatsapp-sim", f"To {lead['name']}", nurture_text(lead, props[d["property_id"]]),
                   lead_id, d["property_id"], d["score"])
            conn.execute("INSERT INTO engagements(lead_id, kind, at) VALUES (?, 'message_sent', ?)", (lead_id, db.iso(now)))
            counts["nurtures"] += 1
    return counts


def top_deals(conn, limit=10, sort="score", min_score=config.DIGEST_THRESHOLD) -> list:
    """Best opportunity per buyer, ranked. sort = 'score' or 'value' (expected commission)."""
    order = "d.expected_value DESC" if sort == "value" else "d.score DESC"
    rows = conn.execute(
        f"""SELECT d.*, l.name AS lead_name, l.phone, l.source, p.title, p.locality, p.bhk, p.property_type, p.price
            FROM deals d JOIN leads l ON l.id = d.lead_id JOIN properties p ON p.id = d.property_id
            WHERE d.score >= ? AND l.status = 'active' AND (d.outcome IS NULL OR d.outcome NOT IN ('lost', 'override_down'))
            ORDER BY {order}""", (min_score,)).fetchall()
    seen, out = set(), []
    for r in rows:
        if r["lead_id"] in seen:
            continue
        seen.add(r["lead_id"])
        d = dict(r)
        d["reasons"] = json.loads(d["reasons"])
        d["features"] = json.loads(d["features"])
        d["property_label"] = property_label(d)
        out.append(d)
        if len(out) == limit:
            break
    return out


def digest(conn) -> dict:
    deals = top_deals(conn, 10)
    lines = []
    for i, d in enumerate(deals, 1):
        why = " · ".join(r["text"] for r in d["reasons"] if r["sign"] == "+")[:90]
        lines.append(f"{i}. {_first_name_initial(d['lead_name'])} × {d['property_label']} — {d['score']:.0f} — {why} → {d['next_step']}")
    title = f"Your {len(deals)} best deals today"
    body = "\n".join(lines) or "No deals above 50 today. Nurture mode is re-engaging older enquiries."
    _queue(conn, "digest", _deliver(title, body), title, body)
    return {"title": title, "body": body, "deals": deals}


def ai_brief(lead, prop, deal) -> dict:
    """A short call brief for the broker. Claude when available, a template otherwise."""
    facts = {
        "buyer": {k: lead[k] for k in ("name", "budget_min", "budget_max", "localities", "bhk",
                                         "property_type", "urgency_days", "loan_preapproved", "notes")},
        "recent_messages": [m["text"] for m in lead["raw_messages"][-3:]],
        "property": {k: prop[k] for k in ("title", "locality", "bhk", "area_sqft", "price", "seller_flexibility")},
        "score": deal["score"], "signals": [r["text"] for r in deal["reasons"]],
    }
    if config.llm_enabled():
        try:
            import anthropic
            client = anthropic.Anthropic()
            resp = client.messages.create(
                model=config.LLM_MODEL, max_tokens=2000, output_config={"effort": "low"},
                system=("You brief an Indian real-estate broker before a call. Write at most 5 short bullet lines: "
                        "opening line, the 2 strongest reasons this buyer fits this property, the likely objection "
                        "and how to handle it, and the ask (visit/offer). Use INR in lakh/crore. No preamble."),
                messages=[{"role": "user", "content": json.dumps(facts, default=str)}],
            )
            if resp.stop_reason != "refusal":
                text = "".join(b.text for b in resp.content if b.type == "text").strip()
                if text:
                    return {"brief": text, "source": "llm"}
        except Exception as exc:
            log.warning("AI brief failed, using template: %s", exc)
    pos = [r["text"] for r in deal["reasons"] if r["sign"] == "+"][:2]
    neg = [r["text"] for r in deal["reasons"] if r["sign"] == "-"][:1]
    lines = [f"- Open: \"Hi {lead['name'].split()[0]}, I have a {property_label(prop)} that fits what you described.\""]
    lines += [f"- Why it fits: {p}" for p in pos]
    if neg:
        lines.append(f"- Likely objection: {neg[0]}; be ready with comparables and seller's flexibility.")
    lines.append("- Ask: lock a site visit this week." if deal["score"] < 85 else "- Ask: visit + token discussion this week.")
    return {"brief": "\n".join(lines), "source": "template"}
