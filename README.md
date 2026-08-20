# AI Indoor Reconstruction Agent

一个可审计、可协同、完全本地运行的室内点云/照片到 Three.js 参数化模型框架。仓库保留通用 Agent 编排、证据提取工具、流程契约、空白账本初始化能力、Three.js 审查界面和质量门禁；每个采集的专用生成器、坐标决议、复盘记录、回执、最终场景与审查图均在独立工作目录生成，不保存原始点云、相机照片、客户大数据或其派生生成物。

## 核心原则

- 算法只负责候选、切片、测量和反证；Agent/人工负责最终语义判断与绘制。
- `accepted`、`accepted-inferred`、`REVIEW` 明确分层，推断不能冒充实测。
- 楼层等容器不计入候选几何；issue 必须通过原子 `open_issue` 明确保留风险，并只能依靠独立 reviewer 和哈希绑定回执迁移状态，禁止手改状态冒充闭环。
- 原始数据只读，所有工作目录绑定 capture fingerprint。
- 发布前独立检查拓扑、遗漏、墙体交接、家具姿态、碰撞、地面和静态/三维视觉证据。
- 不依赖 GroundingDINO、TRELLIS、Blender 或模型 API。

## 新电脑快速开始

```powershell
git clone <PRIVATE_REPOSITORY_URL>
Set-Location ai-indoor-reconstruction-agent
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt

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
  渲染编译层（miter/T 交接与门窗真开洞）、V1 迁移与合成样例。见 [docs/SCENE-V2.zh-CN.md](docs/SCENE-V2.zh-CN.md)。
- `.codex/skills/reconstruct-indoor-scene/`：可复用 Agent 工作流、编排、证据审计和发布评分。
- `prototypes/litereality-three-redraw-20260812/`：通用 Three.js 产品界面与验收入口（自动识别 V1/V2 场景）。
- `tests/codex-scan/`、`tests/scene-v2/`：编排器门禁测试与 Scene V2 API/几何数值测试。
- `docs/`：跨电脑迁移、数据契约和协作说明。
- `data/`：仅作本地挂载点；内容被 Git 忽略。

详细流程见 [docs/WORKFLOW.zh-CN.md](docs/WORKFLOW.zh-CN.md)，迁移边界见 [docs/PORTABILITY.zh-CN.md](docs/PORTABILITY.zh-CN.md)。

## 验证

```powershell
python scripts/validate_portable_repo.py
python tests/codex-scan/bh-20260812-reconstruction-orchestrator.test.py
python tests/scene-v2/test_scene_api.py
node --test tests/scene-v2/scene-core.test.mjs
```

本仓库默认作为私有协作项目。没有数据收集授权前，不要提交客户原图、原始点云、注册码、API Token 或包含个人目录的绝对路径。
