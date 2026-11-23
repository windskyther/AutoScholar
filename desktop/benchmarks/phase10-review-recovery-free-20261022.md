# Phase 10：Reviewer 与恢复协议的免费修复验证

这是对 [v3 真实补验缺口](phase10-repairs-v3-20261022.md) 的后续实现，
不是新的真实模型验收。新增供应商请求 **LLM 0、Tavily 0**，没有使用 v3 剩余额度。

## 已实现

- Reviewer 从持久化记录及校验后的文件读取正向事实：静态检查/pytest、代码摘要、
  产物清单与摘要、raw metrics 与平台汇总的区别、逐字引用核对。
- 审查问题可标注机器检查类别。与已验证事实相冲突的结论进入原有至多一次纠正，
  持续无效则停止；不静默丢弃异议或直接替模型签发 PASS。
- 标注为 `source_defect` 的问题（以及旧版 `coding_handoff_schema_unverified` 诊断），
  必须给出当前源码路径及非空逐字代码片段；
  仅有实验采集错误不能证明源码有错。通用需求是否满足仍需语义审查，
  静态检查、测试或匹配到代码片段都不是任意需求正确性的证明。
- 确认故障只适合实验重试且已验证源码完整时，使用严格的
  `ExperimentRetry {rerun_steps, reason}`，由服务端保留整份不可变 DAG。
  不接收完整 plan、重复步骤、代码/研究重跑；恢复前后重新核对源码，
  并要求持久化训练轨迹确实成功退出。
- 普通 `PlanRevision` 的目标/步骤身份、规格、重复 rerun 和未解决 issue 覆盖检查
  全部进入有限纠正回调，状态变更前再次验证。确定性检查还核对 raw 与 summary 的一致性。

独立 checkpoint oracle、判分器、原预算、付费入口及旧授权声明均未放宽或重置。

## 免费 Docker 回放

从 v3 原始记录只读回放三份真实 Reviewer 响应。纠正响应由显式模型替身提供，
训练、故障注入、保存产物及 checkpoint 独立复算在真实 Docker 中执行。

| 场景 | 代码步骤 | 训练次数 | 验证结果 |
|---|---:|---:|---|
| 正常 baseline | 1 | 1 | 拒绝错误审查后通过，10 个产物及两份 checkpoints 复验通过 |
| 故障恢复 baseline | 1 | 2 | 拒绝无依据的源码指摘和无关代码重跑，仅实验恢复；独立复算通过 |
| 故障恢复 No Replanning | 1 | 1 | 到达真实注入故障后按合同停止，没有重规划/第二次训练 |

三组全部达到免费验证合同。证据保存在忽略目录
`data/validation/repairs-preflight-104025b8/`，含 `preflight.json`、工作流记录、
资源信息及 `audit-close.json`。最初回放脚本错误地要求负对照也必须纠正一次，
修正测试预期后完整复验通过；原失败记录保留在
`data/validation/repairs-preflight-3a06b5a8/`，没有修改生产停止条件。

本轮 96 个原始 v3 文件与 301 个历史证据摘要未变，旧授权账本和唯一声明仍保留。
专属临时控制器及内部网络已按所有权清理，镜像、数据卷和全部记录保留。

## 回归与适用边界

新增 **22 项**测试覆盖：事实冲突纠正及重复错误停止、无源码证据的诊断纠正、
窄接口拒绝完整 DAG/错误步骤/重复步骤、普通修订语义纠正、模型等待期间源码篡改。
最终完整短路径回归 **932 passed / 3 skipped**（935 项、0 failure/error）；
Ruff、mypy（240 文件）及修改文件格式检查通过，工程整包单独复验 37 项通过。
跳过项为旧的手动启用 Compose smoke，以及两项当前 Windows 账户无符号链接权限的测试。
最终 JUnit 保存在忽略的 `data/validation/review-recovery-complete-shortpath-results.xml`。

Windows 的系统临时目录可能无权限，长路径还会导致深层整包产物无法创建。
初次全量运行保留 **925 passed / 3 skipped / 7 failed**：失败全部来自工程整包，
其中 manifest 路径长 254 字符可保存，报告路径长 261 字符则未能创建。
同版源码改用短路径后，工程整包单独复验 **37 passed**；未放宽判分或删除初始记录。
免费测试可用一个全新、较短的工作区临时目录（不能复用已有目录）：

```powershell
$phase10TestTemp = Join-Path (Get-Location) ('data/t-' + [guid]::NewGuid().ToString('N').Substring(0,8))
if (Test-Path -LiteralPath $phase10TestTemp) { throw 'Fresh test path unexpectedly exists' }
.venv\Scripts\python.exe -m pytest -q --basetemp $phase10TestTemp
```

免费 Docker 验收沿用 [专属控制器准备步骤](README.md#targeted-repair-verification-v3)，
没有旧真实响应也可省略 `--replay-run`，运行显式模型替身的三个场景。
只有保留本地旧记录时才添加下面的回放参数；这些文件不上传：

```powershell
# 先按准备步骤设置两个缓存镜像 SHA 环境变量，再创建与下方命令同名的专属项目。
docker compose --env-file benchmarks/phase10d.defaults -f benchmarks/phase10d.compose.yaml -p autoscholar-eval-review-recovery-20261022 up -d --pull never
if ($LASTEXITCODE -ne 0) { throw 'Dedicated free replay controller startup failed' }
.venv\Scripts\python.exe -m tests.integration.phase10_repairs_acceptance --sandbox-container autoscholar-eval-review-recovery-20261022-sandbox-manager-1 --replay-run data/evaluation/live-repairs-v3-160affbfec68445aad963f7ddfc2485b
if ($LASTEXITCODE -ne 0) { throw 'Inspect preserved free replay evidence' }
```

控制器必须先按对应项目名创建；本轮控制器已清理，不能直接复用不存在的容器。
不要给控制器传 `.env` 或供应商密钥，不要删除数据卷、镜像或正常应用资源。

本轮只证明免费路径与保护有效，不能证明真实模型会稳定纠正，
更不是完整工作流消融或统计组件收益证据。`phase10_overall_acceptance=false`、
`main_merge_authorized=false` 保留。下一次真实补验须准备新的版本化合同并取得单独授权，
不得重置 v3 声明或重试旧任务；设计文档、`.env` 及原始数据仍不提交。
