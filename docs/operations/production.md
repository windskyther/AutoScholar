# 单操作者生产打包与本地验收

生产配置是独立的 `compose.production.yaml`。每条命令都显式指定它，**不要与开发
`compose.yaml` 合并**，否则开发端口、挂载和配置可能被保留。以下启动命令由操作者在
准备切换时执行；代码合并不会启动或替换日常服务。

## 构建与配置

需要 Docker Compose 2.24.4 或更高版本、Python 3.12 项目环境。Windows 的 Docker
Desktop 保留在 D 盘；如需启动，使用隐藏窗口。生产镜像分别运行 API/Worker
（UID/GID 10001）和 Nginx 静态 Web（101），应用根文件系统只读。

```powershell
Copy-Item .env.production.example .env.production
.venv\Scripts\python.exe scripts/configure_production.py
.venv\Scripts\python.exe scripts/build_production.py --env-file .env.production
```

秘密初始化只接受全新空目录，默认生成 `data/deployment/secrets`。数据库、Redis、
Qdrant、操作者和管理器各用独立随机凭据；不会覆盖旧文件，也不会修改开发 `.env`。
`api_token` 是工作台登录使用的 Token，只在本地读取和保管。目录与真实配置都被 Git
忽略。Linux 父目录权限为 0700；Windows 部署时应使用当前操作者专属的 NTFS ACL。
文件为显式挂载给非 root 容器保留读权限；不要把它们复制到公共共享目录。

按需在四个空文件中填写供应商密钥，再启用 `.env.production` 的对应路径：

| 本地文件 | 环境项的值 |
|---|---|
| `llm_api_key` | `LLM_API_KEY_FILE=/run/secrets/llm_api_key` |
| `embedding_api_key` | `EMBEDDING_API_KEY_FILE=/run/secrets/embedding_api_key` |
| `tavily_api_key` | `TAVILY_API_KEY_FILE=/run/secrets/tavily_api_key` |
| `semantic_scholar_api_key` | `SEMANTIC_SCHOLAR_API_KEY_FILE=/run/secrets/semantic_scholar_api_key` |

同时填写 `LLM_BASE_URL` 和 `LLM_MODEL`。未配置模型时，页面可以管理已有资料，但不会
开放需要模型的任务提交。默认使用本地嵌入与重排模型，需要首次下载公开模型并写入
独立模型缓存卷。若改用远程嵌入，需单独配置现有 Settings 对应项并保持向量维度一致；
不要直接复用维度不匹配的旧 Qdrant 集合。

