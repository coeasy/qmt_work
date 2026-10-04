"""R1 数据完整性修复的回归测试（纯逻辑，无网络 / 无券商）。

覆盖 2026-10-04 R1 审计发现的四个真缺陷：
1. ``_pfx6`` 只认 'sh600519' 形态，QMT 形态 '600519.SH' 抛异常
   → stock_topics / stock_score 对平台最常见入参形态静默降级为空。
2. eltdx_source 传裸 6 位代码（_num），门面对无前缀代码一律加 'sh'
   → 所有深市 000xxx/300xxx 个股被当成沪市查询，行业与题材恒空。
3. 行业/题材空结果被无条件永久缓存 → 一次网络抖动即永久掩盖真实数据。
4. get_quote 与 get_instrument_detail 各写一份字段映射 → 必然漂移。

运行：``pytest tests/test_tdx_data_completeness.py -q``
"""
from __future__ import annotations

import asyncio
import time

import pytest

from datasource.eltdx_industry import _INDUSTRY_RETRY_TTL
from datasource.eltdx_source import EltdxSource
from datasource.tdx_transport import _pfx6

CODE = "600519.SH"          # 测试标的（沪市主板）
SZ_CODE = "300750.SZ"       # 深市创业板（R1 实测恒空的标的）
KCB_CODE = "688981.SH"      # 科创板（R1 实测恒空的标的）


# --------------------------------------------------------------------------
# 1. _pfx6 代码形态规范化
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("sh600519", ("sh", "600519")),        # TDX 原生形态
        ("sz000001", ("sz", "000001")),
        ("bj920002", ("bj", "920002")),
        ("600519.SH", ("sh", "600519")),       # QMT / Wind 形态
        ("000001.SZ", ("sz", "000001")),
        ("920002.BJ", ("bj", "920002")),
        ("sh600519", ("sh", "600519")),        # 混写形态
        ("SH600519", ("sh", "600519")),        # 大小写不敏感
        (" 600519.SH ", ("sh", "600519")),     # 首尾空白
        ("600519", ("sh", "600519")),          # 纯数字（沪市推断）
        ("300750", ("sz", "300750")),          # 纯数字（深市推断）
        ("920002", ("bj", "920002")),          # 纯数字（北交所推断）
    ],
)
def test_pfx6_accepts_all_code_forms(raw, expected):
    assert _pfx6(raw) == expected


@pytest.mark.parametrize("bad", ["", "none", "60051", "999999999", "sh", "abc"])
def test_pfx6_rejects_invalid(bad):
    with pytest.raises(ValueError):
        _pfx6(bad)


def test_pfx6_bare_000001_ambiguity_is_documented():
    """'000001' 裸代码天然歧义（上证指数 SH / 平安银行 SZ）。

    按通达信自身约定取 'sh'；需要精确指定深市个股时必须传 'sz000001'。
    固化该约定，避免后续被「顺手修好」而悄悄改变指数查询语义。
    """
    assert _pfx6("000001") == ("sh", "000001")
    assert _pfx6("000001.SZ") == ("sz", "000001")


# --------------------------------------------------------------------------
# 2. 字段映射单点定义（杜绝 get_quote / get_instrument_detail 漂移）
# --------------------------------------------------------------------------


def test_snap_metric_map_is_single_source_of_truth():
    """两路行情出口共用同一份映射表，且覆盖前端契约要求的全部指标。"""
    from datasource.base import EXT_DETAIL_KEYS

    required = {"open", "high", "low", "avg_price", "amplitude", "turnover_rate",
                "volume_ratio", "pe_ttm", "pb", "circ_mv", "total_mv", "amount"}
    missing = required - set(EltdxSource._SNAP_METRIC_MAP)
    assert not missing, f"映射表缺项：{missing}"
    # 契约清单里凡能由快照原生提供的字段，映射表都必须有对应关系
    assert set(EltdxSource._SNAP_METRIC_MAP) & set(EXT_DETAIL_KEYS), (
        "映射表与契约清单无任何交集，说明两侧已完全漂移"
    )


