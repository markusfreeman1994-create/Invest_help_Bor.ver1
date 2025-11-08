from __future__ import annotations

import os
import ujson as json
import tempfile
from typing import Dict
from .config import DATA_PATH

def load_db() -> Dict[str, Dict]:
    if not DATA_PATH.exists():
        return {}
    try:
        return json.loads(DATA_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}

def save_db(db: Dict[str, Dict]):
    tmp_fd, tmp_path = tempfile.mkstemp(prefix="data.", suffix=".json")
    with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
        f.write(json.dumps(db, ensure_ascii=False, indent=2))
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, DATA_PATH)

db: Dict[str, Dict] = load_db()

def get_user(db_ref: Dict[str, Dict], uid: int) -> Dict:
    return db_ref.get(str(uid), {"tickers": [], "base": "usd"})

def set_user(db_ref: Dict[str, Dict], uid: int, user: Dict):
    db_ref[str(uid)] = user
    save_db(db_ref)