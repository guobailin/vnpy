#!/usr/bin/env python3
"""启动 VeighNa Trader（CTP 版）。

加载内容：CTP 交易接口（vnpy_ctp）+ CTA 策略 / CTA 回测 / 数据管理。

运行方式（在仓库根目录下）：

    ./.venv/bin/python run_trader.py

macOS 环境说明：
* vnpy_ctp 由源码编译后安装在本仓库的 .venv 中（当前 6.7.7.2，对应 CTP API 6.7.7）。
  上期技术未公开发布 6.7.8 及以上的 macOS 版 CTP API，因此 macOS 上暂时无法使用 6.7.11。
* 编译产物（vnctptd/vnctpmd 扩展 + thosttraderapi_se / thostmduserapi_se framework）
  已随 wheel 安装进 site-packages，运行期不依赖 vnpy_ctp 源码目录。
* 重新编译步骤见 docs/community/install/mac_install.md，注意用 ./.venv/bin/pip 而不是 pip3。
"""
from vnpy.event import EventEngine
from vnpy.trader.engine import MainEngine
from vnpy.trader.ui import MainWindow, create_qapp

from vnpy_ctp import CtpGateway
from vnpy_ctastrategy import CtaStrategyApp
from vnpy_ctabacktester import CtaBacktesterApp
from vnpy_datamanager import DataManagerApp


def main() -> None:
    """"""
    qapp = create_qapp()

    event_engine = EventEngine()
    main_engine = MainEngine(event_engine)

    main_engine.add_gateway(CtpGateway)

    main_engine.add_app(CtaStrategyApp)
    main_engine.add_app(CtaBacktesterApp)
    main_engine.add_app(DataManagerApp)

    main_window = MainWindow(main_engine, event_engine)
    main_window.showMaximized()

    qapp.exec()


if __name__ == "__main__":
    main()
