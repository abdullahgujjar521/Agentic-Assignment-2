"""The four tools available to the agent.

  1. record_reader        - reads the documents list (SQLite current versions, or the baseline CSV)
  2. requirements_search  - retrieves evidence from the locally indexed requirements file
  3. date_calc            - deterministic date arithmetic (days left, latest start date, stage)
  4. write_plan           - persistent write: checklist rows + UNSENT reminder draft (SQLite)

Excel output is produced by exporter.py as a one-way review view of the SQLite store.
Calculations, validation and stable record updates are deterministic code; the LLM never does them.
"""
import csv
import json
import re
import time
from datetime import date, timedelta
from pathlib import Path

from . import db
from .guard import clean_line

REMINDER_STAGES = ("upcoming", "start_now", "overdue_start", "expired")


class ToolError(Exception):
    """A tool failed. The review is checkpointed and can be resumed safely."""


class FaultInjector:
    """Deterministic failure injection for the 'tool failure and retry' test. Fires once per process."""

    def __init__(self, point: str | None = None):
        self.point = point
        self.fired: set[str] = set()

    def maybe_fail(self, where: str) -> None:
        if self.point == where and where not in self.fired:
            self.fired.add(where)
            raise ToolError(f"simulated failure at '{where}'")


# ------------------------------------------------------------------------------------------------
# Tool 3: date calculator
# ------------------------------------------------------------------------------------------------
def date_calc(expiry_iso: str, today_iso: str, lead_days: int | None = None,
              buffer_days: int = 0, start_now_window: int = 21) -> dict:
    """Pure function. Raises ValueError on a malformed date."""
    expiry = date.fromisoformat(expiry_iso)
    today = date.fromisoformat(today_iso)
    days_left = (expiry - today).days
    out = {"expiry": expiry_iso, "today": today_iso, "days_left": days_left,
           "lead_days": lead_days, "buffer_days": buffer_days,
           "latest_start": None, "days_to_latest_start": None}
    if lead_days is not None:
        latest = expiry - timedelta(days=lead_days + buffer_days)
        out["latest_start"] = latest.isoformat()
        out["days_to_latest_start"] = (latest - today).days
    if days_left < 0:
        out["stage"] = "expired"
    elif lead_days is None:
        out["stage"] = "unknown_lead"
    elif out["days_to_latest_start"] < 0:
        out["stage"] = "overdue_start"
    elif out["days_to_latest_start"] <= start_now_window:
        out["stage"] = "start_now"
    else:
        out["stage"] = "upcoming"
    return out


STAGE_RANK = {"expired": 0, "overdue_start": 1, "start_now": 2, "unknown_lead": 2, "upcoming": 3}


def priority_key(calc: dict) -> tuple:
    """Lower sorts first. Uses days to the latest start date when known, otherwise days left."""
    primary = calc["days_to_latest_start"] if calc["days_to_latest_start"] is not None else calc["days_left"]
    return (STAGE_RANK.get(calc["stage"], 4), primary)


# ------------------------------------------------------------------------------------------------
# Tool 1: record reader
# ------------------------------------------------------------------------------------------------
def parse_documents_csv(path: str) -> tuple[list[dict], list[str]]:
    """Returns (rows, errors). Expected columns: doc_type,label,expiry_date."""
    rows, errors = [], []
    with open(path, newline="", encoding="utf-8") as fh:
        for n, rec in enumerate(csv.DictReader(fh), start=2):
            try:
                doc_type = re.sub(r"[^a-z0-9]+", "_", rec["doc_type"].strip().lower()).strip("_")
                if not doc_type:
                    raise ValueError("empty doc_type")
                date.fromisoformat(rec["expiry_date"].strip())
                rows.append({"doc_type": doc_type, "label": clean_line(rec["label"], 80),
                             "expiry_date": rec["expiry_date"].strip()})
            except (KeyError, ValueError, AttributeError) as exc:
                errors.append(f"line {n}: {exc}")
    return rows, errors


def record_reader(conn, user_id: str, source: str, sim_date: str | None = None, cycle_id=None) -> list[dict]:
    """source='sqlite': current versions from the store.  source='csv': the baseline CSV only
    (what an agent without memory would see)."""
    t0 = time.perf_counter()
    if source == "sqlite":
        rows = [dict(r) for r in conn.execute(
            "SELECT d.doc_id, d.doc_type, d.label, v.expiry_date, v.version_id, v.origin "
            "FROM documents d JOIN document_versions v ON v.doc_id=d.doc_id AND v.status='current' "
            "WHERE d.user_id=? ORDER BY d.doc_id", (user_id,))]
    elif source == "csv":
        src = db.get_current_source(conn, user_id, "documents_csv")
        if not src or not Path(src["path"]).exists():
            raise ToolError("baseline CSV is not available")
        parsed, _ = parse_documents_csv(src["path"])
        rows = [{"doc_id": f"{user_id}-{r['doc_type']}", "doc_type": r["doc_type"], "label": r["label"],
                 "expiry_date": r["expiry_date"], "version_id": None, "origin": "csv_baseline"} for r in parsed]
    else:
        raise ToolError(f"unknown source {source}")
    db.log_event(conn, user_id, "tool_call", "record_reader", {"source": source},
                 {"documents": len(rows)}, "ok", cycle_id, sim_date,
                 latency_ms=(time.perf_counter() - t0) * 1000)
    return rows


