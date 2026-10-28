"""The decision loop.

For each document that is close to expiry the agent repeats (at most settings.max_steps times):
    observe -> LLM proposes ONE action -> code validates/gates it -> tool runs -> observe again
The LLM chooses WHICH tool to use or whether to ask the user (evidence-dependent). Dates,
permissions, validation and every database write are deterministic code.
"""
import json
from dataclasses import dataclass, field

from . import db, guard, memory
from .exporter import export_register
from .llm import SYSTEM_PROMPT, LLMError, offline_policy
from .tools import (FaultInjector, ToolError, date_calc, priority_key, record_reader, requirements_search,
                    write_plan)


class CycleInterrupted(Exception):
    def __init__(self, cycle_id, doc_id, cause):
        super().__init__(f"cycle {cycle_id} interrupted at {doc_id}: {cause}")
        self.cycle_id, self.doc_id, self.cause = cycle_id, doc_id, cause


@dataclass
class Session:
    conn: object
    user: object
    settings: object
    llm: object
    sim_date: str
    memory_enabled: bool = True
    fault: FaultInjector = field(default_factory=FaultInjector)
    cycle_id: int | None = None
    known: dict = field(default_factory=dict)      # what this run learned (used when memory is disabled)
    stats: dict = field(default_factory=lambda: {"llm_calls": 0, "llm_fallbacks": 0, "guard_overrides": 0,
                                                   "duplicates_blocked": 0, "tool_calls": 0})

    @property
    def uid(self):
        return self.user["user_id"]


# ------------------------------------------------------------------------------------------------
# decision step
# ------------------------------------------------------------------------------------------------
def _compact_observation(res: dict) -> dict:
    steps = [guard.clean_line(s["text"], 120) for h in res["matching"][:1] for s in h["steps"]][:8]
    return {"tool": "requirements_search", "query": res["query"], "n_matching": res["n_matching"],
            "conflict": res["conflict"], "conflicting_values": res["conflicting_values"],
            "lead_days": res["lead_days"], "lead_source": res["lead_source"],
            "steps": len(steps), "step_texts": steps,
            "evidence": [h["evidence_ref"] for h in res["matching"]],
            "quarantined_lines": sum(len(h["quarantined"]) for h in res["matching"])}


def _build_context(sess: Session, doc: dict, mem, observations: list) -> dict:
    label = guard.clean_line(doc["label"], 80)
    if guard.scan_text(label):
        db.log_event(sess.conn, sess.uid, "guard", "build_context", {"doc_id": doc["doc_id"]},
                     {"label_replaced": True}, "quarantined", sess.cycle_id, sess.sim_date)
        label = doc["doc_type"].replace("_", " ")
    days_left = date_calc(doc["expiry_date"], sess.sim_date)["days_left"]
    return {"today": sess.sim_date,
            "document": {"doc_type": doc["doc_type"], "label": label, "expiry_date": doc["expiry_date"],
                         "days_left": days_left},
            "memory": memory.compact_for_prompt(mem),
            "observations": observations,
            "allowed_actions": list(guard.ALLOWED_ACTIONS)}


def _log_guard(sess, doc, what, detail):
    sess.stats["guard_overrides"] += 1
    db.log_event(sess.conn, sess.uid, "guard", "decision_gate", {"doc_id": doc["doc_id"], "issue": what},
                 detail, "overridden", sess.cycle_id, sess.sim_date)


