# AutoScholar

AutoScholar 是运行在本机的 AI 研究工作台。你可以围绕一个研究目标整理 PDF、检索网页和论文，让 AI 制定计划并执行任务，再查看引用依据、实验结果和报告。

## 你可以用它做什么

| 需求 | 使用方式 |
| --- | --- |
| 阅读与整理资料 | 按项目上传 PDF，让研究任务结合文档内容回答问题，并查看引用页码与原文片段。 |
| 调研一个主题 | 检索网页和论文，整理方法、差异与依据。 |
| 执行研究计划 | 输入目标，由 AI 拆解步骤、调用工具、审查结果，并在预算范围内调整计划。 |
| 编写与验证代码 | 在隔离环境中生成、检查和运行 Python 代码，查看错误与修复结果。 |
| 进行小规模实验 | 使用当前支持的 MNIST CPU 实验，比较模型指标并下载生成的产物。 |
| 管理研究过程 | 查看任务进度，暂停、恢复或取消任务，审阅实验审批，导出已有报告。 |

当前版本面向个人在本机使用。实验能力以 MNIST CPU 流程为主，尚不支持在工作台中任意上传训练数据或选择 GPU 环境。

## 一键启动（Windows）

安装好 Docker Desktop、Node.js 24.x，并按下方说明填写一次 `.env` 后，双击项目根目录中的 **[Start-AutoScholar.cmd](Start-AutoScholar.cmd)**。

启动器会自动启动 Docker、启动后台服务、按需安装界面依赖，并在后台运行工作台、打开浏览器。已有工作台会直接复用，启动窗口关闭后仍可使用。登录时输入原来的 `EXPERIMENT_API_TOKEN`。

启动失败时窗口会保留具体提示；界面进程日志保存在 `data/launcher/`。首次 MNIST 数据准备仍按下方步骤执行。启动器不会自动提交研究任务。

更新了后台代码、需要重新构建时，在项目根目录运行：

```powershell
.\Start-AutoScholar.cmd -Rebuild
```

## 首次启动

以下命令以 **Windows PowerShell** 为例。已下载项目的用户直接进入项目目录，跳过下载步骤。

### 1. 准备环境

- Git。
- Docker Desktop，已启动并使用 Linux 容器。
- Node.js **24.x**，用于启动工作台界面。
- 支持原生工具调用的 OpenAI-compatible 模型服务及其 API Key；仅支持普通聊天的接口无法完成自主研究任务。
- 可访问镜像、模型下载源和所选 API 服务的网络。首次构建与首次处理 PDF 会下载依赖，请预留磁盘空间；Docker 数据目录可设置在非系统盘。

使用下面的 Docker 启动方式，无需在宿主机另行安装 Python 或数据库。

```powershell
git clone https://github.com/windskyther/AutoScholar.git
Set-Location AutoScholar
```

### 2. 填写本地配置

在项目根目录创建配置文件；已有 `.env` 时保留原文件：

```powershell
if (-not (Test-Path -LiteralPath .env)) {
    Copy-Item .env.example .env
}
notepad .env
```

根据下表填写，其他选项初次使用可保持默认。

| 配置项 | 如何填写 |
| --- | --- |
| `LLM_BASE_URL` | 模型服务商提供的兼容接口地址，通常以 `/v1` 结尾。 |
| `LLM_API_KEY` | 模型服务的 API Key。 |
| `LLM_MODEL` | 服务商提供的模型名称，需与其接口支持的名称一致。 |
| `EXPERIMENT_API_TOKEN` | 自行生成的随机访问凭据；登录工作台时使用这一项。 |
| `SANDBOX_MANAGER_TOKEN` | 另行生成的随机凭据，至少 32 个字符；与访问 Token、各服务 API Key 均不同。后台执行环境需要它，登录时不用输入。 |
| `TAVILY_API_KEY` | 使用网页检索时填写 Tavily Key；留空时无法选择 Tavily 网页检索。 |
| `SEMANTIC_SCHOLAR_API_KEY` | 可选。留空时尝试匿名论文检索，可能受到共享限流影响。 |

