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
        weighting: str = "equal",    # equal 等权 / vol_inverse 波动率倒数(回撤更浅夏普更高)
        premium_cap: float | None = 0.03,  # 溢价率>该值剔除（QDII高溢价保护），None 关闭
        volume_boost: bool = True,  # 量能加成：动量分 × 成交额趋势(20日/60日)调整
        premium: "pd.DataFrame | None" = None,  # 溢价率面板（由调用方构建）
        amount: "pd.DataFrame | None" = None,   # 成交额面板
    ):
        self.top_n = top_n
        self.lookback = lookback
        self.freq = freq
        self.abs_filter = abs_filter
        self.risk_adjusted = risk_adjusted
        self.weighting = weighting
        self.premium_cap = premium_cap
        self.volume_boost = volume_boost
        self.premium = premium
        self.amount = amount

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

        momentum = [x for x in self.scores(date) if not self.premium_blocked(date, x[0])]
        if not momentum:
            return {}
        if self.abs_filter and momentum[0][1] <= 0:
            return {}  # 全场无正动量 → 清仓持币

        selected = [c for c, _, _ in momentum[: self.top_n]]

        if self.weighting == "vol_inverse":
            # 波动率倒数加权（简化风险平价）：稳的多配、颠的少配
            window = self.close.loc[:date].iloc[-(self.lookback + 1):]
            vol = window.iloc[1:].pct_change().std()
            inv = {c: 1.0 / vol[c] for c in selected if vol[c] > 0}
            total = sum(inv.values())
            if total > 0:
                return {c: v / total for c, v in inv.items()}
        weight = 1.0 / len(selected)
        return {code: weight for code in selected}

    def scores(self, date) -> list[tuple[str, float, float]]:
        """动量排名（含量能加成，不含溢价过滤）：[(code, 动量分, 区间涨幅)] 按分数降序。"""
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

        # 量能加成：成交额20日均/60日均 比值，裁剪到[0.5,1.5]后微调动量分
        if self.volume_boost and self.amount is not None:
            ahist = self.amount.loc[:date]
            if len(ahist) >= 80:
                a20 = ahist.iloc[-20:].mean()
                a60 = ahist.iloc[-80:-20].mean()
                boost = (a20 / a60.where(a60 > 0)).clip(0.5, 1.5).fillna(1.0)
                df["score"] = df["score"] * (1 + 0.5 * (boost - 1))
                df = df.dropna()

        df = df.sort_values("score", ascending=False)
        return [(code, float(row.score), float(row.ret)) for code, row in df.iterrows()]

    def premium_at(self, date, code) -> float:
        """当日溢价率（净值缺失返回 nan，不做过滤）"""
        if self.premium is None or code not in self.premium:
            return float("nan")
        return float(self.premium[code].asof(date))

    def premium_blocked(self, date, code) -> bool:
        p = self.premium_at(date, code)
        return p == p and self.premium_cap is not None and p > self.premium_cap
