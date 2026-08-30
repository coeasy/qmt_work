"""qmt_work 统一数据返回容器（G1-3）。

来源借鉴：OpenBB `OBBject` —— 任何数据端点的返回不只是一个裸 list，而是携带
``source`` / ``as_of`` / ``stale`` / ``warnings`` 的富容器，使「降级 ≠ 造假」
可被强制执行：当数据来自本地缓存或远端失败回退时，必须显式标记 ``stale=True``
并附 ``as_of``（数据时间戳）与 ``source``，前端据此提示「数据截至 X，未取得最新行情」。

与路由信封的关系：
- 路由层仍用 ``ok({...})`` / ``err(code, msg)`` 信封（``{code, message, data}``）。
- ``DataResult`` 是**数据源层**的内部契约：``DataSource`` / ``DataSourceManager``
  产出的「带溯源信息的数据」统一包成 ``DataResult``；路由在组装 ``ok()`` 时
  取出 ``results`` 并据 ``stale``/``as_of``/``source`` 决定是否追加陈旧提示。
- 本批次仅定义容器与契约，端点实际接入（G1-6 降级策略）后续子批次落地。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class DataResult:
    """统一数据返回容器。

    字段：
    - ``results``：核心数据（list[标准模型] / dict / 任意可序列化对象）。
    - ``source``：数据来源标识（"broker:ths" / "eltdx" / "local:sqlite" 等）。**必填**。
    - ``as_of``：数据时间戳（ISO 字符串）。``stale=True`` 时**必填**，否则无意义。
    - ``stale``：是否为陈旧/降级数据（远程失败回退本地、或本地缓存未刷新）。
    - ``warnings``：非阻断告警（如部分字段缺失、源降级说明）。
    - ``extra``：附加元信息（原始响应码、上游延迟等），可选。
    """

    results: Any
    source: str
    as_of: Optional[str] = None
    stale: bool = False
    warnings: List[str] = field(default_factory=list)
    extra: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # 契约护栏：标记陈旧必须附时间戳，否则降级将「静默造假」。
        if self.stale and not self.as_of:
            raise ValueError(
                "DataResult.stale=True 必须提供 as_of（数据时间戳），否则前端无法区分"
                "『陈旧但明示』与『伪造最新』——违反零 mock 铁律。"
            )
        if not self.source:
            raise ValueError("DataResult.source 不能为空，须标明数据来源以便溯源。")

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {
            "results": self.results,
            "source": self.source,
            "as_of": self.as_of,
            "stale": self.stale,
            "warnings": list(self.warnings),
        }
        if self.extra:
            d["extra"] = self.extra
        return d

    @classmethod
    def from_source(
        cls,
        data: Any,
        source: str,
        *,
        stale: bool = False,
        as_of: Optional[str] = None,
        warnings: Optional[List[str]] = None,
        **extra: Any,
    ) -> "DataResult":
        """便捷构造：从「源 + 数据」直接产出容器。

        ``stale=True`` 时调用方**必须**传入 ``as_of``（契约由 ``__post_init__`` 强制）。
        """
        return cls(
            results=data,
            source=source,
            stale=stale,
            as_of=as_of,
            warnings=list(warnings or []),
            extra=extra or {},
        )


__all__ = ["DataResult"]