# ------------------------------------------------------------------------------------------------
# Tool 2: requirements search over the locally indexed file
# ------------------------------------------------------------------------------------------------
_GENERIC = {"renewal", "renew", "requirements", "requirement", "documents", "document", "steps", "step",
            "lead", "time", "checklist", "the", "an", "of", "for", "and", "to", "how", "what", "need",
            "needed", "process", "rules", "info", "information", "about", "my", "is", "are", "in", "on"}


def _tokens(text: str) -> set[str]:
    words = re.findall(r"[a-z0-9]+", (text or "").lower().replace("licence", "license"))
    return {w for w in words if w not in _GENERIC and len(w) > 1}


def requirements_search(conn, user_id: str, query: str, doc_type: str,
                        lead_override: int | None = None, sim_date=None, cycle_id=None) -> dict:
    t0 = time.perf_counter()
    src = db.get_current_source(conn, user_id, "requirements")
    if not src:
        raise ToolError("no requirements file has been loaded for this user")
    fname = Path(src["path"]).name
    q = _tokens(query)
    hits = []
    for ch in conn.execute("SELECT * FROM requirement_chunks WHERE user_id=? AND source_id=?",
                           (user_id, src["source_id"])):
        steps = json.loads(ch["steps_json"])
        chunk_tokens = _tokens(f"{ch['heading']} {ch['doc_type']} {ch['body']} " + " ".join(s["text"] for s in steps))
        score = len(q & chunk_tokens)
        if _tokens(ch["doc_type"]) and _tokens(ch["doc_type"]) <= q:
            score += 3
        if score >= 2:
            hits.append({"chunk_id": ch["chunk_id"], "doc_type": ch["doc_type"], "heading": ch["heading"],
                         "score": score, "lead_days": ch["lead_days"], "steps": steps,
                         "quarantined": json.loads(ch["quarantined_json"]),
                         "evidence_ref": f"{fname}:L{ch['line_start']}-L{ch['line_end']}",
                         "lead_ref": None})
    hits.sort(key=lambda h: -h["score"])
    matching = [h for h in hits if h["doc_type"] == doc_type]
    leads = {h["lead_days"] for h in matching if h["lead_days"] is not None}
    lead_days, lead_source = (next(iter(leads)), matching[0]["evidence_ref"]) if len(leads) == 1 else (None, None)
    conflict = len(leads) > 1
    if lead_override is not None:                       # a lead time the user supplied beats the file
        lead_days, lead_source, conflict = lead_override, "user_answer", False
    result = {"query": query, "n_hits": len(hits), "n_matching": len(matching), "conflict": conflict,
              "lead_days": lead_days, "lead_source": lead_source, "matching": matching,
              "conflicting_values": sorted(leads) if conflict else []}
    db.log_event(conn, user_id, "tool_call", "requirements_search",
                 {"query": query, "doc_type": doc_type},
                 {"hits": len(hits), "matching": len(matching), "lead_days": lead_days, "conflict": conflict,
                  "evidence": [h["evidence_ref"] for h in matching]},
                 "ok", cycle_id, sim_date, latency_ms=(time.perf_counter() - t0) * 1000)
    return result


# ------------------------------------------------------------------------------------------------
# Tool 4: checklist and reminder writer (persistent write, idempotent)
# ------------------------------------------------------------------------------------------------
STAGE_TEXT = {
    "upcoming": "there is time, but plan the steps now",
    "start_now": "you should start within the next few weeks",
    "overdue_start": "the latest start date has passed, so start immediately",
    "expired": "this document has already expired, so renew it as soon as possible",
}


