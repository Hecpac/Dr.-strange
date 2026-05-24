"""Book C paper trading runner — XAUT/USDT 15m TF — exploratory.

Bridge from Dr.-strange workspace into QTS-ARCHITECT runtime. Runs in
parallel with the live Sprint 5d A/B (which keeps 1h). Same Sprint 6
canonical guardrails — only the timeframe differs.

Window aligned with the active A/B (T0 = 2026-05-11) so per-day comparisons
stay valid. Exploratory: no promotion gate, no B-book control.

State: ~/Projects/QTS-ARCHITECT/state/paper_book_c_gold_15m.json
Reports: ~/Projects/QTS-ARCHITECT/reports/qts-paper-book-c-gold-15m-state-*

Run: ~/Projects/QTS-ARCHITECT/.venv/bin/python ~/Projects/Dr.-strange/scripts/qts_book_c_gold_15m_runner.py
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timedelta, timezone
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
STATE_DIR = QTS_ROOT / "state"
REPORTS_DIR.mkdir(parents=True, exist_ok=True)
STATE_DIR.mkdir(parents=True, exist_ok=True)

STATE_FILE = STATE_DIR / "paper_book_c_gold_15m.json"

SYMBOL = "XAUT/USDT"
T0_ISO = "2026-05-11T12:00:00Z"
WINDOW_DAYS = 90
TIMEFRAME = "15m"
SESSION_START_NY = 8
SESSION_END_NY = 11
SESSION_TZ_NY = "America/New_York"
INITIAL_CAPITAL = 50_000.0
TRADE_SIZE = 5_000.0


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


def serialize_trades(trades):
    out = []
    for t in trades:
        out.append({
            "trade_id": t.trade_id,
            "decision_id": t.decision_id,
            "timestamp": t.timestamp.isoformat(),
            "instrument_id": t.instrument_id,
            "side": str(t.side),
            "quantity": float(t.quantity),
            "price": float(t.price),
            "commission": float(t.commission),
            "slippage": float(t.slippage),
            "rationale": t.rationale,
        })
    return out


def compute_pnl(trades, initial_capital, df):
    if not trades:
        last_close = float(df["close"][-1]) if len(df) > 0 else 0.0
        return {"capital": initial_capital, "realized_pnl": 0.0, "unrealized_pnl": 0.0,
                "mark_price": last_close, "drawdown_pct": 0.0, "trades_count": 0,
                "open_position": None, "equity_curve": [initial_capital]}
    sorted_trades = sorted(trades, key=lambda x: x["timestamp"])
    cash = initial_capital
    pos_qty = 0.0
    pos_avg = 0.0
    realized = 0.0
    equity_points = [initial_capital]
    for t in sorted_trades:
        qty = t["quantity"]
        price = t["price"]
        commission = t.get("commission", price * qty * 0.0015)
        signed = qty if "BUY" in t["side"] else -qty
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
        unrealized = (price - pos_avg) * pos_qty
        equity_points.append(cash + unrealized)
    last_close = float(df["close"][-1]) if len(df) > 0 else pos_avg
    final_unrealized = (last_close - pos_avg) * pos_qty if pos_qty != 0 else 0.0
    final_equity = cash + final_unrealized
    peak = max(equity_points + [final_equity])
    dd_pct = (peak - final_equity) / peak * 100 if peak > 0 else 0.0
    return {
        "capital": round(final_equity, 2),
        "realized_pnl": round(realized, 2),
        "unrealized_pnl": round(final_unrealized, 2),
        "mark_price": round(last_close, 4),
        "drawdown_pct": round(dd_pct, 4),
        "trades_count": len(sorted_trades),
        "open_position": {
            "qty": round(pos_qty, 6),
            "avg_price": round(pos_avg, 4),
            "mark_price": round(last_close, 4),
            "unrealized_pnl": round(final_unrealized, 2),
        } if abs(pos_qty) > 1e-9 else None,
        "equity_curve": [round(x, 2) for x in equity_points + [final_equity]],
    }


def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return None


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2, default=str))


async def main():
    now_utc = datetime.now(timezone.utc)
    t0 = datetime.fromisoformat(T0_ISO.replace("Z", "+00:00"))
    t_end = t0 + timedelta(days=WINDOW_DAYS)
    until_iso = (now_utc if now_utc < t_end else t_end).strftime("%Y-%m-%dT%H:%M:%SZ")

    log.info("book_c_run_start", t0=T0_ISO, until=until_iso, tf=TIMEFRAME)
    df = await download_ohlcv(SYMBOL, T0_ISO, until_iso, TIMEFRAME)
    log.info("download_done", bars=len(df))

    if len(df) == 0:
        print(json.dumps({"status": "no_bars"}))
        return

    sup = make_supervisor()
    cfg = canonical_config()
    engine = EventEngine(sup, cfg)
    result = await engine.run(df)

    trades = serialize_trades(result.trades)
    pnl = compute_pnl(trades, INITIAL_CAPITAL, df)

    days_elapsed = (now_utc - t0).total_seconds() / 86400.0
    ret_pct = (pnl["capital"] / INITIAL_CAPITAL - 1) * 100

    state = {
        "spec": "qts-book-c-gold-15m-exploratory",
        "parent_ab": "qts-sprint5d-paper-ab-spec-20260511",
        "t0_iso": T0_ISO,
        "t_end_iso": t_end.isoformat(),
        "last_invocation_utc": now_utc.isoformat(),
        "symbol": SYMBOL,
        "timeframe": TIMEFRAME,
        "initial_capital": INITIAL_CAPITAL,
        "trade_size": TRADE_SIZE,
        "bars_available": len(df),
        "book_C": {
            "capital": pnl["capital"],
            "return_pct": round(ret_pct, 4),
            "realized_pnl": pnl["realized_pnl"],
            "unrealized_pnl": pnl["unrealized_pnl"],
            "drawdown_pct": pnl["drawdown_pct"],
            "trades_count": pnl["trades_count"],
            "open_position": pnl["open_position"],
            "mark_price": pnl["mark_price"],
            "trades": trades,
        },
        "diff": {
            "days_elapsed": round(days_elapsed, 3),
            "days_remaining": round(WINDOW_DAYS - days_elapsed, 3),
        },
        "kill_alert": pnl["drawdown_pct"] > 5.0,
        "warn_alert": pnl["drawdown_pct"] > 3.0,
    }
    save_state(state)

    date_str = now_utc.strftime("%Y%m%d-%H%M")
    snap_json = REPORTS_DIR / f"qts-paper-book-c-gold-15m-state-{date_str}.json"
    snap_json.write_text(json.dumps(state, indent=2, default=str))

    md = [
        f"# QTS Book C — Gold 15m exploratory — state @ {now_utc.strftime('%Y-%m-%d %H:%M UTC')}",
        "",
        f"- Parent A/B spec: `docs/qts-sprint5d-paper-ab-spec-20260511.md`",
        f"- T0: {T0_ISO}  ·  T_end: {t_end.isoformat()}",
        f"- Days elapsed: {state['diff']['days_elapsed']}  ·  remaining: {state['diff']['days_remaining']}",
        f"- Bars consumed: {len(df)}  ·  TF: {TIMEFRAME}",
        "",
        "## Book C (15m exploratory)",
        f"- Capital: ${pnl['capital']:,.2f}  ({ret_pct:+.4f}%)",
        f"- Realized: ${pnl['realized_pnl']:+,.2f}  ·  Unrealized: ${pnl['unrealized_pnl']:+,.2f}",
        f"- Trades: {pnl['trades_count']}  ·  Drawdown: {pnl['drawdown_pct']:.2f}%",
        f"- Open position: {pnl['open_position'] or 'flat'}",
        "",
        "## Kill / warn",
        f"- Kill threshold: DD > 5% → {'TRIGGERED' if state['kill_alert'] else 'safe'}",
        f"- Warn threshold: DD > 3% → {'TRIGGERED' if state['warn_alert'] else 'safe'}",
    ]
    snap_md = REPORTS_DIR / f"qts-paper-book-c-gold-15m-state-{date_str}.md"
    snap_md.write_text("\n".join(md))

    print(json.dumps({
        "status": "ok",
        "days_elapsed": state["diff"]["days_elapsed"],
        "book_C": {"capital": pnl["capital"], "return_pct": round(ret_pct, 4),
                    "trades": pnl["trades_count"], "dd_pct": pnl["drawdown_pct"]},
        "kill_alert": state["kill_alert"],
        "warn_alert": state["warn_alert"],
        "snap_md": str(snap_md),
        "state_file": str(STATE_FILE),
    }, indent=2, default=str))


if __name__ == "__main__":
    asyncio.run(main())
