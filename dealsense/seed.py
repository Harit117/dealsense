"""Generate a realistic synthetic dataset for a Bengaluru broker and run it through the pipeline.

    python -m dealsense.seed            # reset DB and seed
    python -m dealsense.seed --keep     # seed on top of the existing DB

Everything here is synthetic: buyer names, phone numbers and listings are made up.
Raw enquiries are rendered as messy portal / WhatsApp / email / call-note text and pushed
through the real W1 extractor, so the dataset also exercises the enrichment step.
"""
import argparse
import json
import math
import random
from datetime import timedelta

from . import actions, config, db, pipeline, scoring
from .market import BHK_SIZES, LOCALITIES

RNG = random.Random(42)

FIRST = ["Aarav", "Aditi", "Akash", "Ananya", "Arjun", "Deepa", "Farhan", "Gaurav", "Ishaan", "Kavya", "Karthik",
         "Lakshmi", "Manoj", "Meera", "Nikhil", "Nisha", "Pooja", "Pradeep", "Rahul", "Ramya", "Sanjay", "Shreya",
         "Siddharth", "Sneha", "Suresh", "Tanvi", "Varun", "Vidya", "Yash", "Zoya", "Harish", "Divya", "Abhishek",
         "Bhavana", "Chetan", "Esha", "Girish", "Hema", "Imran", "Jyothi"]
LAST = ["Iyer", "Reddy", "Nair", "Gowda", "Sharma", "Rao", "Shetty", "Kulkarni", "Hegde", "Menon", "Pillai", "Joshi",
        "Bhat", "Khan", "Das", "Agarwal", "Kamath", "Naidu", "Desai", "Prasad"]
PROJECTS = ["Green Meadows", "Lakeview Residency", "Palm Grove", "Silver Oak Heights", "Maple Court", "Sunrise Towers",
            "Orchid Enclave", "Banyan Park", "Cedar Springs", "Lotus Gardens", "Skyline Vista", "Amber Woods",
            "Coral Bay Homes", "Ivy Terraces", "Jasmine Square", "Riverstone"]

# Ground truth for the synthetic history (unknown to DealSense; the learner has to recover it).
TRUE_WEIGHTS = {"intercept": -10.3, "budget_fit": 2.0, "location": 0.3, "requirements": 0.4, "seller_flex": 2.2,
                "urgency": 2.6, "financing": 1.8, "engagement": 3.0, "responsiveness": 2.4, "recency": 0.2,
                "broker_affinity": 2.2}


def money_text(v: float, style: str) -> str:
    if v >= 1e7:
        return f"{v / 1e7:.2f}".rstrip("0").rstrip(".") + (" Cr" if style != "chat" else "cr")
    lakhs = round(v / 1e5)
    return {"chat": f"{lakhs}L", "formal": f"{lakhs} lakhs"}.get(style, f"{lakhs} Lakh")


def urgency_text(days):
    return {20: "urgent", 30: "within 30 days", 45: "next month", 60: "in 2 months",
            90: "in 3 months", 240: "just exploring"}[days]


def make_properties(conn, now):
    props = []
    for _ in range(140):
        loc = RNG.choice(list(LOCALITIES))
        ppsf = LOCALITIES[loc][0]
        kind = RNG.choices(["apartment", "villa", "plot"], [0.8, 0.1, 0.1])[0]
        if kind == "apartment":
            bhk = RNG.choices([1, 2, 3, 4], [0.1, 0.42, 0.38, 0.1])[0]
            area = int(BHK_SIZES[bhk] * RNG.uniform(0.85, 1.2))
            price = area * ppsf * RNG.uniform(0.88, 1.15)
            title = f"{RNG.choice(PROJECTS)} · {bhk}BHK, {area:,} sq ft"
        elif kind == "villa":
            bhk = RNG.choice([3, 4])
            area = RNG.randint(2600, 4000)
            price = area * ppsf * RNG.uniform(1.15, 1.4)
            title = f"{RNG.choice(PROJECTS)} Villas · {bhk}BHK villa, {area:,} sq ft"
        else:
            bhk = None
            area = RNG.choice([1200, 1500, 2400, 4000])
            price = area * ppsf * RNG.uniform(0.6, 0.85)
            title = f"{RNG.choice(PROJECTS)} Layout · {area:,} sq ft plot"
        props.append(dict(title=title, locality=loc, property_type=kind, bhk=bhk, area_sqft=area,
                          price=round(price / 50000) * 50000, seller_flexibility=round(RNG.uniform(0.05, 0.9), 2),
                          listed_at=db.iso(now - timedelta(days=RNG.randint(1, 90)))))
    return props


