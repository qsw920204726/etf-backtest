"""确定性合成数据测试套件 —— 不依赖网络与数据缓存

覆盖：
  1. 防未来函数：篡改 T 之后的所有行情，T 日前的净值与交易必须逐字节不变
  2. 空字典清仓信号（回归测试：曾因 `if targets:` 被吞掉）
  3. 先卖后买：满仓切换时现金全程非负、份额按 100 取整
  4. 佣金边界：最低佣金、万2.5费率
  5. T+1：当日买入当日不可卖
  6. 绩效指标与手算对照（年化/回撤/夏普）
  7. 基准归一化：基准净值从初始资金起步

用法：py tests/test_synthetic.py
"""

import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import numpy as np
import pandas as pd

from engine.broker import Broker, BrokerConfig
from engine.engine import BacktestConfig, run, _weights_changed
from engine.metrics import compute_metrics
from engine.portfolio import Portfolio
from strategies.base import Strategy

PASS, FAIL = 0, 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"[PASS] {name}")
    else:
        FAIL += 1
        print(f"[FAIL] {name}  {detail}")


# ---------- 合成数据 ----------

def make_panels(spec: dict[str, float], days=160, start="2023-01-02"):
    """每只 ETF 从 100 起，按固定日增长率走出确定性曲线；开盘价 = 前收。"""
    idx = pd.bdate_range(start, periods=days)
    close = pd.DataFrame(
        {code: 100.0 * ((1 + g) ** np.arange(days)) for code, g in spec.items()},
        index=idx,
    )
    open_ = close.shift(1).fillna(close.iloc[0])
    return close, open_


class BuyA(Strategy):
    """首日全仓 A，之后一直返回 None"""

    def on_bar(self, date, value):
        if not getattr(self, "_done", False):
            self._done = True
            return {"A": 1.0}
        return None


class BuyAThenCash(Strategy):
    """首日全仓 A，次日信号清仓持币（返回空字典）"""

    def on_bar(self, date, value):
        if not getattr(self, "_done", False):
            self._done = True
            return {"A": 1.0}
        if not getattr(self, "_cash", False):
            self._cash = True
            return {}
        return None


# ---------- 1. 防未来函数 ----------