def test_snap_metric_map_targets_real_transport_fields():
    """映射表两个消费方都必须引用同一份 _SNAP_METRIC_MAP（防各写一份漂移）。"""
    import inspect

    from datasource import eltdx_source

    src = inspect.getsource(eltdx_source)
    for method in ("get_quote", "get_instrument_detail"):
        code = inspect.getsource(getattr(EltdxSource, method))
        assert "_SNAP_METRIC_MAP" in code, (
            f"{method} 未引用 _SNAP_METRIC_MAP —— 可能各写了一份映射，必然漂移"
        )
    # 全模块内 _SNAP_METRIC_MAP 只能有一处字面定义（类属性），其余都是引用
    assert src.count("_SNAP_METRIC_MAP = {") == 1, "映射表存在重复定义"


# --------------------------------------------------------------------------
# 3. 行业/题材空结果的 retry 窗口
# --------------------------------------------------------------------------


class _FakeNS:
    """极简命名空间：属性赋值后 .get() 可读。"""

    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)

    def get(self, key, default=None):
        return getattr(self, key, default)


class _FakeClient:
    """受控假客户端：返回预设的行业与题材。"""

    def __init__(self, industry: str, concepts: list):
        class _F10:
            def __init__(self, ind):
                self._ind = ind

            def stock_score(self, num, section="pf"):
                return _FakeNS(rows=[{"N012": self._ind}] if self._ind else [])

        class _Helpers:
            def __init__(self, cs):
                self._cs = cs

            def stock_topics(self, num):
                return _FakeNS(topics=[_FakeNS(topic_name=c) for c in self._cs])

        self.f10 = _F10(industry)
        self.helpers = _Helpers(concepts)


@pytest.fixture
def src(monkeypatch):
    """构造不走 __init__（避免网络）的实例，并隔离类级共享状态。

    ★ 必须**逐模块**打桩，不能只打 ``eltdx_source``（2026-10-05 P1-1 拆分踩到）：
    两个模块都是 ``from datasource.eltdx_utils import _save_json_cache``，
    即各自持有一份**独立绑定**。行业能力拆到 ``eltdx_industry`` 之后，
    只打 ``eltdx_source._save_json_cache`` 拦不住行业侧的落盘
    —— 测试当场变红（"拿到真值后应落盘" 断在 0==1）。
    这与 R21「函数内局部 import 绕过模块级替换点」是同一族：
    **打桩要打在真正发起调用的那个模块上**，而不是名字的来源地。
    """
    for mod in ("datasource.eltdx_source", "datasource.eltdx_industry"):
        monkeypatch.setattr(f"{mod}._save_json_cache",
                            lambda *a, **k: None)      # 不落盘，避免污染 data/
        monkeypatch.setattr(f"{mod}._load_json_cache",
                            lambda *a, **k: None)
    EltdxSource._industry_map.clear()
    EltdxSource._industry_ts.clear()
    EltdxSource._industry_loaded = True                # 跳过磁盘加载
    return EltdxSource.__new__(EltdxSource)


def _install_network(src, industry: str, concepts: list, calls: list | None = None) -> list:
    """把网络调用替换成受控返回，返回调用计数器（可复用同一列表跨多次安装）。"""
    if calls is None:
        calls = []

    def _fake_use(fn):
        calls.append(fn)
        return fn(_FakeClient(industry, concepts))

    src._use_client = _fake_use
    return calls


