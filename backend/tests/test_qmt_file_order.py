"""大 QMT 文件单（DBF）：**列序与方言必须逐字对齐**，且默认不许写进监控目录。

背景（2026-11-08 实测）：
* 下单口是 ``TC::CDbfStrategyRecord``（``corderservicedbfhelper.cpp``），
  15 个大写列名硬编码在 ``OrderService.dll`` 偏移 3211512–3211716；
* CSV 侧只有 ``CCsvHandlerCanOrderVolume`` / ``CCsvHandlerQueryInstrumentFee``，
  是**查询**口，不是下单口；
* 客户端自有 DBF（``userdata/data/xt_client_status.dbf``）实测：版本 ``0x03``、
  字段名 **GBK**、类型 ``C``、长度 20、头长 ``32+32n+1``、记录长 ``1+Σlen``。

盯的不变量：
* 列序 = 实测列序（客户端按列序读，重排等于写错单）；
* DBF 头两个长度字段必须由公式算出（算错客户端读不出行）；
* GBK 字段名能往返；
* **默认拒绝写入监控目录** —— 文件单"出现文件就下单"，这一步必须是显式开关。
"""
import struct
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from connectors.qmt_file_order import (  # noqa: E402
    DBF_COLUMNS,
    DBF_HEADER_BASE,
    DbfField,
    FileOrderError,
    FileOrderRow,
    adopt_schema,
    batch_order_of,
    default_fields,
    find_monitor_settings,
    is_under,
    monitor_paths_near,
    monitor_paths_of,
    parse_batch_order,
    parse_monitor_setting,
    read_dbf,
    read_schema,
    write_dbf,
    write_order_file,
)


def _row(**kw) -> FileOrderRow:
    base = dict(stock_code="600036.SH", volume=100, order_type=23, price_type=5,
                mode_price=10.5, account_id="410001005814")
    base.update(kw)
    return FileOrderRow(**base)


# ----------------------------------------------------------------------
# 列序
# ----------------------------------------------------------------------
def test_column_order_matches_reverse_engineered_schema():
    assert DBF_COLUMNS == (
        "ORDER_TYPE", "PRICE_TYPE", "MODE_PRICE", "STOCK_CODE", "VOLUME",
        "ACCOUNT_ID", "ACT_TYPE", "BROKERTYPE", "STRATEGY", "NOTE",
        "TRADEPARAM", "INSERTTIME", "BASKETPATH", "EXTRAPARAM", "NOTE1",
    ), "列序来自 OrderService.dll 偏移 3211512–3211716，重排即写错单"


def test_default_fields_follow_column_order():
    assert [f.name for f in default_fields()] == list(DBF_COLUMNS)


# ----------------------------------------------------------------------
# DBF 结构
# ----------------------------------------------------------------------
def test_header_lengths_follow_dbase3_formula(tmp_path):
    path = str(tmp_path / "o.dbf")
    fields = [DbfField("A", "C", 10), DbfField("B", "C", 5)]
    write_dbf(path, [{"A": "x", "B": "y"}], fields)

    head = open(path, "rb").read(DBF_HEADER_BASE)
    header_len = struct.unpack_from("<H", head, 8)[0]
    record_len = struct.unpack_from("<H", head, 10)[0]
    assert header_len == DBF_HEADER_BASE + 32 * 2 + 1
    assert record_len == 1 + 10 + 5
    assert struct.unpack_from("<I", head, 4)[0] == 1
    assert head[0] == 0x03, "客户端自有 DBF 版本字节实测为 0x03"


def test_roundtrip_preserves_values(tmp_path):
    path = str(tmp_path / "o.dbf")
    write_dbf(path, [_row().to_record()], default_fields())
    rows = read_dbf(path)
    assert len(rows) == 1
    assert rows[0]["STOCK_CODE"] == "600036.SH"
    assert rows[0]["ACCOUNT_ID"] == "410001005814"
    assert str(rows[0]["VOLUME"]).strip() == "100"


def test_numeric_field_is_right_justified(tmp_path):
    path = str(tmp_path / "o.dbf")
    write_dbf(path, [{"N": 42}], [DbfField("N", "N", 8)])
    blob = open(path, "rb").read()
    record = blob[DBF_HEADER_BASE + 32 + 1:]           # 头 + 1 个描述符 + 0x0D
    assert record[0:1] == b"\x20", "未删除标记必须是 0x20"
    assert record[1:9] == b"      42", "N 型必须右对齐左填空格"


