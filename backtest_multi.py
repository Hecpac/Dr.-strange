"""Multi-asset EMA9/21 + RSI backtest with stop-loss — BTC, XAU, NASDAQ proxy."""
import asyncio
import ccxt.async_support as ccxt
import numpy as np
from datetime import datetime, timedelta, timezone

STOP_LOSS_PCT = 0.025     # 2.5% hard stop-loss
TRAILING_STOP_PCT = 0.015 # 1.5% trailing stop
POSITION_SIZE_PCT = 0.05  # 5% of capital per trade
MAX_PORTFOLIO_DD = 0.05   # 5% equity circuit breaker
COMMISSION_PCT = 0.001    # 0.1% per trade (entry + exit)

ASSETS = [
    {"symbol": "BTC/USDT", "exchange": "okx", "name": "Bitcoin"},
    {"symbol": "XAUT/USDT", "exchange": "okx", "name": "Oro (XAUT/USDT)"},
    {"symbol": "DOGE/USDT", "exchange": "okx", "name": "NASDAQ Proxy (DOGE)"},
]

async def fetch_ohlcv(exchange_id, symbol):
    ex = getattr(ccxt, exchange_id)()
    since = int((datetime.now(timezone.utc) - timedelta(days=180)).timestamp() * 1000)
    all_candles = []
    try:
        while True:
            candles = await ex.fetch_ohlcv(symbol, "1h", since=since, limit=300)
            if not candles:
                break
            all_candles.extend(candles)
            since = candles[-1][0] + 1
            if len(candles) < 300:
                break
    finally:
        await ex.close()
    return all_candles

def ema(data, period):
    result = np.zeros_like(data)
    k = 2.0 / (period + 1)
    result[0] = data[0]
    for i in range(1, len(data)):
        result[i] = data[i] * k + result[i-1] * (1 - k)
    return result

def rsi(close, period=14):
    deltas = np.diff(close)
    gains = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)
    avg_gain = np.zeros(len(close))
    avg_loss = np.zeros(len(close))
    avg_gain[period] = np.mean(gains[:period])
    avg_loss[period] = np.mean(losses[:period])
    for i in range(period + 1, len(close)):
        avg_gain[i] = (avg_gain[i-1] * (period - 1) + gains[i-1]) / period
        avg_loss[i] = (avg_loss[i-1] * (period - 1) + losses[i-1]) / period
    rs = np.divide(avg_gain, avg_loss, out=np.zeros_like(avg_gain), where=avg_loss != 0)
    rsi_vals = 100 - (100 / (1 + rs))
    rsi_vals[:period] = 50
    return rsi_vals

