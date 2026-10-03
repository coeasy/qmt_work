"""远程访问三档模型可证伪性验证（Phase 1 安全守卫）。

守卫意图：证明 `core/config.py` + `run.py::_self_check` 的分档安全约束**确实有效**——
不是「写了代码但没生效」的假绿灯。

方法：对每档环境（remote_access × api_key × totp_secret × host）直接调用
``run._self_check()`` 断言其 ``sys.exit(1)``（拒绝）或正常返回（放行）——不启动
uvicorn、不占用端口、不申请单实例锁，可在 CI / 打包流水线里安全重跑。

★ 常驻守卫：改动 `core/config.py`、`run.py::_self_check`、`normalize_remote_access`
  或 `effective_host` 后必须重跑本脚本。若自检写松（例如去掉 lan 档的默认密钥检查），
  本脚本会转红——说明护栏已失效。

用法：backend/runtimes/cp311/python.exe -u scripts/verify_remote_access_safety.py
"""
from __future__ import annotations

import os
import subprocess
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
PY = BACKEND / "runtimes" / "cp311" / "python.exe"
if not PY.exists():
    PY = Path(sys.executable)


# ---- 守卫用例 ----
# 每条：说明 / 期望 rc / 需要注入的环境变量
#   QMT_REMOTE_ACCESS = off|lan|wan
#   QMT_API_KEY       = 空 / "qmt-dev-key" / 非默认强密钥
#   QMT_TOTP_SECRET   = 空 / 20+ 字符 TOTP 基字符串
#   QMT_HOST          = 127.0.0.1 / 0.0.0.0
CASES: list[tuple[str, int, dict[str, str]]] = [
    (
        "G1-A: lan 档 + 默认密钥 → 必须拒绝启动（内网可达 + 弱密钥 = 高危）",
        1,
        {"QMT_REMOTE_ACCESS": "lan", "QMT_API_KEY": "qmt-dev-key",
         "QMT_TOTP_SECRET": "", "QMT_HOST": "0.0.0.0"},
    ),
    (
        "G1-B: wan 档 + 强密钥 + 无 TOTP → 必须拒绝启动（公网无二次确认）",
        1,
        {"QMT_REMOTE_ACCESS": "wan", "QMT_API_KEY": "my-strong-key",
         "QMT_TOTP_SECRET": "", "QMT_HOST": "0.0.0.0"},
    ),
    (
        "G1-C: wan 档 + 强密钥 + TOTP + 0.0.0.0 → 必须放行",
        0,
        {"QMT_REMOTE_ACCESS": "wan", "QMT_API_KEY": "my-strong-key",
         "QMT_TOTP_SECRET": "JBSWY3DPEHPK3PXP", "QMT_HOST": "0.0.0.0"},
    ),
    (
        "G1-D: off 档 + 默认密钥 + 127.0.0.1 → 必须放行（严格单机，向后兼容）",
        0,
        {"QMT_REMOTE_ACCESS": "off", "QMT_API_KEY": "qmt-dev-key",
         "QMT_TOTP_SECRET": "", "QMT_HOST": "127.0.0.1"},
    ),
    (
        "G1-E: off 档 + 默认密钥 + 0.0.0.0 → 必须拒绝启动（用户手动改远程监听）",
        1,
        {"QMT_REMOTE_ACCESS": "off", "QMT_API_KEY": "qmt-dev-key",
         "QMT_TOTP_SECRET": "", "QMT_HOST": "0.0.0.0"},
    ),
    (
        "G1-F: wan 档 + 空 api_key → 必须拒绝启动（无密钥等同零鉴权）",
        1,
        {"QMT_REMOTE_ACCESS": "wan", "QMT_API_KEY": "",
         "QMT_TOTP_SECRET": "JBSWY3DPEHPK3PXP", "QMT_HOST": "0.0.0.0"},
    ),
]


def _env_for(overrides: dict[str, str]) -> dict[str, str]:
    """构造子进程环境：保留 PATH 等必备项，只覆盖 QMT_* 与备份开关。"""
    env = dict(os.environ)
    env["QMT_DB_BACKUP_ENABLED"] = "0"
    env.update(overrides)
    return env


def _run_case(overrides: dict[str, str]) -> tuple[int, str]:
    """在独立子进程里调用 ``run._self_check()``，返回 (rc, tail)。

    独立子进程而非主进程 import 是关键：
    - ``core.config.settings`` 在 import 时就读了环境变量，必须进程隔离才能注入新档；
    - ``_self_check`` 内部 ``sys.exit(1)`` 会被 subprocess returncode 捕获，主进程不中断。
    """
    code = (
        "import sys; sys.path.insert(0, r'%s');"
        "from app.logging_setup import setup_logging; setup_logging();"
        "import run; run._self_check();"
        "print('[SELF_CHECK_OK]')"
    ) % str(BACKEND)
    r = subprocess.run(
        [str(PY), "-u", "-c", code],
        capture_output=True, text=True,
        cwd=str(BACKEND),
        env=_env_for(overrides),
        timeout=30,
    )
    out = (r.stdout or "") + (r.stderr or "")
    return r.returncode, out


def main() -> int:
    print("=" * 64)
    print("远程访问三档模型 · 可证伪性验证")
    print("=" * 64)
    print(f"python: {PY}")
    print()

    failures = 0
    for i, (desc, expected_rc, env_override) in enumerate(CASES, 1):
        print(f"[{i}/{len(CASES)}] {desc}")
        try:
            rc, out = _run_case(env_override)
        except subprocess.TimeoutExpired:
            print(f"  ✗ TIMEOUT (expected rc={expected_rc})")
            failures += 1
            continue
        except Exception as exc:  # noqa: BLE001
            print(f"  ✗ EXCEPTION: {exc}")
            print(traceback.format_exc())
            failures += 1
            continue

        marker = "PASS" if rc == expected_rc else "FAIL"
        sign = "✓" if marker == "PASS" else "✗"
        print(f"  {sign} {marker} (rc={rc}, expected={expected_rc})")
        if marker == "FAIL":
            # 只打印含关键字的 5 行，避免日志噪声
            keys = ("自检：", "WARNING", "ERROR", "Traceback", "assert")
            picks = [ln for ln in out.splitlines() if any(k in ln for k in keys)]
            for ln in picks[-5:]:
                print(f"    | {ln}")
            failures += 1

    print()
    print("=" * 64)
    if failures == 0:
        print(f"全部 {len(CASES)} 个守卫通过 ✓")
        return 0
    print(f"失败 {failures}/{len(CASES)} 个守卫 ✗")
    return 1


if __name__ == "__main__":
    sys.exit(main())
