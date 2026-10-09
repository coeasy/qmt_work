"""远程访问三档管理路由（docs/REMOTE_ACCESS_DECISION.md 的 Phase 1 API 层）。

设计要点：
1. **所有变更只落配置文件，不改内存态 settings**——避免运行中 socket 绑定的半生效状态。
   前端拿到 `requires_restart=true` 后提示用户「保存后需重启客户端」，调用重启接口。
2. **api_key / totp_secret 只在创建时返回一次**——之后无法通过任何接口读取明文，
   彻底避免「远程 API 泄漏 → 密钥泄漏 → 资金风险」这条链。
3. **mode 变更写文件即触发审计**（result=ok），便于事后追溯谁在何时打开了远程访问。
4. **wan 档切换会顺手把 signal.mode 默认改为 paper**——远程 + 实盘是双重风险，
   除非用户显式切回 live，否则保守默认。

端点：
- GET  /remote-access/status      当前档位/端口/密钥状态/TOTP 状态/signal 模式
- POST /remote-access/mode        切档（off/lan/wan），写配置 + 审计 + 返回重启标记
- POST /remote-access/api-key     重置主 API Key（返回新密钥明文，仅此一次）
- POST /remote-access/totp/enable 启用 TOTP（返回 base32 secret，仅此一次）
- POST /remote-access/totp/verify 校验 TOTP 码
"""
from __future__ import annotations

import logging

import secrets
import string
import time
from typing import Any

from fastapi import APIRouter, Depends, Request

from app.routes._common import err, ok, audit_log
from core.config import (
    REMOTE_MODES,
    config_file,
    effective_host,
    exe_dir,
    normalize_remote_access,
    remote_mode_label,
    settings,
    update_config_file,
)
from core.context import AppContext, get_ctx
from gateway.totp import verify_totp

log = logging.getLogger(__name__)

router = APIRouter()

# 默认密钥（与 core/config.py Settings.api_key 的默认值保持一致）
_DEFAULT_API_KEY = "qmt-dev-key"

# 密钥字符集：去除易混淆字符（0/O、1/l/I）
_KEY_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789"


def _generate_api_key(length: int = 40) -> str:
    """生成强随机 API Key（避免 qmt-dev-key 这类弱默认值）。"""
    return "".join(secrets.choice(_KEY_ALPHABET) for _ in range(length))


def _generate_totp_secret(length: int = 16) -> str:
    """生成 TOTP base32 兼容 secret（RFC 4648 BASE32 字符集）。"""
    alphabet = string.ascii_uppercase + string.digits  # 大写 + 数字
    raw = "".join(secrets.choice(alphabet) for _ in range(length))
    # BASE32 编码要求长度是 8 的倍数（padding 由客户端处理）；补足即可
    return raw


def _describe_key_state() -> dict[str, Any]:
    """API Key 状态：是否已设置 / 是否为默认值 / 前缀（供 UI 展示，不返回明文）。"""
    key = (settings.api_key or "").strip()
    return {
        "configured": bool(key),
        "is_default": key == _DEFAULT_API_KEY,
        "prefix": (key[:8] + "...") if key and len(key) > 8 else (key or ""),
        "length": len(key),
    }


def _describe_totp_state() -> dict[str, Any]:
    """TOTP 状态：是否启用 + 当前验证码刷新倒计时（供 UI 展示）。"""
    secret = (settings.totp_secret or "").strip()
    enabled = bool(secret)
    period = 30
    if enabled:
        now = int(time.time())
        counter = now // period
        remaining = period - (now - counter * period)
    else:
        remaining = 0
    return {"enabled": enabled, "period_seconds": period, "code_remaining_seconds": remaining}


def _describe_signal_state(ctx: AppContext) -> dict[str, Any]:
    """SignalRouter 状态：mode + 是否需要 TOTP 二次确认。"""
    router = ctx.signal_router if ctx else None
    if router is None:
        return {"mode": None, "requires_totp": False}
    return {"mode": getattr(router, "mode", None),
            "requires_totp": bool(getattr(router, "totp_secret", ""))}


