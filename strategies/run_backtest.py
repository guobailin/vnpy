#!/usr/bin/env python
"""无 GUI 回测跑批入口：直接驱动 vnpy_ctastrategy.backtesting.BacktestingEngine。

为什么需要它：GUI 的 BacktesterEngine 是 App 层，headless 跑完 MainEngine 线程仍挂住
进程、管道不 flush（踩过一次，只能外部收尾）。这里只起 BacktestingEngine，跑完即退。

运行时数据安全：默认**不碰**真实 `~/.vntrader/database.db`——先复制到临时目录，再把
HOME 指过去，vnpy 全程只读副本。要对着别处的库跑用 `--db`。

回测参数（k1/k2/…）取策略类默认值；本脚本所在目录下所有 .py 里的 CtaTemplate
子类会被自动发现并逐个回测，`--strategy` 可只跑指定文件。

用法：
    python strategies/run_backtest.py --symbol MA701 --interval 15m
    python strategies/run_backtest.py --symbol MA701 --interval 1h --rate 0 --slippage 0
    python strategies/run_backtest.py --list

坑：`calculate_result()` 必须在 `calculate_statistics()` 之前调用，否则 daily_df
为空、统计指标返回空 dict（不是 0，是空——很容易被误读成"完全没有成交"）。
"""
from __future__ import annotations

import argparse
import importlib.util
import os
import shutil
import sqlite3
import sys
import tempfile
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
REAL_DB = os.path.expanduser("~/.vntrader/database.db")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Headless vnpy CTA backtest runner")
    p.add_argument("--symbol", help="合约代码，如 MA701")
    p.add_argument("--exchange", default="CZCE")
    p.add_argument("--interval", help="周期，如 1m / 5m / 15m / 1h / d")
    p.add_argument("--start", help="起始时间 (ISO)，默认取库中最早")
    p.add_argument("--end", help="结束时间 (ISO)，默认取库中最晚")
    p.add_argument("--rate", type=float, default=0.0001, help="手续费率")
    p.add_argument("--slippage", type=float, default=1.0, help="滑点（价格单位）")
    p.add_argument("--size", type=float, default=10.0, help="合约乘数")
    p.add_argument("--pricetick", type=float, default=1.0, help="最小变动价位")
    p.add_argument("--capital", type=int, default=100000, help="初始资金")
    p.add_argument("--db", default=REAL_DB, help="源 database.db（只读复制后使用）")
    p.add_argument("--strategy", action="append", default=None,
                   help="只跑指定策略文件，可重复")
    p.add_argument("--set", action="append", default=[], metavar="KEY=VALUE",
                   help="覆盖策略参数（按类默认值推断类型），可重复")
    p.add_argument("--list", action="store_true", help="只列出可发现的策略")
    return p.parse_args()


def parse_settings(cls, items: list[str]) -> dict:
    """把 --set k=v 按策略类默认值的类型做转换。"""
    setting = {}
    for item in items:
        key, _, raw = item.partition("=")
        default = getattr(cls, key, None)
        if default is None and not hasattr(cls, key):
            raise SystemExit(f"{cls.__name__} 没有参数 {key!r}")
        if isinstance(default, bool):
            value = raw.lower() in ("1", "true", "yes")
        elif isinstance(default, int):
            value = int(raw)
        elif isinstance(default, float):
            value = float(raw)
        else:
            value = raw
        setting[key] = value
    return setting


def isolate_db(source: str) -> str:
    """把源库复制到临时 HOME 并返回它——vnpy 按 HOME/.vntrader 找库。"""
    tmp = tempfile.mkdtemp(prefix="vnpy-bt-")
    os.makedirs(os.path.join(tmp, ".vntrader"))
    shutil.copy2(source, os.path.join(tmp, ".vntrader", "database.db"))
    return tmp


def discover(paths: list[str]):
    """从给定 .py 文件里找出 CtaTemplate 子类。"""
    from vnpy_ctastrategy import CtaTemplate

    found = []
    for path in sorted(paths):
        name = os.path.splitext(os.path.basename(path))[0]
        spec = importlib.util.spec_from_file_location(f"_bt_{name}", path)
        if spec is None or spec.loader is None:
            print(f"  ! 跳过（无法导入）{path}", file=sys.stderr)
            continue
        module = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(module)
        except Exception as exc:  # noqa: BLE001
            print(f"  ! 跳过 {path}: {exc}", file=sys.stderr)
            continue
        for attr in vars(module).values():
            if (isinstance(attr, type)
                    and issubclass(attr, CtaTemplate)
                    and attr is not CtaTemplate
                    and attr.__module__ == module.__name__):
                found.append((name, attr))
    return found


def span(db: str, symbol: str, interval: str):
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return conn.execute(
            "select min(datetime), max(datetime), count(*) from dbbardata "
            "where symbol=? and interval=?", (symbol, interval)).fetchone()
    finally:
        conn.close()


def run_one(cls, args, start: datetime, end: datetime) -> None:
    from vnpy_ctastrategy.backtesting import BacktestingEngine

    engine = BacktestingEngine()
    engine.set_parameters(
        vt_symbol=f"{args.symbol}.{args.exchange}",
        interval=args.interval,
        start=start,
        end=end,
        rate=args.rate,
        slippage=args.slippage,
        size=args.size,
        pricetick=args.pricetick,
        capital=args.capital,
    )
    setting = parse_settings(cls, args.set)
    engine.add_strategy(cls, setting)
    engine.load_data()
    engine.run_backtesting()
    engine.calculate_result()
    stats = engine.calculate_statistics(output=False)
    if not stats:
        print(f"  {cls.__name__:<30s} 无成交，无法计算指标")
        return
    print(f"  {cls.__name__:<30s} "
          f"收益 {stats['total_return']:>7.2f}%   "
          f"Sharpe {stats['sharpe_ratio']:>6.2f}   "
          f"最大回撤 {stats['max_ddpercent']:>6.2f}%   "
          f"交易 {stats['total_trade_count']:>4d} 笔   "
          f"天数 {stats['total_days']:>4d}")


def main() -> int:
    args = parse_args()

    if not os.path.exists(args.db):
        print(f"源数据库不存在：{args.db}", file=sys.stderr)
        return 2

    paths = args.strategy or [
        os.path.join(HERE, f) for f in sorted(os.listdir(HERE)) if f.endswith(".py")
    ]

    home = isolate_db(args.db)
    os.environ["HOME"] = home

    strategies = discover(paths)
    if args.list:
        for name, cls in strategies:
            print(f"  {name:<34s} {cls.__name__}")
        return 0
    if not strategies:
        print("没有发现任何策略类", file=sys.stderr)
        return 2
    if not args.symbol or not args.interval:
        print("需要 --symbol 与 --interval", file=sys.stderr)
        return 2

    lo, hi, count = span(args.db, args.symbol, args.interval)
    if not count:
        print(f"库中无 {args.symbol} {args.interval} 数据", file=sys.stderr)
        return 2
    start = datetime.fromisoformat(args.start) if args.start else datetime.fromisoformat(lo)
    end = datetime.fromisoformat(args.end) if args.end else datetime.fromisoformat(hi)

    print(f"{args.symbol}.{args.exchange} {args.interval}  "
          f"{start:%Y-%m-%d} ~ {end:%Y-%m-%d}  {count} 根  "
          f"rate={args.rate} slippage={args.slippage} size={args.size} capital={args.capital}")
    print(f"隔离库：{home}/.vntrader/database.db（源库只读复制）")
    for _name, cls in strategies:
        run_one(cls, args, start, end)
    shutil.rmtree(home, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