可在 PowerShell 中执行以下命令，生成两个独立的随机凭据。将第一行填入 `EXPERIMENT_API_TOKEN`，第二行填入 `SANDBOX_MANAGER_TOKEN`：

```powershell
$random = [System.Security.Cryptography.RandomNumberGenerator]::Create()
1..2 | ForEach-Object {
    $bytes = New-Object byte[] 32
    $random.GetBytes($bytes)
    [Convert]::ToBase64String($bytes)
}
$random.Dispose()
```

`.env` 只保存在本机。不要将它上传到 GitHub，也不要把其中的密钥粘贴到问题反馈或截图中。

### 3. 启动后台服务

在项目根目录执行：

```powershell
docker compose up -d --build
docker compose ps -a
```

首次构建可能需要较长时间。等待 `api` 显示 `healthy`，以及两个 Worker 服务保持运行。`migrate` 和 `sandbox-image` 是一次性准备服务，正常完成后显示 `Exited (0)`。

### 4. 打开工作台

另开一个 PowerShell 窗口，先进入项目根目录，再执行：

```powershell
Set-Location web
npm ci
npm run dev
```

保持这个窗口运行，浏览器访问 **[http://127.0.0.1:5173](http://127.0.0.1:5173)**。

在“操作者 Token”输入框中填写 `.env` 的 **`EXPERIMENT_API_TOKEN` 的值**，点击连接。无需添加 `Bearer` 前缀，也不要输入模型 API Key 或 `SANDBOX_MANAGER_TOKEN`。

界面与后台需要分别启动；只运行 `docker compose up` 不会启动这个地址上的工作台。刷新页面或断开连接后需要重新输入 Token。

### 5. 需要训练实验时，准备数据

只整理资料和调研时可以跳过。首次执行 MNIST 实验前，在项目根目录运行：

```powershell
docker compose build sandbox-image
docker compose --profile datasets run --rm mnist-init
```

此步骤下载公开 MNIST 数据，后续实验复用本机 Docker 数据卷中的副本。训练在隔离、断网的 CPU 环境中运行。

## 完成第一次研究

1. **新建项目。** 在项目列表点击“新建项目”，填写名称和说明。
2. **添加资料（可选）。** 进入项目，上传可复制文本的 PDF，等待状态变为“已就绪”。首次处理可能需要下载本地检索模型。
3. **提交研究目标。** 在项目中打开研究任务提交窗口，描述问题、需要比较的内容和期望产出；选择检索来源和 PDF 范围。
4. **设置预算并确认。** 初次体验建议缩小目标，设置较低的模型调用、检索和训练次数上限。确认可能产生的 API 费用后提交，任务会自动执行。
5. **查看结果。** 在任务详情查看计划、进度和输出；在“证据与引用”核对来源，在“实验对比”查看已有指标，在“文件与报告”下载已生成的内容。

第一次可以尝试这样的目标：

> 比较 LoRA 与 QLoRA 的核心思路、显存需求和适用场景，整理一张对比表，并给出可核查的来源。只做资料调研，不编写代码或运行训练。

需要结合自己的资料时，可以上传相关论文，并明确写出“结合所选 PDF，指出引用页码”。未配置 Tavily 时请选择论文检索；匿名论文检索失败时，任务可能只完成部分内容。

### 暂停、恢复与审批

- **暂停**会请求任务在执行边界停下，正在进行的调用可能先收尾。
- **恢复**会继续已有任务，也可能继续产生 API 费用。
- **取消**会停止后续执行，已经发生的调用与费用无法撤销。
- **等待审批**时，打开审批详情核对实验参数。批准后任务仍保持暂停，需要再单独点击恢复。
- 请求提示“结果未确认”时，先核查任务列表与最新状态，按页面指引使用原编号确认，避免新建重复任务。

### 删除项目

项目列表和详情页均可删除项目，需要输入完整项目名确认。有未完成任务或仍在处理的文档时，需要先处理这些任务或等待文档处理结束。

删除会将项目从工作台移除，目前没有恢复按钮。历史任务记录、PDF 和实验文件仍保存在本机；删除项目不会释放这些文件占用的磁盘空间。

## 日常启动与停止

完成首次安装后，日常使用直接双击 `Start-AutoScholar.cmd` 即可。也可以手动启动：先启动 Docker Desktop，然后在项目根目录执行：

```powershell
docker compose up -d
```

另开一个窗口，进入项目的 `web` 目录执行 `npm run dev`，再打开工作台并输入 Token。依赖未变化时无需重复执行 `npm ci`。

手动启动界面时，在运行 `npm run dev` 的窗口按 `Ctrl+C` 可停止界面。一键启动的界面进程在后台运行，可在任务管理器中按 `data/launcher/workbench.json` 记录的 PID 找到对应的 Node.js 进程并结束。**关闭界面或浏览器不会停止后台任务**；如需暂停研究，先在任务中暂停或取消。

停止后台服务，在项目根目录执行：

```powershell
docker compose down
```

正常停止会保留项目、文档和实验数据。不要添加 `-v`：它会删除数据卷，可能导致数据丢失。修改 `.env` 后，在项目根目录执行 `docker compose up -d`，让服务加载新配置。

## 常见问题

| 遇到的情况 | 处理方式 |
| --- | --- |
| 工作台页面打不开 | 确认 `npm run dev` 的窗口仍在运行，访问 `http://127.0.0.1:5173`；端口被占用时先检查是否已有工作台进程。 |
| Token 正确但连接失败 | 确认输入的是 `EXPERIMENT_API_TOKEN` 的值；在项目根目录运行 `docker compose ps -a`，检查 `api` 是否健康。修改配置后运行 `docker compose up -d`；更新代码后使用 `docker compose up -d --build`。 |
| 无法提交任务 | 检查 `.env` 中三个 `LLM_` 配置项，并确认模型支持原生工具调用。 |
| PDF 长时间未就绪 | 首次使用需要下载检索模型；确认网络可用，文档处理服务保持运行。扫描件与加密 PDF 不受支持，单文件默认上限为 50 MiB、500 页。 |
| 论文检索被限流 | 稍后重试，或配置自己的 Semantic Scholar Key；有 Tavily Key 时可先使用网页检索。 |
| 实验提示缺少数据集 | 执行上面的 MNIST 数据准备命令，确认下载成功后再提交实验。 |
| 任务显示部分完成或预算耗尽 | 查看已有结果与限制说明，缩小研究目标，或在确认费用后为新任务调整预算。 |
| 需要反馈错误 | 记录任务编号、页面错误信息和复现步骤。可运行 `docker compose logs --tail 100 api worker workflow-worker` 查看近期日志；分享前删去密钥、个人资料及不希望公开的研究内容。 |

## 费用、数据与使用范围

- 模型与检索服务按各自规则计费。工作台中的 Token、调用次数和训练次数预算是用量限制，**不等同于金额上限**；最后一次模型响应可能使 Token 用量超过预算。
- 项目、PDF 和实验产物保存在本机。研究目标、相关文档片段和工具结果可能发送到配置的模型服务；检索词会发送到所选搜索服务。使用敏感资料前，请确认这些服务的数据政策。
- PDF 目前仅支持文本提取，不支持 OCR。外部网页和论文研究主要使用检索片段与摘要，不会自动获取所有全文。
- AI 回答与实验结论需要人工核查。当前实验结果来自限定的 MNIST CPU 流程，不能直接视为其他数据集或完整规模实验的结论。

如需使用 Docker 托管界面、配置 HTTPS 或维护数据，参阅[部署指南](docs/operations/production.md)与[备份恢复指南](docs/operations/recovery.md)。本页的启动方式面向本机使用。
