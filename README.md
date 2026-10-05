# DealSense: AI deal intelligence for real-estate brokers

> Brokers don't have a lead problem. They have a prioritization problem.

DealSense pulls in leads from every channel, uses AI to extract what each buyer wants, pairs every buyer with every suitable listing, and ranks each pair by **Deal Score = P(Close) × Deal Value**. The broker gets "Your 10 best deals today" with reasons, plus an instant alert when a deal turns hot.

**AI recommends. The broker decides.**

This repo is a working prototype of the HackSprint design, with all four workflows (W1 to W4) implemented and a synthetic Bengaluru dataset, so you can run it end to end on a laptop with no external services.

## Quick start

```bash
pip install -r requirements.txt
python run.py
```

Open http://localhost:8000. On first start the app seeds a synthetic dataset: 145 listings, 75 buyers, and 600 historical deal outcomes.

Optional: copy `.env.example` to `.env` and set `ANTHROPIC_API_KEY` to have Claude do the lead extraction and write call briefs. Without a key, DealSense uses its built-in rule-based parser, so everything still works offline.

Run the tests:

```bash
python -m pytest -q
```

## What you can do in the demo

| Screen | What it shows |
|---|---|
| **Today** | Today's Top 10 opportunities (one best property per buyer), sortable by score or by expected commission, plus a WhatsApp-style panel of hot-deal alerts. "Run daily refresh" runs W4 by hand. |
| **Deal drawer** (click any row) | Score breakdown: P(close) × commission, plain-language reasons, signal bars, an AI call brief, and outcome buttons (Called / Site visit / Closed / Lost / Override ▲▼) that feed the learning loop. "Buyer viewed listing" simulates engagement so you can watch a deal turn hot and fire an alert. |
| **Lead intake** | Paste any messy enquiry (portal, WhatsApp, email, call notes) and see the extracted fields, de-duplication, top matches and routing. |
| **Buyers / Listings** | Every buyer with their best score; every listing with how many buyers it matches. |
| **Learning** | Day-1 rule weights next to the current weights, holdout AUC (rules vs learned), and a Retrain button. |
| **Outbox** | Every alert, digest and nurture message. These are also pushed to Telegram or Slack if configured. |

Suggested 2-minute demo: open Rohan S. × 3BHK Whitefield (score about 96) → read the reasons → paste the WhatsApp example on the Intake tab → open a warm deal and click "Buyer viewed listing" a couple of times until it crosses 80 and fires an alert → log a few outcomes → Learning tab → Retrain.

## Architecture

```
Lead sources ──► W1 Intake & Enrichment ──► Leads / Listings DB ──► W2 Matching & Scoring ──► W3 Decide & Act
(portals, WhatsApp,   extract fields (Claude          (SQLite; same       filter candidates,           80+   → instant alert
 email, forms,        or rules), de-dup by           schema as the       compute 10 signals,          50–79 → Top-10 digest
 calls, walk-ins)     phone/email, merge             Supabase plan)      P(close) + reasons           <50   → auto-nurture
                                                                                  ▲
                         W4 Refresh & Learn: daily 08:00 re-score with time decay;│
                         broker outcomes → retrain weights ───────────────────────┘
```

| Workflow | Code | Trigger |
|---|---|---|
| W1 Intake & Enrichment | `dealsense/extract.py`, `pipeline.ingest` | `POST /api/intake`, `POST /api/webhooks/{source}` |
| W2 Matching & Scoring | `dealsense/scoring.py` | Every new or updated lead, every engagement event |
| W3 Decide & Act | `dealsense/actions.py` | After every scoring run |
| W4 Refresh & Learn | `dealsense/learning.py`, `pipeline.daily_refresh` | Built-in daily scheduler, `POST /api/refresh`, `POST /api/retrain`, and automatically every 5 logged outcomes |

