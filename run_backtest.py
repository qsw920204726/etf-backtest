"""命令行入口：更新数据 → 跑动量轮动回测 → 输出报告

用法：
  py run_backtest.py                          # 默认参数：月末调仓，持有最强2只，150日风险调整动量
  py run_backtest.py --top-n 1 --lookback 20 --freq W
  py run_backtest.py --refresh                # 强制全量重拉数据
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import pandas as pd

from data.fetcher import build_exec_panels, build_panels, load_all
from data.universe import BENCHMARK, UNIVERSE
from engine.engine import BacktestConfig, run
from engine.broker import BrokerConfig
from engine.metrics import compute_metrics, print_report
from strategies.momentum import MomentumRotation


def main():
    ap = argparse.ArgumentParser(description="ETF 动量轮动回测")
    ap.add_argument("--top-n", type=int, default=3)
    ap.add_argument("--lookback", type=int, default=20)
    ap.add_argument("--freq", choices=["M", "W"], default="W")
    ap.add_argument("--no-abs-filter", action="store_true", help="关闭绝对动量过滤")
    ap.add_argument("--risk-adjusted", action="store_true", default=True, help="风险调整动量(默认开)")
    ap.add_argument("--no-risk-adjusted", dest="risk_adjusted", action="store_false",
                    help="用裸收益率动量")
    ap.add_argument("--start", default="2015-01-01")
    ap.add_argument("--end", default="2099-12-31")
    ap.add_argument("--cash", type=float, default=100_000)
    ap.add_argument("--commission-rate", type=float, default=0.00025)
    ap.add_argument("--min-commission", type=float, default=0.0)
    ap.add_argument("--slippage", type=float, default=0.0)
    ap.add_argument("--refresh", action="store_true", help="强制全量重拉数据")
    args = ap.parse_args()

    print("== 1. 更新数据 ==")
    data = load_all(refresh=args.refresh)
    if not data:
        print("未获取到任何数据，退出")
        return
    close, open_ = build_panels(data)
    raw = load_all(refresh=args.refresh, quiet=True, adjust="")
    exec_close, exec_open = build_exec_panels(data, raw)
    print(f"  执行价口径: {'对齐真实价' if exec_close is not None else '后复权价(真实价拉取失败,自动回退)'}")

    print(f"\n== 2. 回测：动量轮动 top{args.top_n} / {args.lookback}日动量 / {'月末' if args.freq == 'M' else '每周'}调仓 ==")
    strategy = MomentumRotation(
        top_n=args.top_n,
        lookback=args.lookback,
        freq=args.freq,
        abs_filter=not args.no_abs_filter,
        risk_adjusted=args.risk_adjusted,
    )
    config = BacktestConfig(
        initial_cash=args.cash,
        start=args.start,
        end=args.end,
        broker=BrokerConfig(
            commission_rate=args.commission_rate,
            min_commission=args.min_commission,
            slippage=args.slippage,
        ),
    )
    result = run(close, open_, strategy, config, benchmark_close=close[BENCHMARK],
                 exec_close=exec_close, exec_open=exec_open)

    print(f"  区间 {result.nav.index[0]:%Y-%m-%d} ~ {result.nav.index[-1]:%Y-%m-%d}，{len(result.nav)} 个交易日")

    print("\n== 3. 绩效 ==")
    metrics = compute_metrics(result.nav, result.trades)
    print_report(metrics, result.nav)

    # 归档
    report_dir = Path(__file__).parent / "reports"
    report_dir.mkdir(exist_ok=True)
    name_map = {code: info["name"] for code, info in UNIVERSE.items()}
    out = report_dir / f"{pd.Timestamp.now():%Y%m%d_%H%M%S}_top{args.top_n}_lb{args.lookback}_{args.freq}.json"
    out.write_text(
        json.dumps(
            {
                "params": vars(args),
                "metrics": {k: (v if not isinstance(v, float) else round(v, 6)) for k, v in metrics.items()},
                "trades": result.trades,
                "nav": [
                    {
                        "date": str(d.date()),
                        "value": round(row["value"], 2),
                        "benchmark": round(row["benchmark"], 2) if pd.notna(row.get("benchmark")) else None,
                    }
                    for d, row in result.nav.iterrows()
                ],
            },
            ensure_ascii=False,
            indent=1,
        ),
        encoding="utf-8",
    )
    print(f"报告已保存: {out}")


if __name__ == "__main__":
    main()