def test_industry_nonempty_result_is_cached_permanently(src):
    """非空结果照常命中缓存，不因 TTL 到期而反复打网络。"""
    calls = _install_network(src, "酿酒", ["白酒概念"])

    r1 = asyncio.run(src._get_industry(CODE))
    r2 = asyncio.run(src._get_industry(CODE))

    assert r1["industry"] == "酿酒"
    assert r1["concepts"] == ["白酒概念"]
    assert r2 is r1, "非空结果应直接返回同一缓存对象"
    assert len(calls) == 1, f"非空缓存命中不应再次打网络，实际 {len(calls)} 次"


def test_industry_nonempty_result_ignores_retry_ttl(src):
    """即使手工推进时间戳越过 TTL，非空结果也不重拉（行业极少变动）。"""
    calls = _install_network(src, "半导体", ["物联网"])
    asyncio.run(src._get_industry(KCB_CODE))
    assert len(calls) == 1

    EltdxSource._industry_ts[KCB_CODE] = time.time() - _INDUSTRY_RETRY_TTL - 3600
    r2 = asyncio.run(src._get_industry(KCB_CODE))

    assert r2["industry"] == "半导体"
    assert len(calls) == 1, "非空结果不受 retry TTL 约束"


def test_industry_empty_result_is_retryable_after_ttl(src):
    """空结果只留内存、可重试：TTL 内命中，超窗必重拉。"""
    calls = _install_network(src, "", [])

    r1 = asyncio.run(src._get_industry(SZ_CODE))
    assert r1["industry"] == "" and r1["concepts"] == []
    assert len(calls) == 1

    # TTL 内：不再打网络
    r2 = asyncio.run(src._get_industry(SZ_CODE))
    assert r2 is r1
    assert len(calls) == 1

    # 越过 TTL：必须重拉（此时网络已恢复，返回真值）
    EltdxSource._industry_ts[SZ_CODE] = time.time() - _INDUSTRY_RETRY_TTL - 1
    _install_network(src, "电池", ["新能源"], calls)
    r3 = asyncio.run(src._get_industry(SZ_CODE))

    assert r3["industry"] == "电池", "越过 TTL 后应重拉并拿到真值"
    assert len(calls) == 2

    # 真值到手后不再重拉
    r4 = asyncio.run(src._get_industry(SZ_CODE))
    assert r4["industry"] == "电池"
    assert len(calls) == 2


def test_industry_empty_result_never_persisted_to_disk(src, monkeypatch):
    """空值绝不下盘：否则重启后照样读到假空（R1 实测污染路径）。"""
    written: list = []

    def _capture(path, data):
        written.append(dict(data))

    # ★ 打在真正发起落盘的模块（行业能力已拆到 eltdx_industry），见 src fixture 的说明。
    monkeypatch.setattr("datasource.eltdx_industry._save_json_cache", _capture)
    _install_network(src, "", [])

    asyncio.run(src._get_industry(SZ_CODE))
    assert written == [], "空结果不应写入磁盘缓存"

    _install_network(src, "电池", ["新能源"])
    EltdxSource._industry_ts[SZ_CODE] = time.time() - _INDUSTRY_RETRY_TTL - 1
    asyncio.run(src._get_industry(SZ_CODE))
    assert len(written) == 1, "拿到真值后应落盘"
    assert written[0][SZ_CODE]["industry"] == "电池"

def test_industry_stale_disk_empty_entry_self_heals(src):
    """磁盘缓存里的陈旧空值：_industry_ts 缺项 → 首次命中即判过期并重拉。

    这是 R1 实测的缺陷路径：修复深市市场判定后，300750/688981 的行业
    仍显示 --，因为旧的空值已被落盘并直接返回。
    """
    EltdxSource._industry_map[KCB_CODE] = {
        "industry": "", "concepts": [], "ts": "2026-10-01T00:00:00",
    }
    assert KCB_CODE not in EltdxSource._industry_ts

    calls = _install_network(src, "半导体", ["大基金持股"])
    r = asyncio.run(src._get_industry(KCB_CODE))

    assert r["industry"] == "半导体"
    assert len(calls) == 1, "陈旧空值首次命中必须重拉"
