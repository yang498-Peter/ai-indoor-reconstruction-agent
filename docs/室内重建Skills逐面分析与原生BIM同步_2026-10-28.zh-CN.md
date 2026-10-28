# 室内重建 Skills：逐面分析与原生 BIM 同步

## 问题与范围

可移植的点云对齐和 IFC 检查工具已进入主分支；逐面偏差报告和原生 BIM 编辑的可复用经验仍留在本地技能工作树。本次只整理通用技能指引，不提交客户采集数据、案例模型、现场记录或生成结果。

## 修改文件与内容

- `.agents/skills/reconstruct-indoor-scene/SKILL.md` 与 `.codex/skills/reconstruct-indoor-scene/SKILL.md`：按任务条件路由到逐面报告和原生编辑参考。
- `references/surface-deviation-reporting.md`：区分拟合残差、点云到模型偏差、覆盖率、采样和施工验收，并要求数据及报告版本绑定。
- `references/native-bim-editability.md`：区分 IFC 可读取、实际原生编辑、宿主关系和楼层联动，未执行的接收端验证保留 `NOT_RUN`。
- `references/bim-source-overlay.md` 与 `references/pointcloud-model-alignment.md`：接入上述专项指引，避免重复和混淆验收范围。

## 验证与当前状态

在与远端主分支树哈希一致的独立快照中，两套技能格式校验、`sync_skills.py --check` 和便携仓库检查通过；对齐工具的 6 项测试通过。IFC 工具测试因当前环境缺少 IfcOpenShell 等依赖未执行。文档不改变 Scene V2 公共契约，也不证明实测精度、实际软件导入或原生编辑验收。真实数据、目标版本的软件操作及施工验收须在具体项目中独立验证。