def _ask_llm(sess: Session, doc: dict, ctx: dict) -> tuple[dict, str]:
    """Call the model (up to 2 attempts), validate, and fall back to the rule policy if needed."""
    for attempt in (1, 2):
        payload = ctx if attempt == 1 else {**ctx, "previous_reply_was_invalid": True}
        try:
            sess.stats["llm_calls"] += 1
            raw, meta = sess.llm.complete_json(SYSTEM_PROMPT, json.dumps(payload, ensure_ascii=False))
        except LLMError as exc:
            db.log_event(sess.conn, sess.uid, "error", "llm", {"doc_id": doc["doc_id"], "attempt": attempt},
                         str(exc), "llm_error", sess.cycle_id, sess.sim_date)
            continue
        clean, problems = guard.validate_decision(raw)
        db.log_event(sess.conn, sess.uid, "llm_call", sess.llm.name,
                     {"doc_id": doc["doc_id"], "attempt": attempt, "model": meta.get("model"),
                      "memory_in_prompt": ctx["memory"] is not None},
                     {"raw_action": raw.get("action") if isinstance(raw, dict) else None,
                      "valid": clean is not None, "prompt_tokens": meta.get("prompt_tokens"),
                      "completion_tokens": meta.get("completion_tokens")},
                     "ok" if clean else "invalid", sess.cycle_id, sess.sim_date, latency_ms=meta.get("latency_ms"))
        if problems:
            _log_guard(sess, doc, "invalid_or_forbidden_model_output", problems)
        if clean:
            return clean, "llm"
    sess.stats["llm_fallbacks"] += 1
    db.log_event(sess.conn, sess.uid, "guard", "llm_fallback", {"doc_id": doc["doc_id"]},
                 "model output unusable twice; using deterministic rule policy", "fallback", sess.cycle_id, sess.sim_date)
    clean, _ = guard.validate_decision(offline_policy(ctx))
    return clean, "fallback_policy"


def _evidence_state(observations: list) -> str:
    """Deterministic reading of the latest evidence: what the data actually supports."""
    searches = [o for o in observations if o["tool"] == "requirements_search"]
    if not searches:
        return "no_evidence"
    last = searches[-1]
    if last["n_matching"] == 0:
        return "empty"
    if last["conflict"]:
        return "conflict"
    if last["lead_days"] is None:
        return "no_lead"
    if last["steps"] == 0:
        return "no_steps"
    return "ok"


def _gate(sess, doc, decision, observations, mem, ctx) -> dict:
    """Code decides whether the model's action is permitted given the evidence; else overrides it."""
    action, state = decision["action"], _evidence_state(observations)
    n_search = sum(o["tool"] == "requirements_search" for o in observations)
    ok = True
    if action == "write_plan":
        ok = state == "ok"
    elif action == "ask_user":
        ok = state in ("empty", "conflict", "no_lead", "no_steps") and (state != "empty" or n_search >= 2)
    elif action == "search_requirements":
        ok = n_search < 2 and (state in ("no_evidence", "empty"))
    elif action == "wait":
        ok = state == "no_evidence" and mem is not None and (
            mem["reminder_for_projected_stage"] is not None
            or any(q["source_id"] == mem["requirements_source_id"] for q in mem["open_questions"]))
    if ok:
        return decision
    override, _ = guard.validate_decision(offline_policy(ctx))
    _log_guard(sess, doc, f"action_not_allowed:{action}",
               {"evidence_state": state, "model_action": action, "forced_action": override["action"]})
    return override


# ------------------------------------------------------------------------------------------------
# per-document processing
# ------------------------------------------------------------------------------------------------
def _version_for(sess, doc) -> int:
    if doc.get("version_id"):
        return doc["version_id"]
    row = sess.conn.execute("SELECT version_id FROM document_versions WHERE doc_id=? AND origin='csv_import' "
                            "ORDER BY version_id LIMIT 1", (doc["doc_id"],)).fetchone()
    return row["version_id"]


