"""DealSense API + dashboard.

    uvicorn dealsense.main:app --reload      (or: python run.py)
"""
import json
import logging
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Literal, Optional

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import actions, config, db, learning, pipeline, seed
from .actions import property_label

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("dealsense")

STATIC = config.ROOT / "dealsense" / "static"


# --------------------------------------------------------------------------- startup & scheduler

def _scheduler():
    """W4 cron: run the daily refresh once a day at DAILY_REFRESH_AT (server local time)."""
    hh, mm = (int(x) for x in config.DAILY_REFRESH_AT.split(":"))
    while True:
        try:
            now = datetime.now()
            with db.connect() as conn:
                last = db.get_setting(conn, "last_refresh")
                last_local = db.parse_dt(last).astimezone().date() if last else None
                due = (now.hour, now.minute) >= (hh, mm) and last_local != now.date()
                if due:
                    log.info("Daily refresh: %s", pipeline.daily_refresh(conn))
        except Exception:
            log.exception("scheduler tick failed")
        time.sleep(60)


@asynccontextmanager
async def lifespan(_app):
    if not config.DB_PATH.exists():
        log.info("No database found, seeding synthetic Bengaluru dataset ...")
        seed.seed(reset=True)
    db.init_db()
    threading.Thread(target=_scheduler, daemon=True).start()
    log.info("LLM enrichment: %s", f"on ({config.LLM_MODEL})" if config.llm_enabled() else "off (rules extractor)")
    yield


