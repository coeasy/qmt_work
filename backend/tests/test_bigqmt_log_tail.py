"""闭环回传通道（``@@QMTW|`` 日志行）的解析纪律。

盯的是**采集器不能骗人**：

* 非前缀行必须计入 ``ignored`` 而不是当坏行（日志里 99% 是终端自己的噪音）；
* 坏 JSON 只计数、不抛异常（一条畸形日志不该让整条通道停摆）；
* 去重键必须是 ``(boot_id, seq)`` —— 只用 ``seq`` 会把重启后的新会话全判成重复；
* 文件轮转/清空/半行都必须有明确处置（否则静默读空，指标全绿而通道已死）。
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from connectors.bigqmt_log_tail import (  # noqa: E402
    MAX_LINE, PREFIX, AgentRecord, LogTail,
)


def _line(kind, seq=1, boot="b1", **data):
    return PREFIX + json.dumps({"k": kind, "s": seq, "b": boot, "t": 1700000000.0,
                                "d": data or {"x": 1}}, ensure_ascii=False)


def test_prefix_line_parsed():
    tail = LogTail()
    rec = tail.feed_line(_line("ORDER", seq=7, code="600519.SH"))
    assert isinstance(rec, AgentRecord)
    assert rec.kind == "ORDER"
    assert rec.seq == 7
    assert rec.boot_id == "b1"
    assert rec.data["code"] == "600519.SH"
    assert tail.stats.parsed == 1


def test_noise_is_ignored_not_bad():
    tail = LogTail()
    assert tail.feed_line("2026-10-31 09:30:00 普通终端日志") is None
    assert tail.stats.ignored == 1
    assert tail.stats.bad == 0


def test_bad_json_counts_but_does_not_raise():
    tail = LogTail()
    assert tail.feed_line(PREFIX + "{not json") is None
    assert tail.feed_line(PREFIX + "[1,2,3]") is None  # 合法 JSON 但不是对象
    assert tail.feed_line(PREFIX + json.dumps({"s": 1})) is None  # 缺 k
    assert tail.stats.bad == 3
    assert tail.stats.parsed == 0


def test_oversized_line_dropped():
    tail = LogTail()
    big = PREFIX + json.dumps({"k": "ORDER", "s": 1, "b": "b1",
                               "d": {"pad": "x" * MAX_LINE}})
    assert tail.feed_line(big) is None
    assert tail.stats.oversized == 1


def test_dedupe_by_boot_and_seq():
    tail = LogTail()
    assert tail.feed_line(_line("ORDER", seq=3)) is not None
    assert tail.feed_line(_line("ORDER", seq=3)) is None
    assert tail.stats.duplicates == 1


def test_boot_switch_clears_dedupe():
    """重启后 seq 从 1 重来 —— 若只按 seq 去重，新会话第一条会被吞掉。"""
    tail = LogTail()
    assert tail.feed_line(_line("ORDER", seq=1, boot="b1")) is not None
    rec = tail.feed_line(_line("ORDER", seq=1, boot="b2"))
    assert rec is not None and rec.boot_id == "b2"
    assert tail.stats.boot_switches == 1
    assert tail.boot_id == "b2"


def test_seq_zero_not_deduped():
    """``seq=0`` 表示「未编号」，不能拿去重键互相覆盖。"""
    tail = LogTail()
    assert tail.feed_line(_line("STATUS", seq=0)) is not None
    assert tail.feed_line(_line("STATUS", seq=0)) is not None
    assert tail.stats.duplicates == 0


def test_canonical_translation():
    tail = LogTail()
    order = tail.feed_line(_line("ORDER", seq=1, order_id="o1", order_status="50"))
    assert order.to_canonical().kind == "order"
    trade = tail.feed_line(_line("TRADE", seq=2, traded_id="t1"))
    assert trade.to_canonical().kind == "trade"
    result = tail.feed_line(_line("RESULT", seq=3, ok=True))
    assert result.to_canonical() is None


def test_read_follows_cursor_and_keeps_partial_line(tmp_path):
    log = tmp_path / "XtClient.log"
    first_line = _line("ORDER", seq=1) + "\n"
    second_line = _line("ORDER", seq=2) + "\n"
    # 用 write_bytes 而非 write_text：后者在 Windows 上把 \n 翻成 \r\n，
    # 会让「游标字节数」这条断言测的是换行翻译而不是本模块的行为。
    log.write_bytes(("噪音行\n" + first_line + second_line[:20]).encode("utf-8"))

    tail = LogTail(str(log))
    first = tail.read()
    assert len(first) == 1
    assert first[0].seq == 1
    # 半行没被消费：游标停在半行之前
    assert tail.offset == len(("噪音行\n" + first_line).encode("utf-8"))

    with open(log, "ab") as fh:
        fh.write(second_line[20:].encode("utf-8"))
    second = tail.read()
    assert [r.seq for r in second] == [2]


def test_read_handles_rotation(tmp_path):
    log = tmp_path / "XtClient.log"
    log.write_bytes((_line("ORDER", seq=1, pad="x" * 40) + "\n").encode("utf-8"))
    tail = LogTail(str(log))
    assert len(tail.read()) == 1

    # 文件被清空/轮转后变小 ⇒ 必须重置游标，否则永久读空
    log.write_bytes((_line("ORDER", seq=2) + "\n").encode("utf-8"))
    recs = tail.read()
    assert [r.seq for r in recs] == [2]
    assert tail.stats.rotations == 1


def test_read_missing_file_returns_empty(tmp_path):
    tail = LogTail(str(tmp_path / "nope.log"))
    assert tail.read() == []
    assert tail.stats.parsed == 0


def test_read_requires_path():
    with pytest.raises(ValueError):
        LogTail().read()


def test_stats_are_plain_counts():
    tail = LogTail()
    tail.feed("噪音\n" + _line("ORDER", seq=1) + "\n")
    stats = tail.stats.as_dict()
    assert stats["parsed"] == 1
    assert stats["ignored"] == 1
    assert set(stats) == {"parsed", "ignored", "bad", "duplicates", "oversized",
                          "boot_switches", "rotations", "bytes_read"}