def process_document(sess: Session, doc: dict) -> dict:
    conn, uid = sess.conn, sess.uid
    mem = memory.retrieve_memory(conn, uid, {**doc, "version_id": _version_for(sess, doc)}, sess.sim_date,
                                 sess.settings, sess.cycle_id, sess.sim_date) if sess.memory_enabled else None
    prefs = mem["preferences"] if mem else {"buffer_days": 0, "lead_overrides": {}}
    override = prefs["lead_overrides"].get(doc["doc_type"])
    observations: list = []
    result = {"doc_id": doc["doc_id"], "action": None, "decision_source": None, "reason": None,
              "trace": [], "plan": None, "question_id": None}

    for step in range(sess.settings.max_steps):
        ctx = _build_context(sess, doc, mem, observations)
        decision, source = _ask_llm(sess, doc, ctx)
        decision = _gate(sess, doc, decision, observations, mem, ctx)
        action = decision["action"]
        db.log_event(conn, uid, "decision", "agent", {"doc_id": doc["doc_id"], "step": step},
                     {"action": action, "reason": decision["reason"], "source": source,
                      "query": decision.get("query")}, "ok", sess.cycle_id, sess.sim_date)
        result["trace"].append(action)
        result.update(decision_source=source, reason=decision["reason"])

        if action == "search_requirements":
            sess.fault.maybe_fail("search")
            res = requirements_search(conn, uid, decision["query"], doc["doc_type"], override,
                                      sess.sim_date, sess.cycle_id)
            sess.stats["tool_calls"] += 1
            observations.append(_compact_observation(res))      # compact form goes to the prompt
            sess.known.setdefault(doc["doc_id"], {})["search"] = res   # full result stays in code
            continue

        if action == "write_plan":
            res = sess.known[doc["doc_id"]]["search"]
            evidence = res["matching"][0]
            calc = date_calc(doc["expiry_date"], sess.sim_date, res["lead_days"], prefs["buffer_days"],
                             sess.settings.start_now_window_days)
            plan = write_plan(conn, sess.user, doc, _version_for(sess, doc), sess.cycle_id, sess.sim_date, calc,
                              evidence, decision.get("message_intro", ""), res["lead_source"] or evidence["evidence_ref"],
                              sess.settings, sess.fault)
            sess.stats["tool_calls"] += 1
            if plan["reminder"]["reason"] in ("duplicate_blocked", "limit_blocked"):
                sess.stats["duplicates_blocked"] += 1
            sess.known[doc["doc_id"]].update(lead_days=res["lead_days"], lead_source=res["lead_source"],
                                             evidence_ref=evidence["evidence_ref"], steps=evidence["steps"])
            with db.tx(conn):    # evidence now supports the plan, so older open questions are obsolete
                conn.execute("UPDATE questions SET status='answered', answer='auto: resolved by new evidence', "
                             "answered_at=? WHERE doc_id=? AND user_id=? AND status='open'",
                             (db.now_utc(), doc["doc_id"], uid))
            result.update(action="write_plan", plan=plan, stage=calc["stage"])
            return result

        if action == "ask_user":
            state = _evidence_state(observations)
            kind = {"conflict": "conflicting_lead_days", "no_lead": "missing_lead_days"}.get(state, "missing_requirements")
            src = db.get_current_source(conn, uid, "requirements")
            cur = conn.execute(
                "INSERT OR IGNORE INTO questions (user_id,doc_id,cycle_id,source_id,kind,text,created_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (uid, doc["doc_id"], sess.cycle_id, src["source_id"] if src else None, kind,
                 decision["question"], db.now_utc()))
            qid = cur.lastrowid if cur.rowcount else conn.execute(
                "SELECT question_id FROM questions WHERE doc_id=? AND kind=? AND status='open'",
                (doc["doc_id"], kind)).fetchone()["question_id"]
            if not cur.rowcount:
                sess.stats["duplicates_blocked"] += 1
            db.log_event(conn, uid, "memory_write", "questions", {"doc_id": doc["doc_id"], "kind": kind},
                         {"question_id": qid, "created": bool(cur.rowcount)},
                         "ok" if cur.rowcount else "blocked", sess.cycle_id, sess.sim_date,
                         idem_key=f"{uid}|{doc['doc_id']}|question|{kind}")
            sess.known.setdefault(doc["doc_id"], {})["question"] = decision["question"]
            result.update(action="ask_user", question_id=qid, question_kind=kind)
            return result

        result.update(action="wait")                  # action == "wait"
        return result

    # step budget exhausted: record it instead of looping forever
    db.log_event(conn, uid, "guard", "max_steps", {"doc_id": doc["doc_id"]}, {"trace": result["trace"]},
                 "needs_review", sess.cycle_id, sess.sim_date)
    result.update(action="needs_review", reason="step budget exhausted")
    return result


# ------------------------------------------------------------------------------------------------
# the status view (used by `status` and at the end of `review`)
# ------------------------------------------------------------------------------------------------
STAGE_LABEL = {"expired": "EXPIRED", "overdue_start": "OVERDUE START", "start_now": "START NOW",
               "upcoming": "upcoming", "unknown_lead": "UNCERTAIN (lead time unknown)", "not_due": "not due"}


