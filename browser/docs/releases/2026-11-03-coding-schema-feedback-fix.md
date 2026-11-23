# Coding schema 错误的有界纠错修复

[v12 真实复验](2026-11-03-v12-live-stop.md)在第三组收到 336 字符的 `submit_code_ready.summary`，
超过 Snapshot 的 240 字符限制。本地 provider 在返回工具调用前拒绝响应，Coding 原有的参数反馈循环未能接到该错误。
本次把此类完整、用量已知的本地 schema 拒绝接入一次有界纠错，未放宽参数限制或改写历史结果。

## 结果与边界

- 仅 Snapshot 模式处理 `llm_invalid_tool_call / tool_schema_violation`，要求 `finish_reason=tool_calls`、可纠错标记、模型标识，以及非负且加总一致的完整用量。
- 错误响应不暴露任何可执行工具调用，不截断参数，也不执行同批中的有效编辑。反馈使用固定提示和原字段限制，未转发供应商任意错误正文。
- 参数 schema 错误、Snapshot 字节/调用格式错误共享原有一次纠错额度；成功编辑不会重置额度。HTTP 错误、截断、JSON 解析失败、未知用量和非 Snapshot 请求仍停止。
- 首次错误和后续纠错均计入原模型调用及 token 预算；用量通过原 `BudgetedLLM` 和持久化日志核算，保留工作流后续调用的预留空间。
- 合规的 `submit_code_ready` 仍需静态检查、pytest、后续实验与产物检查，不会自动通过任务或跳过失败检查。
- 评测包装器只在实际 HTTP 200 收据与异常用量逐项一致时允许 Coding 纠错，与协调器共用每任务最多一次的原生纠错额度；不重置原授权账本或 12 次任务上限。

Snapshot 提示同时明确说明摘要最多 240 字符；240 字符、4096 UTF-8 字节和完整请求 32768 字节限制均保留。

## 免费验证

dev 的核心回归 **186 passed**，评测/授权/请求预检回归 **58 passed**。
29 项新增生产回归和 6 项评测回归覆盖实际 SDK + Mock HTTP：
摘要/编辑/新建内容超长、类型/必填/额外字段错误、同批部分有效编辑、重复错误、编辑后额度不重置、
Snapshot 字节限制与 schema 错误共享额度，以及截断、未知/不一致用量、供应商异常、预算预留与静态检查失败。
持久化请求开始/完成日志一一闭合，已知用量不会重复计费或漏计。

最终的实际 SDK + Mock HTTP + Docker 路径：

| 项目 | 结果 |
|---|---|
| 场景 | 新建的离线 `normal-no_reviewer` 流程 |
| 首次 Coding 响应 | 原样重放 v12 保存的 336 字符响应，按原 schema 拒绝 |
| 纠错 | 一次，下一条显式模拟响应提交合规摘要 |
| 模型 HTTP | 7 次 Mock HTTP，**0 次新增供应商调用** |
| 静态检查 / pytest | 均通过 |
| CPU MNIST | 一次真实训练，固定 128/128 子集和一次 epoch |
| 独立检查 | 检查点和十项产物核验均通过 |
| Reviewer 消融 | 没有 `submit_review` 模型调用；关闭 Reviewer 的持久化记录正常保留 |
| 历史 / 源码 / 资源 | 本次执行源码摘要不变；原 951 文件未变；专属资源已清理 |

脚本调试中，第一次在 Docker 健康初始化前检查所有权而停止，第二次错误地要求关闭 Reviewer 后完全不保存 review 记录。
已分别修正初始化顺序及断言；第二次工作流本身成功且执行了一次训练，最终脚本复跑后完整通过。
调试与最终证据分别保留，不将多次离线执行算成多个真实模型观测。
保存响应的原收据仅用于核对重放过程，不计为新增供应商用量。

本地证据不上传：

- `data/validation/schema-feedback-core-final-20261103.xml`
- `data/validation/schema-feedback-eval-20261103.xml`
- `data/validation/schema-feedback-main-final-20261103.xml`
- `data/validation/schema-feedback-docker-a379b235/`
- `data/validation/schema-feedback-controller-cleanup-20261103.json`

## 同步与下一步

生产修复及相关测试可独立同步 main；评测包装器和专用 Docker 脚本留在 dev。
独立 main 组合树 `2aa7dd4` 全量回归 **550 passed、3 skipped**，Ruff 和 mypy（189 文件）通过。
三项跳过分别为未启用的应用 Compose 联测，以及当前 Windows 账号不支持的两项符号链接检查；
本次修复涉及的专属 Docker 流程已按上表单独执行。dev 的 Ruff 和 mypy（272 文件）也通过。
`.env`、本地设计文档及 `data/` 不提交；v12 的 19 次调用、未知任务结果和原授权声明保持原值。

下一步建议只做一组新的 `normal-no_reviewer` 真实确认，再决定是否重开完整 15 组试验：

- 新增最多 **12 次物理 LLM、0 次 Tavily / Semantic Scholar**；包含可能的一次有界纠错。
- 沿用当前模型、终端、兼容模式、公开测试目标及模板；单组 token 阈值 120000，最后响应可能越过阈值，单次输出上限 8192。
- 请求最多 32768 UTF-8 字节；不发送 `.env`、密钥内容、本地设计文档或应用数据库。密钥仅用于认证。
- 新声明、新授权账本、新源码冻结清单；旧轮剩余额度不复用。遇供应商/用量/资源/执行检查异常停止，不自动重跑整组。
- 即使通过，也只是该组恢复运行的确认；如果模型未产生 schema 错误，则有界纠错路径仍由强制错误的免费重放证明，不能声称真实模型已触发该分支。
- 不拼接旧观测，不宣布完整配对或 Phase 10 总验收；需要单独批准新增付费调用后才执行。
