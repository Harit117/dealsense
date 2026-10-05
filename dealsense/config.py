"""Runtime configuration, read from environment variables (and an optional .env file)."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv() -> None:
    env = ROOT / ".env"
    if not env.exists():
        return
    for line in env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv()

DB_PATH = Path(os.getenv("DEALSENSE_DB", ROOT / "data" / "dealsense.db"))
BROKER_NAME = os.getenv("DEALSENSE_BROKER", "Anil")

# LLM enrichment. Without a key, DealSense falls back to its rule-based extractor.
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
LLM_MODEL = os.getenv("DEALSENSE_MODEL", "claude-opus-5-5")
USE_LLM = os.getenv("DEALSENSE_USE_LLM", "auto")  # auto | on | off

# Alert channels (all optional; alerts are always stored in the in-app outbox).
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
SLACK_WEBHOOK_URL = os.getenv("SLACK_WEBHOOK_URL", "")

# n8n orchestration (optional). DealSense POSTs every routed deal here so the n8n
# "W3 Decide & Act" workflow can switch on the score and deliver it.
N8N_DEAL_WEBHOOK = os.getenv("N8N_DEAL_WEBHOOK", "")

# Routing thresholds from the deck: 80+ instant alert, 50-79 digest, <50 nurture.
HOT_THRESHOLD = 80
DIGEST_THRESHOLD = 50

# Brokerage commission used to turn a property price into deal value for the broker.
COMMISSION_RATE = float(os.getenv("DEALSENSE_COMMISSION", "0.02"))

# Daily refresh (W4) time, local server time, HH:MM.
DAILY_REFRESH_AT = os.getenv("DEALSENSE_REFRESH_AT", "08:00")


def llm_enabled() -> bool:
    if USE_LLM == "off":
        return False
    return bool(ANTHROPIC_API_KEY) or USE_LLM == "on"
