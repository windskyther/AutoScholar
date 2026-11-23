# v9 HTTP 400 离线诊断

结论：**思考模式与强制工具调用不兼容是首要原因推断；严格 schema 的字符串长度限制是另一项兼容性风险。**
原第六次响应正文没有保存，无法恢复供应商当时的精确错误消息或确认首先触发哪个校验。
本轮没有真实 API 调用，v9 原失败结论与未知用量均保留。

## 本地证据

| 请求 | tool_choice | 工具数 | 显式 thinking / reasoning_effort | HTTP |
|---|---|---:|---|---:|
| 1–5 | auto | 1 | 均无 | 200 |
| 6（首次 Coding） | required | 3 | 均无 | 400 |

六次请求均配置 `deepseek-v4-flash`，成功响应报告 `deepseek-flash`。
第 1、3、4、5 次响应带有非空 reasoning_content，支持这条路径启用了思考模式的推断。
第六次请求的三个工具均为 strict，schema 带 minLength/maxLength。
第 2–5 次虽然也使用 strict，但没有这些字符串长度边界；第 1 次有长度边界但 strict=false。

第六次传输 JSON 为 22416 UTF-8 字节，低于 32768 上限，max_tokens 仍是 8192。
消息仅包含 system/user，没有应回传却丢失的 assistant/tool 历史。
前五次请求已成功，现有证据优先指向 Coding 参数组合，而非一般鉴权或连接失败。

## 官方兼容性依据

2026-11-03 查询的 [DeepSeek Chat Completions 文档](https://api-docs.deepseek.com/api/create-chat-completion/)
说明：thinking 与 reasoning_effort 均未指定时默认开启思考；思考模式不支持 required 或指定函数的
强制工具选择，会返回 HTTP 400。这与第六次请求的参数组合直接吻合。

[DeepSeek 工具调用文档](https://api-docs.deepseek.com/guides/tool_calls/)
将 minLength/maxLength 列为严格模式下不支持的字符串约束。因此只处理 tool_choice
仍可能留下 schema 兼容性问题。以上是官方协议与本地请求的交叉分析，不是假装恢复了原错误正文；
具体服务端或兼容网关行为仍需新范围内的真实确认。

## 本次已完成的免费改动

评测传输层在非 200 返回后新增独立 `error-<provider>-<case>.json`：

- 仅保存 HTTP 状态、有限枚举的错误类型和参数名，以及固定词表中命中的诊断词。
- 不保存任意错误原文、未知字段、响应头、请求回显或认证内容；无法识别时明确留空。
- 诊断词仅代表供应商文本提及，不能自动推导根因、重试或验收结论。
- 原响应继续传给 SDK；物理请求照常记账，缺失用量保持未知，不增加调用或重试。
- 写诊断文件失败时保留已经收到的 HTTP 状态和账本，不因此将请求改记为未返回。

100 项相关测试通过；修正测试导入以满足静态类型检查后，18 项诊断核心测试再次通过。
Ruff 与 mypy（262 文件）通过。测试覆盖 400/401/429/500、非 JSON、异常结构、含敏感内容的错误、
未知用量和诊断写盘失败。这里是 Mock HTTP，不是新增的真实模型能力结果。

785 个历史文件摘要复核一致。离线对比保存在
`data/validation/v9-http400-diagnosis-20261103.json`；测试报告保存在
`data/validation/http-diagnostics-20261103.xml` 与 `http-diagnostics-final-20261103.xml`。
所有本地证据均不上传。评测诊断代码留在 dev，公开诊断报告可独立同步 main。

## 最小修复方向

下一步应通过显式的供应商兼容配置处理这两项约束，避免把 DeepSeek 参数套用到所有兼容 API：

1. 对确需 required 的调用显式关闭 thinking；若保留思考模式则使用 auto，并继续由本地协议
   校验要求一次有效原生调用。不要在收到 400 后自动切换参数并偷偷增加一次请求。
2. 向 strict API 发送其支持的 schema，同时在服务端继续强制执行现有字符串与 UTF-8 总量限制。
   兼容层不得删除本地写入前校验。
3. 先用离线请求序列验证配置和参数一致性，再申请最小真实确认范围。v9 的剩余六次不复用。

本轮仅诊断及补齐诊断设施，尚未更改模型思考模式、工具选择策略或 schema 发送策略。
