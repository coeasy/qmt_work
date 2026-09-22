"""腾讯行情快照的**扩展字段解析** —— 「基本信息」面板要显示市值 / PE / 换手等。

## 为什么单独一份测试

这些字段（市值 / 市盈率 / 市净率 / 换手率 / 振幅 / 量比 / 均价 / 涨跌停）全部来自
**同一条** ``qt.gtimg.cn`` 快照响应里以前被丢弃的那 40 多个字段。把它们解析出来是
「零额外请求」的收益，代价是**必须按实测下标取**，取错一位就会显示一个看起来
完全合理的错数字（比显示 ``--`` 危险得多）。所以这里用**真实抓下来的样本**逐字段锁死。

样本来源：2026-09-21 `curl https://qt.gtimg.cn/q=sh600519` / `sh000001` / `sz300750`。

⚠️ 没装 pytest-asyncio ⇒ 异步一律 sync + ``asyncio.run(...)``。
"""
import asyncio

from datasource.public_sources import TencentSource, _tx_snapshot

# ---- 真实样本（GBK 已转 UTF-8，字段与线上一致）----
SAMPLE_600519 = (
    'v_sh600519="1~贵州茅台~600519~1257.12~1266.98~1262.99~24891~12061~12829~1257.12~8~'
    '1257.11~2~1257.08~1~1257.06~2~1257.05~2~1257.13~1~1257.24~2~1257.28~1~1258.00~16~'
    '1258.28~1~~20260918161436~-9.86~-0.78~1265.88~1256.10~1257.12/24891/3135849108~24891~'
    '313585~0.20~19.30~~1265.88~1256.10~0.77~15715.03~15715.03~6.25~1393.68~1140.28~1.14~'
    '-6~1259.84~17.65~19.09~~~0.08~313584.9108~527.9904~42~   A~GP-A~-6.82~-1.41~4.14~'
    '32.41~27.30~1539.98~1151.01~-5.48~-1.23~7.57~1250081601~1250081601~-16.67~-8.89~'
    '1250081601~~~-11.22~-0.42~~CNY~0~___D__F__N~1257.00~102~";'
)

# 指数：**涨跌停价是 -1 哨兵**（不是 0，也不是空串），市净率是 0.00
SAMPLE_INDEX = (
    'v_sh000001="1~上证指数~000001~3911.87~3875.60~3891.96~485712507~0~0~0.00~0~0.00~0~0.00~0~'
    '0.00~0~0.00~0~0.00~0~0.00~0~0.00~0~0.00~0~0.00~0~~20260918161402~36.27~0.94~3919.67~'
    '3888.50~3911.87/485712507/994169450166~485712507~99416945~1.00~17.06~~3919.67~3888.50~'
    '0.80~612727.33~697380.61~0.00~-1~-1~1.02~0~3905.27~~~~~~99416945.0166~0.0000~0~ ~ZS~'
    '-1.44~0.61~~~~4258.86~3741.11~-0.46~0.17~-2.87~4850070676346~~-1.17~-0.21~4850070676346'
    '~~~2.09~-0.04~~CNY~0~~0.00~0~";'
)

# 创业板 ±20%：涨停 365.16 = 昨收 304.30 × 1.2
# ⚠️ 整条必须**原样照抄线上响应**：早先我按「大概长这样」手搓过一份，中间五档那段的
#    占位数量和线上不一致 ⇒ 下标整体错位，把**正确实现**判成失败（探针/样本本身
#    必须先被证伪，见 §「报 FAIL 前先怀疑探针」）。
SAMPLE_300750 = (
    'v_sz300750="51~宁德时代~300750~301.95~304.30~309.77~380713~189212~191501~301.95~1~'
    '301.94~12~301.93~6~301.92~21~301.91~47~301.96~5~301.97~9~301.98~15~301.99~29~302.00~200~~'
    '20260918161412~-2.35~-0.77~310.00~300.27~301.95/380713/11537664648~380713~1153766~0.89~'
    '16.44~~310.00~300.27~3.20~12864.89~13971.93~3.75~365.16~243.44~0.88~-171~303.05~16.14~'
    '19.35~~~1.07~1153766.4648~431.7885~143~ A A~GP-A-CYB~-15.88~-8.64~2.72~22.41~8.03~'
    '467.35~299.00~-13.97~-22.80~-20.46~4260601929~4627234532~-49.57~-18.35~4260601929~~~'
    '-15.79~0.17~~CNY~0~~301.90~73~";'
)


def _fields(raw: str):
    i = raw.find('"')
    j = raw.rfind('"')
    return raw[i + 1:j].split("~")


class _StubSource(TencentSource):
    """只替换传输层：返回给定文本并记下 URL（应用逻辑 / 解析全部是真的）。"""

    def __init__(self, raw: str):
        self.raw = raw
        self.urls = []

    async def _get(self, url: str) -> str:
        self.urls.append(url)
        return self.raw


