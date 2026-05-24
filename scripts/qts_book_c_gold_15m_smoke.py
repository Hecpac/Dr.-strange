"""Book C smoke — XAUT/USDT 15m TF, same window as live A/B.

Bridge from Dr.-strange workspace into QTS-ARCHITECT runtime.
Tests Hector's hypothesis: does 15m produce more trades on Gold ICT NY 8-11?
Same Sprint 6 canonical guardrails as the active A/B; only TF changes.
Window = T0 of A/B (2026-05-11) → now, directly comparable to live Book A.

Run with: ~/Projects/QTS-ARCHITECT/.venv/bin/python ~/Projects/Dr.-strange/scripts/qts_book_c_gold_15m_smoke.py
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import polars as pl

QTS_ROOT = Path.home() / "Projects" / "QTS-ARCHITECT"
sys.path.insert(0, str(QTS_ROOT / "qts_core" / "src"))

import structlog  # noqa: E402

from qts_core.agents.base import StrictRiskAgent  # noqa: E402
from qts_core.agents.ict import ICTSmartMoneyAgent  # noqa: E402
from qts_core.agents.supervisor import Supervisor  # noqa: E402
from qts_core.backtest.engine import BacktestConfig, EventEngine  # noqa: E402

log = structlog.get_logger()

REPORTS_DIR = QTS_ROOT / "reports"
REPORTS_DIR.mkdir(parents=True, exist_ok=True)

SYMBOL = "XAUT/USDT"
T0_ISO = "2026-05-11T12:00:00Z"
TIMEFRAME = "15m"
INITIAL_CAPITAL = 50_000.0
TRADE_SIZE = 5_000.0
SESSION_START_NY = 8
SESSION_END_NY = 11
SESSION_TZ_NY = "America/New_York"


async def download_ohlcv(symbol, since_iso, until_iso, timeframe, exchange_id="okx"):
    import ccxt.async_support as ccxt_async
    klass = getattr(ccxt_async, exchange_id)
    exchange = klass({"enableRateLimit": True})
    try:
        since_ms = int(datetime.fromisoformat(since_iso.replace("Z", "+00:00")).timestamp() * 1000)
        until_ms = int(datetime.fromisoformat(until_iso.replace("Z", "+00:00")).timestamp() * 1000)
        all_rows = []
        cursor = since_ms
        while cursor < until_ms:
            candles = await exchange.fetch_ohlcv(symbol, timeframe=timeframe, since=cursor, limit=300)
            if not candles:
                break
            all_rows.extend([c for c in candles if c[0] <= until_ms])
            last_ts = candles[-1][0]
            if last_ts <= cursor:
                break
            cursor = last_ts + 1
            if len(candles) < 300:
                break
        seen = set()
        deduped = []
        for r in all_rows:
            if r[0] in seen:
                continue
            seen.add(r[0])
            deduped.append(r)
        if not deduped:
            return pl.DataFrame({"timestamp": [], "instrument_id": [], "open": [], "high": [],
                                  "low": [], "close": [], "volume": []})
        return pl.DataFrame({
            "timestamp": [datetime.fromtimestamp(r[0] / 1000, tz=timezone.utc) for r in deduped],
            "instrument_id": [symbol] * len(deduped),
            "open": [float(r[1]) for r in deduped],
            "high": [float(r[2]) for r in deduped],
            "low": [float(r[3]) for r in deduped],
            "close": [float(r[4]) for r in deduped],
            "volume": [float(r[5]) for r in deduped],
        })
    finally:
        await exchange.close()


def make_supervisor():
    ict = ICTSmartMoneyAgent(
        name=f"ICT_{SYMBOL}",
        symbol=SYMBOL,
        session_start=SESSION_START_NY,
        session_end=SESSION_END_NY,
        session_timezone=SESSION_TZ_NY,
        min_fvg_size=0.001,
        base_confidence=0.80,
        min_confidence=0.6,
    )
    risk = StrictRiskAgent(
        name=f"Risk_{SYMBOL}",
        min_signal_confidence=0.6,
        max_position_size=0.50,
        max_daily_loss=0.05,
    )
    return Supervisor(strategy_agents=[ict], risk_agent=risk, min_confidence=0.6)


def canonical_config():
    return BacktestConfig(
        initial_capital=INITIAL_CAPITAL,
        trade_size=TRADE_SIZE,
        slippage_bps=5.0,
        commission_bps=15.0,
        risk_fraction=0.10,
        max_position_size=0.50,
        stop_loss_pct=0.05,
        volatility_regime_enabled=True,
        volatility_regime_zscore_threshold=2.5,
        volatility_regime_window_bars=48,
        volatility_regime_min_observations=12,
        high_volatility_hours_high_regime_utc=(13, 14, 15, 16, 17, 18),
        high_volatility_hours_entry_block=True,
        force_close_positions_at_end=False,
        risk_cap_per_position=0.15,
        dynamic_extreme_block_enabled=True,
        dynamic_extreme_block_window_bars=48,
        dynamic_extreme_block_zscore_threshold=2.5,
        dynamic_extreme_block_mode="hard",
    )


async def main():
    now_utc = datetime.now(timezone.utc)
    until_iso = now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")

    log.info("book_c_smoke_start", t0=T0_ISO, until=until_iso, tf=TIMEFRAME)
    df = await download_ohlcv(SYMBOL, T0_ISO, until_iso, TIMEFRAME)
    log.info("download_done", bars=len(df))

    if len(df) == 0:
        print(json.dumps({"status": "no_bars"}))
        return

    sup = make_supervisor()
    cfg = canonical_config()
    engine = EventEngine(sup, cfg)
    result = await engine.run(df)

    trades = result.trades
    last_close = float(df["close"][-1])

    cash = INITIAL_CAPITAL
    pos_qty = 0.0
    pos_avg = 0.0
    realized = 0.0
    for t in sorted(trades, key=lambda x: x.timestamp):
        qty = float(t.quantity)
        price = float(t.price)
        commission = float(t.commission)
        signed = qty if "BUY" in str(t.side) else -qty
        if pos_qty != 0 and (pos_qty * signed < 0):
            close_qty = min(abs(pos_qty), abs(signed))
            pnl = (price - pos_avg) * close_qty * (1 if pos_qty > 0 else -1)
            realized += pnl
            cash += pnl
            pos_qty += close_qty if pos_qty < 0 else -close_qty
            if abs(pos_qty) < 1e-9:
                pos_qty = 0.0
                pos_avg = 0.0
            leftover = abs(signed) - close_qty
            if leftover > 1e-9:
                pos_qty = leftover if signed > 0 else -leftover
                pos_avg = price
        else:
            new_qty = pos_qty + signed
            if pos_qty == 0 or (pos_qty * signed > 0):
                total = pos_qty * pos_avg + signed * price
                pos_qty = new_qty
                pos_avg = total / pos_qty if pos_qty != 0 else 0.0
        cash -= commission
    unrealized = (last_close - pos_avg) * pos_qty if pos_qty != 0 else 0.0
    final_equity = cash + unrealized
    ret_pct = (final_equity / INITIAL_CAPITAL - 1) * 100

    days_elapsed = (now_utc - datetime.fromisoformat(T0_ISO.replace("Z", "+00:00"))).total_seconds() / 86400.0
    date_str = now_utc.strftime("%Y%m%d-%H%M")

    report = {
        "symbol": SYMBOL,
        "timeframe": TIMEFRAME,
        "window": {"t0": T0_ISO, "until": until_iso, "days": round(days_elapsed, 2)},
        "bars": len(df),
        "trades_count": len(trades),
        "final_capital": round(final_equity, 2),
        "return_pct": round(ret_pct, 4),
        "realized_pnl": round(realized, 2),
        "unrealized_pnl": round(unrealized, 2),
        "open_position": {
            "qty": round(pos_qty, 6),
            "avg_price": round(pos_avg, 4),
            "mark_price": round(last_close, 4),
        } if abs(pos_qty) > 1e-9 else None,
        "comparison_book_a_1h": {"trades": 1, "return_pct": -0.3183},
    }

    snap_json = REPORTS_DIR / f"qts-book-c-gold-15m-smoke-{date_str}.json"
    snap_json.write_text(json.dumps(report, indent=2, default=str))

    md_lines = [
        f"# QTS Book C Smoke — Gold 15m — {now_utc.strftime('%Y-%m-%d %H:%M UTC')}",
        "",
        f"- Symbol: {SYMBOL}  ·  TF: {TIMEFRAME}",
        f"- Window: {T0_ISO} -> {until_iso} ({round(days_elapsed, 2)} days)",
        f"- Bars consumed: {len(df)}",
        f"- Same Sprint 6 guardrails as live A/B (only TF changed)",
        "",
        "## Results",
        f"- Trades: **{len(trades)}**",
        f"- Final capital: ${final_equity:,.2f}  ({ret_pct:+.4f}%)",
        f"- Realized: ${realized:+,.2f}  ·  Unrealized: ${unrealized:+,.2f}",
        f"- Open position: {report['open_position'] or 'flat'}",
        "",
        "## Comparison with live Book A (1h, same window)",
        f"- Book A (1h): 1 trade, -0.3183%",
        f"- Book C (15m): {len(trades)} trades, {ret_pct:+.4f}%",
    ]
    snap_md = REPORTS_DIR / f"qts-book-c-gold-15m-smoke-{date_str}.md"
    snap_md.write_text("\n".join(md_lines))

    print(json.dumps({"status": "ok", **report, "snap_md": str(snap_md)}, indent=2, default=str))


if __name__ == "__main__":
    asyncio.run(main())
