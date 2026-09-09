# AutoScholar

> 面向 AI/ML 研究与实验的自主智能体平台  
> Autonomous AI/ML Research & Experiment Agent Platform

AutoScholar 的目标是把复杂研究目标转化为可追踪、可恢复、可评测、可复现的研究流程。项目当前已完成 Phase 4：除 Web/论文研究与项目知识库外，还支持任务级代码工作区、代码生成与修复，以及真正隔离的 Docker 执行环境。

## 当前能力

- FastAPI 服务、结构化日志、请求 ID 与统一错误响应
- PostgreSQL、Redis、Docker Compose 与 Alembic 自动迁移
- OpenAI-compatible `LLMProvider`，支持原生 Function Calling
- LangGraph 最小闭环：`Planner → Executor ⇄ Tools → Writer`
- Calculator 安全算术表达式工具
- 受限 Python 子进程工具
- Tavily Web Search 与 Semantic Scholar Paper Search
- Query Planner、Evidence Extractor 与 Citation Writer
- PDF 上传、文本解析、结构感知分块与可恢复后台摄取任务
- Qdrant Dense + BM25 Sparse 检索、RRF 融合与 BGE Reranker
- 独立 RAG Query API 与 Agent `knowledge` 模式
- 本地文档、Web 和论文来源统一进入 Evidence Pool
- Recall@K、HitRate@K、MRR、NDCG RAG Benchmark
- Evidence、Claim-Citation 映射和降级警告持久化
- Agent 任务和 Tool Trace 持久化
- Coding Agent：生成、搜索、读取、原子修改和删除任务源码
- 静态检查、pytest、结构化错误分类和最多 3 次自动修复
- 断网、非 root、限制 CPU/内存/PID/时长的一次性 Docker 沙箱
- 真实 MNIST 只读数据卷与 CPU PyTorch 运行环境
- 同步执行 API 与任务回查 API

当前 PDF 管线只处理可提取文本的 PDF，不执行 OCR；扫描件和加密 PDF 会明确失败。Web/论文检索仍只使用搜索片段与论文摘要，不自动下载外部全文。

## 工作流

```mermaid
flowchart LR
    S([START]) --> P[Planner]
    P -->|compute| E[Executor]
    E -->|调用工具| T[Calculator / Python]
    T --> E
    P -->|research| Q[Query Planner]
    Q --> R[Web / Paper / Project Search]
    R --> V[Evidence Extractor]
    P -->|knowledge| K[Dense + Sparse → RRF → Reranker]
    P -->|coding| C[Workspace → Code Tools → Static Check]
    C --> D[Docker Sandbox]
    D -->|失败，最多 3 次| C
    D -->|pytest 通过| W
    K --> W
    V --> W[Citation Writer]
    E -->|证据充分或预算耗尽| W
    W --> X[(PostgreSQL Task + Trace + Evidence)]
    W --> F([END])
```

默认预算：最多 6 次执行迭代、4 次工具调用、8 个计划步骤。预算耗尽时，任务状态为 `budget_exceeded`，Writer 会利用已有证据给出带限制说明的部分答案。

## 快速开始

### 环境要求

