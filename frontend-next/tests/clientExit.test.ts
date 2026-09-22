import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { describe, expect, it } from "vitest";

const require_ = createRequire(import.meta.url);

/**
 * 客户端「启动 / 退出」机制护栏 —— 2026-09-22。
 *
 * ## 用户报的现象
 * 「关闭客户端后右下角没有托盘图标，无法真实退出」—— 只能进任务管理器杀进程。
 *
 * ## 实测出来的根因链（一条自锁死的缺陷）
 * 1. `electron-builder.yml` 的 `files` 只有「`electron` 目录通配」+ `package.json`
 *    ⇒ `build/icon.png` **没有进 asar**（实测 `asar list` 共 1531 条，
 *    `build/` 与 `icon.*` 命中 **0** 条）。
 * 2. 打包态 `__dirname` = `resources/app.asar/electron`，而 `trayIcon()` 找的是
 *    `path.join(__dirname, "..", "build", "icon.png")` ⇒ 目标文件不存在。
 * 3. `trayIcon()` 返回 `createEmpty()` ⇒ `icon.isEmpty()` 为真 ⇒ `createTray()`
 *    直接 `return` ⇒ **托盘不存在**。
 * 4. 而 `win.on("close")` 的语义是「未退出则隐藏到托盘」，`quitting` **只由托盘菜单的
 *    「退出」**置位 ⇒ 托盘不在时，「关闭」变成**单向隐藏**：窗口消失、托盘没有、
 *    **再也没有任何退出入口**。
 *
 * ## 为什么这些断言必须存在（而不是"小心点就行"）
 * 这是典型的**接线上漏配**：`main.cjs` 里每一行代码单独看都是对的，单测、类型检查、
 * 甚至人工 code review 都抓不住 —— 缺陷只存在于「构建配置」与「运行时路径」之间。
 * 唯一能守住它的地方就是「把构建配置当契约来断言」。
 *
 * ## 为什么断言必须**先剥注释再扫**
 * 修完之后源码里到处都在讨论 `trayAvailable` / `requestQuit` / `build/icon.png`
 * （本文件的注释也在讨论）。不剥注释的话，**把实现删干净、只留注释**，断言照样全绿
 * —— 本仓库已有过这个教训（见 `workbenchInfoPlacement.test.ts` 的同名提醒）。
 *
 * ## 可证伪性
 * 每条断言都做过「把实现改回旧写法 ⇒ 用例必须变红」的验证，见
 * `docs/2026-09-22_第20轮_客户端启动退出机制修复.md`。
 */

const resolve_ = (rel: string) => require_.resolve(rel);

/** 读取文本（⚠️ 必须走 createRequire().resolve() 拿绝对路径：本环境 fs 被 shim 过，
 *  `readFileSync(new URL(...))` 会报 `The URL must be of scheme file`）。 */
const read = (rel: string) => readFileSync(resolve_(rel), "utf8");

