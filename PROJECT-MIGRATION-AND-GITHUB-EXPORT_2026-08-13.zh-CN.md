# 项目迁移与 GitHub 导出记录（2026-08-13）

## 问题现象

室内重建代码、Skill、人工决议账本、质量门禁与轻量审查结果散落在 CloudStudio 共享仓库中，且过程文件夹同时包含约 40 MiB 可再生图像、点云二进制和本机绝对路径，不适合直接整体上传或跨电脑协作。

## 修改文件

- 新建独立仓库根目录及 `.gitignore`、`.gitattributes`、`.editorconfig`。
- 复制 `.codex/skills/reconstruct-indoor-scene/`、当前 Three.js 原型、区域决议包、轻量工作流账本和针对性测试。
- 新增 `README.md`、`docs/WORKFLOW.zh-CN.md`、`docs/PORTABILITY.zh-CN.md`、`CONTRIBUTING.md`、`SECURITY.md`。
- 新增 `requirements.txt`、GitHub Actions 与 `scripts/validate_portable_repo.py`。
- 调整便携版 `run-demo.ps1` 与 `viewer.js`，允许无点云时直接查看语义模型。

## 修改内容

- 排除 LAS/LAZ/E57/PLY/PCD/MCAP/BAG、紧凑点云 `.bin`、原始照片、视频、生成缓存、published 和 checkpoints。
- 最终 `scene.json`、专用生成器、坐标复盘、生成报告、审查图及 manifest 均在本机按需重建；Git 只保存通用 Agent 编排、证据工具、审查界面、流程契约、空白账本初始化能力和通用测试。每次采集的坐标决议账本、审核回执、点云叠加 PNG 与配置均绑定 capture fingerprint，留在独立工作目录，不进入框架仓库。
- 把源数据目录、本机仓库路径和用户目录替换为 `${DATASET_ROOT}`、`${REPO_ROOT}`、`${USER_HOME}`。
- 仓库门禁限制单文件不超过 5 MiB，并检查原始数据扩展名、UTF-8、乱码、本机绝对路径和常见 Token。
- Viewer 在便携场景缺少点云二进制时进入 semantic-model-only 模式，不阻断模型展示。

## 验证方式

- `python scripts/validate_portable_repo.py`
- `python prototypes/litereality-three-redraw-20260812/generate_demo.py --self-test`
- `python tests/codex-scan/bh-20260812-reconstruction-orchestrator.test.py`
- `node --test tests/codex-scan/*.test.mjs`
- 重新生成 top/oblique manifest 和 `wall-joint-review.json`，确认墙体接缝 34 个、问题 0。

## 当前状态

本地便携仓库整理和验证完成；GitHub CLI 已保存的两个账号 Token 均失效，远端创建/推送需要重新授权，不能把本地成功冒充为 GitHub 上传成功。
