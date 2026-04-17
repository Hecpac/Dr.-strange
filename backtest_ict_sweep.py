"""ICT strategy parameter sweep + weakness analysis."""
import asyncio
import ccxt.async_support as ccxt
import numpy as np
from datetime import datetime, timedelta, timezone
from collections import defaultdict

ASSETS = [
    {"symbol": "BTC/USDT", "exchange": "okx", "name": "BTC"},
    {"symbol": "XAUT/USDT", "exchange": "okx", "name": "Gold"},
    {"symbol": "DOGE/USDT", "exchange": "okx", "name": "DOGE"},
]

LONDON_KZ = (7, 10)
NY_KZ = (12, 15)


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


def is_killzone(ts_ms):
    hour = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).hour
    return (LONDON_KZ[0] <= hour < LONDON_KZ[1]) or (NY_KZ[0] <= hour < NY_KZ[1])


def which_kz(ts_ms):
    hour = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).hour
    if LONDON_KZ[0] <= hour < LONDON_KZ[1]:
        return "London"
    if NY_KZ[0] <= hour < NY_KZ[1]:
        return "NY"
    return "None"


def run_backtest(opn, high, low, close, timestamps, params, collect_trades=False):
    atk_k = params["atk_k"]
    hurst_min = params["hurst_min"]
    pos_pct = params["pos_pct"]
    sl = params["sl"]
    trail = params["trail"]
    long_only = params.get("long_only", False)
    no_kz = params.get("no_kz", False)
    no_hurst = params.get("no_hurst", False)

    atr_vals = atr(high, low, close, 14)
    hurst_vals = hurst_rs(close, 100)

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

    warmup = 101

    for i in range(warmup, len(close)):
        body = abs(opn[i] - close[i])
        is_disp = body > atk_k * atr_vals[i-1] and atr_vals[i-1] > 0
        bullish = close[i] > opn[i]
        bearish = close[i] < opn[i]
        in_kz = is_killzone(timestamps[i]) if not no_kz else True
        h = hurst_vals[i]
        trending = h > hurst_min if not no_hurst else True

        cur_eq = capital + (position * (close[i] - entry_price) * direction if position > 0 else 0)
        if position == 0:
            peak_equity = max(peak_equity, capital)
        eq_dd = (cur_eq - peak_equity) / peak_equity

        if position > 0:
            highest_since_entry = max(highest_since_entry, close[i])
            lowest_since_entry = min(lowest_since_entry, close[i])

        # Circuit breaker
        if position > 0 and eq_dd <= -0.05:
            pnl = position * (close[i] - entry_price) * direction
            commission = position * close[i] * COMMISSION_PCT
            capital += pnl - commission
            trades.append({"pnl": pnl, "pnl_pct": ((close[i]/entry_price-1)*direction)*100,
                           "reason": "CIRCUIT", "dir": direction, "kz": which_kz(entry_time),
                           "entry_time": entry_time, "exit_time": timestamps[i],
                           "hurst_at_entry": h, "entry_price": entry_price, "exit_price": close[i]})
            position = 0.0; direction = 0
            equity.append(capital); continue

        # Stops
        if position > 0:
            if direction == 1:
                pnl_pct = close[i] / entry_price - 1
                trailing_pct = close[i] / highest_since_entry - 1
            else:
                pnl_pct = entry_price / close[i] - 1
                trailing_pct = lowest_since_entry / close[i] - 1

            if pnl_pct <= -sl or trailing_pct <= -trail:
                reason = "STOP" if pnl_pct <= -sl else "TRAIL"
                pnl = position * (close[i] - entry_price) * direction
                commission = position * close[i] * COMMISSION_PCT
                capital += pnl - commission
                trades.append({"pnl": pnl, "pnl_pct": ((close[i]/entry_price-1)*direction)*100,
                               "reason": reason, "dir": direction, "kz": which_kz(entry_time),
                               "entry_time": entry_time, "exit_time": timestamps[i],
                               "hurst_at_entry": h, "entry_price": entry_price, "exit_price": close[i]})
                position = 0.0; direction = 0
                equity.append(capital); continue

        # Hurst exit
        if position > 0 and h < 0.48 and not no_hurst:
            pnl = position * (close[i] - entry_price) * direction
            commission = position * close[i] * COMMISSION_PCT
            capital += pnl - commission
            trades.append({"pnl": pnl, "pnl_pct": ((close[i]/entry_price-1)*direction)*100,
                           "reason": "HURST", "dir": direction, "kz": which_kz(entry_time),
                           "entry_time": entry_time, "exit_time": timestamps[i],
                           "hurst_at_entry": h, "entry_price": entry_price, "exit_price": close[i]})
            position = 0.0; direction = 0

        # Opposite displacement exit
        if position > 0 and is_disp:
            if (direction == 1 and bearish) or (direction == -1 and bullish):
                pnl = position * (close[i] - entry_price) * direction
                commission = position * close[i] * COMMISSION_PCT
                capital += pnl - commission
                trades.append({"pnl": pnl, "pnl_pct": ((close[i]/entry_price-1)*direction)*100,
                               "reason": "DISP", "dir": direction, "kz": which_kz(entry_time),
                               "entry_time": entry_time, "exit_time": timestamps[i],
                               "hurst_at_entry": h, "entry_price": entry_price, "exit_price": close[i]})
                position = 0.0; direction = 0

        # Entry
        if position == 0 and is_disp and trending and in_kz and eq_dd > -0.05:
            if bullish:
                capital -= capital * pos_pct * COMMISSION_PCT
                direction = 1
                entry_price = close[i]; entry_time = timestamps[i]
                position = (capital * pos_pct) / close[i]
                highest_since_entry = close[i]; lowest_since_entry = close[i]
            elif bearish and not long_only:
                capital -= capital * pos_pct * COMMISSION_PCT
                direction = -1
                entry_price = close[i]; entry_time = timestamps[i]
                position = (capital * pos_pct) / close[i]
                highest_since_entry = close[i]; lowest_since_entry = close[i]

        cur_eq = capital + (position * (close[i] - entry_price) * direction if position > 0 else 0)
        equity.append(cur_eq)

    if position > 0:
        pnl = position * (close[-1] - entry_price) * direction
        commission = position * close[-1] * COMMISSION_PCT
        capital += pnl - commission
        equity[-1] = capital

    equity = np.array(equity)
    peak = np.maximum.accumulate(equity)
    dd = (equity - peak) / peak
    max_dd = np.min(dd) * 100

    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    wr = len(wins) / len(trades) * 100 if trades else 0
    gp = sum(t["pnl"] for t in wins) if wins else 0
    gl = abs(sum(t["pnl"] for t in losses)) if losses else 1
    pf = gp / gl if gl > 0 else float('inf')
    ret = (equity[-1] / 10000 - 1) * 100

    returns = np.diff(equity) / (equity[:-1] + 1e-10)
    sharpe = np.mean(returns) / (np.std(returns) + 1e-10) * np.sqrt(8760)

    return {
        "ret": ret, "dd": max_dd, "sharpe": sharpe, "pf": pf,
        "wr": wr, "trades": len(trades), "wins": len(wins),
        "longs": len([t for t in trades if t["dir"] == 1]),
        "shorts": len([t for t in trades if t["dir"] == -1]),
        "all_trades": trades if collect_trades else None,
    }


