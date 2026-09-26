"""日内 DualThrust 策略（不跨日）。

基于 vnpy 内置 `DualThrustStrategy`（vnpy_ctastrategy/strategies/dual_thrust_strategy.py）改造，
三处针对日内交易的改动：

1. **平仓时间参数化**：日盘 `day_exit_time`、夜盘 `night_exit_time`（默认 14:55 / 22:55）
2. **收盘前 N 分钟停止开新仓**：`entry_cutoff`（默认 5 分钟）
3. **日盘、夜盘各自收盘前强制平仓，保证不持仓过夜**

第 3 条同时也是修 bug：原版只有一个写死的 `exit_time = 14:55`，而夜盘 bar 的时间（21:00~23:00）
全部大于 14:55，会被判为「已到收盘」，导致**夜盘一律不交易、且每根夜盘 bar 都尝试平仓**。

参数说明：
- k1 / k2：上下轨宽度系数，`上轨 = 今开 + k1 × 昨日振幅`、`下轨 = 今开 - k2 × 昨日振幅`
- fixed_size：每次下单手数
- day_exit_time / night_exit_time：日盘 / 夜盘强制平仓时刻（"HH:MM"，24 小时制）
  **应比所选 K 线周期提前至少一根 bar**：平仓单是挂在当前 bar 上、由后续 bar 成交的，
  如果设在最后一根 bar（如 15 分钟线的 15:00 或 23:00），这一单要等次日开盘才成交，
  等于事实上隔夜。默认 14:45 / 22:45 对 1m / 5m / 15m 都安全。
- entry_cutoff：距平仓时刻不足该分钟数时，不再开新仓（只平不开）

注意：夜盘跨零点的品种（如部分品种交易到 01:00 或 02:30），请把 night_exit_time 设成对应的
"00:55" / "02:25"，判定逻辑以"20:00 之后或凌晨 4:00 之前"视为夜盘。
"""
from datetime import time

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


def parse_time(text: str, default: time) -> time:
    """把 "14:55" 解析为 time，失败则用默认值。"""
    try:
        hour, minute = text.strip().split(":")
        return time(int(hour), int(minute))
    except Exception:
        return default


def shift_time(value: time, minutes: int) -> time:
    """返回 value 往前推 minutes 分钟后的时刻（用于计算"停止开仓"时间）。"""
    total: int = value.hour * 60 + value.minute - minutes
    total = max(total, 0)
    return time(total // 60, total % 60)


class DualThrustIntradayStrategy(CtaTemplate):
    """日内 DualThrust：日盘/夜盘各自收盘前平仓，不跨日。"""

    author = "kellywang"

    # ---- 参数 ----
    k1: float = 0.4
    k2: float = 0.6
    fixed_size: int = 1
    day_exit_time: str = "14:45"
    night_exit_time: str = "22:45"
    entry_cutoff: int = 5

    # ---- 变量 ----
    day_range: float = 0
    long_entry: float = 0
    short_entry: float = 0
    day_open: float = 0
    day_high: float = 0
    day_low: float = 0
    long_entered: bool = False
    short_entered: bool = False

    parameters = [
        "k1",
        "k2",
        "fixed_size",
        "day_exit_time",
        "night_exit_time",
        "entry_cutoff",
    ]
    variables = [
        "day_range",
        "long_entry",
        "short_entry",
    ]

    def on_init(self) -> None:
        """策略初始化"""
        self.write_log("策略初始化")

        self.bg: BarGenerator = BarGenerator(self.on_bar)
        self.am: ArrayManager = ArrayManager()
        self.bars: list[BarData] = []

        # 解析平仓时刻（不放进 variables，避免 time 对象无法序列化）
        self._day_exit: time = parse_time(self.day_exit_time, time(14, 55))
        self._night_exit: time = parse_time(self.night_exit_time, time(22, 55))
        self.write_log(
            f"日内参数：日盘 {self._day_exit:%H:%M} 平仓、夜盘 {self._night_exit:%H:%M} 平仓，"
            f"收盘前 {self.entry_cutoff} 分钟停止开仓"
        )

        self.load_bar(10)

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

        # 维护最近两根 bar，用于判断是否跨日
        self.bars.append(bar)
        if len(self.bars) <= 2:
            return
        else:
            self.bars.pop(0)
        last_bar: BarData = self.bars[-2]

        # ---- 跨日：用前一日振幅计算今日上下轨 ----
        if last_bar.datetime.date() != bar.datetime.date():
            if self.day_high:
                self.day_range = self.day_high - self.day_low
                self.long_entry = bar.open_price + self.k1 * self.day_range
                self.short_entry = bar.open_price - self.k2 * self.day_range

            self.day_open = bar.open_price
            self.day_high = bar.high_price
            self.day_low = bar.low_price

            self.long_entered = False
            self.short_entered = False
        else:
            self.day_high = max(self.day_high, bar.high_price)
            self.day_low = min(self.day_low, bar.low_price)

        if not self.day_range:
            return

        bar_time: time = bar.datetime.time()
        exit_time: time = self.exit_time_for(bar_time)

        # ---- 1) 到达平仓时刻：强制平掉，不跨日 ----
        if bar_time >= exit_time:
            if self.pos > 0:
                self.sell(bar.close_price * 0.99, abs(self.pos))
            elif self.pos < 0:
                self.cover(bar.close_price * 1.01, abs(self.pos))
            self.put_event()
            return

        # ---- 2) 收盘前 entry_cutoff 分钟：只平不开 ----
        if bar_time >= shift_time(exit_time, self.entry_cutoff):
            self.put_event()
            return

        # ---- 3) 盘中：突破挂单 / 反向跟踪 ----
        if self.pos == 0:
            if bar.close_price > self.day_open:
                if not self.long_entered:
                    self.buy(self.long_entry, self.fixed_size, stop=True)
            else:
                if not self.short_entered:
                    self.short(self.short_entry, self.fixed_size, stop=True)

        elif self.pos > 0:
            self.long_entered = True

            self.sell(self.short_entry, self.fixed_size, stop=True)

            if not self.short_entered:
                self.short(self.short_entry, self.fixed_size, stop=True)

        elif self.pos < 0:
            self.short_entered = True

            self.cover(self.long_entry, self.fixed_size, stop=True)

            if not self.long_entered:
                self.buy(self.long_entry, self.fixed_size, stop=True)

        self.put_event()

    def exit_time_for(self, bar_time: time) -> time:
        """返回该 bar 所属交易时段的强制平仓时刻（夜盘 20:00 后或凌晨 4:00 前）。"""
        if bar_time.hour >= 20 or bar_time.hour < 4:
            return self._night_exit
        return self._day_exit

    def on_order(self, order: OrderData) -> None:
        """委托数据更新"""
        pass

    def on_trade(self, trade: TradeData) -> None:
        """成交数据更新"""
        self.put_event()

    def on_stop_order(self, stop_order: StopOrder) -> None:
        """停止单更新"""
        pass
