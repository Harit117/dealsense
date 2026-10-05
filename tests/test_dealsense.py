import os
import tempfile
from pathlib import Path

import pytest

os.environ["DEALSENSE_USE_LLM"] = "off"
os.environ["DEALSENSE_DB"] = str(Path(tempfile.mkdtemp()) / "test.db")

from dealsense import config, db, learning, pipeline, scoring, seed  # noqa: E402
from dealsense.extract import extract_rules  # noqa: E402

config.USE_LLM = "off"


@pytest.fixture(scope="module", autouse=True)
def seeded():
    seed.seed(reset=True, n_buyers=30, verbose=False)


# --------------------------------------------------------------------------- W1 extraction

@pytest.mark.parametrize("text, expected", [
    ("hi sir 2bhk hsr layout around 85L? loan approved. urgent. 9876543210",
     dict(bhk=2, localities=["HSR Layout"], property_type="apartment", urgency_days=20, loan_preapproved=True,
          phone="9876543210")),
    ("Looking for 3BHK in Whitefield or Marathahalli, budget 1.2 to 1.4 Cr, in 2 months. I am Rohan Sharma",
     dict(bhk=3, localities=["Whitefield", "Marathahalli"], budget_min=1.2e7, budget_max=1.4e7, urgency_days=60,
          name="Rohan Sharma")),
    ("Plot in sarjapur under 90 lakhs, just exploring",
     dict(property_type="plot", localities=["Sarjapur Road"], budget_max=9e6, urgency_days=240)),
    ("need villa near hebbal 2.5cr full cash next month",
     dict(property_type="villa", localities=["Hebbal"], loan_preapproved=True, urgency_days=45)),
])
def test_rule_extraction(text, expected):
    got = extract_rules(text).model_dump()
    for k, v in expected.items():
        assert got[k] == pytest.approx(v) if isinstance(v, float) else got[k] == v, (k, got[k])


# --------------------------------------------------------------------------- W2 scoring

def test_persona_ranks_hot_with_reasons():
    with db.connect() as conn:
        top = {d["lead_name"]: d for d in pipeline.actions.top_deals(conn, 50)}
    rohan = top["Rohan Sharma"]
    assert rohan["score"] >= config.HOT_THRESHOLD
    assert rohan["route"] == "alert" and rohan["next_step"] == "Call now"
    texts = " ".join(r["text"] for r in rohan["reasons"])
    assert "Budget within 3% of asking price" in texts
    assert "Viewed this listing" in texts


def test_engagement_and_budget_move_the_score():
    ctx = {"now": db.now(), "engagement": {}, "lead_activity": {}, "affinity": {}}
    lead = dict(id=1, budget_min=9e6, budget_max=1e7, localities=["Whitefield"], bhk=2, property_type="apartment",
                urgency_days=30, loan_preapproved=True, last_activity_at=db.iso(db.now()))
    prop = dict(id=1, price=1e7, locality="Whitefield", bhk=2, property_type="apartment", seller_flexibility=0.5,
                status="available")
    w = scoring.RULE_WEIGHTS
    base = scoring.score_pair(lead, prop, w, ctx)["score"]
    ctx["engagement"] = {(1, 1): {"view": 3, "visit": 1}}
    engaged = scoring.score_pair(lead, prop, w, ctx)["score"]
    pricey = scoring.score_pair(lead, dict(prop, price=1.18e7), w, ctx)["score"]
    assert engaged > base
    assert pricey < engaged
    assert not scoring.is_candidate(lead, dict(prop, property_type="plot"))
    assert not scoring.is_candidate(lead, dict(prop, locality="Yelahanka"))


# --------------------------------------------------------------------------- pipeline

def test_ingest_dedupes_by_phone_and_routes():
    with db.connect() as conn:
        a = pipeline.ingest(conn, "3bhk whitefield around 1.3cr loan approved in 2 months 9811122233", "WhatsApp")
        b = pipeline.ingest(conn, "Call note: 9811122233 says can stretch, urgent", "Call")
    assert not a["deduplicated"] and b["deduplicated"]
    assert a["lead"]["id"] == b["lead"]["id"]
    assert b["lead"]["urgency_days"] == 20 and b["lead"]["bhk"] == 3  # merged: new urgency, kept BHK
    assert a["top_matches"], "expected matches in seeded Whitefield inventory"
    assert a["top_matches"][0]["route"] in {"alert", "digest", "nurture"}


def test_outcomes_feed_learning():
    with db.connect() as conn:
        deals = conn.execute("SELECT lead_id, property_id FROM deals ORDER BY score DESC LIMIT 5").fetchall()
        for i, d in enumerate(deals):
            r = pipeline.log_outcome(conn, d["lead_id"], d["property_id"], "visited" if i % 2 == 0 else "lost")
        assert r["retrained"] is not None  # every 5th broker outcome retrains
        model = learning.model_info(conn)
    assert model["kind"] == "blended"
    assert model["broker_outcomes"] == 5
    assert 0 < model["alpha"] < 1
    assert model["auc_model_holdout"] > 0.5


def test_daily_refresh_sends_digest():
    with db.connect() as conn:
        r = pipeline.daily_refresh(conn)
        n = conn.execute("SELECT COUNT(*) FROM outbox WHERE kind='digest'").fetchone()[0]
    assert r["rescored"] > 0 and n >= 2


# --------------------------------------------------------------------------- API

def test_api_endpoints():
    from fastapi.testclient import TestClient
    from dealsense.main import app

    with TestClient(app) as client:
        assert client.get("/api/summary").json()["leads"] > 0
        top = client.get("/api/top").json()
        assert 0 < len(top) <= 10 and top[0]["score"] >= top[-1]["score"]
        r = client.post("/api/webhooks/99acres", json={"name": "Test Buyer", "mobile": "9700011122",
                                                      "message": "2BHK in HSR Layout, budget 1.3 to 1.5 Cr, urgent"})
        assert r.status_code == 200 and r.json()["lead"]["name"] == "Test Buyer"
        d = top[0]
        assert "brief" in client.get(f"/api/deals/{d['lead_id']}/{d['property_id']}/brief").json()
        assert client.post("/api/events", json={"lead_id": d["lead_id"], "kind": "view",
                                                "property_id": d["property_id"]}).status_code == 200