def test_gbk_field_names_roundtrip(tmp_path):
    path = str(tmp_path / "o.dbf")
    write_dbf(path, [{"状态名称": "正常"}], [DbfField("状态名称", "C", 20)])
    fields = read_schema(path)
    assert fields[0].name == "状态名称", "客户端字段名是 GBK，往返必须无损"
    assert read_dbf(path)[0]["状态名称"] == "正常"


def test_atomic_write_leaves_no_tmp(tmp_path):
    path = str(tmp_path / "o.dbf")
    write_dbf(path, [{"A": "x"}], [DbfField("A", "C", 4)])
    assert Path(path).exists()
    assert not Path(path + ".tmp").exists()


def test_rejects_oversized_field(tmp_path):
    with pytest.raises(FileOrderError):
        write_dbf(str(tmp_path / "o.dbf"), [], [DbfField("A", "C", 300)])


def test_read_schema_rejects_non_dbf(tmp_path):
    p = tmp_path / "bad.dbf"
    p.write_bytes(b"NOTADBF" + b"\x00" * 40)
    with pytest.raises(FileOrderError):
        read_schema(str(p))


# ----------------------------------------------------------------------
# 字段名补齐 / 回读健壮性（2026-11-08 实修）
# ----------------------------------------------------------------------
def test_field_name_is_nul_padded_not_space_padded(tmp_path):
    """dBase III 字段名用 0x00 补齐。

    用空格补会让 read_dbf 得到 ``'ORDER_TYPE '``，按列名取值直接 KeyError ——
    表现成"整张表读空"，而不是报错，极难查。
    """
    path = str(tmp_path / "o.dbf")
    write_dbf(path, [{"A": "x"}], [DbfField("A", "C", 4)])
    desc = open(path, "rb").read()[DBF_HEADER_BASE:DBF_HEADER_BASE + 32]
    assert desc[0:1] == b"A"
    assert desc[1:11] == b"\x00" * 10, "字段名必须用 0x00 补齐，不能是空格"


def test_read_dbf_accepts_space_padded_field_names(tmp_path):
    """读侧要容忍空格补齐的第三方 DBF —— 否则回读别人的模板会整表读空。"""
    path = str(tmp_path / "o.dbf")
    write_dbf(path, [{"ABC": "v"}], [DbfField("ABC", "C", 6)])
    raw = bytearray(open(path, "rb").read())
    raw[DBF_HEADER_BASE + 3:DBF_HEADER_BASE + 11] = b" " * 8     # 改成空格补齐
    Path(path).write_bytes(bytes(raw))
    assert read_schema(path)[0].name == "ABC"
    assert read_dbf(path)[0]["ABC"] == "v"


def test_float_not_truncated_in_char_column(tmp_path):
    """C 型列里的浮点不能取整：mode_price=10.5 写成 "10" 就是一张价格错的单。"""
    path = str(tmp_path / "o.dbf")
    write_dbf(path, [{"MODE_PRICE": 10.5, "VOLUME": 100}],
              [DbfField("MODE_PRICE", "C", 16), DbfField("VOLUME", "C", 16)])
    row = read_dbf(path)[0]
    assert row["MODE_PRICE"] == "10.5", f"C 型列被取整了：{row['MODE_PRICE']!r}"
    assert row["VOLUME"] == "100"


def test_read_dbf_rejects_inconsistent_header(tmp_path):
    """字段宽合计与记录长矛盾 ⇒ 头部已坏，必须报错而不是读出半截垃圾。"""
    path = str(tmp_path / "o.dbf")
    write_dbf(path, [{"A": "x"}], [DbfField("A", "C", 4)])
    raw = bytearray(open(path, "rb").read())
    struct.pack_into("<H", raw, 10, 99)                          # 篡改记录长
    Path(path).write_bytes(bytes(raw))
    with pytest.raises(FileOrderError, match="自相矛盾"):
        read_dbf(path)


# ----------------------------------------------------------------------
# 校准
# ----------------------------------------------------------------------
def test_adopt_schema_picks_only_known_columns(tmp_path):
    """模板可能带额外列；照抄会让列序与 CDbfStrategyRecord 不一致。"""
    path = str(tmp_path / "tpl.dbf")
    write_dbf(path, [{"STOCK_CODE": "600036.SH", "EXTRA": "z", "VOLUME": 100}],
              [DbfField("STOCK_CODE", "C", 12), DbfField("EXTRA", "C", 99),
               DbfField("VOLUME", "C", 8)])
    picked = adopt_schema(path)
    names = [f.name for f in picked]
    assert names == ["STOCK_CODE", "VOLUME"], "只采纳已知列，且保持 DBF_COLUMNS 顺序"
    assert "EXTRA" not in names
    assert picked[0].length == 12, "列宽必须来自模板，不能沿用默认值"