def compute_view(conn, user_id, today, settings, memory_enabled=True, docs=None, known=None) -> list[dict]:
    known = known or {}
    docs = docs if docs is not None else record_reader(conn, user_id, "sqlite", today)
    prefs = memory.get_preferences(conn, user_id) if memory_enabled else {"buffer_days": 0, "lead_overrides": {}}
    rows = []
    for d in docs:
        did = d["doc_id"]
        state = conn.execute("SELECT * FROM doc_state WHERE user_id=? AND doc_id=?", (user_id, did)).fetchone() \
            if memory_enabled else None
        k = known.get(did, {})
        lead = prefs["lead_overrides"].get(d["doc_type"]) or (state["lead_days"] if state else None) or k.get("lead_days")
        lead_source = "user_answer" if d["doc_type"] in prefs["lead_overrides"] else (
            state["lead_source"] if state else k.get("lead_source"))
        calc = date_calc(d["expiry_date"], today, lead, prefs["buffer_days"], settings.start_now_window_days)
        due = calc["days_left"] <= settings.candidate_horizon_days
        stage = calc["stage"] if due else "not_due"
        evidence = [e for e in ((state["evidence_ref"] if state else k.get("evidence_ref")),) if e]
        items, questions, approvals = [], [], []
        if memory_enabled:
            items = [dict(r) for r in conn.execute(
                "SELECT step_no,text,status,evidence_ref FROM checklist_items WHERE user_id=? AND doc_id=? "
                "ORDER BY step_no", (user_id, did))]
            questions = [dict(r) for r in conn.execute(
                "SELECT question_id,kind,text FROM questions WHERE user_id=? AND doc_id=? AND status='open'",
                (user_id, did))]
            approvals = [dict(r) for r in conn.execute(
                "SELECT reminder_id,stage,subject FROM reminders WHERE user_id=? AND doc_id=? AND status='draft_unsent'",
                (user_id, did))]
            n_versions = conn.execute("SELECT COUNT(*) c FROM document_versions WHERE doc_id=?", (did,)).fetchone()["c"]
        else:
            items = [{"step_no": s["step_no"], "text": s["text"], "status": "todo",
                      "evidence_ref": f"{(k.get('evidence_ref') or ':').split(':')[0]}:L{s['line']}"}
                     for s in k.get("steps", [])]
            if k.get("question"):
                questions = [{"question_id": None, "kind": "open", "text": k["question"]}]
            n_versions = 1
        todo = [i for i in items if i["status"] == "todo"]
        prefix = {"expired": "[EXPIRED] ", "overdue_start": "[OVERDUE START] ", "start_now": "[START NOW] "}.get(stage, "")
        if not due:
            action, ev = f"No action needed yet (more than {settings.candidate_horizon_days} days to expiry).", []
        elif questions:
            action, ev = f"Answer question: {questions[0]['text']}", evidence
        elif todo:
            action, ev = f"{prefix}Checklist step {todo[0]['step_no']}: {todo[0]['text']}", evidence + [todo[0]["evidence_ref"]]
        elif items:
            action, ev = ("All checklist steps are done. After you receive the renewed document, record its new "
                          "expiry date with the 'correct' command."), evidence
        else:
            action, ev = f"{prefix}No checklist yet. Run a review.", []
        rows.append({
            "doc_id": did, "doc_type": d["doc_type"], "label": d["label"], "expiry_date": d["expiry_date"],
            "expiry_versions": n_versions, "days_left": calc["days_left"], "lead_days": lead, "lead_source": lead_source,
            "latest_start": calc["latest_start"], "stage": stage, "stage_text": STAGE_LABEL.get(stage, stage),
            "due": due, "uncertain": due and lead is None,
            "checklist_done": sum(i["status"] == "done" for i in items), "checklist_total": len(items),
            "next_action": action, "evidence": ev, "approval_required": approvals, "open_questions": questions,
            "_key": priority_key(calc) if due else (9, calc["days_left"]),
        })
    rows.sort(key=lambda r: r["_key"])
    for rank, r in enumerate(rows, start=1):
        r["priority"] = rank if r["due"] else None
        del r["_key"]
    return rows