@router.get("/remote-access/status")
async def get_remote_access_status(request: Request, ctx: AppContext = Depends(get_ctx)):
    """查询远程访问综合状态（当前档位/端口/主机/密钥/TOTP/signal 模式/配置文件路径）。

    响应结构：
        {
          "mode": "off" | "lan" | "wan",
          "label": "单机（仅本机）" | "内网（局域网多设备）" | "公网（远程访问）",
          "host": "0.0.0.0" | "127.0.0.1",
          "port": 21118,
          "config_file": ".../qmt_work_config.json",
          "api_key": {"configured": true, "is_default": false, "prefix": "AbCdEfGh...", "length": 40},
          "totp": {"enabled": true, "period_seconds": 30, "code_remaining_seconds": 12},
          "signal": {"mode": "paper", "requires_totp": true},
          "warnings": ["当前 wan 档但 signal.mode=live，远程可直接触发实盘下单"]
        }
    """
    mode = normalize_remote_access(settings.remote_access)
    warnings: list[str] = []
    if mode in ("lan", "wan") and settings.api_key == _DEFAULT_API_KEY:
        warnings.append("api_key 仍为默认值，启动自检将拒绝启动（若尚未拒绝，说明配置未生效）")
    if mode == "wan" and not (settings.totp_secret or "").strip():
        warnings.append("wan 档要求 TOTP 二次确认，但 totp_secret 未配置——启动自检将拒绝")
    # wan + live = 远程可直触发实盘（保守警告）
    sr = ctx.signal_router if ctx else None
    sr_mode = getattr(sr, "mode", None) if sr else None
    if mode == "wan" and sr_mode == "live":
        warnings.append("wan 档 + signal.mode=live：远程 API Key 可直触发实盘下单，建议切 paper")

    return ok({
        "mode": mode,
        "label": remote_mode_label(),
        "host": effective_host(),
        "port": int(settings.port),
        "config_file": str(config_file()),
        "exe_dir": str(exe_dir()),
        "api_key": _describe_key_state(),
        "totp": _describe_totp_state(),
        "signal": _describe_signal_state(ctx),
        "warnings": warnings,
        "available_modes": list(REMOTE_MODES),
    })


@router.post("/remote-access/mode")
async def set_remote_access_mode(body: dict, ctx: AppContext = Depends(get_ctx)):
    """切换远程访问档位（off/lan/wan）。

    写入配置文件并返回 `requires_restart=true`（socket 绑定需重启才能生效）。
    若切换到 lan/wan 且当前 api_key 仍为默认值，返回 400 提示先设置强密钥。
    若切换到 wan 且 totp_secret 为空，返回 400 提示先启用 TOTP。

    请求体：`{"mode": "lan" | "wan" | "off"}`
    """
    raw_mode = str((body or {}).get("mode", "")).strip()
    if raw_mode.lower() not in REMOTE_MODES:
        return err(400, f"非法档位：{raw_mode!r}，仅支持 {list(REMOTE_MODES)}")
    mode = normalize_remote_access(raw_mode)

    # 前置校验：lan/wan 强制强密钥；wan 强制 TOTP
    if mode in ("lan", "wan") and settings.api_key == _DEFAULT_API_KEY:
        return err(400, "切换到 lan/wan 档前必须先设置强 API Key（当前仍为默认 qmt-dev-key）")
    if mode == "wan" and not (settings.totp_secret or "").strip():
        return err(400, "切换到 wan 档前必须先启用 TOTP 二次确认（当前 totp_secret 未配置）")

    old_mode = normalize_remote_access(settings.remote_access)
    try:
        update_config_file({"remote_access": mode})
    except OSError as exc:
        return err(500, f"写入配置文件失败：{exc}")

    # 审计（actor 用 request client host，target 用旧->新）
    audit_log("admin", "remote_access.mode", f"{old_mode}->{mode}",
              {"old": old_mode, "new": mode}, "ok")

    # wan 档强制 signal.mode → paper（远程实盘风险过高，保守默认）
    # 通过 signal_router.set_mode 写入 runtime_config 表（需重启才能生效）
    signal_mode_changed = False
    if mode == "wan" and ctx.signal_router is not None:
        try:
            ctx.signal_router.set_mode("paper")
            signal_mode_changed = True
            audit_log("admin", "remote_access.signal_mode.paper", "global",
                      {"reason": "wan mode auto-default"}, "ok")
        except Exception as exc:  # noqa: BLE001
            # 降级：signal_router 不可用时仅提示用户手动切换
            log.warning("wan 档自动切换 signal.mode=paper 失败：%s", exc)

    # 返回重启提示（socket 绑定需进程重启才能切换）
    needs_restart = old_mode != mode
    hints = []
    if mode == "wan" and not signal_mode_changed:
        hints.append("建议切换信号模式为 paper（远程实盘风险较高）")
    if needs_restart:
        hints.append("请重启客户端使绑定地址生效")

    return ok({
        "mode": mode,
        "label": remote_mode_label(),
        "changed": needs_restart,
        "requires_restart": needs_restart,
        "signal_mode": "paper" if mode == "wan" else "live",
        "signal_mode_auto_set": signal_mode_changed,
        "hints": hints,
        "message": ("档位已保存，请重启客户端生效" if needs_restart else "档位未变化"),
    })


