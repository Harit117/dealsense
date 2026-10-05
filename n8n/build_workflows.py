"""Generates the four importable n8n workflows (W1-W4) in this folder.

    python n8n/build_workflows.py

The workflows read two n8n environment variables:
  DEALSENSE_URL       where the DealSense API runs (default http://localhost:8000)
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID   optional broker alert channel
"""
import json
import uuid
from pathlib import Path

HERE = Path(__file__).parent
API = "{{ $env.DEALSENSE_URL || 'http://localhost:8000' }}"


def node(name, type_, params, pos, version=1, **extra):
    n = {"id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"dealsense/{name}")), "name": name, "type": type_,
         "typeVersion": version, "position": pos, "parameters": params}
    n.update(extra)
    return n


def webhook(name, path, pos, respond="lastNode"):
    return node(name, "n8n-nodes-base.webhook",
                {"httpMethod": "POST", "path": path, "responseMode": respond, "options": {}},
                pos, 2, webhookId=str(uuid.uuid5(uuid.NAMESPACE_URL, f"dealsense/hook/{path}")))


def http(name, method, url, pos, body=None, **extra):
    p = {"method": method, "url": "=" + url, "options": {}}
    if body is not None:
        p.update(sendBody=True, specifyBody="json", jsonBody="=" + body)
    return node(name, "n8n-nodes-base.httpRequest", p, pos, 4.2, **extra)


def telegram(name, text_expr, pos):
    """Broker alert via Telegram; skipped quietly when no bot token is configured."""
    return http(name, "POST", "https://api.telegram.org/bot{{ $env.TELEGRAM_BOT_TOKEN }}/sendMessage", pos,
                body="{{ JSON.stringify({ chat_id: $env.TELEGRAM_CHAT_ID, text: " + text_expr + " }) }}",
                onError="continueRegularOutput")


def code(name, js, pos):
    return node(name, "n8n-nodes-base.code", {"jsCode": js}, pos, 2)


def noop(name, pos):
    return node(name, "n8n-nodes-base.noOp", {}, pos)


def link(*pairs):
    """pairs of (from, to) or (from, to, output_index)."""
    conns = {}
    for p in pairs:
        src, dst, out = (p + (0,))[:3]
        outs = conns.setdefault(src, {"main": []})["main"]
        while len(outs) <= out:
            outs.append([])
        outs[out].append({"node": dst, "type": "main", "index": 0})
    return conns


def workflow(name, nodes, connections, notes):
    return {"name": name, "nodes": nodes + [node("About", "n8n-nodes-base.stickyNote",
                                                   {"content": notes, "height": 260, "width": 420}, [-60, -320])],
            "connections": connections, "settings": {"executionOrder": "v1"}, "pinData": {}, "active": False}


W1 = workflow(
    "DealSense W1 · Intake & Enrichment",
    [
        webhook("Lead webhook", "dealsense/lead", [0, 0]),
        code("Normalise channel payload", """// Portals, WhatsApp Business, Gmail and Google Forms all post different shapes.
return $input.all().map(item => {
  const body = item.json.body || {};
  const source = body.source || (item.json.query || {}).source || 'n8n';
  const text = body.text || body.message || body.body || body.enquiry || body.snippet || '';
  return { json: { source, payload: { ...body, text } } };
});""", [220, 0]),
        http("DealSense: extract, de-dup, match, score", "POST", API + "/api/webhooks/{{ $json.source }}", [460, 0],
             body="{{ JSON.stringify($json.payload) }}"),
        code("Summarise result", """const r = $input.first().json;
const best = (r.top_matches || [])[0];
return [{ json: {
  lead: r.lead.name, deduplicated: r.deduplicated, extraction: r.extraction,
  best_match: best ? `${best.property_label} (${Math.round(best.score)})` : null,
  route: best ? best.route : 'nurture', actions: r.actions,
}}];""", [700, 0]),
    ],
    link(("Lead webhook", "Normalise channel payload"),
         ("Normalise channel payload", "DealSense: extract, de-dup, match, score"),
         ("DealSense: extract, de-dup, match, score", "Summarise result")),
    "## W1 Intake & Enrichment\nPoint every lead channel at this webhook "
    "(POST /webhook/dealsense/lead, add `source` in the body or ?source=).\n\n"
    "DealSense extracts budget, location, BHK and urgency with Claude (or rules), de-duplicates by phone/email, "
    "then matches and scores every buyer x property pair.",
)

