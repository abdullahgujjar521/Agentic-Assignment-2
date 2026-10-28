"""Memory layer: what is retrieved, when, and for whom.

Working state   -> working_state (bounded JSON checkpoint per review cycle)
Long-term       -> document_versions, checklist_items, reminders, questions, preferences,
                   decisions, episode_summaries, doc_state
Source knowledge-> sources + requirement_chunks (retrieval only; never proof of past decisions)

Every function takes user_id and refuses to touch another user's records.
"""
import json

from . import db
from .tools import date_calc


class AccessDenied(Exception):
    pass


def get_preferences(conn, user_id: str) -> dict:
    rows = conn.execute("SELECT key, value FROM preferences WHERE user_id=?", (user_id,)).fetchall()
    prefs = {r["key"]: r["value"] for r in rows}
    return {"buffer_days": int(prefs.get("buffer_days", 0)),
            "lead_overrides": {k.split(":", 1)[1]: int(v) for k, v in prefs.items() if k.startswith("lead_days:")}}


def retrieve_memory(conn, user_id, doc, today, settings, cycle_id=None, sim_date=None) -> dict:
    """Everything remembered about one document, for one user. Logged as a memory_read."""
    doc_id = doc["doc_id"]
    if not db.get_document(conn, user_id, doc_id):
        db.log_event(conn, user_id, "guard", "retrieve_memory", {"doc_id": doc_id},
                     {"denied": "document does not belong to this user"}, "denied", cycle_id, sim_date)
        raise AccessDenied(f"{doc_id} is not a document of user {user_id}")

    prefs = get_preferences(conn, user_id)
    history = [dict(r) for r in conn.execute(
        "SELECT version_id, expiry_date, origin, status, reason, recorded_at, superseded_at "
        "FROM document_versions WHERE user_id=? AND doc_id=? ORDER BY version_id", (user_id, doc_id))]
    items = [dict(r) for r in conn.execute(
        "SELECT step_no, text, status, evidence_ref FROM checklist_items WHERE user_id=? AND doc_id=? ORDER BY step_no",
        (user_id, doc_id))]
    todo = [i for i in items if i["status"] == "todo"]
    reminders = [dict(r) for r in conn.execute(
        "SELECT reminder_id, version_id, stage, status, created_at FROM reminders WHERE user_id=? AND doc_id=? "
        "ORDER BY reminder_id", (user_id, doc_id))]
    questions = [dict(r) for r in conn.execute(
        "SELECT question_id, kind, text, source_id FROM questions WHERE user_id=? AND doc_id=? AND status='open'",
        (user_id, doc_id))]
    state = conn.execute("SELECT lead_days, lead_source, evidence_ref, last_stage FROM doc_state "
                         "WHERE user_id=? AND doc_id=?", (user_id, doc_id)).fetchone()
    episode = conn.execute("SELECT summary FROM episode_summaries WHERE user_id=? AND doc_id=? "
                           "ORDER BY episode_id DESC LIMIT 1", (user_id, doc_id)).fetchone()
    src = db.get_current_source(conn, user_id, "requirements")

    lead = prefs["lead_overrides"].get(doc["doc_type"], state["lead_days"] if state else None)
    projected = date_calc(doc["expiry_date"], today, lead, prefs["buffer_days"],
                          settings.start_now_window_days)["stage"] if lead is not None else None
    stage_reminder = None
    if projected:
        row = conn.execute("SELECT status FROM reminders WHERE dedupe_key=?",
                           (f"{user_id}|{doc_id}|{doc['version_id']}|{projected}",)).fetchone()
        stage_reminder = row["status"] if row else None

    mem = {
        "expiry_history": history,
        "checklist": {"total": len(items), "done": sum(i["status"] == "done" for i in items),
                      "skipped": sum(i["status"] == "skipped" for i in items), "next_step": todo[0] if todo else None,
                      "items": items},
        "reminders": reminders,
        "open_questions": questions,
        "doc_state": dict(state) if state else None,
        "preferences": prefs,
        "last_episode": episode["summary"] if episode else None,
        "projected_stage": projected,
        "reminder_for_projected_stage": stage_reminder,
        "requirements_source_id": src["source_id"] if src else None,
    }
    db.log_event(conn, user_id, "memory_read", "retrieve_memory", {"doc_id": doc_id},
                 {"versions": len(history), "checklist": f"{mem['checklist']['done']}/{mem['checklist']['total']}",
                  "reminders": len(reminders), "open_questions": len(questions),
                  "projected_stage": projected, "stage_reminder": stage_reminder},
                 "ok", cycle_id, sim_date)
    return mem


def compact_for_prompt(mem: dict | None) -> dict | None:
    """Small, bounded view of memory that is allowed into the model prompt."""
    if mem is None:
        return None
    return {
        "projected_stage": mem["projected_stage"],
        "reminder_for_projected_stage": mem["reminder_for_projected_stage"],
        "checklist": {k: mem["checklist"][k] for k in ("total", "done", "skipped")},
        "next_step": mem["checklist"]["next_step"]["text"] if mem["checklist"]["next_step"] else None,
        "open_questions": [{"kind": q["kind"], "source_id": q["source_id"]} for q in mem["open_questions"]],
        "requirements_source_id": mem["requirements_source_id"],
        "expiry_versions": len(mem["expiry_history"]),
        "last_episode": mem["last_episode"],
    }


# ---- working state (checkpoint) ------------------------------------------------------------------
def save_state(conn, cycle_id: int, user_id: str, state: dict) -> None:
    conn.execute(
        "INSERT INTO working_state (cycle_id,user_id,state_json,updated_at) VALUES (?,?,?,?) "
        "ON CONFLICT(cycle_id) DO UPDATE SET state_json=excluded.state_json, updated_at=excluded.updated_at",
        (cycle_id, user_id, json.dumps(state), db.now_utc()))


def load_state(conn, cycle_id: int, user_id: str) -> dict | None:
    row = conn.execute("SELECT state_json FROM working_state WHERE cycle_id=? AND user_id=?",
                       (cycle_id, user_id)).fetchone()
    return json.loads(row["state_json"]) if row else None
