"""
app.py — MASA Dashboard
=======================
Lightweight Flask app that reads from MongoDB and serves a live
agentic autoscaler dashboard over HTTP.

Collections read (all from mongo_scaling db):
  microservices      — perception metrics (cpu, memory, delivered, replicas, label)
  scaling_plans      — action-agent plan history (status, replicas, confidence)
  model_feedback     — reflection-agent QA verdicts
  governance_context — understanding-agent CR snapshot
"""

import os
import logging
from datetime import datetime, timezone, timedelta
from flask import Flask, jsonify, render_template_string

from pymongo import MongoClient, DESCENDING

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

app = Flask(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

MONGO_SECRET_DIR   = os.getenv("MONGO_SECRET_DIR", "/etc/secrets/mongo")
MONGO_HOST         = os.getenv(
    "MONGO_HOST",
    "mas-mongo-ce-0.mas-mongo-ce-svc.mongoce.svc.cluster.local:27017,"
    "mas-mongo-ce-1.mas-mongo-ce-svc.mongoce.svc.cluster.local:27017,"
    "mas-mongo-ce-2.mas-mongo-ce-svc.mongoce.svc.cluster.local:27017",
)
MONGO_TLS          = os.getenv("MONGO_TLS", "true").lower() == "true"
MONGO_TLS_INSECURE = os.getenv("MONGO_TLS_INSECURE", "true").lower() == "true"
WINDOW_MINUTES     = int(os.getenv("WINDOW_MINUTES", "60"))
STALE_SECONDS      = 180  # 3 minutes — agent card goes stale after this

# ---------------------------------------------------------------------------
# MongoDB helpers
# ---------------------------------------------------------------------------

def _read_secret(path, fallback_env):
    try:
        with open(path) as f:
            v = f.read().strip()
            if v:
                return v
    except (OSError, IOError):
        pass
    return os.environ.get(fallback_env, "")


def _mongo():
    username = _read_secret(f"{MONGO_SECRET_DIR}/username", "MONGO_USER")
    password = _read_secret(f"{MONGO_SECRET_DIR}/password", "MONGO_PASSWORD")
    return MongoClient(
        f"mongodb://{username}:{password}@{MONGO_HOST}",
        authMechanism="SCRAM-SHA-256",
        tlsInsecure=MONGO_TLS_INSECURE,
        authSource="admin",
        connectTimeoutMS=5000,
        socketTimeoutMS=5000,
        maxPoolSize=5,
        tls=MONGO_TLS,
    )


def _since():
    return datetime.now(tz=timezone.utc) - timedelta(minutes=WINDOW_MINUTES)


def _age_status(ts):
    """Return 'healthy'/'stale'/'unknown' based on how old the timestamp is."""
    if ts is None:
        return "unknown"
    now = datetime.now(tz=timezone.utc)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    age = (now - ts).total_seconds()
    return "healthy" if age <= STALE_SECONDS else "stale"


# ---------------------------------------------------------------------------
# Data API
# ---------------------------------------------------------------------------

@app.route("/api/data")
def api_data():
    try:
        client = _mongo()
        db = client["mongo_scaling"]
        since = _since()
        now_utc = datetime.now(tz=timezone.utc)

        # ── Perception ────────────────────────────────────────────────────────
        metrics_cursor = list(
            db["microservices"].find(
                {"timestamp": {"$gte": since}},
                {"_id": 0, "timestamp": 1, "m_cpu": 1, "m_memory": 1,
                 "replicas": 1, "label": 1, "m_delivered": 1, "m_undelivered": 1,
                 "m_percentage__delivered": 1, "m_quantile_90": 1}
            ).sort("timestamp", DESCENDING).limit(120)
        )
        metrics_cursor.reverse()

        timestamps    = [r["timestamp"].strftime("%H:%M:%S") for r in metrics_cursor]
        cpu           = [r.get("m_cpu") for r in metrics_cursor]
        memory        = [r.get("m_memory") for r in metrics_cursor]
        replicas      = [r.get("replicas") for r in metrics_cursor]
        labels        = [r.get("label") for r in metrics_cursor]
        delivered_pct = [r.get("m_percentage__delivered") for r in metrics_cursor]

        latest_perc  = metrics_cursor[-1] if metrics_cursor else {}
        perc_ts      = latest_perc.get("timestamp")
        perc_label   = latest_perc.get("label")
        perc_latency = latest_perc.get("m_quantile_90")
        perc_status  = _age_status(perc_ts)

        current_replicas = latest_perc.get("replicas")
        current_label    = perc_label
        current_cpu      = latest_perc.get("m_cpu")
        current_memory   = latest_perc.get("m_memory")
        current_del_pct  = latest_perc.get("m_percentage__delivered")

        all_labels = [r.get("label") for r in metrics_cursor if r.get("label") is not None]
        label1_window_ratio = round(all_labels.count(1) / len(all_labels), 3) if all_labels else None
        label2_window_ratio = round(all_labels.count(2) / len(all_labels), 3) if all_labels else None
        label3_ratio        = round(all_labels.count(3) / len(all_labels), 3) if all_labels else None

        # ── Planning / Action (scaling_plans) ─────────────────────────────────
        plans_raw = list(
            db["scaling_plans"].find(
                {"created_at": {"$gte": since}},
                {"_id": 1, "status": 1, "decision": 1, "current_replicas": 1,
                 "desired_replicas": 1, "confidence": 1, "created_at": 1,
                 "applied_at": 1, "reason": 1, "error": 1}
            ).sort("created_at", DESCENDING).limit(30)
        )

        # Planning agent card — latest plan written
        latest_plan  = plans_raw[0] if plans_raw else {}
        plan_ts      = latest_plan.get("created_at")   # raw datetime
        plan_status  = _age_status(plan_ts)
        plan_decision   = latest_plan.get("decision", "")
        plan_confidence = latest_plan.get("confidence")

        # Action agent card — latest executed plan
        raw_action = next(
            (p for p in plans_raw if p.get("status") in ("applied", "skipped", "failed")),
            {}
        )
        action_ts_raw    = raw_action.get("applied_at") or raw_action.get("created_at")
        action_age_status = _age_status(action_ts_raw)
        action_status    = raw_action.get("status", "unknown")
        action_curr      = raw_action.get("current_replicas")
        action_des       = raw_action.get("desired_replicas")
        if action_curr is not None and action_des is not None:
            diff = action_des - action_curr
            action_delta = f"+{diff}" if diff > 0 else str(diff)
        else:
            action_delta = "—"
        action_ts_str = (
            action_ts_raw.strftime("%H:%M:%S")
            if action_ts_raw and hasattr(action_ts_raw, "strftime") else "—"
        )

        # Serialise plans for the table
        plans = []
        for p in plans_raw:
            row = dict(p)
            # Serialise ObjectId → short string for the UI
            raw_id = row.pop("_id", None)
            full_id = str(raw_id) if raw_id else ""
            row["plan_id"]       = full_id
            row["plan_id_short"] = f"…{full_id[-8:]}" if full_id else "?"
            raw_ts = row["created_at"]
            row["created_at"] = raw_ts.strftime("%H:%M:%S") if hasattr(raw_ts, "strftime") else str(raw_ts)
            if row.get("applied_at") and hasattr(row["applied_at"], "strftime"):
                row["applied_at"] = row["applied_at"].strftime("%H:%M:%S")
            curr = row.get("current_replicas")
            des  = row.get("desired_replicas")
            if curr is not None and des is not None:
                diff = des - curr
                row["delta"] = f"+{diff}" if diff > 0 else str(diff)
            else:
                row["delta"] = "—"
            plans.append(row)

        plan_counts = {"applied": 0, "skipped": 0, "failed": 0, "pending": 0}
        for p in plans:
            s = p.get("status", "")
            if s in plan_counts:
                plan_counts[s] += 1

        # ── Reflection ────────────────────────────────────────────────────────
        feedbacks = list(
            db["model_feedback"].find(
                {"evaluated_at": {"$gte": since}},
                {"_id": 0, "evaluated_at": 1, "verdict": 1, "label1_ratio": 1,
                 "scaleup_label1_ratio": 1, "scaledown_label1_ratio": 1,
                 "rolling_mae": 1, "model_age_seconds": 1, "consecutive_failures": 1}
            ).sort("evaluated_at", DESCENDING).limit(20)
        )
        refl_ts = None
        for fb in feedbacks:
            raw_ev = fb.get("evaluated_at")
            if raw_ev and hasattr(raw_ev, "strftime"):
                if refl_ts is None:
                    refl_ts = raw_ev
                fb["evaluated_at"] = raw_ev.strftime("%H:%M:%S")

        refl_status  = _age_status(refl_ts)
        latest_feedback = feedbacks[0] if feedbacks else {}
        verdict      = latest_feedback.get("verdict", "unknown")
        rolling_mae  = latest_feedback.get("rolling_mae")
        label1_ratio      = latest_feedback.get("label1_ratio")
        scaleup_l1_ratio  = latest_feedback.get("scaleup_label1_ratio")
        scaledown_l1_ratio = latest_feedback.get("scaledown_label1_ratio")
        model_age_s        = latest_feedback.get("model_age_seconds")
        consecutive_failures = latest_feedback.get("consecutive_failures", 0)
        model_age_str = (
            f"{int(model_age_s // 3600)}h {int((model_age_s % 3600) // 60)}m"
            if model_age_s is not None else "N/A"
        )

        # ── Unified event feed ────────────────────────────────────────────────
        events = []

        # Perception events (last 20 perception rows)
        for r in metrics_cursor[-20:]:
            ts = r.get("timestamp")
            lbl = r.get("label")
            lat = r.get("m_quantile_90")
            if ts and lbl is not None:
                label_str = "EXCEEDING" if lbl == 3 else ("OK" if lbl == 2 else "BREACH")
                events.append({
                    "agent": "Perception",
                    "ts_str": ts.strftime("%H:%M:%S"),
                    "summary": f"label={lbl} ({label_str}) · P90 {round(lat,3) if lat is not None else '—'}s",
                    "level": "good" if lbl == 3 else ("warn" if lbl == 2 else "error"),
                })

        # Planning events
        for p in plans[:20]:
            dec      = p.get("decision", "")
            conf     = p.get("confidence")
            curr2    = p.get("current_replicas")
            des2     = p.get("desired_replicas")
            delta2   = p.get("delta", "—")
            ts_str   = p.get("created_at", "")
            short_id = p.get("plan_id_short", "?")
            if dec == "scale_horizontal":
                summary = f"scale {curr2}→{des2} ({delta2}) · conf {round(conf,3) if conf is not None else '—'} [plan …{short_id}]"
                level = "warn"
            else:
                summary = f"hold at {curr2} · conf {round(conf,3) if conf is not None else '—'}"
                level = "good"
            events.append({"agent": "Planning", "ts_str": ts_str, "summary": summary, "level": level})

        # Action events
        for p in plans[:20]:
            st = p.get("status")
            if st in ("applied", "failed", "skipped"):
                ts_str  = p.get("applied_at") or p.get("created_at", "")
                delta2  = p.get("delta", "—")
                curr2   = p.get("current_replicas")
                des2    = p.get("desired_replicas")
                err     = p.get("error") or ""
                plan_id  = p.get("plan_id", "")
                short_id = p.get("plan_id_short", "?")
                if st == "applied":
                    summary = f"applied {curr2}→{des2} ({delta2})"
                    level = "good"
                elif st == "skipped":
                    summary = f"skipped — already at {curr2} [plan …{short_id}]"
                    level = "warn"
                else:
                    summary = f"FAILED: {err[:70]}"
                    level = "error"
                events.append({"agent": "Action", "ts_str": ts_str, "summary": summary, "level": level})

        # Reflection events
        for fb in feedbacks[:20]:
            ts_str = fb.get("evaluated_at", "")
            v      = fb.get("verdict", "unknown")
            mae    = fb.get("rolling_mae")
            l1     = fb.get("label1_ratio")
            events.append({
                "agent": "Reflection",
                "ts_str": ts_str,
                "summary": f"verdict={v} · MAE {round(mae,4) if mae is not None else '—'} · L1 {round(l1,3) if l1 is not None else '—'}",
                "level": "good" if v == "good" else ("warn" if v == "degraded" else "error"),
            })

        # Sort newest first, cap at 20
        events.sort(key=lambda e: e["ts_str"], reverse=True)
        events = events[:20]

        # ── Governance context ────────────────────────────────────────────────
        gov = db["governance_context"].find_one({}, {"_id": 0}) or {}

        client.close()

        return jsonify({
            "window_minutes": WINDOW_MINUTES,
            "updated_at": now_utc.strftime("%H:%M:%S UTC"),
            "agents": {
                "perception": {
                    "status": perc_status,
                    "last_seen": perc_ts.strftime("%H:%M:%S") if perc_ts and hasattr(perc_ts, "strftime") else "—",
                    "label": perc_label,
                    "latency_p90": round(perc_latency, 3) if perc_latency is not None else None,
                },
                "planning": {
                    "status": plan_status,
                    "last_seen": plan_ts.strftime("%H:%M:%S") if plan_ts and hasattr(plan_ts, "strftime") else "—",
                    "decision": plan_decision,
                    "confidence": round(plan_confidence, 3) if plan_confidence is not None else None,
                },
                "action": {
                    "status": action_age_status,
                    "last_seen": action_ts_str,
                    "last_status": action_status,
                    "delta": action_delta,
                    "current_replicas": action_curr,
                    "desired_replicas": action_des,
                },
                "reflection": {
                    "status": refl_status,
                    "last_seen": refl_ts.strftime("%H:%M:%S") if refl_ts and hasattr(refl_ts, "strftime") else "—",
                    "verdict": verdict,
                    "rolling_mae": round(rolling_mae, 4) if rolling_mae is not None else None,
                },
            },
            "events": events,
            "timeseries": {
                "timestamps": timestamps,
                "cpu": cpu,
                "memory": memory,
                "replicas": replicas,
                "labels": labels,
                "delivered_pct": delivered_pct,
            },
            "current": {
                "replicas": current_replicas,
                "label": current_label,
                "cpu": round(current_cpu, 4) if current_cpu is not None else None,
                "memory": round(current_memory, 2) if current_memory is not None else None,
                "delivered_pct": round(current_del_pct, 2) if current_del_pct is not None else None,
                "label1_window_ratio": label1_window_ratio,
                "label2_window_ratio": label2_window_ratio,
                "label3_ratio": label3_ratio,
            },
            "plans": plans,
            "plan_counts": plan_counts,
            "quality": {
                "verdict": verdict,
                "label1_ratio": label1_ratio,
                "scaleup_label1_ratio": scaleup_l1_ratio,
                "scaledown_label1_ratio": scaledown_l1_ratio,
                "rolling_mae": rolling_mae,
                "model_age": model_age_str,
                "consecutive_failures": consecutive_failures,
            },
            "feedbacks": feedbacks,
            "governance": gov,
        })
    except Exception as e:
        logger.error("Dashboard data error: %s", e)
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------------------
# Dashboard HTML
# ---------------------------------------------------------------------------

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>MASA Dashboard</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4/dist/chart.umd.min.js"></script>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, "Segoe UI", system-ui, sans-serif; background: #0f1117; color: #e2e8f0; font-size: 14px; }
  header { background: #1a1d27; border-bottom: 1px solid #2d3148; padding: 14px 24px; display: flex; align-items: center; justify-content: space-between; }
  header h1 { font-size: 18px; font-weight: 600; letter-spacing: 0.5px; color: #fff; }
  header span { font-size: 12px; color: #64748b; }

  .section-title { font-size: 11px; color: #64748b; text-transform: uppercase; letter-spacing: 1px; padding: 18px 24px 8px; font-weight: 600; }

  /* Agent cards */
  .agents { display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; padding: 0 24px; }
  .agent-card { background: #1a1d27; border: 1px solid #2d3148; border-radius: 10px; padding: 16px 18px; position: relative; overflow: hidden; }
  .agent-card::before { content: ''; position: absolute; top: 0; left: 0; right: 0; height: 3px; background: var(--accent, #2d3148); }
  .agent-card.perception { --accent: #60a5fa; }
  .agent-card.planning   { --accent: #a78bfa; }
  .agent-card.action     { --accent: #34d399; }
  .agent-card.reflection { --accent: #f472b6; }
  .agent-name { font-size: 11px; font-weight: 700; text-transform: uppercase; letter-spacing: 1px; color: #94a3b8; margin-bottom: 10px; display: flex; align-items: center; gap: 8px; }
  .agent-name .dot { width: 8px; height: 8px; border-radius: 50%; background: var(--accent); flex-shrink: 0; }
  .status-badge { font-size: 10px; font-weight: 700; text-transform: uppercase; padding: 2px 8px; border-radius: 20px; margin-left: auto; }
  .status-healthy { background: #14532d; color: #22c55e; }
  .status-stale   { background: #2d1a00; color: #f59e0b; }
  .status-unknown { background: #1e2130; color: #64748b; }
  .agent-output { font-size: 18px; font-weight: 700; color: #fff; margin: 4px 0 2px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .agent-sub { font-size: 11px; color: #64748b; }

  /* Metric stat cards */
  .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 10px; padding: 0 24px; }
  .card { background: #1a1d27; border: 1px solid #2d3148; border-radius: 8px; padding: 13px 15px; }
  .card .label { font-size: 10px; color: #64748b; text-transform: uppercase; letter-spacing: 0.8px; margin-bottom: 4px; }
  .card .value { font-size: 22px; font-weight: 700; color: #fff; }
  .card .sub { font-size: 10px; color: #64748b; margin-top: 3px; }
  .good     { color: #22c55e !important; }
  .degraded { color: #f59e0b !important; }
  .poor     { color: #ef4444 !important; }
  .unknown  { color: #64748b !important; }

  /* Charts */
  .charts { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; padding: 0 24px; }
  .chart-card { background: #1a1d27; border: 1px solid #2d3148; border-radius: 8px; padding: 14px 16px; }
  .chart-card h3 { font-size: 10px; color: #94a3b8; text-transform: uppercase; letter-spacing: 0.8px; margin-bottom: 10px; }
  .chart-card canvas { max-height: 150px; }

  /* Bottom */
  .bottom { display: grid; grid-template-columns: 1.6fr 1fr; gap: 12px; padding: 0 24px 24px; }
  .table-card { background: #1a1d27; border: 1px solid #2d3148; border-radius: 8px; padding: 14px 16px; }
  .table-card h3 { font-size: 10px; color: #94a3b8; text-transform: uppercase; letter-spacing: 0.8px; margin-bottom: 10px; }
  table { width: 100%; border-collapse: collapse; font-size: 11px; }
  th { color: #64748b; text-align: left; padding: 5px 7px; border-bottom: 1px solid #2d3148; font-weight: 500; }
  td { padding: 5px 7px; border-bottom: 1px solid #1e2130; color: #cbd5e1; }
  tr:last-child td { border-bottom: none; }
  .badge { display: inline-block; padding: 2px 7px; border-radius: 4px; font-size: 10px; font-weight: 600; }
  .badge-applied  { background: #14532d; color: #22c55e; }
  .badge-skipped  { background: #1e3a5f; color: #60a5fa; }
  .badge-failed   { background: #450a0a; color: #ef4444; }
  .badge-pending  { background: #2d2a00; color: #f59e0b; }
  .badge-good     { background: #14532d; color: #22c55e; }
  .badge-degraded { background: #2d1a00; color: #f59e0b; }
  .badge-poor     { background: #450a0a; color: #ef4444; }
  .badge-unknown  { background: #1e2130; color: #64748b; }

  /* Event feed */
  .event-feed { display: flex; flex-direction: column; gap: 5px; max-height: 500px; overflow-y: auto; }
  .event { display: flex; align-items: flex-start; gap: 8px; padding: 7px 9px; border-radius: 6px; background: #12141e; border-left: 3px solid #2d3148; font-size: 11px; }
  .event.good  { border-left-color: #22c55e; }
  .event.warn  { border-left-color: #f59e0b; }
  .event.error { border-left-color: #ef4444; }
  .event-ts    { color: #64748b; white-space: nowrap; min-width: 52px; font-family: monospace; font-size: 10px; padding-top: 1px; }
  .event-agent { font-weight: 700; font-size: 10px; min-width: 68px; white-space: nowrap; }
  .event-agent.Perception { color: #60a5fa; }
  .event-agent.Planning   { color: #a78bfa; }
  .event-agent.Action     { color: #34d399; }
  .event-agent.Reflection { color: #f472b6; }
  .event-summary { color: #94a3b8; flex: 1; }

  @media (max-width: 900px) { .agents { grid-template-columns: 1fr 1fr; } .charts, .bottom { grid-template-columns: 1fr; } }
  @media (max-width: 500px)  { .agents { grid-template-columns: 1fr; } }
</style>
</head>
<body>
<header>
  <h1>⚡ MASA — Agentic Autoscaler</h1>
  <span id="updated">Loading…</span>
</header>

<div class="section-title">Agent Status</div>
<div class="agents">
  <div class="agent-card perception">
    <div class="agent-name"><span class="dot"></span>Perception<span class="status-badge" id="ag-perc-status">—</span></div>
    <div class="agent-output" id="ag-perc-output">—</div>
    <div class="agent-sub" id="ag-perc-sub">last seen —</div>
  </div>
  <div class="agent-card planning">
    <div class="agent-name"><span class="dot"></span>Planning<span class="status-badge" id="ag-plan-status">—</span></div>
    <div class="agent-output" id="ag-plan-output">—</div>
    <div class="agent-sub" id="ag-plan-sub">last seen —</div>
  </div>
  <div class="agent-card action">
    <div class="agent-name"><span class="dot"></span>Action<span class="status-badge" id="ag-act-status">—</span></div>
    <div class="agent-output" id="ag-act-output">—</div>
    <div class="agent-sub" id="ag-act-sub">last seen —</div>
  </div>
  <div class="agent-card reflection">
    <div class="agent-name"><span class="dot"></span>Reflection<span class="status-badge" id="ag-refl-status">—</span></div>
    <div class="agent-output" id="ag-refl-output">—</div>
    <div class="agent-sub" id="ag-refl-sub">last seen —</div>
  </div>
</div>

<div class="section-title">System Metrics</div>
<div class="grid">
  <div class="card"><div class="label">Replicas</div><div class="value" id="val-replicas">—</div><div class="sub">current live</div></div>
  <div class="card"><div class="label">SLA Label</div><div class="value" id="val-label">—</div><div class="sub">1 breach · 2 ok · 3 exceeding</div></div>
  <div class="card"><div class="label">Delivered %</div><div class="value" id="val-delivered">—</div><div class="sub">vs 90% target</div></div>
  <div class="card"><div class="label">CPU</div><div class="value" id="val-cpu">—</div><div class="sub">total cores</div></div>
  <div class="card"><div class="label">Memory</div><div class="value" id="val-memory">—</div><div class="sub">total MB</div></div>
  <div class="card"><div class="label">Label-1 Rate</div><div class="value poor" id="val-l1window">—</div><div class="sub">breach fraction</div></div>
  <div class="card"><div class="label">Label-2 Rate</div><div class="value" id="val-l2window">—</div><div class="sub">meeting SLA</div></div>
  <div class="card"><div class="label">Label-3 Rate</div><div class="value good" id="val-l3ratio">—</div><div class="sub">exceeding SLA</div></div>
  <div class="card"><div class="label">↑ After Scale-Up</div><div class="value" id="val-scaleup-l1">—</div><div class="sub">L1 rate post-scaleup</div></div>
  <div class="card"><div class="label">↓ After Scale-Down</div><div class="value" id="val-scaledown-l1">—</div><div class="sub">L1 rate post-scaledown</div></div>
  <div class="card"><div class="label">Model Age</div><div class="value" id="val-model-age">—</div><div class="sub">since last train</div></div>
</div>

<div class="section-title">Time Series</div>
<div class="charts">
  <div class="chart-card"><h3>Replicas</h3><canvas id="chart-replicas"></canvas></div>
  <div class="chart-card"><h3>CPU &amp; Memory</h3><canvas id="chart-resources"></canvas></div>
  <div class="chart-card"><h3>Delivered %</h3><canvas id="chart-delivered"></canvas></div>
  <div class="chart-card"><h3>SLA Label</h3><canvas id="chart-labels"></canvas></div>
</div>

<div class="section-title">Decisions &amp; Events</div>
<div class="bottom">
  <div style="display:flex;flex-direction:column;gap:12px">
    <div class="table-card">
      <h3>Scaling Plans</h3>
      <table>
        <thead><tr><th>Created</th><th>Applied</th><th>Replicas</th><th>Δ</th><th>Reason</th><th>Conf</th><th>Status</th><th>Error</th><th>Plan ID</th></tr></thead>
        <tbody id="plans-tbody"><tr><td colspan="9" style="color:#64748b">Loading…</td></tr></tbody>
      </table>
    </div>
    <div class="table-card">
      <h3>QA History</h3>
      <table>
        <thead><tr><th>Time</th><th>Verdict</th><th>L1</th><th>↑ L1</th><th>↓ L1</th><th>MAE</th><th>Consec.</th></tr></thead>
        <tbody id="qa-tbody"><tr><td colspan="7" style="color:#64748b">Loading…</td></tr></tbody>
      </table>
    </div>
  </div>
  <div class="table-card">
    <h3>Live Event Feed</h3>
    <div class="event-feed" id="event-feed"><div style="color:#64748b;font-size:12px">Loading…</div></div>
  </div>
</div>

<script>
const COLORS = { cpu:'#60a5fa', memory:'#a78bfa', replicas:'#34d399', delivered:'#f472b6', label:'#fbbf24' };

function mkChart(id, type, labels, datasets, opts={}) {
  const ctx = document.getElementById(id).getContext('2d');
  return new Chart(ctx, {
    type, data: { labels, datasets },
    options: {
      responsive:true, maintainAspectRatio:true,
      plugins:{ legend:{ labels:{ color:'#94a3b8', font:{ size:10 } } } },
      scales:{
        x:{ ticks:{ color:'#64748b', maxTicksLimit:8, font:{ size:9 } }, grid:{ color:'#1e2130' } },
        y:{ ticks:{ color:'#64748b', font:{ size:9 } }, grid:{ color:'#1e2130' } },
        ...(opts.y1 ? { y1:{ position:'right', ticks:{ color:'#64748b', font:{ size:9 } }, grid:{ drawOnChartArea:false } } } : {})
      },
      animation:false,
    }
  });
}

let charts = {};

function initCharts(d) {
  const ts = d.timeseries;
  charts.replicas  = mkChart('chart-replicas','line',ts.timestamps,[{label:'Replicas',data:ts.replicas,borderColor:COLORS.replicas,backgroundColor:'rgba(52,211,153,0.1)',fill:true,tension:0.3,pointRadius:0}]);
  charts.resources = mkChart('chart-resources','line',ts.timestamps,[
    {label:'CPU (cores)',data:ts.cpu,borderColor:COLORS.cpu,backgroundColor:'rgba(96,165,250,0.1)',fill:true,tension:0.3,pointRadius:0},
    {label:'Memory (MB)',data:ts.memory,borderColor:COLORS.memory,backgroundColor:'rgba(167,139,250,0.1)',fill:true,tension:0.3,pointRadius:0,yAxisID:'y1'},
  ],{y1:true});
  charts.delivered = mkChart('chart-delivered','line',ts.timestamps,[{label:'Delivered %',data:ts.delivered_pct,borderColor:COLORS.delivered,backgroundColor:'rgba(244,114,182,0.1)',fill:true,tension:0.3,pointRadius:0}]);
  charts.labels    = mkChart('chart-labels','line',ts.timestamps,[{label:'SLA Label',data:ts.labels,borderColor:COLORS.label,backgroundColor:'rgba(251,191,36,0.1)',fill:true,tension:0,pointRadius:0,stepped:true}]);
}

function updateCharts(d) {
  const ts = d.timeseries;
  [['replicas',[ts.replicas]],['resources',[ts.cpu,ts.memory]],['delivered',[ts.delivered_pct]],['labels',[ts.labels]]].forEach(([name,datasets]) => {
    const c = charts[name];
    c.data.labels = ts.timestamps;
    datasets.forEach((data,i) => c.data.datasets[i].data = data);
    c.update('none');
  });
}

function fmt(v, d=2) { return v !== null && v !== undefined ? Number(v).toFixed(d) : '—'; }

function updateAgents(a) {
  // Perception
  const lbl = a.perception.label;
  const lat  = a.perception.latency_p90;
  const percOut = lbl != null ? `label=${lbl} · ${lat != null ? lat+'s' : '—'}` : '—';
  const percCls = lbl===3?'good':lbl===2?'degraded':lbl===1?'poor':'unknown';
  document.getElementById('ag-perc-output').textContent = percOut;
  document.getElementById('ag-perc-output').className = 'agent-output '+percCls;
  document.getElementById('ag-perc-sub').textContent = `P90 latency · last seen ${a.perception.last_seen}`;
  const ps = document.getElementById('ag-perc-status');
  ps.textContent = a.perception.status; ps.className = `status-badge status-${a.perception.status}`;

  // Planning
  const dec  = a.planning.decision || '';
  const conf = a.planning.confidence;
  const planOut = dec==='scale_horizontal' ? `⬆ scale · conf ${fmt(conf,3)}` :
                  dec==='no_scaling_needed' ? `= hold · conf ${fmt(conf,3)}` : '—';
  document.getElementById('ag-plan-output').textContent = planOut;
  document.getElementById('ag-plan-output').className = 'agent-output '+(dec==='no_scaling_needed'?'good':dec==='scale_horizontal'?'degraded':'unknown');
  document.getElementById('ag-plan-sub').textContent = `last decision · last seen ${a.planning.last_seen}`;
  const ps2 = document.getElementById('ag-plan-status');
  ps2.textContent = a.planning.status; ps2.className = `status-badge status-${a.planning.status}`;

  // Action
  const actSt = a.action.last_status || '';
  const delta  = a.action.delta || '—';
  const curr   = a.action.current_replicas;
  const des    = a.action.desired_replicas;
  const actOut = actSt==='applied'  ? `${curr}→${des} (${delta})` :
                 actSt==='skipped'  ? `held at ${curr}` :
                 actSt==='failed'   ? 'FAILED' : '—';
  document.getElementById('ag-act-output').textContent = actOut;
  document.getElementById('ag-act-output').className = 'agent-output '+(actSt==='applied'?'good':actSt==='skipped'?'degraded':actSt==='failed'?'poor':'unknown');
  document.getElementById('ag-act-sub').textContent = `last action · last seen ${a.action.last_seen}`;
  const ps3 = document.getElementById('ag-act-status');
  ps3.textContent = a.action.status; ps3.className = `status-badge status-${a.action.status}`;

  // Reflection
  const v   = a.reflection.verdict || 'unknown';
  const mae = a.reflection.rolling_mae;
  document.getElementById('ag-refl-output').textContent = `${v} · MAE ${fmt(mae,4)}`;
  document.getElementById('ag-refl-output').className = 'agent-output '+(v==='good'?'good':v==='degraded'?'degraded':v==='poor'?'poor':'unknown');
  document.getElementById('ag-refl-sub').textContent = `qa verdict · last seen ${a.reflection.last_seen}`;
  const ps4 = document.getElementById('ag-refl-status');
  ps4.textContent = a.reflection.status; ps4.className = `status-badge status-${a.reflection.status}`;
}

function updateCards(d) {
  const c = d.current, q = d.quality;
  document.getElementById('val-replicas').textContent   = c.replicas ?? '—';
  const lEl = document.getElementById('val-label');
  lEl.textContent = c.label ?? '—';
  lEl.className = 'value '+(c.label===3?'good':c.label===2?'':c.label===1?'poor':'unknown');
  document.getElementById('val-cpu').textContent        = fmt(c.cpu,3);
  document.getElementById('val-memory').textContent     = c.memory != null ? fmt(c.memory,0)+' MB' : '—';
  document.getElementById('val-delivered').textContent  = c.delivered_pct != null ? fmt(c.delivered_pct,1)+'%' : '—';
  document.getElementById('val-l1window').textContent   = c.label1_window_ratio != null ? fmt(c.label1_window_ratio,3) : '—';
  document.getElementById('val-l2window').textContent   = c.label2_window_ratio != null ? fmt(c.label2_window_ratio,3) : '—';
  document.getElementById('val-l3ratio').textContent    = c.label3_ratio        != null ? fmt(c.label3_ratio,3)        : '—';
  document.getElementById('val-scaleup-l1').textContent   = q.scaleup_label1_ratio   != null ? fmt(q.scaleup_label1_ratio,3)   : '—';
  document.getElementById('val-scaledown-l1').textContent = q.scaledown_label1_ratio != null ? fmt(q.scaledown_label1_ratio,3) : '—';
  document.getElementById('val-model-age').textContent  = q.model_age;
  document.getElementById('updated').textContent = 'Updated: '+d.updated_at+' · '+d.window_minutes+'min window';
}

function updatePlans(plans) {
  const tb = document.getElementById('plans-tbody');
  if (!plans.length) { tb.innerHTML = '<tr><td colspan="9" style="color:#64748b">No plans yet</td></tr>'; return; }
  tb.innerHTML = plans.map(p => {
    const dCls = p.delta && p.delta.startsWith('+') ? 'good' : p.delta && p.delta!=='0' && p.delta!=='—' ? 'poor' : '';
    const rsn  = (p.reason||'—').replace('predicted_slo_violation','slo_violation').replace('replicas_unchanged','unchanged').replace('model_predicted','model');
    const sid  = p.plan_id_short || '?';
    const fid  = p.plan_id || '';
    return `<tr>
      <td>${p.created_at}</td><td>${p.applied_at||'—'}</td>
      <td>${p.current_replicas??'—'} → ${p.desired_replicas??'—'}</td>
      <td class="${dCls}" style="font-weight:600">${p.delta??'—'}</td>
      <td style="color:#94a3b8">${rsn}</td>
      <td>${p.confidence !== undefined ? Number(p.confidence).toFixed(3) : '—'}</td>
      <td><span class="badge badge-${p.status}">${p.status}</span></td>
      <td style="color:#ef4444;font-size:10px;max-width:150px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${p.error||''}">${p.error||''}</td>
      <td style="color:#64748b;font-size:10px;font-family:monospace" title="${fid}">${sid}</td>
    </tr>`;
  }).join('');
}

function updateQA(feedbacks) {
  if (!feedbacks || !feedbacks.length) {
    document.getElementById('qa-tbody').innerHTML = '<tr><td colspan="7" style="color:#64748b">No QA data yet</td></tr>';
    return;
  }
  document.getElementById('qa-tbody').innerHTML = feedbacks.slice(0,10).map(fb => `
    <tr>
      <td>${fb.evaluated_at}</td>
      <td><span class="badge badge-${fb.verdict||'unknown'}">${fb.verdict||'—'}</span></td>
      <td>${fb.label1_ratio         != null ? Number(fb.label1_ratio).toFixed(3)         : '—'}</td>
      <td>${fb.scaleup_label1_ratio  != null ? Number(fb.scaleup_label1_ratio).toFixed(3)  : '—'}</td>
      <td>${fb.scaledown_label1_ratio != null ? Number(fb.scaledown_label1_ratio).toFixed(3) : '—'}</td>
      <td>${fb.rolling_mae != null ? Number(fb.rolling_mae).toFixed(4) : '—'}</td>
      <td>${fb.consecutive_failures ?? '—'}</td>
    </tr>`).join('');
}

function updateEvents(events) {
  const feed = document.getElementById('event-feed');
  if (!events || !events.length) {
    feed.innerHTML = '<div style="color:#64748b;font-size:12px">No events yet</div>';
    return;
  }
  feed.innerHTML = events.map(e => `
    <div class="event ${e.level}">
      <span class="event-ts">${e.ts_str}</span>
      <span class="event-agent ${e.agent}">${e.agent}</span>
      <span class="event-summary">${e.summary}</span>
    </div>`).join('');
}

let initialized = false;
async function refresh() {
  try {
    const res = await fetch('/api/data');
    const d = await res.json();
    if (d.error) { console.error(d.error); return; }
    if (!initialized) { initCharts(d); initialized = true; } else { updateCharts(d); }
    updateAgents(d.agents);
    updateCards(d);
    updatePlans(d.plans);
    updateQA(d.feedbacks || []);
    updateEvents(d.events || []);
  } catch(e) { console.error('Refresh error:', e); }
}

refresh();
setInterval(refresh, 15000);
</script>
</body>
</html>"""


@app.route("/")
def index():
    return render_template_string(DASHBOARD_HTML)


@app.route("/health")
def health():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)
