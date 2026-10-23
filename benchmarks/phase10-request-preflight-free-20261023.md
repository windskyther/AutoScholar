# Phase 10：请求预检与源码增长修复

本轮修复 v4 验收暴露的本地请求超限与误记账问题，新增供应商请求 **0**。
此前 v4 的任务结果、分数、声明和已消耗授权保持原样；本报告不表示 Phase 10 整体验收通过。

## 修复后的行为

- 对接显式请求校验器的 Provider，在预算和持久化调用日志登记前构造请求并预检。
  评测传输层共享同一校验逻辑，核对 SDK 输入与加入输出上限后的实际转发 JSON 的 UTF-8
  字节数，仍保持 32768 字节请求上限和原有输出、响应、调用预算。
- 已证实未发送的本地拒绝使用独立错误类型和 `local_request_rejected` 事件。
  极少数发生在日志登记后的本地拒绝，只撤销本次模型调用预约，不修改已知 token 用量，
  也不能关闭其他调用或重复撤销。终止性的本地输入错误会停止工作流，不再调用 Reviewer。
- 连接失败、超时、取消及发送后的无效/超大响应，继续保留未知外部结果和待核对日志，
  不自动重放、不归零费用、不冒充“确认未发送”。
- Coding 完整源码快照模式先只读预览 `create_file` / `edit_file` 的结果，
  连同工具定义和近期操作检查下一轮请求，并预留 1024 字节反馈空间。
  超限返回 `coding_snapshot_too_large`，保留原文件，提示缩小必要修改或在现有代码满足
  要求时提交验证。源码不截断，静态检查和 pytest 仍为必经步骤。

预检为可选 Provider 能力；未配置显式传输策略的普通 Provider 不凭空获得 32 KiB 限制。
预览不消耗调用、生成物理请求收据、写文件或改变模型包装器状态。

## 免费验证

新增针对性回归 **19 passed**，覆盖：

- UTF-8 精确边界、转发编码、反馈预留和凭据/不可序列化输入拒绝；
- 原生工具调用、推理字段、命名工具选择与修复提示的一次转换；
- 前置拒绝与 SDK 包装后的晚期拒绝，均不产生虚构调用或 token；
- 持久化失败终态、重启后不重放、只关闭自己的预约、禁止重复撤销；
- 真实发送后的连接/超时/响应异常保留 `recovery_required`；
- Workspace 预览无副作用，Unicode/换行与实际写入一致，路径、冲突及配额仍受检；
- 超大文件创建与编辑被拒绝后，源码完整、计数一致，并执行后续强制验证。

```powershell
$testTemp = Join-Path (Get-Location) ('data/p-' + [guid]::NewGuid().ToString('N').Substring(0,8))
if (Test-Path -LiteralPath $testTemp) { throw 'Fresh test path unexpectedly exists' }
.venv\Scripts\python.exe -m pytest tests/test_request_preflight.py -q --basetemp $testTemp
if ($LASTEXITCODE -ne 0) { throw 'Request preflight regression failed' }
```

最终完整回归 **967 passed / 3 skipped**，JUnit 为
`data/validation/phase10-preflight-complete-results.xml`。三项跳过分别为需显式开启的
Compose 栈测试，以及当前 Windows 账户不能创建符号链接的两项测试。
Ruff、mypy（244 文件）及本轮修改文件的格式检查通过；未改动的旧格式问题不作全库通过宣称。

只读重放原 v4 失败现场：输入 JSON 仍为 **33749 字节**，加输出上限后的转发编码为
**33872 字节**，均超过原 32768 字节限制。使用新预检在记账前拒绝，已有 7 次模型调用
及已知 token 不变，没有物理预约或网络 delegate 调用。

另将原响应中的 **10231 字符**辅助文件交给新的 Coding 写入预检：文件被拒绝，
完整原源码保留，模型下一轮收到明确反馈。此阶段仅有 **2 次 Mock HTTP**，模拟用量
不计作供应商请求或费用。固定缓存镜像、只读公开 MNIST 和无外网专属控制器下，实际完成：

- Coding 强制静态检查与 pytest；
- 将验证过的源码交给单独的实验子任务，真实训练 1 次并保存 10 个产物；
- 独立核验全部产物、数据摘要、图像和两份 checkpoints；MLP/CNN 准确率分别为
  0.3359375 / 0.203125，均与保存指标一致。负结果正常报告，不要求 CNN 必须胜出。

成功记录为 `data/validation/request-preflight-c24045ec/results.json`。
本地脚本首次检查使用了未固定摘要的控制器，后按既有 worker 要求固定缓存镜像；
第二次在强制代码验证通过后，因脚本复用了 coding 工作区导致文件冲突，尚未训练。
改为生产流程中的独立 experiment 子任务后，上述整体验证通过。失败尝试和成功记录分别保留，
没有修改业务代码或覆盖旧结果来通过检查。

旧 v4 文件、授权与声明，以及 96 份 v3 / 301 份历史证据在内，共 **463 份**受保护
记录摘要未变。本轮模型、Tavily 真实请求均为 **0**，没有新增付费声明或复用旧余额。
专属子容器和临时卷已清理；临时控制器/网络按所有权清理，缓存镜像、数据卷和应用栈保留。
JUnit、模拟收据、数据库和训练产物全部保留在忽略的 `data/validation/`，不上传 GitHub。

## 与真实验收的边界

v4 正常组已通过；原恢复组仍保留未知，第三组仍未测。本地重放使用保存的公开任务和源码、
显式模拟模型响应及本地 Docker，不构成真实模型自主纠错或恢复的新增证据。
判分、独立 oracle 和原任务结果未放宽或改写。

本轮业务代码发生变化，原 v4 提案/执行计划不能沿用；旧固定声明及剩余额度不重置。
真实恢复组及负对照补验仍需另行绑定新版本和明确授权。
`phase10_overall_acceptance=false`，`main_merge_authorized=false`。
