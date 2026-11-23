# Windows 桌面版

运行安装包后，可从开始菜单打开 AutoScholar。构建安装包时，在仓库根目录运行 `desktop/scripts/Build-Desktop.ps1`，产物位于 `desktop/target/AutoScholar-Setup-x64.exe`。

- [src/](src/) 包含 Tauri 窗口、托盘和本地后端进程管理。
- [python/autoscholar/desktop/](python/autoscholar/desktop/) 包含 SQLite 资料库、检索进程、Windows 密钥保护和迁移工具。
- [scripts/](scripts/) 包含打包与桌面验收脚本。

安装包内置从 [browser/](../browser/) 构建的工作台。安装、使用和旧数据导入步骤见[项目说明](../README.md)。
