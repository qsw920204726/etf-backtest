"""数据层：akshare 拉取 ETF 日线 → 本地 parquet 缓存

要点：
- 用后复权(hfq)：后复权历史价格不会因新的分红除权而改变，可安全增量更新；
  前复权(qfq)每次分红后整条历史都会变，长回测必须全量重拉，且早期价格可能为负。
- 缓存目录 data_cache/，每只 ETF 一个 parquet 文件。
- 增量更新：只拉缓存最后日期之后的数据；用 refresh=True 强制全量重拉。
"""

import os
import time
from pathlib import Path

import akshare as ak

# 东财/新浪接口是纯国内源，走系统代理(Clash 等)常被断连；对这些域名强制直连
os.environ["NO_PROXY"] = (
    os.environ.get("NO_PROXY", "").strip(",")
    + ",eastmoney.com,push2.eastmoney.com,sina.com.cn,sinajs.cn"
).strip(",")
import pandas as pd

from data.universe import UNIVERSE

CACHE_DIR = Path(__file__).parent.parent / "data_cache"

# akshare 中文列名 → 英文
_COLUMN_MAP = {
    "日期": "date",
    "开盘": "open",
    "收盘": "close",
    "最高": "high",
    "最低": "low",
    "成交量": "volume",
    "成交额": "amount",
}


def _sina_symbol(code: str) -> str:
    return ("sh" if code.startswith("5") else "sz") + code


def _fetch_sina_daily(code: str) -> pd.DataFrame:
    """新浪 ETF 日线（仅不复权真实价），作为东财的兜底源"""
    df = ak.fund_etf_hist_sina(symbol=_sina_symbol(code))
    if df is None or df.empty:
        return pd.DataFrame()
    df = df[["date", "open", "high", "low", "close", "volume", "amount"]].copy()
    df["date"] = pd.to_datetime(df["date"])
    return df.set_index("date").sort_index()


def _fetch_tx_hfq(code: str) -> pd.DataFrame:
    """腾讯 ETF 日线（后复权），作为东财 hfq 的兜底源。
    与东财 hfq 收益率相关性 ~0.9998（价格保留3位小数有微小量化差）。"""
    df = ak.stock_zh_a_hist_tx(
        symbol=_sina_symbol(code), start_date="20100101", end_date="20991231", adjust="hfq"
    )
    if df is None or df.empty:
        return pd.DataFrame()
    df = df[["date", "open", "high", "low", "close", "volume", "amount"]].copy()
    df["date"] = pd.to_datetime(df["date"])
    return df.set_index("date").sort_index()


def _merge_rescaled(old: pd.DataFrame | None, new: pd.DataFrame) -> pd.DataFrame:
    """把新数据段合并进缓存。若新旧来自不同源（东财/腾讯后复权锚点不同），
    以最后一个共同交易日为基准把新段等比缩放到旧锚点，避免接缝日假跳变。"""
    if old is None or old.empty:
        return new
    ref = old.index.max()
    if ref in new.index:
        ratio = old["close"].loc[ref] / new["close"].loc[ref]
        if abs(ratio - 1) > 1e-9:
            new = new.copy()
            for col in ("open", "high", "low", "close"):
                new[col] *= ratio
        return pd.concat([old, new[new.index > ref]])
    return pd.concat([old, new[~new.index.duplicated(keep="last")]]).sort_index()


