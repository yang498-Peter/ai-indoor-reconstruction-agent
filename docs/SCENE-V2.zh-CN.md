# Semantic Scene V2 —— 节点图 + 证据账本 + 变更 API

V2 吸收 [pascalorg/editor](https://github.com/pascalorg/editor)（MIT）的场景架构思想，同时保留本仓库自己的证据驱动重建体系。三层分离：

```text
Scene Graph（我们认为世界是什么）
    nodes: Record<id, Node>，parentId/children 层级
    Level → Wall → Door/Window（寄生，hostOffsetM 沿墙定位）
Evidence Ledger（为什么敢这么认为）
    evidence[nodeId] = { status, sources[{type,path,sha256}], reviewer, reason }
    candidate / accepted-measured / accepted-inferred / rejected
Review State（还有什么没闭环）
    review.issues / qualityLoops / topology
```

权威坐标是源平面米制（x, y，Z-up）。显示映射只存在一处（`scene-core.js`）：
`display = [x, elevation, -y]`。

## 文件

| 文件 | 职责 |
| --- | --- |
| `scene-core/semantic-scene-v2.schema.json` | V2 契约（唯一权威 schema，消除 V1 的 schema drift） |
| `scene-core/scene_api.py` | Agent 变更 API：所有编辑走命令，不再手改 JSON |
| `scene-core/scene-core.js` | 编译层：joinery（miter/T 嵌入）、门窗真开洞、视图模型 |
| `scene-core/make_sample_scene.py` | 合成样例（无需客户数据即可看 Viewer 全链路） |
| `scene-core/migrate_scene_v1_to_v2.py` | V1 scene.json → V2，门窗自动挂靠最近平行墙 |
| `tests/scene-v2/` | Python API 门禁测试 + Node joinery/编译数值测试 |

## Agent 标准工作流

```powershell
# 初始化
python scene-core\scene_api.py --scene work\scene.json --actor author-west init --dataset capture-001

# 画墙（源平面米制）
python scene-core\scene_api.py --scene work\scene.json --actor author-west create-wall --start 0,0 --end 8.5,0 --thickness 0.2 --id wall_south

# 门窗寄生到墙上（offset = 洞口中心距墙起点）
python scene-core\scene_api.py --scene work\scene.json --actor author-west add-door --wall wall_south --offset 2.0 --width 0.95 --height 2.05

# 绑定证据（自动计算 sha256，文件不存在则拒绝）
python scene-core\scene_api.py --scene work\scene.json --actor author-west attach-evidence --id wall_south --type high-structure-slice --path evidence\south-slice.png

# 接受（fail-closed：无有效证据文件、hash 不符、作者=复核人 都会拒绝）
python scene-core\scene_api.py --scene work\scene.json --actor reviewer-east accept --id wall_south --mode measured --reviewer reviewer-east

# 查询 / 测量 / 校验 / 撤销
python scene-core\scene_api.py --scene work\scene.json find --status candidate
python scene-core\scene_api.py --scene work\scene.json measure --id wall_south
python scene-core\scene_api.py --scene work\scene.json validate
python scene-core\scene_api.py --scene work\scene.json undo

# 原子批量（任一步失败则整批不落盘）
python scene-core\scene_api.py --scene work\scene.json apply-patch --patch ops.json
```

不变式（由 API 强制，不靠自觉）：

- 洞口必须完整落在宿主墙内、不高于墙、互不重叠；
- `accept --mode measured` 要求至少一个存在且 hash 一致的证据文件；
- `accept --mode inferred` 要求 reason + 至少两个独立 source；推断永远不能冒充实测；
- 复核人必须不同于节点作者（`meta.createdBy`）；
- 修改已接受节点的几何会自动把它打回 `candidate`；
- 每次落盘前做结构校验，失败不写盘；每次落盘自动快照上一版（`undo` 可回退）。

## 编译层（渲染几何从权威模型推导，不存储）

- **两墙 L 角**：偏移面线求交得精确 miter 角点，近共线钳制回退；
- **T / X / 三岔**：端头按对方半厚嵌入交点，视觉无缝且不跨洞口；
- **门窗开洞**：墙实体按洞口切成侧柱/过梁/窗下墙 prism，墙上是真洞；
- 由此**替代**旧的 12–24 mm 同材质渲染 overlap hack；V2 场景下
  `canvas.dataset.solidWallJointContract = 'derived-joinery-v2'`，overlap 数恒为 0。

Viewer 检测到 `schemaVersion: "2.0"` 自动走编译层；V1 场景走原路径，行为不变。

## V1 迁移

```powershell
python scene-core\migrate_scene_v1_to_v2.py --input old\scene.json --output work\scene-v2.json
```

- 门窗自动挂靠最近平行墙（±10°、横向 ≤ 半厚+0.15 m）；挂不上的保留 `freeSegment`，不静默丢弃；
- 旧接受状态迁入账本并标注 `inference-basis` 来源——保持"遗留"可见；任何新的接受必须重新过证据门禁。

## 样例与验证

```powershell
# 无需任何数据，新机 clone 后直接看全链路
.\prototypes\litereality-three-redraw-20260812\run-demo.ps1 -Sample

# 测试
python tests\scene-v2\test_scene_api.py
node --test tests\scene-v2\scene-core.test.mjs
python scripts\validate_portable_repo.py
```

样例场景所有接受节点引用 `synthetic-sample` 类型回执——明确标记为演示，不冒充实测。

## 已知边界（后续任务）

- `verify_acceptance.py` 与 `score_scene.py` 仍消费 V1 视图模型；V2 场景先经编译层导出或待其移植；
- Annotator 仍产出 V1 picks；下一步让它直接产出 `scene_api` 的 patch 文件（证据拾取 → 变更 API 的闭环）;
- 曲面墙、斜墙顶、多层（multi-level）尚未进 schema；
- MCP server 包装（把 `scene_api` 子命令暴露为 MCP tools）尚未做，CLI 已按每命令一操作设计，包装是薄层。
