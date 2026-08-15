"""同花顺量化客户端适配器（接口契约，待接入真实同花顺 SDK）。"""
from . import ExternalBrokerAdapter


class ThsAdapter(ExternalBrokerAdapter):
    broker_name = "同花顺"
    adapter_id = "ths"
    sdk_required = "ths_quant_sdk"

    def _install_hint(self) -> str:
        return ("同花顺量化客户端（THS Quant）需单独安装，并参照其 SDK 文档实现 "
                "行情/交易/账户调用后启用本适配器。")
