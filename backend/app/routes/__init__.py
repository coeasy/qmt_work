# 路由聚合：各业务域子模块统一挂载到 /api/v1（保持 main.py 的 include_router(router) 接口不变）。
from fastapi import APIRouter

import app.routes.account as account
import app.routes.alerts as alerts
import app.routes.algo as algo
import app.routes.analysis as analysis
import app.routes.apikeys as apikeys
import app.routes.audit as audit
import app.routes.backtest as backtest
import app.routes.broker as broker
import app.routes.capabilities as capabilities
import app.routes.config as config
import app.routes.factors as factors
import app.routes.health as health
import app.routes.indicators as indicators
import app.routes.limitup as limitup
import app.routes.market as market
import app.routes.metrics as metrics
import app.routes.notifications as notifications
import app.routes.paper as paper
import app.routes.rebalance as rebalance
import app.routes.reconcile as reconcile
import app.routes.reference as reference
import app.routes.research as research
import app.routes.runtime as runtime
import app.routes.screen as screen
import app.routes.signal as signal
import app.routes.strategies as strategies
import app.routes.strategy_market as strategy_market
import app.routes.strategy_run as strategy_run
import app.routes.sync as sync
import app.routes.target_portfolio as target_portfolio
import app.routes.trade as trade
import app.routes.webhooks as webhooks
import app.routes.ws as ws

router = APIRouter(prefix="/api/v1")
router.include_router(broker.router)
router.include_router(account.router)
router.include_router(rebalance.router)
router.include_router(backtest.router)
router.include_router(market.router)
router.include_router(config.router)
router.include_router(health.router)
router.include_router(audit.router)
router.include_router(metrics.router)
router.include_router(apikeys.router)
router.include_router(notifications.router)
router.include_router(webhooks.router)
router.include_router(alerts.router)
router.include_router(reconcile.router)
router.include_router(signal.router)
router.include_router(target_portfolio.router)
router.include_router(limitup.router)
router.include_router(algo.router)
router.include_router(reference.router)
router.include_router(strategies.router)
router.include_router(strategy_run.router)
router.include_router(trade.router)
router.include_router(sync.router)
router.include_router(ws.router)
router.include_router(factors.router)
router.include_router(research.router)
router.include_router(paper.router)
router.include_router(strategy_market.router)
router.include_router(capabilities.router)
router.include_router(indicators.router)
router.include_router(screen.router)
router.include_router(analysis.router)
router.include_router(runtime.router)
