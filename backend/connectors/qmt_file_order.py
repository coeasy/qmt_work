"""大 QMT 文件单（DBF）——**券商官方支持**的外部下单正门。

为什么是 DBF 而不是 CSV
-----------------------
实测（2026-11-08，`OrderService.dll`）：

* CSV 监控只有两个处理器 —— ``CCsvHandlerCanOrderVolume``（查可用数量）与
  ``CCsvHandlerQueryInstrumentFee``（查费用）。**它们是查询口，不是下单口。**
* 下单口的记录类是 ``TC::CDbfStrategyRecord``（源码路径
  ``e:\\v2.0\\src\\business\\orderservice\\corderservicedbfhelper.cpp``），
  列名以**大写 ASCII** 硬编码在 .rdata 里。
* 另有 ``addBatchOrderStrategy`` / ``getDefaultBatchOrderDir`` 的批量下单，
  文件名形如 ``{0}/{1}.{2}.csv`` —— 那是另一条路，字段集未解。

DBF 列（**顺序即实测顺序**，来自 OrderService.dll 偏移 3211512–3211716）：

    ORDER_TYPE  PRICE_TYPE  MODE_PRICE  STOCK_CODE  VOLUME  ACCOUNT_ID
    ACT_TYPE    BROKERTYPE  STRATEGY    NOTE        TRADEPARAM
    INSERTTIME  BASKETPATH  EXTRAPARAM  NOTE1

★ 未确定项（**不臆造**）：各列的 DBF 类型与长度。客户端自己的 DBF 写法已实测
  （`userdata/data/xt_client_status.dbf`）：版本 ``0x03``、字段名 **GBK**、
  类型 ``C``、长度 20、头长 ``32+32n+1``、记录长 ``1+Σlen``。
  但下单表的列宽可能不同，因此本模块：

  1. 默认按"客户端同款方言"写（C 型 + 保守列宽），可跑；
  2. 提供 ``read_schema()`` / ``adopt_schema()`` —— 用**客户端自己导出的模板 DBF**
     校准真实类型与长度（`DbfMonitorSetting` 的 ``is_incexport`` 置 1 会导出）。

安全边界（重要）
----------------
文件单是"目录里出现文件就下单"。因此 ``write_order_file`` **默认拒绝**写入
任何被登记为监控目录的路径 —— 必须显式 ``allow_monitor_dir=True`` 才放行。
这样"生成一份待核对的文件"与"把文件丢进监控目录"在代码层面是两件事。

监控目录**不要猜**，从客户端自己的配置读（实测四个安装）：

    <DbfMonitorSetting monitor_path="{root}\\export_data"
                       backup_path="{root}\\export_data_backup"
                       is_automonitor="0" is_incexport="0" .../>

用 ``monitor_paths_of(userdata)`` 直接拿到全部账号的监控目录喂给
``write_order_file(monitor_paths=...)``。

★ 实测四个安装**全部** ``is_automonitor="0"`` ⇒ 文件单总开关是关的。
  往 ``export_data`` 丢 DBF 不会产生任何委托，文件只会躺在目录里。
  想真下单必须先让用户在界面里打开"自动监控"，本模块不做这件事。
"""
from __future__ import annotations

import datetime
import os
import struct
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from core.clock import local_now

#: 下单 DBF 的列，**顺序即实测顺序**，不要重排（客户端按列序读）。
DBF_COLUMNS: tuple[str, ...] = (
    "ORDER_TYPE",
    "PRICE_TYPE",
    "MODE_PRICE",
    "STOCK_CODE",
    "VOLUME",
    "ACCOUNT_ID",
    "ACT_TYPE",
    "BROKERTYPE",
    "STRATEGY",
    "NOTE",
    "TRADEPARAM",
    "INSERTTIME",
    "BASKETPATH",
    "EXTRAPARAM",
    "NOTE1",
)

#: ★ 2026-11-11 二进制实证的同一份列名（**逐字逐序**，用于回归锁定）。
#:
#: `OrderService.dll` 的 `0x3100F8`–`0x3101C4` 是一段**连续**的 15 个列名字符串，
#: 紧随其后就是 `TC::CDbfStrategyRecord::setColumnParam` 与
#: `[dbfStrategy [{0}] init [{1}] column Failure!`。也就是说：
#: 「列名 + 列序」不再只是"实测顺序"的说法，而是**有二进制出处**。
#:
#: 之所以单列一份常量而不是直接复用 ``DBF_COLUMNS``：两者相等必须由测试
#: 来断言，否则哪天有人"顺手重排"``DBF_COLUMNS``，测试会跟着一起错（同源即同错）。
CLIENT_DBF_COLUMNS_BINARY: tuple[str, ...] = (
    "ORDER_TYPE",
    "PRICE_TYPE",
    "MODE_PRICE",
    "STOCK_CODE",
    "VOLUME",
    "ACCOUNT_ID",
    "ACT_TYPE",
    "BROKERTYPE",
    "STRATEGY",
    "NOTE",
    "TRADEPARAM",
    "INSERTTIME",
    "BASKETPATH",
    "EXTRAPARAM",
    "NOTE1",
)

