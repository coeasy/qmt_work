// 后端打包脚本（跨平台）：调用仓库自带的 backend/build_exe.py（PyInstaller onedir）。
// 自动探测「能跑 PyInstaller 的 Python」：
//   1) PATH 上的 python / py / python3（要求 `python -m PyInstaller --version` 可用）
//   2) 常见 miniforge / 仓库自带运行时路径
// build_exe.py 会负责：隐藏导入、排除重型依赖、补 MSVC 运行库、打包前端静态/runtimes/xtquant_client。
// 用法：node scripts/backend-build.mjs
import { spawnSync } from "node:child_process";
import { existsSync } from "node:fs";
import path from "node:path";

const root = process.cwd(); // 约定从 frontend/ 运行
const backendDir = path.resolve(root, "..", "backend");
const buildExe = path.join(backendDir, "build_exe.py");

function canRun(py, extra) {
  try {
    const r = spawnSync(py, extra, { stdio: "ignore", shell: true, windowsHide: true });
    return r.status === 0;
  } catch {
    return false;
  }
}

function run(cmdStr, cwd) {
  const r = spawnSync(cmdStr, { stdio: "inherit", cwd, shell: true, windowsHide: true });
  if (r.status !== 0) process.exit(r.status ?? 1);
}

if (!existsSync(buildExe)) {
  console.error("找不到 build_exe.py：", buildExe);
  process.exit(2);
}

// 候选 python（按可用性排序）
const candidates = [
  "python", "py", "python3",
  "C:\\python\\miniforge3\\python.exe",
  `${process.env.LOCALAPPDATA}\\miniforge3\\python.exe`,
  "P:\\python\\miniforge3\\python.exe",
  path.join(backendDir, "runtimes", "cp311", "python.exe"),
];

let chosen = null;
for (const py of candidates) {
  if (!py) continue;
  if (!existsSync(py) && !canRun(py, ["--version"])) continue;
  if (canRun(py, ["-m", "PyInstaller", "--version"])) { chosen = py; break; }
  // 兜底：Scripts/pyinstaller.exe 存在即可（build_exe.py 内部会再用 sys.executable -m PyInstaller，
  // 这种情况仍可能需要该 python 装过 pyinstaller；若失败请先 pip install pyinstaller）
  const scriptsExe = path.join(path.dirname(path.dirname(py)), "Scripts", "pyinstaller.exe");
  if (existsSync(scriptsExe)) { chosen = py; break; }
}

if (!chosen) {
  console.error("未找到可用的 Python/PyInstaller。请先安装：pip install pyinstaller");
  process.exit(2);
}

console.log(">>> 使用 Python：", chosen);
run(`"${chosen}" "${buildExe}"`, backendDir);
console.log(">>> 后端打包完成 ->", path.join(backendDir, "dist", "qmt_work"));