def insert_property(conn, p):
    cur = conn.execute("INSERT INTO properties(title, locality, property_type, bhk, area_sqft, price, seller_flexibility, "
                       "listed_at) VALUES (:title,:locality,:property_type,:bhk,:area_sqft,:price,:seller_flexibility,:listed_at)", p)
    return cur.lastrowid


def render_message(b) -> tuple[str, str]:
    """Render a buyer profile as the kind of text that actually arrives. Returns (source, text)."""
    loc = b["localities"][0]
    loc2 = b["localities"][1] if len(b["localities"]) > 1 else None
    what = "plot" if b["type"] == "plot" else "villa" if b["type"] == "villa" else f"{b['bhk']}BHK"
    loan = "Loan pre-approved." if b["loan"] else ""
    urg = urgency_text(b["urgency"])
    source = b.get("source") or RNG.choice(["99acres", "MagicBricks", "Housing.com", "WhatsApp", "WhatsApp",
                                            "Call", "Email", "Google Form", "Walk-in"])
    lo, hi = b["budget_min"], b["budget_max"]
    if source in ("99acres", "Housing.com"):
        text = (f"[{source}] New enquiry from {b['name']} (+91 {b['phone']}) for {what} in {loc}. "
                f"Budget: {money_text(lo, 'portal')} to {money_text(hi, 'portal')}. Buyer note: looking to buy {urg}. {loan}")
    elif source == "MagicBricks":
        text = (f"MagicBricks lead | Name: {b['name']} | Mobile: {b['phone']} | Looking for {what} "
                f"{'apartment' if b['type'] == 'apartment' else ''} | Location: {loc}{', ' + loc2 if loc2 else ''} | "
                f"Budget upto {money_text(hi, 'portal')} | Possession {urg}")
    elif source == "WhatsApp":
        text = (f"hi sir {what.lower()} {loc.lower()}{' or ' + loc2.lower() if loc2 else ''} around "
                f"{money_text((lo + hi) / 2, 'chat')}? {'loan approved' if b['loan'] else ''} {urg}. my no {b['phone']}")
    elif source == "Call":
        text = (f"Call note: spoke to {b['name']}, {b['phone']}. Wants {what} near {loc}, max {money_text(hi, 'formal')}, "
                f"{urg}. {'Has loan sanction letter.' if b['loan'] else 'Financing not discussed.'}")
    elif source == "Email":
        text = (f"Hello, I am {b['name']}. We are looking to buy a {what} in {loc}{' or ' + loc2 if loc2 else ''}. "
                f"Our budget is {money_text(lo, 'formal')} - {money_text(hi, 'formal')}, ideally {urg}. {loan} "
                f"Phone {b['phone']}.")
    elif source == "Google Form":
        text = (f"Form response — Name: {b['name']}; Phone: {b['phone']}; Requirement: {what}; Area: {loc}; "
                f"Budget: {money_text(lo, 'portal')}-{money_text(hi, 'portal')}; Timeline: {urg}; Loan: {'yes, approved' if b['loan'] else 'no'}")
    else:
        text = (f"Walk-in: {b['name']} ({b['phone']}) asked about {what} options in {loc}, budget around "
                f"{money_text((lo + hi) / 2, 'formal')}, {urg}.")
    return source, " ".join(text.split())


def random_buyer(props):
    target = RNG.choice(props)  # anchor demand on real inventory so matches exist
    locs = [target["locality"]]
    if RNG.random() < 0.35:
        locs.append(RNG.choice(sorted(LOCALITIES)))
    anchor = target["price"] * RNG.uniform(0.85, 1.12)
    return dict(
        name=f"{RNG.choice(FIRST)} {RNG.choice(LAST)}", phone=f"9{RNG.randint(100000000, 999999999)}",
        localities=list(dict.fromkeys(locs)), type=target["property_type"], bhk=target["bhk"],
        budget_min=anchor * 0.88, budget_max=anchor,
        urgency=RNG.choices([20, 30, 45, 60, 90, 240], [0.1, 0.15, 0.15, 0.2, 0.2, 0.2])[0],
        loan=RNG.random() < 0.35,
    )


