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
| DOGE | 1.5 | >0.55 | Both | 20% |

## Métricas de Referencia (última corrida)
- **DOGE ICT**: +7.86%, PF 4.77, WR 66.7%, DD -1.61%
- **Gold ICT**: +1.66%, PF 2.19, WR 55.6%, DD -1.19%
- **BTC ICT**: +1.42%, PF 1.28, WR 43.2%, DD -2.18%

## Workflow de Nuevo Backtest
1. Leer datos con `ccxt` (Binance futures, 1h candles)
2. Implementar estrategia como función con señales long/short
3. Loop: entry → stop loss → trailing stop → exit
4. Calcular métricas: return, max DD, PF, Sharpe, Calmar, win rate
5. Comparar vs B&H
6. Guardar reporte en `QTS-ARCHITECT/reports/`

## Circuit Breaker
- Track `peak_equity` solo sobre capital **realizado** (no unrealized P&L)
- Si equity DD supera umbral, bloquear nuevas entradas
- Bug conocido: peak_equity inflado con ganancias no realizadas causa bloqueo permanente

## QTS Multi-Agent DAG (2026-04-22)
Módulo: `claw_v2/qts/`
- `agents.py` — 4 agentes especializados (researcher, analyst, risk, executor)
- `features.py` — LLM feature extractor (sentiment, regime, volatility)
- `dag.py` — DAG Planner que orquesta agentes en capas paralelas/secuenciales

### Arquitectura
```
[researcher] ──┐
               ├──> [analyst] ──> [risk] ──> [executor]
[features]  ───┘
```
- **Regime gate**: si confidence < 0.5 o regime = choppy, no opera
- **Risk veto**: si position_size < 0.05, no opera
- Agentes producen señales (-1.0 a 1.0), nunca ejecutan órdenes directamente
- Basado en: AgenticTrading (NeurIPS, Sharpe 2.63) + LLM-DRL Hybrid (PeerJ)
