# AutoScholar — Phase 10 验收报告

日期：2026-11-03。**当前结论：评测工程与既有组件证据齐备，最新真实配对试验未完成，Phase 10 总验收未通过。**
本报告公开汇总结果；原始响应、数据库、产物、账本和本地设计文档不上传。

## 已完成的必要工作

| 范围 | 已核实结果 |
|---|---|
| 统一评测、六类组件和受控工程消融 | 历史工程整包 155/155 检查通过，原始记录重新审计一致 |
| 真实缓存语义检索与 Reranker 对照 | 质量门槛通过，实际推理结果与原始摘要一致；不要求重排收益显著 |
| 有界供应商能力 | 历史有效覆盖 10/10，保留不同版本、分组验证及原失败记录 |
| Snapshot schema 反馈 | 免费强制错误重放、真实 Docker 检查/训练通过；v13 正常组也实际完成有界纠错 |
| 最新真实正常场景 | 同一冻结版本下 5/5 配置任务成功，执行、源码交接和独立产物核验均通过 |
| 最新故障恢复 | v14 的四个可恢复配置成功，关闭重规划的负对照按预定错误停止且只训练一次 |
| 最新伪造指标检测 | v14 基线与关闭 Planner 组均由独立 oracle 拒绝伪造结果 |
| 证据和清理 | v14 的 92 份用量核对一致；13 个 SQLite 只读检查通过；1591 个文件受保护；独占资源已清理 |

以上历史组件结果是对应冻结版本的工程证据，不冒充本次全套重新测量。
历史汇总来源：`pack-660be828d0564c7d942fa019458eb1cd`、
`closure-51779bc514214d148fa4ec43eecfe425`；本轮免费审计新增供应商请求为 0。

## 最新 v14：12 组达到预期，完整矩阵仍未完成

范围见 [v14 冻结清单](docs/releases/2026-11-03-v14-fixed-dag-paired-scope.md)。
源码为 `f505c682f4ea8118ab2aa87b8e5987ba6330455d`，模型配置、终端、资源及公开输入与前轮保持一致。

| 任务 | 物理 LLM 请求 | 观测 |
|---|---:|---|
| normal-baseline | 8 | 成功，执行检查通过 |
| normal-no_planner | 6 | 成功，执行检查通过 |
| normal-no_reviewer | 6 | 成功，执行检查通过 |
| normal-no_memory | 7 | 成功，执行检查通过 |
| normal-no_replanning | 7 | 成功，执行检查通过 |
| recover-baseline | 10 | 恢复成功；保留有效上游，只重试实验，训练两次 |
| recover-no_planner | 8 | 恢复成功 |
| recover-no_reviewer | 7 | 恢复成功 |
| recover-no_memory | 9 | 恢复成功 |
| recover-no_replanning | 7 | 负对照通过：一次故障后 `evaluation_replanning_disabled`，训练一次 |
| deception-baseline | 8 | 独立 oracle 拒绝伪造指标，任务失败属于预期观测 |
| deception-no_planner | 7 | 独立 oracle 拒绝伪造指标，任务失败属于预期观测 |
| deception-no_reviewer | 2 | 根计划协议错误；原 `task_success=null` 保留 |
| deception-no_memory | 0 | 未执行 |
| deception-no_replanning | 0 | 未执行 |

本轮新增 **92 次 LLM、0 次搜索**，全部 HTTP 200，未结束请求为 0。
输入 300792、输出 93551，合计 **394343 token**；用量全部已知且与持久化预算一致，金额无可核验单价而保持未知。
正常、恢复、伪造指标三个基线控制与关闭重规划负对照均通过；它们不等同于 15 组完整测量。
No Planner 已有 3/3 配对，其余三个对比各有 2/3，整组 Holm 校正仍未完成，不能声称普遍组件收益。

### 本轮停止与字段修复

第 13 组连续两次根计划都返回正确的三种步骤，但所有步骤均漏掉机器可读的 `dependencies` 数组。
当时发给模型的通用 `TaskPlan` schema 将该字段列为可选，而固定线性 DAG 校验必须检查实际依赖边。
模型在一次纠错后仍未补齐，触发原停止规则；未启动该组子任务，剩余两组不再派发。
这是已收到、已计量的输出协议错误，不是 HTTP 503 或未知供应商用量。

本轮结束后已免费修复：仅为固定试验复制并收紧 wire schema，明确三种步骤、恰好三步及必填的依赖数组；
反馈指出每一步都应提供具体前驱 ID。通用平台 schema 不改，不替模型补边，不放宽校验、不增加纠错额度。
该修复的定向回归 **63 passed**，Ruff/mypy 通过；原始 v14 结果不重评。

新声明版本的独立 main 组合树 `da7398807510c6474b9d82519d819aeb226e20ed` 完整回归
**1268 passed、3 skipped**，Ruff/mypy（273 文件）通过。
随后依赖字段修复在独立 main 组合树 `c9652a4` 上的 **15 项定向检查通过**，包含完整模拟矩阵；Ruff/mypy 通过。
这 15 项与前述 63 项定向回归有重叠，不把两者相加冒充新的完整后端回归。
原 1148 个文件未变，新保护索引覆盖 **1591 个文件**；13 个数据库只读完整性及摘要核对通过，专属资源已清理。
本轮剩余 88 次空间不自动复用，也未追加付费重跑。

