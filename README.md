# AI Indoor Reconstruction Agent

一个可审计、可协同、完全本地运行的室内点云/照片到 Three.js 参数化模型框架。仓库保留通用 Agent 编排、证据提取工具、流程契约、空白账本初始化能力、Three.js 审查界面和质量门禁；每个采集的专用生成器、坐标决议、复盘记录、回执、最终场景与审查图均在独立工作目录生成，不保存原始点云、相机照片、客户大数据或其派生生成物。

## 核心原则

- 算法只负责候选、切片、测量和反证；Agent/人工负责最终语义判断与绘制。
- `accepted`、`accepted-inferred`、`REVIEW` 明确分层，推断不能冒充实测。
- 楼层等容器不计入候选几何；issue 必须通过原子 `open_issue` 明确保留风险，并只能依靠独立 reviewer 和哈希绑定回执迁移状态，禁止手改状态冒充闭环。
- 原始数据只读，所有工作目录绑定 capture fingerprint。
- 发布前独立检查拓扑、遗漏、墙体交接、家具姿态、碰撞、地面和静态/三维视觉证据。
- 不依赖 GroundingDINO、TRELLIS、Blender 或模型 API。

## 独立安装（无需源码 checkout）

使用 Python 3.11 或 3.12。下载同版本的 wheel 和 `constraints-runtime.txt`，在新电脑建立虚拟环境：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -c constraints-runtime.txt "./indoor_reconstruction_agent-0.9.0-py3-none-any.whl[presentation]"
.\.venv\Scripts\indoor-recon.exe doctor --json
.\.venv\Scripts\indoor-recon.exe smoke --work smoke-001
```

Linux 使用 `.venv/bin/python` 和 `.venv/bin/indoor-recon`。基础 CLI/MCP 可不安装 `[presentation]`；完整 IFC/网格工具使用该额外依赖。安装依赖需要访问 Python 包源，之后常规计算无需在线模型服务。完整权威流程中的 JavaScript 几何检查另需 Node 22；没有 Node 时相应门禁仍阻断，不自动略过。

Schema、策略、配置、模板、领域实现与多视图/证据/IFC 脚本均由安装包按白名单携带，并校核资源哈希。命令可从任意工作目录运行，所有项目结果使用自己的 `--work`/输出路径保存。`doctor` 应显示 `runtimeKind: installed`，不会从当前目录的其他仓库加载代码。Python 环境的安装与升级不移动既有模型、点云或证据。

可用工具入口包括 `multiview`、`camera-control`、`multiview-batch`、`evidence`、`alignment`、`ifc-check` 和 `smoke`；每个入口支持 `--help`。它们沿用现有生产器和验收规则，不自动应用拟合候选或接受 Scene。

## 开发源码检出

```powershell
git clone https://github.com/yang498-Peter/ai-indoor-reconstruction-agent.git
Set-Location ai-indoor-reconstruction-agent
python -m pip install --user uv==0.12.5
python -m uv sync --locked --all-groups

# 统一入口：查看命令、初始化任务、检查状态和下一步
.\.venv\Scripts\indoor-recon.exe --help
.\.venv\Scripts\indoor-recon.exe init --help
.\.venv\Scripts\indoor-recon.exe status --help
.\.venv\Scripts\indoor-recon.exe next --help

# 零数据冒烟：生成合成 Scene V2 样例并启动 Viewer
.\prototypes\litereality-three-redraw-20260812\run-demo.ps1 `
  -PythonPath .\.venv\Scripts\python.exe -Sample
```

接入自备数据时：

```powershell
python .\.codex\skills\reconstruct-indoor-scene\scripts\discover_capture.py `
  --data 'X:\captures\capture-001' `
  --length-unit metre `
  --up-axis Z `
  --coordinate-reference local-scanner-frame `
  --output '.\outputs\capture-001\capture-manifest.json'
