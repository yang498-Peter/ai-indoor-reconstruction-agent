# 跨电脑迁移与数据边界

## 安装发行包

从 0.9.0 起，运行工具无需完整 checkout 或 Git。发行物为 `indoor_reconstruction_agent-0.9.0-py3-none-any.whl`、源码包及 `constraints-runtime.txt`。Python 3.11/3.12 的 Windows/Linux 是验证目标；其他平台需要另行验证依赖轮子和系统运行库。

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -c constraints-runtime.txt "./indoor_reconstruction_agent-0.9.0-py3-none-any.whl[presentation]"
.\.venv\Scripts\indoor-recon.exe doctor --json
.\.venv\Scripts\indoor-recon.exe smoke --work smoke-001
```

Linux/macOS 的命令路径为 `.venv/bin/`；macOS 不属于本次自动化验证结论。基础 CLI/MCP 可以省略 `[presentation]`；IFC、网格和完整合成冒烟使用该 extra。约束文件来自同一 `uv.lock`，避免另一台电脑无意升级运行依赖。完整权威流程若调用 JavaScript 几何检查，需自行安装 Node 22，未提供时保持对应门禁阻断。

`doctor` 校核安装包的资源清单和哈希，并返回 `installed` 或开发态 `source`。资源与当前工作目录无关；缺失或损坏时明确失败，不借用旁边 checkout。不要把客户文件放入 site-packages，输出放到显式工作目录。

新机器接续已有建模成果时，另外迁移私有原始数据、工作目录和交付清单，核对源、变换、生成器、模型和回执哈希。既有工作记录可能绑定旧路径，不能仅复制一个 job.json 就宣称已恢复；路径变化后的重新发现或适配必须沿用原门禁，禁止手改生成的状态或把旧回执升级为当前接受。

开发者仍可按下文使用源码环境。发行包通过明确的 `runtime-files.json` 和构建钩子携带公共运行资源；本机未跟踪的脚本、模型、点云、客户图片、缓存不会被目录遍历收入发行物。

## Git 中保留

- Python/JavaScript/HTML/CSS/PowerShell 源码；
- 室内重建 Skill、参考规范与编排器；
- 数据无关的参考、模板和合成测试生成器；
- 只包含合成数据的示例；小于 5 MiB 不是客户数据可以公开的条件；
- 测试、CI、依赖清单和文档。

## Git 中排除

- LAS/LAZ/E57/PLY/PCD/MCAP/BAG 与紧凑点云 `.bin`；
- 原始相机照片、视频、SDK 输出和临时切片；
- `generated/` 的可再生中间证据、`published/` 和 checkpoints；
- 采集专属坐标、布局、模型、照片及其派生审查材料；
- 本机绝对路径、账号 Token、`.env` 与任何客户隐私数据。

## 新电脑恢复

1. 按上文安装发行包并运行 doctor、smoke；只有开发源码时才需要 clone。
2. 通过受控渠道把 capture 放到本机 `data/` 或外部磁盘。
3. 运行 discovery，人工选择 capture unit 并确认 indoor domain。
4. 用新 work 目录生成 capture manifest；不得复用另一数据集的账本。
5. 运行生成器和独立静态审查，最后再启动 Viewer。

`generated/`、工作目录和客户模型不是运行本仓库零数据验证的先决条件；合成样例由已提交的生成器产生。不要为通过迁移验证把现有客户输出加进 Git。

## 展示建模与 BIM 工具环境

推荐先使用 Python 3.12、Git；浏览器展示需要支持 WebGL 的浏览器。Node 22 用于 JavaScript 测试，MVPStudio、SketchUp 和供应商 SDK 是独立软件，不随本仓库分发。

Windows PowerShell（从仓库根目录执行）：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-presentation.txt pytest
.\.venv\Scripts\python.exe -m pip check
.\.venv\Scripts\python.exe scripts/sync_skills.py --check
.\.venv\Scripts\python.exe -m pytest -q tests/test_presentation_evidence_fast.py tests/test_presentation_alignment.py tests/test_presentation_ifc.py tests/test_presentation_delivery_inventory.py tests/test_presentation_brand_asset.py
.\.venv\Scripts\python.exe scripts/smoke_presentation.py --work ..\presentation-smoke-001
```

