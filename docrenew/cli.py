"""DocRenew command line interface.   python -m docrenew.cli --help"""
import argparse
import csv
import json
import sys
from datetime import date

from . import actions, db, memory
from .agent import CycleInterrupted, compute_view, run_review
from .config import get_settings
from .llm import LLMError, make_llm
from .onboard import onboard
from .tools import FaultInjector


def _conn(args, settings):
    conn = db.connect(args.db or settings.db_path)
    db.init_schema(conn)
    return conn


def _today(args) -> str:
    return args.today or date.today().isoformat()


def _print_view(view, today, user):
    print(f"\nUser {user['user_id']} ({user['display_name']}) - review date {today}")
    print(f"{'#':<3}{'DOCUMENT':<22}{'EXPIRY':<12}{'DAYS':>5} {'LATEST START':<13}{'STAGE':<30}{'CHECKLIST':<10}")
    for r in view:
        print(f"{(r['priority'] or '-'):<3}{r['label'][:21]:<22}{r['expiry_date']:<12}{r['days_left']:>5} "
              f"{(r['latest_start'] or 'unknown'):<13}{r['stage_text']:<30}"
              f"{str(r['checklist_done']) + '/' + str(r['checklist_total']):<10}")
    print()
    for r in view:
        if not r["due"]:
            continue
        print(f"[{r['priority']}] {r['label']}  (versions of expiry date stored: {r['expiry_versions']})")
        print(f"    NEXT ACTION : {r['next_action']}")
        print(f"    EVIDENCE    : {'; '.join(r['evidence']) or 'none yet'}")
        if r["lead_days"] is not None:
            print(f"    LEAD TIME   : {r['lead_days']} days (source: {r['lead_source']})")
        if r["uncertain"]:
            print("    UNCERTAIN   : lead time unknown, so no latest start date is claimed")
        for a in r["approval_required"]:
            print(f"    APPROVAL    : reminder #{a['reminder_id']} - UNSENT DRAFT ({a['stage']}) - "
                  f"decide --reminder {a['reminder_id']} --approve | --edit \"...\" | --reject")
        for q in r["open_questions"]:
            print(f"    QUESTION    : #{q['question_id']} {q['text']}")
    print()


def cmd_onboard(a, s, conn):
    rep = onboard(conn, a.user, a.name, a.documents, a.requirements, a.today)
    print(json.dumps(rep, indent=2))


def cmd_review(a, s, conn):
    provider = a.provider or s.provider
    try:
        llm = make_llm(provider, s)
    except LLMError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    s.output_dir = a.output_dir or s.output_dir
    try:
        out = run_review(conn, a.user, _today(a), s, llm, memory_enabled=not a.no_memory,
                         fault=FaultInjector(a.simulate_failure), resume=a.resume, export=not a.no_export)
    except CycleInterrupted as exc:
        print(f"\nREVIEW INTERRUPTED (cycle {exc.cycle_id}) at {exc.doc_id}: {exc.cause}")
        print("State was saved. Re-run with --resume; completed writes will not be duplicated.")
        return 3
    user = db.get_user(conn, a.user)
    print(f"Review cycle {out['cycle_id']} completed (provider={llm.name}, memory={'on' if out['memory_enabled'] else 'OFF'})")
    for p in out["processed"]:
        extra = ""
        if p.get("plan"):
            rem = p["plan"]["reminder"]
            extra = (f" | checklist rows created: {p['plan']['checklist_created']}/{p['plan']['checklist_total']}"
                     f" | reminder: {rem['reason']}" + (f" #{rem['reminder_id']}" if rem["reminder_id"] else ""))
        print(f"  - {p['doc_id']}: {' -> '.join(p['trace'])} => {p['action']}{extra}")
        print(f"      why: {p['reason']}")
    _print_view(out["view"], out["sim_date"], user)
    print(f"Stats: {out['stats']}")
    if out["export_path"]:
        print(f"Excel review view: {out['export_path']}")