def test_adopt_schema_errors_when_nothing_known(tmp_path):
    path = str(tmp_path / "tpl.dbf")
    write_dbf(path, [{"FOO": "x"}], [DbfField("FOO", "C", 4)])
    with pytest.raises(FileOrderError, match="没有任何已知下单列"):
        adopt_schema(path)


# ----------------------------------------------------------------------
# 校验
# ----------------------------------------------------------------------
def test_validate_accepts_normal_row():
    assert _row().validate() == []


@pytest.mark.parametrize("kw,needle", [
    ({"stock_code": "600036"}, "stock_code"),
    ({"stock_code": "600036."}, "不能为空"),
    ({"stock_code": ""}, "stock_code"),
    ({"volume": 0}, "volume"),
    ({"volume": -100}, "volume"),
    ({"volume": 1.5}, "volume"),
    ({"order_type": -1}, "order_type"),
    ({"price_type": -2}, "price_type"),
    ({"mode_price": -0.01}, "mode_price"),
])
def test_validate_rejects_bad_values(kw, needle):
    problems = _row(**kw).validate()
    assert problems, f"{kw} 应当被判非法"
    assert any(needle in p for p in problems), problems


# ----------------------------------------------------------------------
# 安全：默认不许写进监控目录
# ----------------------------------------------------------------------
def test_is_under_handles_prefix_lookalike(tmp_path):
    root = tmp_path / "export_data"
    root.mkdir()
    (tmp_path / "export_data_backup").mkdir()
    assert is_under(str(root / "a.dbf"), str(root))
    assert not is_under(str(tmp_path / "export_data_backup" / "a.dbf"), str(root)), \
        "export_data_backup 不是 export_data 的子目录，不能被前缀误判"


def test_write_order_file_refuses_monitor_dir_by_default(tmp_path):
    mon = tmp_path / "export_data"
    mon.mkdir()
    target = str(mon / "order.dbf")
    with pytest.raises(FileOrderError, match="监控目录"):
        write_order_file(target, [_row()], monitor_paths=[str(mon)])
    assert not Path(target).exists(), "拒绝时不能留下半成品文件"


def test_write_order_file_allows_when_explicit(tmp_path):
    mon = tmp_path / "export_data"
    mon.mkdir()
    target = str(mon / "order.dbf")
    info = write_order_file(target, [_row()], monitor_paths=[str(mon)],
                            allow_monitor_dir=True)
    assert info["rows"] == 1 and info["monitor_dir"] is True
    assert read_dbf(target)[0]["STOCK_CODE"] == "600036.SH"


def test_write_order_file_outside_monitor_dir_is_fine(tmp_path):
    safe = tmp_path / "staging"
    safe.mkdir()
    info = write_order_file(str(safe / "order.dbf"), [_row()],
                            monitor_paths=[str(tmp_path / "export_data")])
    assert info["monitor_dir"] is False
    assert info["monitor_checked"] is True, "给了监控目录 ⇒ 确实检查过"
    assert info["columns"] == list(DBF_COLUMNS)


def test_write_order_file_reports_unchecked_when_paths_missing(tmp_path):
    """没传监控目录 ≠ 检查过且安全。

    两态混同会把"漏传参数"读成"已核对"，正是本仓库最忌的假绿。
    """
    info = write_order_file(str(tmp_path / "o.dbf"), [_row()])
    assert info["monitor_dir"] is False
    assert info["monitor_checked"] is False, "没给监控目录必须报未检查，不能报安全"


def test_write_order_file_rejects_empty_and_invalid(tmp_path):
    with pytest.raises(FileOrderError, match="没有可写"):
        write_order_file(str(tmp_path / "a.dbf"), [])
    with pytest.raises(FileOrderError, match="校验失败"):
        write_order_file(str(tmp_path / "b.dbf"), [_row(volume=0)])


def test_insert_time_defaults_to_now(tmp_path):
    target = str(tmp_path / "o.dbf")
    write_order_file(target, [_row()])
    got = read_dbf(target)[0]["INSERTTIME"]
    assert len(got) >= 19, f"INSERTTIME 应自动填当前时间，实际 {got!r}"


