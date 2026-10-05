"""W2 matching & scoring.

Deal Score = P(close) x Deal Value. We score a specific buyer x property pair:
  1. filter candidate properties for each lead,
  2. compute normalised signals (0..1) for the pair,
  3. P(close) = sigmoid(intercept + sum(w_i * x_i)) with rule-based weights on day one,
     shifting to learned weights as broker outcomes accumulate (see learning.py),
  4. explain the score with the signals that pushed it up or down.
"""
import json
import math
from collections import defaultdict
from datetime import timedelta

from . import config, db
from .market import location_affinity

FEATURES = [
    "budget_fit",       # price vs buyer budget, softened by seller flexibility
    "location",         # exact / neighbouring locality
    "requirements",     # BHK / property type fit
    "seller_flex",      # seller open to negotiation
    "urgency",          # buyer timeline
    "financing",        # loan pre-approved or cash
    "engagement",       # views / visits / calls on THIS listing, last 14 days
    "responsiveness",   # how reliably the buyer replies
    "recency",          # time decay since the buyer's last activity
    "broker_affinity",  # broker's historical conversion in this locality
]

# Day-1 weights (logit space), set by hand from broker intuition.
RULE_WEIGHTS = {
    "intercept": -8.6,
    "budget_fit": 2.6,
    "location": 1.6,
    "requirements": 1.2,
    "seller_flex": 0.9,
    "urgency": 1.2,
    "financing": 0.6,
    "engagement": 3.0,
    "responsiveness": 0.9,
    "recency": 1.0,
    "broker_affinity": 0.6,
}

LABELS = {
    "budget_fit": "Budget fit",
    "location": "Location match",
    "requirements": "Property requirements",
    "seller_flex": "Seller flexibility",
    "urgency": "Buyer urgency",
    "financing": "Financing ready",
    "engagement": "Engagement on listing",
    "responsiveness": "Reply behaviour",
    "recency": "Recent activity",
    "broker_affinity": "Your conversion pattern",
}


def sigmoid(z: float) -> float:
    return 1 / (1 + math.exp(-z))


def current_weights(conn) -> dict:
    model = db.get_setting(conn, "model")
    return model["weights"] if model else dict(RULE_WEIGHTS)


def p_close(weights: dict, x: dict) -> float:
    z = weights["intercept"] + sum(weights[f] * x[f] for f in FEATURES)
    return sigmoid(z)


# --------------------------------------------------------------------------- candidates

def is_candidate(lead: dict, prop: dict) -> bool:
    if prop["status"] != "available":
        return False
    if lead["property_type"] and lead["property_type"] != prop["property_type"]:
        return False
    if lead["budget_max"] and prop["price"] > lead["budget_max"] * 1.25:
        return False
    if lead["budget_min"] and prop["price"] < lead["budget_min"] * 0.5:
        return False
    if lead["localities"] and location_affinity(lead["localities"], prop["locality"]) == 0:
        return False
    if lead["bhk"] and prop["bhk"] and abs(lead["bhk"] - prop["bhk"]) > 1:
        return False
    return True


# --------------------------------------------------------------------------- features

def budget_gap(lead, prop):
    """(price - budget_max) / budget_max; positive means over budget."""
    if not lead["budget_max"]:
        return None
    return (prop["price"] - lead["budget_max"]) / lead["budget_max"]