本地证据不上传：`data/evaluation/live-paired-v14-e5a6921c11c7429c8edffb41dafa58e9/`、
`data/validation/live-v14-protected-20261103.json`、`phase10-v14-final-acceptance-20261103.json`、
`v14-dependency-diagnosis-20261103.json`、`v14-dependency-fields-20261103.xml` 与 `v14-dependency-main-20261103.xml`。

## 历史 v13 真实配对测量（原值保留）

执行范围见 [固定清单](docs/releases/2026-11-03-v13-final-paired-scope.md)。
测量源码为 `4c2615a34cfe7333fe599b9f7a0041e6168b46d5`，新轮上限 180 次 LLM / 0 次搜索，每组最多 12 次。
未增加独立付费预检，也没有 HTTP 自动重试或整组自动重跑。

| 任务 | 物理 LLM 请求 | 任务结果 | 原生纠错 |
|---|---:|---|---:|
| normal-baseline | 7 | 成功，执行检查通过 | 0 |
| normal-no_planner | 7 | 成功，执行检查通过 | 1 |
| normal-no_reviewer | 8 | 成功，执行检查通过 | 1 |
| normal-no_memory | 7 | 成功，执行检查通过 | 0 |
| normal-no_replanning | 8 | 成功，执行检查通过 | 0 |
| recover-baseline | 3 | 失败，未进入预定训练故障 | 0 |
| 其余恢复/伪造指标任务 | 0 | 9 组未执行 | — |

实际新增 **40 次 LLM、0 次 Tavily / Semantic Scholar**，全部 HTTP 200，未结束请求为 0。
输入 119786、输出 28896，合计 **148682 token**；无可核验单价，金额保持未知。
正常场景完成五次真实 CPU 训练；这是公开封闭语料、预置代码和 MNIST 128/128 子集、一次 epoch 的工程试验，
不代表开放网络研究、从零编码或完整 MNIST 性能基准。

### 恢复基线停止原因

模型返回的根 DAG 是 `knowledge → coding → experiment`，偏离本轮明确要求的 `research → coding → experiment`。
它在通用平台 schema 中合法，因此进入 Knowledge 分支，并以 `rag_query_failed` 失败；下游依赖失败后在重规划阶段以
`autonomous_budget_exceeded` 停止。该组只消耗 11521 token，不是耗尽本组 120000-token 阈值。
没有执行代码检查、训练或故障注入，不能解释为“实验恢复能力已失败或通过”。

按原冻结协议的基线停止规则，剩余九组不再派发。每个对比只有 normal 的 1/3 配对；
完整差值检验与 Holm 校正保持未完成，不补零、不把旧版本结果拼入本轮。
本轮 `paired_measurement_complete=false`、`paired_pilot_acceptance=false`，剩余 140 次空间不自动复用。

原 951 个历史文件未变，新索引保护 **1148 个文件**；账本、声明、原始响应、六个数据库、诊断和清理记录保持原值。
本地证据：`data/evaluation/live-paired-v13-fc6b4e56965846d19caed9bc607e64a4/`、
`data/validation/live-v13-protected-20261103.json`、`phase10-prior-final-20261103.json`。

## 本轮发现后的免费修复

1. 固定试验在启动任何子任务前检查三类步骤及依赖结构；偏离计划最多占用原有的一次已计量纠错，
   不修改模型计划、不自动替换步骤、不改变通用 Knowledge 功能，也不重置调用额度。
   协调器对该明确的契约错误返回固定反馈，避免错误地提示为 JSON 格式问题。
2. 模拟入口的临时目录遵守显式短 `--basetemp`，避免独立 Git 工作树额外加深 Windows 路径。
   首次组合回归为 1236 passed、11 failed、3 skipped；原失败保留，短路径对照已通过，未删除用例或放宽检查。

定向修复回归 **47 passed**；最终 main 组合树 `946a193dd74c566df05d10205fbf13eacf3e4163`
完整回归 **1253 passed、3 skipped**，Ruff 和 mypy（273 文件）通过。
全量用例按不重叠的两路执行：配对入口 56 项，其余 1197 项通过、3 项跳过，合计 1256 个唯一测试节点，无失败或错误。
三项跳过为未启用的应用 Compose smoke，以及当前 Windows 账号不支持的两项符号链接检查。
已验证的评测工程、必要依赖、回归测试和本公开报告可独立同步 main，不宣告真实配对验收完成。
最终测试与审计记录保留在 `data/validation/phase10-main-{paired,other}-final-20261103.xml`
及 `phase10-final-status-20261103.json`，不上传。
这些修复没有新增付费调用，不将旧 v13 改判通过。

## 剩余的验收边界

v14 已完成真实故障恢复、负对照及部分伪造指标检测；伪造指标场景的关闭 Reviewer / Memory / Replanning 三格仍缺有效测量。
不能将不同源码或 wire schema 下的观测拼成完整同版本配对；完整矩阵仍须在有效授权下取得完整证据，才能决定 Phase 10 总验收。
不新增多模型、大样本、长时间训练或显著性门槛；后续生产加固留给 Phase 11。