#: 上述列名的取证出处（可复现：按偏移 dump `OrderService.dll` 的 ASCII 串）。
CLIENT_DBF_COLUMNS_EVIDENCE: dict[str, str] = {
    "binary": "OrderService.dll",
    "start_offset": "0x3100F8",
    "end_offset": "0x3101C4",
    "adjacent_symbol": "TC::CDbfStrategyRecord::setColumnParam",
    "adjacent_log": "[dbfStrategy [{0}] init [{1}] column Failure!",
    "verified_at": "2026-11-11",
}

#: INSERTTIME 的写入口径（**单点定义**，不要在别处内联格式串）。
#:
#: ★ 已知不一致，尚未裁决：`OrderService.dll` 的 `0x310418` 处格式串是
#: ``yyyy-MM-ddHH:mm:ss.zzz`` —— ``dd`` 与 ``HH`` 之间**没有空格**（22 字符），
#: 而这里写的是带空格的 23 字符。若客户端用它解析 INSERTTIME
#: （`COrderServiceDbfHelper::checkInsertTimeInvalid` 存在，说明确有校验），
#: 我们的时间戳就会解析失败。
#:
#: 但该串也可能只用于日志：它紧邻 `read {0} rows from {1} lastModify {2}`。
#: **在没有 armed 状态实测之前不改默认值** —— 改了同样是"无据的另一种写法"，
#: 只会把"可能错"换成"另一个可能错"。先把它收成单点常量，等实测再定。
INSERT_TIME_FORMAT = "%Y-%m-%d %H:%M:%S.000"

#: 二进制里出现的 INSERTTIME 候选格式（仅供对照与告警，不参与写入）。
CLIENT_INSERT_TIME_FORMAT_BINARY = "yyyy-MM-ddHH:mm:ss.zzz"

INSERT_TIME_FORMAT_EVIDENCE: dict[str, str] = {
    "binary": "OrderService.dll",
    "offset": "0x310418",
    "raw": "yyyy-MM-ddHH:mm:ss.zzz",
    "ours": INSERT_TIME_FORMAT,
    "status": "unresolved",
    "why": ("二进制串无空格（22 字符），我们写带空格（23 字符）；"
            "该串也可能只用于日志，未 armed 实测前不改"),
}

#: 客户端自有 DBF 的写法（实测 xt_client_status.dbf）：版本 0x03、C 型、长 20。
DBF_VERSION = 0x03
DBF_FIELD_NAME_LEN = 11
DBF_HEADER_BASE = 32
DBF_FIELD_DESC_LEN = 32
DBF_EOF = b"\x1a"

#: 保守列宽（字符数）。数值列也按 C 写 —— 客户端自己的 DBF 全用 C，
#: 用 C 比用 N 更宽容（N 会因空白/负号格式被拒）。真实列宽用 adopt_schema() 校准。
DEFAULT_WIDTHS: dict[str, int] = {
    "ORDER_TYPE": 8,
    "PRICE_TYPE": 8,
    "MODE_PRICE": 16,
    "STOCK_CODE": 16,
    "VOLUME": 16,
    "ACCOUNT_ID": 24,
    "ACT_TYPE": 8,
    "BROKERTYPE": 16,
    "STRATEGY": 40,
    "NOTE": 40,
    "TRADEPARAM": 64,
    "INSERTTIME": 24,
    "BASKETPATH": 80,
    "EXTRAPARAM": 64,
    "NOTE1": 40,
}

#: 默认编码：客户端字段名是 GBK（实测 `状态名称` 的字节即 GBK）。
DEFAULT_ENCODING = "gbk"


class FileOrderError(ValueError):
    """文件单生成/校验失败。"""


# ----------------------------------------------------------------------
# DBF 读写（dBase III，无 memo）
# ----------------------------------------------------------------------
@dataclass(frozen=True)
class DbfField:
    name: str
    type: str          # 'C' 字符 / 'N' 数值 / 'D' 日期 / 'L' 逻辑
    length: int
    decimals: int = 0


def _pad_bytes(raw: bytes, width: int, *, left: bool) -> bytes:
    """按 DBF 规矩补齐：C 左对齐右填空格，N 右对齐左填空格。"""
    if len(raw) > width:
        raw = raw[:width]
    fill = b" " * (width - len(raw))
    return raw + fill if left else fill + raw


