"""账户与持仓管理

T+1 实现：当日买入进 frozen，日终由引擎调用 unfreeze() 解冻为可卖。卖出只能动 available。
"""


class Position:
    def __init__(self, code: str):
        self.code = code
        self.shares = 0        # 总份额
        self.available = 0     # 可卖份额
        self.frozen = 0        # 今日买入冻结份额（T+1）

    def buy(self, shares: int):
        self.shares += shares
        self.frozen += shares

    def sell(self, shares: int) -> int:
        shares = min(shares, self.available)
        if shares <= 0:
            return 0
        self.shares -= shares
        self.available -= shares
        return shares


class Portfolio:
    def __init__(self, initial_cash: float):
        self.initial_cash = initial_cash
        self.cash = initial_cash
        self.positions: dict[str, Position] = {}

    def position(self, code: str) -> Position:
        if code not in self.positions:
            self.positions[code] = Position(code)
        return self.positions[code]

    def unfreeze(self):
        """日终调用：今日买入的份额明日可卖。"""
        for pos in self.positions.values():
            pos.available += pos.frozen
            pos.frozen = 0

    def market_value(self, closes: dict[str, float]) -> float:
        value = self.cash
        for code, pos in self.positions.items():
            if pos.shares > 0:
                value += pos.shares * closes[code]
        return value

    def current_weights(self, closes: dict[str, float]) -> dict[str, float]:
        total = self.market_value(closes)
        if total <= 0:
            return {}
        return {
            code: pos.shares * closes[code] / total
            for code, pos in self.positions.items()
            if pos.shares > 0
        }

    def cleanup(self):
        """清掉份额为 0 的死仓位。"""
        self.positions = {
            code: pos for code, pos in self.positions.items() if pos.shares > 0
        }
