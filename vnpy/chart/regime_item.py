"""回测 K 线图专用的 ER / ADX / 市场状态图元。

配合 `vnpy.chart.ChartWidget` 使用，把"市场状态判定"可视化到回测 K 线图上：

* `IndicatorItem`：在独立面板里画 **ER（×100，金黄）** 与 **ADX（天蓝）** 两条曲线
  （ER 原本是 0~1，乘 100 后与 ADX 的 0~100 共用一个坐标；十字光标里会给出 ER 原始值）
* `RegimeItem`：在独立面板里用彩色柱带标出**每根 K 线被判成震荡还是单边**
  （蓝 = 震荡、橙 = 单边）

判定阈值与 `strategies/range_reversion_intraday.py` 的默认值保持一致，
改这里的常量即可让图上标注随之变化。
"""
import numpy as np
import talib
from PySide6 import QtCore, QtGui

from vnpy.trader.object import BarData

from .item import ChartItem
from .manager import BarManager

# ---- 与策略默认参数保持一致的判定阈值 ----
ER_WINDOW: int = 20
ER_THRESHOLD: float = 0.35
ADX_WINDOW: int = 14
ADX_THRESHOLD: float = 25.0

# ---- 画图参数 ----
ER_COLOR: str = "#FFC000"          # ER 线：金黄
ADX_COLOR: str = "#33BBFF"         # ADX 线：天蓝
RANGE_COLOR: str = "#2E75B6"       # 震荡：蓝
TREND_COLOR: str = "#ED7D31"       # 单边：橙
PEN_WIDTH: float = 2               # 像素宽（cosmetic 画笔，不随缩放变化）
BAR_WIDTH: float = 0.5             # 状态柱带满宽，视觉上成连续色带


def calc_efficiency_ratio(close: np.ndarray, n: int) -> np.ndarray:
    """Kaufman 效率比序列：|净位移| / |路径总长|，取值 0~1。"""
    er: np.ndarray = np.full(len(close), np.nan)

    for i in range(n, len(close)):
        window: np.ndarray = close[i - n:i + 1]
        path: float = float(np.abs(np.diff(window)).sum())
        er[i] = abs(float(window[-1] - window[0])) / path if path else 0.0

    return er


def compute_series(bars: list[BarData]) -> tuple[list[float], list[float], list[str]]:
    """一次性算出 ER / ADX / 状态 三条序列（长度与 bars 对齐）。"""
    size: int = len(bars)
    if size < 2:
        return [], [], []

    close: np.ndarray = np.array([b.close_price for b in bars], dtype=float)
    high: np.ndarray = np.array([b.high_price for b in bars], dtype=float)
    low: np.ndarray = np.array([b.low_price for b in bars], dtype=float)

    er: np.ndarray = calc_efficiency_ratio(close, ER_WINDOW)

    try:
        adx: np.ndarray = talib.ADX(high, low, close, ADX_WINDOW)
    except Exception:
        adx = np.full(size, np.nan)

    regimes: list[str] = []
    for i in range(size):
        if np.isnan(er[i]) or np.isnan(adx[i]):
            regimes.append("未知")
        elif er[i] < ER_THRESHOLD and adx[i] < ADX_THRESHOLD:
            regimes.append("震荡")
        else:
            regimes.append("单边")

    return list(er), list(adx), regimes