Compose 使用逐服务授权的只读文件挂载；它不是加密保管库，拥有 Docker 管理权限的
用户仍属于受信范围。参见 [Docker secrets 说明](https://docs.docker.com/compose/how-tos/use-secrets/)。
`DATABASE_URL_FILE` 与 `REDIS_URL_FILE` 也受支持：最多 8192 字节 UTF-8、无内部空白，
允许末尾换行；同名直接配置与文件配置冲突时启动失败。修改文件后应在维护窗口重启相关
进程，不能把文件编辑等同于即时轮换。

构建脚本要求干净 Git 提交，向两类镜像写入完整提交 SHA，后端还绑定 `uv.lock` 摘要和
唯一迁移 head，前端绑定 `package-lock.json` 摘要。`--development` 明确生成
`working-tree` 未封存镜像。构建不会发布镜像。可通过 `AUTOSCHOLAR_API_IMAGE` 和
`AUTOSCHOLAR_WEB_IMAGE` 选择自己的本地版本标签；API 和两类 Worker 必须使用同一版本。
默认基础镜像来自官方分发；网络无法访问 Docker Hub 时，可在构建前将
`NODE_BUILD_IMAGE`、`NGINX_RUNTIME_IMAGE` 指向经核验的 Docker Official Images 镜像源。

## 本机入口

```powershell
docker compose --env-file .env.production -f compose.production.yaml config --quiet
docker compose --env-file .env.production -f compose.production.yaml up -d --no-build --wait
docker compose --env-file .env.production -f compose.production.yaml ps -a
```

默认打开 `http://127.0.0.1:8080`，Web 与 `/api/*` 同源；请求只剥离 `/api`，保留
`/workbench` 语义。仅 Web 发布回环端口，API、PostgreSQL、Redis、Qdrant 和管理器均
无宿主机端口。Web 独占入站网络，通过内部 edge 网络访问 API；内部服务只连接各自网络。
API/业务 Worker 的独立 egress 网络用于已授权的供应商与公开模型访问。

先完成空卷初始化和迁移，再启动 API/Worker。健康检查分别覆盖进程存活、Worker 心跳
与依赖进程；带 Bearer 访问 `/api/health/ready` 才能查看实际依赖状态和能力。心跳不保证
任务正在取得进展，不能代替任务状态、Journal 和错误诊断。应用停止宽限为 45 秒。

Nginx 对 API/SSE 禁用缓存和响应缓冲，禁止失败后的上游请求重发；保留事件游标和心跳。
代理上传上限 51 MiB，包含应用的 50 MiB 文件上限与最多 1 MiB multipart 开销，应用仍
逐字节校验。静态入口要求重新验证缓存，带哈希的资源可长期缓存。API 404 不回退 SPA。
原理见 [Nginx 代理文档](https://nginx.org/en/docs/http/ngx_http_proxy_module.html)。

Host 使用明确白名单。Nginx 清除转发地址/协议头，Uvicorn 使用 `--no-proxy-headers`，
准入按操作者限额，不依赖可伪造的客户端 IP。不要通过放开通配 Host 或信任全部代理
解决连接问题。访问日志只保留请求 ID、方法、状态、耗时和字节数；Nginx 普通错误日志
不会输出可能带秘密的请求 URI。诊断使用请求 ID 和应用安全日志。

管理器是唯一持有 Docker socket 的长期服务，以 root 运行并使用独立内部 Token。
它拥有宿主 Docker 控制能力，属于明确的受信例外。根权限初始化器仅执行一次空卷目录
初始化，不递归修改内容；其余应用服务不会挂载 socket。详见
[管理器边界](../security/sandbox.md)。生产实验需要另外准备兼容的沙箱镜像和项目专属
MNIST 数据卷；空数据卷不能通过真实训练验收，不会自动下载或复用开发数据。

## HTTPS 入口

远程访问先准备匹配访问主机名的受信证书。TLS overlay 使用 `!override` 替换 HTTP
端口，仍默认绑定本机 127.0.0.1；对外监听地址应在已确定的部署配置中明确选择。不要
仅为了远程访问把带 Token 的入口暴露为 HTTP。

在本地 `.env.production` 配置证书路径、TLS 端口、明确主机名，并把实际 HTTPS origin
加入 `API_ALLOWED_ORIGINS`（包含端口、没有路径）。后端不信任代理协议头，所以此项
对 TLS 浏览器写入是必需的。

```dotenv
PROD_TLS_CERT=D:/private/certificates/server.pem
PROD_TLS_KEY=D:/private/certificates/server.key
PROD_TLS_PORT=8443
API_ALLOWED_HOSTS=["localhost","127.0.0.1"]
API_ALLOWED_ORIGINS=["https://localhost:8443","https://127.0.0.1:8443"]
```

```powershell
docker compose --env-file .env.production -f compose.production.yaml -f compose.production.tls.yaml config --quiet
docker compose --env-file .env.production -f compose.production.yaml -f compose.production.tls.yaml up -d --no-build --wait
```

证书不进入镜像。TLS 只启用 1.2/1.3 并发送 HSTS；真实客户端应正常验证受信证书。
下面的验收脚本生成短期自签名证书，其浏览器忽略证书校验仅限该临时回环夹具，不能据此
宣称已验证公网域名、证书链或生产 DNS。

## 零供应商费用的独立验收

先安装本地前端依赖与 Chromium，构建生产镜像后运行：

```powershell
npm.cmd --prefix web ci
node web/scripts/browser.mjs install chromium
.venv\Scripts\python.exe scripts/verify_production.py
.venv\Scripts\python.exe scripts/verify_production.py --tls
```

TLS 测试需要本机 OpenSSL；不在 PATH 时用 `--openssl "D:/path/to/openssl.exe"`。
脚本随机生成 `phase11e-*` 项目、独立凭据和空卷，只读取公开配置，拒绝继承真实应用
环境变量。API/Worker 网络断开外网，向量使用确定性公开夹具，工作流 Worker 保持等待。
测试覆盖真实 PostgreSQL/Redis/Qdrant、PDF 解析索引、浏览器登录与断开、中文报告
下载、SSE 心跳与游标重连、幂等提交和审批控制。夹具路由只通过测试挂载存在，正式
镜像不含测试代码。

结果保存在 Git 忽略的 `data/validation/phase11e-*/`。成功或失败均移除本轮容器与
本轮卷，保留记录；不会删除旧运行数据。此检查验证入口和持久化控制行为，**不执行
真实模型、自主研究或 MNIST 训练**，不替代最终整体验收。

## 既有数据和升级边界

应用卷使用项目作用域名称，不自动引用开发卷。未标记且非空的目录初始化会拒绝执行；
不要向真实数据添加标记以绕过检查，也不要递归 `chown` 开发数据或缓存。旧数据应在
停写和完整备份后复制到新卷，核对数据库、文件摘要与向量维度，并在副本演练后切换。
完整跨存储备份恢复、受控升级回退、供应链发布门槛与最终 CPU 整栈验收仍属后续模块。
普通 `down` 保留数据卷；`down --volumes` 仅用于明确拥有的临时验收项目。

生产 CPU 镜像现使用 `autoscholar-python-sandbox:phase11`，可用
`scripts/build_production.py --sandbox` 一起构建。依赖在镜像构建时按独立哈希锁安装，
运行容器不保留 pip；需要额外依赖时先更新锁文件并重建。当前扫描与未修复上游项见
[容器漏洞处理记录](../security/container-vulnerabilities.md)，镜像发布仍受这些阻断项限制。
