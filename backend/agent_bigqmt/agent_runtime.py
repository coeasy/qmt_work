"""独立进程模式的**运行环境层**（从 ``BIGQMT_AGENT.py`` 按职责拆出）。

## 职责边界

QMT 的「模型交易 -> 运行」是把策略文件当**独立进程**拉起（实测参数形状
``argv = [<策略.py>, <userdata>, <时间戳>]``，解释器是 QMT 自带 Python 3.6.8），
不是公式模式的 in-process 挂载。这一模式需要一组与「交易上下文」无关的**环境
自举**工具：

- ``_qmt_root_from_userdata`` / ``_augment_sys_path`` -- 从 userdata 反推 QMT
  根目录并把内置库目录加进 ``sys.path``；
- ``_import_xtdata`` / ``_forward_xtdata_funcs`` -- 导入 ``xtquant.xtdata`` 并把
  其函数转发到策略命名空间（**这是独立进程模式唯一能拿到的能力：行情**）；
- ``_StandaloneContext`` -- 没有终端注入 ContextInfo 时的**诚实**替身；
- ``_is_process_main`` / ``should_autorun`` -- 自举与自动运行判定；
- ``_env_float`` / ``_consume_stop_file`` -- 环境变量读取与 STOP 文件消费。

## 为什么单独一个文件

``BIGQMT_AGENT.py`` 曾是 51.5KB 的上帝文件（策略入口 + 自检探针 + 请求执行 +
环境自举混在一起），超出本项目 50KB 源码上限
（``scripts/check_execution_architecture.py`` Gate 4）。本模块只放**不与**
``_STATE`` / ``_HERE`` / ``ContextInfo`` 交互的纯环境代码 -- 抽走后入口文件
留下的是真正的「策略入口 + 状态机」。

* **仍然是一个单文件策略**：QMT 只把 ``passorder`` 等注入被挂载那**一个**文件，
所以本模块只是**开发态**拆分；发布产物由 ``scripts/gen_qmt_agent_bundle.py``
把 ``qmt_api.py`` + ``agent_runtime.py`` + ``BIGQMT_AGENT.py`` **内联**成一个
``QMT_WORK_AGENT.py``，部署形态与从前完全一致。

py3.6 兼容：仅标准库，无 3.7+ 语法（由 ``scripts/check_bigqmt_agent_py36.py`` 守护）。
"""
import os
import sys
import time

def _qmt_root_from_userdata(userdata):
    """从 QMT 传进来的 userdata 目录反推 QMT 根目录（<root>/userdata）。"""
    if not userdata:
        return ""
    try:
        return os.path.dirname(os.path.abspath(userdata))
    except Exception:
        return ""


def _augment_sys_path(root):
    """把 QMT 自带解释器的库目录补进 sys.path（只追加，绝不抢占标准库位置）。

    返回实际补进去的目录列表（probe 里如实上报，不做「应该能找到」的假设）。
    """
    added = []
    if not root:
        return added
    candidates = [
        os.path.join(root, "bin.x64", "Lib", "site-packages"),
        os.path.join(root, "bin.x64", "Lib"),
        os.path.join(root, "python"),
    ]
    for d in candidates:
        try:
            if os.path.isdir(d) and d not in sys.path:
                sys.path.append(d)
                added.append(d)
        except Exception:
            pass
    return added


def _import_xtdata():
    """尽力导入 QMT 行情模块；失败返回 None（绝不伪造）。

    ★ `import xtdata` 在 QMT 内置解释器里是失败的（xtdata 是 xtquant 的子模块），
      必须用 `from xtquant import xtdata` —— 拿前者当判据会误判「行情不可用」。
    """
    try:
        from xtquant import xtdata as _xtd
        return _xtd
    except Exception:
        pass
    try:
        import xtdata as _xtd2  # 某些发行版把 xtdata 直接暴露在顶层
        return _xtd2
    except Exception:
        return None


def _forward_xtdata_funcs(ns, xtdata):
    """把 xtdata 的**全部**公开可调用接口转发进入口命名空间。

    ★ 为什么是「全部」而不是挑几个：本仓铁律是**禁止手抄名单**（CI 的 G3 闸门
      就是拦这个）。手抄一份「行情函数应该有哪些」的清单必然随版本漂移，漏掉的
      那个会被误报成「终端没有该接口」。这里直接问模块本身（``dir(xtdata)``）。

    返回 ``{name: fn}``（名字排序写入的普通 dict）。调用方需把它与
    ``capture_qmt_injected_funcs`` 的 ``exclude`` 配套使用 —— 这些是**我们转发**的，
    不是终端注入的，混为一谈会同时造成「关键警告被吞」与「能力面假绿灯」。
    """
    forwarded = {}
    if xtdata is None:
        return forwarded
    try:
        names = dir(xtdata)
    except Exception:
        return forwarded
    for name in names:
        if name.startswith("_"):
            continue
        if name in ns:
            continue
        try:
            fn = getattr(xtdata, name)
        except Exception:
            continue
        if callable(fn):
            ns[name] = fn
            forwarded[name] = fn
    return dict(sorted(forwarded.items()))