W2 = workflow(
    "DealSense W2 · Matching & Scoring",
    [
        webhook("Engagement webhook", "dealsense/engagement", [0, 0]),
        http("DealSense: log event + re-score buyer", "POST", API + "/api/events", [260, 0],
             body="{{ JSON.stringify({ lead_id: $json.body.lead_id, kind: $json.body.kind || 'view', "
                  "property_id: $json.body.property_id }) }}"),
        node("Hourly", "n8n-nodes-base.scheduleTrigger",
             {"rule": {"interval": [{"field": "hours", "hoursInterval": 1}]}}, [0, 220], 1.2),
        http("DealSense: current Top 10", "GET", API + "/api/top?sort=score", [260, 220]),
    ],
    link(("Engagement webhook", "DealSense: log event + re-score buyer"), ("Hourly", "DealSense: current Top 10")),
    "## W2 Matching & Scoring\nEvery new or updated lead is re-matched and scored inside DealSense (W1 triggers it).\n\n"
    "This workflow feeds engagement signals (listing views, replies, visits) from portals/WhatsApp into "
    "POST /api/events, which re-scores that buyer immediately. A deal that crosses 80 fires W3.",
)

W3 = workflow(
    "DealSense W3 · Decide & Act",
    [
        webhook("Deal scored (from DealSense)", "dealsense/deal-scored", [0, 0], respond="onReceived"),
        node("Switch on score", "n8n-nodes-base.switch",
             {"mode": "expression", "numberOutputs": 3,
              "output": "={{ $json.body.score >= 80 ? 0 : ($json.body.score >= 50 ? 1 : 2) }}"},
             [240, 0], 3),
        code("Format hot-deal alert", """const d = $input.first().json.body;
const text = `🔥 ${d.title}\\n${d.message}\\n\\nCall ${d.lead.name}: ${d.lead.phone || 'n/a'}`;
return [{ json: { ...d, text } }];""", [500, -180]),
        telegram("Instant alert to broker (Telegram)", "$json.text", [760, -180]),
        noop("Hold for daily Top-10 digest", [500, 0]),
        code("Draft nurture message", """const d = $input.first().json.body;
return [{ json: { to: d.lead.phone, name: d.lead.name, text: d.message } }];""", [500, 180]),
        noop("Send via WhatsApp Business API", [760, 180]),
    ],
    link(("Deal scored (from DealSense)", "Switch on score"),
         ("Switch on score", "Format hot-deal alert", 0),
         ("Switch on score", "Hold for daily Top-10 digest", 1),
         ("Switch on score", "Draft nurture message", 2),
         ("Format hot-deal alert", "Instant alert to broker (Telegram)"),
         ("Draft nurture message", "Send via WhatsApp Business API")),
    "## W3 Decide & Act\nDealSense POSTs every routed deal here (set N8N_DEAL_WEBHOOK on DealSense).\n\n"
    "Switch on score: **80+** instant alert to the broker · **50-79** daily Top-10 digest · **<50** auto-nurture "
    "to the lead.\n\nSwap the WhatsApp placeholder for your WhatsApp Business node once you have API access.",
)

W4 = workflow(
    "DealSense W4 · Refresh & Learn",
    [
        node("Every day 8 AM", "n8n-nodes-base.scheduleTrigger",
             {"rule": {"interval": [{"field": "cronExpression", "expression": "0 8 * * *"}]}}, [0, 0], 1.2),
        http("DealSense: re-score all with time decay", "POST", API + "/api/refresh", [240, 0]),
        http("DealSense: retrain weights from outcomes", "POST", API + "/api/retrain", [480, 0]),
        telegram("Send Top-10 digest (Telegram)",
                 "'📋 ' + $('DealSense: re-score all with time decay').first().json.digest_title + '\\n\\n' + "
                 "$('DealSense: re-score all with time decay').first().json.digest", [720, 0]),
        webhook("Broker outcome webhook", "dealsense/outcome", [0, 220]),
        http("DealSense: log outcome", "POST",
             API + "/api/deals/{{ $json.body.lead_id }}/{{ $json.body.property_id }}/outcome", [240, 220],
             body="{{ JSON.stringify({ outcome: $json.body.outcome }) }}"),
    ],
    link(("Every day 8 AM", "DealSense: re-score all with time decay"),
         ("DealSense: re-score all with time decay", "DealSense: retrain weights from outcomes"),
         ("DealSense: retrain weights from outcomes", "Send Top-10 digest (Telegram)"),
         ("Broker outcome webhook", "DealSense: log outcome")),
    "## W4 Refresh & Learn\nDaily 8 AM: re-score every deal with time decay (stale leads sink), retrain the "
    "scoring weights from logged outcomes, and send the broker the Top-10 digest.\n\n"
    "Brokers log outcomes (called / visited / closed / lost) via the dashboard or POST /webhook/dealsense/outcome.",
)

if __name__ == "__main__":
    for fname, wf in [("W1_intake_enrichment.json", W1), ("W2_matching_scoring.json", W2),
                      ("W3_decide_act.json", W3), ("W4_refresh_learn.json", W4)]:
        (HERE / fname).write_text(json.dumps(wf, indent=2, ensure_ascii=False), encoding="utf-8")
        print("wrote", fname)
