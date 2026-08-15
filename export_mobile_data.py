"""导出移动端离线数据快照 → app/assets/data.js

数据含：交易日历、各ETF后复权开/收盘（3位小数）、最新真实价（信号参考价）。
体积约 0.5MB，打进 APK 实现完全离线回测。

用法：py export_mobile_data.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from data.fetcher import build_panels, load_all
from data.universe import BENCHMARK, UNIVERSE


def arr(df, code):
    return [None if v != v else round(float(v), 3) for v in df[code]]


def main():
    data = load_all(quiet=True)
    raw = load_all(quiet=True, adjust="")
    close, open_ = build_panels(data)

    # 用主日历（所有 ETF 日期并集后对齐），缺上市前数据用 null（JS 侧跳过）
    dates = [str(d.date()) for d in close.index]
    names = {c: i["name"] for c, i in UNIVERSE.items()}
    raw_last = {c: round(float(raw[c]["close"].iloc[-1]), 3) for c in data if c in raw}

    js = (
        "window.ETF_DATA={\n"
        f'asOf:"{dates[-1]}",bench:"{BENCHMARK}",\n'
        f'dates:{_js_list(dates)},\ncodes:{_js_list(list(data.keys()))},\n'
        f"names:{_js_dict(names)},\nrawLast:{_js_dict(raw_last)},\n"
        f"close:{_js_nested(close, data)},\nopen:{_js_nested(open_, data)}\n}};\n"
    )
    out = Path(__file__).parent / "app" / "assets" / "data.js"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(js, encoding="utf-8")
    print(f"已导出 {out} ({out.stat().st_size/1024:.0f} KB, 截至 {dates[-1]}, {len(dates)}个交易日)")


def _js_list(items):
    return "[" + ",".join(f'"{i}"' if isinstance(i, str) else str(i) for i in items) + "]"


def _js_dict(d):
    return "{" + ",".join(f'"{k}":{v if not isinstance(v,str) else chr(34)+v+chr(34)}' for k, v in d.items()) + "}"


def _js_nested(panel, data):
    parts = []
    for c in data:
        vals = ",".join("null" if v is None else str(v) for v in arr(panel, c))
        parts.append(f'"{c}":[{vals}]')
    return "{" + ",".join(parts) + "}"


if __name__ == "__main__":
    main()
