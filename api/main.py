"""FastAPI 服务：接收回测参数 → 跑回测 → 返回 JSON；托管报告页面

启动：py -m uvicorn api.main:app --port 8321
"""

import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import pandas as pd

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from data.fetcher import build_exec_panels, build_panels, load_all
from data.universe import BENCHMARK, UNIVERSE
from engine.broker import BrokerConfig
from engine.engine import BacktestConfig, run
from engine.metrics import compute_metrics
from strategies.momentum import MomentumRotation

app = FastAPI(title="ETF 回测系统")

# 进程启动只读本地缓存（秒级启动，不联网）；联网更新走 /api/refresh
_data = load_all(quiet=True, local_only=True)
if not _data:  # 缓存全空（首次安装）才联网
    _data = load_all(quiet=True)
CLOSE, OPEN = build_panels(_data) if _data else (None, None)
_raw = load_all(quiet=True, adjust="", local_only=True)
CLOSE_RAW, OPEN_RAW = build_exec_panels(_data, _raw)


@app.post("/api/refresh")
def refresh_data():
    """增量更新全部 ETF 行情并重建面板"""
    global _data, CLOSE, OPEN, _raw, CLOSE_RAW, OPEN_RAW
    new_data = load_all(quiet=True)
    if not new_data:
        raise HTTPException(503, "数据更新失败（网络不可用且无本地缓存）")
    _data = new_data
    CLOSE, OPEN = build_panels(_data)
    _raw = load_all(quiet=True, adjust="")
    CLOSE_RAW, OPEN_RAW = build_exec_panels(_data, _raw)
    return {
        "updated": len(_data),
        "last_date": str(CLOSE.index[-1].date()),
        "exec_price": "real" if CLOSE_RAW is not None else "adjusted(fallback)",
    }


@app.get("/api/signal")
def signal(top_n: int = 3, lookback: int = 20, freq: str = "W",
           abs_filter: bool = True, risk_adjusted: bool = True):
    """按当前参数给出最新交易日的操作信号"""
    if not _data:
        raise HTTPException(503, "数据未加载")
    strategy = MomentumRotation(
        top_n=top_n, lookback=lookback, freq=freq,
        abs_filter=abs_filter, risk_adjusted=risk_adjusted,
    )
    strategy.prepare(CLOSE)
    today = CLOSE.index[-1]
    name_of = {c: i["name"] for c, i in UNIVERSE.items()}

    # 数据的最后一个"周期末"可能是不完整周期（如月中截止被误当月末），
    # 只有真正走完的周期才能作为调仓信号日
    genuine = [d for d in sorted(strategy._rebalance_dates)
               if d < today or _period_complete(today, freq)]
    signal_date = genuine[-1] if genuine else None
    is_rebalance_day = signal_date == today
    targets = strategy.on_bar(signal_date, 0.0) if signal_date is not None else None

    if is_rebalance_day and targets:
        action = "rebalance"
    elif is_rebalance_day and targets is not None and not targets:
        action = "cash"
    else:
        action = "hold"  # 显示"当前应持有"（由最近一次真实调仓确立）
    holdings = None
    if targets:
        last_raw = CLOSE_RAW.iloc[-1] if CLOSE_RAW is not None else None
        holdings = [
            {
                "code": c,
                "name": name_of.get(c, ""),
                "weight": round(w, 3),
                "ref_price": round(float(last_raw[c]), 3) if last_raw is not None else None,
            }
            for c, w in targets.items()
        ]

    # 调仓日：与上期持仓做差集，直接给出 卖出/继续持有/买入 清单
    diff = None
    if action in ("rebalance", "cash"):
        prev_dates = [d for d in genuine if d < signal_date]
        old_codes = set()
        if prev_dates:
            old = strategy.on_bar(prev_dates[-1], 0.0)
            if old:
                old_codes = set(old)
        new_codes = set(targets) if targets else set()
        last_raw = CLOSE_RAW.iloc[-1] if CLOSE_RAW is not None else None

        def _with_price(code, weight=None):
            return {
                "code": code,
                "name": name_of.get(code, ""),
                **({"weight": round(weight, 3)} if weight is not None else {}),
                "ref_price": round(float(last_raw[code]), 3) if last_raw is not None else None,
            }

        diff = {
            "sell": [_with_price(c) for c in sorted(old_codes - new_codes)],
            "keep": [_with_price(c, targets[c]) for c in sorted(old_codes & new_codes)],
            "buy": [_with_price(c, targets[c]) for c in sorted(new_codes - old_codes)],
        }

    return {
        "as_of": str(today.date()),
        "action": action,          # rebalance 次日调仓 / hold 维持 / cash 次日清仓
        "holdings": holdings,
        "diff": diff,
        "last_rebalance": str(signal_date.date()) if signal_date is not None else None,
        "next_rebalance": _next_rebalance(today, freq),
        "note": "信号按收盘价计算，实际操作应在次一交易日开盘执行",
    }