# ------------------------------------------------------------------------------------------------
# the review cycle
# ------------------------------------------------------------------------------------------------
def run_review(conn, user_id, sim_date, settings, llm, memory_enabled=True, fault=None, resume=False,
               export=True) -> dict:
    user = db.get_user(conn, user_id)
    if not user:
        raise ValueError(f"unknown user {user_id!r}; run onboard first")
    sess = Session(conn, user, settings, llm, sim_date, memory_enabled, fault or FaultInjector())

    if resume:
        row = conn.execute("SELECT cycle_id FROM review_cycles WHERE user_id=? AND status='interrupted' "
                           "ORDER BY cycle_id DESC LIMIT 1", (user_id,)).fetchone()
        if not row:
            raise ValueError("no interrupted review to resume")
        sess.cycle_id = row["cycle_id"]
        state = memory.load_state(conn, sess.cycle_id, user_id)
        sess.sim_date = sim_date = conn.execute("SELECT sim_date FROM review_cycles WHERE cycle_id=?",
                                                (sess.cycle_id,)).fetchone()["sim_date"]
        conn.execute("UPDATE review_cycles SET status='running' WHERE cycle_id=?", (sess.cycle_id,))
        db.log_event(conn, user_id, "cycle", "review", {"documents_already_done": len(state["done"])},
                     "resumed from checkpoint", "ok", sess.cycle_id, sim_date)
    else:
        cur = conn.execute("INSERT INTO review_cycles (user_id,sim_date,started_at,status,provider,memory_enabled) "
                           "VALUES (?,?,?, 'running', ?, ?)",
                           (user_id, sim_date, db.now_utc(), llm.name, int(memory_enabled)))
        sess.cycle_id = cur.lastrowid
        state = {"queue": [], "done": {}}
        db.log_event(conn, user_id, "cycle", "review", {"sim_date": sim_date, "provider": llm.name,
                                                          "memory_enabled": memory_enabled},
                     "started", "ok", sess.cycle_id, sim_date)

    docs = record_reader(conn, user_id, "sqlite" if memory_enabled else "csv", sim_date, sess.cycle_id)
    by_id = {d["doc_id"]: d for d in docs}
    if not resume:
        prefs = memory.get_preferences(conn, user_id) if memory_enabled else {"buffer_days": 0}
        due = sorted((date_calc(d["expiry_date"], sim_date, None, prefs["buffer_days"])["days_left"], d["doc_id"])
                     for d in docs
                     if date_calc(d["expiry_date"], sim_date)["days_left"] <= settings.candidate_horizon_days)
        state["queue"] = [doc_id for _, doc_id in due]
        memory.save_state(conn, sess.cycle_id, user_id, state)
        db.log_event(conn, user_id, "decision", "agent", {"candidates": state["queue"]},
                     {"not_due": [d["doc_id"] for d in docs if d["doc_id"] not in state["queue"]]},
                     "ok", sess.cycle_id, sim_date)

    results = []
    for doc_id in state["queue"]:
        if doc_id in state["done"]:
            continue
        try:
            res = process_document(sess, by_id[doc_id])
        except ToolError as exc:
            conn.execute("UPDATE review_cycles SET status='interrupted' WHERE cycle_id=?", (sess.cycle_id,))
            memory.save_state(conn, sess.cycle_id, user_id, state)
            db.log_event(conn, user_id, "error", "tool", {"doc_id": doc_id}, str(exc), "tool_failure",
                         sess.cycle_id, sim_date)
            raise CycleInterrupted(sess.cycle_id, doc_id, exc) from exc
        state["done"][doc_id] = res["action"]
        memory.save_state(conn, sess.cycle_id, user_id, state)      # checkpoint after every document
        results.append(res)
        if memory_enabled and res["action"] in ("write_plan", "ask_user"):
            summary = (f"{res['action']} at stage {res.get('stage', 'n/a')}; "
                       + (f"checklist {res['plan']['checklist_total']} steps, reminder "
                          f"{res['plan']['reminder']['reason']}" if res.get("plan") else res.get("reason", "")))
            conn.execute("INSERT INTO episode_summaries (user_id,cycle_id,doc_id,summary,created_at) VALUES (?,?,?,?,?)",
                         (user_id, sess.cycle_id, doc_id, summary[:300], db.now_utc()))

    conn.execute("UPDATE review_cycles SET status='completed', finished_at=? WHERE cycle_id=?",
                 (db.now_utc(), sess.cycle_id))
    db.log_event(conn, user_id, "cycle", "review", {"sim_date": sim_date}, {"processed": len(state["done"]),
                 **sess.stats}, "completed", sess.cycle_id, sim_date)
    view = compute_view(conn, user_id, sim_date, settings, memory_enabled, docs, sess.known)
    export_path = export_register(conn, user_id, sim_date, settings) if (export and memory_enabled) else None
    return {"cycle_id": sess.cycle_id, "sim_date": sim_date, "processed": results, "view": view,
            "stats": sess.stats, "export_path": export_path, "memory_enabled": memory_enabled}