The deck's plan runs orchestration in n8n. The API is built to sit behind it. Each n8n workflow becomes a trigger node (Webhook, Gmail, WhatsApp Business or Cron) followed by an HTTP Request node pointed at the matching endpoint above. `/api/webhooks/{source}` accepts any JSON payload and looks for the message in `text`, `message`, `body` or `enquiry`.

## How it decides

We score the match between a **specific buyer and a specific property**, not just the lead.

`P(close) = sigmoid(b + Σ wᵢ · signalᵢ)`. The score shown is P(close) × 100, and the Top 10 can also be ranked by expected commission, P(close) × price × 2%.

| Signal | How it's computed |
|---|---|
| Budget fit | Price vs the buyer's budget. A gap of up to 20% fades the fit, and a flexible seller absorbs part of it. |
| Location | 1.0 for an exact locality, 0.6 for a neighbouring one (hand-built Bengaluru neighbour map) |
| Requirements | BHK and property type match |
| Seller flexibility | From the listing |
| Buyer urgency | Timeline extracted from the messages |
| Financing | Loan pre-approved or cash |
| Engagement | Views, visits and calls on *this* listing in the last 14 days |
| Reply behaviour | The buyer's replies relative to messages sent (smoothed) |
| Recency | Time decay with a 14-day constant, so stale leads sink and fresh intent rises |
| Your conversion pattern | The broker's smoothed historical win rate in that locality |

**Day-1 ready, then learns.** It starts with hand-set rule weights. Every outcome logged (visited or closed = 1, lost = 0, overrides) stores a snapshot of the deal's signals. A logistic regression is fitted on those snapshots and blended with the rules at `α = n / (n + 250)`, so the weights move toward what actually closes for this broker as data builds up. On the seeded history the holdout AUC goes from about 0.78 (rules) to about 0.81 (learned).

Every score comes with its reasons, the broker can override it, and overrides become training data.

## Data

All data is **synthetic** (`dealsense/seed.py`): 16 Bengaluru localities with indicative 2025–26 price per sq ft, randomly generated listings, buyers anchored on that inventory, and raw enquiries written in each channel's style. The enquiries go through the real extractor during seeding. The five buyers from the deck's mock-up (Rohan S., Priya M., Arjun K., Neha R., Vikram P.) are included. The 600 historical outcomes come from a hidden "true" model that differs from the day-1 rules, so the learner has something real to discover. To use real data, replace the seed with your listings and connect your lead channels to the webhook.

## API

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/summary` | KPIs, routing counts, model status |
| GET | `/api/top?sort=score\|value` | Top-10 (best property per buyer) |
| GET | `/api/leads`, `/api/leads/{id}` | Buyers, and one buyer with all scored matches and events |
| GET | `/api/properties` | Listings with match counts |
| POST | `/api/intake` | `{text, source, name?, phone?, email?, property_id?}`, runs W1 → W2 → W3 |
| POST | `/api/webhooks/{source}` | Generic channel webhook (any JSON) |
| POST | `/api/events` | `{lead_id, kind: view\|reply\|visit\|call, property_id?}` |
| POST | `/api/deals/{lead}/{property}/outcome` | `{outcome: called\|visited\|closed\|lost\|override_up\|override_down}` |
| GET | `/api/deals/{lead}/{property}/brief` | AI call brief (Claude, or a template without a key) |
| POST | `/api/refresh` | W4 daily refresh + digest |
| POST | `/api/retrain`, GET `/api/model` | Learning |
| GET | `/api/outbox?kind=alert\|digest\|nurture` | Messages sent or queued |
| POST | `/api/reset` | `{confirm: true}` wipes and reseeds the demo |

Interactive docs: http://localhost:8000/docs

## Tech

Python · FastAPI · SQLite · scikit-learn · Claude (`claude-opus-5-5`, structured outputs) · vanilla JS dashboard with no build step. The schema maps one-to-one onto the Supabase/Postgres tables in the design.

## Roadmap

Cross-brokerage deal matching · auto deal briefs and site-visit plans · rentals and commercial leasing · call transcription → auto-enrichment · consent-based data handling designed around India's DPDP Act.
