# QMT 量化 Agent 平台 · 前端

基于 React + Vite + ECharts 的可视化前端，由后端 FastAPI 同源托管（`/`）或经 Electron 桌面壳加载。

## 开发

```bash
npm install
npm run dev        # http://localhost:5173，API/WS 经 vite proxy 转发到 :21117 后端
```

开发态默认连接本机 `http://127.0.0.1:21117`，后端需先启动：`cd ../backend && python run.py`。

## 构建（产出到后端静态目录）

```bash
npm run build      # 输出到 ../backend/static，FastAPI 自动托管
```

构建后访问 `http://127.0.0.1:21117/` 即为完整 Web 应用。

## 桌面壳 + 独立 EXE 打包（Phase 6）

桌面壳为 Electron：启动 Python 后端子进程 → 等待就绪 → 加载同源前端 URL。
后端由 PyInstaller 打成 `qmt_backend.exe`，作为 `extraResources` 随包分发，运行时数据写至用户 `userData`。

步骤：

```bash
# 1) 构建前端
npm run build

# 2) 打包后端（在 backend venv 中安装 pyinstaller）
cd ../backend
pip install pyinstaller
python build_exe.py            # 产出 backend/dist/qmt_backend/qmt_backend.exe

# 3) 打包桌面应用（Electron）
cd ../frontend
npm install
npm run dist                   # NSIS 安装包 + 绿色 zip，输出到 dist-electron/
```

- 配置见 `electron-builder.yml`，主进程见 `electron/main.cjs`，桥接见 `electron/preload.cjs`。
- 后端打包脚本见 `../backend/build_exe.py`（已包含静态资源与动态依赖收集）。
- 缺少图标文件不影响打包（托盘/安装图标使用默认）。

## 目录

```
frontend/
  src/
    api.js                 # REST + Agent SSE 客户端
    ws 逻辑在各组件中       # WebSocket 实时行情
    components/
      Dashboard / Quote / Backtest / Rebalance / Agent / Settings / Chart
  electron/                # 桌面壳（main.cjs / preload.cjs）
  electron-builder.yml
  vite.config.js
```
