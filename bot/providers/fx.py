from __future__ import annotations

from typing import Dict, Optional
from ..config import logger
from ..utils_http import http_get_json

_fx_cache: Dict[str, float] = {}

def fx_rate(frm: str, to: str) -> Optional[float]:
    frm = (frm or "").upper()
    to = (to or "").upper()
    if not frm or not to:
        return None
    if frm == to:
        return 1.0
    key = f"{frm}->{to}"
    if key in _fx_cache:
        return _fx_cache[key]
    data = http_get_json(
        "https://api.exchangerate.host/convert",
        params={"from": frm, "to": to},
        timeout=10,
    )
    rate = None
    if isinstance(data, dict):
        rate = data.get("result") or (data.get("info", {}) or {}).get("rate")
    if isinstance(rate, (int, float)) and rate > 0:
        _fx_cache[key] = float(rate)
        return _fx_cache[key]
    logger.warning("FX rate fetch failed %s -> %s", frm, to)
    data2 = http_get_json(f"https://open.er-api.com/v6/latest/{frm}", timeout=10)
    try:
        rate2 = (data2 or {}).get("rates", {}).get(to)
        if isinstance(rate2, (int, float)) and rate2 > 0:
            _fx_cache[key] = float(rate2)
            return _fx_cache[key]
    except Exception:
        pass
    return None