app = FastAPI(title="DealSense", description="AI deal intelligence for real-estate brokers", version="1.0",
              lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(STATIC / "index.html")


# --------------------------------------------------------------------------- read APIs

@app.get("/api/summary")
def summary():
    with db.connect() as conn:
        q = lambda sql, *a: conn.execute(sql, a).fetchone()[0]
        model = learning.model_info(conn)
        return {
            "broker": config.BROKER_NAME,
            "llm": {"enabled": config.llm_enabled(), "model": config.LLM_MODEL if config.llm_enabled() else None},
            "leads": q("SELECT COUNT(*) FROM leads WHERE status='active'"),
            "properties": q("SELECT COUNT(*) FROM properties WHERE status='available'"),
            "deals": q("SELECT COUNT(*) FROM deals"),
            "hot": q("SELECT COUNT(DISTINCT lead_id) FROM deals WHERE route='alert'"),
            "pipeline_value": q("SELECT COALESCE(SUM(ev),0) FROM (SELECT MAX(expected_value) ev FROM deals GROUP BY lead_id)"),
            "closed": q("SELECT COUNT(*) FROM leads WHERE status='closed'"),
            "routes": {r["route"]: r["n"] for r in conn.execute(
                "SELECT route, COUNT(*) n FROM (SELECT lead_id, route, MAX(score) FROM deals GROUP BY lead_id) GROUP BY route")},
            "sources": {r["source"]: r["n"] for r in conn.execute("SELECT source, COUNT(*) n FROM leads GROUP BY source")},
            "model": {k: model.get(k) for k in ("kind", "n_train", "alpha", "auc_rules_holdout", "auc_model_holdout",
                                                 "broker_outcomes", "trained_at")},
            "last_refresh": db.get_setting(conn, "last_refresh"),
            "thresholds": {"hot": config.HOT_THRESHOLD, "digest": config.DIGEST_THRESHOLD},
        }


@app.get("/api/top")
def top(sort: Literal["score", "value"] = "score", limit: int = 10, min_score: float = config.DIGEST_THRESHOLD):
    with db.connect() as conn:
        return actions.top_deals(conn, limit, sort, min_score)


@app.get("/api/leads")
def leads(q: Optional[str] = None):
    with db.connect() as conn:
        rows = conn.execute(
            """SELECT l.*, MAX(d.score) AS best_score, COUNT(d.property_id) AS matches
               FROM leads l LEFT JOIN deals d ON d.lead_id = l.id GROUP BY l.id
               ORDER BY best_score DESC NULLS LAST""").fetchall()
        out = []
        for r in rows:
            d = db.lead_from_row(r)
            d["best_score"], d["matches"] = r["best_score"], r["matches"]
            if q and q.lower() not in (d["name"] + " " + " ".join(d["localities"]) + " " + d["source"]).lower():
                continue
            out.append(d)
        return out


@app.get("/api/leads/{lead_id}")
def lead_detail(lead_id: int):
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM leads WHERE id = ?", (lead_id,)).fetchone()
        if not row:
            raise HTTPException(404, "lead not found")
        lead = db.lead_from_row(row)
        deals = []
        for r in conn.execute(
                """SELECT d.*, p.title, p.locality, p.bhk, p.property_type, p.price, p.area_sqft, p.seller_flexibility
                   FROM deals d JOIN properties p ON p.id = d.property_id WHERE d.lead_id = ? ORDER BY d.score DESC""",
                (lead_id,)):
            d = dict(r)
            d["reasons"], d["features"] = json.loads(d["reasons"]), json.loads(d["features"])
            d["property_label"] = property_label(d)
            deals.append(d)
        events = [dict(r) for r in conn.execute(
            "SELECT * FROM engagements WHERE lead_id = ? ORDER BY at DESC LIMIT 40", (lead_id,))]
        return {"lead": lead, "deals": deals, "events": events}


@app.get("/api/properties")
def properties():
    with db.connect() as conn:
        return [dict(r) for r in conn.execute(
            """SELECT p.*, COUNT(d.lead_id) AS interested, MAX(d.score) AS best_score
               FROM properties p LEFT JOIN deals d ON d.property_id = p.id
               GROUP BY p.id ORDER BY best_score DESC NULLS LAST""")]


@app.get("/api/outbox")
def outbox(kind: Optional[str] = None, limit: int = 50):
    with db.connect() as conn:
        sql = """SELECT o.*, l.name AS lead_name FROM outbox o LEFT JOIN leads l ON l.id = o.lead_id"""
        args = ()
        if kind:
            sql += " WHERE o.kind = ?"
            args = (kind,)
        sql += " ORDER BY o.id DESC LIMIT ?"
        return [dict(r) for r in conn.execute(sql, args + (limit,))]


@app.get("/api/model")
def model():
    with db.connect() as conn:
        return learning.model_info(conn)


# --------------------------------------------------------------------------- W1 intake

class Intake(BaseModel):
    text: str
    source: str = "manual"
    name: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None
    property_id: Optional[int] = None


@app.post("/api/intake")
def intake(body: Intake):
    """W1: one raw enquiry in -> extracted, de-duplicated, matched, scored and routed."""
    if not body.text.strip():
        raise HTTPException(400, "text is required")
    with db.connect() as conn:
        return pipeline.ingest(conn, body.text, body.source, body.name, body.phone, body.email, body.property_id)


@app.post("/api/webhooks/{source}")
async def webhook(source: str, request: Request):
    """Generic channel webhook (portals, WhatsApp Business, Gmail via n8n, Google Forms).

    Accepts JSON and looks for the message in common field names; everything else is kept as context.
    """
    try:
        payload = await request.json()
    except Exception:
        payload = {"text": (await request.body()).decode("utf-8", "ignore")}
    if not isinstance(payload, dict):
        payload = {"text": str(payload)}
    text = next((str(payload[k]) for k in ("text", "message", "body", "msg", "enquiry", "requirement") if payload.get(k)), "")
    extras = [f"{k}: {v}" for k, v in payload.items() if k not in ("text", "message", "body", "msg") and isinstance(v, (str, int, float))]
    if extras:
        text = f"{text} | " + "; ".join(extras) if text else "; ".join(extras)
    if not text:
        raise HTTPException(400, "no message text in payload")
    with db.connect() as conn:
        return pipeline.ingest(conn, text, source, payload.get("name"), payload.get("phone") or payload.get("mobile"),
                               payload.get("email"), payload.get("property_id"))


# --------------------------------------------------------------------------- feedback & actions

class Event(BaseModel):
    lead_id: int
    kind: Literal["view", "reply", "visit", "call", "message_sent"]
    property_id: Optional[int] = None


@app.post("/api/events")
def event(body: Event):
    with db.connect() as conn:
        return pipeline.log_event(conn, body.lead_id, body.kind, body.property_id)


class Outcome(BaseModel):
    outcome: Literal["called", "visited", "closed", "lost", "override_up", "override_down"]


@app.post("/api/deals/{lead_id}/{property_id}/outcome")
def outcome(lead_id: int, property_id: int, body: Outcome):
    with db.connect() as conn:
        try:
            return pipeline.log_outcome(conn, lead_id, property_id, body.outcome)
        except KeyError:
            raise HTTPException(404, "deal not found")


@app.get("/api/deals/{lead_id}/{property_id}/brief")
def brief(lead_id: int, property_id: int):
    with db.connect() as conn:
        d = conn.execute("SELECT * FROM deals WHERE lead_id=? AND property_id=?", (lead_id, property_id)).fetchone()
        if not d:
            raise HTTPException(404, "deal not found")
        deal = dict(d)
        deal["reasons"] = json.loads(deal["reasons"])
        lead = db.lead_from_row(conn.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone())
        prop = dict(conn.execute("SELECT * FROM properties WHERE id=?", (property_id,)).fetchone())
    return actions.ai_brief(lead, prop, deal)  # outside the DB connection: may call the LLM


@app.post("/api/outbox/{item_id}/snooze")
def snooze(item_id: int):
    with db.connect() as conn:
        conn.execute("UPDATE outbox SET status='snoozed' WHERE id=?", (item_id,))
    return {"ok": True}


@app.post("/api/refresh")
def refresh():
    """W4: re-score everything with time decay, act, and send the Top-10 digest."""
    with db.connect() as conn:
        return pipeline.daily_refresh(conn)


@app.post("/api/retrain")
def retrain():
    with db.connect() as conn:
        m = learning.retrain(conn)
        from .scoring import rescore
        rescore(conn)
        return m


@app.post("/api/reset")
def reset(confirm: bool = Body(False, embed=True)):
    if not confirm:
        raise HTTPException(400, "send {\"confirm\": true} to wipe and reseed the demo data")
    seed.seed(reset=True, verbose=False)
    return {"ok": True}