def cmd_status(a, s, conn):
    user = db.get_user(conn, a.user)
    if not user:
        print(f"error: unknown user {a.user}", file=sys.stderr)
        return 2
    view = compute_view(conn, a.user, _today(a), s, True)
    if a.json:
        print(json.dumps(view, indent=2))
    else:
        _print_view(view, _today(a), user)


def cmd_tick(a, s, conn):
    print(json.dumps(actions.tick(conn, a.user, a.doc, a.step, a.status)))


def cmd_reminders(a, s, conn):
    rows = conn.execute("SELECT * FROM reminders WHERE user_id=? ORDER BY reminder_id", (a.user,)).fetchall()
    for r in rows:
        if a.doc and r["doc_id"] != a.doc:
            continue
        print(f"#{r['reminder_id']} [{r['status'].upper()}] {r['doc_id']} stage={r['stage']} (UNSENT - never sent by this system)")
        print(f"   {r['subject']}")
        if a.full:
            print("   " + (r["edited_body"] or r["body"]).replace("\n", "\n   "))


def cmd_decide(a, s, conn):
    d = "approve" if a.approve else "reject" if a.reject else "edit"
    print(json.dumps(actions.decide_reminder(conn, a.user, a.reminder, d, a.edit, a.note)))


def cmd_correct(a, s, conn):
    if not a.yes:
        try:
            ans = input(f"Change expiry of {a.doc} to {a.expiry}? Old value is kept as history. [y/N] ")
        except EOFError:
            ans = ""
        if ans.strip().lower() != "y":
            print("aborted: confirmation required")
            return 4
    print(json.dumps(actions.correct_expiry(conn, a.user, a.doc, a.expiry, a.reason), indent=2))


def cmd_answer(a, s, conn):
    print(json.dumps(actions.answer_question(conn, a.user, a.question, a.lead_days)))


def cmd_prefs(a, s, conn):
    if not db.get_user(conn, a.user):
        raise actions.NotFound(f"unknown user {a.user}")
    if a.set:
        key, value = a.set.split("=", 1)
        if key != "buffer_days" or not value.isdigit():
            raise actions.ActionError("only buffer_days=<non-negative integer> can be set here")
        db.set_pref(conn, a.user, key, int(value), "user")
        db.log_event(conn, a.user, "memory_write", "prefs", {key: value}, "saved", "ok")
    print(json.dumps(memory.get_preferences(conn, a.user)))


def cmd_history(a, s, conn):
    if not db.get_document(conn, a.user, a.doc):
        raise actions.NotFound(f"no document {a.doc!r} for user {a.user}")
    for r in conn.execute("SELECT * FROM document_versions WHERE user_id=? AND doc_id=? ORDER BY version_id",
                          (a.user, a.doc)):
        print(f"v{r['version_id']} {r['expiry_date']} [{r['status']}] origin={r['origin']} recorded={r['recorded_at']}"
              f" reason={r['reason']!r}" + (f" superseded={r['superseded_at']}" if r["superseded_at"] else ""))


def cmd_memory(a, s, conn):
    doc = next((d for d in conn.execute(
        "SELECT d.*, v.expiry_date, v.version_id FROM documents d JOIN document_versions v ON v.doc_id=d.doc_id "
        "AND v.status='current' WHERE d.user_id=? AND d.doc_id=?", (a.user, a.doc))), None)
    if not doc:
        raise actions.NotFound(f"no document {a.doc!r} for user {a.user}")
    print(json.dumps(memory.retrieve_memory(conn, a.user, dict(doc), _today(a), s, None, _today(a)), indent=2))


def cmd_log(a, s, conn):
    q, params = "SELECT * FROM action_log WHERE user_id=?", [a.user]
    if a.type:
        q, params = q + " AND event_type=?", params + [a.type]
    rows = conn.execute(q + " ORDER BY log_id DESC LIMIT ?", params + [a.last]).fetchall()[::-1]
    if a.csv:
        w = csv.writer(sys.stdout)
        w.writerow(rows[0].keys() if rows else [])
        for r in rows:
            w.writerow(list(r))
        return
    for r in rows:
        print(f"{r['log_id']:>4} {r['ts']} pid={r['pid']} cyc={r['cycle_id']} {r['event_type']:<12}"
              f"{(r['tool'] or ''):<20}{r['outcome']:<12}{(r['output_summary'] or '')[:110]}")


