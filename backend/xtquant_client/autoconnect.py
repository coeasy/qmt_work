"""自动探测与「启动自动连接」决策。

为什么要单独成模块
------------------
「探测本机客户端 → 挑一个连上」这条链路有两个消费方：

1. REST 端点 ``GET /brokers/auto-detect``（用户点「重新探测」/ 进页面自动探测）；
2. 启动 bootstrap 的自动连接（``app/bootstrap/phase_broker.py``）。

两者**必须同源**：如果界面推荐的是 A 客户端、启动时自动连的是 B，用户会觉得
「软件自己在乱连」。因此把「丰富化候选 + 挑活跃项 + 组装连接字段」全部收在这里，
路由层与 bootstrap 都只调用本模块。

设计要点
--------
- ``detect_candidates``：唯一的候选丰富化实现（账号发现 / 券商档案 / 极速版改写）。
- ``pick_active``：**只挑「正在运行」的客户端**。未运行的客户端需要用户先在 GUI 里
  登录，自动拉起进程会弹出登录窗并可能让无人值守的启动路径卡住 —— 那不属于
  「自动连接」的语义，属于「自动启动客户端」，刻意不做。
- ``build_connection``：缺资金账号或路径时返回 ``None``，**绝不猜一个账号出来** ——
  猜错账号意味着把订单下到别人的户上。
"""
from __future__ import annotations

from .discovery import discover, discover_accounts, guess_broker_id_by_name

__all__ = ["detect_candidates", "pick_active", "build_connection", "resolve_broker_id"]


def resolve_broker_id(cand: dict) -> str:
    """候选的券商档案 id（优先级：Config.xml 真实券商名 > 路径猜测 > generic）。

    只认「无歧义」的券商名关键词（光大/国信等确认归入 generic 通用迅投档案，
    广发/银河/国金等归各自档案）。路径缩写（gd=广发/光大）在这里不猜。
    真实券商名无法确定识别时不覆盖路径猜测，仍为空则归 generic —— 避免把
    目录明确的候选（如 银河/国金）在无账号时被误降级成 generic。
    """
    by_name = guess_broker_id_by_name(cand.get("broker_name") or "")
    if by_name:
        return by_name
    return cand.get("broker_id") or "generic"


def detect_candidates() -> list[dict]:
    """探测本机 QMT 候选并**就地丰富化**（资金账号 / 券商档案 / 客户端模式）。

    这是「自动探测」的唯一实现，REST 端点与启动自动连接都走它。
    单个候选的丰富化失败被隔离（该候选降级为「无账号 + generic」），不影响其它候选。
    """
    cands = discover()
    for c in cands:
        try:
            # 防御性归一化：c["root"] 可能缺失，发现接口也可能非 list
            scan_path = c.get("client_path") or c.get("root") or ""
            c["accounts"] = list(discover_accounts(scan_path) or [])
            if c["accounts"]:
                # 取第一个账号作为默认补全，供前端「一键填入」与启动自动连接使用
                c["default_account_id"] = c["accounts"][0]["account_id"]
                c["broker_name"] = (c["accounts"][0].get("broker_name")
                                    or c.get("broker_name", ""))
            else:
                c["default_account_id"] = ""

            c["broker_id"] = resolve_broker_id(c)

            # 通用档案建议用极速版：识别为 generic（含光大/国信等）或无明确券商时，
            # 完整版大客户端(XtItClient)常对独立外部进程报 'illegal pid' 拒绝接入，
            # 而极速版 MiniQMT(userdata_mini) 是更稳的程序化通道——优先建议 mini。
            if c.get("broker_id") == "generic" and c.get("has_userdata_mini"):
                c["client_mode"] = "mini"
                cur = c.get("client_path") or ""
                if not cur.endswith("userdata_mini"):
                    c["client_path"] = c.get("client_path_mini") or cur
        except Exception:  # noqa: BLE001 — 单候选失败降级，不影响整体
            c["accounts"] = []
            c["default_account_id"] = ""
            if not c.get("broker_id"):
                c["broker_id"] = "generic"
    return cands


def _rank(cand: dict) -> tuple[int, int, int]:
    """活跃度打分：**运行中 + 有资金账号** > 仅运行中 > 仅有账号。

    三个分量都进元组（而不是只留总分），是为了让「运行中且无账号」仍然排在
    「未运行但有账号」之前 —— 前者只差一次账号发现，后者还差用户去登录客户端。
    """
    running = 1 if cand.get("running") else 0
    has_acc = 1 if (cand.get("default_account_id") or cand.get("accounts")) else 0
    return (running * 2 + has_acc, running, has_acc)


def pick_active(candidates: list[dict] | None) -> dict | None:
    """挑出「当前活跃客户端」：运行中且已读到资金账号的那个。

    返回 ``None`` 表示没有可自动接入的客户端（没装 / 没启动 / 没登录）。
    排序用 ``sorted``（稳定），同分时保持 ``discover()`` 的原始顺序，
    使「界面列表里的第一个推荐项」与「实际自动连的」一致。
    """
    usable = [c for c in (candidates or []) if isinstance(c, dict)]
    if not usable:
        return None
    best = sorted(usable, key=_rank, reverse=True)[0]
    # 未运行 → 不自动连（需要用户先登录客户端，见模块 docstring）
    if not best.get("running"):
        return None
    return best


def build_connection(cand: dict) -> dict | None:
    """把候选转成 ``BrokerManager.add_connection`` 所需的连接字段。

    返回 ``None`` = 该候选不足以建立连接（缺资金账号或客户端路径）。
    ``conn_id`` 留空由 manager 生成，避免与既有连接 id 冲突。
    """
    client_path = (cand.get("client_path") or cand.get("root") or "").strip()
    accounts = cand.get("accounts") or []
    acc_id = str(cand.get("default_account_id") or "").strip()
    if not acc_id and accounts:
        acc_id = str(accounts[0].get("account_id") or "").strip()
    if not client_path or not acc_id:
        return None

    acc_type = "STOCK"
    for a in accounts:
        if str(a.get("account_id") or "") == acc_id:
            acc_type = a.get("account_type") or "STOCK"
            break

    label = cand.get("broker_name") or cand.get("name") or "本机客户端"
    return {
        "conn_id": "",
        "name": f"{label}（自动连接）",
        "broker_id": resolve_broker_id(cand),
        "client_path": client_path,
        "client_mode": cand.get("client_mode") or "auto",
        "account_id": acc_id,
        "account_type": acc_type,
        "active": True,
    }
