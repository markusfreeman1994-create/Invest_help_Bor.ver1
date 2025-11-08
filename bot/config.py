from __future__ import annotations

import os
import logging
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

COINGECKO_BASE = os.getenv("COINGECKO_BASE", "https://api.coingecko.com/api/v3")
COINGECKO_API_KEY = os.getenv("COINGECKO_API_KEY") or os.getenv("CG_API_KEY")
TONAPI_BASE = os.getenv("TONAPI_BASE", "https://tonapi.io")
DEFAULT_BASE = os.getenv("DEFAULT_BASE", "usd").lower()
DATA_PATH = Path("data.json")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("invest_bot")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)