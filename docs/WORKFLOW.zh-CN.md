# AI 室内重建三层闭环

## 0. 三层场景与全局草模

任何新数据先在 30–60 分钟内完成全局 `Reconstruction Hypothesis`，再开始逐墙逐件校核：

- `scene-authority.json`：严格证据账本，只约束可声明的精度与证据等级；
- `scene-hypothesis.json`：完整空间假设，包含主轴、外壳、主要空间、连续视觉地面、主要墙带、通道、家具分区和推断扫描边界；
- `scene-presentation.json`：产品化展示模型，采用逻辑家具、连续表面、正常材质、灯光与构图。

缺少 measured 证据时，合理对象应保留为带置信度的推断，而不是从展示模型删除。证据模式用透明、虚线、`INFERRED` 和置信区间区分；展示模式仍保持空间连续可读。

首轮家具角度统一吸附到建筑主轴或其正交轴，记录原始角度与展示角度；只有独立点云或照片明确证明偏转时才允许例外。先保证同族平行整齐，再逐件微调对齐点云。

## 1. 发现与隔离

1. 识别单一 capture unit；多采集根目录必须人工选定，禁止按文件大小自动配对。
2. 计算 capture fingerprint，初始化独立 `work/` 和空白决议账本。
3. 抽查无标注照片和点云总览，确认是室内数据；缺点云直接阻断，缺照片只能进入 geometry-only。

## 2. 从完整假设向下校正，而不是从碎墙向上拼

1. 去除天花板，生成全量彩色正射、高结构、桌面、椅子和家具 X 光切片。
2. Agent 在静态图上拾取中心、端点、轴向和区域；算法提供局部剖面、尺寸与残差。
3. 对透明、遮挡和漏扫部分使用照片与空间拓扑推断，并明确 `accepted-inferred`。
4. 先求解全局主轴、墙族、空间连通、房间包络、通道和重复模数，再把测量墙段挂到空间上。
5. 每次修改 authority geometry 后同步更新 hypothesis 和 presentation，禁止把权威零件列表直接当成成品模型。

## 2b. 测量服务与参考视觉（Agent 是作者，算法是测量员）

几何决策由 Agent 做，确定性工具负责测量与复核：

- `level_survey.py`：RANSAC 地板/天花平面（抗倾斜、多层感知），优先于标量直方图 floorZ；
- `structural_proposals.py`：墙面提案；漂移双线经墙腔验空+颜色一致性自动合并（`driftMergedFrom`），配对墙必须复核 `cavityPointRatio`；
- `fit_service.py` / MCP `propose_wall`：Agent 画粗线（容忍 0.3 m/8°），服务用 RANSAC+IRLS 精化并回报 support/残差/对面厚度候选——只测量不写场景；
- `opening_candidates.py`：沿墙占用剖面自动提门窗候选；合并墙上的 `bridgedGaps` 是疑似被焊死的门洞；
- `wall_graph_adjust.py`：轴族/共线/角点闭合/墙厚族联合平差，输出前后位移表供 Agent 显式采纳；
- `wall_dossier.py` / `render_photo_overlay.py`：每墙一页参考档案（正射 + 剖面 + 线框投影到最近照片），建模前定位、建模后核对——线框没落在照片真实墙上就是最快的缺陷检测器；
- `capture_readiness.py validate-pose-reprojection`：逐帧投影-照片相关性门，坏帧单独剔除，PASS 后经 `revalidate-intake` 解锁照片关联能力；
- `semantic_candidates.py`：VLM/看图观察（像素框）只能经反投影产出 candidate 几何，接受前必须几何复核；
- Viewer 照片对齐模式（视锥点击进入、线框叠加照片、透明度滑杆；2:1 图自动全景球）与审查面板（状态色点、support/残差、证据预览、聚焦过滤）供人与 Agent 用同一视角核对。

## 3. 分区复核

每个区域至少核对：

- 墙、玻璃、门、窗、柱、地面和吊顶是否完整；
- 家具中心、角度、尺寸、家族轴与通行空间；
- 门洞/continuation 的拓扑语义；
- 结构与点云、照片、立面剖面是否一致；
- 是否存在漏扫但建筑逻辑上必须补全的部分。

## 4. 独立对抗审查

审查 Agent 不读取作者的结论分数，按 `raw -> model -> overlay` 顺序检查：

- 原始高结构轴数量是否被模型解释；
- 同族桌椅是否出现异常歪轴、离群尺寸或自证回归；
- 墙体接头是否有微缝、穿柱、重叠或错误端点；
- 程序化子件是否继承局部坐标，并位于父几何内部；
- 地面是否互斥、连续且不凭相机轨迹补面；
- 所有 REVIEW 是否仍然阻断发布，而不只是显示警告。

## 5. 发布门

只有以下内容全部绑定同一 scene SHA 后才允许发布：

- 区域遗漏复核和全局 omission sweep；
- topology、overlap、floor、facade、wall-joint、furniture clearance；
- 静态 top/oblique 图与其 renderer/output hash；
- reviewer、reviewedAt、证据文件存在性；
- 所有 blocking quality loop 为 PASS。

展示模型还必须通过首眼视觉拒绝门：空间数不能为 0；主要视觉地面不能无解释断开；主要墙不能悬空；不能被黑洞和孤立墙主导；主体必须占审核画面至少 65%；默认列表不得暴露零件 ID；逻辑家具必须折叠分组；交付页不得仍标 `EVIDENCE_ONLY`；首次观看者应能立即理解主要空间、通道和家具分区。

详细字段与命令以 `.codex/skills/reconstruct-indoor-scene/SKILL.md` 为准。
