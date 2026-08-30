# 第三方许可与合规清单

> 建立日期：2026-08-30 · 对应方案 `docs/archive/qmt_work 终极整合优化改进方案（2026-08-30）·五参照系.md` 的 **G0 合规基线**
>
> 本文件是唯一权威来源。新增任何依赖前先在此登记；**AGPL / GPL / SSPL / 非商业类许可零容忍**（理由见第 4 节）。

---

## 1. 本仓库自身许可

| 项 | 值 |
|---|---|
| 许可 | **Apache-2.0** |
| 文件 | 仓库根 `LICENSE` |
| 专利授权 | 有（Apache-2.0 第 3 条，含诉讼终止条款） |
| 商用 | 允许（须满足第 2 节的前置条件） |

**选型理由**：项目接入真实券商通道，Apache-2.0 的明示专利授权对所有使用者（含企业）都更友好；同时 Apache-2.0 与 AGPL-3.0 **单向不兼容**——AGPL 代码不可并入 Apache-2.0 项目，这从法律层面天然强化了第 4 节的借鉴隔离（我们只取架构思想，不复制一行代码，许可上也无法"顺手复制"）。

> 若你希望改为 MIT / BSD / 商用双许可，只需替换根 `LICENSE` 全文并同步本文件第 1 节，其余内容不受影响。

---

## 2. ⛔ 商用前置条件（务必阅读）

本仓库默认可商用，但**必须同时满足**：

1. **部署环境不得安装 `eltdx`**。该包采用 `ELTDX Research-Only License`，明文"禁止一切商业使用和滥用"（见第 3.1 节）。
2. 若打包分发，需确认未把 `eltdx` 打进产物 —— 见第 3.1 节的构建检查命令。
3. 行情层在无 `eltdx` 时自动降级为券商数据源（已由软依赖化改造保证，见第 3.1 节"改造内容"）。

---

## 3. 依赖许可清单

### 3.1 后端 Python 依赖（`backend/requirements.txt`）

| 包 | 版本约束 | 许可 | 类别 | 商用 | 备注 |
|---|---|---|---|---|---|
| fastapi | >=0.115 | MIT | 运行时 | ✅ | |
| uvicorn[standard] | >=0.30 | BSD-3-Clause | 运行时 | ✅ | |
| fastmcp | >=1.5,<3 | **Apache-2.0** | 运行时 | ✅ | 实测本机 2.14.7，元数据 `License-Expression: Apache-2.0` |
| pydantic | >=2.7 | MIT | 运行时 | ✅ | |
| pydantic-settings | >=2.4 | MIT | 运行时 | ✅ | |
| httpx | >=0.27 | BSD-3-Clause | 运行时 | ✅ | |
| python-dotenv | >=1.0 | BSD-3-Clause | 运行时 | ✅ | |
| email-validator | — | MIT | 运行时 | ✅ | 缺失会导致 fastapi 导入 EmailStr 崩溃 |
| psutil | — | BSD-3-Clause | 运行时 | ✅ | 本机 QMT 进程发现 |
| numpy | >=1.24 | **BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0** | 运行时 | ✅ | 实测本机 2.5.2 |
| pandas | >=2.0 | BSD-3-Clause | 运行时 | ✅ | 实测本机 3.0.5 |
| redis | >=5.0 | MIT | 运行时 | ✅ | 行情总线真路径 |
| fakeredis | >=2.0 | BSD-3-Clause | 测试 | ✅ | 测试替身，不进产物 |
| pytest | — | MIT | 测试 | ✅ | |
| pyinstaller | >=6.0 | **GPL-2.0-or-later + 例外** | **构建工具** | ✅ | ⚠️ 见 3.3 |
| **eltdx** | >=3.0 | **ELTDX Research-Only** | **可选** | ❌ **禁止** | ⛔ 见 3.2 |

### 3.2 ⛔ 阻断级风险：eltdx（Research-Only License）

**实证**（本机 `eltdx==3.0.4` 的 `dist-info/licenses/LICENSE` 首 4 行原文）：

```
ELTDX Research-Only License

Copyright (c) 2026 ELTDX contributors

This project is provided only for personal learning, protocol research, and
non-commercial study.
```

- 全文 28 行，含 1 处 `non-commercial` / 非商业限制措辞。
- **常见误解：网上资料（含部分第三方文档站）称 eltdx 为 MIT。那是 1.x 时期的历史信息，3.0.4 已改为 Research-Only。** 同时因为该许可是自定义的，`importlib.metadata` 读不到 `License-Expression`、GitHub licensee 判为 `NOASSERTION` —— **自动化扫描工具不会报警，只能人工核对 LICENSE 文件正文**。

**风险等级**：高。该包是"无券商连接时的行情 / 基础数据补充源"，属运行时核心路径，而非可选装饰。若随产物分发并商用，直接违反其许可。

**已实施的处置（本次 G0 改造）**：

