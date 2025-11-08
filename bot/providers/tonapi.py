from __future__ import annotations

from typing import Dict, List
from ..config import TONAPI_BASE, logger
from ..utils_http import http_get_json

def fetch_ton_rates(symbols: List[str], vs: str) -> Dict[str, Dict[str, float]]:
    out: Dict[str, Dict[str, float]] = {}
    if not any(s.upper() == "TON" for s in symbols):
        return out
    params = {"tokens": "ton", "currencies": vs}
    data = http_get_json(f"{TONAPI_BASE}/v2/rates", params=params, timeout=10)
    try:
        if isinstance(data, dict) and "rates" in data:
            ton_obj = data["rates"].get("ton") or data["rates"].get("TON")
            if isinstance(ton_obj, dict):
                candidates = []
                if "prices" in ton_obj and isinstance(ton_obj["prices"], dict):
                    candidates.append(ton_obj["prices"])
                if "values" in ton_obj and isinstance(ton_obj["values"], dict):
                    candidates.append(ton_obj["values"])
                if isinstance(ton_obj.get(vs), (int, float)):
                    candidates.append({vs: ton_obj.get(vs)})
                price = None
                for d in candidates:
                    if isinstance(d.get(vs), (int, float)):
                        price = float(d[vs]); break
                if price is not None:
                    out["TON"] = {"price": price, "chg": None}
    except Exception as e:
        logger.warning("TonAPI parse error: %s", e)
    return out