import { useEffect, useRef, useState } from "react";
import { MENU, PAGES, pageLabel, visibleMenuItems } from "@/app/routes";
import { ConfirmModal } from "@/design/primitives";
import { useWorkspaceStore } from "@/stores/workspace";
import { useOpenWorkbench } from "@/hooks/useOpenWorkbench";
import { marketApi } from "@/services/api";
import type { Instrument } from "@/shared/types";
import s from "./shell.module.css";

/**
 * 顶部菜单栏（嵌在自绘标题栏内，见 shell/TitleBar.tsx）。
 *
 * 设计决策（方案 §4.4）：功能树不放左侧，改为顶部「一级菜单 = 业务域 + 下拉二级 = 域内页」。
 * 配套的快捷跳转（代码直达 / 命令面板 / 快捷键）在同批交付，
 * 否则高频操作会比侧边栏更慢 —— 这是顶部菜单能成立的前提。
 *
 * 品牌字样由 TitleBar 负责渲染，本组件只出菜单项与全局搜索，
 * 避免同一行里出现两个「qmt_work」。
 */
export function MenuBar() {
  const [openKey, setOpenKey] = useState<string>("");
  const open = useWorkspaceStore((st) => st.open);
  const barRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!openKey) return;
    const onDown = (e: MouseEvent) => {
      if (barRef.current && !barRef.current.contains(e.target as Node)) setOpenKey("");
    };
    document.addEventListener("mousedown", onDown);
    return () => document.removeEventListener("mousedown", onDown);
  }, [openKey]);

  const go = (key: string) => {
    open(key, {}, { title: pageLabel(key) });
    setOpenKey("");
  };

  return (
    <div className={s.menubar} ref={barRef}>
      {MENU.map((g) => (
        <div key={g.key} style={{ position: "relative", height: "100%" }}>
          <button
            type="button"
            className={[s.menuItem, openKey === g.key ? s.menuItemActive : ""]
              .filter(Boolean)
              .join(" ")}
            onClick={() => setOpenKey(openKey === g.key ? "" : g.key)}
            onMouseEnter={() => openKey && setOpenKey(g.key)}
          >
            {g.label}
          </button>
          {openKey === g.key && (
            <div className={s.menuDrop} role="menu">
              {/* ★ 走 visibleMenuItems：menu:false 的页面（已合并进上层页面）不重复列出 */}
              {visibleMenuItems(g).map((k) => {
                const def = PAGES[k];
                if (!def) return null;
                return (
                  <button
                    key={k}
                    type="button"
                    role="menuitem"
                    className={s.menuDropItem}
                    onClick={() => go(k)}
                  >
                    <span>{def.label}</span>
                    {def.status === "planned" && (
                      <span style={{ marginLeft: "auto", color: "var(--text-faint)", fontSize: 11 }}>
                        待实现
                      </span>
                    )}
                  </button>
                );
              })}
            </div>
          )}
        </div>
      ))}

      <span className={s.menuSpacer} />
      <GlobalSearch />
      <AppExitButton />
    </div>
  );
}

/**
 * 桌面壳内的「退出」入口。
 *
 * ★ 为什么必须有（2026-09-22 实测，用户报「右下角没有图标、无法真实退出」）：
 * 此前应用内**没有任何退出入口** —— 标题栏的「关闭」语义是「隐藏到托盘」，
 * 唯一的「真退出」藏在托盘右键菜单里。而 **Windows 11 默认把新出现的托盘图标
 * 收进「隐藏的图标」溢出层**，用户根本看不见它 ⇒ 一旦托盘不可见（或托盘因图标
 * 未打包而根本没建起来），用户就彻底退不出去，只能进任务管理器。
 * 结论：**退出入口不能依赖托盘的可见性**，应用内必须有明确可见的「退出」。
 *
 * 浏览器里（没有 electronAPI）不渲染；退出会中断后台任务，故走二次确认。
 */