def compute_features(lead: dict, prop: dict, ctx: dict) -> dict:
    gap = budget_gap(lead, prop)
    flex = prop["seller_flexibility"]
    if gap is None:
        budget_fit = 0.5
    elif gap <= 0:
        # Comfortably affordable; very cheap listings are a weaker fit for what they want.
        budget_fit = 1.0 if not lead["budget_min"] or prop["price"] >= 0.7 * lead["budget_min"] else 0.7
    else:
        effective = max(0.0, gap - 0.06 * flex)   # a flexible seller can close a small gap
        budget_fit = max(0.0, 1 - effective / 0.2)

    if lead["bhk"] is None or prop["bhk"] is None:
        requirements = 1.0 if lead["property_type"] == prop["property_type"] else 0.7
    else:
        requirements = {0: 1.0, 1: 0.4}.get(abs(lead["bhk"] - prop["bhk"]), 0.0)

    u = lead["urgency_days"]
    urgency = 0.4 if u is None else 1.0 if u <= 30 else 0.8 if u <= 60 else 0.55 if u <= 90 else 0.2

    eng = ctx["engagement"].get((lead["id"], prop["id"]), {})
    engagement = 1 - math.exp(-(0.35 * eng.get("view", 0) + 1.2 * eng.get("visit", 0) + 0.5 * eng.get("call", 0)))

    lead_eng = ctx["lead_activity"].get(lead["id"], {})
    responsiveness = (lead_eng.get("reply", 0) + 1) / (lead_eng.get("message_sent", 0) + 2)
    responsiveness = min(1.0, responsiveness)

    days_idle = (ctx["now"] - db.parse_dt(lead["last_activity_at"])).total_seconds() / 86400
    recency = math.exp(-max(0.0, days_idle) / 14)

    return {
        "budget_fit": round(budget_fit, 4),
        "location": location_affinity(lead["localities"], prop["locality"]),
        "requirements": requirements,
        "seller_flex": flex,
        "urgency": urgency,
        "financing": 1.0 if lead["loan_preapproved"] else 0.0,
        "engagement": round(engagement, 4),
        "responsiveness": round(responsiveness, 4),
        "recency": round(recency, 4),
        "broker_affinity": ctx["affinity"].get(prop["locality"], 0.5),
    }


# --------------------------------------------------------------------------- explanations

def _fmt_inr(v: float) -> str:
    return f"₹{v / 1e7:.2f} Cr" if v >= 1e7 else f"₹{v / 1e5:.0f} L"


def explain(lead, prop, x, weights, ctx) -> list:
    """Plain-language reasons, ordered by how much each signal moved the score."""
    eng = ctx["engagement"].get((lead["id"], prop["id"]), {})
    gap = budget_gap(lead, prop)
    contributions = {f: weights[f] * (x[f] - 0.5) for f in FEATURES}

    def text(f, positive):
        if f == "budget_fit":
            if gap is None:
                return "Budget not stated yet"
            if abs(gap) < 0.005:
                return "Budget matches asking price"
            if 0 < gap <= 0.05:
                return f"Budget within {gap * 100:.0f}% of asking price" + (" · seller flexible" if prop["seller_flexibility"] >= 0.6 else "")
            if gap > 0:
                return f"Budget stretch of {gap * 100:.0f}%" + (" (seller flexible)" if prop["seller_flexibility"] >= 0.6 else "")
            if gap > -0.05:
                return f"Budget within {abs(gap) * 100:.0f}% of asking price"
            return f"Asking {_fmt_inr(prop['price'])} is within budget"
        if f == "location":
            return "Exact locality match" if x[f] == 1 else "Neighbouring locality" if x[f] > 0 else "No locality preference"
        if f == "requirements":
            return "Exact configuration match" if x[f] == 1 else "Configuration is a near miss"
        if f == "seller_flex":
            return "Seller open to negotiation" if positive else "Seller firm on price"
        if f == "urgency":
            u = lead["urgency_days"]
            if u is None:
                return "Timeline unknown"
            return f"Wants to move within {u} days" if positive else f"Long timeline ({u} days)"
        if f == "financing":
            return "Loan pre-approved / cash ready" if positive else "Financing not confirmed"
        if f == "engagement":
            v, vis = eng.get("view", 0), eng.get("visit", 0)
            if vis:
                return f"Visited this property{f' + viewed {v}x' if v else ''}"
            if v:
                return f"Viewed this listing {v}x in 2 weeks"
            return "No activity on this listing yet"
        if f == "responsiveness":
            return "Replies quickly" if positive else "Slow replies"
        if f == "recency":
            return "Active in the last few days" if positive else "Gone quiet recently"
        if f == "broker_affinity":
            return f"You close well in {prop['locality']}" if positive else f"Low historic conversion in {prop['locality']}"
        return LABELS[f]

    ranked = sorted(FEATURES, key=lambda f: abs(contributions[f]), reverse=True)
    reasons = []
    for f in ranked:
        c = contributions[f]
        if abs(c) < 0.15:
            continue
        reasons.append({"feature": f, "text": text(f, c > 0), "sign": "+" if c > 0 else "-", "impact": round(c, 2)})
    return reasons[:6]


