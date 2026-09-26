"""日内区间回归 / 单边突破 双模式策略（按市场状态切换），不跨日。

**核心思路：先把"震荡市 / 单边市"区分开，再各用各的打法。**

市场状态判定（每根 K 线更新）：

    效率比 ER = |收盘_t − 收盘_{t−n}| / Σ|逐根涨跌|      （→1 单边，→0 震荡）
    ADX(14)                                              （>25 趋势，<20 震荡）

    ER < er_threshold 且 ADX < adx_threshold  →  震荡市
    否则                                        →  单边市

两种打法：

    震荡市（均值回归）：在区间边缘**逆势**接单，回到中轴止盈
        - 下轨挂限价买、上轨挂限价卖（OCO，先成交一个就撤另一个）
        - 持多：中轴挂限价止盈；下轨外侧 (k×ATR) 挂停止单止损
        - 持空：中轴挂限价止盈；上轨外侧 (k×ATR) 挂停止单止损

    单边市（突破跟随）：在区间边缘**顺势**追
        - 上轨挂买入停止单、下轨挂卖出停止单（OCO）
        - 持仓：反向轨道平仓 + trail_percent 移动止损

`regime_mode` 选择参与哪种状态："只做震荡" / "只做单边" / "两种都做"。

**执行顺序很关键**（顺序错了会隔夜）：
    1. 更新指标、判定状态
    2. **无条件**执行日内时间闸门（到点强平）—— 与当前状态/模式无关
    3. 收盘前 entry_cutoff 分钟内只管理仓位、不开新仓
    4. 状态与模式匹配时按对应打法执行；不匹配时仅挂保护性止损
"""
from datetime import time

import numpy as np

from vnpy_ctastrategy import (
    ArrayManager,
    BarData,
    BarGenerator,
    CtaTemplate,
    OrderData,
    StopOrder,
    TickData,
    TradeData,
)
from vnpy.trader.constant import Direction


def parse_time(text: str, default: time) -> time:
    """把 "14:45" 解析为 time，失败则用默认值。"""
    try:
        hour, minute = text.strip().split(":")
        return time(int(hour), int(minute))
    except Exception:
        return default


