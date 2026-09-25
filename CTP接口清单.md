# CTP 接口对接清单（macOS / CTP API 6.7.13 口径）

> 生成方式：从源码静态提取（grep/正则），非人工誊写。
> 基准：`vnpy` 核心仓库 `773436de`；`vnpy_ctp` 包装层 `6.7.11.4`（分支 `mac-6.7.13`）；
> 底层 CTP API **6.7.13**（macOS，取自 SimNow 官方包 `macOS_API_6.7.13`）。
> 版本核实方式：运行时调用 `TdApi.getApiVersion()` → `v6.7.13_MacOS_20260529 15:38:00`。

## 0. 结论速览

| 层次 | 情况 |
|------|------|
| 本仓库（vnpy 核心） | **不含任何 CTP 实现代码**，只通过网关插件机制对接；引用点见第 1 节 |
| CTP 落地载体 | 独立包 [`vnpy_ctp`](https://github.com/vnpy/vnpy_ctp)（PyPI 名 `vnpy_ctp`，包装层版本 `6.7.11.4`） |
| 底层 CTP API | **6.7.13**（macOS 版，`v6.7.13_MacOS_20260529`；Windows/Linux 官方同期版本仍为 6.7.11） |
| 生态中的 CTP 系接口 | 4 个：`ctp`（期货/期货期权）、`ctptest`（测试）、`mini`（CTP Mini）、`sopt`（CTP 期权） |
| 包装层实际对接 | 交易 `CThostFtdcTraderApi` **134** 个调用；行情 `CThostFtdcMdApi` **16** 个调用 |
| 包装层回调（SPI） | 交易 **164** 个；行情 **13** 个（全部透出到 Python） |
| 官方 6.7.13 全量 | TraderApi 声明 **125** 个 `Req*`；MdApi 声明 **7** 个 `Req*`/`Sub*` |
| **未封装** | **7** 个 6.7.13 新接口没有 Python 绑定，见第 6 节 |

## 1. 本仓库（vnpy 核心）的 CTP 引用点

按**文件名**搜索，本仓库里带 ctp 的只有文档文件；代码中不存在任何 `CThostFtdc*` / `thost*api` 符号 ——
CTP 的实现全部在独立包 `vnpy_ctp` 中，本仓库只通过网关插件机制调用。

| 文件 | 引用内容 |
|------|----------|
| `run_trader.py` | 本仓库的启动入口：`from vnpy_ctp import CtpGateway` + `add_gateway(CtpGateway)` |
| `examples/veighna_trader/run.py` | 官方示例，同上（含被注释的 `CtptestGateway`） |
| `examples/client_server/run_server.py` | `main_engine.connect(setting, "CTP")` |
| `examples/data_recorder/data_recorder.py` | 通过 `CtpGateway` 录制行情 |
| `examples/no_ui/run.py` | 无界面模式连接 CTP |
| `docs/community/info/gateway.md` | 接口分类表：CTP / CTP测试 / CTP Mini / CTP期权 |
| `docs/community/info/introduction.md` | 支持接口列表（`ctp` / `ctptest` / `mini` / `sopt`） |
| `docs/community/install/mac_install.md` | Mac 编译步骤（注意：该文档有 3 处过时，见第 7 节） |

## 2. 生态中的 4 个 CTP 系接口（文档口径）

| 接口名 | 网关 ID | 覆盖品种 | 对应 PyPI 包 |
|--------|---------|----------|--------------|
| CTP | `ctp` | 期货、期货期权 | `vnpy_ctp` |
| CTP测试 | `ctptest` | 期货、期货期权（测试环境） | `vnpy_ctptest` |
| CTP Mini | `mini` | 期货、期货期权 | `vnpy_mini` |
| CTP期权 | `sopt` | ETF 期权 | `vnpy_sopt` |

## 3. 网关实际跑在交易链路上的 CTP 接口（核心子集）

`CtpGateway` 只直接使用 `CtpTdApi`（继承 `vnctp.TdApi`）/ `CtpMdApi`（继承 `vnctp.MdApi`）的少量方法，其余接口由包装层统一透出、可供策略侧直接调用。

| 环节 | 调用链 |
|------|--------|
| 交易 - 建立连接 | `CtpTdApi.connect` → `registerFront`（`RegisterFront`）+ `init`（`Init` + `Join`） |
| 交易 - 认证 | `onFrontConnected` → `reqAuthenticate`（`ReqAuthenticate`） |
| 交易 - 登录 | `onRspAuthenticate` → `reqUserLogin`（`ReqUserLogin`） |
| 交易 - 结算单确认 | `onRspUserLogin` → `reqSettlementInfoConfirm`（`ReqSettlementInfoConfirm`） |
| 交易 - 查询合约 | `onRspSettlementInfoConfirm` → `reqQryInstrument`（`ReqQryInstrument`，while 循环重试处理流控） |
| 交易 - 发送报单 | `CtpTdApi.send_order` → `reqOrderInsert`（`ReqOrderInsert`） |
| 交易 - 撤销报单 | `CtpTdApi.cancel_order` → `reqOrderAction`（`ReqOrderAction`） |
| 交易 - 查询资金 | `CtpTdApi.query_account` → `reqQryTradingAccount`（`ReqQryTradingAccount`） |
| 交易 - 查询持仓 | `CtpTdApi.query_position` → `reqQryInvestorPosition`（`ReqQryInvestorPosition`） |
| 交易 - 释放连接 | `CtpTdApi.close` → `exit`（`Release`） |
| 行情 - 建立连接并登录 | `CtpMdApi.connect` → `registerFront` + `init`；`onFrontConnected` → `reqUserLogin`（`ReqUserLogin`） |
| 行情 - 订阅行情 | `CtpMdApi.subscribe` → `subscribeMarketData`（`SubscribeMarketData`） |
| 行情 - 释放连接 | `CtpMdApi.close` → `exit`（`Release`） |

`CtpGateway` 实际实现的回调（其余 164+13 个回调由包装层透出、策略侧可自行覆盖）：

| 通道 | 回调 | 作用 |
|------|------|------|
| 交易 | `onFrontConnected` | 前置连接建立，触发认证/登录流程 |
| 交易 | `onFrontDisconnected` | 前置断开，通知重连 |
| 交易 | `onRspAuthenticate` | 客户端认证响应 |
| 交易 | `onRspUserLogin` | 登录响应，随后查询合约/资金/持仓并确认结算单 |
| 交易 | `onRspSettlementInfoConfirm` | 结算单确认响应 |
| 交易 | `onRspQryTradingAccount` | 资金查询响应 |
| 交易 | `onRspQryInvestorPosition` | 持仓查询响应 |
| 交易 | `onRspQryInstrument` | 合约查询响应 |
| 交易 | `onRspOrderInsert` | 报单被拒响应 |
| 交易 | `onRspOrderAction` | 撤单被拒响应 |
| 交易 | `onRspError` | 错误响应 |
| 交易 | `onRtnOrder` | 委托状态变化推送 |
| 交易 | `onRtnTrade` | 成交回报推送 |
| 行情 | `onFrontConnected` | 前置连接建立 |
| 行情 | `onFrontDisconnected` | 前置断开 |
| 行情 | `onRspUserLogin` | 行情登录响应 |
| 行情 | `onRspSubMarketData` | 订阅行情响应 |
| 行情 | `onRtnDepthMarketData` | 行情 tick 推送 |
| 行情 | `onRspError` | 错误响应 |

包装层向 Python 透出的方法数：交易 **299** 个（`req*` 直通 118、`on*` 回调 164），行情 **30** 个（`req*` 3、`on*` 13）。
网关内直通的原生请求调用点共 9 处：`reqAuthenticate`, `reqOrderAction`, `reqOrderInsert`, `reqQryInstrument`, `reqQryInvestorPosition`, `reqQryTradingAccount`, `reqSettlementInfoConfirm`, `reqUserLogin`、`subscribeMarketData`。

## 4. CTP 原生接口完整清单

以下为 `vnpy_ctp` 的 C++ 包装层（`vnctp`）实际调用的 CTP 原生接口。

### 4.1 交易接口 CThostFtdcTraderApi（134 个）

合计 **134** 个。

**1. 连接、注册与会话管理（非 Req 前缀）**（16）

| 接口 |
|------|
| `GetApiVersion` |
| `GetFrontInfo` |
| `GetTradingDay` |
| `Init` |
| `Join` |
| `RegisterFensUserInfo` |
| `RegisterFront` |
| `RegisterNameServer` |
| `RegisterSpi` |
| `RegisterUserSystemInfo` |
| `RegisterWechatUserSystemInfo` |
| `Release` |
| `SubmitUserSystemInfo` |
| `SubmitWechatUserSystemInfo` |
| `SubscribePrivateTopic` |
| `SubscribePublicTopic` |

**2. 认证与登录**（11）

| 接口 |
|------|
| `ReqAuthenticate` |
| `ReqGenUserCaptcha` |
| `ReqGenUserText` |
| `ReqTradingAccountPasswordUpdate` |
| `ReqUserAuthMethod` |
| `ReqUserLogin` |
| `ReqUserLoginWithCaptcha` |
| `ReqUserLoginWithOTP` |
| `ReqUserLoginWithText` |
| `ReqUserLogout` |
| `ReqUserPasswordUpdate` |

**3. 结算单确认**（1）

| 接口 |
|------|
| `ReqSettlementInfoConfirm` |

**4. 交易指令（报单、撤单、报价、执行、自对冲、组合、套利/套保确认）**（17）

| 接口 |
|------|
| `ReqBatchOrderAction` |
| `ReqCancelOffsetSetting` |
| `ReqCombActionInsert` |
| `ReqExecOrderAction` |
| `ReqExecOrderInsert` |
| `ReqForQuoteInsert` |
| `ReqOffsetSetting` |
| `ReqOptionSelfCloseAction` |
| `ReqOptionSelfCloseInsert` |
| `ReqOrderAction` |
| `ReqOrderInsert` |
| `ReqParkedOrderAction` |
| `ReqParkedOrderInsert` |
| `ReqQuoteAction` |
| `ReqQuoteInsert` |
| `ReqRemoveParkedOrder` |
| `ReqRemoveParkedOrderAction` |

**5.1 查询 - 账户 / 持仓 / 投资者 / 交易编码**（11）

| 接口 |
|------|
| `ReqQryInvestUnit` |
| `ReqQryInvestor` |
| `ReqQryInvestorInfoCommRec` |
| `ReqQryInvestorPosition` |
| `ReqQryInvestorPositionCombineDetail` |
| `ReqQryInvestorPositionDetail` |
| `ReqQrySecAgentTradingAccount` |
| `ReqQryTraderOffer` |
| `ReqQryTradingAccount` |
| `ReqQryTradingCode` |
| `ReqQryUserSession` |

**5.2 查询 - 合约 / 品种 / 交易所 / 行情**（10）

| 接口 |
|------|
| `ReqQryClassifiedInstrument` |
| `ReqQryCombInstrumentGuard` |
| `ReqQryDepthMarketData` |
| `ReqQryExchange` |
| `ReqQryInstrument` |
| `ReqQryInstrumentCommissionRate` |
| `ReqQryMMInstrumentCommissionRate` |
| `ReqQryProduct` |
| `ReqQryProductExchRate` |
| `ReqQryProductGroup` |

**5.3 查询 - 手续费 / 保证金 / 汇率 / 经纪商参数**（6）

| 接口 |
|------|
| `ReqQryBrokerTradingAlgos` |
| `ReqQryBrokerTradingParams` |
| `ReqQryExchangeRate` |
| `ReqQryMMOptionInstrCommRate` |
| `ReqQryOptionInstrCommRate` |
| `ReqQryOptionInstrTradeCost` |

**5.4 查询 - 委托 / 报价 / 执行 / 自对冲 / 组合**（11）

| 接口 |
|------|
| `ReqQryCombAction` |
| `ReqQryExecOrder` |
| `ReqQryForQuote` |
| `ReqQryInstrumentOrderCommRate` |
| `ReqQryMaxOrderVolume` |
| `ReqQryOffsetSetting` |
| `ReqQryOptionSelfClose` |
| `ReqQryOrder` |
| `ReqQryParkedOrder` |
| `ReqQryParkedOrderAction` |
| `ReqQryQuote` |

**5.5 查询 - 银行 / 银期 / 资金账户**（3）

| 接口 |
|------|
| `ReqQryCFMMCTradingAccountKey` |
| `ReqQryContractBank` |
| `ReqQryTransferBank` |

**5.6 查询 - 组合与风险参数（SPBM / RCAMS / RULE / SPMM）**（32）

| 接口 |
|------|
| `ReqQryEWarrantOffset` |
| `ReqQryExchangeMarginRate` |
| `ReqQryExchangeMarginRateAdjust` |
| `ReqQryInstrumentMarginRate` |
| `ReqQryInvestorCommodityGroupSPMMMargin` |
| `ReqQryInvestorCommoditySPMMMargin` |
| `ReqQryInvestorPortfMarginRatio` |
| `ReqQryInvestorPortfSetting` |
| `ReqQryInvestorProdRCAMSMargin` |
| `ReqQryInvestorProdRULEMargin` |
| `ReqQryInvestorProdSPBMDetail` |
| `ReqQryInvestorProductGroupMargin` |
| `ReqQryRCAMSCombProductInfo` |
| `ReqQryRCAMSInstrParameter` |
| `ReqQryRCAMSInterParameter` |
| `ReqQryRCAMSIntraParameter` |
| `ReqQryRCAMSInvestorCombPosition` |
| `ReqQryRCAMSShortOptAdjustParam` |
| `ReqQryRULEInstrParameter` |
| `ReqQryRULEInterParameter` |
| `ReqQryRULEIntraParameter` |
| `ReqQryRiskSettleInvstPosition` |
| `ReqQryRiskSettleProductStatus` |
| `ReqQrySPBMAddOnInterParameter` |
| `ReqQrySPBMFutureParameter` |
| `ReqQrySPBMInterParameter` |
| `ReqQrySPBMIntraParameter` |
| `ReqQrySPBMInvestorPortfDef` |
| `ReqQrySPBMOptionParameter` |
| `ReqQrySPBMPortfDefinition` |
| `ReqQrySPMMInstParam` |
| `ReqQrySPMMProductParam` |

**5.7 查询 - 通知 / 结算单**（4）

| 接口 |
|------|
| `ReqQryNotice` |
| `ReqQrySettlementInfo` |
| `ReqQrySettlementInfoConfirm` |
| `ReqQryTradingNotice` |

**5.8 查询 - 其他**（8）

| 接口 |
|------|
| `ReqQryAccountregister` |
| `ReqQryCombLeg` |
| `ReqQryCombPromotionParam` |
| `ReqQrySecAgentACIDMap` |
| `ReqQrySecAgentCheckMode` |
| `ReqQrySecAgentTradeInfo` |
| `ReqQryTrade` |
| `ReqQryTransferSerial` |

**6. 银期与资金划转**（4）

| 接口 |
|------|
| `ReqFromBankToFutureByFuture` |
| `ReqFromFutureToBankByFuture` |
| `ReqQueryBankAccountMoneyByFuture` |
| `ReqQueryCFMMCTradingAccountToken` |

### 4.2 行情接口 CThostFtdcMdApi（16 个）

合计 **16** 个。

**1. 连接、注册与会话管理**（9）

| 接口 |
|------|
| `GetApiVersion` |
| `GetTradingDay` |
| `Init` |
| `Join` |
| `RegisterFensUserInfo` |
| `RegisterFront` |
| `RegisterNameServer` |
| `RegisterSpi` |
| `Release` |

**2. 认证与登录**（2）

| 接口 |
|------|
| `ReqUserLogin` |
| `ReqUserLogout` |

**3. 行情订阅与询价订阅**（4）

| 接口 |
|------|
| `SubscribeForQuoteRsp` |
| `SubscribeMarketData` |
| `UnSubscribeForQuoteRsp` |
| `UnSubscribeMarketData` |

**4. 查询**（1）

| 接口 |
|------|
| `ReqQryMulticastInstrument` |

## 5. CTP 回调（SPI）完整清单

包装层实现了以下回调并全部透出给 Python。

### 5.1 交易回调 CThostFtdcTraderSpi（164 个）

合计 **164** 个。

**1. 连接与心跳**（3）

| 接口 |
|------|
| `OnFrontConnected` |
| `OnFrontDisconnected` |
| `OnHeartBeatWarning` |

**2. 错误响应**（1）

| 接口 |
|------|
| `OnRspError` |

**3. 登录与认证**（8）

| 接口 |
|------|
| `OnRspAuthenticate` |
| `OnRspGenUserCaptcha` |
| `OnRspGenUserText` |
| `OnRspTradingAccountPasswordUpdate` |
| `OnRspUserAuthMethod` |
| `OnRspUserLogin` |
| `OnRspUserLogout` |
| `OnRspUserPasswordUpdate` |

**4. 结算单**（3）

| 接口 |
|------|
| `OnRspQrySettlementInfo` |
| `OnRspQrySettlementInfoConfirm` |
| `OnRspSettlementInfoConfirm` |

**5. 错误回报（OnErrRtn*）**（18）

| 接口 |
|------|
| `OnErrRtnBankToFutureByFuture` |
| `OnErrRtnBatchOrderAction` |
| `OnErrRtnCancelOffsetSetting` |
| `OnErrRtnCombActionInsert` |
| `OnErrRtnExecOrderAction` |
| `OnErrRtnExecOrderInsert` |
| `OnErrRtnForQuoteInsert` |
| `OnErrRtnFutureToBankByFuture` |
| `OnErrRtnOffsetSetting` |
| `OnErrRtnOptionSelfCloseAction` |
| `OnErrRtnOptionSelfCloseInsert` |
| `OnErrRtnOrderAction` |
| `OnErrRtnOrderInsert` |
| `OnErrRtnQueryBankBalanceByFuture` |
| `OnErrRtnQuoteAction` |
| `OnErrRtnQuoteInsert` |
| `OnErrRtnRepealBankToFutureByFutureManual` |
| `OnErrRtnRepealFutureToBankByFutureManual` |

**6. 实时推送（OnRtn*）**（27）

| 接口 |
|------|
| `OnRtnBulletin` |
| `OnRtnCFMMCTradingAccountToken` |
| `OnRtnCancelAccountByBank` |
| `OnRtnChangeAccountByBank` |
| `OnRtnCombAction` |
| `OnRtnErrorConditionalOrder` |
| `OnRtnExecOrder` |
| `OnRtnForQuoteRsp` |
| `OnRtnFromBankToFutureByBank` |
| `OnRtnFromBankToFutureByFuture` |
| `OnRtnFromFutureToBankByBank` |
| `OnRtnFromFutureToBankByFuture` |
| `OnRtnInstrumentStatus` |
| `OnRtnOffsetSetting` |
| `OnRtnOpenAccountByBank` |
| `OnRtnOptionSelfClose` |
| `OnRtnOrder` |
| `OnRtnQueryBankBalanceByFuture` |
| `OnRtnQuote` |
| `OnRtnRepealFromBankToFutureByBank` |
| `OnRtnRepealFromBankToFutureByFuture` |
| `OnRtnRepealFromBankToFutureByFutureManual` |
| `OnRtnRepealFromFutureToBankByBank` |
| `OnRtnRepealFromFutureToBankByFuture` |
| `OnRtnRepealFromFutureToBankByFutureManual` |
| `OnRtnTrade` |
| `OnRtnTradingNotice` |

**7. 查询响应（OnRspQry*）**（104）

| 接口 |
|------|
| `OnRspBatchOrderAction` |
| `OnRspCancelOffsetSetting` |
| `OnRspCombActionInsert` |
| `OnRspExecOrderAction` |
| `OnRspExecOrderInsert` |
| `OnRspForQuoteInsert` |
| `OnRspFromBankToFutureByFuture` |
| `OnRspFromFutureToBankByFuture` |
| `OnRspOffsetSetting` |
| `OnRspOptionSelfCloseAction` |
| `OnRspOptionSelfCloseInsert` |
| `OnRspOrderAction` |
| `OnRspOrderInsert` |
| `OnRspParkedOrderAction` |
| `OnRspParkedOrderInsert` |
| `OnRspQryAccountregister` |
| `OnRspQryBrokerTradingAlgos` |
| `OnRspQryBrokerTradingParams` |
| `OnRspQryCFMMCTradingAccountKey` |
| `OnRspQryClassifiedInstrument` |
| `OnRspQryCombAction` |
| `OnRspQryCombInstrumentGuard` |
| `OnRspQryCombLeg` |
| `OnRspQryCombPromotionParam` |
| `OnRspQryContractBank` |
| `OnRspQryDepthMarketData` |
| `OnRspQryEWarrantOffset` |
| `OnRspQryExchange` |
| `OnRspQryExchangeMarginRate` |
| `OnRspQryExchangeMarginRateAdjust` |
| `OnRspQryExchangeRate` |
| `OnRspQryExecOrder` |
| `OnRspQryForQuote` |
| `OnRspQryInstrument` |
| `OnRspQryInstrumentCommissionRate` |
| `OnRspQryInstrumentMarginRate` |
| `OnRspQryInstrumentOrderCommRate` |
| `OnRspQryInvestUnit` |
| `OnRspQryInvestor` |
| `OnRspQryInvestorCommodityGroupSPMMMargin` |
| `OnRspQryInvestorCommoditySPMMMargin` |
| `OnRspQryInvestorInfoCommRec` |
| `OnRspQryInvestorPortfMarginRatio` |
| `OnRspQryInvestorPortfSetting` |
| `OnRspQryInvestorPosition` |
| `OnRspQryInvestorPositionCombineDetail` |
| `OnRspQryInvestorPositionDetail` |
| `OnRspQryInvestorProdRCAMSMargin` |
| `OnRspQryInvestorProdRULEMargin` |
| `OnRspQryInvestorProdSPBMDetail` |
| `OnRspQryInvestorProductGroupMargin` |
| `OnRspQryMMInstrumentCommissionRate` |
| `OnRspQryMMOptionInstrCommRate` |
| `OnRspQryMaxOrderVolume` |
| `OnRspQryNotice` |
| `OnRspQryOffsetSetting` |
| `OnRspQryOptionInstrCommRate` |
| `OnRspQryOptionInstrTradeCost` |
| `OnRspQryOptionSelfClose` |
| `OnRspQryOrder` |
| `OnRspQryParkedOrder` |
| `OnRspQryParkedOrderAction` |
| `OnRspQryProduct` |
| `OnRspQryProductExchRate` |
| `OnRspQryProductGroup` |
| `OnRspQryQuote` |
| `OnRspQryRCAMSCombProductInfo` |
| `OnRspQryRCAMSInstrParameter` |
| `OnRspQryRCAMSInterParameter` |
| `OnRspQryRCAMSIntraParameter` |
| `OnRspQryRCAMSInvestorCombPosition` |
| `OnRspQryRCAMSShortOptAdjustParam` |
| `OnRspQryRULEInstrParameter` |
| `OnRspQryRULEInterParameter` |
| `OnRspQryRULEIntraParameter` |
| `OnRspQryRiskSettleInvstPosition` |
| `OnRspQryRiskSettleProductStatus` |
| `OnRspQrySPBMAddOnInterParameter` |
| `OnRspQrySPBMFutureParameter` |
| `OnRspQrySPBMInterParameter` |
| `OnRspQrySPBMIntraParameter` |
| `OnRspQrySPBMInvestorPortfDef` |
| `OnRspQrySPBMOptionParameter` |
| `OnRspQrySPBMPortfDefinition` |
| `OnRspQrySPMMInstParam` |
| `OnRspQrySPMMProductParam` |
| `OnRspQrySecAgentACIDMap` |
| `OnRspQrySecAgentCheckMode` |
| `OnRspQrySecAgentTradeInfo` |
| `OnRspQrySecAgentTradingAccount` |
| `OnRspQryTrade` |
| `OnRspQryTraderOffer` |
| `OnRspQryTradingAccount` |
| `OnRspQryTradingCode` |
| `OnRspQryTradingNotice` |
| `OnRspQryTransferBank` |
| `OnRspQryTransferSerial` |
| `OnRspQryUserSession` |
| `OnRspQueryBankAccountMoneyByFuture` |
| `OnRspQueryCFMMCTradingAccountToken` |
| `OnRspQuoteAction` |
| `OnRspQuoteInsert` |
| `OnRspRemoveParkedOrder` |
| `OnRspRemoveParkedOrderAction` |

### 5.2 行情回调 CThostFtdcMdSpi（13 个）

合计 **13** 个。

| # | 接口 |
|---|------|
| 1 | `OnFrontConnected` |
| 2 | `OnFrontDisconnected` |
| 3 | `OnHeartBeatWarning` |
| 4 | `OnRspError` |
| 5 | `OnRspQryMulticastInstrument` |
| 6 | `OnRspSubForQuoteRsp` |
| 7 | `OnRspSubMarketData` |
| 8 | `OnRspUnSubForQuoteRsp` |
| 9 | `OnRspUnSubMarketData` |
| 10 | `OnRspUserLogin` |
| 11 | `OnRspUserLogout` |
| 12 | `OnRtnDepthMarketData` |
| 13 | `OnRtnForQuoteRsp` |

## 6. 官方 6.7.13 已声明、但 vnpy 未封装的接口

CTP API 6.7.13 相比 6.7.11 新增了以下接口，包装层（仍为 6.7.11.4 代码）**没有提供 Python 绑定**，
直接在策略里调用会报 `AttributeError`：

| 接口 | 功能（据官方 README） |
|------|----------------------|
| `ReqGenSMSCode` | 申请短信验证码 |
| `ReqSpdApply` | 套利确认请求 |
| `ReqSpdApplyAction` | 套利确认撤销请求 |
| `ReqQrySpdApply` | 套利确认查询请求 |
| `ReqHedgeCfm` | 套保确认请求 |
| `ReqHedgeCfmAction` | 套保确认撤销请求 |
| `ReqQryHedgeCfm` | 套保确认查询请求 |

补绑定的做法：在 `vnpy_ctp/api/vnctp/vnctptd/vnctptd.{h,cpp}` 中按现有 `reqXxx` 的模式加一个
「构造 `CThostFtdcXxxField` → 调用 `this->api->ReqXxx` → pybind `.def("reqXxx", ...)`」的三段式，
回调侧同理增加 `OnRspXxx`/`OnRtnXxx` 的 Python 分发。

**生产版 / 测评版开关是暴露的**：6.7.13 把「测评版」与「生产版」合并为同一个库，由
`CreateFtdcTraderApi(pszFlowPath, bIsProductionMode)` 的第二个参数选择。`CtpGateway` 通过配置项
**「柜台环境」**（`实盘` → `production_mode=True`，`测试` → `False`）透出该开关，并同样传给
`CreateFtdcMdApi`。连实盘与 SimNow 均选「实盘」；`测试` 档用于配合测评版逻辑。

## 7. macOS 安装实录（本机已验证可用）

官方 mac 包（SimNow 下载中心 → macOS API）与仓库内既有文件并不一致，实际可用步骤如下。

### 7.1 上游的两个坑

| 问题 | 现象 | 原因 |
|------|------|------|
| macOS framework 长期未更新 | 用 master（6.7.11.4）直接编译报 20 个 `unknown type name`（如 `CThostFtdcInputOffsetSettingField`） | 仓库里的 mac framework + `api/include/mac` 头文件停在 **CTP 6.7.7**（2025-03-30），而 C++ 包装层在 2025-10-13 升到了 6.7.11 |
| `meson.build` 链接参数写死 | `ld: library 'thostmduserapi_se' not found` | 6.7.11 的 `py.extension_module()` 里硬编码了 Linux 风格 `-L api/libs -lthostX -Wl,-rpath,$ORIGIN`，mac 分支里写好的 `-F/-framework` 依赖对象成了死代码；而 `api/libs` 里只有 Windows 的 `.lib` |

### 7.2 6.7.13 集成步骤（本次实际执行）

| 步骤 | 内容 |
|------|------|
| 1 | 从 SimNow 下载 `macOS_API_6.7.13`（含两个 `.framework.zip`），解压得到通用二进制 framework（x86_64 + arm64，macOS 10.15+） |
| 2 | 替换 `vnpy_ctp/api/thost{trader,md}api_se.framework` 与 `vnpy_ctp/api/include/mac/ctp/*.h` |
| 3 | 删除 `vnctptd.cpp` 中 mac 的 `ReqUserLogin(&myreq, reqid, 2, "vn")` 四参数特例 —— 6.7.13 起 mac 版回归标准两参数签名（采集逻辑已内嵌） |
| 4 | 修 `meson.build`：恢复按平台使用依赖对象（darwin 用 `-framework`，Windows 用 `find_library`，Linux 保留 `$ORIGIN` rpath） |
| 5 | 用 **仓库 venv 的 pip** 编译安装：`pip wheel . --no-build-isolation`（`PATH` 里加上 `.venv/bin` 以便找到 `meson`/`ninja`） |

### 7.3 三个容易踩的坑

1. **不要用 `pip3`**：它指向系统 Python（`/usr/local/bin/pip3`），装完你的项目依然 `import` 不到。用 `./.venv/bin/pip`。
2. **不要用 editable 安装**：新版 meson-python 的 editable 安装会在 import 时用隔离构建环境里的 ninja 增量重编（环境已删除 → `FileNotFoundError`），且编译产物落在 `build/cp312/` 而 framework 在源码树，`@loader_path` 找不到（`Library not loaded`）。改用普通 wheel 安装，framework 会与扩展**同级**装进 site-packages，运行期不再依赖源码目录。
3. **指南里的信任名单路径已过时**：`docs/community/install/mac_install.md` 第 84–85 行写的是 `api/libs/thost*_se.framework/...`，实际 framework 在 `api/` 下；且经 `git clone` + 本地编译的文件不带 `com.apple.quarantine`，本次**无需**手动去【访达】授权。

## 8. 为什么清单里没有"止盈/止损接口"

**CTP 原生 API 没有名为止盈/止损的 `Req*` 函数**（6.7.13 复核后依然如此）。CTP 的做法是把触发条件做成普通报单上的两个字段，挂在同一个 `ReqOrderInsert` 上：

| 字段 | 含义 | 说明 |
|------|------|------|
| `ContingentCondition` | 触发条件 | 6.7.13 头文件中 **16** 个常量：`THOST_FTDC_CC_Immediately('1')`、`Touch('2')`、`TouchProfit('3')`、`ParkedOrder('4')`、`LastPriceGreaterThanStopPrice('5')` … `BidPriceLesserEqualStopPrice('H')` |
| `StopPrice` | 触发价 | 6.7.13 结构体头文件中出现 **6** 处（InputOrder / Order / ParkedOrder 等） |

### 8.1 包装层支持，网关没用

| 环节 | 情况 |
|------|------|
| 包装层 `reqOrderInsert` | **支持**：读取并透传 `ContingentCondition`、`StopPrice` |
| `CtpGateway.send_order` | **未使用**：`"ContingentCondition": THOST_FTDC_CC_Immediately` 写死，从不传 `StopPrice` |
| `ORDERTYPE_VT2CTP` | 只映射 `LIMIT` / `MARKET` / `FAK` / `FOK`；`OrderType.STOP` 会被拒，日志提示"当前接口不支持该类型的委托STOP" |

### 8.2 vnpy 中止盈止损的真实实现位置

止盈止损在**策略层**，不在网关层：核心仓库只提供 `OrderType.STOP` 枚举（`vnpy/trader/constant.py`）与
`ContractData.stop_supported`（`vnpy/trader/object.py`，默认 `False`），`BaseGateway` 抽象里没有停止单方法。

`vnpy_ctastrategy` 的 CTA 引擎按 `contract.stop_supported` 分流：

```
stop=True
 ├─ contract.stop_supported == True  → send_server_stop_order()   # 柜台原生条件单
 └─ contract.stop_supported == False → send_local_stop_order()    # 本地停止单
```

CTP 合约未设置 `stop_supported`（默认 `False`）→ 走**本地停止单**：`check_stop_order(tick)` 在每个 tick 上遍历
`self.stop_orders`，多头 `tick.last_price >= price` 触发后转为普通报单。触发发生在自己的 vnpy 进程内，
**进程退出或断线即失效**。

实务做法：CTA 策略里用 `self.send_order(direction, offset, price, volume, stop=True)`；或在自己的 `on_tick`
中判断价格后 `send_order`。若要走柜台侧条件单，则需绕过网关直接调 `reqOrderInsert` 并自行填
`ContingentCondition` + `StopPrice`，且需实测所在期货公司柜台是否受理。
