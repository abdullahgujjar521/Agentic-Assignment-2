"""Actions that only a HUMAN can take. The agent never calls these.

Everything is scoped by user_id: asking for another user's record behaves exactly like asking for
a record that does not exist.
"""
from datetime import date

from . import db


class NotFound(Exception):
    pass


class ActionError(Exception):
    pass


def tick(conn, user_id, doc_id, step_no, status="done") -> dict:
    if status not in ("done", "todo", "skipped"):
        raise ActionError("status must be done, todo or skipped")
    if not db.get_document(conn, user_id, doc_id):
        raise NotFound(f"no document {doc_id!r} for user {user_id}")
    now = db.now_utc()
    with db.tx(conn):
        cur = conn.execute("UPDATE checklist_items SET status=?, updated_at=? WHERE user_id=? AND doc_id=? AND step_no=?",
                           (status, now, user_id, doc_id, step_no))
        if not cur.rowcount:
            raise NotFound(f"no checklist step {step_no} for {doc_id}")
        conn.execute("INSERT INTO decisions (user_id,target_type,target_id,decision,note,decided_at) VALUES (?,?,?,?,?,?)",
                     (user_id, "checklist", f"{doc_id}#{step_no}", status, None, now))
    db.log_event(conn, user_id, "memory_write", "tick", {"doc_id": doc_id, "step": step_no}, {"status": status}, "ok")
    return {"doc_id": doc_id, "step_no": step_no, "status": status}


def decide_reminder(conn, user_id, reminder_id, decision, edited_body=None, note=None) -> dict:
    row = conn.execute("SELECT * FROM reminders WHERE user_id=? AND reminder_id=?", (user_id, reminder_id)).fetchone()
    if not row:
        raise NotFound(f"no reminder #{reminder_id} for user {user_id}")
    if row["status"] != "draft_unsent":
        raise ActionError(f"reminder #{reminder_id} is already '{row['status']}'")
    if decision == "edit" and not edited_body:
        raise ActionError("edit needs replacement text")
    status = {"approve": "approved", "edit": "edited", "reject": "rejected"}[decision]
    now = db.now_utc()
    with db.tx(conn):
        conn.execute("UPDATE reminders SET status=?, edited_body=?, decided_at=?, decision_note=? WHERE reminder_id=?",
                     (status, edited_body, now, note, reminder_id))
        conn.execute("INSERT INTO decisions (user_id,target_type,target_id,decision,note,decided_at) VALUES (?,?,?,?,?,?)",
                     (user_id, "reminder", str(reminder_id), status, note, now))
    db.log_event(conn, user_id, "memory_write", "decide", {"reminder_id": reminder_id}, {"status": status}, "ok")
    return {"reminder_id": reminder_id, "status": status, "sent": False}


def correct_expiry(conn, user_id, doc_id, new_expiry, reason) -> dict:
    try:
        date.fromisoformat(new_expiry)
    except ValueError as exc:
        raise ActionError(f"invalid date {new_expiry!r}: use YYYY-MM-DD") from exc
    if not reason or not reason.strip():
        raise ActionError("a reason is required so the history stays meaningful")
    if not db.get_document(conn, user_id, doc_id):
        raise NotFound(f"no document {doc_id!r} for user {user_id}")
    cur = conn.execute("SELECT * FROM document_versions WHERE doc_id=? AND user_id=? AND status='current'",
                       (doc_id, user_id)).fetchone()
    if cur["expiry_date"] == new_expiry:
        return {"changed": False, "doc_id": doc_id, "expiry_date": new_expiry}
    now = db.now_utc()
    with db.tx(conn):
        conn.execute("UPDATE document_versions SET status='superseded', superseded_at=? WHERE version_id=?",
                     (now, cur["version_id"]))
        new = conn.execute(
            "INSERT INTO document_versions (doc_id,user_id,expiry_date,origin,source_id,status,reason,recorded_at) "
            "VALUES (?,?,?, 'user_correction', NULL, 'current', ?, ?)",
            (doc_id, user_id, new_expiry, reason.strip(), now))
        stale = conn.execute("UPDATE reminders SET status='superseded', decided_at=? "
                             "WHERE doc_id=? AND version_id=? AND status='draft_unsent'",
                             (now, doc_id, cur["version_id"])).rowcount
        conn.execute("INSERT INTO decisions (user_id,target_type,target_id,decision,note,decided_at) VALUES (?,?,?,?,?,?)",
                     (user_id, "document", doc_id, f"expiry {cur['expiry_date']} -> {new_expiry}", reason.strip(), now))
    db.log_event(conn, user_id, "memory_write", "correct_expiry", {"doc_id": doc_id},
                 {"old": cur["expiry_date"], "new": new_expiry, "stale_drafts_superseded": stale}, "ok")
    return {"changed": True, "doc_id": doc_id, "old": cur["expiry_date"], "new": new_expiry,
            "version_id": new.lastrowid, "stale_drafts_superseded": stale}


def answer_question(conn, user_id, question_id, lead_days=None) -> dict:
    q = conn.execute("SELECT q.*, d.doc_type FROM questions q JOIN documents d ON d.doc_id=q.doc_id "
                     "WHERE q.user_id=? AND q.question_id=?", (user_id, question_id)).fetchone()
    if not q:
        raise NotFound(f"no question #{question_id} for user {user_id}")
    if q["status"] != "open":
        raise ActionError(f"question #{question_id} is already answered")
    if q["kind"] not in ("missing_lead_days", "conflicting_lead_days"):
        raise ActionError("this question is answered by adding evidence: edit the requirements file and re-run "
                          "'onboard'; the question closes by itself once the evidence supports a plan")
    if lead_days is None or lead_days < 0 or lead_days > 365:
        raise ActionError("provide --lead-days (0-365)")
    now = db.now_utc()
    with db.tx(conn):
        db.set_pref(conn, user_id, f"lead_days:{q['doc_type']}", lead_days, "user_answer")
        conn.execute("UPDATE questions SET status='answered', answer=?, answered_at=? WHERE question_id=?",
                     (f"lead_days={lead_days}", now, question_id))
        conn.execute("INSERT INTO decisions (user_id,target_type,target_id,decision,note,decided_at) VALUES (?,?,?,?,?,?)",
                     (user_id, "question", str(question_id), f"lead_days={lead_days}", q["doc_type"], now))
    db.log_event(conn, user_id, "memory_write", "answer", {"question_id": question_id},
                 {"preference": f"lead_days:{q['doc_type']}={lead_days}"}, "ok")
    return {"question_id": question_id, "doc_type": q["doc_type"], "lead_days": lead_days}