def next_step(score, x, eng):
    if score >= config.HOT_THRESHOLD:
        return "Call now"
    if score >= config.DIGEST_THRESHOLD:
        if x["responsiveness"] < 0.4:
            return "Follow up"
        return "Book visit" if not eng.get("visit") else "Negotiate"
    return "Nurture"


def route_for(score):
    if score >= config.HOT_THRESHOLD:
        return "alert"
    if score >= config.DIGEST_THRESHOLD:
        return "digest"
    return "nurture"


# --------------------------------------------------------------------------- context & run

def build_context(conn) -> dict:
    now = db.now()
    since = db.iso(now - timedelta(days=14))
    engagement = defaultdict(lambda: defaultdict(int))
    for r in conn.execute(
        "SELECT lead_id, property_id, kind, COUNT(*) n FROM engagements "
        "WHERE property_id IS NOT NULL AND at >= ? GROUP BY lead_id, property_id, kind", (since,)
    ):
        engagement[(r["lead_id"], r["property_id"])][r["kind"]] = r["n"]

    since30 = db.iso(now - timedelta(days=30))
    lead_activity = defaultdict(lambda: defaultdict(int))
    for r in conn.execute(
        "SELECT lead_id, kind, COUNT(*) n FROM engagements WHERE at >= ? GROUP BY lead_id, kind", (since30,)
    ):
        lead_activity[r["lead_id"]][r["kind"]] = r["n"]

    # Broker's conversion by locality, Laplace-smoothed towards the overall rate.
    rows = conn.execute("SELECT locality, SUM(label) wins, COUNT(*) n FROM outcomes GROUP BY locality").fetchall()
    total_n = sum(r["n"] for r in rows) or 1
    base = (sum(r["wins"] for r in rows) + 1) / (total_n + 2)
    affinity = {}
    for r in rows:
        if r["locality"]:
            rate = (r["wins"] + 10 * base) / (r["n"] + 10)
            affinity[r["locality"]] = round(min(1.0, rate / (2 * base)), 4)

    return {"now": now, "engagement": engagement, "lead_activity": lead_activity, "affinity": affinity}


def score_pair(lead, prop, weights, ctx) -> dict:
    x = compute_features(lead, prop, ctx)
    p = p_close(weights, x)
    score = round(p * 100, 1)
    value = prop["price"] * config.COMMISSION_RATE
    eng = ctx["engagement"].get((lead["id"], prop["id"]), {})
    return {
        "lead_id": lead["id"], "property_id": prop["id"],
        "score": score, "p_close": p, "deal_value": value, "expected_value": p * value,
        "features": x, "reasons": explain(lead, prop, x, weights, ctx),
        "route": route_for(score), "next_step": next_step(score, x, eng),
    }


def rescore(conn, lead_ids=None) -> list:
    """Recompute deals for the given leads (or everyone). Returns the fresh deal dicts."""
    weights = current_weights(conn)
    ctx = build_context(conn)
    q = "SELECT * FROM leads WHERE status = 'active'"
    params = ()
    if lead_ids:
        q += f" AND id IN ({','.join('?' * len(lead_ids))})"
        params = tuple(lead_ids)
    leads = [db.lead_from_row(r) for r in conn.execute(q, params)]
    props = [dict(r) for r in conn.execute("SELECT * FROM properties WHERE status = 'available'")]

    computed_at = db.iso(ctx["now"])
    results = []
    for lead in leads:
        # Keep broker-logged outcomes; replace everything else for this lead.
        kept = {r["property_id"]: r for r in conn.execute(
            "SELECT property_id, outcome, outcome_at FROM deals WHERE lead_id = ? AND outcome IS NOT NULL", (lead["id"],))}
        conn.execute("DELETE FROM deals WHERE lead_id = ?", (lead["id"],))
        for prop in props:
            if not is_candidate(lead, prop):
                continue
            d = score_pair(lead, prop, weights, ctx)
            k = kept.get(prop["id"])
            conn.execute(
                "INSERT INTO deals(lead_id, property_id, score, p_close, deal_value, expected_value, features, "
                "reasons, route, next_step, computed_at, outcome, outcome_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (d["lead_id"], d["property_id"], d["score"], d["p_close"], d["deal_value"], d["expected_value"],
                 json.dumps(d["features"]), json.dumps(d["reasons"]), d["route"], d["next_step"], computed_at,
                 k["outcome"] if k else None, k["outcome_at"] if k else None),
            )
            results.append(d)
    return results