async def main():
    all_data = {}
    for asset in ASSETS:
        print(f"Descargando {asset['name']}...")
        candles = await fetch_ohlcv(asset["exchange"], asset["symbol"])
        print(f"  {len(candles)} candles")
        all_data[asset["name"]] = {
            "opn": np.array([c[1] for c in candles]),
            "high": np.array([c[2] for c in candles]),
            "low": np.array([c[3] for c in candles]),
            "close": np.array([c[4] for c in candles]),
            "ts": [c[0] for c in candles],
        }

    # ── 1. Parameter sweep ──
    print(f"\n{'='*75}")
    print(f"  PARAMETER SWEEP")
    print(f"{'='*75}")
    print(f"  {'Asset':<6} {'k':>4} {'H_min':>5} {'Pos%':>5} {'Ret%':>7} {'MaxDD%':>7} {'Sharpe':>7} {'PF':>6} {'WR%':>5} {'#Tr':>4}")
    print(f"  {'-'*70}")

    for name, d in all_data.items():
        for k in [1.0, 1.5, 2.0]:
            for h_min in [0.48, 0.50, 0.52, 0.55]:
                for pos in [0.10, 0.15, 0.20]:
                    r = run_backtest(d["opn"], d["high"], d["low"], d["close"], d["ts"],
                                     {"atk_k": k, "hurst_min": h_min, "pos_pct": pos,
                                      "sl": 0.025, "trail": 0.015})
                    if r["trades"] >= 5:
                        print(f"  {name:<6} {k:>4.1f} {h_min:>5.2f} {pos*100:>4.0f}% {r['ret']:>+7.2f} {r['dd']:>7.2f} {r['sharpe']:>7.3f} {r['pf']:>6.3f} {r['wr']:>5.1f} {r['trades']:>4}")

    # ── 2. Ablation study — what does each filter add? ──
    print(f"\n{'='*75}")
    print(f"  ABLATION STUDY (k=1.5, H>0.50, 15%)")
    print(f"{'='*75}")
    base = {"atk_k": 1.5, "hurst_min": 0.50, "pos_pct": 0.15, "sl": 0.025, "trail": 0.015}

    for name, d in all_data.items():
        full = run_backtest(d["opn"], d["high"], d["low"], d["close"], d["ts"], base)
        no_h = run_backtest(d["opn"], d["high"], d["low"], d["close"], d["ts"], {**base, "no_hurst": True})
        no_k = run_backtest(d["opn"], d["high"], d["low"], d["close"], d["ts"], {**base, "no_kz": True})
        lo = run_backtest(d["opn"], d["high"], d["low"], d["close"], d["ts"], {**base, "long_only": True})

        print(f"\n  {name}:")
        print(f"  {'Config':<20} {'Ret%':>7} {'MaxDD%':>7} {'PF':>6} {'WR%':>5} {'#Tr':>4}")
        print(f"  {'Full ICT':<20} {full['ret']:>+7.2f} {full['dd']:>7.2f} {full['pf']:>6.3f} {full['wr']:>5.1f} {full['trades']:>4}")
        print(f"  {'No Hurst filter':<20} {no_h['ret']:>+7.2f} {no_h['dd']:>7.2f} {no_h['pf']:>6.3f} {no_h['wr']:>5.1f} {no_h['trades']:>4}")
        print(f"  {'No Kill Zone':<20} {no_k['ret']:>+7.2f} {no_k['dd']:>7.2f} {no_k['pf']:>6.3f} {no_k['wr']:>5.1f} {no_k['trades']:>4}")
        print(f"  {'Long Only':<20} {lo['ret']:>+7.2f} {lo['dd']:>7.2f} {lo['pf']:>6.3f} {lo['wr']:>5.1f} {lo['trades']:>4}")

    # ── 3. Trade-level weakness analysis (best config) ──
    print(f"\n{'='*75}")
    print(f"  WEAKNESS ANALYSIS — Trade-Level Breakdown")
    print(f"{'='*75}")

    for name, d in all_data.items():
        r = run_backtest(d["opn"], d["high"], d["low"], d["close"], d["ts"],
                         {**base}, collect_trades=True)
        trades = r["all_trades"]
        if not trades:
            continue

        print(f"\n  {name} ({len(trades)} trades):")

        # By direction
        for dir_label, dir_val in [("Long", 1), ("Short", -1)]:
            dt = [t for t in trades if t["dir"] == dir_val]
            if not dt:
                continue
            w = len([t for t in dt if t["pnl"] > 0])
            avg_pnl = np.mean([t["pnl_pct"] for t in dt])
            print(f"    {dir_label}: {len(dt)} trades, WR {w/len(dt)*100:.0f}%, avg P&L {avg_pnl:+.2f}%")

        # By kill zone
        for kz in ["London", "NY"]:
            dt = [t for t in trades if t["kz"] == kz]
            if not dt:
                continue
            w = len([t for t in dt if t["pnl"] > 0])
            avg_pnl = np.mean([t["pnl_pct"] for t in dt])
            print(f"    {kz}: {len(dt)} trades, WR {w/len(dt)*100:.0f}%, avg P&L {avg_pnl:+.2f}%")

        # By exit reason
        reasons = defaultdict(list)
        for t in trades:
            reasons[t["reason"]].append(t["pnl_pct"])
        print(f"    Exit reasons:")
        for reason, pnls in sorted(reasons.items()):
            avg = np.mean(pnls)
            print(f"      {reason}: {len(pnls)} trades, avg {avg:+.2f}%")

        # Worst 3 trades
        worst = sorted(trades, key=lambda t: t["pnl_pct"])[:3]
        print(f"    Worst trades:")
        for t in worst:
            et = datetime.fromtimestamp(t["entry_time"]/1000, tz=timezone.utc).strftime("%m-%d %H:%M")
            d_label = "L" if t["dir"] == 1 else "S"
            print(f"      {et} {d_label} {t['pnl_pct']:+.2f}% exit:{t['reason']} kz:{t['kz']}")

        # Consecutive losses
        max_streak = 0; cur_streak = 0
        for t in trades:
            if t["pnl"] <= 0:
                cur_streak += 1
                max_streak = max(max_streak, cur_streak)
            else:
                cur_streak = 0
        print(f"    Max consecutive losses: {max_streak}")

asyncio.run(main())