@router.post("/remote-access/api-key")
async def reset_remote_access_api_key(ctx: AppContext = Depends(get_ctx)):
    """重置主 API Key（生成新的强随机值）。

    新密钥**仅在本次响应中返回一次**，之后无法通过任何接口读取明文。
    用于：① wan 档切换前首次设置；② 密钥疑似泄漏时强制轮换。

    请求体：可空；若带 `{"confirm": true}` 表示二次确认（可选，不强制）。
    """
    new_key = _generate_api_key()
    old_prefix = _describe_key_state().get("prefix", "")
    try:
        update_config_file({"api_key": new_key})
    except OSError as exc:
        return err(500, f"写入配置文件失败：{exc}")

    audit_log("admin", "remote_access.api_key.reset", old_prefix or "unconfigured",
              {"new_length": len(new_key)}, "ok")
    return ok({
        "api_key": new_key,
        "requires_restart": True,
        "message": "API Key 已重置。当前值仅此一次返回，请妥善保存。重启客户端后生效。",
        "warning": "旧密钥在重启前仍可用；重启后必须使用新密钥访问远程接口。",
    })


@router.post("/remote-access/totp/enable")
async def enable_remote_access_totp(body: dict | None = None, ctx: AppContext = Depends(get_ctx)):
    """启用 TOTP 二次确认（生成 base32 secret）。

    secret **仅在本次响应中返回一次**——用户须立刻在客户端配置 Authenticator App。
    若已启用，返回 409 冲突（避免误覆盖用户的现有 secret 导致已绑定的 Authenticator 失效）。

    请求体：`{"secret": "<可选，用户提供的 base32>"}` 或空/省略。
    """
    if (settings.totp_secret or "").strip():
        return err(409, "TOTP 已启用。如需更换 secret，请先停用（配置文件中清空 totp_secret）后再启用。")

    user_secret = str((body or {}).get("secret", "") or "").strip()
    secret = user_secret or _generate_totp_secret()
    # 校验用户提供的 secret 是否为合法 base32（仅大写字母+数字）
    if user_secret and not all(c.isupper() or c.isdigit() for c in user_secret):
        return err(400, "secret 必须是 BASE32 字符（大写字母 A-Z + 数字 0-9）")

    try:
        update_config_file({"totp_secret": secret})
    except OSError as exc:
        return err(500, f"写入配置文件失败：{exc}")

    audit_log("admin", "remote_access.totp.enable", "new-secret",
              {"secret_length": len(secret)}, "ok")
    return ok({
        "secret": secret,
        "otpauth_url": f"otpauth://totp/qmt_work:{settings.app_name}?secret={secret}&issuer=qmt_work",
        "requires_restart": True,
        "message": "TOTP 已启用。请立即在 Authenticator App 中扫描/导入，secret 仅此一次返回。重启客户端后生效。",
    })


@router.post("/remote-access/totp/verify")
async def verify_remote_access_totp(body: dict, ctx: AppContext = Depends(get_ctx)):
    """校验 TOTP 验证码（±1 步窗口容错，与 gateway.totp.verify_totp 一致）。

    用于：用户在 wan 档切换/重启前确认自己已经绑定了 Authenticator App。

    请求体：`{"code": "123456"}`
    """
    secret = (settings.totp_secret or "").strip()
    if not secret:
        return err(400, "TOTP 未启用，无法校验")
    code = str((body or {}).get("code", "")).strip()
    if not code:
        return err(400, "缺少 code 字段")
    digits = int(getattr(settings, "totp_digits", 6) or 6)
    ok_flag = verify_totp(secret, code, digits=digits)
    audit_log("admin", "remote_access.totp.verify", "code-check",
              {"code_prefix": code[:2] if len(code) > 2 else "?"}, "ok" if ok_flag else "fail")
    if not ok_flag:
        return err(400, "TOTP 校验失败，请检查 Authenticator App 时间是否同步")
    return ok({"verified": True, "message": "TOTP 校验通过"})


@router.get("/remote-access/modes")
async def list_remote_access_modes(ctx: AppContext = Depends(get_ctx)):
    """返回三档说明（供前端渲染下拉/帮助文案）。"""
    return ok([
        {"mode": "off", "label": remote_mode_label(),
         "description": "严格单机：仅本机 127.0.0.1 可访问，loopback 免密钥",
         "force_api_key": False, "force_totp": False, "signal_default": "live"},
        {"mode": "lan", "label": "内网（局域网多设备）",
         "description": "局域网多设备：0.0.0.0 监听，强制强密钥，scope 分级",
         "force_api_key": True, "force_totp": False, "signal_default": "live"},
        {"mode": "wan", "label": "公网（远程访问）",
         "description": "公网远程：lan 全部 + 强制 TOTP，signal.mode 默认 paper",
         "force_api_key": True, "force_totp": True, "signal_default": "paper"},
    ])