def test_snapshot_parses_extended_fields():
    d = _tx_snapshot("600519.SH", _fields(SAMPLE_600519))
    assert d["name"] == "贵州茅台"
    assert d["last"] == 1257.12
    assert d["pre_close"] == 1266.98
    assert d["open"] == 1262.99
    assert d["high"] == 1265.88
    assert d["low"] == 1256.10
    # 单位换算：手 → 股、万元 → 元、亿元 → 元
    assert d["volume"] == 24891 * 100
    assert d["amount"] == 313585 * 1e4
    assert d["circ_mv"] == 15715.03 * 1e8
    assert d["total_mv"] == 15715.03 * 1e8
    assert d["turnover_rate"] == 0.20
    assert d["pe_ttm"] == 19.30
    assert d["amplitude"] == 0.77
    assert d["pb"] == 6.25
    assert d["volume_ratio"] == 1.14
    assert d["avg_price"] == 1259.84


def test_limit_prices_match_board_ratio():
    """涨跌停必须与板块涨跌幅自洽 —— 这是「下标没取错位」最硬的证据。"""
    gufeng = _tx_snapshot("600519.SH", _fields(SAMPLE_600519))
    # 主板 ±10%
    assert gufeng["high_limit"] == round(gufeng["pre_close"] * 1.1, 2)
    assert gufeng["low_limit"] == round(gufeng["pre_close"] * 0.9, 2)

    chuangye = _tx_snapshot("300750.SZ", _fields(SAMPLE_300750))
    # 创业板 ±20%
    assert chuangye["high_limit"] == round(chuangye["pre_close"] * 1.2, 2)
    assert chuangye["low_limit"] == round(chuangye["pre_close"] * 0.8, 2)


def test_index_sentinel_limit_is_none_not_minus_one():
    """指数没有涨跌停 ⇒ 腾讯返回 **-1**（不是 0/空）。

    ⚠️ 直接透传会让界面出现「上证指数 涨停价 -1.00」这种荒谬值；
       市净率同理（指数返回 0.00，显示 0 会被读成「净资产为零」）。
    """
    d = _tx_snapshot("000001.SH", _fields(SAMPLE_INDEX))
    assert d["high_limit"] is None
    assert d["low_limit"] is None
    assert d["pb"] is None


def test_negative_pe_is_kept():
    """亏损股的市盈率 TTM 是负数 —— 不许被「<=0 就当没有」的规则抹掉。"""
    fields = _fields(SAMPLE_600519)
    fields[39] = "-12.34"  # pe_ttm
    d = _tx_snapshot("600519.SH", fields)
    assert d["pe_ttm"] == -12.34


def test_missing_or_junk_fields_are_none_not_zero():
    fields = _fields(SAMPLE_600519)
    for idx in (33, 34, 37, 38, 39, 43, 44, 45, 46, 49, 51):
        fields[idx] = ""
    d = _tx_snapshot("600519.SH", fields)
    for key in ("high", "low", "amount", "turnover_rate", "pe_ttm", "amplitude",
                "circ_mv", "total_mv", "pb", "volume_ratio", "avg_price"):
        assert d[key] is None, f"{key} 空串应解析为 None，实际 {d[key]!r}"

    junk = _fields(SAMPLE_600519)
    junk[44] = "N/A"
    assert _tx_snapshot("600519.SH", junk)["circ_mv"] is None


def test_short_snapshot_does_not_crash():
    """少数字段的老形态 / 截断响应：缺位一律 None，不抛 IndexError。"""
    d = _tx_snapshot("600519.SH", ["1", "贵州茅台", "600519", "10.0", "9.9", "9.95", "100"])
    assert d["name"] == "贵州茅台"
    assert d["last"] == 10.0
    assert d["circ_mv"] is None
    assert d["high_limit"] is None


def test_single_and_batch_parse_identically():
    """单只与批量**共用一份解析** —— 历史上批量就是漏改的那条路径。"""
    src = _StubSource(SAMPLE_600519)
    one = asyncio.run(src.get_quote("600519.SH"))

    two = _StubSource(SAMPLE_600519 + "\n" + SAMPLE_300750)
    many = asyncio.run(two.get_quotes(["600519.SH", "300750.SZ"]))
    assert set(many) == {"600519.SH", "300750.SZ"}
    # 批量里 600519 的字段必须与单只**逐键相同**
    assert many["600519.SH"] == one
    assert len(two.urls) == 1, "批量必须是 1 次 HTTP"


def test_instrument_detail_exposes_extended_fields():
    """画像层必须把扩展字段原样透出 —— 基本信息面板因此不用再发第二次请求。"""
    src = _StubSource(SAMPLE_600519)
    det = asyncio.run(src.get_instrument_detail("600519.SH"))
    for key in ("circ_mv", "total_mv", "pe_ttm", "pb", "turnover_rate",
                "amplitude", "volume_ratio", "avg_price", "high_limit", "low_limit"):
        assert key in det, f"get_instrument_detail 少了扩展字段 {key}"
    assert det["circ_mv"] == 15715.03 * 1e8

    # 派生钩子（批量路径）与详情层键集一致，否则批量富化会静默丢字段
    derived = src.derive_detail(_tx_snapshot("600519.SH", _fields(SAMPLE_600519)))
    assert set(derived) >= set(TencentSource._DETAIL_KEYS)
