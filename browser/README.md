# 浏览器版

在仓库根目录配置 `.env` 后，双击本目录中的 [Start-AutoScholar.cmd](Start-AutoScholar.cmd)。启动器使用根目录的 Compose 配置启动后端，并在浏览器中打开工作台。[完整启动步骤](../README.md)

[src/](src/) 包含两端共用的 React 工作台，[docker/](docker/) 包含浏览器版生产镜像及 Nginx 配置。可在本目录运行 `npm ci`、`npm test` 和 `npm run build`。