def _period_complete(as_of, freq: str) -> bool:
    """as_of 作为周期末（月末/周末）是否真实走完：周期内其后还有交易日 = 未走完"""
    if freq == "M":
        month_end = as_of + pd.offsets.MonthEnd(0)
        remaining = pd.bdate_range(as_of + pd.Timedelta(days=1), month_end)
    else:  # W：本周五（或周末）前还有交易日则未走完
        week_end = as_of + pd.Timedelta(days=4 - as_of.weekday())
        remaining = pd.bdate_range(as_of + pd.Timedelta(days=1), week_end)
    return len(remaining) == 0


def _next_rebalance(as_of, freq: str):
    """估算下一个调仓日（按工作日近似，忽略法定节假日）"""
    if freq == "M":
        month_end = as_of + pd.offsets.MonthEnd(0)
        bdays = pd.bdate_range(as_of + pd.Timedelta(days=1), month_end)
        if len(bdays):
            return str(bdays[-1].date())
        nxt = month_end + pd.offsets.MonthEnd(1)
        return str(pd.bdate_range(nxt.replace(day=1), nxt)[-1].date())
    friday = as_of + pd.Timedelta(days=4 - as_of.weekday())
    if friday > as_of and friday.weekday() >= as_of.weekday():
        # 本周五若仍在未来且数据未走完本周，则本周五；否则下周五
        bdays = pd.bdate_range(as_of + pd.Timedelta(days=1), friday)
        return str((friday if len(bdays) else friday + pd.Timedelta(weeks=1)).date())
    return str((friday + pd.Timedelta(weeks=1)).date())


class BacktestRequest(BaseModel):
    top_n: int = 3
    lookback: int = 20
    freq: str = "W"          # M 月末 / W 每周
    abs_filter: bool = True  # 绝对动量过滤
    risk_adjusted: bool = True  # 风险调整动量（收益/波动）
    start: str = "2015-01-01"
    end: str = "2099-12-31"
    initial_cash: float = 100_000.0
    commission_rate: float = 0.00025
    min_commission: float = 0.0
    slippage: float = 0.0


@app.get("/api/universe")
def universe():
    return {"benchmark": BENCHMARK, "etfs": [
        {"code": c, "name": i["name"], "category": i["category"]} for c, i in UNIVERSE.items()
    ]}


@app.post("/api/backtest")
def backtest(req: BacktestRequest):
    if not _data:
        raise HTTPException(503, "数据未加载：请先在项目目录跑 py run_backtest.py 拉取数据")
    strategy = MomentumRotation(
        top_n=req.top_n,
        lookback=req.lookback,
        freq=req.freq,
        abs_filter=req.abs_filter,
        risk_adjusted=req.risk_adjusted,
    )
    config = BacktestConfig(
        initial_cash=req.initial_cash,
        start=req.start,
        end=req.end,
        broker=BrokerConfig(
            commission_rate=req.commission_rate,
            min_commission=req.min_commission,
            slippage=req.slippage,
        ),
    )
    result = run(CLOSE, OPEN, strategy, config, benchmark_close=CLOSE[BENCHMARK],
                 exec_close=CLOSE_RAW, exec_open=OPEN_RAW)
    metrics = compute_metrics(result.nav, result.trades)

    name_of = {c: i["name"] for c, i in UNIVERSE.items()}
    nav = [
        {
            "date": str(d.date()),
            "value": round(v, 0),
            "benchmark": round(b, 0) if b == b else None,  # NaN 防护
        }
        for d, v, b in zip(
            result.nav.index, result.nav["value"], result.nav.get("benchmark")
        )
    ]
    trades = [{**t, "name": name_of.get(t["code"], "")} for t in result.trades]
    return {
        "metrics": metrics,
        "nav": nav,
        "trades": trades,
        "holdings": _holdings_blocks(result, name_of),
    }


def _holdings_blocks(result, name_of: dict) -> list[dict]:
    """把每日持仓快照合并成 {code, name, start, end, weight} 连续持仓块"""
    by_code: dict[str, list] = {}
    for h in result.holdings:
        by_code.setdefault(h["code"], []).append((h["date"], h["weight"]))

    dates = result.nav.index
    pos_of = {d: i for i, d in enumerate(dates)}
    blocks = []
    for code, snaps in by_code.items():
        snaps.sort(key=lambda x: x[0])
        start = end = snaps[0][0]
        weight = snaps[0][1]
        for d, w in snaps[1:]:
            if pos_of[d] - pos_of[end] == 1:  # 下一交易日，延续
                end, weight = d, w
            else:  # 断档 → 结算上一个块
                blocks.append({"code": code, "name": name_of.get(code, ""),
                               "start": str(start.date()), "end": str(end.date()),
                               "weight": round(weight, 3)})
                start, end, weight = d, d, w
        blocks.append({"code": code, "name": name_of.get(code, ""),
                       "start": str(start.date()), "end": str(end.date()),
                       "weight": round(weight, 3)})
    return blocks


@app.get("/")
def index():
    return FileResponse(ROOT / "web" / "index.html")


app.mount("/static", StaticFiles(directory=ROOT / "web"), name="static")