# The five buyers from the deck's dashboard mock-up, with the properties they are matched to.
PERSONAS = [
    dict(buyer=dict(name="Rohan Sharma", phone="9845012345", localities=["Whitefield"], type="apartment", bhk=3,
                    budget_min=1.18e7, budget_max=1.32e7, urgency=60, loan=True, source="99acres"),
         prop=dict(title="Lakeview Residency · 3BHK, 1,640 sq ft", locality="Whitefield", property_type="apartment",
                   bhk=3, area_sqft=1640, price=1.36e7, seller_flexibility=0.85),
         views=3, visits=0, replies=4, sent=4, idle_days=0),
    dict(buyer=dict(name="Priya Menon", phone="9880023456", localities=["HSR Layout"], type="apartment", bhk=2,
                    budget_min=1.3e7, budget_max=1.5e7, urgency=30, loan=True, source="WhatsApp"),
         prop=dict(title="Palm Grove · 2BHK, 1,180 sq ft", locality="HSR Layout", property_type="apartment",
                   bhk=2, area_sqft=1180, price=1.45e7, seller_flexibility=0.4),
         views=2, visits=0, replies=3, sent=3, idle_days=1),
    dict(buyer=dict(name="Arjun Kulkarni", phone="9900034567", localities=["Sarjapur Road"], type="plot", bhk=None,
                    budget_min=7.5e6, budget_max=8.6e6, urgency=45, loan=False, source="Housing.com"),
         prop=dict(title="Banyan Park Layout · 1,500 sq ft plot", locality="Sarjapur Road", property_type="plot",
                   bhk=None, area_sqft=1500, price=9.0e6, seller_flexibility=0.7),
         views=5, visits=1, replies=5, sent=5, idle_days=1),
    dict(buyer=dict(name="Neha Rao", phone="9741045678", localities=["Indiranagar"], type="apartment", bhk=2,
                    budget_min=1.8e7, budget_max=2.05e7, urgency=60, loan=False, source="Email"),
         prop=dict(title="Maple Court · 2BHK, 1,150 sq ft", locality="Indiranagar", property_type="apartment",
                   bhk=2, area_sqft=1150, price=1.98e7, seller_flexibility=0.3),
         views=2, visits=0, replies=1, sent=6, idle_days=3),
    dict(buyer=dict(name="Vikram Patil", phone="9632056789", localities=["Hebbal"], type="apartment", bhk=3,
                    budget_min=1.45e7, budget_max=1.6e7, urgency=90, loan=True, source="Call"),
         prop=dict(title="Skyline Vista · 3BHK, 1,620 sq ft", locality="Hebbal", property_type="apartment",
                   bhk=3, area_sqft=1620, price=1.79e7, seller_flexibility=0.5),
         views=1, visits=0, replies=2, sent=3, idle_days=4),
]


def seed_history(conn, now, n=600):
    """Past deal outcomes for this broker (feature snapshot + did it progress), for the W4 learner."""
    skill = {loc: RNG.uniform(0.2, 0.9) for loc in LOCALITIES}
    rows = []
    for _ in range(n):
        loc = RNG.choice(list(LOCALITIES))
        x = {
            "budget_fit": min(1.0, max(0.0, RNG.gauss(0.7, 0.3))),
            "location": RNG.choices([1.0, 0.6, 0.5], [0.6, 0.3, 0.1])[0],
            "requirements": RNG.choices([1.0, 0.4, 0.7], [0.65, 0.25, 0.1])[0],
            "seller_flex": round(RNG.uniform(0.05, 0.9), 2),
            "urgency": RNG.choice([1.0, 0.8, 0.55, 0.2, 0.4]),
            "financing": 1.0 if RNG.random() < 0.35 else 0.0,
            "engagement": round(1 - math.exp(-RNG.expovariate(1.0)), 3),
            "responsiveness": round(RNG.uniform(0.2, 1.0), 3),
            "recency": round(math.exp(-RNG.uniform(0, 30) / 14), 3),
            "broker_affinity": round(skill[loc], 3),
        }
        z = TRUE_WEIGHTS["intercept"] + sum(TRUE_WEIGHTS[k] * v for k, v in x.items())
        label = int(RNG.random() < 1 / (1 + math.exp(-z)))
        at = db.iso(now - timedelta(days=RNG.randint(30, 540)))
        rows.append((loc, json.dumps(x), label, at))
    conn.executemany("INSERT INTO outcomes(locality, features, label, kind, at) VALUES (?,?,?,'historical',?)", rows)
    return sum(r[2] for r in rows)


