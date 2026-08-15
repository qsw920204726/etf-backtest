"""策略基类

生命周期：
  prepare(close)  回测开始前调用一次，close 为全量收盘价面板（策略不应在这里"偷看"未来做决策，
                  只允许做与日期无关的预计算，如生成调仓日历）
  on_bar(date, portfolio_value) -> dict[str, float] | None
      每个交易日收盘后调用。返回目标权重 dict 则次日开盘调仓，返回 None 则不动。
      防未来函数：引擎保证策略只能访问 self.close.loc[:date] 的数据。
"""


class Strategy:
    def prepare(self, close) -> None:
        self.close = close

    def on_bar(self, date, portfolio_value: float) -> dict[str, float] | None:
        raise NotImplementedError
