"""成交模拟：按次日开盘价撮合，含 T+1、佣金、滑点、100 份整数倍。

ETF 场内费用规则：
- 佣金双边收取，默认万 2.5；min_commission 设 0（多数券商 ETF 免 5 元起收），需可配
- 无印花税
- 申报单位 100 份
"""

import math
from dataclasses import dataclass, field

from engine.portfolio import Portfolio


@dataclass
class BrokerConfig:
    commission_rate: float = 0.00025   # 佣金万 2.5
    min_commission: float = 0.0        # 最低佣金（元），0 = 免 5 元
    slippage: float = 0.0              # 单边滑点比例，如 0.001 = 0.1%
    min_trade_value: float = 500.0     # 低于该金额的调仓单直接跳过（省佣金噪音）


@dataclass
class Trade:
    date: object
    code: str
    side: str            # 'buy' / 'sell'
    shares: int
    price: float
    commission: float

    @property
    def value(self) -> float:
        return self.shares * self.price

    def as_dict(self) -> dict:
        return {
            "date": str(self.date.date()),
            "code": self.code,
            "side": self.side,
            "shares": self.shares,
            "price": round(self.price, 3),
            "value": round(self.value, 2),
            "commission": round(self.commission, 2),
        }


class Broker:
    def __init__(self, config: BrokerConfig | None = None):
        self.config = config or BrokerConfig()
        self.trades: list[Trade] = []

    def _commission(self, amount: float) -> float:
        return max(amount * self.config.commission_rate, self.config.min_commission)

    def rebalance(
        self,
        portfolio: Portfolio,
        targets: dict[str, float],
        open_prices: dict[str, float],
        last_closes: dict[str, float],
        date,
    ) -> None:
        """把组合调整到目标权重。

        targets: {code: weight}，weight 之和 <= 1，缺省的持仓视为清仓。
        目标市值按昨收盘总资产估算，实际按今日开盘价成交。
        """
        total_value = portfolio.market_value(last_closes)
        if total_value <= 0:
            return

        # ---- 先卖后买 ----
        for code in list(portfolio.positions):
            pos = portfolio.positions[code]
            if pos.shares <= 0:
                continue
            price = open_prices.get(code)
            if price is None or price != price:  # 停牌无价，跳过留待下日
                continue
            target_value = total_value * targets.get(code, 0.0)
            current_value = pos.shares * price
            if target_value >= current_value * 0.995:
                continue  # 基本到位，不动
            sell_value = current_value - target_value
            shares = math.floor(sell_value / price / 100) * 100
            if targets.get(code, 0.0) == 0.0:
                shares = pos.available  # 清仓（只能卖 T+1 可用部分）
            executed = pos.sell(shares)
            if executed <= 0:
                continue
            exec_price = price * (1 - self.config.slippage)
            commission = self._commission(executed * exec_price)
            portfolio.cash += executed * exec_price - commission
            self.trades.append(Trade(date, code, "sell", executed, exec_price, commission))

        portfolio.cleanup()

        # ---- 买入 ----
        buy_weight_sum = sum(w for w in targets.values())
        if buy_weight_sum <= 0:
            return
        for code, weight in sorted(targets.items()):
            if weight <= 0:
                continue
            price = open_prices.get(code)
            if price is None or price != price:
                continue
            pos = portfolio.positions.get(code)
            held_value = (pos.shares * price) if pos else 0.0
            alloc_value = total_value * weight - held_value
            if alloc_value < self.config.min_trade_value:
                continue
            # 预算受当前现金约束（按权重比例预留其他买单）
            budget = min(alloc_value, portfolio.cash * weight / buy_weight_sum)
            shares = math.floor(budget / price / 100) * 100
            if shares <= 0:
                continue
            exec_price = price * (1 + self.config.slippage)
            commission = self._commission(shares * exec_price)
            cost = shares * exec_price + commission
            while shares > 0 and cost > portfolio.cash:
                shares -= 100
                commission = self._commission(shares * exec_price)  # 同步更新，避免记录减仓前旧佣金
                cost = shares * exec_price + commission
            if shares <= 0:
                continue
            portfolio.cash -= cost
            portfolio.position(code).buy(shares)
            self.trades.append(Trade(date, code, "buy", shares, exec_price, commission))