# ----------------------------------------------------------------------
# 监控目录：从 Config.xml 读（实测标签见 docs 与模块 docstring）
# ----------------------------------------------------------------------
REAL_TAG = (
    '<?xml version="1.0" encoding="utf-8"?>\n'
    '<TTUserConfigFile>\n'
    '    <DbfMonitorSetting is_incexport="0" useringbuffer="0"'
    ' need_remove_exprotfile_fristmonitor="1" is_usesettingfeedbackval="0"'
    ' orderidfeedbackval="-1" is_automonitor="0" is_clear="0"'
    ' monitor_path="P:\\stock\\gd_qmt\\export_data" clear_date=""'
    ' is_sharedread="0" backup_path="P:\\stock\\gd_qmt\\export_data_backup"/>\n'
    '    <CsvMonitorSetting is_automonitor="0" monitor_path="P:\\stock\\gd_qmt\\export_data"/>\n'
    '</TTUserConfigFile>\n'
)


def _write_config(tmp_path, account: str, *, automonitor: str = "0",
                  monitor: str, backup: str) -> str:
    d = tmp_path / "userdata" / "users" / account
    d.mkdir(parents=True, exist_ok=True)
    xml = REAL_TAG.replace('is_automonitor="0" is_clear', f'is_automonitor="{automonitor}" is_clear')
    xml = xml.replace('P:\\stock\\gd_qmt\\export_data_backup', backup)
    xml = xml.replace('monitor_path="P:\\stock\\gd_qmt\\export_data"', f'monitor_path="{monitor}"')
    p = d / "Config.xml"
    p.write_bytes(xml.encode("utf-8"))
    return str(p)


def test_parse_monitor_setting_reads_real_tag():
    s = parse_monitor_setting(REAL_TAG.encode("utf-8"))
    assert s is not None
    assert s.monitor_path == r"P:\stock\gd_qmt\export_data"
    assert s.backup_path == r"P:\stock\gd_qmt\export_data_backup"
    assert s.is_automonitor is False, "实测四个安装全部 is_automonitor=0"
    assert s.remove_before_first_monitor is True
    assert s.orderid_feedback == "-1"
    assert s.armed is False, "自动监控关着 ⇒ 文件单不生效，armed 必须为 False"


def test_armed_requires_automonitor_and_path():
    assert parse_monitor_setting(
        REAL_TAG.replace('is_automonitor="0"', 'is_automonitor="1"').encode("utf-8")
    ).armed is True
    assert parse_monitor_setting(
        REAL_TAG.replace('is_automonitor="0" is_clear', 'is_automonitor="1" is_clear')
                .replace('monitor_path="P:\\stock\\gd_qmt\\export_data"', 'monitor_path=""')
                .encode("utf-8")
    ).armed is False, "开了监控但没目录，仍然不算 armed"


def test_parse_monitor_setting_missing_tag_and_bad_xml():
    assert parse_monitor_setting(b"<Root><Other/></Root>") is None
    assert parse_monitor_setting(b"not xml at all") is None


def test_parse_monitor_setting_survives_malformed_config():
    """实测客户端 Config.xml **不是合法 XML**（ET 报 invalid token）。

    只走 ElementTree 会在真机上恒返回 None —— 静默假阴性，监控目录永远读不到。
    """
    dirty = (b'<?xml version="1.0" encoding="utf-8"?>\n<TTUserConfigFile>\n'
             b'    <gold opSetting="0+0+1&2"/>\n'      # 裸 & 就是非法 token
             b'    <DbfMonitorSetting is_automonitor="1" is_incexport="0"'
             b' monitor_path="P:\\stock\\gd_qmt\\export_data"'
             b' backup_path="P:\\stock\\gd_qmt\\export_data_backup"/>\n'
             b'</TTUserConfigFile>\n')
    with pytest.raises(Exception):
        import xml.etree.ElementTree as ET
        ET.fromstring(dirty)
    s = parse_monitor_setting(dirty)
    assert s is not None and s.monitor_path == r"P:\stock\gd_qmt\export_data"
    assert s.armed is True


def test_find_monitor_settings_scans_all_accounts(tmp_path):
    """一个安装可以有多个登录账号，各有自己的监控目录 —— 漏一个就是漏一个下单口。"""
    _write_config(tmp_path, "acctA", monitor=r"P:\a\export_data",
                  backup=r"P:\a\export_data_backup")
    _write_config(tmp_path, "acctB", automonitor="1", monitor=r"P:\b\export_data",
                  backup=r"P:\b\export_data_backup")
    (tmp_path / "userdata" / "users" / "acctC").mkdir()
    (tmp_path / "userdata" / "users" / "acctC" / "Config.xml").write_bytes(b"broken")

    got = find_monitor_settings(str(tmp_path / "userdata"))
    assert [name for name, _ in got] == ["acctA", "acctB"], "坏配置要跳过，不能拖垮整轮"
    assert [s.armed for _, s in got] == [False, True]

    paths = monitor_paths_of(str(tmp_path / "userdata"))
    assert r"P:\a\export_data" in paths and r"P:\b\export_data_backup" in paths


