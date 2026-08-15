"""恒生 PTrade 客户端适配器（接口契约，待接入真实 PTrade SDK）。"""
from . import ExternalBrokerAdapter


class PTradeAdapter(ExternalBrokerAdapter):
    broker_name = "恒生PTrade"
    adapter_id = "ptrade"
    sdk_required = "ptrade_sdk"

    def _install_hint(self) -> str:
        return ("恒生 PTrade / iFinD 客户端需单独安装，并参照其 SDK 文档实现 "
                "行情/交易/账户调用后启用本适配器。")
