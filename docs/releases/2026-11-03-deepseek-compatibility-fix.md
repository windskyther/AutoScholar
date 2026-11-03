# DeepSeek 工具调用兼容修复

针对 [v9 HTTP 400 诊断](2026-11-03-v9-http400-diagnosis.md)，完成思考模式和严格工具
schema 的兼容处理。修复已完成免费验证；本轮新增供应商请求 **0**，没有新的真实模型验收结论。

## 运行行为与配置

新增 `LLM_COMPATIBILITY_PROFILE`：

| 配置 | 行为 |
|---|---|
| `auto`（默认） | 仅在 HTTPS 地址的主机精确为 `api.deepseek.com` 时启用 DeepSeek 兼容 |
| `deepseek` | 显式启用，供遵循相同协议的兼容网关使用 |
| `generic` | 保留标准 OpenAI-compatible 请求 |

不会根据模型名称或域名子串猜测网关协议。当前本地配置已经自动识别为 DeepSeek，
无需修改 `.env`；现有 API/Worker 进程需重启后加载代码。没有修改本地 `.env` 或凭据。

DeepSeek 的 `required` 和指定函数调用现在显式携带 `thinking={"type":"disabled"}`；
`auto`、`none` 与普通文本请求继续沿用供应商默认思考设置。工具选择要求没有被悄悄放宽为 auto。
SDK 使用 extra_body 发送 thinking，但预检及传输保存的是合并后的实际 JSON，
不将 extra_body 包装误算进请求，也不遗漏该字段的字节开销。

严格工具的发送 schema 仅移除官方不支持的 `minLength/maxLength/minItems/maxItems`。
递归转换区分 schema 节点与普通数据，不改动同名业务属性、枚举、示例或原始工具定义。
其他 schema 约束继续发送；这不是对任意供应商 schema 的通用转换器。

有上述投影的工具，其返回参数会在提供给调用方前按**原始完整 schema**本地校验。
一条违规调用会拒绝整份完成，不执行先前有效的部分；用量随错误保留。
本地 schema 引用不允许联网解析。Snapshot 写入前的 4096 UTF-8 字节参数上限、
完整源码请求预检、代码检查及 Reviewer 也继续执行。
没有增加 HTTP 重试、整轮重跑或旧授权恢复规则。

依据：[DeepSeek Chat Completions](https://api-docs.deepseek.com/api/create-chat-completion/)
说明强制工具调用与思考模式的限制；[工具调用文档](https://api-docs.deepseek.com/guides/tool_calls/)
列出严格 schema 的字符串及数组约束限制。本次转换针对这些已记录的协议差异。

## 验证证据

- dev 核心集合 **70 passed**，包含 27 项新增兼容性测试；这 27 项随后独立复测通过。
- dev 评测/传输集合 **103 passed**，包含显式评测配置读取 profile 的验证。
- dev 工作流与 Coding 收敛集合 **36 passed**。
- 独立 main 树完整 pytest：**521 passed、3 skipped**；Ruff/mypy（188 文件）通过。
- dev Ruff/mypy（264 文件）通过，离线锁文件检查通过；referencing 作为直接依赖声明，
  继续使用锁中已有版本，没有升级其他依赖。

三项跳过为未启用的 Compose smoke，以及当前 Windows 账户没有符号链接权限的两项测试。
没有将这三项算作通过；本轮没有重跑整个 dev 测试库。
main 检查前显式固定其源码路径，并核对运行时代码、必要测试和依赖文件与 dev 一致。

原 v9 六条请求在新代码下逐条通过 Mock HTTP 与实际 SDK 序列化边界：
前五条仍使用 auto；第六条 required 明确关闭思考、严格 schema 经过投影。
第六条实际转发大小为 **22275 UTF-8 字节**；六条均满足 32768 字节及 8192 输出额度限制，
预检内容与实际发送一致。这是六次 Mock 请求，**不代表真实供应商接受或研究流程成功**。

785 个历史文件摘要保持一致，v9 原失败结果、未知用量、声明和旧授权没有改写。
没有复用 v9 剩余六次额度，没有实际 Docker 训练或新增供应商请求。

本地证据不上传：

- `data/validation/deepseek-first-20261103.xml`、`deepseek-core-20261103.xml`
- `data/validation/deepseek-evaluation-20261103.xml`、`deepseek-workflow-20261103.xml`
- `data/validation/deepseek-main-full-20261103.xml`
- `data/validation/deepseek-wire-4d0ec75e/replay.json`
- `data/validation/deepseek-repair-final-20261103.json`

## 同步及剩余范围

运行时兼容配置、请求转换、本地校验、必要依赖和核心测试可独立同步 main。
评测显式配置读取的衔接依赖 dev 的评测框架，继续保留在 dev。
README 和公开 `.env.example` 描述新配置，真实 `.env`、设计文档及运行数据不提交。

修复消除了文档已知的两类请求不兼容，原 HTTP 400 的精确正文仍无法恢复；
需要基于新冻结源码另行确认真实模型行为。该真实确认不在本轮免费修复内，
也不宣告 Phase 10 总验收通过。