def cmd_export(a, s, conn):
    from .exporter import export_register
    s.output_dir = a.output_dir or s.output_dir
    print(export_register(conn, a.user, _today(a), s))


def build_parser():
    p = argparse.ArgumentParser(prog="docrenew", description="Personal document renewal assistant (fictional data, local only)")
    p.add_argument("--db", help="SQLite path (default: $DOCRENEW_DB or ./docrenew.db)")
    sub = p.add_subparsers(dest="cmd", required=True)

    def cmd(name, fn, help_, user=True, today=False):
        sp = sub.add_parser(name, help=help_)
        if user:
            sp.add_argument("--user", required=True)
        if today:
            sp.add_argument("--today", help="simulated date YYYY-MM-DD (manually advanced clock)")
        sp.set_defaults(fn=fn)
        return sp

    sp = cmd("onboard", cmd_onboard, "load documents CSV and requirements file once", today=True)
    sp.add_argument("--name", required=True); sp.add_argument("--documents", required=True)
    sp.add_argument("--requirements", required=True)

    sp = cmd("review", cmd_review, "run one review cycle", today=True)
    sp.add_argument("--provider", choices=["offline", "groq", "ollama"])
    sp.add_argument("--no-memory", action="store_true", help="ablation: do not retrieve saved state")
    sp.add_argument("--resume", action="store_true", help="resume an interrupted cycle from its checkpoint")
    sp.add_argument("--simulate-failure", choices=["search", "writer_after_checklist"])
    sp.add_argument("--no-export", action="store_true"); sp.add_argument("--output-dir")

    sp = cmd("status", cmd_status, "show saved state, next actions and approvals", today=True)
    sp.add_argument("--json", action="store_true")

    sp = cmd("tick", cmd_tick, "mark a checklist step done/todo/skipped")
    sp.add_argument("--doc", required=True); sp.add_argument("--step", type=int, required=True)
    sp.add_argument("--status", default="done", choices=["done", "todo", "skipped"])

    sp = cmd("reminders", cmd_reminders, "list reminder drafts (all UNSENT)")
    sp.add_argument("--doc"); sp.add_argument("--full", action="store_true")

    sp = cmd("decide", cmd_decide, "approve / edit / reject a reminder draft")
    sp.add_argument("--reminder", type=int, required=True)
    g = sp.add_mutually_exclusive_group(required=True)
    g.add_argument("--approve", action="store_true"); g.add_argument("--reject", action="store_true")
    g.add_argument("--edit", metavar="TEXT")
    sp.add_argument("--note")

    sp = cmd("correct", cmd_correct, "correct an expiry date (old value kept as history)")
    sp.add_argument("--doc", required=True); sp.add_argument("--expiry", required=True)
    sp.add_argument("--reason", required=True); sp.add_argument("--yes", action="store_true")

    sp = cmd("answer", cmd_answer, "answer an open lead-time question")
    sp.add_argument("--question", type=int, required=True); sp.add_argument("--lead-days", type=int)

    sp = cmd("prefs", cmd_prefs, "show or set preferences (buffer_days=N)")
    sp.add_argument("--set")

    sp = cmd("history", cmd_history, "expiry-date version history of a document")
    sp.add_argument("--doc", required=True)

    sp = cmd("memory", cmd_memory, "inspect what is remembered about a document", today=True)
    sp.add_argument("--doc", required=True)

    sp = cmd("log", cmd_log, "show the action log")
    sp.add_argument("--last", type=int, default=30); sp.add_argument("--type"); sp.add_argument("--csv", action="store_true")

    sp = cmd("export", cmd_export, "regenerate the Excel review view", today=True)
    sp.add_argument("--output-dir")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    settings = get_settings()
    conn = _conn(args, settings)
    try:
        return args.fn(args, settings, conn) or 0
    except (actions.NotFound, actions.ActionError, memory.AccessDenied, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
