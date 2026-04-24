"""Paper trading engine — simulates order execution and tracks PnL."""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path

from claw_v2.qts.dag import DAGResult

logger = logging.getLogger(__name__)

STATE_PATH = Path.home() / ".claw" / "qts" / "paper_state.json"


@dataclass
class Order:
    id: str
    asset: str
    side: str  # "long" | "short"
    order_type: str  # "market" | "limit"
    size_usd: float
    entry_price: float
    stop_loss: float | None = None
    status: str = "pending"  # pending | filled | cancelled
    filled_price: float | None = None
    created_at: float = field(default_factory=time.time)


@dataclass
class Position:
    asset: str
    side: str
    size_usd: float
    entry_price: float
    stop_loss: float | None = None
    opened_at: float = field(default_factory=time.time)

    @property
    def size_units(self) -> float:
        return self.size_usd / self.entry_price if self.entry_price else 0.0

    def unrealized_pnl(self, current_price: float) -> float:
        delta = current_price - self.entry_price
        if self.side == "short":
            delta = -delta
        return self.size_units * delta


@dataclass
class Portfolio:
    cash: float = 10_000.0
    positions: list[Position] = field(default_factory=list)
    closed_pnl: float = 0.0
    orders: list[Order] = field(default_factory=list)
    trade_log: list[dict] = field(default_factory=list)

    def total_equity(self, prices: dict[str, float]) -> float:
        unrealized = sum(
            p.unrealized_pnl(prices.get(p.asset, p.entry_price))
            for p in self.positions
        )
        return self.cash + unrealized

    def save(self, path: Path = STATE_PATH) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self), indent=2, default=str))

    @classmethod
    def load(cls, path: Path = STATE_PATH) -> Portfolio:
        if not path.exists():
            return cls()
        raw = json.loads(path.read_text())
        p = cls(cash=raw.get("cash", 10_000.0), closed_pnl=raw.get("closed_pnl", 0.0))
        for pos in raw.get("positions", []):
            p.positions.append(Position(**{k: v for k, v in pos.items() if k != "size_units"}))
        for log in raw.get("trade_log", []):
            p.trade_log.append(log)
        return p


class PaperTrader:
    def __init__(self, capital: float = 10_000.0) -> None:
        self.portfolio = Portfolio.load()
        if not self.portfolio.positions and not self.portfolio.trade_log:
            self.portfolio.cash = capital
        self._order_counter = len(self.portfolio.orders)

    def execute(self, result: DAGResult, current_price: float, asset: str = "BTC/USDT") -> dict:
        if not result.should_trade:
            return {"action": "hold", "reason": "DAG says no trade"}

        direction = result.metadata.get("direction", "long")
        order_type = result.metadata.get("order_type", "market")
        size_fraction = result.position_size

        existing = [p for p in self.portfolio.positions if p.asset == asset]
        if existing:
            return {"action": "hold", "reason": f"Already have {asset} position"}

        size_usd = self.portfolio.cash * size_fraction
        if size_usd < 10:
            return {"action": "hold", "reason": "Position too small"}

        risk_meta = result.signals.get("risk", None)
        stop_loss = None
        if risk_meta and risk_meta.metadata:
            stop_loss = risk_meta.metadata.get("stop_loss_price")

        exec_meta = result.signals.get("executor", None)
        entry_price = current_price
        if order_type == "limit" and exec_meta and exec_meta.metadata:
            entry_price = exec_meta.metadata.get("entry_price", current_price)

        if order_type == "market":
            return self._fill_order(asset, direction, size_usd, current_price, stop_loss)
        else:
            self._order_counter += 1
            order = Order(
                id=f"paper-{self._order_counter}",
                asset=asset,
                side=direction,
                order_type="limit",
                size_usd=size_usd,
                entry_price=entry_price,
                stop_loss=stop_loss,
            )
            self.portfolio.orders.append(order)
            self.portfolio.save()
            return {
                "action": "limit_placed",
                "order_id": order.id,
                "side": direction,
                "size_usd": round(size_usd, 2),
                "limit_price": round(entry_price, 2),
                "stop_loss": round(stop_loss, 2) if stop_loss else None,
            }

    def check_fills(self, current_price: float) -> list[dict]:
        filled = []
        for order in self.portfolio.orders:
            if order.status != "pending":
                continue
            should_fill = (
                (order.side == "long" and current_price <= order.entry_price) or
                (order.side == "short" and current_price >= order.entry_price)
            )
            if should_fill:
                result = self._fill_order(
                    order.asset, order.side, order.size_usd,
                    current_price, order.stop_loss,
                )
                order.status = "filled"
                order.filled_price = current_price
                filled.append(result)
        if filled:
            self.portfolio.save()
        return filled

    def check_stops(self, current_price: float, asset: str = "BTC/USDT") -> list[dict]:
        closed = []
        remaining = []
        for pos in self.portfolio.positions:
            if pos.asset != asset or pos.stop_loss is None:
                remaining.append(pos)
                continue
            hit = (
                (pos.side == "long" and current_price <= pos.stop_loss) or
                (pos.side == "short" and current_price >= pos.stop_loss)
            )
            if hit:
                pnl = pos.unrealized_pnl(current_price)
                self.portfolio.closed_pnl += pnl
                self.portfolio.cash += pos.size_usd + pnl
                entry = {
                    "action": "stop_hit",
                    "asset": asset,
                    "side": pos.side,
                    "entry": pos.entry_price,
                    "exit": current_price,
                    "pnl": round(pnl, 2),
                    "ts": time.time(),
                }
                self.portfolio.trade_log.append(entry)
                closed.append(entry)
            else:
                remaining.append(pos)
        self.portfolio.positions = remaining
        if closed:
            self.portfolio.save()
        return closed

    def status(self, prices: dict[str, float] | None = None) -> dict:
        prices = prices or {}
        equity = self.portfolio.total_equity(prices)
        return {
            "cash": round(self.portfolio.cash, 2),
            "equity": round(equity, 2),
            "positions": len(self.portfolio.positions),
            "pending_orders": sum(1 for o in self.portfolio.orders if o.status == "pending"),
            "closed_pnl": round(self.portfolio.closed_pnl, 2),
            "total_trades": len(self.portfolio.trade_log),
        }

    def _fill_order(self, asset: str, side: str, size_usd: float,
                    fill_price: float, stop_loss: float | None) -> dict:
        self.portfolio.cash -= size_usd
        pos = Position(
            asset=asset, side=side, size_usd=size_usd,
            entry_price=fill_price, stop_loss=stop_loss,
        )
        self.portfolio.positions.append(pos)
        entry = {
            "action": "filled",
            "asset": asset,
            "side": side,
            "size_usd": round(size_usd, 2),
            "price": round(fill_price, 2),
            "stop_loss": round(stop_loss, 2) if stop_loss else None,
            "ts": time.time(),
        }
        self.portfolio.trade_log.append(entry)
        self.portfolio.save()
        logger.info("Paper %s %s: $%.2f @ $%.2f", side, asset, size_usd, fill_price)
        return entry
