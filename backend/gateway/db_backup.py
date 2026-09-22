"""SQLite 自动备份（防单点损坏）：启动时 + 周期性 + 关闭前各一次。

- 备份目标：<db 同目录>/backups/app.YYYYMMDD_HHMMSS.db（连同 -wal/-shm 一并复制，
  保证 WAL 模式下备份点一致）。
- 传入 `db`（app.db.DB 实例）时改用 **sqlite3 backup API**：在事务层面拷贝出
  单一一致性文件（含 WAL 已提交数据），可直接单独还原，不再依赖 -wal/-shm。
- 保留策略：**份数上限 `keep`** + **总体积上限 `max_total_mb`**，取两者更严的那个，
  但**至少保留 `min_keep` 份**（默认 2）—— 见下。
- 由 app.main 生命周期驱动：启动备份一次、后台周期任务、关闭前再备份一次。

## 为什么必须同时有「体积」上限（2026-09-20 实测修复）

原来只有 `keep=10`。听起来很保守，实际是**灾难**：备份是**整库全量复制**，
而本机主库有 1.14 GB（分钟线 + 成交 + 审计长期累积）。于是

    「保留最近 10 份」 == 11.4 GB 常驻磁盘

实测用户数据目录 `backups/` 就是 **11 GB**（C: 盘 321 GB 仅剩 47 GB / 已用 86%），
而界面上**看不到任何一处**提到它 —— 用户只能等磁盘满了才发现。磁盘满在本项目
不是理论风险：它会让测试假失败、同步写库失败、客户端行为诡异。
所以保留策略必须按**字节**封顶，而不是按份数。

## 为什么「没变化就跳过」

周期任务是每小时一次、无条件全量复制 1.14 GB。客户端开着一天 = 24 次 = 27 GB
写入量（还不含启动/关闭各一次）。绝大多数时刻主库根本没变（收盘后到次日开盘
之间几乎静止）。因此：**源库指纹（db 与 -wal 的 size+mtime_ns）与上次成功备份
完全一致时跳过**。

- 跳过是**安全**的：只有「上一次备份成功」才会记录指纹，指纹相同即意味着
  磁盘上那份备份已经精确覆盖了当前状态，跳过不会丢任何数据。
- 指纹记录在 `backups/.last_backup.json`（连带记下备份文件名），
  **跨进程有效**（客户端反复启停也不会重复整库复制）；若那份备份已被保留策略
  删除，则不再跳过（记录的文件不存在 ⇒ 覆盖关系失效）。
- 与 `list_backups()` 的 `app.*.db` 通配不冲突（点号开头、非 .db）。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import sqlite3
import time
from datetime import datetime
from pathlib import Path

from core.clock import now_iso, to_iso

log = logging.getLogger("qmt_work.db_backup")

#: 指纹旁挂文件（放在 backups/ 里，点号开头、非 .db ⇒ 不会被 list_backups 当备份）
_FP_FILE = ".last_backup.json"

#: 主库目录里**属于主库自身**的文件名（其余同名派生文件视为「未纳管的库副本」）
_MANAGED_DB_NAMES = {"app.db", "app.db-wal", "app.db-shm"}


def human_size(n: int) -> str:
    """字节 → 人类可读（用于日志与界面文案，两端同一套口径）。"""
    try:
        n = int(n)
    except (TypeError, ValueError):
        return "?"
    if n <= 0:
        return "0B"
    for unit, div in (("GB", 1024 ** 3), ("MB", 1024 ** 2), ("KB", 1024)):
        if n >= div:
            return f"{n / div:.1f}{unit}"
    return f"{n}B"


def stray_db_files(db_path: Path) -> list[dict]:
    """主库目录里**不在保留策略内**的库副本（只读上报，绝不删）。

    典型：手工 `cp app.db app.db.bak_bardate` 留下的 603 MB 死重量 —— 它不叫
    `app.*.db`、不在 `backups/`，因此**既不被备份策略统计、也不被清理**，
    界面上同样一处都不显示。这类文件必须让用户看见占了多少，否则「磁盘去哪了」
    永远查不出来。删不删由用户决定（可能在别处仍有用途）。
    """
    parent = Path(db_path).parent
    prefix = Path(db_path).name
    try:
        entries = list(parent.iterdir())
    except OSError:
        return []
    out: list[dict] = []
    for p in entries:
        try:
            if not p.is_file():
                continue
            name = p.name
            if name in _MANAGED_DB_NAMES or not name.startswith(prefix):
                continue
            st = p.stat()
            out.append({
                "name": name,
                "size": int(st.st_size),
                "size_str": human_size(st.st_size),
                "mtime": to_iso(datetime.fromtimestamp(st.st_mtime)),
            })
        except OSError:
            continue
    out.sort(key=lambda d: d["size"], reverse=True)
    return out


class DBBackup:
    def __init__(self, db_path: Path, keep: int = 10,
                 backups_dir: Path | None = None, interval: float = 3600.0,
                 db=None, max_total_mb: float = 4096.0, min_keep: int = 2):
        self.db_path = Path(db_path)
        self.keep = max(1, int(keep))
        #: 无论体积上限多小，至少保留这么多份：否则「1.14 GB 的库 + 1 GB 预算」
        #: 会把备份全删光 —— 等于静默关掉了保护，比不设上限更危险。
        self.min_keep = max(1, min(int(min_keep), self.keep))
        self.backups_dir = Path(backups_dir) if backups_dir else (self.db_path.parent / "backups")
        self.interval = max(60.0, float(interval))
        self.db = db  # 阶段 3：可选 app.db.DB 实例，提供 backup API 一致性备份
        try:
            mb = float(max_total_mb)
        except (TypeError, ValueError):
            mb = 0.0
        #: 0 或负数 = 不设体积上限（只按份数）——保留给「我就想要 10 份全量」的用户
        self.max_total_bytes = max(0, int(mb * 1024 * 1024)) if mb > 0 else 0
        self._task: asyncio.Task | None = None
        self._stop = False
        #: 最近一次 backup_once 的**真实结果**：created / skipped / failed / idle。
        #: 返回值是 `str | None`（None 既可能是「跳过」也可能是「失败」），
        #: 调用方要区分必须读这里 —— 把「跳过」当成「失败」会误报磁盘故障。
        self.last_status: dict = {"action": "idle"}

    # ---------------- 内部小工具 ----------------
    @staticmethod
    def _size_of(p: Path) -> int:
        try:
            return int(p.stat().st_size)
        except OSError:
            return 0

    def _size_str(self, p: Path) -> str:
        return human_size(self._size_of(p))

    def _files(self) -> list[Path]:
        """现有备份，**新 → 旧**排序。

        ★★ 排序键必须是**文件名**，不能是 mtime（2026-09-20 实测修复，这是个会
        **删错备份**的真缺陷，不是测试 flake）。

        为什么 mtime 不可靠：
        ① 回退复制路径用 ``shutil.copy2``，它会把**主库的 mtime 复制给备份**
           ⇒ 备份文件的 mtime 记的是「主库最后被写的时间」，不是「备份生成的时间」；
        ② Windows/NTFS 下短间隔连续写同一个文件，mtime 可能**完全相同**
           （实测三个备份 mtime_ns 都是 ``1789918599694760700``）。

        后果：``sort`` 在键相等时是**稳定排序**，顺序退化为 ``glob`` 的目录顺序
        ⇒ 「哪个是最旧的那份」变成随机。而 ``_plan`` 就是按这个顺序决定删谁的：
        实测同一轮里 round1 删掉了 2000（该删 1000），round4 甚至删掉了**最新那份**
        3000 —— 保留策略本该保住最新的还原点，实际却把它删了。

        文件名则天然有序：``app.<%Y%m%d_%H%M%S>.db`` 按字典序即时间序，
        同秒碰撞由 ``_unique_name`` 追加**补零**序号（``_002``），字典序仍然正确。
        """
        try:
            files = [p for p in self.backups_dir.glob("app.*.db") if p.is_file()]
        except OSError:
            return []
        # 名称即顺序（确定性，不依赖文件系统时间戳精度）
        files.sort(key=lambda p: p.name, reverse=True)
        return files

    def _wal_path(self) -> Path:
        return Path(str(self.db_path) + "-wal")

    def _normalize_wal(self) -> bool:
        """把 ``-wal`` 合并回主库（best-effort），返回「是否**确实**归一化成功」。

        ★★ 为什么指纹必须先归一化（2026-09-21 实测，A5）：
        SQLite 在 WAL 模式下把已提交事务先写进 ``-wal``，之后某个时刻（自动检查点、
        ``db.close()``、手动触发）才并回主库。**同一个逻辑状态**因此有多种文件形态：

            备份时     主库 4096B / -wal 45352B
            检查点后   主库 36864B / -wal 0B      ← 逻辑数据一字未改

        指纹若按文件形态取，这两种形态就不相等 ⇒ ``_covered_by()`` 判定「源已变化」
        ⇒ 白复制一份整库（实测库越大越贵，用户数据目录曾是 1.14GB）。实测复现：
        一次纯检查点之后，第二次「立即备份」拿到的是 ``created`` 而不是 ``skipped``。

        归一化后 ``-wal`` 归零 ⇒ 全部已提交数据都在主库里 ⇒ **主库的 (size, mtime_ns)
        就足以代表逻辑内容**，与「数据曾在 WAL 里待过多久」无关。

        ⚠️ 失败必须**降级**，不能抛：有活跃读者持有旧快照时检查点会 busy（实测），
        此时 ``_source_fingerprint()`` 退回今天的文件形态口径 —— 宁可多备份，
        绝不漏备份。
        """
        sql = "PRAGMA wal_checkpoint(TRUNCATE)"
        try:
            if self.db is not None and hasattr(self.db, "execute"):
                # 用应用自己的写连接：它就是写入方，不会与自己的写锁相争
                row = self.db.execute(sql).fetchone()
            else:
                conn = sqlite3.connect(str(self.db_path), timeout=2.0)
                try:
                    row = conn.execute(sql).fetchone()
                finally:
                    conn.close()
        except Exception as exc:  # noqa: BLE001 归一化失败只影响口径，不该影响备份
            log.debug("备份指纹：WAL 归一化失败，退回文件形态指纹：%s", exc)
            return False
        busy = int(row[0]) if row else 1
        if busy != 0:
            return False
        try:
            return self._wal_path().stat().st_size == 0
        except OSError:
            return True  # -wal 不存在 = 已被清掉，等价于归零

    def _source_fingerprint(self) -> dict:
        """主库当前指纹。

        归一化**成功**时 = 主库 ``(size, mtime_ns)``（``wal`` 只留 ``[0]`` 作痕迹）；
        归一化**失败**时 = 主库与 ``-wal`` 各自的 ``(size, mtime_ns)``（旧口径）。

        mtime 用 ns 精度：秒级精度下「同一秒内写库 + 备份」会被误判成未变化，
        从而漏掉一次真备份。

        ★★ 归一化成功后**只认主库**（2026-09-21，A5）：``_normalize_wal()`` 已把
        ``-wal`` 并回主库并归零，此时主库即完整逻辑内容。为什么不连 ``-wal`` 的
        mtime 一起记：**空操作检查点也会更新 ``-wal`` 的 mtime**（实测 size 恒为 0，
        mtime 每次都变）⇒ 记了它就会「每次判断都算变化」，走向反面：永远跳过不了、
        每次点击白复制一份整库。归一化失败时保留 ``-wal`` 的 (size, mtime)，
        等价于修复前的保守口径。

        ⚠️⚠️ **本指纹单独使用是不完备的**（2026-09-21，A11）：主库 ``(size, mtime_ns)``
        在「原地 UPDATE + 与上次备份同 tick」时会**完全不变** ⇒ 静默丢备份。
        所以「是否已被覆盖」的判定**不能只看本方法**，必须由 ``_covered_by()``
        再叠加一个与文件时间无关的信号（归一化**前** ``-wal`` 是否有帧）。详见其 docstring。
        """
        normalized = self._normalize_wal()
        fp: dict = {}
        for tag, suffix in (("db", ""), ("wal", "-wal")):
            p = Path(str(self.db_path) + suffix)
            try:
                st = p.stat()
                fp[tag] = [int(st.st_size), int(st.st_mtime_ns)]
            except OSError:
                fp[tag] = None
        if normalized:
            # -wal 已归零；只留 size（恒 0）作「确实归一化过」的痕迹，剔除 mtime
            fp["wal"] = [0]
        return fp

    def _fp_file(self) -> Path:
        return self.backups_dir / _FP_FILE

    def _unique_name(self, stem: str) -> Path:
        """给备份挑一个**不重名**的文件名。

        ★ 文件名只到「秒」，而备份是可以在同一秒内触发多次的（启动 + 手动 +
        关闭；实测两次调用落在同一秒）。直接写同名文件 = **静默覆盖掉上一份备份**，
        等于悄悄少了一个还原点 —— 而且因为 `_covered_by` 只认最新那份，
        覆盖后连「跳过判断」都看不出异常。故重名时追加序号。
        """
        p = self.backups_dir / f"{stem}.db"
        if not p.exists():
            return p
        # ★ 序号**必须补零**：``_10`` 在字典序上小于 ``_2``，不补零的话同秒内
        #   第 10 份会被排到第 2 份前面 —— 而 ``_files()`` 正是按名字排「新 → 旧」。
        for i in range(2, 1000):
            cand = self.backups_dir / f"{stem}_{i:03d}.db"
            if not cand.exists():
                return cand
        return self.backups_dir / f"{stem}_{time.time_ns()}.db"

    def _read_fp(self) -> tuple[dict | None, str]:
        try:
            raw = json.loads(self._fp_file().read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, ValueError):
            return None, ""
        if not isinstance(raw, dict):
            return None, ""
        fp = raw.get("fingerprint")
        return (fp if isinstance(fp, dict) else None), str(raw.get("backup") or "")

    def _write_fp(self, fp: dict, backup_name: str) -> None:
        try:
            self._fp_file().write_text(
                json.dumps({"fingerprint": fp, "backup": backup_name,
                            "at": now_iso()}, ensure_ascii=False),
                encoding="utf-8")
        except OSError as exc:
            # 写不进去只影响「下次能否跳过」，不影响备份本身 ⇒ 不抛
            log.warning("备份指纹写入失败（仅影响重复备份的跳过判断）：%s", exc)

    def _wal_size(self) -> int:
        """``-wal`` 当前字节数（**纯读，不做任何归一化**）。

        ⚠️ 必须在 ``_normalize_wal()`` **之前**调用：归一化会把 ``-wal`` 归零，
        读在它后面就等于什么都没读到（A11 的关键，写反了探针会给出假阴性）。
        """
        try:
            return int(self._wal_path().stat().st_size)
        except OSError:
            return 0  # -wal 不存在 = 没有待并回的已提交数据

    def _covered_by(self) -> str:
        """当前主库状态是否已被某份**现存**备份覆盖；是则返回该备份文件名。

        ★★ 为什么要额外看「归一化**之前**的 ``-wal``」（2026-09-21，A11）：
        ``_source_fingerprint()`` 归一化成功后只认主库 ``(size, mtime_ns)``。
        但 Windows 的**文件时间按系统 tick 更新** —— 实测 200 次连续追加写里
        **188 次 mtime 根本没推进**（同一 tick 内的写不更新文件时间）。于是：

            原地 UPDATE / 小改动 ⇒ 主库 size 不变；
            若又落在与上次备份**同一个 tick** ⇒ mtime 也不变；
            ⇒ 指纹与上次备份完全相同 ⇒ 判定「已覆盖」⇒ **静默跳过备份**。

        风险方向是最坏的那种：**静默丢备份（不可恢复）**。实测复现率极高 ——
        不加任何 sleep 连跑 6 轮，**4 轮**出现「真写入却未触发备份」；
        每轮之间加 20ms sleep 则 3 轮全过（⇒ 根因就是时间精度，不是逻辑错）。

        补上的信号**与文件时间无关**：``_normalize_wal()`` 会把已提交数据并回主库
        并把 ``-wal`` 归零，所以「**归一化前** ``-wal`` > 0」⇔「自上次归一化以来
        确有提交」。空操作检查点不会留下帧 ⇒ 不会误报（A5 的跳过语义完好）。
        """
        old, name = self._read_fp()
        if not old or not name:
            return ""
        pending = self._wal_size()          # ★ 必须早于下面的归一化，否则恒为 0
        cur = self._source_fingerprint()
        if pending > 0 and cur.get("wal") == [0]:
            # 归一化成功（-wal 已归零）且归一化前还有帧 ⇒ 一定有新提交。
            # 归一化**失败**时 cur 是降级口径（含 -wal 的 size/mtime），
            # 那种情况交给下面的 != 比较处理即可，不必在这里下结论。
            return ""
        if cur != old:
            return ""
        if not (self.backups_dir / name).is_file():
            return ""  # 记录的那份已被清理 ⇒ 覆盖关系失效，必须重新备份
        return name

    # ---------------- 保留策略 ----------------
    def _plan(self, files: list[Path]) -> tuple[list[Path], list[Path]]:
        """按「份数 + 体积」双上限算出 (保留, 待删)，新 → 旧。"""
        survivors: list[Path] = []
        doomed: list[Path] = []
        running = 0
        budget = self.max_total_bytes
        for i, f in enumerate(files):
            sz = self._size_of(f)
            within_count = i < self.keep
            within_budget = budget <= 0 or (running + sz) <= budget
            if (within_count and within_budget) or i < self.min_keep:
                survivors.append(f)
                running += sz
            else:
                doomed.append(f)
        return survivors, doomed

    def _unlink(self, p: Path) -> bool:
        ok_any = False
        for suffix in ("", "-wal", "-shm"):
            t = Path(str(p) + suffix)
            if t.exists():
                try:
                    t.unlink()
                    ok_any = True
                except OSError as exc:
                    log.warning("清理旧备份失败（%s）：%s", t.name, exc)
        return ok_any

    def _prune(self) -> int:
        """执行保留策略，返回释放的字节数。"""
        _, doomed = self._plan(self._files())
        freed = 0
        for f in doomed:
            sz = self._size_of(f)
            if self._unlink(f):
                freed += sz
        return freed

    # ---------------- 备份操作 ----------------
    def backup_once(self, reason: str = "manual", after_create=None) -> str | None:
        """执行一次备份，返回目标路径。

        ⚠️ 返回 ``None`` 有**两种**含义：跳过（主库未变化）或失败。
        调用方需要区分时读 ``self.last_status["action"]``。

        ``after_create``：文件落盘后、**记录指纹之前**调用的回调（参数是备份路径）。
        ★ 存在的理由：调用方常常要在备份成功后写一条审计行，而审计行**本身就是写库**
        —— 若指纹在它之前记录，立刻就会失效，于是「主库没变就跳过」永远不成立，
        用户连点两次「立即备份」必然白复制两份整库（实测 1.14GB × 2）。
        回调异常只记日志，不影响备份结果。
        """
        if not self.db_path.exists():
            self.last_status = {"action": "failed", "reason": reason,
                                "detail": f"主库文件不存在：{self.db_path}"}
            return None
        covered = self._covered_by()
        if covered:
            self.last_status = {
                "action": "skipped", "reason": reason,
                "detail": f"主库自 {covered} 以来未发生变化，已有备份覆盖当前状态",
            }
            log.info("数据库备份跳过（%s）：%s", reason, self.last_status["detail"])
            return None
        try:
            self.backups_dir.mkdir(parents=True, exist_ok=True)
            ts = time.strftime("%Y%m%d_%H%M%S")
            dst = self._unique_name(f"app.{ts}")
            if self.db is not None and hasattr(self.db, "backup_to"):
                # 阶段 3：backup API 生成单一一致性文件（无需 -wal/-shm）
                if not self.db.backup_to(dst):
                    raise RuntimeError("sqlite backup API 返回失败")
            else:
                # 回退：逐文件 copy 主库 + -wal/-shm
                for suffix in ("", "-wal", "-shm"):
                    src = Path(str(self.db_path) + suffix)
                    if src.exists():
                        shutil.copy2(src, str(dst) + suffix)
                # ★ ``copy2`` 会把**主库的 mtime** 复制过来，于是备份文件的
                #   mtime 记的是「主库最后写入时间」而不是「备份生成时间」
                #   —— 界面上「备份时间」显示的是错的。这里改回落盘时刻。
                #   （``stats()`` / ``list_backups()`` 展示的 mtime 源就在这里。）
                try:
                    os.utime(dst, None)
                except OSError:  # noqa: BLE001  改不了时间也不该让备份失败
                    pass
            # ★ 先让调用方做完副作用（审计行等），**再**采指纹
            if after_create is not None:
                try:
                    after_create(dst)
                except Exception as exc:  # noqa: BLE001 回调失败不影响备份本身
                    log.warning("备份回调失败（不影响备份结果）：%s", exc)
            self._write_fp(self._source_fingerprint(), dst.name)
            freed = self._prune()
            size = self._size_of(dst)
            self.last_status = {
                "action": "created", "reason": reason, "path": str(dst),
                "name": dst.name, "bytes": size, "pruned_bytes": freed,
                "detail": f"备份完成 {dst.name} [{human_size(size)}]"
                          + (f"，顺带清理旧备份释放 {human_size(freed)}" if freed else ""),
            }
            log.info("数据库备份完成（%s）：%s [%s]%s", reason, dst.name,
                     human_size(size),
                     f"（清理旧备份释放 {human_size(freed)}）" if freed else "")
            return str(dst)
        except Exception as exc:  # noqa: BLE001
            self.last_status = {"action": "failed", "reason": reason, "detail": str(exc)}
            log.warning("数据库备份失败（%s）：%s", reason, exc)
            return None

    def list_backups(self) -> list[dict]:
        """返回现有备份清单（含大小/时间），新 → 旧。"""
        if not self.backups_dir.exists():
            return []
        out = []
        for f in self._files():
            try:
                st = f.stat()
                out.append({
                    "name": f.name, "size": st.st_size,
                    "size_str": human_size(st.st_size),
                    "mtime": to_iso(datetime.fromtimestamp(st.st_mtime)),
                })
            except OSError:
                continue
        return out

    def stats(self) -> dict:
        """备份占用与保留策略的**完整快照**，供运维界面直接展示。

        ★ 存在的意义：原来 ``list_backups()`` 一个调用方都没有 ⇒ 11 GB 占用在
        界面上完全不可见。占用必须可见，否则「磁盘去哪了」无从回答。
        """
        files = self._files()
        survivors, doomed = self._plan(files)
        total = sum(self._size_of(f) for f in files)
        reclaimable = sum(self._size_of(f) for f in doomed)
        newest = files[0] if files else None
        oldest = files[-1] if files else None
        # ★ 「未纳管副本」的总量也由后端算：前端只做格式化，不做业务算术
        strays = stray_db_files(self.db_path)
        stray_total = sum(int(x.get("size") or 0) for x in strays)

        def _info(p: Path | None) -> dict | None:
            if p is None:
                return None
            try:
                st = p.stat()
            except OSError:
                return None
            return {"name": p.name, "size": int(st.st_size),
                    "size_str": human_size(st.st_size),
                    "mtime": to_iso(datetime.fromtimestamp(st.st_mtime))}

        return {
            "dir": str(self.backups_dir),
            "count": len(files),
            "total_bytes": total,
            "total_size": human_size(total),
            "keep": self.keep,
            "min_keep": self.min_keep,
            "max_total_bytes": self.max_total_bytes,
            "max_total_size": human_size(self.max_total_bytes) if self.max_total_bytes else "",
            "over_count": len(files) > self.keep,
            "over_budget": bool(self.max_total_bytes and total > self.max_total_bytes),
            "retain_count": len(survivors),
            #: 按当前策略现在就能回收多少（>0 ⇒ 界面应给出「清理」入口）
            "reclaimable_bytes": reclaimable,
            "reclaimable_size": human_size(reclaimable),
            "newest": _info(newest),
            "oldest": _info(oldest),
            "interval": self.interval,
            #: 主库目录里不受策略管理的库副本（如 app.db.bak_bardate）
            "strays": strays,
            "stray_count": len(strays),
            "stray_total_bytes": stray_total,
            "stray_total_size": human_size(stray_total),
            "last": dict(self.last_status),
        }

    def prune_now(self) -> dict:
        """按当前策略立即清理，返回清理结果 + 清理后的最新快照。"""
        files = self._files()
        _, doomed = self._plan(files)
        removed: list[str] = []
        freed = 0
        for f in doomed:
            sz = self._size_of(f)
            if self._unlink(f):
                freed += sz
                removed.append(f.name)
        return {
            "removed": removed,
            "removed_count": len(removed),
            "freed_bytes": freed,
            "freed_size": human_size(freed),
            "note": ("已按保留策略清理（至少保留 %d 份，不超过 %s）"
                     % (self.min_keep, human_size(self.max_total_bytes))
                     if self.max_total_bytes else
                     "已按保留策略清理（至少保留 %d 份，不超过 %d 份）"
                     % (self.min_keep, self.keep)),
            "stats": self.stats(),
        }

    # ---------------- 周期任务 ----------------
    async def start(self) -> None:
        """启动后台周期备份任务（事件循环内）。"""
        self._stop = False
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop())

    async def _loop(self) -> None:
        try:
            while not self._stop:
                await asyncio.sleep(self.interval)
                if self._stop:
                    break
                # ★ 一致性复制是**同步重活**（1GB+ 主库要好几秒）：必须丢线程，
                #   否则周期任务一到点，整个事件循环（WS 广播 + 全部 HTTP）跟着卡住。
                await asyncio.to_thread(self.backup_once, "periodic")
        except asyncio.CancelledError:
            pass
        except Exception as exc:  # noqa: BLE001
            log.warning("db backup loop error: %s", exc)

    async def stop(self) -> None:
        """停止周期任务，并在关闭前再备份一次。"""
        self._stop = True
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None
        await asyncio.to_thread(self.backup_once, "shutdown")
