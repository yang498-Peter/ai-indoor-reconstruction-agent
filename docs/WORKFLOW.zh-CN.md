# AI 室内重建闭环

## 1. 发现与隔离

1. 识别单一 capture unit；多采集根目录必须人工选定，禁止按文件大小自动配对。
2. 计算 capture fingerprint，初始化独立 `work/` 和空白决议账本。
3. 抽查无标注照片和点云总览，确认是室内数据；缺点云直接阻断，缺照片只能进入 geometry-only。

## 2. 静态拾取，而不是盲目自动识别

1. 去除天花板，生成全量彩色正射、高结构、桌面、椅子和家具 X 光切片。
2. Agent 在静态图上拾取中心、端点、轴向和区域；算法提供局部剖面、尺寸与残差。
3. 对透明、遮挡和漏扫部分使用照片与空间拓扑推断，并明确 `accepted-inferred`。
4. 每次修改 authority geometry 后重新生成模型，禁止只在 Viewer 中遮住错误。

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

详细字段与命令以 `.codex/skills/reconstruct-indoor-scene/SKILL.md` 为准。
