from __future__ import annotations

from typing import Dict
import requests
from .config import logger

def http_get_json(url: str, params: Dict = None, headers: Dict = None, timeout: int = 10):
    try:
        r = requests.get(url, params=params, headers=headers, timeout=timeout)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        logger.warning("GET %s failed: %s", url, e)
        return None