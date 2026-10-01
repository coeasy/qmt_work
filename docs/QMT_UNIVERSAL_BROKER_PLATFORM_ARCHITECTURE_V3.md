# QMT_UNIVERSAL_BROKER_PLATFORM_ARCHITECTURE_V3

> ⚠️ **已归档（愿景稿）**：执行口径请改用 **`docs/UNIVERSAL_BROKER_PLATFORM_FINAL_PLAN_V4.md`**。
> V4 继承了本文的目标定位，并修订了 12 项内容（补 Orchestration 不可绕过层、Dialect×Transport 正交、
> 运行时 Capability、EventPort、每阶段的 DoD/门禁等）。逐条对照见 V4 附录 A。

## qmt_work 证券客户端统一适配平台架构设计方案

版本：V3.0

------------------------------------------------------------------------

# 1. 项目定位升级

qmt_work 不再定位为单一 QMT 工具，而升级为：

**Universal Securities Broker Runtime Platform**

证券客户端统一适配平台。

目标：

-   MiniQMT
-   BigQMT
-   PTrade
-   同花顺量化
-   掘金
-   其他券商 SDK

通过统一接口接入。

------------------------------------------------------------------------

# 2. 总体架构

    qmt_work

        |
        |
    Universal Trading Core

        |
        |
    Broker Abstraction Layer

        |
    ------------------------------------------------
    |              |              |                |
    MiniQMT       BigQMT        PTrade        Other
    Adapter       Adapter       Adapter       Adapter
    ------------------------------------------------

        |

    Unified Domain Model

        |

    REST / MCP / WebSocket / SDK

------------------------------------------------------------------------

# 3. Broker Core 抽象层

核心接口：

-   connect
-   disconnect
-   account
-   positions
-   orders
-   trades
-   submit_order
-   cancel_order
-   subscribe_market

业务层不直接依赖具体客户端。

------------------------------------------------------------------------

# 4. 统一领域模型

## OrderRequest

统一订单：

-   account_id
-   symbol
-   side
-   order_type
-   price
-   quantity

## Position

统一持仓：

-   symbol
-   quantity
-   available
-   cost
-   pnl

## Account

统一账户：

-   total_asset
-   cash
-   market_value
-   profit_loss

------------------------------------------------------------------------

# 5. MiniQMT 与 BigQMT 架构

## MiniQMT

    qmt_work

     |

    MiniQMT Adapter

     |

    XTQuant Mini

     |

    MiniQMT Client

------------------------------------------------------------------------

## BigQMT

采用 Agent 隔离：

    qmt_work

     |

    Broker Service

     |

    RPC

     |

    BigQMT Agent

     |

    XTQuant Full

     |

    QMT Client

优势：

-   Python 环境隔离
-   DLL 隔离
-   多版本共存
-   崩溃隔离

------------------------------------------------------------------------

# 6. Runtime Manager

负责：

-   自动发现客户端
-   加载运行环境
-   生命周期管理
-   健康检查
-   自动恢复

结构：

    runtime/

     detector.py
     loader.py
     manager.py
     health.py

------------------------------------------------------------------------

# 7. Adapter 插件体系

目录：

    broker/adapters/

     qmt_mini/

     qmt_big/

     ptrade/

     ths/

     easytrader/

每个 Adapter 实现：

-   连接
-   数据转换
-   下单
-   查询
-   状态管理

------------------------------------------------------------------------

# 8. Capability 能力模型

不同客户端能力不同。

例如：

``` json
{
 "broker":"bigqmt",
 "features":{
   "trade":true,
   "algo_order":true,
   "margin":false
 }
}
```

前端根据能力动态展示。

------------------------------------------------------------------------

# 9. 多账户路由

支持：

    账户A -> MiniQMT

    账户B -> BigQMT

    账户C -> PTrade

统一：

    Account Router

            |

    Broker Runtime

------------------------------------------------------------------------

# 10. EasyTrader 兼容设计

不直接复制旧 API。

采用：

    EasyTrader Compatibility Layer

                |

    Unified Broker Core

旧策略可以迁移，新系统保持现代化。

------------------------------------------------------------------------

# 11. 扩展方向

未来支持：

-   第三方 Broker Plugin
-   Python SDK
-   MCP Agent 调用
-   多客户端组合运行
-   企业级账户管理

------------------------------------------------------------------------

# 12. 实施路线

## Phase 1

Broker Core 抽象。

## Phase 2

迁移 MiniQMT。

## Phase 3

实现 BigQMT Agent。

## Phase 4

接入 PTrade、同花顺等客户端。

## Phase 5

形成插件生态。

------------------------------------------------------------------------

# 13. 最终目标

qmt_work 成为：

> 国内证券交易客户端统一运行平台。

类似：

-   CCXT 的交易抽象思想
-   EasyTrader 的客户端兼容思想
-   现代 Runtime Plugin 架构

融合形成统一证券 Broker 平台。
