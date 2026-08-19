# Semantic Scene V2 —— 节点图 + 证据账本 + 变更 API

V2 吸收 [pascalorg/editor](https://github.com/pascalorg/editor)（MIT）的场景架构思想，同时保留本仓库自己的证据驱动重建体系。三层分离：

```text
Scene Graph（我们认为世界是什么）
    nodes: Record<id, Node>，parentId/children 层级
    Level → Wall → Door/Window（寄生，hostOffsetM 沿墙定位）
Evidence Ledger（为什么敢这么认为）
    evidence[nodeId] = { status, sources[{contentSha256,lineageId,roots}], reviewer, claimHash }
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
| `scene-core/mcp_server.py` | MCP stdio server：scene_api 的 23 个工具面，纯 stdlib |
| `scene-core/scene-core.js` | 编译层：joinery（miter/T 嵌入）、门窗真开洞、视图模型 |
| `scene-core/pointcloud_evidence.py` | 点云证据生成：正射/高程带切片/任意断面，含像素↔米映射 |
| `scene-core/detect_trees.py` | CHM 树候选检测 + 树干回波核验（只出候选） |
| `scene-core/render_scene_overlay.py` | 场景叠加正射的 QA 对比图 |
| `scene-core/make_sample_scene.py` | 合成样例（无需客户数据即可看 Viewer 全链路） |
| `scene-core/migrate_scene_v1_to_v2.py` | V1 scene.json → V2，门窗自动挂靠最近平行墙 |
| `tests/scene-v2/` | Python API/MCP 门禁测试 + Node joinery/编译数值测试 |

Viewer 实时能力：`viewer.html?scene=<路径>&watch=2` 轮询场景文件并原地热重建（相机不动），Agent 边建边看。

`sceneLayer=authority`（或缺省）中的 accepted 条目必须带当前 claim/reviewer receipt；缺失或 stale 时 Viewer 按 candidate 显示。显式 `sceneLayer=hypothesis|presentation` 可显示标注为 inferred 的连贯补全，但这两层不能通过 `quality_report_v2.py` 的 authority 发布门。

## Agent 标准工作流

```powershell
# 初始化
python scene-core\scene_api.py --scene work\scene.json --actor author-west --identity work\author-identity.json init --dataset capture-001

# 画墙（源平面米制）
python scene-core\scene_api.py --scene work\scene.json --actor author-west --identity work\author-identity.json create-wall --start 0,0 --end 8.5,0 --thickness 0.2 --id wall_south

# 门窗寄生到墙上（offset = 洞口中心距墙起点）
python scene-core\scene_api.py --scene work\scene.json --actor author-west --identity work\author-identity.json add-door --wall wall_south --offset 2.0 --width 0.95 --height 2.05

# 绑定证据（自动计算 sha256，文件不存在则拒绝）
python scene-core\scene_api.py --scene work\scene.json --actor author-west --identity work\author-identity.json attach-evidence --id wall_south --type high-structure-slice --path evidence\south-slice.png --source-role measurement --producer local-elevation-v1

# 接受（fail-closed：证据 hash/lineage、claimHash、独立只读 reviewer execution 均受约束）
python scene-core\scene_api.py --scene work\scene.json --actor reviewer-east --identity work\reviewer-readonly-identity.json accept --id wall_south --mode measured

# 查询 / 测量 / 校验 / 撤销
python scene-core\scene_api.py --scene work\scene.json find --status candidate
python scene-core\scene_api.py --scene work\scene.json measure --id wall_south
python scene-core\scene_api.py --scene work\scene.json validate
python scene-core\scene_api.py --scene work\scene.json --actor author-west --identity work\author-identity.json undo

# 原子批量（任一步失败则整批不落盘）
python scene-core\scene_api.py --scene work\scene.json --actor author-west --identity work\author-identity.json apply-patch --patch ops.json
```

不变式（由 API 强制，不靠自觉）：

- 洞口必须完整落在宿主墙内、不高于墙、互不重叠；
- `accept --mode measured` 要求至少一个存在且 hash 一致的证据文件；
- `accept --mode inferred` 要求 reason + 至少两个内容 hash 不同且根集合不相交的已验证文件；同文件重复引用、同根裁剪或部分共享上游根都不算独立证据；
- 复核 actor 与 execution run 都必须独立，reviewer 使用只读策略；P0/P1 还要求 regional/adversarial class；
- 接受时写入当前几何/拓扑 `claimHash`；修改节点、宿主墙、寄生洞口或坐标系会把受影响声明打回 `candidate`；
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
python tests\contracts\test_stage_contract.py
python tests\contracts\test_v2_publish_contract.py
node --test tests\scene-v2\scene-core.test.mjs
python scripts\validate_portable_repo.py
```

样例场景所有接受节点引用 `synthetic-sample` 类型回执——明确标记为演示，不冒充实测。

## 九阶段 Pipeline V2

`schemas/pipeline-contract-v2.json` 是阶段顺序、依赖、能力、typed artifact、专用 evaluator 和失效传播的唯一机器契约：

`intake → evidence → macro-hypothesis → seed → author → presentation-review → regional-review → global-review → publish`

- `stage` 只能记录 `IN_PROGRESS`、`REVIEW`、`BLOCKED` 或 `FAILED`，不能直接写 `PASS`；
- `evaluate-stage` 根据契约逐项验证当前 typed artifact、payload 检查向量、producer/config/environment/input hash 和 authority SHA；
- V1 状态稳定返回 `PIPELINE_STATE_MIGRATION_REQUIRED`，必须执行 `migrate-state`；迁移保留 capture/job 绑定及 intake 结论，下游全部重新评估，旧 issue 会重开并标记 `LEGACY_REVIEW_IDENTITY_RECHECK_REQUIRED`，不会沿用 actor-only 的 RESOLVED；
- authority 变化会失效 author 之后的三层 review 与 publish；presentation-only/renderer-only 变化不会倒灌失效 evidence、macro、seed 或 authority；
- 状态写入使用独占锁、revision compare-and-swap 和原子替换；并发旧写者返回 `STATE_REVISION_CONFLICT`。

```powershell
python .codex\skills\reconstruct-indoor-scene\scripts\reconstruction_loop.py migrate-state --state work\pipeline-state.json --actor migration-owner
python .codex\skills\reconstruct-indoor-scene\scripts\reconstruction_loop.py evaluate-stage --state work\pipeline-state.json --actor evidence-owner --execution work\author-identity.json --name evidence --artifact evidence-bundle=work\evidence-bundle.json
python .codex\skills\reconstruct-indoor-scene\scripts\reconstruction_loop.py status --state work\pipeline-state.json
```

## V2 评估与发布

```powershell
python scene-core\quality_report_v2.py --scene work\scene-authority.json --review work\review-receipt.json --output work\quality-report.json
python .codex\skills\reconstruct-indoor-scene\scripts\reconstruction_loop.py publish --state work\pipeline-state.json --actor publish-gate --execution work\publisher-identity.json --scene work\scene-authority.json --review work\review-receipt.json --quality-report work\quality-report.json --output work\publish
```

- `geometryDigest` 只绑定稳定的权威几何；`evidenceSetDigest` 单独绑定证据账本；`artifactSha256` 绑定场景文件字节；
- 发布会重新运行同一 V2 evaluator，并逐字段比较报告；
- V1 输入稳定返回 `LEGACY_SCENE_REQUIRES_MIGRATION`，必须先显式迁移；
- 旧评分器已移至 `scripts/legacy/score_scene_v1.py`，只保留历史样例兼容入口，不再参与 V2 发布。

## 已知边界（后续任务）

- `verify_acceptance.py` 仍是 V1 prototype 的历史验收入口；V2 authority 必须走 `quality_report_v2.py`，两者不静默互转；
- Annotator 仍产出 V1 picks；下一步让它直接产出 `scene_api` 的 patch 文件（证据拾取 → 变更 API 的闭环）;
- 曲面墙、斜墙顶、多层（multi-level）尚未进 schema；坡屋面暂以 `roof-panel` item 表达，未来应升级为 roof 节点；
- 同一平面区间上下堆叠的洞口（窗下检修孔）在权威模型合法且校验通过，但 `splitWallParts` 按沿墙区间切分，渲染时下部孔洞会被上部洞口的窗下墙实体遮住——账本正确、渲染近似；
- CLI 负数参数需用 `--flag=value` 形式（argparse 前导 `-` 限制）。
