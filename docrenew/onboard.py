"""Onboarding: load the documents list and the requirements file ONCE per content hash.

Re-loading an identical file is a no-op. A changed requirements file becomes a new source version
(the old one is marked superseded). A changed documents CSV never overwrites existing documents or
user corrections; it can only add documents that are not known yet.
"""
import hashlib
import json
import re
from pathlib import Path

from . import db, guard
from .tools import parse_documents_csv

_LEAD_RE = re.compile(r"^\s*lead_days\s*:\s*(\d+)\s*$", re.IGNORECASE)
_STEP_RE = re.compile(r"^\s*STEP\s+(\d+)\s*:\s*(.+?)\s*$", re.IGNORECASE)


def parse_requirements(text: str) -> list[dict]:
    """Sections start with '## <doc_type>'. Lines matching injection patterns are quarantined."""
    chunks, cur = [], None
    lines = text.splitlines()
    for no, line in enumerate(lines, start=1):
        if line.startswith("## "):
            if cur:
                cur["line_end"] = no - 1
                chunks.append(cur)
            heading = line[3:].strip()
            cur = {"doc_type": re.sub(r"[^a-z0-9]+", "_", heading.lower()).strip("_"), "heading": heading,
                   "lead_days": None, "steps": [], "body": [], "quarantined": [],
                   "line_start": no, "line_end": no}
            continue
        if cur is None or not line.strip() or line.lstrip().startswith("#"):
            continue
        hits = guard.scan_text(line)
        if hits:
            cur["quarantined"].append({"line": no, "patterns": hits, "text": guard.clean_line(line, 160)})
            continue
        m_lead, m_step = _LEAD_RE.match(line), _STEP_RE.match(line)
        if m_lead:
            cur["lead_days"] = int(m_lead.group(1))
        elif m_step:
            cur["steps"].append({"step_no": int(m_step.group(1)), "text": guard.clean_line(m_step.group(2)), "line": no})
        else:
            cur["body"].append(guard.clean_line(line))
    if cur:
        cur["line_end"] = len(lines)
        chunks.append(cur)
    return chunks


def _sha(path: str) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _register_source(conn, user_id, kind, path, sha, now):
    """Returns (source_id, is_new)."""
    row = conn.execute("SELECT source_id FROM sources WHERE user_id=? AND kind=? AND sha256=?",
                       (user_id, kind, sha)).fetchone()
    if row:
        return row["source_id"], False
    version = conn.execute("SELECT COALESCE(MAX(version),0)+1 v FROM sources WHERE user_id=? AND kind=?",
                           (user_id, kind)).fetchone()["v"]
    conn.execute("UPDATE sources SET status='superseded' WHERE user_id=? AND kind=? AND status='current'",
                 (user_id, kind))
    cur = conn.execute("INSERT INTO sources (user_id,kind,path,sha256,version,status,loaded_at) "
                       "VALUES (?,?,?,?,?, 'current', ?)", (user_id, kind, str(path), sha, version, now))
    return cur.lastrowid, True


def onboard(conn, user_id: str, display_name: str, documents_csv: str, requirements_txt: str, sim_date=None) -> dict:
    now = db.now_utc()
    report = {"user_id": user_id, "documents_added": 0, "documents_skipped": 0, "csv_errors": [],
              "documents_source": "skipped (identical file already loaded)",
              "requirements_source": "skipped (identical file already loaded)", "chunks": 0, "quarantined": 0}
    with db.tx(conn):
        conn.execute("INSERT OR IGNORE INTO users (user_id, display_name, created_at) VALUES (?,?,?)",
                     (user_id, display_name, now))

    # ---- documents CSV ------------------------------------------------------------------------
    sha = _sha(documents_csv)
    with db.tx(conn):
        src_id, is_new = _register_source(conn, user_id, "documents_csv", documents_csv, sha, now)
        if is_new:
            rows, errors = parse_documents_csv(documents_csv)
            report["csv_errors"] = errors
            for r in rows:
                doc_id = f"{user_id}-{r['doc_type']}"
                if db.get_document(conn, user_id, doc_id):
                    report["documents_skipped"] += 1        # never overwrite existing / corrected records
                    continue
                conn.execute("INSERT INTO documents (doc_id,user_id,doc_type,label,created_at) VALUES (?,?,?,?,?)",
                             (doc_id, user_id, r["doc_type"], r["label"], now))
                conn.execute("INSERT INTO document_versions (doc_id,user_id,expiry_date,origin,source_id,status,"
                             "reason,recorded_at) VALUES (?,?,?, 'csv_import', ?, 'current', 'initial import', ?)",
                             (doc_id, user_id, r["expiry_date"], src_id, now))
                report["documents_added"] += 1
            report["documents_source"] = f"loaded (sha256 {sha[:12]})"
    db.log_event(conn, user_id, "memory_write", "onboard", {"file": Path(documents_csv).name, "sha256": sha[:12]},
                 {k: report[k] for k in ("documents_added", "documents_skipped", "documents_source")},
                 "ok", None, sim_date, idem_key=f"{user_id}|documents_csv|{sha[:12]}")

    # ---- requirements file --------------------------------------------------------------------
    sha = _sha(requirements_txt)
    with db.tx(conn):
        src_id, is_new = _register_source(conn, user_id, "requirements", requirements_txt, sha, now)
        if is_new:
            chunks = parse_requirements(Path(requirements_txt).read_text(encoding="utf-8"))
            for ch in chunks:
                conn.execute(
                    "INSERT INTO requirement_chunks (user_id,source_id,doc_type,heading,lead_days,body,steps_json,"
                    "quarantined_json,line_start,line_end) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (user_id, src_id, ch["doc_type"], ch["heading"], ch["lead_days"], "\n".join(ch["body"]),
                     json.dumps(ch["steps"]), json.dumps(ch["quarantined"]), ch["line_start"], ch["line_end"]))
                report["quarantined"] += len(ch["quarantined"])
                for q in ch["quarantined"]:
                    db.log_event(conn, user_id, "guard", "onboard",
                                 {"file": Path(requirements_txt).name, "line": q["line"]},
                                 {"quarantined": q["text"], "patterns": q["patterns"]}, "quarantined", None, sim_date)
            report["chunks"] = len(chunks)
            report["requirements_source"] = f"loaded (sha256 {sha[:12]}, {len(chunks)} sections)"
    db.log_event(conn, user_id, "memory_write", "onboard", {"file": Path(requirements_txt).name, "sha256": sha[:12]},
                 {"chunks": report["chunks"], "quarantined": report["quarantined"],
                  "status": report["requirements_source"]}, "ok", None, sim_date,
                 idem_key=f"{user_id}|requirements|{sha[:12]}")
    return report
