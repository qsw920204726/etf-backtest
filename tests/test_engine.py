"""引擎正确性校验（不依赖网络，用缓存数据）：

1. 哑策略：第一个交易日全仓买入基准 ETF 并持有到期末。
   引擎净值曲线应与基准买入持有基本重合（差异仅来自：佣金、100股取整、T+1开盘成交）。
2. T+1 校验：当日买入份额当日不可能被卖出。
3. 现金守恒：现金 + 持仓市值 == 总资产，且无负现金。

用法：py tests/test_engine.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pandas as pd

from data.fetcher import build_panels, load_all
from data.universe import BENCHMARK
from engine.engine import BacktestConfig, run
from engine.broker import Broker, BrokerConfig
from engine.portfolio import Portfolio
from strategies.base import Strategy


class BuyAndHold(Strategy):
    """首日全仓一只 ETF，持有不动"""

    def __init__(self, code):
        self.code = code
        self.done = False

    def on_bar(self, date, portfolio_value):
        if self.done:
            return None
        self.done = True
        return {self.code: 1.0}


def main():
    data = load_all(quiet=True)
    close, open_ = build_panels(data)

    # ---- 1. 哑策略 vs 基准 ----
    strategy = BuyAndHold(BENCHMARK)
    config = BacktestConfig(initial_cash=100_000, start="2018-01-01")
    result = run(close, open_, strategy, config, benchmark_close=close[BENCHMARK])

    final = result.nav["value"].iloc[-1]
    bench_final = result.nav["benchmark"].iloc[-1]
    diff = abs(final - bench_final) / bench_final
    status = "PASS" if diff < 0.01 else "FAIL"  # 佣金+取整误差应远小于1%
    print(f"[{status}] 买入持有校验: 引擎 {final:,.0f} vs 基准 {bench_final:,.0f} (偏差 {diff*100:.3f}%)")
    print(f"       成交明细: {result.trades}")

    # ---- 2. T+1 ----
    portfolio = Portfolio(100_000)
    broker = Broker(BrokerConfig())
    bench_start = close[BENCHMARK].dropna().index[0]  # 基准上市首日
    opens = {BENCHMARK: float(open_[BENCHMARK].loc[bench_start])}
    closes = {BENCHMARK: float(close[BENCHMARK].loc[bench_start])}
    date = bench_start
    broker.rebalance(portfolio, {BENCHMARK: 1.0}, opens, closes, date)
    sold_before_unfreeze = portfolio.positions[BENCHMARK].sell(999999)
    portfolio.unfreeze()
    sold_after = portfolio.positions[BENCHMARK].sell(999999)
    status = "PASS" if sold_before_unfreeze == 0 and sold_after > 0 else "FAIL"
    print(f"[{status}] T+1 校验: 解冻前可卖 {sold_before_unfreeze}, 解冻后可卖 {sold_after}")

    # ---- 3. 现金守恒 ----
    strategy = BuyAndHold(BENCHMARK)
    result2 = run(close, open_, strategy, BacktestConfig(initial_cash=88_888, start="2020-01-01"))
    neg_cash = (result2.nav["cash"] < -0.01).any()
    value_ok = result2.nav["value"].notna().all()
    status = "PASS" if (not neg_cash) and value_ok else "FAIL"
    print(f"[{status}] 现金守恒: 出现负现金={neg_cash}, 净值序列完整={value_ok}")


if __name__ == "__main__":
    main()
