# DocRenew - Personal Document Renewal Assistant (D10)

Agentic AI assignment, student 23i-2641 (Muhammad Abdullah). A single-agent Python CLI that keeps a
student's expiring documents (passport, CNIC, driving licence, student card) on track across
repeated reviews. **All data is fictional and local. The system never sends anything.**

## What it does
Each review: read the documents -> pick those near expiry -> the LLM chooses what to do for each
(search requirements / write a plan / ask you / wait) -> code validates the choice -> tools run ->
a checklist and an **UNSENT** reminder draft are saved to SQLite and an Excel review view. You approve,
edit or reject. Everything is remembered across process restarts.

## Setup (Windows / macOS / Linux)
```bash
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                                    # optional; offline provider needs no key
```
Providers (`--provider` or `DOCRENEW_PROVIDER`):
* `offline` - deterministic rule policy with the same input/output contract as a model. Default; no network.
* `groq` - set `GROQ_API_KEY` in `.env`. Default model `llama-3.3-70b-versatile` (change with `GROQ_MODEL`).
* `ollama` - run `ollama pull llama3.1:8b` and keep the server running locally.

## Quick demo (about 8 minutes)
```bash
export DOCRENEW_DB=demo.db
python -m docrenew.cli onboard --user u001 --name "Ali Raza (fictional)" --documents data/documents_u001.csv --requirements data/requirements.txt
python -m docrenew.cli review  --user u001 --today 2026-10-24                 # cycle 1
python -m docrenew.cli tick    --user u001 --doc u001-passport --step 1       # you did step 1
python -m docrenew.cli decide  --user u001 --reminder 1 --approve             # approve a draft (still unsent)
python -m docrenew.cli review  --user u001 --today 2026-11-14                 # cycle 2: remembers progress
python -m docrenew.cli correct --user u001 --doc u001-passport --expiry 2027-04-30 --reason "misread date" --yes
python -m docrenew.cli review  --user u001 --today 2026-12-05                 # cycle 3 (new process): uses corrected date
python -m docrenew.cli history --user u001 --doc u001-passport                # old value kept as history
python -m docrenew.cli log     --user u001 --last 40                          # every tool call / memory read / write
```
Failure + retry: `review --simulate-failure writer_after_checklist` then `review --resume`.
Memory ablation: `review --no-memory`. Untrusted-text test: onboard with `data/variants/requirements_injected.txt`.

## Evaluation
`python -m eval.run_scenarios` runs 12 scenarios, the memory on/off comparison, 5 fact checks and metrics
through real CLI processes and rewrites `results/`. `--provider groq` repeats it with a live model.

## Files
| Path | Purpose |
|---|---|
| `docrenew/schema.sql` | memory schema (initialised automatically) |
| `docrenew/agent.py` | decision loop, gates, review cycle, status view |
| `docrenew/tools.py` | the four tools (record reader, requirements search, date calculator, writer) |
| `docrenew/memory.py`, `onboard.py`, `actions.py` | retrieval + checkpoints, load-once onboarding, human-only actions |
| `docrenew/guard.py`, `llm.py`, `exporter.py`, `cli.py` | safety checks, model providers, Excel view, CLI |
| `data/` | fictional sample inputs; `data/variants/` conflicting and poisoned requirement files |
| `results/` | scenario results, action log, transcript, sample Excel + SQLite |

## Disclosure
Built with the assistance of Claude (Anthropic) as a coding assistant. Third-party code: `openpyxl` only.
Offline-provider results do not measure language-model quality; see the report's limitations.
