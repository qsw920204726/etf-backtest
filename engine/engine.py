"""回测引擎主循环

时间线（防未来函数的机制保证）：
  T 日收盘 → 策略用截至 T 日（含）的数据产生目标权重
  T+1 开盘 → broker 按开盘价撮合（先卖后买）
  T+1 日终 → 解冻 T+1 买入份额（T+1 制度）

引擎只把当日及之前的数据传给策略，策略拿不到未来。
"""

from dataclasses import dataclass, field

import pandas as pd

from engine.broker import Broker, BrokerConfig
from engine.portfolio import Portfolio
from strategies.base import Strategy


@dataclass
class BacktestConfig:
    initial_cash: float = 100_000.0
    start: str = "2015-01-01"
    end: str = "2099-12-31"
    broker: BrokerConfig = field(default_factory=BrokerConfig)


@dataclass
class BacktestResult:
    nav: pd.DataFrame            # index=date, columns=[value, cash, benchmark, n_holdings]
    trades: list[dict]
    holdings: list[dict]         # 每日持仓快照 {date, code, name?, weight}
    config: BacktestConfig


def run(
    close: pd.DataFrame,
    open_: pd.DataFrame,
    strategy: Strategy,
    config: BacktestConfig | None = None,
    benchmark_close: pd.Series | None = None,
    exec_close: pd.DataFrame | None = None,
    exec_open: pd.DataFrame | None = None,
) -> BacktestResult:
    """close/open_: 信号面板（后复权，算动量用）。
    exec_close/exec_open: 执行面板（真实价，成交/估值/股数取整用）；
    缺省时退回信号面板（合成数据/测试场景）。"""
    config = config or BacktestConfig()
    start = pd.Timestamp(config.start)
    end = pd.Timestamp(config.end)
    calendar = close.index[(close.index >= start) & (close.index <= end)]

    if exec_close is None:
        exec_close = close
    if exec_open is None:
        exec_open = open_
    # 执行面板对齐信号面板的交易日历
    exec_close = exec_close.reindex(close.index).ffill()
    exec_open = exec_open.reindex(close.index).ffill()

    portfolio = Portfolio(config.initial_cash)
    broker = Broker(config.broker)
    strategy.prepare(close)

    pending_targets: dict[str, float] | None = None
    last_closes: dict[str, float] | None = None

    nav_rows = []
    holdings_snap = []
    current_weights: dict[str, float] = {}

    for i, date in enumerate(calendar):
        closes = exec_close.loc[date].to_dict()  # 估值用真实价

        # ---- 开盘：执行上一收盘产生的目标 ----
        if pending_targets is not None:
            opens = exec_open.loc[date].to_dict()  # 成交用真实价
            # 权重与当前基本一致则跳过，省佣金（调仓噪音过滤）
            if not _weights_changed(current_weights, pending_targets):
                pending_targets = None
            else:
                broker.rebalance(portfolio, pending_targets, opens, last_closes, date)
                pending_targets = None
        portfolio.unfreeze()

        # ---- 收盘估值 ----
        value = portfolio.market_value(closes)
        nav_rows.append(
            {
                "date": date,
                "value": value,
                "cash": portfolio.cash,
                "n_holdings": sum(1 for p in portfolio.positions.values() if p.shares > 0),
            }
        )
        current_weights = portfolio.current_weights(closes)
        for code, w in current_weights.items():
            holdings_snap.append({"date": date, "code": code, "weight": w})
        last_closes = closes

        # ---- 收盘：策略产生信号（只允许看到 <= date 的数据）----
        if i < len(calendar) - 1:  # 最后一天不需要产生次日订单
            targets = strategy.on_bar(date, value)
            if targets is not None:  # 注意 {} = 清仓持币，也是有效信号
                pending_targets = targets

    nav = pd.DataFrame(nav_rows).set_index("date")
    if benchmark_close is not None:
        bench = benchmark_close.reindex(nav.index).dropna()
        if not bench.empty:  # 基准在区间内无数据时不画基准线
            bench = bench.reindex(nav.index).ffill()
            nav["benchmark"] = bench / bench.iloc[0] * config.initial_cash

    return BacktestResult(
        nav=nav,
        trades=[t.as_dict() for t in broker.trades],
        holdings=holdings_snap,
        config=config,
    )


def _weights_changed(current: dict[str, float], target: dict[str, float], tol: float = 0.02) -> bool:
    """目标权重与当前权重差异小于 tol（每只 2%）视为无需调仓。"""
    codes = set(current) | set(target)
    return any(abs(current.get(c, 0.0) - target.get(c, 0.0)) > tol for c in codes)
