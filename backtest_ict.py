"""ICT ATR Displacement + Hurst Filter + Kill Zone backtest — from wiki knowledge base."""
import asyncio
import ccxt.async_support as ccxt
import numpy as np
from datetime import datetime, timedelta, timezone

MAX_PORTFOLIO_DD = 0.05
ATR_PERIOD = 14
HURST_WINDOW = 100

LONDON_KZ = (7, 10)   # UTC hours (2-5 AM EST = 7-10 UTC)
NY_KZ = (12, 15)      # UTC hours (7-10 AM EST = 12-15 UTC)

ASSETS = [
    {"symbol": "BTC/USDT", "exchange": "okx", "name": "Bitcoin",
     "atk_k": 1.5, "hurst_min": 0.50, "pos_pct": 0.10, "sl": 0.025, "trail": 0.015,
     "use_hurst": True, "kz": "both"},
    {"symbol": "XAUT/USDT", "exchange": "okx", "name": "Oro (XAUT/USDT)",
     "atk_k": 2.0, "hurst_min": 0.48, "pos_pct": 0.10, "sl": 0.025, "trail": 0.015,
     "use_hurst": False, "kz": "ny_only"},
    {"symbol": "DOGE/USDT", "exchange": "okx", "name": "NASDAQ Proxy (DOGE)",
     "atk_k": 1.5, "hurst_min": 0.55, "pos_pct": 0.10, "sl": 0.025, "trail": 0.015,
     "use_hurst": True, "kz": "both"},
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


def atr(high, low, close, period=14):
    tr = np.zeros(len(close))
    tr[0] = high[0] - low[0]
    for i in range(1, len(close)):
        tr[i] = max(high[i] - low[i], abs(high[i] - close[i-1]), abs(low[i] - close[i-1]))
    result = np.zeros(len(close))
    result[period-1] = np.mean(tr[:period])
    for i in range(period, len(close)):
        result[i] = (result[i-1] * (period - 1) + tr[i]) / period
    return result


def hurst_rs(close, window=100):
    """Rolling Hurst exponent via Rescaled Range (R/S) method."""
    result = np.full(len(close), 0.5)
    log_returns = np.diff(np.log(close))
    for i in range(window, len(log_returns)):
        seg = log_returns[i-window:i]
        mean = np.mean(seg)
        devs = seg - mean
        cumdev = np.cumsum(devs)
        R = np.max(cumdev) - np.min(cumdev)
        S = np.std(seg, ddof=1)
        if S > 1e-12 and R > 1e-12:
            result[i+1] = np.log(R / S) / np.log(window)
    return result


def is_killzone(ts_ms, kz_mode="both"):
    hour = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).hour
    if kz_mode == "ny_only":
        return NY_KZ[0] <= hour < NY_KZ[1]
    return (LONDON_KZ[0] <= hour < LONDON_KZ[1]) or (NY_KZ[0] <= hour < NY_KZ[1])