- Python 3.12
- [`uv`](https://docs.astral.sh/uv/)
- Docker Desktop（包含 Docker Compose）

当前 Windows 开发环境把 Docker Desktop 安装在 `D:\Applications\Docker`，运行数据保存在 `D:\DockerData\wsl`，避免镜像和卷占用系统盘。其他环境可自行选择安装位置。

### 配置模型

```powershell
Copy-Item .env.example .env
```

在本地 `.env` 中填写 OpenAI-compatible 服务：

```dotenv
LLM_BASE_URL=https://your-provider.example/v1
LLM_API_KEY=your-api-key
LLM_MODEL=your-model

TAVILY_API_KEY=your-tavily-api-key
# 可选但推荐；匿名 Semantic Scholar API 也可使用
SEMANTIC_SCHOLAR_API_KEY=your-semantic-scholar-api-key
```

Agent 只使用原生 Function Calling，不提供 JSON Prompt 降级方案。模型至少需要支持 `tools` 和 `tool_choice=auto`；若未返回要求的原生工具调用，任务会明确失败并持久化错误。`.env` 已被 Git 忽略，禁止把任何密钥提交到仓库或粘贴到日志、Issue。

### 使用 Docker 启动

```powershell
docker compose up -d --build
docker compose ps -a
```

`migrate` 服务会先执行 `alembic upgrade head`，成功后 API 才会启动。API 默认地址为 `http://localhost:8000`，交互式文档位于 `http://localhost:8000/docs`。

停止服务：

```powershell
docker compose down
```

命名卷会保留 PostgreSQL、Redis、Qdrant、PDF 原文件和本地模型缓存。除非确认需要清空全部数据，否则不要执行 `docker compose down -v`。

首次使用 Coding Agent 前，需要联网准备一次真实 MNIST 数据；之后任务沙箱始终断网运行：

```powershell
docker compose build sandbox-image
docker compose --profile datasets run --rm mnist-init
docker compose up -d --build
```

该操作只写入 `autoscholar_mnist_data` Docker 卷，不会把数据写入 Git。

### 本地开发

```powershell
uv sync --all-groups
uv run alembic upgrade head
uv run uvicorn autoscholar.main:app --reload
```

## API

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/health/live` | API 进程存活检查 |
| `GET` | `/health/ready` | PostgreSQL、Redis 和 LLM 配置状态 |
| `POST` | `/chat` | 无状态单轮模型调用 |
| `POST` | `/agent/run` | 同步运行最小 Agent 闭环 |
| `GET` | `/agent/tasks/{task_id}` | 回查任务、指标和 Tool Trace |
| `GET` | `/agent/tasks/{task_id}/evidence` | 分页查询结构化 Evidence |
| `GET` | `/agent/tasks/{task_id}/workspace` | 查询 Coding 任务工作区清单和校验值 |
| `GET` | `/agent/tasks/{task_id}/workspace/files/{path}` | 读取工作区 UTF-8 文本文件 |
| `POST` | `/projects` | 创建隔离的知识库项目 |
| `GET` | `/projects`、`/projects/{project_id}` | 列出或读取项目 |
| `POST` | `/projects/{project_id}/documents` | 上传 PDF 并进入后台摄取队列 |
| `GET` | `/projects/{project_id}/documents` | 查询 PDF 处理状态 |
| `GET` | `/projects/{project_id}/documents/{document_id}/content` | 受控读取 PDF 原文件 |
| `POST` | `/projects/{project_id}/documents/{document_id}/retry` | 重试失败的 PDF |
| `POST` | `/projects/{project_id}/documents/{document_id}/reindex` | 重新解析和索引 PDF |
| `DELETE` | `/projects/{project_id}/documents/{document_id}` | 异步删除原文件、元数据和向量 |
| `POST` | `/projects/{project_id}/rag/query` | 查询项目知识库并返回页码证据 |

运行 Agent：

```powershell
$body = @{
  objective = "调研 LoRA、QLoRA 和 DoRA 的核心区别"
  mode = "research"
} | ConvertTo-Json -Compress

$bytes = [System.Text.Encoding]::UTF8.GetBytes($body)

$result = Invoke-RestMethod `
  -Method Post `
  -Uri "http://127.0.0.1:8000/agent/run" `
  -ContentType "application/json; charset=utf-8" `
  -Body $bytes `
  -TimeoutSec 180

$result | ConvertTo-Json -Depth 10
```

回查已持久化任务：

```powershell
Invoke-RestMethod `
  -Uri "http://127.0.0.1:8000/agent/tasks/$($result.task_id)" |
  ConvertTo-Json -Depth 10
```

查询 Evidence：

```powershell
Invoke-RestMethod `
  -Uri "http://127.0.0.1:8000/agent/tasks/$($result.task_id)/evidence?limit=50&offset=0" |
  ConvertTo-Json -Depth 10
```

`mode` 支持 `auto`、`research`、`compute`、`knowledge` 和 `coding`，默认 `auto`。`knowledge` 必须携带 `project_id`；可用 `document_ids` 进一步缩小范围。`research` 携带 `project_id` 时会混合本地文档和外部证据；`research_sources` 可选择 `web`、`paper` 或两者，默认 `@("web", "paper")`。`coding` 在任务专属工作区内创建项目，只有静态检查和 pytest 都通过才会返回 `succeeded`。失败响应也会携带 `task_id`，可回查持久化错误和 Tool Trace。

## 受限 Python 的边界

Python 工具会先做 AST 白名单检查，再使用 `python -I -S`、空临时目录和最小环境启动独立子进程。当前限制包括：

- 代码最多 4,000 字符
- 执行超时 3 秒
- 输出最多 16,000 字符
- 禁止 import、文件、网络、进程、属性访问、函数/类定义、异常结构和 `while`

这是 Phase 1 的轻量计算器，不适合运行生成项目。Phase 4 Coding Agent 使用单独的 Docker 沙箱：API 不直接持有 Docker Socket；内部管理服务为每次运行创建一次性、断网、非 root 容器，而且不会向容器传递 AutoScholar 的 `.env` 或外部服务密钥。

## 测试

```powershell
uv run ruff check src tests migrations
uv run mypy
uv run pytest
```

Compose 服务健康后运行真实基础设施测试：

```powershell
$env:AUTOSCHOLAR_RUN_INTEGRATION="1"
uv run pytest tests/integration -m integration
```

### Phase 2 验收要点

使用上面的 LoRA/QLoRA/DoRA 请求完成真实验收后，确认：

- `status` 为 `succeeded`；如果某个外部服务失败，则应为 `partial` 而不是伪造完整结果
- `tool_calls` 同时包含 `web_search` 和 `paper_search`
- LoRA、QLoRA、DoRA 各有来自原始论文的 Evidence
- `answer` 中的 `[E#]` 都能在 `evidence` 和独立 Evidence 接口中找到
- `citations` 中每个主要 Claim 都至少绑定一个 Evidence ID
- API 重启后仍能通过 `task_id` 查到 Answer、Trace、Evidence 和 Citation

自动化测试使用 Mock Provider，不消耗 Tavily 或 Semantic Scholar API 配额。真实验收需要在本地 `.env` 配置 Tavily Key；Semantic Scholar 匿名接口可能受共享限流影响，因此建议同时配置其 API Key。

### Phase 3 本地验收

以下命令适用于 Windows PowerShell。先启动 Compose，并确认 `api`、`worker`、`postgres`、`redis`、`qdrant` 为正常状态：

```powershell
docker compose up -d --build
docker compose ps -a
Invoke-RestMethod http://127.0.0.1:8000/health/ready |
  ConvertTo-Json -Depth 5
```

创建项目：

```powershell
$projectBody = @{ name = "Phase 3 验收"; description = "本地论文知识库" } |
  ConvertTo-Json -Compress
$project = Invoke-RestMethod `
  -Method Post `
  -Uri "http://127.0.0.1:8000/projects" `
  -ContentType "application/json; charset=utf-8" `
  -Body ([System.Text.Encoding]::UTF8.GetBytes($projectBody))
$projectId = $project.id
```

准备一个可复制文本的 PDF，把路径替换为你的实际文件。使用 `curl.exe` 可兼容 Windows PowerShell 5.1 的 multipart 上传：

```powershell
$pdfPath = "D:\papers\lora.pdf"
$uploadJson = curl.exe -sS -X POST `
  "http://127.0.0.1:8000/projects/$projectId/documents" `
  -F "file=@$pdfPath;type=application/pdf" `
  -F "title=LoRA Paper"
$document = $uploadJson | ConvertFrom-Json
$documentId = $document.id
```

后台 Worker 首次运行会下载 Dense 和 BM25 模型。轮询直到 `ready`；如果变为 `failed`，命令会输出错误码和错误信息：

```powershell
do {
  Start-Sleep -Seconds 2
  $document = Invoke-RestMethod `
    "http://127.0.0.1:8000/projects/$projectId/documents/$documentId"
  $document | Select-Object status,page_count,chunk_count,index_version,error_code,error_message
} while ($document.status -in @("queued", "processing"))

if ($document.status -ne "ready") { throw "PDF indexing failed: $($document.error_code)" }
```

执行默认的 Hybrid + Reranker 查询。首次查询还会下载 Reranker 模型，因此耗时会明显长于后续请求：

```powershell
$ragBody = @{
  question = "这篇论文提出了什么方法？请指出依据。"
  document_ids = @($documentId)
  retrieval_mode = "hybrid_rerank"
  top_k = 5
} | ConvertTo-Json -Depth 5 -Compress

$rag = Invoke-RestMethod `
  -Method Post `
  -Uri "http://127.0.0.1:8000/projects/$projectId/rag/query" `
  -ContentType "application/json; charset=utf-8" `
  -Body ([System.Text.Encoding]::UTF8.GetBytes($ragBody)) `
  -TimeoutSec 300

$rag.answer
$rag.evidence | Format-Table citation_key,title,page,section,relevance,document_id,chunk_id
$rag.citations | ConvertTo-Json -Depth 5
```

验收时确认：`evidence` 全部属于当前项目和选定文档；每项都有 `page`、原文 `excerpt` 与 `chunk_id`；回答里的 `[E#]` 都存在于 `evidence`，且 `citations` 的映射一致。还可以把 `retrieval_mode` 分别改为 `dense`、`sparse`、`hybrid` 做对照。

通过 Agent 验收 `knowledge` 路由：

```powershell
$agentBody = @{
  objective = "根据我上传的论文解释其核心方法"
  mode = "knowledge"
  project_id = $projectId
  document_ids = @($documentId)
  retrieval_mode = "hybrid_rerank"
} | ConvertTo-Json -Depth 5 -Compress

$agent = Invoke-RestMethod `
  -Method Post `
  -Uri "http://127.0.0.1:8000/agent/run" `
  -ContentType "application/json; charset=utf-8" `
  -Body ([System.Text.Encoding]::UTF8.GetBytes($agentBody)) `
  -TimeoutSec 300

$agent | ConvertTo-Json -Depth 10
Invoke-RestMethod "http://127.0.0.1:8000/agent/tasks/$($agent.task_id)" |
  ConvertTo-Json -Depth 10
```

使用 Tavily 验收本地与外部资料混合，同时避免 Semantic Scholar 匿名限流影响本阶段结果：

```powershell
$mixedBody = @{
  objective = "对照上传论文和 Web 资料，解释 LoRA 的参数效率、初始化和推理行为"
  mode = "research"
  project_id = $projectId
  document_ids = @($documentId)
  retrieval_mode = "hybrid_rerank"
  research_sources = @("web")
} | ConvertTo-Json -Depth 6 -Compress

$mixed = Invoke-RestMethod `
  -Method Post `
  -Uri "http://127.0.0.1:8000/agent/run" `
  -ContentType "application/json; charset=utf-8" `
  -Body ([System.Text.Encoding]::UTF8.GetBytes($mixedBody)) `
  -TimeoutSec 600

$mixed | ConvertTo-Json -Depth 10

if ($mixed.status -ne "succeeded") { throw "Mixed research failed: $($mixed.status)" }
if (-not ($mixed.tool_calls | Where-Object { $_.tool_name -eq "web_search" -and $_.status -eq "succeeded" })) {
  throw "Tavily web_search did not succeed"
}
if (-not ($mixed.tool_calls | Where-Object { $_.tool_name -eq "knowledge_search" -and $_.status -eq "succeeded" })) {
  throw "Local knowledge_search did not succeed"
}
if ($mixed.tool_calls | Where-Object { $_.tool_name -eq "paper_search" }) {
  throw "paper_search should not run when research_sources contains only web"
}
if (-not ($mixed.evidence | Where-Object { $_.provider -eq "tavily" -and $_.source_type -eq "web" })) {
  throw "Tavily Evidence is missing"
}
if (-not ($mixed.evidence | Where-Object {
  $_.provider -eq "qdrant" -and $_.source_type -eq "document" -and
  $_.document_id -eq $documentId -and $_.chunk_id -and $_.page
})) {
  throw "Project document Evidence is missing or out of scope"
}
```

验收结果必须同时包含 Tavily `web` 和 Qdrant `document` Evidence，且本地 Evidence 的 `excerpt` 是服务器保存的原始 PDF 分块，不依赖模型逐字符复写。若需要完整 Web + 论文研究，省略 `research_sources` 或指定 `@("web", "paper")`；此时 Semantic Scholar 失败会如实返回 `partial`。

RAG Benchmark 示例位于 `benchmarks/rag/example.jsonl`。把占位 ID 替换为人工标注的 `document_id`/`chunk_id` 后运行：

```powershell
uv run python -m autoscholar.evaluation.rag `
  --dataset benchmarks/rag/my-benchmark.jsonl `
  --project-id $projectId `
  --modes dense sparse hybrid hybrid_rerank `
  --ks 5 10
```

### Phase 4 本地验收

以下命令适用于当前 Windows 环境，并显式使用 D 盘安装的 Docker：

```powershell
$docker = "D:\Applications\Docker\resources\bin\docker.exe"
& $docker compose build sandbox-image
& $docker compose --profile datasets run --rm mnist-init
& $docker compose up -d --build
& $docker compose ps -a

$health = Invoke-RestMethod "http://127.0.0.1:8000/health/ready"
$health.capabilities | Format-List
if ($health.capabilities.sandbox.status -ne "ok") { throw "Sandbox is not ready" }
if ($health.capabilities.mnist_dataset.status -ne "ok") { throw "MNIST is not ready" }

& $docker compose exec -T sandbox-manager python -m autoscholar.sandbox.smoke
```

执行真实 Coding Agent 任务：

```powershell
$codingBody = @{
  objective = @"
在空工作区中创建一个小型、可测试的 PyTorch MNIST MLP 项目。
要求包含模型、训练入口和 pytest；使用 MNIST_ROOT 环境变量、download=False、固定随机种子，
测试真实 MNIST 的一个小批次，验证输出形状、有限损失以及反向传播后参数发生更新。
"@
  mode = "coding"
} | ConvertTo-Json -Compress

$coding = Invoke-RestMethod `
  -Method Post `
  -Uri "http://127.0.0.1:8000/agent/run" `
  -ContentType "application/json; charset=utf-8" `
  -Body ([System.Text.Encoding]::UTF8.GetBytes($codingBody)) `
  -TimeoutSec 1800

$coding | ConvertTo-Json -Depth 10
if ($coding.status -ne "succeeded") { throw "Coding task failed" }
if ($coding.mode -ne "coding") { throw "Coding mode was not used" }
if (-not ($coding.tool_calls | Where-Object { $_.tool_name -eq "static_check" -and $_.status -eq "succeeded" })) {
  throw "Static check did not pass"
}
if (-not ($coding.tool_calls | Where-Object { $_.tool_name -eq "run_pytest" -and $_.status -eq "succeeded" })) {
  throw "pytest did not pass"
}
```

查看生成文件、校验值和持久化执行日志：

```powershell
$workspace = Invoke-RestMethod `
  "http://127.0.0.1:8000/agent/tasks/$($coding.task_id)/workspace"
$workspace.files | Format-Table path,size_bytes,sha256

$sourcePath = ($workspace.files | Where-Object { $_.path -like "source/*.py" } |
  Select-Object -First 1).path
$encodedPath = ($sourcePath -split "/" | ForEach-Object {
  [System.Uri]::EscapeDataString($_)
}) -join "/"
Invoke-RestMethod `
  "http://127.0.0.1:8000/agent/tasks/$($coding.task_id)/workspace/files/$encodedPath" |
  Select-Object path,content
```

自动化验收中的故障脚本会先制造 `SyntaxError`，确认错误被分类后由 Agent 修改文件并重新运行；因此不要求真实模型每次都故意产生错误。可单独执行：

```powershell
uv run pytest tests/test_coding_agent.py tests/test_coding_workspace.py tests/test_sandbox.py
```

Phase 4 验收标准是：工作区不能越界，沙箱看不到密钥且不能联网，超时会终止并清理容器，真实 MNIST 项目静态检查和 pytest 通过，强制故障用例至少完成一次修复。模型检查点、图表、实验指标和通用 Artifact 生命周期留到 Phase 5。

## 开发路线

| 阶段 | 重点 |
|---|---|
| Phase 0 | 工程骨架、基础设施、统一 LLM Provider、`/chat` |
| Phase 1 | 最小 LangGraph Agent、Calculator/Python、任务与轨迹持久化 |
| Phase 2 | Web/论文检索、Evidence/Citation、Research Agent |
| Phase 3 | PDF 文档处理、Qdrant、RAG 知识库 |
| Phase 4 | 任务工作区、代码生成与修复、隔离 Docker 沙箱 |
| Phase 5–6 | 实验指标与产物、Reviewer/Replanning |
| Phase 7–9 | Checkpoint、Memory、Human-in-the-loop、MCP、Web 工作台 |
| Phase 10–11 | 全链路评测、安全加固、CI/CD 与部署 |

## 分支与提交约定

- 日常开发和阶段同步使用 `dev`。
- 每个关键模块通过测试后独立提交并推送到 `origin/dev`。
- `main` 是受审核分支；任何合并都必须由项目所有者单独检查并明确批准。
- 本地设计文档、`.env` 和运行产物不得提交。