Linux/macOS 使用 `.venv/bin/python` 替换 Python 路径。`smoke` 必须使用不存在的目录，不删除或覆盖旧结果；它生成两套不同原点和轴向的合成 LAS、GLB、缓存、查询与 IFC，验证缓存复用、实际场景节点变换和 IFC 世界坐标。它不启动后台服务、不上传数据、不依赖其他项目或特定盘符，也不表示真实场景已建模或在外部 BIM 软件中验收。

`requirements.txt` 是基础证据/Scene V2 环境；`requirements-presentation.txt` 增加 trimesh、IfcOpenShell 和 LAZ 解码器。原生 SKP 仍需可用的 SketchUp/官方 SDK；没有时按用户接受的范围交付 DAE + 纹理包，不修改扩展名伪装为 SKP。

展示环境通过 `constraints-presentation-py312.txt` 约束已测试的依赖版本。升级时同时运行相关测试和合成冒烟，再更新约束；其他 Python 大版本和 CPU 架构需要另行确认依赖轮子可用性。该文件不是供应商 SDK 或外部软件许可证。

开发态使用仓库中的 canonical skill：`.agents/skills/reconstruct-indoor-scene/SKILL.md`。发行包也携带该工作流及其参考资源，但 **单独复制一个 SKILL.md 仍不构成完整安装**。安装用户通过 `indoor-recon` 调用工具并按本文与 SOP 编排；编辑和同步技能源码时才使用 checkout。现有 `.codex/skills/` 用于兼容，不另行生成一套会漂移的副本。`sync_skills.py --check` 校核入口和本地参考，不是 GitHub 上传命令。

## 已提供的可复用工具

| 工具 | 输入与作用 | 不负责的内容 |
| --- | --- | --- |
| `scripts/indoor_presentation_evidence.py` | 明确的 LAS/LAZ、单位/轴向 → 绑定缓存、区域原始切片和照片接触表 | 自动房间分类、相机配准、自动建模 |
| `scripts/presentation_alignment.py` | 绑定缓存 + 当前/可选旧 GLB + 显式 ROI/轴变换 → 批量局部表面观察 | 自动判断柜面/墙面、毫米级精度验收 |
| `scripts/validate_presentation_ifc.py` | IFC → schema/EXPRESS、几何回读和世界包围盒 | 通用 IFC 导出、开洞完备性、独立精度或接收软件验收 |
| 技能 `scripts/inspect_brand_asset.py` | Logo 原图 → 透明边界、比例和放置参数参考 | 猜测现场位置、重绘品牌图 |
| 技能 `scripts/delivery_inventory.py` | 交付目录 → 快照、差异和哈希检查 | IFC 内容或真实网页交互验收 |

对齐参数与调用示例见[点云与模型对齐](../.agents/skills/reconstruct-indoor-scene/references/pointcloud-model-alignment.md)，建筑/源坐标和 SketchUp 交换见[精修交付参考](../.agents/skills/reconstruct-indoor-scene/references/refinement-and-delivery.md)。每个新场景仍需根据证据建立生产器、宿主开洞、楼层和导出映射。专用 No4/AG/展台生产器含客户布局，未被当作通用工具发布。

## 验证范围

源码可迁移、合成工具通过、真实场景效果、浏览器交互、接收软件和现场尺寸验收分别记录。CI 在 Windows 与 Linux 安装声明依赖并运行上述工具测试和合成冒烟；某次 CI 的成功不能保证任何输入都自动生成准确模型。性能优化以相同源/变换下减少重复读取和选择性重建为依据，不承诺跨硬件固定时间或精度百分比。
