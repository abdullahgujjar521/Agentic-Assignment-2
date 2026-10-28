"""Configuration. Secrets come only from environment variables or an untracked .env file."""
import os
from dataclasses import dataclass
from pathlib import Path


def load_dotenv(path: str = ".env") -> None:
    """Tiny .env reader (no dependency). Real environment variables win."""
    p = Path(path)
    if not p.exists():
        return
    for raw_line in p.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()

        # Accept common .env formats like "KEY=value" and "KEY=value # comment".
        if value.startswith(('"', "'")) and value.endswith(value[:1]) and len(value) >= 2:
            value = value[1:-1]
        else:
            value = value.split(" #", 1)[0].split("#", 1)[0].strip()

        os.environ.setdefault(key, value)


@dataclass
class Settings:
    db_path: str
    provider: str            # offline | groq | ollama
    groq_model: str
    ollama_model: str
    ollama_host: str
    output_dir: str
    candidate_horizon_days: int = 120   # documents expiring within this window are reviewed
    start_now_window_days: int = 21     # "start now" if latest start date is within this many days
    reminder_limit: int = 3             # max reminder drafts per document version
    max_steps: int = 4                  # bound on agent steps per document


def get_settings() -> Settings:
    load_dotenv()
    return Settings(
        db_path=os.environ.get("DOCRENEW_DB", "docrenew.db"),
        provider=os.environ.get("DOCRENEW_PROVIDER", "offline"),
        groq_model=os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile"),
        ollama_model=os.environ.get("OLLAMA_MODEL", "llama3.1:8b"),
        ollama_host=os.environ.get("OLLAMA_HOST", "http://localhost:11434"),
        output_dir=os.environ.get("DOCRENEW_OUTPUT_DIR", "outputs"),
    )
