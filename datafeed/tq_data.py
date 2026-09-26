#!/usr/bin/env python
"""天勤（tqsdk）数据层：长历史 K 线 -> 自建后复权主力连续 -> 写入 vnpy 数据库。

为什么需要它
------------
新浪免费分钟源每个周期硬顶 1023 根：15m 只有约 45 个交易日、5m 约 16 天、1m 约 3 天；
而且新浪的 MA888（内部映射 MA0）是**未复权拼接**，2026-09-10 21:15 一次性向下跳约
300 点接到真实合约价位，用它跑出来的回测结论全部作废。要把「45 天全亏」这类判断坐实
或推翻，必须先有长历史 + 口径干净的连续合约。

tqsdk 单序列上限 10000 根（15m 约 400+ 交易日、1h 约 6 年），且 adj_type 参数只对
股票/基金生效——期货连续合约只能自建，所以本模块做两件事：按日线持仓量选主力合约，
再用价差法把各合约拼成连续序列。

口径约定（写死，避免再踩）
--------------------------
- K 线时间戳一律取 bar 收盘时刻：库内既有数据、vnpy GUI 显示都是这个口径（15m 日盘为
  09:15..10:15 / 10:45..11:30 / 13:45..15:00）。tqsdk 原生给的是 bar 起点，本模块统一
  + duration 后入库；日线本身按交易日 00:00 标注，不再平移。
- 时区：Asia/Shanghai 的 aware datetime（vnpy 4.x 约定）。
- 拼接用价差（差值）法，不用比例：期货价格不是比例量，价差法保留绝对波动。
- 连续合约的 volume / open_interest 取当日主力合约的值，不跨合约求和。
- 默认 --anchor latest：最新一段价格 = 真实主力合约价格；--anchor earliest 则保留首段
  真实价格。两种只是复权基准不同，换月处都连续。

凭证只从环境变量或 env 文件读，不落盘、不打印（TQ_USER / TQ_PASS）。

用法
----
    python datafeed/tq_data.py probe  --product MA
    python datafeed/tq_data.py pull   --product MA --durations 15m,1h,d
    python datafeed/tq_data.py build  --product MA --intervals 15m --out-symbol MA888
    python datafeed/tq_data.py import --product MA --intervals 15m,1h,d --db <库路径>
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
import time

import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(HERE, "cache")
TZ = "Asia/Shanghai"

# 周期表：vnpy Interval 值 <-> tqsdk 周期（秒）
DURATIONS = [("1m", 60), ("5m", 300), ("15m", 900), ("1h", 3600), ("d", 86400)]
INTERVAL2DUR = dict(DURATIONS)
# 派生周期：由干净的日线重采样，用来替换 sina 那批用污染日线算出来的周/月/季线
DERIVED = {"w": "W", "1M": "M", "1Q": "Q"}


def load_auth(env_file):
    """TQ_USER/TQ_PASS：环境变量优先，其次 env 文件。只读不写、不回显。"""
    user = os.environ.get("TQ_USER")
    pwd = os.environ.get("TQ_PASS")
    if user and pwd:
        return user, pwd

    path = os.path.expanduser(env_file) if env_file else os.path.expanduser("~/.hermes/.env")
    if not os.path.exists(path):
        raise SystemExit("缺少 TQ_USER/TQ_PASS，且 env 文件不存在：" + path)
    for raw in open(path, encoding="utf-8"):
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key == "TQ_USER" and not user:
            user = value
        elif key == "TQ_PASS" and not pwd:
            pwd = value
    if not user or not pwd:
        raise SystemExit(path + " 中没有 TQ_USER / TQ_PASS")
    return user, pwd


def open_api(env_file):
    from tqsdk import TqApi, TqAuth

    user, pwd = load_auth(env_file)
    return TqApi(auth=TqAuth(user, pwd), web_gui=False)


def to_bars(frame, duration):
    """tqsdk K 线（bar 起点、北京时间为准的纳秒）-> 收盘时刻的 aware 上海时间。"""
    ts = pd.to_datetime(frame["datetime"], unit="ns", utc=True).dt.tz_convert(TZ)
    if duration < 86400:                      # 日线已按交易日 00:00 标注，不再平移
        ts = ts + pd.Timedelta(seconds=duration)
    out = pd.DataFrame({
        "datetime": ts,
        "open": frame["open"].astype(float),
        "high": frame["high"].astype(float),
        "low": frame["low"].astype(float),
        "close": frame["close"].astype(float),
        "volume": frame["volume"].astype(float),
        "open_interest": frame["close_oi"].astype(float),
    })
    out = out.dropna(subset=["open", "high", "low", "close"])
    out = out[out["close"] > 0]
    return out.sort_values("datetime").reset_index(drop=True)


def clip_to_life(symbol, bars):
    """按交割月裁掉不属于本代合约的 bar（郑商所三位月份码跨 10 年重复）"""
    key = delivery_key(symbol)
    if key is None or not len(bars):
        return bars
    year, month = key
    stamp = bars["datetime"]
    start = pd.Timestamp(year=year - 2, month=month, day=1, tz=TZ)
    end = pd.Timestamp(year=year, month=month, day=1, tz=TZ) + pd.DateOffset(months=1)
    keep = (stamp >= start) & (stamp <= end)
    return bars[keep].reset_index(drop=True)


def fetch(api, symbols, duration, data_length=10000, timeout=180.0):
    """批量订阅 K 线并等数据到位；无数据的合约不入结果。"""
    serials = {}
    for symbol in symbols:
        try:
            serials[symbol] = api.get_kline_serial(symbol, duration, data_length=data_length)
        except Exception as exc:                                  # noqa: BLE001
            print("    ! %-16s 订阅失败 %s: %s" % (symbol, type(exc).__name__, str(exc)[:90]))
    if not serials:
        return {}

    deadline = time.time() + timeout
    while time.time() < deadline:
        api.wait_update(deadline=time.time() + 3)
        if all(len(k) and not pd.isna(k["close"].iloc[-1]) for k in serials.values()):
            break

    out = {}
    for symbol, kline in serials.items():
        bars = to_bars(kline, duration)
        bars = clip_to_life(symbol, bars)
        if len(bars):
            out[symbol] = bars
    return out


# vnpy 的 1m / 1M 在 macOS 大小写不敏感文件系统上是同一个文件名，
# 缓存必须换成不会大小写撞车的 token，否则 1 分钟线会被月线覆盖。
CACHE_TOKEN = {"1m": "min1", "5m": "min5", "15m": "min15", "1h": "hour1",
               "d": "day", "w": "week", "1M": "month1", "1Q": "quarter1"}


def cache_path(symbol, interval):
    token = CACHE_TOKEN[interval]
    return os.path.join(CACHE_DIR, "%s_%s.csv" % (symbol.replace(".", "_"), token))


def save_cache(symbol, interval, bars):
    os.makedirs(CACHE_DIR, exist_ok=True)
    bars.to_csv(cache_path(symbol, interval), index=False)


def load_cache(symbol, interval):
    path = cache_path(symbol, interval)
    if not os.path.exists(path):
        return None
    df = pd.read_csv(path, parse_dates=["datetime"])
    df["datetime"] = pd.to_datetime(df["datetime"], utc=True).dt.tz_convert(TZ)
    return df


def delivery_key(tq_symbol):
    """CZCE 三位月份码 / 其他交易所四位 YYMM -> (year, month)；解析不出返回 None。"""
    code = tq_symbol.split(".")[-1]
    digits = "".join(c for c in code if c.isdigit())
    if len(digits) == 3:
        return 2020 + int(digits[0]), int(digits[1:])
    if len(digits) == 4:
        return 2000 + int(digits[:2]), int(digits[2:])
    return None


def list_contracts(api, exchange, product):
    codes = set()
    for expired in (False, True):
        try:
            codes |= set(api.query_quotes(ins_class="FUTURE", exchange_id=exchange,
                                          product_id=product, expired=expired))
        except Exception as exc:                                  # noqa: BLE001
            print("    ! query_quotes(expired=%s) 失败 %s: %s"
                  % (expired, type(exc).__name__, str(exc)[:90]))
    return sorted(c for c in codes if delivery_key(c) is not None)


def list_contracts_from_cache(exchange, product, interval):
    if not os.path.isdir(CACHE_DIR):
        return []
    prefix = "%s_%s" % (exchange, product)
    suffix = "_%s.csv" % CACHE_TOKEN[interval]
    names = []
    for name in os.listdir(CACHE_DIR):
        if name.startswith(prefix) and name.endswith(suffix):
            names.append(name[: -len(suffix)].replace("_", "."))
    return sorted(names)


def dominant_by_date(daily, confirm_days):
    """逐交易日取持仓量最大的合约；新合约需连续 confirm_days 日占优才换月。"""
    oi = {}
    for symbol, bars in daily.items():
        for row in bars.itertuples():
            value = float(row.open_interest)
            if value > 0:
                oi.setdefault(row.datetime.date(), {})[symbol] = value
    if not oi:
        raise SystemExit("日线缓存为空，无法判定主力")

    result = {}
    current = None
    pending = None
    pending_count = 0
    for day in sorted(oi):
        candidates = oi[day]
        best = max(candidates, key=lambda s: candidates[s])
        if current is None:
            current = best
        elif best != current:
            if best == pending:
                pending_count += 1
            else:
                pending, pending_count = best, 1
            if pending_count >= confirm_days:
                current = best
                pending, pending_count = None, 0
        else:
            pending, pending_count = None, 0
        result[day] = current
    return result


def close_on(daily, symbol, day, lookback_days=7):
    """该合约在 day 的日线收盘价；当日无数据则向前找 lookback_days 内最近一根。"""
    bars = daily.get(symbol)
    if bars is None or not len(bars):
        return None
    stamps = bars["datetime"].dt.date
    rows = bars[stamps == day]
    if len(rows):
        return float(rows["close"].iloc[-1])
    earlier = bars[stamps < day]
    if not len(earlier):
        return None
    last = earlier.iloc[-1]
    if (day - last["datetime"].date()).days > lookback_days:
        return None
    return float(last["close"])


def roll_segments(dominant):
    """按主力合约把交易日切成连续段：[合约, [日期...]]。"""
    segments = []
    for day in sorted(dominant):
        contract = dominant[day]
        if segments and segments[-1][0] == contract:
            segments[-1][1].append(day)
        else:
            segments.append([contract, [day]])
    return segments


def roll_gaps(segments, daily):
    """换月基准 = 旧段最后一个交易日两个合约的日线收盘价之差（不是新段首日，否则基差被放大成双倍假跳空）"""
    gaps = []
    for i in range(len(segments) - 1):
        last_day = segments[i][1][-1]
        old_close = close_on(daily, segments[i][0], last_day)
        new_close = close_on(daily, segments[i + 1][0], last_day)
        gaps.append(0.0 if old_close is None or new_close is None else old_close - new_close)
    return gaps

def cumulative_offsets(gaps, anchor):
    """把换月价差落成每段的差值（latest 段价格真实）"""
    n = len(gaps) + 1
    shift = [0.0] * n
    if anchor != "latest":
        for k in range(n - 1):
            shift[k + 1] = shift[k] + gaps[k]
    else:
        for k in range(n - 2, -1, -1):
            shift[k] = shift[k + 1] - gaps[k]
    return shift


def trading_day_map(days):
    """日历日 -> 交易日：夜盘（20 点后）的 bar 归属下一个交易日"""
    ordered = sorted(days)
    return {ordered[i]: ordered[i + 1] for i in range(len(ordered) - 1)}


def bar_trading_days(bars, day_map):
    """每根 bar 的交易日；按日历日分段会把换月切在夜盘中间"""
    out = []
    for stamp in bars["datetime"]:
        day = stamp.date()
        out.append(day_map.get(day, day) if stamp.hour >= 20 else day)
    return out


def shift_segment(series, contract, seg_days, shift, day_map):
    """取某合约在这些交易日的 K 线，价格列整体加 shift"""
    bars = series.get(contract)
    if bars is None or not len(bars):
        return None
    wanted = set(seg_days)
    keep = [d in wanted for d in bar_trading_days(bars, day_map)]
    chunk = bars[keep].copy()
    if not len(chunk):
        return None
    for column in PRICE_COLUMNS:
        chunk[column] = chunk[column] + shift
    chunk["contract"] = contract
    return chunk

PRICE_COLUMNS = ("open", "high", "low", "close")


def assemble(series, daily, dominant, anchor="latest"):
    """逐日取主力合约，按价差平移后拼成一条连续序列"""
    segments = roll_segments(dominant)
    gaps = roll_gaps(segments, daily)
    shifts = cumulative_offsets(gaps, anchor)
    day_map = trading_day_map(dominant)
    frames = []
    for i, (contract, seg_days) in enumerate(segments):
        chunk = shift_segment(series, contract, seg_days, shifts[i], day_map)
        if chunk is not None:
            frames.append(chunk)
    if not frames:
        raise SystemExit("没有任何可拼接的数据，检查缓存与主力判定")
    merged = pd.concat(frames, ignore_index=True)
    merged = merged.sort_values("datetime")
    merged = merged.drop_duplicates("datetime", keep="last")
    return merged.reset_index(drop=True)


def resample_derived(daily_spliced, interval):
    """周/月/季线：按周期分组，时间戳取该周期最后一个交易日"""
    frame = daily_spliced.sort_values("datetime").reset_index(drop=True)
    key = frame["datetime"].dt.to_period(DERIVED[interval])
    out = frame.groupby(key).agg(
        open=("open", "first"),
        high=("high", "max"),
        low=("low", "min"),
        close=("close", "last"),
        volume=("volume", "sum"),
        open_interest=("open_interest", "last"),
        datetime=("datetime", "last"),
    )
    return out.dropna(subset=["close"]).reset_index(drop=True)


def roll_report(bars, rolls, label, guard):
    """换月处相邻 bar 的跳空检查：自建连续在换月处不该出现大跳空"""
    if not rolls or label in DERIVED:
        return ""
    roll_set = set(rolls)
    prices = bars["close"].to_numpy()
    stamps = bars["datetime"]
    worst = 0.0
    worst_day = None
    for i in range(1, len(prices)):
        if stamps.iloc[i].date() in roll_set:
            delta = abs(prices[i] - prices[i - 1])
            if delta > worst:
                worst, worst_day = delta, stamps.iloc[i].date()
    flag = "  ! 超阈值，先看 verify" if worst > guard else ""
    return ("  换月处相邻 bar 差 " + format(worst, ".1f") + " 点@" + str(worst_day)
            + "（含真实隔夜跳空，权威校验用 verify）" + flag)

CZCE_PRODUCTS = {"MA", "SA", "FG", "UR", "TA", "SR", "CF", "PF", "OI", "RM", "SM", "SF",
                 "AP", "CJ", "ZC", "CY", "PK", "PX", "SH", "WH", "PM", "RI", "LR", "JR", "RS"}
DCE_PRODUCTS = {"EG", "V", "PP", "EB", "I", "JM", "J", "L", "M", "Y", "P", "C", "CS", "PG",
                "A", "B", "JD", "LH", "RR", "FB", "BB", "LG"}
SHFE_PRODUCTS = {"BU", "RB", "RU", "SP", "AO", "FU", "CU", "AL", "ZN", "NI", "AG", "AU",
                 "HC", "SS", "SN", "PB", "WR", "BR", "AD"}
INE_PRODUCTS = {"SC", "LU", "NR", "BC", "EC"}
GFEX_PRODUCTS = {"SI", "LC", "PS"}


def guess_exchange(product):
    """品种代码 -> 交易所（写死表，避免猜错）"""
    for exchange, table in (("CZCE", CZCE_PRODUCTS), ("DCE", DCE_PRODUCTS),
                            ("SHFE", SHFE_PRODUCTS), ("INE", INE_PRODUCTS),
                            ("GFEX", GFEX_PRODUCTS)):
        if product in table:
            return exchange
    raise SystemExit("不知道 " + product + " 属于哪个交易所，请写成 交易所.品种，如 CZCE.MA")


def parse_product(text):
    """MA / CZCE.MA / CZCE.MA701 都接受"""
    if "." in text:
        exchange, code = text.split(".", 1)
        return exchange.upper(), "".join(c for c in code if c.isalpha()).upper()
    letters = "".join(c for c in text if c.isalpha()).upper()
    return guess_exchange(letters), letters


def load_daily(exchange, product):
    """读日线缓存（排除连续合约本身，避免自引用）"""
    daily = {}
    for symbol in list_contracts_from_cache(exchange, product, "d"):
        if "888" in symbol:
            continue
        frame = load_cache(symbol, "d")
        if frame is not None and len(frame):
            daily[symbol] = frame
    return daily


def pick_intraday(exchange, product, label, daily):
    """读某周期的分钟线缓存"""
    series = {}
    candidates = set(daily) | set(list_contracts_from_cache(exchange, product, label))
    for symbol in candidates:
        frame = load_cache(symbol, label)
        if frame is not None and len(frame):
            series[symbol] = frame
    return series

def cmd_probe(args):
    """探查各周期实测可取深度"""
    exchange, product = parse_product(args.product)
    api = open_api(args.env_file)
    try:
        print(f"品种 {exchange}.{product} - tqsdk 单序列实测深度（data_length={args.length}）")
        print(f"{'周期':<6}{'合约':<12}{'根数':>7}{'交易日':>8}   区间")
        for label, duration in DURATIONS:
            symbol = f"KQ.m@{exchange}.{product}"
            try:
                bars = fetch(api, [symbol], duration, args.length).get(symbol)
            except Exception as exc:
                print(f"{label:<6}{symbol:<12}  ERR {type(exc).__name__}: {str(exc)[:70]}")
                continue
            if bars is None or not len(bars):
                print(f"{label:<6}{symbol:<12}{0:>7}")
                continue
            days = bars["datetime"].dt.date.nunique()
            span = str(bars["datetime"].iloc[0]) + "  ->  " + str(bars["datetime"].iloc[-1])
            print(f"{label:<6}{symbol:<12}{len(bars):>7}{days:>8}   {span}")
        contracts = list_contracts(api, exchange, product)
        head = ", ".join(contracts[:12]) + (" ..." if len(contracts) > 12 else "")
        print(f"")
        print(f"可交易+已下市合约 {len(contracts)} 个：{head}")
    finally:
        api.close()
    return 0


def cmd_pull(args):
    """拉取品种下全部合约 K 线到缓存"""
    exchange, product = parse_product(args.product)
    intervals = [i.strip() for i in args.durations.split(",") if i.strip()]
    api = open_api(args.env_file)
    try:
        contracts = list_contracts(api, exchange, product)
        if args.limit:
            contracts = contracts[-args.limit:]
        print(f"品种 {exchange}.{product}：{len(contracts)} 个合约，周期 {intervals}")

        for label in intervals:
            duration = INTERVAL2DUR[label]
            todo = [c for c in contracts if args.force or load_cache(c, label) is None]
            print(f"  [{label}] 待拉取 {len(todo)}/{len(contracts)} 个合约")
            for start in range(0, len(todo), args.batch):
                group = todo[start:start + args.batch]
                result = fetch(api, group, duration, args.length)
                for symbol, bars in result.items():
                    save_cache(symbol, label, bars)
                print(f"    {start + len(group)}/{len(todo)} 完成，本批取到 {len(result)} 个")

        for label in intervals:
            symbol = f"KQ.m@{exchange}.{product}"
            if args.force or load_cache(symbol, label) is None:
                result = fetch(api, [symbol], INTERVAL2DUR[label], args.length)
                for sym, frame in result.items():
                    save_cache(sym, label, frame)
    finally:
        api.close()
    return 0

def cmd_build(args):
    """用日线持仓量选主力，自建后复权连续（价差法）"""
    exchange, product = parse_product(args.product)
    daily = load_daily(exchange, product)
    if not daily:
        raise SystemExit("没有日线缓存；先运行 pull --durations d")

    dominant = dominant_by_date(daily, args.confirm_days)
    days = sorted(dominant)
    rolls = [d for i, d in enumerate(days) if i and dominant[d] != dominant[days[i - 1]]]
    preview = ", ".join(str(d) + ":" + dominant[d] for d in rolls[:10])
    print(f"主力判定：{len(days)} 个交易日，换月 {len(rolls)} 次 -> {preview}")

    labels = [i.strip() for i in args.intervals.split(",") if i.strip()]
    for label in labels:
        try:
            if label in DERIVED:
                bars = resample_derived(assemble(daily, daily, dominant, args.anchor), label)
            else:
                series = pick_intraday(exchange, product, label, daily)
                if not series:
                    print(f"  [{label}] 无分钟线缓存，跳过")
                    continue
                bars = assemble(series, daily, dominant, args.anchor)
        except SystemExit as exc:
            print(f"  [{label}] 拼接失败，跳过：{exc}")
            continue

        bars = bars[["datetime", "open", "high", "low", "close", "volume", "open_interest"]]
        save_cache(args.out_symbol, label, bars)
        per_day = len(bars) / max(bars["datetime"].dt.date.nunique(), 1)
        span = str(bars["datetime"].iloc[0].date()) + " -> " + str(bars["datetime"].iloc[-1].date())
        note = roll_report(bars, rolls, label, args.roll_guard)
        print(f"  [{label}] {args.out_symbol} {len(bars)} 根  日均 {per_day:.1f} 根  {span}{note}")
    return 0



def prune_stale(db_path, db_symbol, exchange_value, interval, bars):
    """删掉库里不在本次缓存中的旧口径行（同一 symbol 不能混两种拼接口径）"""
    import sqlite3
    keep = set(str(t)[:19] for t in bars["datetime"].dt.tz_localize(None))
    conn = sqlite3.connect(db_path, timeout=30)
    try:
        rows = conn.execute(
            "select id, datetime from dbbardata where symbol=? and exchange=? and interval=?",
            (db_symbol, exchange_value, interval)).fetchall()
        stale = [rid for rid, stamp in rows if str(stamp)[:19] not in keep]
        for start in range(0, len(stale), 500):
            chunk = stale[start:start + 500]
            marks = ",".join("?" * len(chunk))
            conn.execute("delete from dbbardata where id in (" + marks + ")", chunk)
        stat = conn.execute(
            "select count(*), min(datetime), max(datetime) from dbbardata "
            "where symbol=? and exchange=? and interval=?",
            (db_symbol, exchange_value, interval)).fetchone()
        if stat[0]:
            conn.execute("update dbbaroverview set count=?, start=?, end=? "
                         "where symbol=? and exchange=? and interval=?",
                         (stat[0], stat[1], stat[2], db_symbol, exchange_value, interval))
        else:
            conn.execute("delete from dbbaroverview where symbol=? and exchange=? and interval=?",
                         (db_symbol, exchange_value, interval))
        conn.commit()
        return len(stale)
    finally:
        conn.close()


def cmd_import(args):
    """把缓存的连续合约写进 vnpy sqlite 库"""
    db_path = os.path.abspath(args.db)
    if not os.path.exists(db_path):
        raise SystemExit("目标数据库不存在：" + db_path)
    if db_path.startswith("/var/folders") or "/T/" in db_path:
        raise SystemExit("目标库路径像临时目录（HOME 被重定向？）：" + db_path
                         + "  请用 --db 指向真实的 ~/.vntrader/database.db")
    if args.backup:
        backup = db_path + ".bak-data-" + str(int(time.time()))
        shutil.copy2(db_path, backup)
        print("已备份 -> " + backup)

    repo = os.path.dirname(HERE)
    if repo not in sys.path:
        sys.path.insert(0, repo)
    from vnpy.trader.setting import SETTINGS
    SETTINGS["database.database"] = db_path
    SETTINGS["database.timezone"] = TZ
    from vnpy.trader.constant import Exchange, Interval
    from vnpy.trader.database import get_database
    from vnpy.trader.object import BarData

    database = get_database()
    exchange, product = parse_product(args.product)
    if args.symbols:
        raw_targets = [s.strip() for s in args.symbols.split(",") if s.strip()]
    else:
        raw_targets = [product + "888"]

    # 缓存键可能带交易所前缀（CZCE.MA701），入库 symbol 只取合约代码（MA701）；
    # vnpy 的 vt_symbol 是 "MA701.CZCE"，带前缀入库会查不到。
    targets = []
    for raw in raw_targets:
        if "." in raw:
            exchange_enum = Exchange(raw.split(".", 1)[0].upper())
            targets.append((raw, raw.split(".", 1)[1], exchange_enum))
        else:
            targets.append((raw, raw, Exchange(exchange)))

    for symbol, db_symbol, exchange_enum in targets:
        assert "." not in db_symbol, "入库 symbol 不能带交易所前缀：" + db_symbol
        for label in [i.strip() for i in args.intervals.split(",") if i.strip()]:
            bars = load_cache(symbol, label)
            if bars is None or not len(bars):
                print(f"  {db_symbol} {label}: 无缓存，跳过")
                continue
            objects = [
                BarData(
                    gateway_name="TQ", symbol=db_symbol, exchange=exchange_enum,
                    datetime=row.datetime.to_pydatetime(), interval=Interval(label),
                    volume=float(row.volume), turnover=0.0,
                    open_interest=float(row.open_interest), open_price=float(row.open),
                    high_price=float(row.high), low_price=float(row.low),
                    close_price=float(row.close),
                )
                for row in bars.itertuples()
            ]
            for start in range(0, len(objects), 5000):
                database.save_bar_data(objects[start:start + 5000])
            span = str(bars["datetime"].iloc[0].date()) + " -> " + str(bars["datetime"].iloc[-1].date())
            note = ""
            if args.prune:
                removed = prune_stale(db_path, db_symbol, exchange_enum.value, label, bars)
                note = f"  清理旧口径 {removed} 根" if removed else "  无旧口径残留"
            print(f"  {db_symbol} {label}: 写入 {len(objects)} 根  {span}  源 {symbol}{note}")
    return 0



def cmd_verify(args):
    """校验拼接：换月处的绝对价格变动应等于新合约自身的绝对变动（价差复权保变动）"""
    exchange, product = parse_product(args.product)
    daily = load_daily(exchange, product)
    dominant = dominant_by_date(daily, args.confirm_days)
    days = sorted(dominant)
    rolls = [d for i, d in enumerate(days) if i and dominant[d] != dominant[days[i - 1]]]
    bars = load_cache(args.out_symbol, args.interval)
    if bars is None or not len(bars):
        raise SystemExit("没有连续合约缓存：" + args.out_symbol + " " + args.interval)

    stamp = list(bars["datetime"])
    close = list(bars["close"])
    day_of = [t.date() for t in stamp]
    nearest = {}
    for pos, day in enumerate(day_of):
        nearest.setdefault(day, pos)

    peer = {}
    worst = 0.0
    worst_ts = None
    checked = 0
    for day in rolls:
        pos = nearest.get(day)
        if pos is None or pos == 0:
            continue
        new = dominant[day]
        if new not in peer:
            frame = load_cache(new, args.interval)
            peer[new] = dict(zip(frame["datetime"], frame["close"])) if frame is not None else {}
        table = peer[new]
        prev_ts, cur_ts = stamp[pos - 1], stamp[pos]
        if prev_ts not in table or cur_ts not in table:
            continue
        dev = abs((close[pos] - close[pos - 1]) - (table[cur_ts] - table[prev_ts]))
        checked += 1
        if dev > worst:
            worst, worst_ts = dev, cur_ts
    print(f"{args.out_symbol} {args.interval}: 换月点 {checked}/{len(rolls)} 可校验")
    print(f"  换月处连续序列变动 vs 新合约自身变动，最大点数差 {worst:.1f} @ {worst_ts}")
    print("  基准：价差复权保绝对变动，该差值应约为 0；若量化到基差量级（几十上百点）则拼接有误")
    return 0


def build_parser():
    parser = argparse.ArgumentParser(
        description="天勤数据层：长历史 K 线 -> 后复权主力连续 -> vnpy 库")
    parser.add_argument("--env-file", default=None, help="含 TQ_USER/TQ_PASS 的 env 文件")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("probe", help="探查各周期实测可取深度")
    p.add_argument("--product", default="CZCE.MA")
    p.add_argument("--length", type=int, default=10000)
    p.set_defaults(func=cmd_probe)

    p = sub.add_parser("pull", help="拉取品种下全部合约 K 线到缓存")
    p.add_argument("--product", default="CZCE.MA")
    p.add_argument("--durations", default="15m")
    p.add_argument("--length", type=int, default=10000)
    p.add_argument("--batch", type=int, default=6)
    p.add_argument("--limit", type=int, default=0, help="只用最近 N 个月份的合约（调试）")
    p.add_argument("--force", action="store_true", help="忽略缓存重拉")
    p.set_defaults(func=cmd_pull)

    p = sub.add_parser("build", help="自建后复权主力连续")
    p.add_argument("--product", default="CZCE.MA")
    p.add_argument("--intervals", default="15m")
    p.add_argument("--out-symbol", default="MA888")
    p.add_argument("--anchor", choices=["latest", "earliest"], default="latest")
    p.add_argument("--confirm-days", type=int, default=2)
    p.add_argument("--roll-guard", type=float, default=200.0, help="粗口径告警阈值（点），权威校验用 verify")
    p.set_defaults(func=cmd_build)

    p = sub.add_parser("import", help="写入 vnpy sqlite 库")
    p.add_argument("--product", default="CZCE.MA")
    p.add_argument("--intervals", default="15m")
    p.add_argument("--symbols", default=None, help="要写入的符号，逗号分隔；默认 <品种>888")
    p.add_argument("--db", default=os.path.expanduser("~/.vntrader/database.db"))
    p.add_argument("--backup", action="store_true", default=True)
    p.add_argument("--no-backup", dest="backup", action="store_false")
    p.add_argument("--prune", action="store_true",
                   help="删掉库里不在本次缓存里的旧口径行（同 symbol 只留一种拼接口径）")
    p.set_defaults(func=cmd_import)
    p = sub.add_parser("verify", help="校验自建连续的换月连续性")
    p.add_argument("--product", default="CZCE.MA")
    p.add_argument("--out-symbol", default="MA888")
    p.add_argument("--interval", default="d")
    p.add_argument("--confirm-days", type=int, default=2)
    p.set_defaults(func=cmd_verify)

    return parser


def main():
    """CLI 入口"""
    args = build_parser().parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
