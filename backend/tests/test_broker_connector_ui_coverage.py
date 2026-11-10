"""后端「可用 connector」必须在前端接入模式里有入口（P0-3 结构性护栏）。

成因（本轮修的半吊子状态）：2026-10-23 后 ``connectors/registry.py`` 把 5 个
大 QMT 组合标退役、只留 ``qmt.big.closed`` 可用且推荐，前端却只把历史模板改成
「已停用」而**没有补闭环入口**——模板 hint 写着「请改用闭环」但 UI 里选不到，
默认值还停在已停用的 ``direct``，用户不改就提交一条必然失败的连接。

单靠前端单测挡不住「后端清单变了、前端没跟上」：前端测试用的是自己造的 mock
清单，registry 加一个新组合它照样全绿。所以这里反过来，**从前端源码里抽出
真实接线用的 key**，与 registry 的可用集合对账——两边任一漂移都会红。

匹配口径（与 ``check_capability_coverage.py`` 的「声明不算入口」同源）：
只认 ``ACCESS_MODE_KEYS`` 对象里的值与 ``availabilityKeyForMode`` 的 direct 特判，
且先剥掉 ``//`` 注释，避免注释里出现一个 key 就把门禁骗绿。
"""
from __future__ import annotations

import re
from pathlib import Path

from connectors.registry import available_keys, is_retired

ROOT = Path(__file__).resolve().parents[2]
BROKERS_TSX = ROOT / "frontend-next" / "src" / "domains" / "system" / "Brokers.tsx"

#: 有意不在「券商连接」页承载的组合，必须写明理由（不允许静默忽略）。
#: 退役项不需要登记：UI 里保留历史项是选择，不保留也不算缺口。
UI_EXEMPT: dict[str, str] = {
    "ptrade.http": "PTrade 是独立产品域；本页是大 QMT / xtquant 连接器表单",
}


def _frontend_connector_keys() -> set[str]:
    """抽出前端**真正用于提交/判可用性**的 connector key 字面量。"""
    src = BROKERS_TSX.read_text(encoding="utf-8")
    block = re.search(
        r"export const ACCESS_MODE_KEYS\s*:\s*Record<string, string>\s*=\s*\{(.*?)\n\};",
        src, re.S)
    assert block is not None, (
        "Brokers.tsx 里找不到 ACCESS_MODE_KEYS 映射表；"
        "接入模式必须与后端 connector 清单可对账")
    body = re.sub(r"//[^\n]*", "", block.group(1))   # 注释里的 key 不算入口
    keys = {v for v in re.findall(r'^\s*\w+\s*:\s*"([^"]+)"', body, re.M)}

    # direct 提交空 connector_key，它的可用性 key 在函数体里特判。
    fn = re.search(r"export function availabilityKeyForMode.*?\n\}", src, re.S)
    assert fn is not None, "找不到 availabilityKeyForMode，无法核对 direct 的可用性 key"
    fn_body = re.sub(r"//[^\n]*", "", fn.group(0))
    keys |= set(re.findall(r'"(qmt\.[a-z.]+)"', fn_body))
    return keys


def _expected_ui_keys() -> set[str]:
    return {k for k in available_keys() if not is_retired(k)} - set(UI_EXEMPT)


def test_every_available_connector_has_frontend_entry():
    """唯一真源里的可用组合，界面上必须选得到（否则就是「指引指向不存在的路」）。"""
    missing = _expected_ui_keys() - _frontend_connector_keys()
    assert not missing, (
        f"以下可用 connector 在 Brokers.tsx 接入模式里没有入口：{sorted(missing)}；"
        "补 ACCESS_MODE_KEYS 映射，或在 UI_EXEMPT 里写明为什么本页不该承载")


def test_frontend_keys_are_all_known_to_registry():
    """前端拼错 key（或后端删了组合）时立刻红，别等提交才 400。"""
    unknown = _frontend_connector_keys() - set(available_keys())
    assert not unknown, f"前端接入模式引用了 registry 里不存在的 key：{sorted(unknown)}"


def test_exempt_keys_are_still_in_registry():
    """豁免表不许腐烂：指向一个早已删除的 key 会让门禁悄悄失效。"""
    stale = set(UI_EXEMPT) - set(available_keys())
    assert not stale, f"UI_EXEMPT 里有 registry 不存在的 key：{sorted(stale)}"


def test_closed_loop_is_available_and_not_retired():
    """闭环是 2026-10-23 后大 QMT 唯一可执行路径，不能被误标退役。

    与 ``test_broker_connectors_route`` 的端点断言互补：那里验 REST 输出，
    这里验注册表口径本身（端点是在它之上加的一层）。
    """
    assert "qmt.big.closed" in available_keys()
    assert not is_retired("qmt.big.closed")
    assert "qmt.big.closed" in _frontend_connector_keys()
