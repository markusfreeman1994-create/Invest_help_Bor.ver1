from __future__ import annotations

from typing import List, Tuple
from datetime import datetime, timedelta
import io
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from .providers.stooq import fetch_history as stooq_history
from .providers.coingecko import fetch_history as cg_history
from .providers.fx import fx_rate

def parse_period(arg: str) -> int:
    s = (arg or "").lower().strip()
    if s.endswith("d") and s[:-1].isdigit(): return max(1, int(s[:-1]))
    if s in {"1y","12m"}: return 365
    if s in {"3m"}: return 90
    if s in {"6m"}: return 180
    return 7

def make_chart(series: List[Tuple[datetime, float]], ticker: str, base: str) -> bytes:
    if not series: return b""
    if len(series)==1:
        d0, v0 = series[0]; series = [(d0 - timedelta(hours=1), v0), (d0, v0)]
    dates = [d for d,_ in series]; values = [v for _,v in series]
    fig, ax = plt.subplots(figsize=(6,3))
    ax.plot(dates, values, linewidth=2)
    ax.set_title(f"{ticker} in {base.upper()}"); ax.set_xlabel("Date"); ax.set_ylabel(base.upper())
    ax.grid(True, linestyle="--", linewidth=0.5, alpha=0.5)
    ax.xaxis.set_major_locator(mdates.AutoDateLocator())
    ax.xaxis.set_major_formatter(mdates.ConciseDateFormatter(ax.xaxis.get_major_locator()))
    fig.tight_layout()
    buf = io.BytesIO(); fig.savefig(buf, format="png", dpi=160); plt.close(fig); buf.seek(0)
    return buf.read()

def history_for_chart(ticker: str, base: str, days: int):
    stq = stooq_history(ticker, days)
    if stq:
        rate = fx_rate("USD", base.upper())
        if isinstance(rate, (int, float)) and rate > 0:
            return [(d, v * rate) for (d, v) in stq]
    return cg_history(ticker, base, days)