def add_engagement(conn, lead_id, prop_id, views, visits, replies, sent, idle_days, now):
    last = now - timedelta(days=idle_days, hours=RNG.randint(0, 8))
    rows = []
    for _ in range(views):
        rows.append((lead_id, prop_id, "view", db.iso(last - timedelta(days=RNG.uniform(0, 6)))))
    for _ in range(visits):
        rows.append((lead_id, prop_id, "visit", db.iso(last - timedelta(days=RNG.uniform(0, 4)))))
    for _ in range(replies):
        rows.append((lead_id, None, "reply", db.iso(last - timedelta(days=RNG.uniform(0, 20)))))
    for _ in range(sent):
        rows.append((lead_id, None, "message_sent", db.iso(last - timedelta(days=RNG.uniform(0, 20)))))
    conn.executemany("INSERT INTO engagements(lead_id, property_id, kind, at) VALUES (?,?,?,?)", rows)
    conn.execute("UPDATE leads SET last_activity_at = ? WHERE id = ?", (db.iso(last), lead_id))


def seed(reset=True, n_buyers=70, verbose=True):
    RNG.seed(42)  # same demo dataset on every reset
    db.init_db()
    if reset:
        with db.connect() as conn:  # clear in place: the server may hold the file open
            for table in ("deals", "outbox", "engagements", "outcomes", "settings", "leads", "properties"):
                conn.execute(f"DELETE FROM {table}")
            conn.execute("DELETE FROM sqlite_sequence")
    prev_llm = config.USE_LLM
    config.USE_LLM = "off"  # seed with the free rules extractor; live intake uses Claude when configured
    now = db.now()
    try:
        with db.connect() as conn:
            props = make_properties(conn, now)
            prop_ids = [insert_property(conn, p) for p in props]
            for p, pid in zip(props, prop_ids):
                p["id"] = pid
            wins = seed_history(conn, now)

            # Persona buyers from the deck, wired to their showcase properties.
            for persona in PERSONAS:
                pp = dict(persona["prop"], listed_at=db.iso(now - timedelta(days=12)))
                pid = insert_property(conn, pp)
                b = persona["buyer"]
                source, text = render_message(b)
                created = db.iso(now - timedelta(days=RNG.randint(5, 20)))
                res = pipeline.ingest(conn, text, source=source, name=b["name"], at=created, act=False)
                add_engagement(conn, res["lead"]["id"], pid, persona["views"], persona["visits"],
                               persona["replies"] - 1, persona["sent"], persona["idle_days"], now)

            # The long tail of ordinary enquiries.
            for _ in range(n_buyers):
                b = random_buyer(props)
                source, text = render_message(b)
                created = db.iso(now - timedelta(days=RNG.randint(1, 45)))
                res = pipeline.ingest(conn, text, source=source, name=b["name"], at=created, act=False)
                lead = res["lead"]
                cands = [p for p in props if scoring.is_candidate(lead, dict(p, status="available"))]
                RNG.shuffle(cands)
                engaged = RNG.random()
                idle = int(RNG.expovariate(1 / 9))
                for i, p in enumerate(cands[: RNG.randint(0, 3)]):
                    add_engagement(conn, lead["id"], p["id"],
                                   views=RNG.choices([0, 1, 2, 3, 4], [0.2, 0.35, 0.25, 0.12, 0.08])[0],
                                   visits=1 if engaged > 0.85 and i == 0 else 0,
                                   replies=0, sent=0, idle_days=idle, now=now)
                add_engagement(conn, lead["id"], None, 0, 0, replies=int(engaged * 4), sent=RNG.randint(1, 5),
                               idle_days=idle, now=now)

            # Duplicate enquiry from an existing buyer on another channel (exercises de-duplication).
            pipeline.ingest(conn, "Rohan here again, 9845012345. Still keen on 3bhk whitefield, can stretch a bit. "
                                  "Loan pre-approved.", source="WhatsApp", act=False,
                            at=db.iso(now - timedelta(hours=3)))

            deals = scoring.rescore(conn)
            acted = actions.dispatch(conn, deals)
            actions.digest(conn)
            db.set_setting(conn, "last_refresh", db.iso(now))

            if verbose:
                n_leads = conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0]
                print(f"Seeded {len(prop_ids) + len(PERSONAS)} properties, {n_leads} leads, "
                      f"600 historical outcomes ({wins} progressed), {len(deals)} buyer x property deals.")
                print(f"Routing: {acted['alerts']} hot alerts, {acted['nurtures']} nurture messages queued.")
                for d in actions.top_deals(conn, 10):
                    print(f"  {d['score']:5.1f}  {d['lead_name']:<18} x {d['property_label']:<26} {d['next_step']}")
    finally:
        config.USE_LLM = prev_llm


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true", help="do not delete the existing database first")
    ap.add_argument("--buyers", type=int, default=70)
    args = ap.parse_args()
    seed(reset=not args.keep, n_buyers=args.buyers)
