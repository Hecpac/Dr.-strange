"""BTC/USDT EMA9/21 + RSI Backtesting — 6 months, 1h candles."""
import asyncio
import ccxt.async_support as ccxt
import numpy as np
from datetime import datetime, timedelta

async def fetch_ohlcv():
    exchange = ccxt.okx()
    since = int((datetime.utcnow() - timedelta(days=180)).timestamp() * 1000)
    all_candles = []
    while True:
        candles = await exchange.fetch_ohlcv("BTC/USDT", "1h", since=since, limit=300)
        if not candles:
            break
        all_candles.extend(candles)
        since = candles[-1][0] + 1
        if len(candles) < 300:
            break
    await exchange.close()
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

async def main():
    print("Descargando datos BTC/USDT 1h (6 meses)...")
    candles = await fetch_ohlcv()
    print(f"Candles descargados: {len(candles)}")

    close = np.array([c[4] for c in candles])
    timestamps = [c[0] for c in candles]

    ema9 = ema(close, 9)
    ema21 = ema(close, 21)
    rsi_vals = rsi(close, 14)

    COMMISSION_PCT = 0.001
    capital = 10000.0
    position = 0.0
    entry_price = 0.0
    entry_time = 0
    trades = []
    equity = [capital]

    for i in range(1, len(close)):
        prev_cross = ema9[i-1] - ema21[i-1]
        curr_cross = ema9[i] - ema21[i]

        if position == 0 and prev_cross <= 0 and curr_cross > 0 and rsi_vals[i] < 70:
            capital -= capital * COMMISSION_PCT
            position = (capital) / close[i]
            entry_price = close[i]
            entry_time = timestamps[i]

        elif position > 0 and ((prev_cross >= 0 and curr_cross < 0) or rsi_vals[i] > 80):
            exit_price = close[i]
            pnl = position * (exit_price - entry_price)
            commission = position * exit_price * COMMISSION_PCT
            capital += pnl - commission
            trades.append({
                "entry": entry_price, "exit": exit_price,
                "pnl": pnl,
                "pnl_pct": (exit_price / entry_price - 1) * 100,
                "entry_time": datetime.utcfromtimestamp(entry_time/1000).strftime("%Y-%m-%d %H:%M"),
                "exit_time": datetime.utcfromtimestamp(timestamps[i]/1000).strftime("%Y-%m-%d %H:%M"),
            })
            position = 0.0

        current_equity = capital + (position * (close[i] - entry_price) if position > 0 else 0)
        equity.append(current_equity)

    if position > 0:
        exit_price = close[-1]
        pnl = position * (exit_price - entry_price)
        commission = position * exit_price * COMMISSION_PCT
        capital += pnl - commission
        trades.append({
            "entry": entry_price, "exit": exit_price,
            "pnl": pnl, "pnl_pct": (exit_price / entry_price - 1) * 100,
            "entry_time": datetime.utcfromtimestamp(entry_time/1000).strftime("%Y-%m-%d %H:%M"),
            "exit_time": "OPEN",
        })
        position = 0.0
        equity[-1] = capital

    equity = np.array(equity)
    returns = np.diff(equity) / equity[:-1]

    sharpe = np.mean(returns) / (np.std(returns) + 1e-10) * np.sqrt(8760)
    downside = returns[returns < 0]
    sortino = np.mean(returns) / (np.std(downside) + 1e-10) * np.sqrt(8760)
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

    start_date = datetime.utcfromtimestamp(timestamps[0]/1000).strftime("%Y-%m-%d")
    end_date = datetime.utcfromtimestamp(timestamps[-1]/1000).strftime("%Y-%m-%d")

    print(f"""
{'='*60}
  BACKTESTING REPORT — EMA 9/21 + RSI
{'='*60}
  Par:        BTC/USDT
  Timeframe:  1H
  Periodo:    {start_date} -> {end_date}
  Candles:    {len(candles):,}
{'='*60}
  ESTRATEGIA
  Entry: EMA9 cruza sobre EMA21 + RSI < 70
  Exit:  EMA9 cruza bajo EMA21 OR RSI > 80
{'='*60}
  RESULTADOS
  Capital inicial:   $10,000.00
  Capital final:     ${equity[-1]:>10,.2f}
  Retorno:           {strat_return:>+.2f}%
{'='*60}
  METRICAS
  Sharpe Ratio:      {sharpe:.3f}
  Sortino Ratio:     {sortino:.3f}
  Max Drawdown:      {max_dd:.2f}%
  Win Rate:          {win_rate:.1f}%
  Profit Factor:     {profit_factor:.3f}
  Total Trades:      {len(trades)}
  Wins / Losses:     {len(wins)} / {len(losses_t)}
{'='*60}
  BUY & HOLD
  BTC inicio:        ${close[0]:>10,.2f}
  BTC final:         ${close[-1]:>10,.2f}
  Retorno B&H:       {bh_return:>+.2f}%
{'='*60}
  ESTRATEGIA vs B&H: {strat_return - bh_return:>+.2f} pp
{'='*60}
""")

    print(f"{'Entry Time':<18} {'Exit Time':<18} {'Entry $':>10} {'Exit $':>10} {'P&L $':>10} {'P&L %':>8}")
    print("-" * 80)
    for t in trades[-10:]:
        print(f"{t['entry_time']:<18} {t['exit_time']:<18} {t['entry']:>10,.2f} {t['exit']:>10,.2f} {t['pnl']:>+10,.2f} {t['pnl_pct']:>+7.2f}%")

asyncio.run(main())