class IndicatorItem(ChartItem):
    """ER（×100）与 ADX 曲线。"""

    def __init__(self, manager: BarManager) -> None:
        """"""
        super().__init__(manager)

        self.er_values: list[float] = []
        self.adx_values: list[float] = []
        self.regimes: list[str] = []

        # cosmetic 画笔：线宽按像素固定，缩放时不会变粗变细
        self._er_pen: QtGui.QPen = QtGui.QPen(QtGui.QColor(ER_COLOR))
        self._er_pen.setWidthF(PEN_WIDTH)
        self._er_pen.setCosmetic(True)

        self._adx_pen: QtGui.QPen = QtGui.QPen(QtGui.QColor(ADX_COLOR))
        self._adx_pen.setWidthF(PEN_WIDTH)
        self._adx_pen.setCosmetic(True)

    def update_history(self, history: list[BarData]) -> None:
        """"""
        bars: list[BarData] = self._manager.get_all_bars()
        self.er_values, self.adx_values, self.regimes = compute_series(bars)

        super().update_history(history)

    def _draw_bar_picture(self, ix: int, bar: BarData) -> QtGui.QPicture:
        """"""
        picture: QtGui.QPicture = QtGui.QPicture()
        painter: QtGui.QPainter = QtGui.QPainter(picture)

        if ix > 0 and ix < len(self.er_values):
            er0, er1 = self.er_values[ix - 1], self.er_values[ix]
            if not (np.isnan(er0) or np.isnan(er1)):
                painter.setPen(self._er_pen)
                painter.drawLine(
                    QtCore.QPointF(ix - 1, er0 * 100),
                    QtCore.QPointF(ix, er1 * 100),
                )

            adx0, adx1 = self.adx_values[ix - 1], self.adx_values[ix]
            if not (np.isnan(adx0) or np.isnan(adx1)):
                painter.setPen(self._adx_pen)
                painter.drawLine(
                    QtCore.QPointF(ix - 1, adx0),
                    QtCore.QPointF(ix, adx1),
                )

        painter.end()
        return picture

    def boundingRect(self) -> QtCore.QRectF:
        """"""
        return QtCore.QRectF(0, 0, self._manager.get_count() + 2, 100)

    def get_y_range(self, min_ix: int | None = None, max_ix: int | None = None) -> tuple[float, float]:
        """"""
        values: list[float] = []

        if self.er_values:
            start: int = min_ix if min_ix is not None else 0
            end: int = min(max_ix, len(self.er_values)) if max_ix is not None else len(self.er_values)

            for i in range(start, end):
                if not np.isnan(self.er_values[i]):
                    values.append(self.er_values[i] * 100)
                if not np.isnan(self.adx_values[i]):
                    values.append(self.adx_values[i])

        if not values:
            return 0.0, 100.0

        low: float = min(values)
        high: float = max(values)
        padding: float = max((high - low) * 0.1, 1.0)

        return max(low - padding, 0.0), min(high + padding, 100.0)

    def get_info_text(self, ix: int) -> str:
        """"""
        if not self.er_values or ix >= len(self.er_values):
            return ""

        er: float = self.er_values[ix]
        adx: float = self.adx_values[ix]

        er_text: str = "—" if np.isnan(er) else f"{er:.2f}"
        adx_text: str = "—" if np.isnan(adx) else f"{adx:.1f}"
        regime_text: str = self.regimes[ix] if ix < len(self.regimes) else "—"

        # 柱带面板已压成细条，状态文字并到这里显示
        return f"ER {er_text}  ADX {adx_text}  {regime_text}"


class RegimeItem(ChartItem):
    """用彩色柱带标出每根 K 线判定的市场状态。"""

    def __init__(self, manager: BarManager) -> None:
        """"""
        super().__init__(manager)

        self.regimes: list[str] = []
        self.er_values: list[float] = []
        self.adx_values: list[float] = []

        self._range_brush: QtGui.QBrush = QtGui.QBrush(QtGui.QColor(RANGE_COLOR))
        self._trend_brush: QtGui.QBrush = QtGui.QBrush(QtGui.QColor(TREND_COLOR))

    def update_history(self, history: list[BarData]) -> None:
        """"""
        bars: list[BarData] = self._manager.get_all_bars()
        self.er_values, self.adx_values, self.regimes = compute_series(bars)

        super().update_history(history)

    def _draw_bar_picture(self, ix: int, bar: BarData) -> QtGui.QPicture:
        """"""
        picture: QtGui.QPicture = QtGui.QPicture()
        painter: QtGui.QPainter = QtGui.QPainter(picture)

        if ix < len(self.regimes):
            regime: str = self.regimes[ix]
            if regime == "震荡":
                painter.setBrush(self._range_brush)
            elif regime == "单边":
                painter.setBrush(self._trend_brush)
            else:
                painter.end()
                return picture

            # 必须显式关掉画笔：默认画笔不会为 drawRect 填充，柱带会整片不显色
            painter.setPen(QtCore.Qt.PenStyle.NoPen)

            rect: QtCore.QRectF = QtCore.QRectF(ix - BAR_WIDTH, 0, BAR_WIDTH * 2, 1)
            painter.drawRect(rect)

        painter.end()
        return picture

    def boundingRect(self) -> QtCore.QRectF:
        """"""
        return QtCore.QRectF(0, 0, self._manager.get_count() + 2, 1)

    def get_y_range(self, min_ix: int | None = None, max_ix: int | None = None) -> tuple[float, float]:
        """"""
        return 0.0, 1.0

    def get_info_text(self, ix: int) -> str:
        """柱带面板高度只有约 10px，信息框会被裁剪，状态文字并入指标面板显示。"""
        return ""