class _StandaloneContext(object):
    """独立进程模式下的 ContextInfo 替身。

    ★ 诚实边界：所有属性**转发到真实 xtdata**；导入失败/没有该方法就不提供
      （``Executor`` 用 ``getattr(ctx, name, None)`` 取，拿不到会如实报「未捕获」）。
      绝不造一个返回编造行情的假 getter —— 那是零 mock 契约的红线。
    """

    def __init__(self, xtdata, root):
        self._xtdata = xtdata
        self._root = root
        self.barpos = 0
        self.period = "1d"
        self.dividend_type = "none"

    def __getattr__(self, name):
        xt = self.__dict__.get("_xtdata")
        if xt is not None:
            try:
                fn = getattr(xt, name)
            except Exception:
                fn = None
            if callable(fn):
                return fn
        raise AttributeError(name)

    def __dir__(self):
        """唯一来源纪律：方法面直接问对象，不手抄「ContextInfo 应该有哪些方法」。"""
        names = ["barpos", "period", "dividend_type"]
        xt = self.__dict__.get("_xtdata")
        if xt is not None:
            try:
                names += [n for n in dir(xt) if not n.startswith("_")]
            except Exception:
                pass
        return sorted(set(names))


def _is_process_main():
    """本文件是否**真的是**被当作主程序执行的（而不是被 exec 进别人的命名空间）。

    QMT 公式模式把源码 exec 进终端自己的命名空间，此时 ``sys.modules['__main__']``
    是终端主模块、其 ``__dict__`` 与本文件的 ``globals()`` **不是同一个对象**。
    —— 这条判别保证「独立进程自举」绝不会在公式模式下把终端线程卡死。
    """
    try:
        main_mod = sys.modules.get("__main__")
        return main_mod is not None and getattr(main_mod, "__dict__", None) is globals()
    except Exception:
        return False


def should_autorun(argv=None):
    """是否应进入独立进程自举。三条同时成立才启动主循环：

      ① ``__name__ == "__main__"``（由调用点保证，这里再核一遍）
      ② ``sys.modules['__main__'].__dict__ is globals()``（真的是主程序）
      ③ ``argv[0]`` 指向一个 .py 文件（QMT：``pythonw.exe -u <策略.py> ...``）

    这样：
      * 公式模式（终端 exec 源码）→ ② 为假 ⇒ 只定义入口函数，不动终端线程；
      * 独立进程模式 / 开发机 ``python QMT_WORK_AGENT.py`` → 三条全真 ⇒ 自举；
      * 被当模块 import（测试、IDE 补全）→ ① 为假 ⇒ 不自举。

    需要在不启动主循环的前提下执行本文件时，置 ``QMT_WORK_AGENT_NO_AUTORUN=1``。
    """
    if __name__ != "__main__":
        return False
    raw = os.environ.get("QMT_WORK_AGENT_NO_AUTORUN", "")
    if raw not in ("", "0", "false", "False", "no"):
        return False
    if not _is_process_main():
        return False
    argv = list(argv if argv is not None else (getattr(sys, "argv", None) or []))
    if not argv:
        return False
    try:
        return os.path.splitext(argv[0] or "")[1].lower() == ".py"
    except Exception:
        return False


def _env_float(name, default):
    try:
        raw = os.environ.get(name)
        if raw is None or raw == "":
            return default
        return float(raw)
    except Exception:
        return default


def _consume_stop_file(path):
    """消费 STOP 文件：**改名**而非删除 —— 留物证，且让下次启动不被立即杀死。

    ★ 为什么必须消费（2026-10-12 真机教训）：STOP 留在原处时，QMT 每次拉起策略都会
      在**第一轮**就退出（表现即「启动即停止」）；而 QMT **不会**自动重启 graceful
      退出的策略 —— 桥就这样静默变黑，运维在诊断里只看得到一句含糊的 ``exited``，
      既不知道是 STOP 干的、也不知道下一步该做什么。改名后：① 下次启动正常；
      ② ``STOP.consumed.<ts>`` 就是「确实被 STOP 停过」的物证。
    """
    try:
        os.rename(path, path + ".consumed.%d" % int(time.time()))
        return True
    except Exception:
        try:
            os.remove(path)
            return True
        except Exception:
            return False