def build_reminder(user, doc, calc, evidence_ref, intro, n_steps) -> tuple[str, str]:
    first = (user["display_name"] or "there").split()[0]
    subject = f"Renewal reminder: {doc['label']} expires {calc['expiry']} ({calc['days_left']} days left)"
    if calc["days_left"] < 0:
        subject = f"Renewal reminder: {doc['label']} EXPIRED on {calc['expiry']}"
    lines = [
        "*** UNSENT DRAFT - this system never sends messages. Review, edit and send it yourself. ***",
        "",
        f"Hi {first},",
    ]
    if intro:
        lines.append(intro)
    lines += [
        f"Your {doc['label']} expires on {calc['expiry']} ({calc['days_left']} days from {calc['today']}).",
        f"The requirements note a {calc['lead_days']}-day lead time ({evidence_ref}), so the latest start "
        f"date is {calc['latest_start']}: {STAGE_TEXT[calc['stage']]}.",
        f"The renewal checklist has {n_steps} steps; see the register for what is still open.",
    ]
    return subject, "\n".join(lines)


def write_plan(conn, user, doc, version_id, cycle_id, sim_date, calc, evidence, intro,
               lead_source, settings, fault: FaultInjector) -> dict:
    """Writes (1) checklist rows, (2) doc_state, (3) at most one reminder draft. Safe to retry:
    checklist rows are UNIQUE(doc_id, step_no) and reminders are UNIQUE(dedupe_key)."""
    t0 = time.perf_counter()
    uid, did = user["user_id"], doc["doc_id"]
    now = db.now_utc()
    steps = evidence["steps"]
    created = 0
    with db.tx(conn):
        for s in steps:
            cur = conn.execute(
                "INSERT OR IGNORE INTO checklist_items (user_id,doc_id,step_no,text,status,evidence_ref,"
                "created_cycle_id,created_at,updated_at) VALUES (?,?,?,?, 'todo', ?,?,?,?)",
                (uid, did, s["step_no"], clean_line(s["text"]), f"{evidence['evidence_ref'].split(':')[0]}:L{s['line']}",
                 cycle_id, now, now))
            created += cur.rowcount
        conn.execute(
            "INSERT INTO doc_state (doc_id,user_id,lead_days,lead_source,evidence_ref,last_stage,last_cycle_id,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(doc_id) DO UPDATE SET lead_days=excluded.lead_days, "
            "lead_source=excluded.lead_source, evidence_ref=excluded.evidence_ref, last_stage=excluded.last_stage, "
            "last_cycle_id=excluded.last_cycle_id, updated_at=excluded.updated_at",
            (did, uid, calc["lead_days"], lead_source, evidence["evidence_ref"], calc["stage"], cycle_id, now))
    db.log_event(conn, uid, "memory_write", "writer", {"table": "checklist_items", "doc_id": did},
                 {"rows_created": created, "rows_total": len(steps)}, "ok", cycle_id, sim_date,
                 idem_key=f"{uid}|{did}|checklist")
    fault.maybe_fail("writer_after_checklist")          # simulated crash between the two writes

    stage = calc["stage"]
    if stage not in REMINDER_STAGES:
        raise ToolError(f"no reminder stage for {stage}")
    key = f"{uid}|{did}|{version_id}|{stage}"
    existing = conn.execute("SELECT reminder_id, status FROM reminders WHERE dedupe_key=?", (key,)).fetchone()
    n_for_version = conn.execute("SELECT COUNT(*) c FROM reminders WHERE doc_id=? AND version_id=?",
                                 (did, version_id)).fetchone()["c"]
    reminder = {"created": False, "reason": None, "reminder_id": None}
    if existing:
        reminder.update(reason="duplicate_blocked", reminder_id=existing["reminder_id"])
    elif n_for_version >= settings.reminder_limit:
        reminder.update(reason="limit_blocked")
    else:
        subject, body = build_reminder(user, doc, calc, evidence["evidence_ref"], intro, len(steps))
        with db.tx(conn):
            conn.execute("UPDATE reminders SET status='superseded', decided_at=? "
                         "WHERE doc_id=? AND status='draft_unsent'", (now, did))
            cur = conn.execute(
                "INSERT INTO reminders (user_id,doc_id,version_id,cycle_id,stage,dedupe_key,subject,body,status,created_at) "
                "VALUES (?,?,?,?,?,?,?,?, 'draft_unsent', ?)",
                (uid, did, version_id, cycle_id, stage, key, subject, body, now))
        reminder.update(created=True, reason="created", reminder_id=cur.lastrowid)
    db.log_event(conn, uid, "memory_write", "writer", {"table": "reminders", "doc_id": did, "stage": stage},
                 reminder, "ok" if reminder["created"] else "blocked", cycle_id, sim_date, idem_key=key)
    db.log_event(conn, uid, "tool_call", "writer", {"doc_id": did, "stage": stage},
                 {"checklist_created": created, "reminder": reminder}, "ok", cycle_id, sim_date,
                 latency_ms=(time.perf_counter() - t0) * 1000)
    return {"checklist_created": created, "checklist_total": len(steps), "reminder": reminder}