def run_backtest(opn, high, low, close, timestamps, label, cfg):
    ATR_K = cfg["atk_k"]
    HURST_MIN = cfg["hurst_min"]
    POSITION_SIZE_PCT = cfg["pos_pct"]
    STOP_LOSS_PCT = cfg["sl"]
    TRAILING_STOP_PCT = cfg["trail"]
    use_hurst = cfg.get("use_hurst", True)
    kz_mode = cfg.get("kz", "both")

    atr_vals = atr(high, low, close, ATR_PERIOD)
    hurst_vals = hurst_rs(close, HURST_WINDOW)

    COMMISSION_PCT = 0.001
    capital = 10000.0
    peak_equity = capital
    position = 0.0
    direction = 0
    entry_price = 0.0
    entry_time = 0
    highest_since_entry = 0.0
    lowest_since_entry = 0.0
    trades = []
    equity = [capital]
    stop_hits = 0
    trailing_hits = 0
    circuit_breaks = 0
    hurst_exits = 0

    warmup = max(ATR_PERIOD, HURST_WINDOW + 1)

    for i in range(warmup, len(close)):
        body = abs(opn[i] - close[i])
        is_displacement = body > ATR_K * atr_vals[i-1] and atr_vals[i-1] > 0
        bullish = close[i] > opn[i]
        bearish = close[i] < opn[i]
        in_kz = is_killzone(timestamps[i], kz_mode)
        h = hurst_vals[i]
        trending = h > HURST_MIN if use_hurst else True

        current_equity = capital + (
            position * (close[i] - entry_price) * direction if position > 0 else 0
        )
        if position == 0:
            peak_equity = max(peak_equity, capital)
        equity_dd = (current_equity - peak_equity) / peak_equity

        if position > 0:
            highest_since_entry = max(highest_since_entry, close[i])
            lowest_since_entry = min(lowest_since_entry, close[i])

        # Circuit breaker
        if position > 0 and equity_dd <= -MAX_PORTFOLIO_DD:
            exit_price = close[i]
            pnl = position * (exit_price - entry_price) * direction
            commission = position * exit_price * COMMISSION_PCT
            capital += pnl - commission
            circuit_breaks += 1
            trades.append({
                "entry": entry_price, "exit": exit_price,
                "pnl": pnl, "pnl_pct": ((exit_price / entry_price - 1) * direction) * 100,
                "reason": "CIRCUIT", "dir": "L" if direction == 1 else "S",
                "entry_time": datetime.fromtimestamp(entry_time/1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M"),
                "exit_time": datetime.fromtimestamp(timestamps[i]/1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M"),
            })
            position = 0.0
            direction = 0
            equity.append(capital)
            continue

        # Stop-loss / trailing
        if position > 0:
            if direction == 1:
                pnl_pct = close[i] / entry_price - 1
                trail_ref = highest_since_entry
                trailing_pct = close[i] / trail_ref - 1
            else:
                pnl_pct = entry_price / close[i] - 1
                trail_ref = lowest_since_entry
                trailing_pct = trail_ref / close[i] - 1

            hit_hard = pnl_pct <= -STOP_LOSS_PCT
            hit_trail = trailing_pct <= -TRAILING_STOP_PCT

            if hit_hard or hit_trail:
                exit_price = close[i]
                pnl = position * (exit_price - entry_price) * direction
                commission = position * exit_price * COMMISSION_PCT
                capital += pnl - commission
                reason = "STOP" if hit_hard else "TRAIL"
                if hit_hard:
                    stop_hits += 1
                else:
                    trailing_hits += 1
                trades.append({
                    "entry": entry_price, "exit": exit_price,
                    "pnl": pnl, "pnl_pct": ((exit_price / entry_price - 1) * direction) * 100,
                    "reason": reason, "dir": "L" if direction == 1 else "S",
                    "entry_time": datetime.fromtimestamp(entry_time/1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M"),
                    "exit_time": datetime.fromtimestamp(timestamps[i]/1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M"),
                })
                position = 0.0
                direction = 0
                equity.append(capital)
                continue

        # Hurst regime exit
        if position > 0 and use_hurst and h < 0.48:
            exit_price = close[i]
            pnl = position * (exit_price - entry_price) * direction
            commission = position * exit_price * COMMISSION_PCT
            capital += pnl - commission
            hurst_exits += 1
            trades.append({
                "entry": entry_price, "exit": exit_price,
                "pnl": pnl, "pnl_pct": ((exit_price / entry_price - 1) * direction) * 100,
                "reason": "HURST", "dir": "L" if direction == 1 else "S",
                "entry_time": datetime.fromtimestamp(entry_time/1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M"),
                "exit_time": datetime.fromtimestamp(timestamps[i]/1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M"),
            })
            position = 0.0
            direction = 0

        # Opposite displacement exit
        if position > 0 and is_displacement:
            should_exit = (direction == 1 and bearish) or (direction == -1 and bullish)
            if should_exit:
                exit_price = close[i]
                pnl = position * (exit_price - entry_price) * direction
                commission = position * exit_price * COMMISSION_PCT
                capital += pnl - commission
                trades.append({
                    "entry": entry_price, "exit": exit_price,
                    "pnl": pnl, "pnl_pct": ((exit_price / entry_price - 1) * direction) * 100,
                    "reason": "DISP", "dir": "L" if direction == 1 else "S",
                    "entry_time": datetime.fromtimestamp(entry_time/1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M"),
                    "exit_time": datetime.fromtimestamp(timestamps[i]/1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M"),
                })
                position = 0.0
                direction = 0

        # Entry: displacement + trending + kill zone
        if position == 0 and is_displacement and trending and in_kz and equity_dd > -MAX_PORTFOLIO_DD:
            entry_price = close[i]
            entry_time = timestamps[i]
            if bullish:
                capital -= capital * POSITION_SIZE_PCT * COMMISSION_PCT
                direction = 1
                position = (capital * POSITION_SIZE_PCT) / close[i]
                highest_since_entry = close[i]
                lowest_since_entry = close[i]
            elif bearish:
                capital -= capital * POSITION_SIZE_PCT * COMMISSION_PCT
                direction = -1
                position = (capital * POSITION_SIZE_PCT) / close[i]
                highest_since_entry = close[i]
                lowest_since_entry = close[i]

        current_equity = capital + (
            position * (close[i] - entry_price) * direction if position > 0 else 0
        )
        equity.append(current_equity)

    # Close open position
    if position > 0:
        exit_price = close[-1]
        pnl = position * (exit_price - entry_price) * direction
        commission = position * exit_price * COMMISSION_PCT
        capital += pnl - commission
        trades.append({
            "entry": entry_price, "exit": exit_price,
            "pnl": pnl, "pnl_pct": ((exit_price / entry_price - 1) * direction) * 100,
            "reason": "OPEN", "dir": "L" if direction == 1 else "S",
            "entry_time": datetime.fromtimestamp(entry_time/1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M"),
            "exit_time": "OPEN",
        })
        equity[-1] = capital

    equity = np.array(equity)
    returns = np.diff(equity) / (equity[:-1] + 1e-10)

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

    longs = len([t for t in trades if t["dir"] == "L"])
    shorts = len([t for t in trades if t["dir"] == "S"])
    disp_exits = len([t for t in trades if t["reason"] == "DISP"])

    print(f"""
{'='*65}
  {label} — ICT: ATR Displacement + Hurst + KillZone
{'='*65}
  Periodo:  {start_date} -> {end_date}  |  {len(close):,} candles
  ATR k:    {ATR_K}  |  Hurst: {"H>"+str(HURST_MIN) if use_hurst else "OFF"}  |  KZ: {kz_mode}
  SL: {STOP_LOSS_PCT*100:.1f}%  |  Trail: {TRAILING_STOP_PCT*100:.1f}%  |  Size: {POSITION_SIZE_PCT*100:.0f}%
{'='*65}
  Capital inicial:   $10,000.00
  Capital final:     ${equity[-1]:>10,.2f}
  Retorno estrategia:{strat_return:>+8.2f}%
  Retorno B&H:       {bh_return:>+8.2f}%
  Alpha vs B&H:      {strat_return - bh_return:>+8.2f} pp
{'-'*65}
  Sharpe:  {sharpe:>7.3f}   |  Sortino: {sortino:>7.3f}
  Max DD:  {max_dd:>7.2f}%  |  Win Rate: {win_rate:>5.1f}%
  PF:      {profit_factor:>7.3f}   |  Trades:  {len(trades)}  (L:{longs} S:{shorts})
  Wins:    {len(wins):>4}     |  Losses:  {len(losses_t)}
  Stops:   {stop_hits:>4}     |  Trail:   {trailing_hits}
  Hurst:   {hurst_exits:>4}     |  Disp:    {disp_exits}
  Circuit: {circuit_breaks:>4}
{'='*65}""")

    if trades:
        print(f"\n  Ultimos 5 trades:")
        print(f"  {'Dir':<3} {'Entry':<16} {'Exit':<16} {'$Entry':>9} {'$Exit':>9} {'P&L':>9} {'%':>7} {'Razon'}")
        for t in trades[-5:]:
            print(f"  {t['dir']:<3} {t['entry_time']:<16} {t['exit_time']:<16} {t['entry']:>9,.2f} {t['exit']:>9,.2f} {t['pnl']:>+9,.2f} {t['pnl_pct']:>+6.2f}% {t['reason']}")
    else:
        print("\n  Sin trades en el periodo.")
    print()


async def main():
    for asset in ASSETS:
        print(f"\nDescargando {asset['name']} ({asset['symbol']})...")
        try:
            candles = await fetch_ohlcv(asset["exchange"], asset["symbol"])
            if len(candles) < HURST_WINDOW + 50:
                print(f"  Solo {len(candles)} candles — insuficiente, saltando.")
                continue
            print(f"  {len(candles)} candles descargados")
            opn = np.array([c[1] for c in candles])
            high = np.array([c[2] for c in candles])
            low = np.array([c[3] for c in candles])
            close = np.array([c[4] for c in candles])
            timestamps = [c[0] for c in candles]
            cfg = {k: asset[k] for k in ("atk_k", "hurst_min", "pos_pct", "sl", "trail", "use_hurst", "kz") if k in asset}
            run_backtest(opn, high, low, close, timestamps, f"{asset['name']} ({asset['symbol']})", cfg)
        except Exception as e:
            import traceback
            print(f"  Error: {e}")
            traceback.print_exc()

asyncio.run(main())
