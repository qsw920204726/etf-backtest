"""动量轮动策略

规则：
  每个调仓日（月末/周五），计算池内各 ETF 近 lookback 个交易日的涨幅（动量），
  持有动量最强的 top_n 只（等权）。若启用绝对动量过滤，最强者的动量 <= 0 时空仓持币。
"""

import pandas as pd

from strategies.base import Strategy


class MomentumRotation(Strategy):
    def __init__(
        self,
        top_n: int = 3,
        lookback: int = 20,       # 默认取自科技池 IS/OOS 扫描的稳健高原(20/W 邻域)
        freq: str = "W",          # 'M' 月末调仓 / 'W' 每周最后一个交易日
        abs_filter: bool = True,  # 绝对动量过滤：无正动量标的则持币
        risk_adjusted: bool = True,  # 动量分 = 收益/波动(夏普式)；扫描显示 OOS 显著更稳
    ):
        self.top_n = top_n
        self.lookback = lookback
        self.freq = freq
        self.abs_filter = abs_filter
        self.risk_adjusted = risk_adjusted

    def prepare(self, close) -> None:
        super().prepare(close)
        # 调仓日历：每个周期的最后一个交易日
        s = pd.Series(close.index, index=close.index)
        if self.freq == "M":
            self._rebalance_dates = set(s.groupby(close.index.to_period("M")).max())
        else:
            self._rebalance_dates = set(s.groupby(close.index.to_period("W")).max())

    def on_bar(self, date, portfolio_value: float) -> dict[str, float] | None:
        if date not in self._rebalance_dates:
            return None

        momentum = self.scores(date)
        if not momentum:
            return {}
        if self.abs_filter and momentum[0][1] <= 0:
            return {}  # 全场无正动量 → 清仓持币

        selected = [c for c, _, _ in momentum[: self.top_n]]
        weight = 1.0 / len(selected)
        return {code: weight for code in selected}

    def scores(self, date) -> list[tuple[str, float, float]]:
        """动量排名：[(code, 动量分, 区间涨幅)] 按分数降序。on_bar 与此同口径。"""
        hist = self.close.loc[:date]
        if len(hist) < self.lookback + 1:
            return []
        window = hist.iloc[-(self.lookback + 1):]
        ret = window.iloc[-1] / window.iloc[0] - 1
        if self.risk_adjusted:
            vol = window.iloc[1:].pct_change().std() * (self.lookback ** 0.5)
            score = ret / vol.replace(0, float("nan"))
        else:
            score = ret
        df = pd.DataFrame({"score": score, "ret": ret}).dropna()
        df = df.sort_values("score", ascending=False)
        return [(code, float(row.score), float(row.ret)) for code, row in df.iterrows()]