def run_backtest(close, timestamps, label):
    ema9 = ema(close, 9)
    ema21 = ema(close, 21)
    rsi_vals = rsi(close, 14)

    capital = 10000.0
    peak_equity = capital
    position = 0.0
    entry_price = 0.0
    entry_time = 0
    highest_since_entry = 0.0
    trades = []
    equity = [capital]
    stop_hits = 0
    trailing_hits = 0
    circuit_breaks = 0

    for i in range(1, len(close)):
        prev_cross = ema9[i-1] - ema21[i-1]
        curr_cross = ema9[i] - ema21[i]

        current_equity = capital + (position * (close[i] - entry_price) if position > 0 else 0)
        if position == 0:
            peak_equity = max(peak_equity, capital)
        equity_dd = (current_equity - peak_equity) / peak_equity

        if position > 0:
            highest_since_entry = max(highest_since_entry, close[i])

        if position > 0 and equity_dd <= -MAX_PORTFOLIO_DD:
            exit_price = close[i]
            pnl = position * (exit_price - entry_price)
            commission = position * exit_price * COMMISSION_PCT
            capital += pnl - commission
            circuit_breaks += 1
            trades.append({
                "entry": entry_price, "exit": exit_price,
                "pnl": pnl, "pnl_pct": (exit_price / entry_price - 1) * 100,
                "reason": "CIRCUIT",
                "entry_time": datetime.fromtimestamp(entry_time/1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M"),
                "exit_time": datetime.fromtimestamp(timestamps[i]/1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M"),
            })
            position = 0.0
            equity.append(capital)
            continue

        # Check stop-loss
        if position > 0:
            pnl_pct = (close[i] / entry_price - 1)
            trailing_pct = (close[i] / highest_since_entry - 1)

            hit_hard_stop = pnl_pct <= -STOP_LOSS_PCT
            hit_trailing = trailing_pct <= -TRAILING_STOP_PCT

            if hit_hard_stop or hit_trailing:
                exit_price = close[i]
                pnl = position * (exit_price - entry_price)
                commission = position * exit_price * COMMISSION_PCT
                capital += pnl - commission
                reason = "STOP-LOSS" if hit_hard_stop else "TRAILING"
                if hit_hard_stop:
                    stop_hits += 1
                else:
                    trailing_hits += 1
                trades.append({
                    "entry": entry_price, "exit": exit_price,
                    "pnl": pnl, "pnl_pct": (exit_price / entry_price - 1) * 100,
                    "reason": reason,
                    "entry_time": datetime.fromtimestamp(entry_time/1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M"),
                    "exit_time": datetime.fromtimestamp(timestamps[i]/1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M"),
                })
                position = 0.0
                equity.append(capital)
                continue

        # Entry: EMA9 crosses above EMA21 + RSI < 70
        if position == 0 and prev_cross <= 0 and curr_cross > 0 and rsi_vals[i] < 70 and equity_dd > -MAX_PORTFOLIO_DD:
            capital -= capital * POSITION_SIZE_PCT * COMMISSION_PCT
            position = (capital * POSITION_SIZE_PCT) / close[i]
            entry_price = close[i]
            entry_time = timestamps[i]
            highest_since_entry = close[i]

        # Exit: EMA9 crosses below EMA21 OR RSI > 80
        elif position > 0 and ((prev_cross >= 0 and curr_cross < 0) or rsi_vals[i] > 80):
            exit_price = close[i]
            pnl = position * (exit_price - entry_price)
            commission = position * exit_price * COMMISSION_PCT
            capital += pnl - commission
            trades.append({
                "entry": entry_price, "exit": exit_price,
                "pnl": pnl, "pnl_pct": (exit_price / entry_price - 1) * 100,
                "reason": "SIGNAL",
                "entry_time": datetime.fromtimestamp(entry_time/1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M"),
                "exit_time": datetime.fromtimestamp(timestamps[i]/1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M"),
            })
            position = 0.0

        current_equity = capital + (position * (close[i] - entry_price) if position > 0 else 0)
        equity.append(current_equity)

    # Close open position
    if position > 0:
        exit_price = close[-1]
        pnl = position * (exit_price - entry_price)
        commission = position * exit_price * COMMISSION_PCT
        capital += pnl - commission
        trades.append({
            "entry": entry_price, "exit": exit_price,
            "pnl": pnl, "pnl_pct": (exit_price / entry_price - 1) * 100,
            "reason": "OPEN",
            "entry_time": datetime.fromtimestamp(entry_time/1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M"),
            "exit_time": "OPEN",
        })
        equity[-1] = capital

    equity = np.array(equity)
    returns = np.diff(equity) / equity[:-1]

    sharpe = np.mean(returns) / (np.std(returns) + 1e-10) * np.sqrt(8760)
    downside = returns[returns < 0]
    sortino = np.mean(returns) / (np.std(downside) + 1e-10) * np.sqrt(8760) if len(downside) > 0 else 0
    peak = np.maximum.accumulate(equity)
    drawdown = (equity - peak) / peak
    max_dd = np.min(drawdown) * 100

    wins = [t for t in trades if t["pnl"] > 0]
    losses_t = [t for t in trades if t["pnl"] <= 0]
    win_rate = len(wins) / len(trades) * 100 if trades else 0
    gross_profit = sum(t["pnl"] for t in wins) if wins else 0
    gross_loss = abs(sum(t["pnl"] for t in losses_t)) if losses_t else 1
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else float('inf')

    bh_return = (close[-1] / close[0] - 1) * 100
    strat_return = (equity[-1] / 10000 - 1) * 100

    start_date = datetime.fromtimestamp(timestamps[0]/1000, tz=timezone.utc).strftime("%Y-%m-%d")
    end_date = datetime.fromtimestamp(timestamps[-1]/1000, tz=timezone.utc).strftime("%Y-%m-%d")

    signal_exits = len([t for t in trades if t["reason"] == "SIGNAL"])

    print(f"""
{'='*60}
  {label} — EMA 9/21 + RSI + STOPS
{'='*60}
  Periodo:    {start_date} -> {end_date}  |  {len(close):,} candles
  Stop-Loss:  {STOP_LOSS_PCT*100:.1f}% hard  |  {TRAILING_STOP_PCT*100:.1f}% trail  |  {MAX_PORTFOLIO_DD*100:.0f}% circuit
{'='*60}
  Capital inicial:   $10,000.00
  Capital final:     ${equity[-1]:>10,.2f}
  Retorno estrategia:{strat_return:>+8.2f}%
  Retorno B&H:       {bh_return:>+8.2f}%
  Alpha vs B&H:      {strat_return - bh_return:>+8.2f} pp
{'-'*60}
  Sharpe:  {sharpe:>7.3f}   |  Sortino: {sortino:>7.3f}
  Max DD:  {max_dd:>7.2f}%  |  Win Rate: {win_rate:>5.1f}%
  PF:      {profit_factor:>7.3f}   |  Trades:  {len(trades)}
  Wins:    {len(wins):>4}     |  Losses:  {len(losses_t)}
  Stops:   {stop_hits:>4}     |  Trailing: {trailing_hits}
  Circuit: {circuit_breaks:>4}     |  Signals:  {signal_exits}
{'='*60}""")

    print(f"\n  Ultimos 5 trades:")
    print(f"  {'Entry':<16} {'Exit':<16} {'$Entry':>9} {'$Exit':>9} {'P&L':>9} {'%':>7} {'Razon'}")
    for t in trades[-5:]:
        print(f"  {t['entry_time']:<16} {t['exit_time']:<16} {t['entry']:>9,.2f} {t['exit']:>9,.2f} {t['pnl']:>+9,.2f} {t['pnl_pct']:>+6.2f}% {t['reason']}")
    print()

async def main():
    for asset in ASSETS:
        print(f"\nDescargando {asset['name']} ({asset['symbol']})...")
        try:
            candles = await fetch_ohlcv(asset["exchange"], asset["symbol"])
            if len(candles) < 100:
                print(f"  Solo {len(candles)} candles — insuficiente, saltando.")
                continue
            print(f"  {len(candles)} candles descargados")
            close = np.array([c[4] for c in candles])
            timestamps = [c[0] for c in candles]
            run_backtest(close, timestamps, f"{asset['name']} ({asset['symbol']})")
        except Exception as e:
            print(f"  Error: {e}")

asyncio.run(main())