def _text_of(value: Any, fld: DbfField) -> str:
    """把任意值变成写进 DBF 单元格的文本。"""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "T" if value else "F"
    if isinstance(value, float):
        if fld.decimals:
            return ("%%.%df" % fld.decimals) % value
        if fld.type == "N":
            return str(int(round(value)))
        # C 型列不能取整：mode_price=10.5 会被写成 "10"，是一张价格错的单。
        return "%g" % value
    return str(value)


def _encode_value(value: Any, fld: DbfField, encoding: str) -> bytes:
    raw = _text_of(value, fld).encode(encoding, errors="replace")
    return _pad_bytes(raw, fld.length, left=(fld.type != "N"))


def write_dbf(path: str, rows: Sequence[dict], fields: Sequence[DbfField], *,
              encoding: str = DEFAULT_ENCODING) -> int:
    """写一个 dBase III DBF，返回写入记录数。

    头长 ``32 + 32n + 1``、记录长 ``1 + Σlen`` —— 与客户端自有 DBF 实测一致。
    """
    if not fields:
        raise FileOrderError("至少需要一个字段")
    for fld in fields:
        if not fld.name:
            raise FileOrderError("字段名不能为空")
        if not 1 <= fld.length <= 255:
            raise FileOrderError(f"字段 {fld.name} 长度非法: {fld.length}")

    header_len = DBF_HEADER_BASE + DBF_FIELD_DESC_LEN * len(fields) + 1
    record_len = 1 + sum(f.length for f in fields)
    if header_len > 0xFFFF or record_len > 0xFFFF:
        raise FileOrderError("字段过多/过宽，超出 DBF 头部可表达范围")

    today = datetime.date.today()
    header = bytearray(DBF_HEADER_BASE)
    header[0] = DBF_VERSION
    header[1] = today.year - 1900
    header[2] = today.month
    header[3] = today.day
    struct.pack_into("<I", header, 4, len(rows))
    struct.pack_into("<H", header, 8, header_len)
    struct.pack_into("<H", header, 10, record_len)

    descs = bytearray()
    for fld in fields:
        name = fld.name.encode(encoding, errors="replace")[:DBF_FIELD_NAME_LEN]
        # dBase III 用 0x00 补齐字段名。用空格补会让读回的名字带尾随空格
        # （"ORDER_TYPE "），键名对不上就等于整张表读空。
        descs += name.ljust(DBF_FIELD_NAME_LEN, b"\x00")
        descs += fld.type.encode("ascii")[:1]
        descs += b"\x00\x00\x00\x00"          # 字段数据地址（写文件时填 0）
        descs += bytes([fld.length, fld.decimals])
        descs += b"\x00" * 14

    # ★ 列宽截断必须**报错**，不能静默切掉。
    #   实测踩过：模板把 INSERTTIME 校准成 16 字节，23 字符的时间戳被截成
    #   "2026-11-09 08:39"，DBF 本身完全合法、客户端也不报错，但写进去的
    #   时间戳是错的。价格 / 账号 / 代码被截断同理 —— 对下单文件就是写错单。
    #   旧版回读自检只比 4 列，恰好没看这些列，于是一路绿灯。
    for i, row in enumerate(rows):
        for fld in fields:
            text = _text_of(row.get(fld.name), fld)
            raw = text.encode(encoding, errors="replace")
            if len(raw) > fld.length:
                raise FileOrderError(
                    f"第 {i + 1} 行 {fld.name} 需要 {len(raw)} 字节，列宽只有 "
                    f"{fld.length}（值 {text!r}）：写入会被静默截断成 "
                    f"{raw[:fld.length]!r}。请换用列宽正确的模板，拒绝写错的单")

    body = bytearray()
    for row in rows:
        body += b"\x20"                        # 未删除标记
        for fld in fields:
            body += _encode_value(row.get(fld.name), fld, encoding)
    body += DBF_EOF

    tmp = f"{path}.tmp"
    with open(tmp, "wb") as fh:
        fh.write(bytes(header))
        fh.write(bytes(descs))
        fh.write(b"\x0d")
        fh.write(bytes(body))
    os.replace(tmp, path)
    return len(rows)