def test_monitor_paths_of_missing_userdata_is_empty(tmp_path):
    assert find_monitor_settings(str(tmp_path / "nope")) == []
    assert monitor_paths_of(str(tmp_path / "nope")) == []


def test_write_order_file_refuses_real_backup_lookalike(tmp_path):
    """实测 monitor=export_data、backup=export_data_backup 是**两个**目录。

    只登记 monitor_path 会让写进 backup 的路径被判成"安全"；反过来用前缀匹配
    也会把 export_data_backup 误判成 export_data 的子目录。两个方向都要防。
    """
    mon = tmp_path / "export_data"
    bak = tmp_path / "export_data_backup"
    mon.mkdir()
    bak.mkdir()
    with pytest.raises(FileOrderError, match="监控目录"):
        write_order_file(str(bak / "o.dbf"), [_row()],
                         monitor_paths=[str(mon), str(bak)])
    # 前缀相似但确实不在监控目录内 ⇒ 放行
    info = write_order_file(str(tmp_path / "staging.dbf"), [_row()],
                            monitor_paths=[str(mon), str(bak)])
    assert info["monitor_dir"] is False


# ----------------------------------------------------------------------
# 写入口护栏：列序 / 列宽 / 与 roots 无关的监控目录发现
# ----------------------------------------------------------------------
def test_write_order_file_refuses_partial_field_set(tmp_path):
    """★ 库函数也是一条入口：传部分列会静默写出一张**列序错位**的 DBF。

    /stage 已经在校准模板时挡过一次，但护栏必须落在最终写入口，
    不能指望每个调用方都记得检查（adopt_schema 本来就允许返回部分列）。
    """
    partial = default_fields()[:3]
    with pytest.raises(FileOrderError, match="列序"):
        write_order_file(str(tmp_path / "o.dbf"), [_row()], fields=partial)


def test_write_dbf_refuses_silent_truncation(tmp_path):
    """列宽不够时拒绝，不能写一张"格式合法但时间戳/价格被切短"的单。"""
    path = str(tmp_path / "o.dbf")
    with pytest.raises(FileOrderError, match="截断"):
        write_dbf(path, [{"INSERTTIME": "2026-11-09 09:38:01.000"}],
                  [DbfField("INSERTTIME", "C", 16)])
    assert not Path(path).exists(), "拒绝时不能留下半成品"


BATCH_TAG = ('<batchOrderSetting key_sequence="" '
             'monitor_path="P:\\stock\\gd_qmt\\batchOrder\\orderFile.csv" '
             'key_enable="0"/>')


def test_parse_batch_order_reports_disabled_channel():
    """实测四套安装都有批量单配置、全部 key_enable=0。"""
    got = parse_batch_order(BATCH_TAG.encode("utf-8"))
    assert got is not None
    assert got["key_enable"] is False
    assert got["monitor_path"].endswith("orderFile.csv")
    assert parse_batch_order(b"<nothing/>") is None


def test_monitor_paths_near_finds_config_without_roots(tmp_path):
    """★ 不扫安装、只从目标路径向上看，也必须发现监控目录。

    /stage 的反向自检原先只在 roots 扫到的安装里找；roots 为空时等于空集检查。
    """
    root = tmp_path / "gd_qmt"
    mon = str(root / "export_data")
    _write_config(root, "410001005814", monitor=mon,
                  backup=str(root / "export_data_backup"))
    probe = str(root / "export_data" / "file_order_staging" / "x.dbf")
    paths = monitor_paths_near(probe)
    assert mon in paths, f"没从 staging 路径自身找到监控目录: {paths}"
    assert monitor_paths_near(str(tmp_path / "far" / "away" / "x.dbf")) == []


def test_batch_order_of_lists_each_account(tmp_path):
    root = tmp_path / "gd_qmt"
    cfg = Path(_write_config(root, "410001005814", monitor=r"P:\m", backup=r"P:\b"))
    cfg.write_bytes(cfg.read_bytes() + BATCH_TAG.encode("utf-8"))
    rows = batch_order_of(str(root / "userdata"))
    assert len(rows) == 1 and rows[0][0] == "410001005814"
    assert rows[0][1]["key_enable"] is False
