"""涨停监控池：**名称必须真的去查**，否则 ST 涨停幅度判错（2026-09-20 实测发现）。

## 缺陷回顾

`engines/limitup.py::LimitUpMonitor.add` 此前是：

```python
self._pool[code] = name or code          # ← 未传 name 时把**代码当名称**
```

而 `routes/limitup.py::limitup_pool_add` 调的是 `m.add(body.get("code", ""))`
（**从不传 name**），MCP 工具 `limitup_pool_add` 同样是 `_monitor().add(code)`。
于是池子里的「名称」恒等于代码。实测：

    POST /limitup/pool {"code":"600000.SH"}
    → {"code":"600000.SH","name":"600000.SH"}

两个后果，都静默、都不报错：

1. **功能（严重）**：`_limit_factor(code, name)` 靠「名称含 ST」判 **5%** 涨停幅度。
   名称恒等于代码 ⇒ ST 分支永不命中 ⇒ **ST 股被按 10% 判涨停，而它 5% 就封板了
   ⇒ 永远等不到触发**。用户看到的是「今天没涨停」，实际是监控对 ST 股完全失效。
   这与选股里 `_is_st(names.get(c, ""))` 恒 False 是**同一个根因**。

2. **展示**：界面「名称」列把代码当名称显示 —— 拿占位值冒充真实数据。

## 修法与单一真相来源

名称表**本来就在**运行时数据目录（与 app.db 同目录）的 `stock_names.json`
（真实安装 215 KB / 7175 条）。查询已收敛到
`datasource/eltdx_utils.py::lookup_name / lookup_names` **唯一一份实现**
（选股股票池与涨停池共用，避免两处各写一套再次漂移）。

`add()` 改为：未显式传 name 时查名称表；**查不到留空**，绝不再拿代码顶上。

## 本文件锁死什么

1. `add()` 未传 name 时会去查本地名称表；
2. 查不到时 `name == ""`（**不得**回退成代码）；
3. **功能级**：`add()` 之后 `_limit_factor` 拿到的是真名 ⇒ ST 股判 5%；
4. 反向锁死：若把 `add()` 改回 `name or code`，第 3 条必须失败。
"""
from _phase4_support import FakeStore  # noqa: F401  (保持与其他选股测试一致的导入环境)

import datasource.eltdx_utils as EU

from engines.limitup import LimitUpMonitor, _limit_factor


def _monitor() -> LimitUpMonitor:
    # 只测池子与名称，不需要真实券商/风控
    return LimitUpMonitor(manager=None)


# ---- add() 必须查名称表 ----------------------------------------------------

def test_add_resolves_name_from_local_table(monkeypatch):
    """★ 核心回归：未传 name 时必须查本地名称表（此前直接拿代码当名称）。"""
    monkeypatch.setattr(EU, "_NAME_MEMO", {"600000.SH": "浦发银行"})
    m = _monitor()
    got = m.add("600000.SH")
    assert got["name"] == "浦发银行", f"未查名称表：{got}"


def test_add_never_uses_code_as_name_when_unknown(monkeypatch):
    """★ 核心回归：查不到就留空 —— 零 mock，绝不拿代码冒充名称。"""
    monkeypatch.setattr(EU, "_NAME_MEMO", {})
    m = _monitor()
    got = m.add("999999.XX")
    assert got["name"] == "", f"未知代码不得回退成代码当名称：{got}"
    assert m.status()["pool"][0]["name"] == ""


def test_add_prefers_explicit_name(monkeypatch):
    """调用方显式给了名称就用它（不被本地表覆盖）。"""
    monkeypatch.setattr(EU, "_NAME_MEMO", {"600000.SH": "本地旧名"})
    m = _monitor()
    assert m.add("600000.SH", "调用方给的")["name"] == "调用方给的"


def test_add_normalizes_code_and_lookup_is_uppercase(monkeypatch):
    monkeypatch.setattr(EU, "_NAME_MEMO", {"600000.SH": "浦发银行"})
    m = _monitor()
    assert m.add(" 600000.sh ")["name"] == "浦发银行"


# ---- 功能级：ST 涨停幅度必须判对 -------------------------------------------

def test_st_stock_added_without_name_still_gets_5pct_limit(monkeypatch):
    """★★ 核心回归（功能级）：ST 股入池后 `_limit_factor` 必须判 5%。

    这正是缺陷的用户可见后果：名称拿不到 ⇒ `_limit_factor` 走代码前缀分支
    ⇒ 主板 ST 判 10% ⇒ 实际 5% 就封板 ⇒ **永远等不到触发**。
    """
    monkeypatch.setattr(EU, "_NAME_MEMO", {"600000.SH": "ST浦发"})
    m = _monitor()
    m.add("600000.SH")
    name = m._pool.get("600000.SH", "")
    assert _limit_factor("600000.SH", name) == 0.05, (
        f"ST 股被按 10% 判涨停（名称={name!r}）⇒ 该股永远等不到触发")


def test_non_st_stock_still_10pct(monkeypatch):
    """反向保护：非 ST 主板股仍是 10%（修复不能把所有票都判成 ST）。"""
    monkeypatch.setattr(EU, "_NAME_MEMO", {"600000.SH": "浦发银行"})
    m = _monitor()
    m.add("600000.SH")
    assert _limit_factor("600000.SH", m._pool.get("600000.SH", "")) == 0.10


def test_reverse_lock_old_behaviour_would_miss_st():
    """反向锁死：**把名称设成代码**（旧行为）时 ST 必然判错 —— 说明本修复真的有效。

    这条不依赖名称表，只证明「名称=代码」这条路径确实会漏判 ST；
    若哪天有人把 `add()` 改回 `name or code`，上面那条核心回归会失败，
    而这条说明它失败的原因。
    """
    assert _limit_factor("600000.SH", "600000.SH") == 0.10
    assert _limit_factor("600000.SH", "ST浦发") == 0.05


# ---- status() 契约 ---------------------------------------------------------

def test_status_pool_exposes_code_and_name(monkeypatch):
    monkeypatch.setattr(EU, "_NAME_MEMO", {"600000.SH": "浦发银行",
                                           "300750.SZ": "宁德时代"})
    m = _monitor()
    m.add("600000.SH")
    m.add("300750.SZ")
    pool = {p["code"]: p["name"] for p in m.status()["pool"]}
    assert pool == {"600000.SH": "浦发银行", "300750.SZ": "宁德时代"}


def test_remove_and_reset_still_work(monkeypatch):
    """回归保护：名称改动不得影响增删/重置语义。"""
    monkeypatch.setattr(EU, "_NAME_MEMO", {"600000.SH": "浦发银行"})
    m = _monitor()
    m.add("600000.SH")
    m.remove("600000.SH")
    assert m.status()["pool"] == []
    m.add("600000.SH")
    m._triggered.add("600000.SH")
    m.reset_triggered()
    assert m._triggered == set()
