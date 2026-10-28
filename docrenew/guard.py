"""Safety layer: treat file/email/repo text as UNTRUSTED DATA and validate every LLM output.

The model can only choose one of four actions. It cannot send messages, change expiry dates,
approve reminders or choose recipients: those capabilities simply do not exist as tools.
"""
import re

INJECTION_PATTERNS = {
    "ignore_instructions": r"ignore\s+(all\s+|any\s+)?(the\s+)?(previous|prior|above|earlier)\s+(instructions|rules|messages|policy)",
    "disregard": r"disregard\s+(all\s+|any\s+)?(the\s+)?(previous|prior|above|system)",
    "role_prefix": r"^\s*(system|assistant|developer)\s*:",
    "role_change": r"you\s+are\s+now\b",
    "new_instructions": r"\bnew\s+instructions?\b",
    "send_message": r"\bsend\b.{0,60}\b(e-?mail|mail|message|sms|whatsapp)\b",
    "email_address": r"[\w.+-]+@[\w-]+\.[\w.-]+",
    "mark_done": r"\bmark\b.{0,40}\b(renewed|completed?|done)\b",
    "change_fields": r"\b(change|set|update|overwrite)\b.{0,30}\b(expiry|expiration|recipient|status|date)\b",
    "hide_from_user": r"do\s+not\s+(tell|inform|show|notify)\s+the\s+user",
    "reveal_secrets": r"reveal\b.{0,30}\b(system\s+prompt|instructions|keys?|password)",
}
_COMPILED = {k: re.compile(v, re.IGNORECASE) for k, v in INJECTION_PATTERNS.items()}

ALLOWED_ACTIONS = ("search_requirements", "write_plan", "ask_user", "wait")
FORBIDDEN_KEYS = {"to", "recipient", "recipients", "email", "send", "new_expiry", "expiry",
                  "expiry_date", "status", "tool", "command", "approve", "delete"}


def scan_text(text: str) -> list[str]:
    """Names of injection patterns that match this text (empty list = clean)."""
    return [name for name, rx in _COMPILED.items() if rx.search(text or "")]


def clean_line(text: str, max_len: int = 300) -> str:
    """Strip control characters and trim. Used for any free text that may reach a prompt or a draft."""
    text = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", " ", text or "")
    return re.sub(r"\s+", " ", text).strip()[:max_len]


def scrub_free_text(text: str, max_len: int = 240) -> str:
    """Model-written free text (reminder intro) must be plain: no e-mail, URLs or injection phrases."""
    text = clean_line(text, max_len)
    text = re.sub(r"https?://\S+", "[link removed]", text)
    text = re.sub(r"[\w.+-]+@[\w-]+\.[\w.-]+", "[address removed]", text)
    return "" if scan_text(text) else text


def validate_decision(raw) -> tuple[dict | None, list[str]]:
    """Validate a model decision. Returns (clean_decision_or_None, problems)."""
    problems: list[str] = []
    if not isinstance(raw, dict):
        return None, ["decision is not a JSON object"]
    extra = sorted(FORBIDDEN_KEYS & {str(k).lower() for k in raw})
    if extra:
        problems.append(f"forbidden keys ignored: {extra}")
    action = raw.get("action")
    if action not in ALLOWED_ACTIONS:
        return None, problems + [f"unknown action {action!r}"]
    clean = {"action": action, "reason": clean_line(str(raw.get("reason", "")), 300)}
    if action == "search_requirements":
        query = clean_line(str(raw.get("query", "")), 120)
        if not query:
            return None, problems + ["search_requirements needs a query"]
        clean["query"] = query
    elif action == "ask_user":
        question = clean_line(str(raw.get("question", "")), 300)
        if not question:
            return None, problems + ["ask_user needs a question"]
        clean["question"] = question
    elif action == "write_plan":
        clean["message_intro"] = scrub_free_text(str(raw.get("message_intro", "")))
    return clean, problems
