# AutoScholar

AutoScholar 用于检索论文、分析项目 PDF，并生成带引用的研究报告。选择一个版本，从对应目录开始。

| 版本 | 运行方式 | 入口 |
| --- | --- | --- |
| [浏览器版](browser/) | Docker Compose 运行后端，在浏览器中打开工作台 | [配置与启动说明](browser/README.md) |
| [Windows 桌面版](desktop/) | Windows 11 x64 原生客户端，使用本地 SQLite 和按需启动的检索引擎 | [构建与安装说明](desktop/README.md) |

每个目录都是完整项目，包含自己的 Python 后端、React 前端、依赖锁和测试。可以单独复制、构建或维护任一版本。若两端都需要同一项修复，请分别修改和验证。
