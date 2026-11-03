# Phase 10 v6：新版真实三组联测准备（2026-10-27）

为已修复的阶段职责、Reviewer 依据和实验恢复准备独立 v6 验收。
本轮先完成免费验证和确切清单，新增供应商请求 **0**；付费执行等待单独确认。
原 v5 恢复失败及其他历史结果保持原值，Phase 10 整体验收仍未通过。

## 版本与范围

业务源码沿用 dev `7cb579d` 已验证的修复；本次生产源码变更仅限四个评测入口/传输文件。
v6 新增阶段职责摘要及审查/恢复规则绑定，使用新的 `live-repair-v6-claim.json` 和独立授权台账。
v3–v5 的入口合同在本次改动前后摘要相同；旧调用、结果未知记录和剩余额度不复用或重置。
清单保护 v3/v4/v5 的已消耗声明、授权和历史原始记录，包括 SQLite、源码、checkpoint 等文件。

在干净提交上生成的提案绑定源码、公开夹具、运行时 schema、阶段职责及旧证据摘要；
执行前和每次模型调用前重新核验。旧结果被修改、绑定变化、重复声明均拒绝执行。
本次不修改 Agent 业务代码、夹具、独立 oracle、判分规则或验收门槛。

## 拟确认的真实调用清单

| 项目 | 范围或上限 |
|---|---|
| 测试组 | 正常 baseline、故障恢复 baseline、故障恢复 No Replanning |
| 新增 LLM | 总计最多 36 次，每组最多 12 次；所有纠正调用计入 |
| Tavily | 0 次，研究使用公开闭集语料 |
| 每组 token | 120000-token 停止阈值；最后一个响应可能越过阈值 |
| 单次输出 | 最多 8192 token |
| 请求体 | 32768 字节；完整源码不截断，写入预览保留 1024 字节反馈空间 |
| 发送内容 | 公开 MNIST 目标、模板源码、公开检索证据、验证及实验结果 |
| 保密内容 | 不发送密钥、`.env`、设计文档或私人用户资料 |
| 失败处理 | 不自动重跑整组、不扩大额度；供应商/未知用量/记账/隔离异常停止 |
| 结果范围 | 定向三组；不包含 15 组完整消融或 Phase 10 总验收 |

正常组须成功且代码只执行一次；恢复组须只注入一次收集故障，保持原源码及计划，
仅重跑实验一次，最终产物与独立 checkpoint 核验通过。
负对照须在同类故障后以 `evaluation_replanning_disabled` 停止，不追加训练或重规划；
其原任务结果仍是失败，按预期停止只计为负对照合同通过。

## 免费验证与执行准备

相关回归 **194 passed**，包括 v6 完整 Mock HTTP 入口、旧合同摘要、旧授权/结果保护、
阶段职责绑定、请求预检和业务恢复规则。Ruff、mypy（248 个文件）及本次 8 个文件格式检查通过。
生产 Agent 业务源码未改变，本次没有重复运行上一轮 1009 项完整后端回归。

三组免费 Docker 验证均满足预期合同：

| 场景 | 结果 | Coding / 训练执行次数 | Reviewer 纠正 |
|---|---|---|---:|
| normal-baseline | 成功，产物和独立 checkpoint 核验通过 | 1 / 1 | 0 |
| recover-baseline | 成功，同一代码、原计划不变，仅实验重试 | 1 / 2 | 1 |
| recover-no_replanning | 按预期停止，原任务失败保留 | 1 / 1 | 1 |

本地记录：`data/validation/phase10-v6-focused-results.xml`、
`data/validation/repairs-preflight-a09ef770/`。
子容器/临时卷和专属控制器/隔离网络已核验清理；应用资源、缓存镜像、公开 MNIST 及报告保留。
预检使用保存的 v5 计划和最后一份 Reviewer 响应，后续行为由显式脚本提供；
实际训练与独立 oracle 在内部网络的专属 Docker 资源中执行。
它验证入口、保护和实验链路，不替代新版真实模型决策的验收。

确切提案、可执行清单及 SHA-256 保存在忽略的 `data/evaluation/`，不上传 GitHub。
准备阶段不读取 `.env`，不创建 v6 声明或付费授权账本。
只有新增付费范围得到确认后，才读取所配置模型并调用以下入口：

```powershell
.venv\Scripts\python.exe -m autoscholar.evaluation repairs --protocol-version 6 --allow-paid --plan data/evaluation/repair-plan-<UUID>/plan.json --approval-id phase10-recovery-authority-v6-20261027 --sandbox-container autoscholar-eval-live-v6-20261027-sandbox-manager-1
```

付费运行将另建内部网络控制器，固定缓存镜像，不挂载 `.env`、设计文档或正常应用数据库。
已通过的业务修复此前已独立同步 main；本次评测入口与准备材料保留在 dev。
本报告不触发部署，也不改变按模块同步 main 的工作约定。

随后所有者明确允许并执行冻结清单：实际 29 次 LLM、0 次 Tavily，正常组和恢复组通过，
负对照未达到故障注入点，三组验收仍未通过。准备阶段的零请求记录保持原值；
真实运行的完整结果见 [v6 实测报告](../docs/releases/2026-10-27-v6-acceptance.md)。
