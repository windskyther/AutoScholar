# Phase 10 v5 真实验收：2026-10-23

三组真实测量完成：正常组通过，关闭重规划的负对照按预期停止，故障恢复组未通过。
**定向验收未通过，Phase 10 整体未通过，未合并 main。**

## 授权与实际用量

所有者确认后执行冻结清单，源码为 `9f4557fb5568ddaaace17f1ffc31ba986273b2d1`。
本次独立 v5 声明最多新增 36 次 LLM、0 次 Tavily，每组最多 12 次；
120000-token 停止阈值（末次可能越过）、8192 输出上限及 32768 字节请求上限保持不变。
使用公开 MNIST、模板代码和闭集工程证据；不上传 `.env`、密钥、设计文档或私人材料。

| 场景 | 实际 LLM | token | 原任务结果 | 场景合同 |
|---|---:|---:|---|---|
| normal-baseline | 12 | 85830 | succeeded | 通过 |
| recover-baseline | 8 | 37355 | budget_exceeded | 未通过 |
| recover-no_replanning | 9 | 44903 | failed / evaluation_replanning_disabled | 按预期停止，通过负对照合同 |

共 **29 次 LLM / 0 次 Tavily**，**117828 输入 + 50260 输出 = 168088 token**。
29 次响应均 HTTP 200，原生工具参数均为有效 JSON，原始用量、物理收据及持久化调用一致，
没有用量未知的调用或未闭合 LLM 日志。供应商单价未知，不推算金额。
本轮没有自动重试整组；剩余 7 次上限空间不自动用于补跑、重置或其他测试。

准备阶段的 248 项相关回归与三组显式模型脚本 + 真实 Docker 预检通过，
但本次真实模型暴露了脚本未覆盖的审查诊断与恢复范围问题，不能用免费预检代替本次结果。

## 各组实测

正常组研究、代码、训练各执行一次。模型在首次 coding 步骤内进行了多轮文件修改，
最终在第 12 次调用完成审查；强制验证、完整源码交接、报告来源绑定及全部 10 个产物检查通过，
两份 checkpoints 的独立复算通过。

故障恢复组研究、代码、首次训练各执行一次，注入一次指标收集故障。
初始 coding 使用未改动的可信模板，静态检查、pytest 和源码交接已验证。
首次训练退出成功，收集到预期的五类训练产物，但注入后的指标未通过 schema 校验。

第 7 次响应的 Reviewer 没有将问题限定为实验收集故障，而是要求补充代码层面的 schema
验证，并把 coding 步骤标为待修复。第 8 次重规划因此选择重跑 coding 和 experiment。
第二个 coding 步骤在启动时触及 **`code_repairs=0`**，工作流以
`autonomous_budget_exceeded` 停止；并未执行第二次模型编程或第二次训练。
这不是 12 次模型调用或 120000-token 上限耗尽，也不是网络错误或 v4 的请求超限误记账。

负对照研究、代码、训练各执行一次，注入一次同类故障后，以
`evaluation_replanning_disabled` 停止，没有重规划或第二次训练。其原任务失败保留，
负对照合同通过不等于该任务成功。

本次没有出现 `coding_snapshot_too_large`，不能额外声称真实模型已经测试了超限写入纠正；
该路径的证据仍为此前保存真实文件的零 API 重放和针对性回归。

## 零 API 事后诊断

通过保存的请求、响应和只读 SQLite，确认恢复组的 `train.py` / `test_models.py`
与平台模板一致。Review 请求中的 `source_handoff`、`coding_validation` 均为真。

1. Reviewer 使用 `check=requirement`、问题代码 `missing_schema_validation_of_raw_metrics`，
   `source_path` / `source_quote` 均为空。这种写法通过了当前两项校验；将同一内存对象
   标为 `source_defect` 则会被现有源码依据检查拒绝。因此类型标签能影响证据要求，
   当前规则没有充分约束针对成功 coding 步骤的此类诊断。
2. `_experiment_retry_steps` 要求所有问题的自由文本 `code` 都属于两项固定错误代码。
   即使只保留模型对 experiment 的问题，`raw_metrics_schema_mismatch` 等自定义名称
   仍使返回为空；只在内存中使用原始确定性错误 `experiment_metrics_invalid` 时，
   同一成功训练与源码来源满足实验单步恢复条件。
3. 初始 Planner 把“验证过的 CPU 训练和完整指标产物”也写入 coding 的 `expected_output`，
   与平台将训练交给 experiment 的职责划分冲突，给上述诊断提供了不恰当的依据。

诊断只是反事实内存对象和只读查询，没有执行新的工作流、调用 API、改写数据库或改分。
下一步应约束计划的阶段职责、成功 coding 问题的证据要求，以及基于权威失败事实的恢复资格；
先将本轮响应纳入免费回归，再决定新的真实验证范围。
不得靠提高代码修复预算、丢弃真实问题或自动设置 PASS 来通过本轮。

## 证据保留与清理

本轮原始材料保存在忽略目录：
`data/evaluation/live-repairs-v5-b5262d5e9e7d442cab3b25a8bc5fa04e/`。
`audit-close-20261023.json` 核对实际收据、用量、持久化日志及产物摘要；
`diagnosis-local-20261023.json` 保存零 API 诊断；
`cleanup-close-20261023.json` 记录专属资源清理。
原始请求、响应、结果、产物和 SQLite 均未覆盖。

旧 v3/v4 声明、授权及历史证据共 **464 份**受保护文件摘要未变。
专属控制器和内部网络已按所有权清理，临时子容器及卷无残留；
应用栈、缓存镜像、公开 MNIST 数据卷与运行记录保留。私有文件和原始数据不提交 GitHub。

`repair_measurement_complete=true`，`targeted_repairs_acceptance=false`，
`phase10_overall_acceptance=false`，`main_merge_authorized=false`。
本轮既不改写旧 v4 的未知结果，也不构成完整真实模型消融或统计组件收益证据。