def _parse_field_descriptors(raw: bytes, encoding: str) -> list[DbfField]:
    """解析字段描述符区（每 32 字节一个）。

    字段名兼容两种补齐方式：本模块写的 ``0x00``，以及个别工具用空格补齐的写法 ——
    不 strip 掉空格会得到 ``"ORDER_TYPE "`` 这种键名，按列名取值必然 KeyError。
    """
    out: list[DbfField] = []
    for i in range(len(raw) // DBF_FIELD_DESC_LEN):
        chunk = raw[i * DBF_FIELD_DESC_LEN:(i + 1) * DBF_FIELD_DESC_LEN]
        if len(chunk) < DBF_FIELD_DESC_LEN:
            break
        if chunk[0:1] == b"\x0d":            # 描述符区提前结束
            break
        name = chunk[:DBF_FIELD_NAME_LEN].split(b"\x00", 1)[0].rstrip(b" ")
        out.append(DbfField(
            name=name.decode(encoding, errors="replace"),
            type=chr(chunk[11]),
            length=chunk[16],
            decimals=chunk[17],
        ))
    return out


def read_schema(path: str, *, encoding: str = DEFAULT_ENCODING) -> list[DbfField]:
    """读 DBF 头部拿到真实字段类型与长度（用于校准）。"""
    with open(path, "rb") as fh:
        head = fh.read(DBF_HEADER_BASE)
        if len(head) < DBF_HEADER_BASE:
            raise FileOrderError("DBF 头部不完整")
        if head[0] & 0x07 != DBF_VERSION:
            raise FileOrderError(f"不是 dBase III DBF（版本字节 0x{head[0]:02x}）")
        n = (struct.unpack_from("<H", head, 8)[0] - DBF_HEADER_BASE - 1) // DBF_FIELD_DESC_LEN
        raw = fh.read(DBF_FIELD_DESC_LEN * n)
    return _parse_field_descriptors(raw, encoding)


def read_dbf(path: str, *, encoding: str = DEFAULT_ENCODING) -> list[dict]:
    """读 DBF 全部记录（本模块自测与"写完回读核对"用）。"""
    with open(path, "rb") as fh:
        head = fh.read(DBF_HEADER_BASE)
        if len(head) < DBF_HEADER_BASE:
            raise FileOrderError("DBF 头部不完整")
        count = struct.unpack_from("<I", head, 4)[0]
        header_len = struct.unpack_from("<H", head, 8)[0]
        record_len = struct.unpack_from("<H", head, 10)[0]
        if record_len <= 0:
            raise FileOrderError("DBF 记录长度非法（0），头部已损坏")
        fh.seek(DBF_HEADER_BASE)
        raw = fh.read(max(header_len - DBF_HEADER_BASE - 1, 0))
        fields = _parse_field_descriptors(raw, encoding)
        if sum(f.length for f in fields) + 1 != record_len:
            raise FileOrderError(
                f"DBF 头部自相矛盾：字段宽合计 {sum(f.length for f in fields)}+1 "
                f"≠ 记录长 {record_len}")
        fh.seek(header_len)
        blob = fh.read(record_len * count)

    rows: list[dict] = []
    for r in range(count):
        chunk = blob[r * record_len:(r + 1) * record_len]
        if len(chunk) < record_len:
            break
        if chunk[0:1] == b"\x2a":          # '*' 已删除
            continue
        pos = 1
        row: dict[str, Any] = {}
        for fld in fields:
            cell = chunk[pos:pos + fld.length]
            pos += fld.length
            text = cell.decode(encoding, errors="replace").strip()
            if fld.type == "N" and text:
                try:
                    row[fld.name] = float(text) if fld.decimals else int(text)
                    continue
                except ValueError:
                    pass
            row[fld.name] = text
        rows.append(row)
    return rows


def adopt_schema(template_path: str, *, encoding: str = DEFAULT_ENCODING) -> list[DbfField]:
    """用客户端导出的模板 DBF 校准列宽/类型。

    只采纳**列名在本模块 DBF_COLUMNS 里**的字段，其余忽略 —— 模板可能带额外列，
    照抄会让写出的文件与 `CDbfStrategyRecord` 的列序不一致。
    """
    found = {f.name.upper(): f for f in read_schema(template_path, encoding=encoding)}
    picked: list[DbfField] = []
    for col in DBF_COLUMNS:
        fld = found.get(col)
        if fld is not None:
            picked.append(fld)
    if not picked:
        raise FileOrderError(f"模板 {template_path} 里没有任何已知下单列，无法校准")
    return picked


def default_fields() -> list[DbfField]:
    """默认字段表：客户端同款方言（C 型 + 保守列宽）。"""
    return [DbfField(name=c, type="C", length=DEFAULT_WIDTHS[c]) for c in DBF_COLUMNS]


# ----------------------------------------------------------------------
# 下单行
# ----------------------------------------------------------------------
@dataclass
class FileOrderRow:
    """一行文件单。字段名与 DBF 列一一对应。"""

    stock_code: str
    volume: int
    order_type: int
    price_type: int
    mode_price: float = 0.0
    account_id: str = ""
    act_type: str = ""
    broker_type: str = ""
    strategy: str = ""
    note: str = ""
    trade_param: str = ""
    insert_time: str = ""
    basket_path: str = ""
    extra_param: str = ""
    note1: str = ""
    #: 由 ``to_record()`` 生成，不在构造参数里 —— 保持 __init__ 简单可读。

    def validate(self) -> list[str]:
        """返回问题列表；空列表表示可以写。

        校验只做**能确定**的事（代码格式、数量、价格），不猜业务规则。
        """
        problems: list[str] = []
        code = (self.stock_code or "").strip()
        if "." not in code:
            problems.append(f"stock_code 必须形如 '600036.SH'，收到 {code!r}")
        else:
            left, _, right = code.partition(".")
            if not left or not right:
                problems.append(f"stock_code 代码段/市场段不能为空: {code!r}")
        if not isinstance(self.volume, int) or self.volume <= 0:
            problems.append(f"volume 必须是正整数，收到 {self.volume!r}")
        if not isinstance(self.order_type, int) or self.order_type < 0:
            problems.append(f"order_type 必须是非负整数，收到 {self.order_type!r}")
        if not isinstance(self.price_type, int) or self.price_type < -1:
            problems.append(f"price_type 必须是 >= -1 的整数，收到 {self.price_type!r}")
        if self.mode_price < 0:
            problems.append(f"mode_price 不能为负，收到 {self.mode_price!r}")
        return problems

    def to_record(self) -> dict:
        return {
            "ORDER_TYPE": self.order_type,
            "PRICE_TYPE": self.price_type,
            "MODE_PRICE": self.mode_price,
            "STOCK_CODE": self.stock_code,
            "VOLUME": self.volume,
            "ACCOUNT_ID": self.account_id,
            "ACT_TYPE": self.act_type,
            "BROKERTYPE": self.broker_type,
            "STRATEGY": self.strategy,
            "NOTE": self.note,
            "TRADEPARAM": self.trade_param,
            # ★ 2026-11-10：改走 core.clock.local_now()（唯一时刻实现），
            #   DBF 的 INSERTTIME 格式串逐字不变（test_clock_unity 禁内联 now）。
            # ★ 2026-11-11：格式串收进 ``INSERT_TIME_FORMAT`` 单点定义
            #   （二进制里另有一个无空格写法，见该常量注释，未裁决前不改）。
            "INSERTTIME": self.insert_time or local_now().strftime(
                INSERT_TIME_FORMAT),
            "BASKETPATH": self.basket_path,
            "EXTRAPARAM": self.extra_param,
            "NOTE1": self.note1,
        }


# ----------------------------------------------------------------------
# 监控目录：从客户端自己的 Config.xml 读，不靠猜
# ----------------------------------------------------------------------
#: 四个安装实测都是 ``{root}/export_data`` + ``{root}/export_data_backup``，
#: 但**不要**硬编码 —— 用户可以在界面里改，必须以 Config.xml 为准。
CONFIG_XML = "Config.xml"


@dataclass(frozen=True)
class DbfMonitorSetting:
    """``<DbfMonitorSetting>`` 标签的语义化视图。"""

    monitor_path: str
    backup_path: str = ""
    is_automonitor: bool = False
    is_incexport: bool = False
    is_clear: bool = False
    remove_before_first_monitor: bool = False
    is_sharedread: bool = False
    orderid_feedback: str = ""

    @property
    def armed(self) -> bool:
        """监控是否真的开着 —— **这是文件单能不能生效的总开关**。

        实测四个安装全部 ``is_automonitor="0"``：往 export_data 丢 DBF
        不会产生任何委托，文件只是躺在目录里。
        """
        return self.is_automonitor and bool(self.monitor_path)


def _attr_bool(tag, name: str) -> bool:
    raw = (tag.get(name) or "").strip()
    return raw in ("1", "true", "True", "yes")


def _setting_from_attrs(attrs: dict) -> DbfMonitorSetting:
    def flag(name: str) -> bool:
        return (attrs.get(name) or "").strip() in ("1", "true", "True", "yes")

    return DbfMonitorSetting(
        monitor_path=(attrs.get("monitor_path") or "").strip(),
        backup_path=(attrs.get("backup_path") or "").strip(),
        is_automonitor=flag("is_automonitor"),
        is_incexport=flag("is_incexport"),
        is_clear=flag("is_clear"),
        remove_before_first_monitor=flag("need_remove_exprotfile_fristmonitor"),
        is_sharedread=flag("is_sharedread"),
        orderid_feedback=(attrs.get("orderidfeedbackval") or "").strip(),
    )


def parse_monitor_setting(xml_bytes: bytes) -> DbfMonitorSetting | None:
    """从 Config.xml 字节里取出 ``<DbfMonitorSetting>``；没有该标签返回 None。

    ★ 实测客户端自己的 ``Config.xml`` **不是合法 XML**（ET 在第 345 行
    ``not well-formed (invalid token)`` 直接抛 ParseError），所以必须有正则兜底。
    只走 ElementTree 会在真机上恒返回 None —— 一个静默的假阴性。
    """
    import re
    import xml.etree.ElementTree as ET

    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        root = None
    if root is not None:
        node = root.find(".//DbfMonitorSetting")
        if node is not None:
            return _setting_from_attrs(node.attrib)
        return None

    text = xml_bytes.decode("utf-8", errors="replace")
    m = re.search(r"<DbfMonitorSetting\b([^>]*)>", text)
    if not m:
        return None
    return _setting_from_attrs(dict(re.findall(r'(\w+)="([^"]*)"', m.group(1))))


def read_monitor_setting(config_xml: str) -> DbfMonitorSetting | None:
    """读一个 ``users/<账号>/Config.xml``。"""
    try:
        with open(config_xml, "rb") as fh:
            return parse_monitor_setting(fh.read())
    except OSError:
        return None


def find_monitor_settings(userdata: str) -> list[tuple[str, DbfMonitorSetting]]:
    """扫描 ``userdata/users/*/Config.xml``，返回 ``[(账号目录, 设置)]``。

    一个安装可能有多个登录账号，每个账号**各有自己的**监控目录。
    """
    users = os.path.join(userdata, "users")
    if not os.path.isdir(users):
        return []
    out: list[tuple[str, DbfMonitorSetting]] = []
    for name in sorted(os.listdir(users)):
        cfg = os.path.join(users, name, CONFIG_XML)
        setting = read_monitor_setting(cfg)
        if setting is not None:
            out.append((name, setting))
    return out


def monitor_paths_of(userdata: str) -> list[str]:
    """收集一个安装下**所有账号**的监控目录（去重、只留非空）。

    直接喂给 ``write_order_file(monitor_paths=...)`` 用。
    """
    seen: list[str] = []
    for _, setting in find_monitor_settings(userdata):
        for p in (setting.monitor_path, setting.backup_path):
            if p and p not in seen:
                seen.append(p)
    return seen


# ----------------------------------------------------------------------
# 批量单（第二条外部下单口）：只读盘点，不写、不下单
# ----------------------------------------------------------------------
def parse_batch_order(xml_bytes: bytes) -> dict | None:
    """取 ``<batchOrderSetting>``。实测四个安装的 Config.xml 都有，全部 key_enable=0。

    与 DBF 文件单是**两条不同的通道**：``addBatchOrderStrategy`` /
    ``getDefaultBatchOrderDir``，文件名形如 ``{0}/{1}.{2}.csv``。
    字段集尚未解出，本模块只做盘点，**不提供写入**。
    """
    import re

    m = re.search(rb"<batchOrderSetting\b([^>]*)>", xml_bytes)
    if not m:
        return None
    attrs = dict(re.findall(rb'(\w+)="([^"]*)"', m.group(1)))
    dec = lambda k: (attrs.get(k) or b"").decode("utf-8", errors="replace").strip()  # noqa: E731
    return {
        "monitor_path": dec(b"monitor_path"),
        "key_enable": dec(b"key_enable") in ("1", "true", "True", "yes"),
        "key_sequence": dec(b"key_sequence"),
    }


def batch_order_of(userdata: str) -> list[tuple[str, dict]]:
    """一个安装下各账号的批量单设置。"""
    users = os.path.join(userdata, "users")
    if not os.path.isdir(users):
        return []
    out: list[tuple[str, dict]] = []
    for name in sorted(os.listdir(users)):
        cfg = os.path.join(users, name, CONFIG_XML)
        try:
            with open(cfg, "rb") as fh:
                setting = parse_batch_order(fh.read())
        except OSError:
            continue
        if setting is not None:
            out.append((name, setting))
    return out


def _configs_near(base: str) -> list[str]:
    """列出 ``base`` 附近可能出现的 Config.xml（根目录 / users / userdata*）。"""
    out: list[str] = []
    direct = os.path.join(base, CONFIG_XML)
    if os.path.isfile(direct):
        out.append(direct)
    for sub in ("users", os.path.join("userdata", "users"),
                os.path.join("userdata_mini", "users")):
        folder = os.path.join(base, sub)
        if not os.path.isdir(folder):
            continue
        try:
            names = sorted(os.listdir(folder))
        except OSError:
            continue
        for name in names:
            cfg = os.path.join(folder, name, CONFIG_XML)
            if os.path.isfile(cfg):
                out.append(cfg)
    return out


def monitor_paths_near(path: str, *, max_up: int = 4) -> list[str]:
    """**不依赖 roots** 地从目标路径自身向上找监控目录。

    ``stage`` 的反向自检原先只拿「扫描 roots 得到的安装」里的监控目录；
    一旦 roots 解析不出来（既没显式传、也没有已配置连接），这张表就是空的，
    ``is_under(staging, p)`` 恒为 False —— 检查看起来做了，其实是**空集上的检查**。
    这里直接从 staging 路径的祖先目录读 Config.xml，把检查变成局部的、
    与"有没有扫到其它安装"无关。
    """
    cur = os.path.abspath(path)
    out: list[str] = []
    for _ in range(max_up + 1):
        for cfg in _configs_near(cur):
            try:
                with open(cfg, "rb") as fh:
                    raw = fh.read()
            except OSError:
                continue
            setting = parse_monitor_setting(raw)
            batch = parse_batch_order(raw)
            candidates = []
            if setting is not None:
                candidates += [setting.monitor_path, setting.backup_path]
            if batch is not None:
                candidates.append(batch["monitor_path"])
            for p in candidates:
                if p and p not in out:
                    out.append(p)
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    return out


# ----------------------------------------------------------------------
# 安全写入口
# ----------------------------------------------------------------------
def is_under(path: str, parent: str) -> bool:
    """``path`` 是否位于 ``parent`` 之下（**解析符号链接 / Windows junction**）。

    ★ 原来只用 ``abspath`` 比 commonpath。Windows 上目录联接（junction）很常见：
      监控目录里放一个指向别处的联接，或 staging 路径经过联接指回监控目录，
      字面路径比不出来，安全检查就会被绕过 —— 而这里检查的正是
      "生成文件会不会变成真委托"。必须用 ``realpath`` 落到物理路径再比。
    """
    try:
        a = os.path.realpath(os.path.abspath(path))
        b = os.path.realpath(os.path.abspath(parent))
        return os.path.commonpath([a, b]) == b
    except (ValueError, OSError):
        return False


def _ensure_order_columns(fields: Sequence[DbfField]) -> None:
    """写单前强制核对列名与**列序**：客户端按列序读，少一列后面全错位。

    ``/qmt/file-order/stage`` 已经在校准模板时检查过缺列，但库函数
    ``write_order_file(fields=...)`` 是另一条入口 —— 任何调用方把
    ``adopt_schema()`` 的**部分结果**直接传进来，都会静默写出一张错位的表。
    护栏要放在最终写入口，而不是依赖每个调用方都记得检查。
    """
    names = [str(f.name).upper() for f in fields]
    if names != list(DBF_COLUMNS):
        missing = [c for c in DBF_COLUMNS if c not in names]
        raise FileOrderError(
            "字段表必须按 CDbfStrategyRecord 的列序完整覆盖 15 列："
            f"缺列 {missing or '无'}，当前列序 {names}")


def write_order_file(path: str, rows: Iterable[FileOrderRow], *,
                     fields: Sequence[DbfField] | None = None,
                     monitor_paths: Iterable[str] = (),
                     allow_monitor_dir: bool = False,
                     encoding: str = DEFAULT_ENCODING) -> dict:
    """把下单行写成 DBF。**默认拒绝写入监控目录。**

    文件单的语义是"目录里出现文件就下单"，所以"生成文件"与"把文件交给客户端"
    必须是两步。本函数把这一步做成显式开关，避免调试时误触发真单。

    返回 ``{"path", "rows", "bytes", "monitor_dir"}``。
    """
    items = list(rows)
    if not items:
        raise FileOrderError("没有可写的下单行")

    bad: list[str] = []
    for i, row in enumerate(items):
        for p in row.validate():
            bad.append(f"第 {i + 1} 行: {p}")
    if bad:
        raise FileOrderError("下单行校验失败：\n" + "\n".join(bad))

    hit = [m for m in monitor_paths if m and is_under(path, m)]
    if hit and not allow_monitor_dir:
        raise FileOrderError(
            "目标路径位于文件单监控目录内（%s）。写入即等于下单；"
            "确认要交付给客户端请显式传 allow_monitor_dir=True" % ", ".join(hit))

    used = list(fields) if fields else default_fields()
    _ensure_order_columns(used)
    n = write_dbf(path, [r.to_record() for r in items], used, encoding=encoding)
    known = [m for m in monitor_paths if m]
    return {
        "path": path,
        "rows": n,
        "bytes": os.path.getsize(path),
        "monitor_dir": bool(hit),
        #: ``False`` = 调用方没给监控目录，**等于没检查过** —— 不是"检查了且安全"。
        #: 不区分这两态就会把"漏传参数"读成"已核对"，正是本仓库最忌的假绿。
        "monitor_checked": bool(known),
        "columns": [f.name for f in used],
    }


#: readiness 里「不过就真的下不了单」的步骤；其余步骤只告警不拦。
READINESS_BLOCKING_STEPS: tuple[str, ...] = ("config", "armed", "monitor_dir", "writable")


def file_order_readiness(userdata: str, *,
                         client_running: bool | None = None) -> dict:
    """文件单从「发现」到「真能下单」的完整前置链，逐项给结论。

    ★ 为什么需要它
    --------------
    ``armed`` 只是**总开关**，不是全部前置。一条文件单要变成委托，至少还要：
    监控目录存在、目录可写、列名/列序对得上、INSERTTIME 口径对得上、客户端在线。
    过去只有一个 ``armed`` 布尔，很容易把「开关开了」读成「可以下单」。

    ★ 刻意**不做**写探测
    --------------------
    验证「目录可写」最直觉的做法是往监控目录写个探针文件 —— 但 **armed 时
    写文件就等于下单**。所以这里只用 ``os.access`` 做只读权限判断，并在返回值里
    写明 ``probe_write_skipped=True``：不把「没试过」说成「试过且没问题」。

    返回 ``{"ready","blocking_step","blocking_detail","steps":[...],...}``。
    ``ready=True`` 只表示**客户端侧前置齐全**，不代表通道已被证实能成交。
    """
    steps: list[dict] = []

    def add(name: str, is_ok: bool, detail: str) -> None:
        steps.append({"name": name, "ok": is_ok, "detail": detail,
                      "blocking": name in READINESS_BLOCKING_STEPS})

    accounts: list[tuple[str, DbfMonitorSetting]] = []
    cfg_err = ""
    try:
        accounts = find_monitor_settings(userdata)
    except Exception as exc:  # noqa: BLE001 配置读不出来要如实报，不能当成"没开"
        cfg_err = f"{type(exc).__name__}: {exc}"
    add("config", bool(accounts),
        cfg_err or (f"解析到 {len(accounts)} 个账号" if accounts
                    else "未找到 <DbfMonitorSetting>（Config.xml 可能不在该 userdata 下）"))

    account, setting = (accounts[0][0], accounts[0][1]) if accounts else ("", None)
    monitor = setting.monitor_path if setting else ""
    armed = bool(setting and setting.armed)
    add("armed", armed,
        f"is_automonitor={getattr(setting, 'is_automonitor', None)}，"
        f"monitor_path={monitor or '(空)'}"
        + ("" if armed else " ⇒ 丢 DBF 不会产生任何委托，文件只会躺在目录里"))

    exists = bool(monitor) and os.path.isdir(monitor)
    add("monitor_dir", exists, f"{monitor or '(空)'} " + ("存在" if exists else "不存在"))

    writable = bool(exists) and os.access(monitor, os.W_OK)
    add("writable", writable,
        ("未做写探测（armed 时写探针文件即等于下单），仅用 os.access 判只读权限"
         f" → {writable}") if exists else "目录不存在，未判定")

    templates: list[str] = []
    if exists:
        try:
            templates = sorted(n for n in os.listdir(monitor) if n.lower().endswith(".dbf"))
        except OSError:
            templates = []
    add("template", True,
        (f"监控目录有 {len(templates)} 个 DBF 可作列宽模板" if templates else
         "无 DBF 模板 ⇒ 只能用 DEFAULT_WIDTHS 保守列宽（列名/列序已由二进制证实）"))

    add("client", client_running is not False,
        "未检查（调用方未上报客户端运行态）" if client_running is None
        else ("客户端在运行" if client_running else "客户端未运行：文件不会被读取"))

    add("insert_time_format", True,
        f"写入用 {INSERT_TIME_FORMAT}；二进制为 {CLIENT_INSERT_TIME_FORMAT_BINARY}，"
        "口径未裁决（见 INSERT_TIME_FORMAT_EVIDENCE）")

    blocking_bad = [s for s in steps if s["blocking"] and not s["ok"]]
    return {
        "userdata": userdata,
        "account": account,
        "monitor_path": monitor,
        "armed": armed,
        "ready": not blocking_bad,
        "blocking_step": blocking_bad[0]["name"] if blocking_bad else "",
        "blocking_detail": blocking_bad[0]["detail"] if blocking_bad else "",
        "steps": steps,
        "columns_verified": tuple(DBF_COLUMNS) == CLIENT_DBF_COLUMNS_BINARY,
        "columns_evidence": dict(CLIENT_DBF_COLUMNS_EVIDENCE),
        "insert_time_format": dict(INSERT_TIME_FORMAT_EVIDENCE),
        "probe_write_skipped": True,
        "honesty": ("ready=True 只表示客户端侧前置齐全；本轮仍未投单，"
                    "不代表该通道已被证实能成交"),
    }


__all__ = [
    "DBF_COLUMNS", "DEFAULT_WIDTHS", "DEFAULT_ENCODING", "DBF_VERSION",
    "CLIENT_DBF_COLUMNS_BINARY", "CLIENT_DBF_COLUMNS_EVIDENCE",
    "INSERT_TIME_FORMAT", "CLIENT_INSERT_TIME_FORMAT_BINARY",
    "INSERT_TIME_FORMAT_EVIDENCE", "READINESS_BLOCKING_STEPS",
    "DbfField", "FileOrderRow", "FileOrderError", "DbfMonitorSetting",
    "write_dbf", "read_dbf", "read_schema", "adopt_schema", "default_fields",
    "is_under", "write_order_file", "file_order_readiness",
    "parse_monitor_setting", "read_monitor_setting", "find_monitor_settings",
    "monitor_paths_of", "monitor_paths_near",
    "parse_batch_order", "batch_order_of",
]