function AppExitButton() {
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const api = typeof window !== "undefined" ? window.electronAPI : undefined;
  if (typeof api?.quitApp !== "function") return null;
  const doQuit = () => {
    setBusy(true);
    // 正常路径下进程会随即退出，这个 Promise 往往不会 resolve；
    // 真失败（极少）时回到可点状态，而不是把按钮永久卡在 loading。
    void api?.quitApp?.()?.catch(() => {}).finally(() => setBusy(false));
  };
  return (
    <>
      <button
        type="button"
        className={s.menuExit}
        title="退出 qmt_work（会同时停止后台服务）"
        aria-label="退出"
        onClick={() => setOpen(true)}
      >
        退出
      </button>
      <ConfirmModal
        open={open}
        title="退出 qmt_work"
        message="将结束客户端并停止后台服务（行情同步、任务调度一并停止）。下次启动需重新连接券商。"
        warn="若只是想收起窗口，请点标题栏右上角的「关闭」——那会最小化到系统托盘，程序继续运行。"
        confirmText="退出"
        danger
        loading={busy}
        onConfirm={doQuit}
        onCancel={() => setOpen(false)}
      />
    </>
  );
}

/**
 * 全局搜索 / 代码直达。
 * 输入 6 位代码回车 → 直接打开 K 线分析；输入文字 → 检索标的。
 */
function GlobalSearch() {
  const [q, setQ] = useState("");
  const [items, setItems] = useState<Instrument[]>([]);
  const [active, setActive] = useState(0);
  const [focused, setFocused] = useState(false);
  const openWorkbench = useOpenWorkbench();
  const timer = useRef<number | null>(null);

  useEffect(() => {
    if (timer.current !== null) clearTimeout(timer.current);
    const text = q.trim();
    if (text.length < 1 || /^\d{6}$/.test(text)) {
      setItems([]);
      return;
    }
    timer.current = window.setTimeout(() => {
      void marketApi
        .search(text, 8)
        .then((res) => {
          setItems(res ?? []);
          setActive(0);
        })
        .catch(() => setItems([]));
    }, 220);
    return () => {
      if (timer.current !== null) clearTimeout(timer.current);
    };
  }, [q]);

  const openCode = (code: string, name?: string) => {
    // ★ 顶部搜索框敲代码/选搜索结果 ⇒ 进「行情工作台」（看盘 + 交易同屏），
    //   而不是只开一个 K 线页。唯一出口 `useOpenWorkbench`（自带 normalizeCode）。
    openWorkbench(code, name);
    setQ("");
    setItems([]);
  };

  const onKeyDown = (e: React.KeyboardEvent<HTMLInputElement>) => {
    if (e.key === "Enter") {
      const text = q.trim();
      if (/^\d{6}$/.test(text)) {
        openCode(text);
        return;
      }
      const hit = items[active];
      if (hit) openCode(hit.code, hit.name);
      return;
    }
    if (e.key === "ArrowDown") {
      e.preventDefault();
      setActive((i) => Math.min(i + 1, items.length - 1));
    }
    if (e.key === "ArrowUp") {
      e.preventDefault();
      setActive((i) => Math.max(i - 1, 0));
    }
    if (e.key === "Escape") {
      setQ("");
      setItems([]);
    }
  };

  return (
    <div className={s.globalSearch}>
      <input
        className={s.globalSearchInput}
        placeholder="代码直达 / 搜索标的"
        value={q}
        onChange={(e) => setQ(e.target.value)}
        onKeyDown={onKeyDown}
        onFocus={() => setFocused(true)}
        onBlur={() => window.setTimeout(() => setFocused(false), 160)}
        aria-label="全局搜索"
      />
      {focused && items.length > 0 && (
        <div className={s.searchResults}>
          {items.map((it, i) => (
            <button
              key={it.code}
              type="button"
              className={[s.searchItem, i === active ? s.searchItemActive : ""]
                .filter(Boolean)
                .join(" ")}
              onMouseDown={(e) => {
                e.preventDefault();
                openCode(it.code, it.name);
              }}
            >
              <span className={s.searchCode}>{it.code}</span>
              <span>{it.name}</span>
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
