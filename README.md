# AutoScholar

> 面向 AI/ML 研究与实验的长任务自主智能体平台  
> Autonomous AI/ML Research & Experiment Agent Platform

> [!IMPORTANT]
> AutoScholar 目前处于规划与工程初始化阶段。本文描述的是项目目标与演进路线，相关能力将按里程碑逐步实现。

## 项目简介

AutoScholar 希望将一个复杂的 AI/ML 研究目标转化为可追踪、可恢复、可评测、可复现的完整研究流程。它不止回答问题，还将围绕目标自主规划任务、检索资料、整理证据、生成代码、执行实验、分析结果，并在质量不足时重新规划，最终产出带引用的研究报告。

一个典型任务是：

> 比较 ResNet18 与 Vision Transformer 在 CIFAR-10 小样本场景下的性能差异，并结合相关论文分析产生差异的原因。

## 核心工作流

```mermaid
flowchart LR
    A[研究目标] --> B[规划]
    B --> C[资料检索]
    C --> D[证据整理]
    D --> E[实验设计]
    E --> F[编码与执行]
    F --> G[结果分析]
    G --> H[质量审查]
    H -->|通过| I[研究报告]
    H -->|证据或实验不足| J[重新规划]
    J --> C
```

系统的核心执行模式是：

```text
Plan → Retrieve → Reason → Act → Observe → Evaluate → Replan → Final Answer
```

## 目标能力

- **Agent 编排**：基于显式状态和工作流执行长任务，支持规划、审查、重规划与预算控制。
- **Research 与 RAG**：联合检索论文、Web 和本地知识库，提供可追溯的 Evidence 与 Citation。
- **Coding**：分析代码仓库，创建或修改 Python/PyTorch 代码，并进行静态检查与错误修复。
- **Experiment**：在受限 Docker 沙箱中执行实验，收集指标、日志、图表和模型等产物。
- **Memory 与 HITL**：保存任务、项目和历史经验，在高风险或高成本操作前请求人工审批。
- **Evaluation**：从任务成功率、检索质量、工具调用、代码测试、延迟与成本等维度评测系统。

## 架构概览

AutoScholar 计划采用一个主 LangGraph 与多个专业子图，而不是让多个独立 Agent 自由对话：

```text
Task Intake
    ↓
Planner
    ├── Research Subgraph
    ├── Coding Subgraph
    └── Experiment Subgraph
              ↓
           Reviewer
          ↙        ↘
      Replanner    Writer
```

拟采用的主要技术栈：

| 领域 | 技术 |
|---|---|
| Agent 编排 | LangGraph、LangChain Model/Tool Adapter |
| 后端与任务 | FastAPI、Celery |
| 数据与缓存 | PostgreSQL、Redis |
| 检索 | Qdrant、BM25、RRF、Reranker |
| 实验执行 | Docker、Python、PyTorch |
| 前端 | React、TypeScript、Ant Design |
| 可观测与评测 | Node/Tool Trace、Benchmark、Ablation Study |

模型层将通过统一的 `LLMProvider` 接口解耦具体供应商，以支持不同模型的效果、成本与延迟对比。工具层会先以原生工具跑通端到端流程，再逐步迁移到 MCP。

## 开发路线

| 里程碑 | 对应阶段 | 主要成果 |
|---|---|---|
| 1. Research Assistant | Phase 0–3 | 工程骨架、最小 Agent、论文/Web 检索、Evidence/Citation、RAG 知识库 |
| 2. Autonomous Experiment Agent | Phase 4–6 | 代码生成与修复、Docker 实验、结果分析、Reviewer 与 Replanning |
| 3. AutoScholar Platform | Phase 7–9 | Checkpoint、Memory、Human-in-the-loop、MCP 与完整 Web 工作台 |
| 4. Evaluation & Production | Phase 10–11 | 全链路评测、消融实验、安全加固、CI/CD 与部署 |

项目将遵循以下演进原则：

```text
先跑通 → 再自动化 → 再智能化 → 再平台化 → 最后评测
```

### 当前近期目标：Phase 0

- 建立 Python 项目与基础目录结构。
- 提供 FastAPI 基础服务和 `POST /chat` 接口。
- 接入 PostgreSQL、Redis 与 Docker Compose。
- 实现配置管理、结构化日志和统一 LLM Provider。
- 建立 pytest 测试基础设施。
- 确保服务、数据库、缓存与模型调用链路能够稳定运行。

## 最终演示目标

最终 Demo 将围绕一个完整研究任务展开：比较 CNN 与 Vision Transformer 在 CIFAR-10 小数据场景下的表现。AutoScholar 将自主完成问题拆解、论文与知识库检索、证据整理、实验设计、代码生成、模型训练、错误恢复、指标分析、质量审查、补充实验和报告生成。

这个任务用于同时验证 Planning、Research、RAG、Tool Calling、Coding、PyTorch、Sandbox、Replanning、Memory、Human-in-the-loop 与 Evaluation，而非仅展示一次性问答。

## 项目文档

- [总体设计文档 V1.0](./AutoScholar_%E6%80%BB%E4%BD%93%E8%AE%BE%E8%AE%A1%E6%96%87%E6%A1%A3_V1.0.md)：完整的系统设计、数据模型、API 草案、分阶段开发计划与验收标准。

## 参与项目

项目尚处于早期阶段，欢迎通过 GitHub Issue 或 Discussion 交流使用场景、架构建议和评测思路。安装方式、运行命令、贡献规范与许可证将在工程骨架确定后补充。
