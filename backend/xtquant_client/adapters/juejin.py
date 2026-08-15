"""掘金量化客户端适配器（接口契约，待接入真实掘金 gm SDK）。"""
from . import ExternalBrokerAdapter


class JuejinAdapter(ExternalBrokerAdapter):
    broker_name = "掘金量化"
    adapter_id = "juejin"
    sdk_required = "gm"

    def _install_hint(self) -> str:
        return ("掘金量化需 `pip install gm` 并完成终端登录，参照 gm SDK 文档实现 "
                "行情/交易/账户调用后启用本适配器。")