def shift_time(value: time, minutes: int) -> time:
    """返回 value 往前推 minutes 分钟后的时刻。"""
    total: int = value.hour * 60 + value.minute - minutes
    total = max(total, 0)
    return time(total // 60, total % 60)


class RangeReversionIntradayStrategy(CtaTemplate):
    """震荡市做区间回归、单边市做突破跟随，按状态切换；日内平仓不跨日。"""

    author = "kellywang"

    # ---- 区间参数 ----
    band_window: int = 20
    band_dev: float = 2.0

    # ---- 市场状态判定 ----
    er_window: int = 20
    er_threshold: float = 0.35
    adx_window: int = 14
    adx_threshold: float = 25.0
    regime_mode: str = "两种都做"          # 只做震荡 / 只做单边 / 两种都做

    # ---- 交易参数 ----
    fixed_size: int = 1
    stop_atr_multiplier: float = 1.5
    trail_percent: float = 0.8

    # ---- 日内参数 ----
    day_exit_time: str = "14:45"
    night_exit_time: str = "22:45"
    entry_cutoff: int = 5

    # ---- 变量（界面显示）----
    regime: str = "未知"
    er_value: float = 0
    adx_value: float = 0
    boll_up: float = 0
    boll_mid: float = 0
    boll_down: float = 0
    atr_value: float = 0
    intra_trade_high: float = 0
    intra_trade_low: float = 0

    parameters = [
        "band_window",
        "band_dev",
        "er_window",
        "er_threshold",
        "adx_window",
        "adx_threshold",
        "regime_mode",
        "fixed_size",
        "stop_atr_multiplier",
        "trail_percent",
        "day_exit_time",
        "night_exit_time",
        "entry_cutoff",
    ]
    variables = [
        "regime",
        "er_value",
        "adx_value",
        "boll_up",
        "boll_mid",
        "boll_down",
        "atr_value",
    ]

    def on_init(self) -> None:
        """策略初始化"""
        self.write_log("策略初始化")

        self.bg: BarGenerator = BarGenerator(self.on_bar)
        self.am: ArrayManager = ArrayManager()

        self._day_exit: time = parse_time(self.day_exit_time, time(14, 45))
        self._night_exit: time = parse_time(self.night_exit_time, time(22, 45))
        self.orderids: list[str] = []

        # 归一化 regime_mode：界面上是自由文本框，写错字不应导致"完全不下单"
        self._mode: str = self.normalize_mode(self.regime_mode)
        if self._mode != str(self.regime_mode).strip():
            self.write_log(f"regime_mode「{self.regime_mode}」无法识别，已按「两种都做」处理")

        self.write_log(
            f"状态判定：ER({self.er_window}) < {self.er_threshold} 且 ADX({self.adx_window}) < "
            f"{self.adx_threshold} → 震荡（区间回归），否则 → 单边（突破跟随）；"
            f"参与模式：{self._mode}；日内平仓 日盘 {self._day_exit:%H:%M} / 夜盘 {self._night_exit:%H:%M}"
        )

        self.load_bar(20)

    def on_start(self) -> None:
        """策略启动"""
        self.write_log("策略启动")

    def on_stop(self) -> None:
        """策略停止"""
        self.write_log("策略停止")

    def on_tick(self, tick: TickData) -> None:
        """Tick 数据更新"""
        self.bg.update_tick(tick)

    def on_bar(self, bar: BarData) -> None:
        """K 线数据更新"""
        self.cancel_all()
        self.orderids.clear()

        am: ArrayManager = self.am
        am.update_bar(bar)
        if not am.inited:
            return

        # ---- 1) 指标与市场状态 ----
        self.boll_up, self.boll_down = am.boll(self.band_window, self.band_dev)
        self.boll_mid: float = am.sma(self.band_window)          # type: ignore[assignment]
        self.atr_value = am.atr(14)
        self.adx_value = am.adx(self.adx_window)
        self.er_value = self.calc_efficiency_ratio(am.close, self.er_window)

        self.regime = "震荡" if (
            self.er_value < self.er_threshold and self.adx_value < self.adx_threshold
        ) else "单边"

        # ---- 2) 日内时间闸门（无条件，先保证不跨日）----
        bar_time: time = bar.datetime.time()
        exit_time: time = self.exit_time_for(bar_time)

        if bar_time >= exit_time:
            self.close_position(bar)
            self.put_event()
            return

        allow_entry: bool = bar_time < shift_time(exit_time, self.entry_cutoff)

        # ---- 3) 状态与模式是否匹配 ----
        trade_range: bool = self.regime == "震荡" and self._mode in ("只做震荡", "两种都做")
        trade_trend: bool = self.regime == "单边" and self._mode in ("只做单边", "两种都做")

        # ---- 4) 执行（不匹配时只挂保护单、不开新仓）----
        if trade_range:
            self.manage_range(bar, allow_entry)
        elif trade_trend:
            self.manage_trend(bar, allow_entry)
        elif self.pos != 0:
            self.protect_position(bar)

        self.put_event()

    # ------------------------------------------------------------------
    # 震荡市：区间边缘逆势接单，回到中轴止盈
    # ------------------------------------------------------------------
    def manage_range(self, bar: BarData, allow_entry: bool) -> None:
        """"""
        if self.pos == 0:
            if not allow_entry:
                return

            # 区间边缘逆势挂限价单（OCO：先成交一边，另一边在 on_trade 撤掉）
            self.orderids += self.buy(self.boll_down, self.fixed_size)
            self.orderids += self.short(self.boll_up, self.fixed_size)

        elif self.pos > 0:
            self.intra_trade_high = max(self.intra_trade_high, bar.high_price)
            self.intra_trade_low = bar.low_price

            self.orderids += self.sell(self.boll_mid, abs(self.pos))       # 回到中轴止盈
            stop_price: float = self.boll_down - self.stop_atr_multiplier * self.atr_value
            self.orderids += self.sell(stop_price, abs(self.pos), stop=True)   # 破位止损

        elif self.pos < 0:
            self.intra_trade_high = bar.high_price
            self.intra_trade_low = min(self.intra_trade_low, bar.low_price)

            self.orderids += self.cover(self.boll_mid, abs(self.pos))
            stop_price = self.boll_up + self.stop_atr_multiplier * self.atr_value
            self.orderids += self.cover(stop_price, abs(self.pos), stop=True)

    # ------------------------------------------------------------------
    # 单边市：区间边缘顺势追，反轨平仓 + 移动止损
    # ------------------------------------------------------------------
    def manage_trend(self, bar: BarData, allow_entry: bool) -> None:
        """"""
        if self.pos == 0:
            if not allow_entry:
                return

            self.intra_trade_high = bar.high_price
            self.intra_trade_low = bar.low_price

            if bar.close_price > self.boll_mid:
                self.orderids += self.buy(self.boll_up, self.fixed_size, stop=True)
            else:
                self.orderids += self.short(self.boll_down, self.fixed_size, stop=True)

        elif self.pos > 0:
            self.intra_trade_high = max(self.intra_trade_high, bar.high_price)
            self.intra_trade_low = bar.low_price

            self.orderids += self.sell(self.boll_down, abs(self.pos), stop=True)
            self.orderids += self.sell(
                self.intra_trade_high * (1 - self.trail_percent / 100),
                abs(self.pos),
                stop=True,
            )

        elif self.pos < 0:
            self.intra_trade_high = bar.high_price
            self.intra_trade_low = min(self.intra_trade_low, bar.low_price)

            self.orderids += self.cover(self.boll_up, abs(self.pos), stop=True)
            self.orderids += self.cover(
                self.intra_trade_low * (1 + self.trail_percent / 100),
                abs(self.pos),
                stop=True,
            )

    # ------------------------------------------------------------------
    # 状态与模式不匹配但仍持仓时：只挂移动止损
    # ------------------------------------------------------------------
    def protect_position(self, bar: BarData) -> None:
        """"""
        if self.pos > 0:
            self.intra_trade_high = max(self.intra_trade_high, bar.high_price)
            self.orderids += self.sell(
                self.intra_trade_high * (1 - self.trail_percent / 100),
                abs(self.pos),
                stop=True,
            )
        elif self.pos < 0:
            self.intra_trade_low = min(self.intra_trade_low, bar.low_price)
            self.orderids += self.cover(
                self.intra_trade_low * (1 + self.trail_percent / 100),
                abs(self.pos),
                stop=True,
            )

    # ------------------------------------------------------------------
    # 工具
    # ------------------------------------------------------------------
    def close_position(self, bar: BarData) -> None:
        """强制平掉全部持仓（用激进限价单保证成交）"""
        if self.pos > 0:
            self.sell(bar.close_price * 0.99, abs(self.pos))
        elif self.pos < 0:
            self.cover(bar.close_price * 1.01, abs(self.pos))

    @staticmethod
    def normalize_mode(text: str) -> str:
        """把 regime_mode 归一化成三种标准取值之一（写错字时按"两种都做"）。"""
        mode: str = str(text).strip().lower()
        if mode in ("只做震荡", "震荡", "range", "reversion", "revert"):
            return "只做震荡"
        if mode in ("只做单边", "单边", "trend", "breakout"):
            return "只做单边"
        return "两种都做"

    @staticmethod
    def calc_efficiency_ratio(close: np.ndarray, n: int) -> float:
        """Kaufman 效率比：|净位移| / |路径总长|，越接近 1 越单边。"""
        if len(close) < n + 1:
            return 0.0

        window: np.ndarray = close[-(n + 1):]
        net_change: float = abs(float(window[-1] - window[0]))
        path: float = float(np.abs(np.diff(window)).sum())

        return net_change / path if path else 0.0

    def exit_time_for(self, bar_time: time) -> time:
        """夜盘（20:00 后或凌晨 4:00 前）用夜盘平仓时刻，否则用日盘。"""
        if bar_time.hour >= 20 or bar_time.hour < 4:
            return self._night_exit
        return self._day_exit

    def on_trade(self, trade: TradeData) -> None:
        """成交后撤销本策略全部在途委托。

        包含 OCO 的反方向单，以及可能残留的保护性止损单 —— 否则止盈成交后
        那张止损单还留在市场里，后续触发会在空仓状态下反手开仓。
        """
        for orderid in self.orderids:
            self.cancel_order(orderid)
        self.orderids.clear()

        self.put_event()

    def on_order(self, order: OrderData) -> None:
        """委托数据更新"""
        pass

    def on_stop_order(self, stop_order: StopOrder) -> None:
        """停止单更新"""
        pass
