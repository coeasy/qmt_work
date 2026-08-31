"""拼音首字母工具（GBK 区间法，纯 stdlib 零依赖）。

用途：标的检索支持 TDX 习惯的拼音首字母输入（gzmt → 贵州茅台）。
原理：GBK 双字节编码按「拼音序」近似单调排列，对常用汉字（B0A1-F7FE 区段）
可用一组首字母边界码线性判别。多音字/生僻字存在少量误差，属可接受的
近似匹配（搜索场景宁多召回不错杀，最终由用户在候选中确认）。

边界值经真实锚点字实测校准（啊B0A1/芭B0C5/…/哈B9FE/机BBFA/科BFC6/…/匝D4D1），
并用证券常用字（贵B9F3 g / 国B9FA g / 招D5D0 z / 银D2F8 y / 行D0D0 x）回归验证全档自洽。
"""
from __future__ import annotations

# (起始 GBK 码, 首字母) 边界表：汉字落在 [code_i, code_{i+1}) 区间即属首字母 i
_BOUNDS = (
    (0xB0A1, "a"), (0xB0C5, "b"), (0xB2C1, "c"), (0xB4EE, "d"),
    (0xB6EA, "e"), (0xB7A2, "f"), (0xB8C1, "g"), (0xB9FE, "h"),
    (0xBBF7, "j"), (0xBFA6, "k"), (0xC0AC, "l"), (0xC2E8, "m"),
    (0xC4C3, "n"), (0xC5B6, "o"), (0xC5BE, "p"), (0xC6DF, "q"),
    (0xC8BB, "r"), (0xC8F6, "s"), (0xCBFA, "t"), (0xCDDA, "w"),
    (0xCEF4, "x"), (0xD1B9, "y"), (0xD4D1, "z"),
)
_LETTERS = [b[1] for b in _BOUNDS]
_CODES = [b[0] for b in _BOUNDS]


def _char_initial(ch: str) -> str:
    """单字符拼音首字母：汉字返回小写字母，非汉字返回空串。"""
    try:
        b = ch.encode("gbk")
    except (UnicodeEncodeError, UnicodeDecodeError, LookupError):
        return ""
    if len(b) != 2:
        # ASCII / 其它单字节：字母原样小写，其余忽略
        return ch.lower() if ch.isascii() and ch.isalpha() else ""
    code = (b[0] << 8) | b[1]
    # 常用汉字区段之外（GBK 扩展区/符号区）不判别，避免乱猜
    if code < _CODES[0] or code > 0xF7FE:
        return ""
    # 线性扫边界（23 档，无需二分）
    for i in range(len(_CODES) - 1):
        if _CODES[i] <= code < _CODES[i + 1]:
            return _LETTERS[i]
    return _LETTERS[-1]


def pinyin_initials(name: str) -> str:
    """中文名称的拼音首字母序列（主音）：贵州茅台 → gzmt。"""
    if not name:
        return ""
    return "".join(_char_initial(ch) for ch in name)


# 金融语境高频多音字：主音之外的候选首字母（「行」xíng/háng → x/h 双候选，
# 使 payh / payx 均能命中「平安银行」）。宁多召回不错杀。
_MULTI = {
    "行": "xh", "长": "cz", "重": "cz", "数": "s", "乐": "yl",
    "都": "d", "空": "k", "种": "z", "着": "z", "差": "c", "单": "d",
    "降": "jx", "处": "c", "参": "cs", "率": "l", "藏": "cz", "系": "xj",
}


def _char_candidates(ch: str) -> str:
    """单字符的候选首字母集合（主音 + 多音），非汉字返回空串。"""
    main = _char_initial(ch)
    extra = _MULTI.get(ch, "")
    return (main + extra) if main else ""


def matches_initials(name: str, q: str) -> bool:
    """名称拼音首字母是否匹配 q：q 为纯字母（小写比较），逐位置支持多音字候选。"""
    if not q or not q.isalpha():
        return False
    ql = q.lower()
    cands = [ _char_candidates(ch) for ch in (name or "") ]
    cands = [c for c in cands if c]
    if len(cands) < len(ql):
        return False
    # 逐位比对：q 的每个字母须落在对应字的候选集合内
    for i, letter in enumerate(ql):
        if letter not in cands[i]:
            return False
    return True


def starts_with_initials(name: str, q: str) -> bool:
    """名称拼音首字母是否以 q 开头（q 须为纯字母，小写化后比较）。"""
    if not q or not q.isalpha():
        return False
    ini = pinyin_initials(name)
    return bool(ini) and ini.startswith(q.lower())
