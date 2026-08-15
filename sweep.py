"""参数扫描 + 样本内外(IS/OOS)稳健性验证

网格：lookback × freq × top_n × risk_adjusted。
每组分两段评估：IS 2015~2020（选参用），OOS 2021~至今（验证用）。
选参数时看 IS 段的高原而非峰值，再对照 OOS 段是否延续。

用法：py sweep.py [--full-start 2015-01-01] [--is-end 2020-12-31]
输出：reports/sweep.csv + 控制台汇总
"""

import argparse
import itertools
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import pandas as pd

from data.fetcher import build_exec_panels, build_panels, load_all
from data.universe import BENCHMARK
from engine.engine import BacktestConfig, run
from engine.metrics import compute_metrics
from strategies.momentum import MomentumRotation

GRID = {
    "lookback": [20, 40, 60, 90, 120, 150],
    "freq": ["W", "M"],
    "top_n": [1, 2, 3],
    "risk_adjusted": [False, True],
}


def run_one(close, open_, combo, start, end, exec_close=None, exec_open=None):
    strategy = MomentumRotation(
        top_n=combo["top_n"],
        lookback=combo["lookback"],
        freq=combo["freq"],
        abs_filter=True,
        risk_adjusted=combo["risk_adjusted"],
    )
    config = BacktestConfig(initial_cash=100_000, start=start, end=end)
    result = run(close, open_, strategy, config, benchmark_close=close[BENCHMARK],
                 exec_close=exec_close, exec_open=exec_open)
    return compute_metrics(result.nav, result.trades)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--full-start", default="2015-01-01")
    ap.add_argument("--is-end", default="2020-12-31")
    args = ap.parse_args()
    oos_start = (pd.Timestamp(args.is_end) + pd.Timedelta(days=1)).strftime("%Y-%m-%d")

    print("加载数据…")
    data = load_all(quiet=True)
    close, open_ = build_panels(data)
    raw = load_all(quiet=True, adjust="")
    exec_close, exec_open = build_exec_panels(data, raw)

    keys = list(GRID)
    rows = []
    combos = list(itertools.product(*GRID.values()))
    t0 = time.time()
    for i, values in enumerate(combos, 1):
        combo = dict(zip(keys, values))
        full = run_one(close, open_, combo, args.full_start, "2099-12-31", exec_close, exec_open)
        is_m = run_one(close, open_, combo, args.full_start, args.is_end, exec_close, exec_open)
        oos = run_one(close, open_, combo, oos_start, "2099-12-31", exec_close, exec_open)
        rows.append({
            **combo,
            "全期年化": full["年化收益"], "全期回撤": full["最大回撤"], "全期夏普": full["夏普比率"],
            "IS夏普": is_m["夏普比率"], "IS年化": is_m["年化收益"], "IS回撤": is_m["最大回撤"],
            "OOS夏普": oos["夏普比率"], "OOS年化": oos["年化收益"], "OOS回撤": oos["最大回撤"],
            "OOS超额": oos["超额年化"] if "超额年化" in oos else None,
            "全期换手": full["年化换手(单边)"],
        })
        if i % 12 == 0:
            print(f"  {i}/{len(combos)} 组完成 ({time.time()-t0:.0f}s)")

    df = pd.DataFrame(rows)
    out = Path(__file__).parent / "reports" / "sweep.csv"
    df.to_csv(out, index=False, encoding="utf-8-sig")
    print(f"\n已保存 {out}（{len(df)} 组）\n")

    pd.set_option("display.width", 200)
    print("== 按 IS 夏普 Top 10（选参只看 IS 段）==")
    top_is = df.sort_values("IS夏普", ascending=False).head(10)
    print(top_is[["lookback", "freq", "top_n", "risk_adjusted",
                  "IS年化", "IS回撤", "IS夏普", "OOS年化", "OOS回撤", "OOS夏普"]].to_string(index=False,
                  float_format=lambda x: f"{x:.3f}"))

    print("\n== 按 OOS 夏普 Top 10（验证段，仅供参考，不得用于选参）==")
    top_oos = df.sort_values("OOS夏普", ascending=False).head(10)
    print(top_oos[["lookback", "freq", "top_n", "risk_adjusted",
                   "IS夏普", "OOS年化", "OOS回撤", "OOS夏普"]].to_string(index=False,
                   float_format=lambda x: f"{x:.3f}"))

    # IS/OOS 一致性：IS Top10 组合的 OOS 表现 vs 全体中位数
    med = df["OOS夏普"].median()
    print(f"\n全体 OOS 夏普中位数: {med:.3f}")
    print(f"IS Top10 组合的 OOS 夏普中位数: {top_is['OOS夏普'].median():.3f}")
    print("（若 IS 优选组的 OOS 表现 ≥ 全体中位数，说明信号在样本外有一定延续性，而非曲线拟合）")


if __name__ == "__main__":
    main()