def test_no_lookahead():
    close, open_ = make_panels({"A": 0.002, "B": -0.001, "C": 0.004})
    from strategies.momentum import MomentumRotation

    cfg = BacktestConfig(initial_cash=100_000)
    r1 = run(close, open_, MomentumRotation(top_n=1, lookback=20), cfg)

    # 篡改中期之后的全部行情（×10），前半段结果必须完全一致（mid 当天保持原值）
    mid = close.index[len(close) // 2]
    after = close.index[close.index.get_loc(mid) + 1]
    close2, open2 = close.copy(), open_.copy()
    close2.loc[after:] *= 10
    open2.loc[after:] *= 10
    r2 = run(close2, open2, MomentumRotation(top_n=1, lookback=20), cfg)

    same_nav = np.allclose(r1.nav["value"].loc[:mid], r2.nav["value"].loc[:mid])
    t1 = [t for t in r1.trades if t["date"] < str(mid.date())]
    t2 = [t for t in r2.trades if t["date"] < str(mid.date())]
    check("防未来函数: 篡改未来不改历史净值", same_nav)
    check("防未来函数: 篡改未来不改历史交易", t1 == t2, f"{len(t1)} vs {len(t2)} 笔")


# ---------- 2. 空字典清仓信号 ----------

def test_cash_signal():
    close, open_ = make_panels({"A": 0.001})
    r = run(close, open_, BuyAThenCash(), BacktestConfig(initial_cash=100_000))
    last = r.nav.iloc[-1]
    sides = [t["side"] for t in r.trades]
    check("空字典信号: 产生了卖出", "sell" in sides, str(r.trades))
    check("空字典信号: 期末满仓现金", abs(last["cash"] - last["value"]) < 1e-6,
          f"cash={last['cash']:.2f} value={last['value']:.2f}")


# ---------- 3. 先卖后买 / 取整 / 现金流 ----------

def test_switch_and_rounding():
    class SwitchOnce(Strategy):
        def __init__(self):
            self.n = 0

        def on_bar(self, date, value):
            self.n += 1
            if self.n == 1:
                return {"A": 1.0}
            if self.n == 2:
                return {"B": 1.0}
            return None

    close, open_ = make_panels({"A": 0.001, "B": 0.001})
    r = run(close, open_, SwitchOnce(), BacktestConfig(initial_cash=99_999))
    check("满仓切换: 全程现金非负", (r.nav["cash"] >= -1e-6).all())
    check("成交份额为 100 整数倍", all(t["shares"] % 100 == 0 for t in r.trades))
    sell_buy_same_day = (
        len(r.trades) >= 3
        and r.trades[1]["side"] == "sell" and r.trades[2]["side"] == "buy"
        and r.trades[1]["date"] == r.trades[2]["date"]
    )
    check("切换日先卖后买(同一日内成交两笔)", sell_buy_same_day, str(r.trades))


# ---------- 4. 佣金边界 ----------

def test_commission():
    p = Portfolio(100_000)
    b = Broker(BrokerConfig(commission_rate=0.00025, min_commission=5.0, min_trade_value=0))
    d = pd.Timestamp("2024-01-02")
    # 目标权重 0.4% → 约 400 份 × 1 元 = 400 元 → 万2.5 = 0.1 元 < 5 元 → 收最低 5 元
    b.rebalance(p, {"A": 0.004}, {"A": 1.0}, {"A": 1.0}, d)
    tiny = [t for t in b.trades if t.value <= 500]
    check("最低佣金 5 元生效", bool(tiny) and all(t.commission == 5.0 for t in tiny),
          str([(t.side, t.shares, t.value, t.commission) for t in b.trades]))

    p2 = Portfolio(1_000_000)
    b2 = Broker(BrokerConfig(commission_rate=0.00025, min_commission=5.0))
    b2.rebalance(p2, {"A": 1.0}, {"A": 10.0}, {"A": 10.0}, d)
    t = b2.trades[0]
    check("佣金率万2.5按金额计", abs(t.commission - t.value * 0.00025) < 0.01,
          f"{t.commission} vs {t.value*0.00025:.4f}")


# ---------- 5. T+1 ----------

def test_t_plus_1():
    p = Portfolio(100_000)
    b = Broker()
    d = pd.Timestamp("2024-01-02")
    b.rebalance(p, {"A": 1.0}, {"A": 10.0}, {"A": 10.0}, d)
    pos = p.positions["A"]
    check("T+1: 买入当日冻结", pos.available == 0 and pos.frozen == pos.shares)
    p.unfreeze()
    check("T+1: 次日解冻可卖", pos.available == pos.shares)


# ---------- 6. 指标手算对照 ----------

def test_metrics():
    # 3 个交易日: 100 → 120 → 90（人为构造，252 日年化按公式手算）
    idx = pd.bdate_range("2024-01-02", periods=3)
    nav = pd.DataFrame(
        {"value": [100.0, 120.0, 90.0], "cash": [100.0, 100.0, 100.0],
         "n_holdings": [0, 0, 0], "benchmark": [100.0, 110.0, 105.0]},
        index=idx,
    )
    m = compute_metrics(nav, [])
    ann = (90 / 100) ** (252 / 3) - 1
    check("指标: 年化收益公式", abs(m["年化收益"] - ann) < 1e-9, f"{m['年化收益']:.6f} vs {ann:.6f}")
    check("指标: 最大回撤 -25%", abs(m["最大回撤"] - (-0.25)) < 1e-9)
    rets = pd.Series([0.2, -0.25])
    sharpe = rets.mean() / rets.std() * math.sqrt(252)  # pandas ddof=1
    check("指标: 夏普公式", abs(m["夏普比率"] - sharpe) < 1e-9, f"{m['夏普比率']:.4f} vs {sharpe:.4f}")
    check("指标: 基准年化", abs(m["基准年化"] - ((105 / 100) ** (252 / 3) - 1)) < 1e-9)


# ---------- 7. 基准归一化 ----------

def test_benchmark_norm():
    close, open_ = make_panels({"A": 0.001})
    r = run(close, open_, BuyA(), BacktestConfig(initial_cash=250_000),
            benchmark_close=close["A"])
    check("基准净值从初始资金起步", abs(r.nav["benchmark"].iloc[0] - 250_000) < 1e-6)


# ---------- 附: 权重跳过逻辑 ----------

def test_weights_changed():
    check("权重差异判断: 2% 阈值",
          _weights_changed({"A": 0.5}, {"A": 0.5}) is False
          and _weights_changed({"A": 0.5}, {"A": 0.515}) is False
          and _weights_changed({"A": 0.5}, {"A": 0.53}) is True
          and _weights_changed({}, {}) is False
          and _weights_changed({"A": 1.0}, {}) is True)


if __name__ == "__main__":
    test_no_lookahead()
    test_cash_signal()
    test_switch_and_rounding()
    test_commission()
    test_t_plus_1()
    test_metrics()
    test_benchmark_norm()
    test_weights_changed()
    print(f"\n{'='*40}\n  {PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)
