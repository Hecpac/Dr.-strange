---
name: QTS Backtesting
triggers:
  - backtest
  - backtesting
  - qts
  - trading
  - ict strategy
  - ema
  - drawdown
priority: 8
---

# QTS Backtesting — Workflows de Backtesting

## Archivos Clave
- `backtest_multi.py` — Multi-asset EMA 9/21 con sizing configurable, trailing stops, circuit breaker
- `backtest_ict.py` — ICT ATR Displacement + Hurst + Kill Zones (per-asset config optimizada)
- `backtest_ict_sweep.py` — Parameter sweep + ablation study
- `QTS-ARCHITECT/reports/` — Reportes guardados

## Configs Per-Asset Optimizadas (ICT)
| Asset | ATR k | Hurst | KZ | Sizing |
|-------|-------|-------|----|--------|
| BTC | 1.5 | >0.50 | Both | 20% |
| Gold | 2.0 | None | NY only | 20% |

## Activos Retirados
- **DOGE** — retirado 2026-05-09 antes de Sprint 5 paper. Razón: edge genuinamente perdido. Sprint 6 OOS con guardrails restaurados (`risk_cap_per_position`, `dynamic_extreme_block`) recuperó BTC OOS y multi-symbol pero NO rescató DOGE: -6.33% / Sharpe -0.64 / PF 0.98. La degradación no era artifact de configs perdidos. No reincluir sin nuevo edge verificado en walk-forward.

## Métricas de Referencia (post-Sprint-6, guardrails ON)
- **Gold ICT NY 8-11**: +2.83%, Sharpe 0.98, DD -2.64% (Tier 1 paper-sostenido candidato).
- **BTC ICT OOS 10wk**: +0.43%, Sharpe +0.46 (Tier 2 paper $5k validación).
- **Multi BTC+ETH+SOL**: -5.19% / DD -14.57% — recuperado vs catástrofe pre-guardrails (-32.90% / DD -46%) pero SOL infla trade count, no listo para paper.
- Baseline 2026-02-26 (sin guardrails) en `reports/qts-1y-multisymbol-backtest-2026-02-26.json` queda como referencia histórica, no como expectancy operacional.

## Workflow de Nuevo Backtest
1. Leer datos con `ccxt` (Binance futures, 1h candles)
2. Implementar estrategia como función con señales long/short
3. Loop: entry → stop loss → trailing stop → exit
4. Calcular métricas: return, max DD, PF, Sharpe, Calmar, win rate
5. Comparar vs B&H
6. Guardar reporte en `QTS-ARCHITECT/reports/`

## Circuit Breaker
- Track `peak_equity` solo sobre capital **realizado** (cash + blocked_cash + closed-trade P&L), nunca con unrealized P&L de posiciones abiertas.
- Si equity DD supera umbral, bloquear nuevas entradas.
- Fix landed 2026-05-09 en `qts_core/src/qts_core/agents/watchdog.py` (`evaluate(realized_equity=...)`). El bug original — peak_equity inflado con ganancias no realizadas que causaba bloqueo permanente al revertir el precio — quedó cubierto por `tests/test_watchdog_realized_peak.py`.