def fetch_one(code: str, refresh: bool = False, adjust: str = "hfq",
              local_only: bool = False) -> pd.DataFrame:
    """拉取单只 ETF 日线，返回带 date 索引的 DataFrame。
    adjust: 'hfq' 后复权 / '' 真实价；local_only: 只读缓存不联网（服务启动用）。"""
    suffix = "" if adjust == "hfq" else "_raw"
    cache_file = CACHE_DIR / f"{code}{suffix}.parquet"

    start_date = "20100101"
    old = None
    if cache_file.exists() and not refresh:
        old = pd.read_parquet(cache_file)
        if not old.empty:
            last = old.index.max()
            start_date = (last + pd.Timedelta(days=1)).strftime("%Y%m%d")
            if start_date > pd.Timestamp.now().strftime("%Y%m%d") or local_only:
                return old  # 已是最新 / 仅本地模式

    df = None
    for attempt in range(3):  # 东财偶发断连，重试
        try:
            df = ak.fund_etf_hist_em(
                symbol=code,
                period="daily",
                start_date=start_date,
                end_date="20991231",
                adjust=adjust,
            )
            break
        except Exception:
            if attempt == 2:
                # 兜底源：真实价→新浪，后复权→腾讯
                fallback = _fetch_sina_daily if adjust == "" else _fetch_tx_hfq
                try:
                    alt = fallback(code)
                    if alt is not None and not alt.empty:
                        print("(东财限速，兜底源)", end=" ")
                        merged = _merge_rescaled(old, alt)
                        CACHE_DIR.mkdir(exist_ok=True)
                        merged.to_parquet(cache_file)
                        return merged
                except Exception:
                    pass
                if old is not None and not old.empty:
                    print("(网络失败，用本地缓存)", end=" ")
                    return old
                raise
    if df is None or df.empty:
        return old if old is not None else pd.DataFrame()

    df = df.rename(columns=_COLUMN_MAP)[list(_COLUMN_MAP.values())]
    df["date"] = pd.to_datetime(df["date"])
    df = df.set_index("date").sort_index()

    if old is not None and not old.empty:
        df = _merge_rescaled(old, df)

    CACHE_DIR.mkdir(exist_ok=True)
    df.to_parquet(cache_file)
    return df


def load_all(refresh: bool = False, quiet: bool = False, adjust: str = "hfq",
             local_only: bool = False) -> dict[str, pd.DataFrame]:
    """拉取/更新整个轮动池，返回 {code: DataFrame}。
    adjust='' 为真实价（下单口径）；local_only=True 只读缓存不联网（服务秒启动）。"""
    data = {}
    for i, (code, info) in enumerate(UNIVERSE.items(), 1):
        if i > 1 and not local_only:
            time.sleep(0.5)  # 避免请求过快被东财限速
        if not quiet:
            print(f"  [{i:>2}/{len(UNIVERSE)}] {code} {info['name']} ...", end=" ")
        try:
            df = fetch_one(code, refresh=refresh, adjust=adjust, local_only=local_only)
            if df is None or df.empty:
                print("无数据(未上市?)")
                continue
            if not quiet:
                print(f"{df.index.min():%Y-%m-%d} ~ {df.index.max():%Y-%m-%d}  {len(df)}条")
            data[code] = df
        except Exception as e:
            print(f"失败: {e}")
    return data


def build_panels(data: dict[str, pd.DataFrame]) -> tuple[pd.DataFrame, pd.DataFrame]:
    """把各 ETF 拼成对齐日历的面板：(close, open)。

    个别 ETF 停牌缺行情时用前值填充（估值用），动量计算在策略侧 dropna。
    """
    close = pd.DataFrame({code: df["close"] for code, df in data.items()}).sort_index()
    open_ = pd.DataFrame({code: df["open"] for code, df in data.items()}).sort_index()
    close = close.ffill()
    open_ = open_.reindex(close.index).ffill()
    return close, open_


def build_exec_panels(
    hfq: dict[str, pd.DataFrame], raw: dict[str, pd.DataFrame]
) -> tuple[pd.DataFrame, pd.DataFrame] | tuple[None, None]:
    """执行价面板：后复权面板 × 每只固定系数（最新真实价/最新后复权价）。

    为什么不直接用真实价回测：A 股 ETF 有份额合并/拆分，真实价会把这些
    公司行为算成暴跌/暴涨；乘固定系数既保持总回报口径正确，又让近端
    成交价与行情软件一致（远端价格等比缩放，不影响任何收益率计算）。
    """
    if not hfq or not raw:
        return None, None
    codes = [c for c in hfq if c in raw]
    if not codes:
        return None, None
    close = pd.DataFrame({c: hfq[c]["close"] for c in codes}).sort_index().ffill()
    open_ = pd.DataFrame({c: hfq[c]["open"] for c in codes}).sort_index().ffill()
    open_ = open_.reindex(close.index).ffill()
    ratio = {c: raw[c]["close"].iloc[-1] / close[c].iloc[-1] for c in codes}
    for c in codes:
        close[c] *= ratio[c]
        open_[c] *= ratio[c]
    return close, open_