1. **软依赖化** —— `app/datasource/eltdx_source.py:26` 原为顶层硬导入 `from eltdx import TdxClient`，未安装会导致模块加载失败。已改为 `try/except ImportError` + `_HAS_ELTDX` 标志，缺失时该数据源不可用但**应用正常启动**，行情层自动降级到券商源（符合项目零 mock 铁律：降级 ≠ 造假）。
2. **依赖隔离** —— 从 `requirements.txt` 主列表移出，新建 `backend/requirements-optional.txt` 单独承载，并在注释中显著标注风险。
3. **构建自检** —— 打包前执行下方命令，产物中不得出现 eltdx。

```bash
# 打包前自检：应为 0 个匹配
python -c "import eltdx" 2>&1 | grep -q "No module" && echo "OK: 商用构建环境无 eltdx" || echo "FAIL: 存在 eltdx，禁止商用分发"

# 检查构建产物
ls dist/qmt_work/_internal/ | grep -i eltdx && echo "FAIL: eltdx 已打入产物" || echo "OK"
```

**后续建议（未实施，需决策）**：若需长期商用，考虑替换为许可清晰的数据源，或改为"用户自行 pip install 且明确勾选非商业用途"的安装时选择。

### 3.3 ⚠️ PyInstaller（GPL-2.0-or-later + 例外条款）

PyInstaller 本体是 GPL-2.0，但带有**明确的例外条款**：允许将其用于打包任意许可（含专有软件）的应用并分发，只要不修改 PyInstaller 本身。

- 我们**仅将其作为构建工具使用**，未修改其源码 → 适用例外条款，可商用分发。
- 分发时需要随产物提供 PyInstaller 的 GPL 许可文本副本（业界惯例；若要做严谨合规，建议在 EXE 目录中附带 `PyInstaller-LICENSE.txt`）。
- **未进入运行时依赖**（`pip install -r requirements.txt` 的生产部署不需要它）。

### 3.4 前端 npm 依赖（`frontend/package.json`）

| 包 | 许可 | 类别 | 商用 | 备注 |
|---|---|---|---|---|
| react | MIT | 运行时 | ✅ | |
| react-dom | MIT | 运行时 | ✅ | |
| echarts | **Apache-2.0** | 运行时 | ✅ | 图表核心，K 线 / 板块 / 资金流均依赖 |
| electron-updater | MIT | 桌面壳 | ✅ | |

依赖项仅 4 个，许可面极干净。**新增图表库 / UI 库前务必先查许可** —— 部分金融图表库（如 TradingView Lightweight Charts 是 Apache-2.0，可用；但某些 K 线库为商用授权）不可直接引入。

---

## 4. 借鉴隔离声明（架构对标项目）

本轮重构参考了以下开源项目。为避免 copyleft 污染，**只借鉴架构范式与接口契约，零代码复制**。

| 参照项目 | 许可 | 借鉴内容 | 隔离方式 | 风险 |
|---|---|---|---|---|
| **Fincept Terminal** | **AGPL-3.0-or-later** | DataHub topic pub/sub 范式、`TopicPolicy` 策略表设计、屏幕生命周期纪律（P1–P15）、PythonRunner 并发闸门思路 | 仅取设计思想，按 qmt_work 现有栈（FastAPI + React）**独立实现**；不复制任何源码、不复制文档文本 | 高（若混入代码即污染全仓） |
| **OpenBB** | **AGPL-3.0-only** | `Fetcher` 三段式（transform_query / extract_data / transform_data）、`standard_models` + `__alias_dict__` 字段别名层、`OBBject` 结果容器、入口点自动发现扩展机制 | 同上，仅取接口契约设计 | 高 |
| 通达信 / 大智慧 / 东方财富 | 闭源商业产品 | 产品范式与交互模式（条件选股器、公式体系、K 线缩放平移、版面管理） | 属产品行为层，不涉及代码；且其 UI 交互不构成可版权保护的独创表达 | 低（交互范式不受版权保护） |

### 强制纪律（后续所有借鉴工作必须遵守）

1. **零代码复制** —— 不得从 AGPL 项目中复制任何代码行、配置片段或文档段落。
2. **只实现契约** —— 参照的是"接口应该长什么样"（如 `Fetcher` 的三段职责划分），实现由本项目独立编写。
3. **新增依赖先过许可扫描** —— AGPL / GPL（无例外）/ SSPL / 非商业许可一律禁止引入运行时依赖。
4. **自动化工具不可尽信** —— eltdx 案例已证明：自定义许可的 `License-Expression` 为空、GitHub 判 `NOASSERTION`、网传文档过时。**必须人工打开 LICENSE 文件正文核对**。
5. **本文件是唯一登记处** —— 任何新依赖在合并前须在此表格中登记并注明许可来源（附核对方式）。

---

## 5. 变更记录

| 日期 | 变更 | 依据 |
|---|---|---|
| 2026-08-30 | 初版建立。补 LICENSE(Apache-2.0)；登记后端 16 + 前端 4 个依赖；识别 eltdx 阻断级风险并完成软依赖化改造；记录 PyInstaller GPL 例外条款；确立借鉴隔离纪律 | G0 合规基线 |
