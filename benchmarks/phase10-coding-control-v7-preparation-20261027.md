# Phase 10 v7：单个负对照的真实联测准备（2026-10-27）

本轮只准备尚未通过的 `recover-no_replanning`，拟新增最多 **12 次 LLM、0 次 Tavily**。
v6 正常与故障恢复的真实通过证据保留其原版本，不拼成一份新的三组通过结果。
准备和免费验证新增供应商请求为 **0**；真实执行须对新清单单独确认。

## 固定范围与通过标准

| 项目 | 约束 |
|---|---|
| 新任务 | 仅关闭重规划的故障负对照一组 |
| 调用上限 | 新增最多 12 次 LLM，纠正调用计入；Tavily 0 次 |
| token | 120000-token 停止阈值，末次响应可能越过阈值 |
| 单次输出 | 最多 8192 token |
| 请求与源码 | 32768 字节上限，完整源码不截断，1024 字节写入反馈预留 |
| 发送内容 | 公开 MNIST 目标、模板、公开闭集检索证据和验证/实验结果 |
| 保密内容 | 不发送密钥、`.env`、设计文档或私人用户资料 |
| 失败处理 | 不自动重跑整组，不重置或复用 v3–v6 声明和剩余额度 |

必须完成研究和 Coding 强制检查，实际执行一次训练、触发一次指标收集故障，
然后以 `evaluation_replanning_disabled` 停止；不允许重规划、代码重跑或第二次训练。
提前因 Coding、预算或其他错误停止，不能算作负对照通过。
按预期停止时任务结果仍为失败，只是该负对照合同通过。

## 源码与旧证据保护

业务代码沿用 dev `7be53fa` 的已验证修复，运行时修复已独立同步 main。
本次生产源码改动仅限四个评测入口/传输文件，不改变 Coding、Agent、夹具、oracle 或原判分门槛。
v7 单独绑定 Coding 进度反馈、调用预留、额度边界强制验证、批量拒绝及检查完整性规则。

v3–v6 的合同摘要保持本轮修改前的值，原三组排程和 36 次上限保留。
v7 使用独立单组排程、12 次台账和 `live-repair-v7-claim.json`；台账拒绝其他任务。
提案绑定干净提交、夹具、运行时 schema、阶段职责和旧文件 SHA-256；执行前及每次调用前复核。
旧 v6 的原始请求、响应、SQLite、源码、产物及最终失败结论保持原值。

## 免费准备结果

相关回归 **118 passed**，包含 v7 完整 Mock HTTP 入口、单任务派发、真实停止标准、
12 次硬上限、旧合同/声明/授权/结果绑定、请求预检和 Coding 收敛控制。
Ruff、mypy（251 个文件）通过。业务代码未变，未重复宣称一遍 1043 项完整回归。

v7 专属真实 Docker 单组预检符合预期：显式脚本在四次编辑后由服务端交给静态检查及 pytest，
实际训练一次，注入一次收集故障，然后按预期停止；没有重规划或第二次训练。
脚本回调为 10 次，预算仍为 12 次；供应商请求与供应商 Token 为零。
所有脚本决策均明确标识，不作为真实模型在新提示下的决策证据。

本地记录（忽略、不上传）：

- `data/validation/phase10-v7-focused-final-results.xml`：118 项回归。
- `data/validation/convergence-preflight-378276ae/`：v7 单组真实 Docker 预检。
- `data/validation/v7-preflight-controller-cleanup-20261027.json`：专属资源清理。

专属子容器、临时卷、控制器和内部网络均已清理；应用资源、固定缓存镜像、公开 MNIST 及报告保留。
确切提案和执行清单将在干净提交上生成，位于被忽略的 `data/evaluation/`。
准备阶段不读取 `.env`，不创建正式 v7 声明或付费台账。

## 待确认的执行

```powershell
.venv\Scripts\python.exe -m autoscholar.evaluation repairs --protocol-version 7 --allow-paid --plan data/evaluation/repair-plan-<UUID>/plan.json --approval-id phase10-coding-control-v7-20261027 --sandbox-container autoscholar-eval-live-v7-20261027-sandbox-manager-1
```

本次只闭合一个负对照，不重新执行正常/恢复组，也不执行 15 组消融。
真实调用须单独确认新增预算；完整消融和 Phase 10 总验收仍是后续工作。
