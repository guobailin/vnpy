# 多周期 K 线支持补丁（5 分钟 / 15 分钟 / 周 / 月 / 季）

VeighNa 原生 `Interval` 只有 `1m / 1h / d / w / tick`，本目录记录为支持更多周期所做的改动。
其中两个改动位于**第三方 pip 包**（`site-packages`），重装/升级这两个包后会被覆盖，需要用本目录的 patch 重新应用。

## 1. `vnpy` 核心（本仓库内，随 git 管理）

`vnpy/trader/constant.py` 的 `Interval` 枚举新增：

```python
MINUTE_5 = "5m"
MINUTE_15 = "15m"
MONTHLY = "1M"      # 注意大小写：小写 m 是分钟
QUARTERLY = "1Q"
```

数据库层无需改动：`vnpy_sqlite` 的 `interval` 是字符串列，按 `Interval.value` 存取，新增成员向后兼容。

## 2. `vnpy_datamanager`（需打补丁）

应用：`patch -p0 < vnpy_datamanager-intervals.patch`（或 `git apply`，注意路径）

改动内容：

- `INTERVAL_NAME_MAP` 增加 `5分钟线 / 15分钟线 / 周线 / 月线 / 季线`
- 数据树的周期节点由**写死的 `[MINUTE, HOUR, DAILY]`** 改为**按库中实际存在的周期动态创建**（原实现在库中出现周线等周期时会因
  `interval_childs[overview.interval]` 抛 `KeyError`）

## 3. `vnpy_ctastrategy`（需打补丁）

应用：`patch -p0 < vnpy_ctastrategy-intervals.patch`

改动内容：`base.py` 的 `INTERVAL_DELTA_MAP` 补齐新周期
（原先连 `WEEKLY` 都缺失，用周线回测会 `KeyError`）。

## 4. `vnpy_akshare`（本地包 `~/vnpy_akshare`）

- `5m / 15m`：走 sina 分钟接口 `futures_zh_minute_sina(period="5"/"15")`
- `周 / 月 / 季`：拉日线后按周期分组重采样（K 线时间取该周期最后一个交易日）
- 时间戳统一为 `Asia/Shanghai` 的 aware datetime（vnpy 4.x 约定）
- 合约代码转换 `to_sina_symbol()`：
  - 主力连续 `rb888` → `RB0`
  - **郑商所三位月份码 → sina 四位码**：`MA611` → `MA2611`、`MA701` → `MA2701`
    （sina 对具体合约只认四位"年份+月份"，直接用三位码会返回 0 条）

## 5. `vnpy_ctabacktester` 回测 K 线图（需打补丁）

应用：`patch -p0 < vnpy_ctabacktester-chart-indicators.patch`

配套文件为本仓库内的 `vnpy/chart/regime_item.py`（随 git 管理，无需打补丁）。

改动内容：`ui/widget.py` 的 `CandleChartDialog` 在原有「K 线 / 成交量」之间插入两个面板，
并在图表下方的图例里补充说明：

| 面板 | 内容 |
|------|------|
| `indicator` | **ER×100（金黄 `#FFC000`）**、**ADX（天蓝 `#33BBFF`）** 两条曲线，共用 0~100 坐标 |
| `regime` | **市场状态柱带**：蓝 `#2E75B6` = 震荡、橙 `#ED7D31` = 单边（该面板隐藏右轴） |

判定阈值写在 `vnpy/chart/regime_item.py` 顶部，与
`strategies/range_reversion_intraday.py` 的默认参数保持一致：
`ER_WINDOW=20 / ER_THRESHOLD=0.35 / ADX_WINDOW=14 / ADX_THRESHOLD=25.0`。
MA701 15 分钟实测：996 根可判定 K 线中震荡 303 根（30%）、单边 693 根（70%），
与策略回测时实际判定完全一致。

### 2026-09-26 视觉优化（已并入补丁与本仓库 chart 模块）

- **ER / ADX 曲线改 cosmetic 画笔（2px）**：原先 `QPen(color, 1.5)` 是非 cosmetic 画笔，
  线宽随视图缩放被放大（缩略时金黄线粗成一片）；改为 `setCosmetic(True)` 后任何缩放级别都是 2px。
- **状态柱带满宽（`BAR_WIDTH 0.4 → 0.5`）**：消除柱间黑缝，视觉上是连续色带。
- **K 线信息框紧凑化**（`vnpy/chart/item.py`）：11 行 → 8 行（去掉空行、OHLC 改单行缩写），
  不再溢出面板下边界。
- **十字光标 y 轴标签保留两位小数**（`vnpy/chart/widget.py`）：不再显示 `45.038213951` 这种全精度浮点。
- **交易标记增强**（本补丁内）：虚线 1.5 → 2.5px；开平仓箭头 10 → 14 并加白描边；
  手数文字偏移 ×3 → ×10，不再压在箭头上。

### 2026-09-26 二次调整：去图例 + 柱带置顶变细

- **删除图表下方全部 4 行图例文字**（含原生的 6 个标签），图例靠颜色区分，空间全部留给 K 线。
- **市场状态柱带移到最顶端**，高度 70 → 10~12px 细条（`minimum_height=10, maximum_height=12`）。
- 柱带面板太矮放不下信息框，`RegimeItem.get_info_text` 返回空，
  状态文字并入指标面板光标信息：`ER 0.39  ADX 18.5  单边`。

### 2026-09-26 三次调整：修复十字线各面板错位

- **根因**：柱带面板用 `hideAxis("right")` 隐藏右轴后，其视图比其他面板宽 60px。
  pyqtgraph 的 `setXLink` 在视图宽度不同时走「像素对齐补偿」（`ViewBox.linkedViewChanged`），
  沿 regime→candle→indicator→volume 链式传播时，每一跳都重新换算范围，
  浮点换算出相同值时信号被抑制、传播中断，部分面板停留在旧范围 —— 竖线错位一个轴宽。
  （此 bug 在旧布局同样存在，旧截图中 volume 面板错位 120px 即为此因。）
- **修复**：柱带面板右轴改为 `setStyle(showValues=False)`（保留 60px 占位、不显示刻度），
  四个面板视图宽度完全一致，x-link 传播的是相同数值，不再经过像素补偿换算。
  已验证：初始 / 缩放 / 单视图拖动三种场景下所有面板 range 与像素映射完全一致。

两个坑（改这个文件时注意）：

- **`drawRect` 若只 `setBrush` 不 `setPen`，柱带不显色**（Qt6 下表现为整片空白/背景色）。
  画柱带必须先 `painter.setPen(QtCore.Qt.PenStyle.NoPen)`。
- 该文件使用 **CRLF** 换行；重做补丁时要保留，否则 diff 会把整个文件当成改动。

## 数据源限制

sina 分钟接口每个周期最多返回 **1023 条**，因此：

| 周期 | 可获取深度（本机实测） |
|------|----------------------|
| 1m | 约 3 个交易日 |
| 5m | 约 3 周 |
| 15m | 约 2 个月 |
| 1h | 约 8 个月 |
| 日 / 周 / 月 / 季 | 11 年以上（日线 2014-12-24 起） |

要累积更长的分钟历史，需要定期增量下载（或自行录制 tick 后合成）。
