# Phase 10 v5：验收准备与免费三组预检

请求预检修复后，新增独立 v5 验收入口，重新验证正常、故障恢复和关闭重规划三组。
本轮完成准备和免费预检，**真实供应商请求 0**，尚未执行 v5 付费验收。

## 版本与证据约束

Agent 业务代码沿用 `7de7ef7` 的已测试修复。本次源码变更限于四个已有评测入口/传输文件，
不修改业务流程、夹具、oracle 或判分规则。v3/v4 合同保持原值，v5 使用新的
`live-repair-v5-claim.json`、独立 scope、提案、执行计划及账本；旧声明和余额不复用。

v5 提案绑定干净的已提交源码、公开夹具、运行时结构，以及历史证据的原始 SHA-256。
支持校验历史 SQLite、源码、checkpoints 等二进制/非 JSON 文件；路径必须位于评测输出
目录内，读取有大小上限。旧 v3/v4 两份声明均须保留在保护列表中。
执行前及每次模型调用前重新校验绑定，发生变化即停止。

输入仍限制为 **32768 字节**，输出仍最多 **8192 token/次**；完整源码不截断，
文件写入预览保留 1024 字节反馈空间。已有未知结果不改写为已知，本地拒绝不伪造供应商调用。

## 免费验收结果

入口、物理账本、重放保护、源码/历史记录绑定、请求预检、Reviewer 和恢复相关回归：
**248 passed**。Ruff、mypy（244 文件）和修改文件格式检查通过。
JUnit：`data/validation/phase10-v5-focused-results.xml`。
上一轮完整业务回归仍为 **967 passed / 3 skipped**；本轮未重复宣称一遍全套回归。

使用保存的 v3 Reviewer 响应、显式脚本模型和真实 Docker/MNIST 完成三组验证：

| 场景 | 实际流程 | 免费验证结果 |
|---|---|---|
| normal-baseline | 研究/代码/训练各一次；一次有限审查纠正；独立 checkpoints 复算 | 通过 |
| recover-baseline | 研究/代码各一次；注入一次指标故障；仅实验重跑，训练共两次；独立复算 | 通过 |
| recover-no_replanning | 研究/代码/训练各一次；注入一次故障；无重规划和第二次训练 | 按预期以 `evaluation_replanning_disabled` 停止 |

这三组通过表示脚本预检符合各自合同，负对照的原任务结果仍为失败。
免费记录：`data/validation/repairs-preflight-4eac4986/`。
专属子容器/临时卷和控制器/隔离网络均已按所有权清理；缓存镜像、公开数据卷和应用栈保留。
模型脚本不是新的真实模型能力证据，Mock HTTP 的模拟 token 不计费。

## 待单独批准的真实验收

| 约束 | 上限或范围 |
|---|---|
| 三组 | 正常 baseline、故障恢复 baseline、故障恢复 No Replanning |
| 新增真实访问 | 合计最多 36 次 LLM；每组最多 12 次；Tavily 0 次 |
| 每组 token | 120000-token 停止阈值，最后一次返回可能越过阈值 |
| 输出 | 每次最多 8192 token；纠正调用计入上限 |
| 内容 | 公开 MNIST 目标、模板源码、公开闭集证据和测试结果 |
| 失败处理 | 不自动重跑整组，不重置声明或余额；未知用量保留未知并停止 |
| 排除内容 | 密钥、`.env`、私人设计文档、非公开用户文档 |

提案和执行计划保存在忽略的 `data/evaluation/`。准备过程不读取 `.env`，
不创建 v5 声明或授权账本，不发送 API 请求。执行必须绑定单独审核的确切清单，例如：

```powershell
.venv\Scripts\python.exe -m autoscholar.evaluation repairs --protocol-version 5 --proposal data/evaluation/acceptance-v5-proposal-<UUID>/plan.json
```

只有所有者明确批准新增付费范围，且专属固定镜像控制器准备完成后，才可执行：

```powershell
.venv\Scripts\python.exe -m autoscholar.evaluation repairs --protocol-version 5 --allow-paid --plan data/evaluation/repair-plan-<UUID>/plan.json --approval-id phase10-request-preflight-v5-20261023 --sandbox-container autoscholar-eval-live-v5-20261023-sandbox-manager-1
```

三组定向验收、完整消融、Phase 10 整体验收及合并 main 是分别核对的条件。
本轮 `phase10_overall_acceptance=false`、`main_merge_authorized=false`；设计文档、
`.env`、原始请求/响应和运行产物不提交 GitHub。