/** 剥掉 JS 的块注释与行注释。`(^|\s)` 前缀保证 `http://`、`data:` 这类字面量不被误伤。 */
const stripJs = (s: string) =>
  s.replace(/\/\*[\s\S]*?\*\//g, "").replace(/(^|\s)\/\/[^\n]*/g, "$1");

/** 剥掉 YAML 的 `#` 注释（整行 + 行尾）。 */
const stripYaml = (s: string) =>
  s
    .split("\n")
    .map((l) => l.replace(/(^|\s)#.*$/, "$1"))
    .join("\n");

const MAIN = stripJs(read("../electron/main.cjs"));
const PRELOAD = stripJs(read("../electron/preload.cjs"));
const YML = stripYaml(read("../electron-builder.yml"));
const MENUBAR = stripJs(read("../src/shell/MenuBar.tsx"));
const CSS = read("../src/shell/shell.module.css");
const VITE_ENV = read("../src/vite-env.d.ts");

/** 取某个 `function xxx()` 的完整函数体（非贪婪到第一个顶格 `}`）。 */
const fnBody = (src: string, name: string): string => {
  const m = src.match(new RegExp(`function ${name}\\([^)]*\\)[\\s\\S]*?\\n\\}`));
  expect(m, `找不到 function ${name}()`).toBeTruthy();
  return m![0];
};

describe("★ 构建配置：托盘图标必须真的进包（根因）", () => {
  // 这一组是本缺陷的**唯一**防线。它断言的不是代码逻辑，而是「构建配置与运行时
  // 路径约定一致」—— 这正是所有单测都覆盖不到的地方。
  const filesPart = YML.split(/^extraResources:/m)[0];
  const extraPart = YML.split(/^extraResources:/m)[1] ?? "";

  it("files 段里必须有一条 `- \"build/icon.png\"`", () => {
    // 判据取「YAML 列表项的精确形态」，而不是全文 contains("build/icon.png")：
    // 后者会被 `directories.buildResources: build` 与注释蒙混过关，而
    // buildResources **只决定安装包/EXE 用哪个图标，不会把它塞进 asar**。
    expect(
      filesPart,
      'files 段里没有 `- "build/icon.png"` ⇒ 托盘图标不进 asar ⇒ 托盘建不起来 ⇒ 关闭后再也退不出去',
    ).toMatch(/^\s*-\s*"build\/icon\.png"\s*$/m);
  });

  it("extraResources 里还有一条独立冗余路径（asar 外那份）", () => {
    // 与 asar 内那份互为冗余：任何一条可用即可建起托盘。
    // 两处都配，是为了不把「能建起托盘」押在单一机制上。
    expect(extraPart, "extraResources 里没有托盘图标").toMatch(
      /from:\s*build\/icon\.png/,
    );
  });

  it("图标文件真的存在且非空（配置写对了但文件没了 = 一样白搭）", () => {
    const p = resolve_("../build/icon.png");
    const buf = readFileSync(p);
    expect(buf.length, "build/icon.png 是空文件").toBeGreaterThan(0);
    // PNG magic：89 50 4E 47 0D 0A 1A 0A
    expect(buf.subarray(0, 8).toString("hex")).toBe("89504e470d0a1a0a");
  });
});

describe("★ 关闭窗口：托盘不在时必须是真退出（自锁死防线）", () => {
  it("close 处理器仍然拦截默认行为、且对 quitting 放行", () => {
    const m = MAIN.match(/win\.on\(\s*"close"\s*,[\s\S]*?\n\s*\}\);/);
    expect(m, '找不到 win.on("close") 处理器').toBeTruthy();
    const h = m![0];
    expect(h, "不再拦截默认行为（会直接销毁窗口）").toContain("e.preventDefault()");
    expect(h, "退出流程中不再放行 ⇒ 会退不掉").toMatch(
      /if\s*\(\s*quitting\s*\)\s*return/,
    );
  });

  it("★★ `!trayAvailable` ⇒ requestQuit() 的兜底分支必须存在", () => {
    // 这是「自锁死」的唯一解锁点。删掉它，托盘一旦建不起来，
    // 用户点关闭 ⇒ 窗口消失 + 无托盘 + 无入口 ⇒ 只能进任务管理器。
    const m = MAIN.match(/win\.on\(\s*"close"\s*,[\s\S]*?\n\s*\}\);/);
    const h = m![0];
    expect(
      h,
      "托盘不可用时没有兜底成真退出 —— 「关不掉」缺陷会复发",
    ).toMatch(
      /if\s*\(\s*!trayAvailable\s*\)\s*\{\s*requestQuit\(\)\s*;?\s*return\s*;?\s*\}/,
    );
    // 且必须在 win.hide() **之前**：放到 hide() 后面虽然也能跑，但语义上
    // 「先隐藏再退出」会闪一下，而且后人重构时极易把 hide() 挪到前面。
    expect(h.indexOf("requestQuit()")).toBeLessThan(h.indexOf("win.hide()"));
  });

  it("window-all-closed 也必须兜底退出（无窗口 + 无托盘 = 幽灵进程）", () => {
    expect(MAIN).toMatch(
      /app\.on\(\s*"window-all-closed"\s*,[\s\S]*?!trayAvailable[\s\S]*?requestQuit\(\)/,
    );
  });
});

describe("★ trayAvailable 的语义：只有真建起来才算数", () => {
  it("声明为可变、初值 false（不许写成常量或初值 true）", () => {
    expect(MAIN, "trayAvailable 不是 let 声明").toMatch(
      /let\s+trayAvailable\s*=\s*false\s*;/,
    );
  });

  it("`trayAvailable = true` 全文件只出现一次（单一真源）", () => {
    const n = (MAIN.match(/trayAvailable\s*=\s*true/g) || []).length;
    expect(n, "多处置 true ⇒ 无法判断托盘到底建起来没有").toBe(1);
  });

  it("置 true 的位置在 `new Tray(...)` 成功之后", () => {
    const body = fnBody(MAIN, "createTray");
    const iNew = body.indexOf("new Tray");
    const iTrue = body.indexOf("trayAvailable = true");
    expect(iNew, "createTray 里没有 new Tray").toBeGreaterThan(-1);
    expect(iTrue, "createTray 里没有置 trayAvailable = true").toBeGreaterThan(-1);
    expect(
      iTrue,
      "在 new Tray 之前就置 true ⇒ 托盘没建起来也以为有托盘",
    ).toBeGreaterThan(iNew);
  });

  it("图标为空 / 构造抛异常两条失败路径都置 false 并 return", () => {
    const body = fnBody(MAIN, "createTray");
    expect(body, "icon.isEmpty() 分支不再放弃托盘").toMatch(
      /if\s*\(\s*icon\.isEmpty\(\)\s*\)\s*\{[\s\S]*?trayAvailable\s*=\s*false[\s\S]*?return\s*;/,
    );
    expect(body, "new Tray 抛异常时不再降级").toMatch(
      /catch\s*\([^)]*\)\s*\{[\s\S]*?trayAvailable\s*=\s*false[\s\S]*?return\s*;/,
    );
  });

  it("托盘右键菜单含「退出」且指向 requestQuit", () => {
    const body = fnBody(MAIN, "createTray");
    expect(body, "托盘菜单没有「退出」项").toMatch(
      /label:\s*"退出"\s*,\s*click:\s*\(\)\s*=>\s*requestQuit\(\)/,
    );
  });
});

describe("★ 托盘图标：必须有内嵌兜底，且兜底图是真 PNG", () => {
  it("候选文件路径全失败后，仍走内嵌 base64 兜底", () => {
    const body = fnBody(MAIN, "trayIcon");
    const iPath = body.indexOf("createFromPath");
    const iData = body.indexOf("createFromDataURL");
    expect(iPath, "不再尝试真实图标文件").toBeGreaterThan(-1);
    expect(iData, "★ 内嵌兜底不见了 —— 打包态图标一缺就又没有托盘").toBeGreaterThan(
      iPath,
    );
    expect(MAIN, "没有内嵌图标表").toContain("TRAY_ICON_PNG_B64");
  });

  it("★ 内嵌 base64 必须是合法 PNG 且尺寸正确（写错了等于没有兜底）", () => {
    // 这条不是形式主义：base64 抄错一个字符，`createFromDataURL` 会静默返回空图
    // ⇒ `icon.isEmpty()` 为真 ⇒ 托盘又建不起来 ⇒ 又回到「关不掉」。
    // 而"抄错字符"恰恰是人工维护大段 base64 时最常见的失误。
    const m = MAIN.match(/const TRAY_ICON_PNG_B64\s*=\s*\{([\s\S]*?)\n\};/);
    expect(m, "找不到 TRAY_ICON_PNG_B64 定义").toBeTruthy();
    // ⚠️ tsconfig 开了 noUncheckedIndexedAccess ⇒ 索引访问与解构结果都是
    //    `string | undefined`，必须显式兜底，否则 tsc 直接报错。
    const pairs = [...(m?.[1] ?? "").matchAll(/(\d+)\s*:\s*"([^"]+)"/g)];
    expect(pairs.length, "内嵌图标数量不足（16 与 32 各需一份）").toBe(2);
    const seen: number[] = [];
    for (const pair of pairs) {
      const sizeStr = pair[1] ?? "";
      const b64 = pair[2] ?? "";
      const want = Number(sizeStr);
      const buf = Buffer.from(b64, "base64");
      expect(buf.subarray(0, 8).toString("hex"), `${want}px 不是 PNG`).toBe(
        "89504e470d0a1a0a",
      );
      expect(buf.subarray(12, 16).toString("ascii"), `${want}px 缺 IHDR`).toBe("IHDR");
      const w = buf.readUInt32BE(16);
      const h = buf.readUInt32BE(20);
      expect([w, h], `${want}px 的实际尺寸是 ${w}x${h}`).toEqual([want, want]);
      expect(
        buf.subarray(buf.length - 8, buf.length - 4).toString("ascii"),
        `${want}px 缺 IEND（数据被截断）`,
      ).toBe("IEND");
      seen.push(want);
    }
    expect(seen.sort((a, b) => a - b)).toEqual([16, 32]);
  });
});

describe("★ requestQuit / IPC：真退出只有一条路，且顺序不能反", () => {
  it("requestQuit 先置 quitting 再 app.quit()", () => {
    // 顺序反了 ⇒ app.quit() 触发 close 时 quitting 还是 false ⇒
    // 被 close 处理器 preventDefault 拦回来 ⇒ 退不掉（又是一个自锁死）。
    const body = fnBody(MAIN, "requestQuit");
    const iQuitting = body.indexOf("quitting = true");
    const iQuit = body.indexOf("app.quit()");
    expect(iQuitting, "requestQuit 不再置 quitting").toBeGreaterThan(-1);
    expect(iQuit, "requestQuit 不再调 app.quit()").toBeGreaterThan(-1);
    expect(
      iQuitting,
      "先 app.quit() 再置 quitting ⇒ 退出会被 close 拦回",
    ).toBeLessThan(iQuit);
  });

  it("app-quit 与 window-close 是两条不同的路，不许合并", () => {
    // window-close = 隐藏到托盘（程序继续跑）；app-quit = 结束进程（含后端优雅停机）。
    // 若有人图省事把 window-close 直接改成 quit，标题栏的「关闭」就变成了
    // 「点一下就整个退出」—— 那是另一种让用户困惑的破坏。
    expect(MAIN, "app-quit 不存在或不再走 requestQuit").toMatch(
      /ipcMain\.handle\(\s*"app-quit"\s*,[\s\S]*?requestQuit\(\)/,
    );
    expect(MAIN, "window-close 的语义被改掉了").toMatch(
      /ipcMain\.handle\(\s*"window-close"\s*,[\s\S]*?win\.close\(\)/,
    );
  });

  it("Ctrl+Q 本地快捷键也能退出（退出入口不能只有托盘一个）", () => {
    // Windows 11 默认把新出现的托盘图标收进「隐藏的图标」溢出层，
    // 用户**看不见**它 ⇒ 退出入口必须不依赖托盘的可见性。
    expect(MAIN, '没有 Ctrl+Q 快捷键').toMatch(/toLowerCase\(\)\s*!==\s*"q"/);
    expect(MAIN, "Ctrl+Q 没有调 requestQuit").toMatch(
      /input\.control\s*\|\|\s*input\.meta\)\s*requestQuit\(\)/,
    );
    const handlers = MAIN.match(/before-input-event/g) || [];
    expect(
      handlers.length,
      "before-input-event 只剩一个（F12 与 Ctrl+Q 应各一个监听器）",
    ).toBeGreaterThanOrEqual(2);
  });
});

describe("★ 桥接层：页面必须够得着「真退出」", () => {
  it("preload 暴露 quitApp → app-quit（与 windowClose 分开）", () => {
    expect(PRELOAD, "quitApp 没有暴露给渲染层").toMatch(
      /quitApp:\s*\(\)\s*=>\s*ipcRenderer\.invoke\(\s*"app-quit"\s*\)/,
    );
    expect(PRELOAD, "windowClose 的通道被改了").toMatch(
      /windowClose:\s*\(\)\s*=>\s*ipcRenderer\.invoke\(\s*"window-close"\s*\)/,
    );
  });

  it("vite-env.d.ts 声明了 quitApp（否则 TS 侧调不了）", () => {
    expect(VITE_ENV).toMatch(/quitApp\?:\s*\(\)\s*=>\s*Promise<boolean>/);
  });

  it("菜单栏渲染退出按钮，并在浏览器里自动隐藏", () => {
    expect(MENUBAR, "菜单栏没有挂退出按钮").toMatch(/<AppExitButton\s*\/>/);
    expect(
      MENUBAR,
      "没有 `quitApp` 时仍渲染按钮 ⇒ 浏览器里点了没反应",
    ).toMatch(/typeof\s+api\?\.quitApp\s*!==\s*"function"\s*\)\s*return\s+null/);
    expect(MENUBAR, "退出按钮没有走 quitApp").toContain("quitApp");
  });

  it("退出走确认弹窗（不可逆动作不用 window.confirm）", () => {
    expect(MENUBAR).toMatch(/<ConfirmModal/);
    expect(MENUBAR).not.toMatch(/window\.confirm/);
  });

  it("★ 退出按钮必须 no-drag（否则落在拖动区里点不动）", () => {
    // 自绘标题栏整条是 `-webkit-app-region: drag`。按钮若不显式声明 no-drag，
    // 鼠标事件会被系统拿去拖窗口 —— 表现为「按钮点了没反应，还把窗口拖走了」。
    const m = CSS.match(/\.menuExit\s*\{[\s\S]*?\}/);
    expect(m, "找不到 .menuExit 规则").toBeTruthy();
    expect(m![0], "退出按钮没有声明 no-drag").toContain(
      "-webkit-app-region: no-drag",
    );
  });
});

describe("★ 启动机制：单实例守卫与托盘信息准确性", () => {
  it("★★ 没拿到单实例锁时 whenReady 必须直接 return（否则会起第二个后端）", () => {
    // `app.quit()` 在 ready **之前**调用只是「预约退出」，`whenReady()` 依然会 resolve。
    // 少了这个 return，第二个实例照样 startBackend() + createWindow()：起出第二个后端
    // （端口自动 +1、抢同一份 SQLite）再被退出流程杀掉。症状是「双击图标后闪一下、
    // 日志里多一次后端冷启动、偶尔端口错乱」—— 很难归因，所以必须由护栏守。
    expect(MAIN, "whenReady 里没有单实例守卫").toMatch(
      /app\.whenReady\(\)\.then\([\s\S]*?if\s*\(\s*!gotLock\s*\)\s*return\s*;/,
    );
  });

  it("托盘提示随实际端口刷新（端口不是默认值时提示不能是旧的）", () => {
    // 托盘在 createTray() 建好，而 activePort 要等 waitReady() 健康检查成功才知道
    // （后端端口被占用会自动 +1）。旧实现只在建托盘时 setToolTip 一次
    // ⇒ 端口一旦变化，托盘提示永远是错的，用户按提示开浏览器会打不开。
    expect(MAIN).toContain("refreshTrayTooltip");
    expect(MAIN, "activePort 定下来后没有刷新托盘提示").toMatch(
      /activePort\s*=\s*port\s*;[\s\S]{0,160}?refreshTrayTooltip\(\)/,
    );
    const body = fnBody(MAIN, "createTray");
    expect(body, "createTray 没有走 refreshTrayTooltip").toContain(
      "refreshTrayTooltip()",
    );
    expect(body, "createTray 里又出现了写死的 setToolTip").not.toMatch(
      /tray\.setToolTip\(/,
    );
  });
});

describe("★ 防复发：图标路径与 asar 布局必须一致", () => {
  it("trayIcon 的首选路径是 `__dirname/../build/icon.png`（打包态 = app.asar/build/icon.png）", () => {
    // 这条把「运行时路径约定」也钉死：若有人把首选路径改成别的相对层级，
    // 上面 files 段的断言就与实际路径脱钩了 —— 两边必须一起改。
    expect(MAIN).toMatch(
      /path\.join\(\s*__dirname\s*,\s*"\.\."\s*,\s*"build"\s*,\s*"icon\.png"\s*\)/,
    );
  });

  it("不依赖 `__dirname` 之外的绝对路径（打包态 __dirname 在 asar 里）", () => {
    // 反面例子：写 `path.join(app.getPath("exe"), ...)` 或 process.cwd()，
    // 打包后指向安装目录而不是 asar 内部，同样会找不到。
    const body = fnBody(MAIN, "trayIcon");
    expect(body).not.toContain("process.cwd()");
    expect(body).not.toContain("app.getPath(");
  });
});
