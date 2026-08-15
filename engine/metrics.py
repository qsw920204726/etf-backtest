"""绩效指标：年化/最大回撤/夏普/卡玛/月胜率/换手率 + 对比基准"""

import math

import pandas as pd

TRADING_DAYS = 252


def _annual_return(nav: pd.Series) -> float:
    days = len(nav)
    if days < 2 or nav.iloc[0] <= 0:
        return 0.0
    return (nav.iloc[-1] / nav.iloc[0]) ** (TRADING_DAYS / days) - 1


def _max_drawdown(nav: pd.Series) -> tuple[float, pd.Timestamp, pd.Timestamp]:
    peak = nav.cummax()
    dd = nav / peak - 1
    trough = dd.idxmin()
    peak_date = nav.loc[:trough].idxmax()
    return dd.min(), peak_date, trough


def compute_metrics(nav: pd.DataFrame, trades: list[dict]) -> dict:
    value = nav["value"].astype(float)
    ret = value.pct_change().dropna()

    total_return = value.iloc[-1] / value.iloc[0] - 1
    ann_return = _annual_return(value)
    maxdd, dd_peak, dd_trough = _max_drawdown(value)
    ann_vol = ret.std() * math.sqrt(TRADING_DAYS)
    sharpe = (ret.mean() / ret.std() * math.sqrt(TRADING_DAYS)) if ret.std() > 0 else 0.0
    calmar = ann_return / abs(maxdd) if maxdd != 0 else 0.0

    # 月胜率
    monthly = value.resample("ME").last().pct_change().dropna()
    win_rate = (monthly > 0).mean() if len(monthly) else 0.0

    # 年化换手率（单边，按买入额计）
    years = len(value) / TRADING_DAYS
    buy_value = sum(t["value"] for t in trades if t["side"] == "buy")
    turnover = buy_value / value.mean() / years if years > 0 else 0.0

    total_commission = sum(t["commission"] for t in trades)

    m = {
        "期末资产": float(value.iloc[-1]),
        "总收益": total_return,
        "年化收益": ann_return,
        "年化波动": ann_vol,
        "最大回撤": maxdd,
        "回撤区间": f"{dd_peak:%Y-%m-%d} ~ {dd_trough:%Y-%m-%d}",
        "夏普比率": sharpe,
        "卡玛比率": calmar,
        "月胜率": win_rate,
        "年化换手(单边)": turnover,
        "交易笔数": len(trades),
        "总佣金": total_commission,
    }

    if "benchmark" in nav.columns:
        bench = nav["benchmark"].astype(float)
        bench_ret = bench.pct_change().dropna()
        bench_maxdd = _max_drawdown(bench)[0]
        m["基准总收益"] = bench.iloc[-1] / bench.iloc[0] - 1
        m["基准年化"] = _annual_return(bench)
        m["基准最大回撤"] = bench_maxdd
        m["超额年化"] = ann_return - m["基准年化"]
        m["基准夏普"] = (
            bench_ret.mean() / bench_ret.std() * math.sqrt(TRADING_DAYS)
            if bench_ret.std() > 0
            else 0.0
        )

    return m


def print_report(metrics: dict, nav: pd.DataFrame) -> None:
    def pct(x):
        return f"{x * 100:.2f}%"

    print("\n" + "=" * 52)
    print("  回测报告  ({} ~ {})".format(nav.index[0].date(), nav.index[-1].date()))
    print("=" * 52)
    print(f"  期末资产     {nav['value'].iloc[-1]:>14,.0f}")
    print(f"  总收益       {pct(metrics['总收益']):>14}")
    print(f"  年化收益     {pct(metrics['年化收益']):>14}")
    print(f"  年化波动     {pct(metrics['年化波动']):>14}")
    print(f"  最大回撤     {pct(metrics['最大回撤']):>14}   {metrics['回撤区间']}")
    print(f"  夏普比率     {metrics['夏普比率']:>14.2f}")
    print(f"  卡玛比率     {metrics['卡玛比率']:>14.2f}")
    print(f"  月胜率       {pct(metrics['月胜率']):>14}")
    print(f"  年化换手     {metrics['年化换手(单边)']:>14.1f} 倍")
    print(f"  交易笔数     {metrics['交易笔数']:>14d}")
    print(f"  总佣金       {metrics['总佣金']:>14,.0f} 元")
    if "基准年化" in metrics:
        print("-" * 52)
        print(f"  基准年化     {pct(metrics['基准年化']):>14}   (买入持有)")
        print(f"  基准回撤     {pct(metrics['基准最大回撤']):>14}")
        print(f"  基准夏普     {metrics['基准夏普']:>14.2f}")
        print(f"  超额年化     {pct(metrics['超额年化']):>14}")
    print("=" * 52 + "\n")