```

发现器只会把 checked-in adapter registry 中有真实解析器的点云标记为可用。V3 manifest
按内容而不是 mtime 绑定点云、图片和 pose；即使 pose 格式和图片路径通过，也必须再用
`scene-core\capture_readiness.py validate-pose` 对 V2 CaptureIndex 做空间对齐，才能启用
posed-photo association。

`geometry_workflow.py prepare` 现在会同时写出 `structural-proposals.json` 和
`candidate-topology.json`。前者把真实墙面 observation 与 wall hypothesis 分开；单面回波保留
墙厚两侧的两个中心线备选。后者做连续片段、区域轴族、beam 全局选择、房间候选和邻接分析，
但状态始终是 `PROPOSED_NOT_AUTHORITY`。通用流程默认使用 `profiles/default-office-v1.json`；
clean-room 的重复玻璃分区启发式只在显式传入 `--topology-profile profiles\cleanroom-v1.json`
时启用。`scanCoverageBoundary`、`floorSupportPolygon` 和 `inferredRoomBoundary` 不会自动生成墙。

场景 authority 完成后，用二维墙面指标和反向结构遗漏审计做硬门：

```powershell
python scene-core\geometry_workflow.py evaluate `
  --scene '.\outputs\capture-001\scene-authority.json' `
  --workspace '.\outputs\capture-001\geometry' `
  --output '.\outputs\capture-001\pointcloud-scene-metrics-v2.json'
```

V2 报告按墙输出 `(s,h)` 面积覆盖、真实最长连续无支撑段、门窗垂向 mask、
有符号表面残差/异常点和玻璃弱回波 profile，并从高结构点反查当前 Scene 未解释的墙段。
`accepted-measured` 墙只要返回 `REVIEW`，或 point→model 审计不是 `PASS`，场景级结果就会阻断。

浏览器打开：

`http://127.0.0.1:8765/prototypes/litereality-three-redraw-20260812/viewer.html`

接入本地真实数据：

```powershell
.\prototypes\litereality-three-redraw-20260812\run-demo.ps1 `
  -PythonPath .\.venv\Scripts\python.exe `
  -DataPath 'X:\captures\capture-001'
```

## 仓库结构

- `scene-core/`：Semantic Scene V2 内核——节点图 Schema、Agent 变更 API（`scene_api.py`）、
  渲染编译层（miter/T 交接与门窗真开洞）、统一质量报告、自包含内容寻址发布、V1 迁移与合成样例。
  见 [docs/SCENE-V2.zh-CN.md](docs/SCENE-V2.zh-CN.md)。
- `.agents/skills/reconstruct-indoor-scene/`：Codex 标准发现路径中的精简工作流；只编排统一 CLI/MCP，不复制领域规则。
- `.codex/skills/reconstruct-indoor-scene/`：迁移期兼容层及底层脚本；`scripts/sync_skills.py --check` 防止身份和入口漂移。
- `prototypes/litereality-three-redraw-20260812/`：通用 Three.js 产品界面与验收入口（自动识别 V1/V2 场景）。
- `tests/codex-scan/`、`tests/scene-v2/`：编排器门禁测试与 Scene V2 API/几何数值测试。
- `docs/`：跨电脑迁移、数据契约和协作说明。
- `data/`：仅作本地挂载点；内容被 Git 忽略。

详细流程见 [docs/WORKFLOW.zh-CN.md](docs/WORKFLOW.zh-CN.md)，迁移边界见 [docs/PORTABILITY.zh-CN.md](docs/PORTABILITY.zh-CN.md)。

展示精修、点云对齐和 BIM 回读的依赖与零数据验证见[跨电脑迁移](docs/PORTABILITY.zh-CN.md)。代理入口为 [.agents/skills/reconstruct-indoor-scene/SKILL.md](.agents/skills/reconstruct-indoor-scene/SKILL.md)，常用操作见[室内建模 SOP](docs/室内建模准确性与快速交付SOP.zh-CN.md)。新电脑先运行合成冒烟，再接入自己的采集数据；它不自动复现某个客户场景。

## 验证

```powershell
.\.venv\Scripts\python.exe scripts/validate_portable_repo.py
.\.venv\Scripts\python.exe scripts/sync_skills.py --check
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check src tests/contracts/test_wp8_engineering.py
.\.venv\Scripts\python.exe -m mypy src
.\.venv\Scripts\indoor-recon.exe benchmark --output wp8-benchmark-report.json
node --test tests/scene-v2/scene-core.test.mjs
```

快速基准没有给定历史基线或真实采集时，性能回归和真实数据回归必须保持 `NOT_RUN`，
因此报告总状态为 `REVIEW`，不能据此宣称真实场景或发布验收通过。

本仓库为公开的流程与工具仓库。客户原图、点云、场景专属模型和精确布局、注册码、API Token 及个人目录不进入 Git；私有成果按清单和哈希独立保管。可选的外部 Codex 自动审查尚未配置或运行，保持 `NOT_RUN`；常规 CI 不发送源码给外部模型，场景的独立 Reviewer 门禁仍按原契约执行。
