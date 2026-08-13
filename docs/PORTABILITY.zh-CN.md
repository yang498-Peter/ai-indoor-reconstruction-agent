# 跨电脑迁移与数据边界

## Git 中保留

- Python/JavaScript/HTML/CSS/PowerShell 源码；
- 室内重建 Skill、参考规范与编排器；
- 结构/家具决议 JSON、区域候选 JSON/Markdown；
- 小于 5 MiB 的最终语义 `scene.json`、审查清单、最终 top/oblique 图片；
- 测试、CI、依赖清单和文档。

## Git 中排除

- LAS/LAZ/E57/PLY/PCD/MCAP/BAG 与紧凑点云 `.bin`；
- 原始相机照片、视频、SDK 输出和临时切片；
- `generated/` 的可再生中间证据、`published/` 和 checkpoints；
- 本机绝对路径、账号 Token、`.env` 与任何客户隐私数据。

## 新电脑恢复

1. Clone 私有仓库并创建 Python 虚拟环境。
2. 通过受控渠道把 capture 放到本机 `data/` 或外部磁盘。
3. 运行 discovery，人工选择 capture unit 并确认 indoor domain。
4. 用新 work 目录生成 capture manifest；不得复用另一数据集的账本。
5. 运行生成器和独立静态审查，最后再启动 Viewer。

仓库中的 `generated/scene.json` 是便携式语义模型演示，不含点云；接入真实数据后会被本地输出替换，但默认不会进入 Git。
