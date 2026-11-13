# Windows 首次启动自动配置设计

日期：2026-11-13

## 目标

Windows 用户从 GitHub 下载或克隆 ShortVideoDesk 后，双击 `start-local.cmd` 即可完成本地工作台所需环境的首次配置并启动服务。后续启动复用已安装的软件和项目依赖。遇到缺少 winget、网络故障或安装失败时，用中文说明具体原因和下一步操作。

## 当前行为

- `.venv` 和 `frontend/node_modules` 已被 `.gitignore` 排除，不会随仓库克隆。
- `start-local.cmd` 只检查 `.venv`、Node/npm 和 Vite 依赖，缺少时退出。
- README 要求手动安装 Python 3.11、Node、uv、Chrome 和 FFmpeg，再运行数条命令。
- 工作台说明使用 `requirements-poc.txt`，启动器默认安装 `requirements.txt`。两份清单各有工作台需要的包，不能只安装其中一份就假定工作台依赖齐全。

## 推荐方案

### 首次启动流程

保留 `start-local.cmd` 作为面向用户的唯一入口，并让它调用一个职责清楚的 PowerShell 配置脚本。PowerShell 进程负责检查工具、调用 winget、刷新当前进程的 PATH、准备本地依赖并启动工作台，使新安装的 Node 和 FFmpeg 在本次启动中即可被后端及 Vite 找到。

当工具缺失时，通过 winget 安装到 Windows：

| 工具 | winget 包 ID | 用途 |
| --- | --- | --- |
| Python 3.11 | `Python.Python.3.11` | 创建工作台虚拟环境 |
| uv | `astral-sh.uv` | 创建环境并通过系统证书安装 Python 包 |
| Node.js LTS（含 npm） | `OpenJS.NodeJS.LTS` | 运行 Vite 开发服务器 |
| Git | `Git.Git` | 获取当前依赖清单引用的 GitHub Python 包 |
| FFmpeg | `Gyan.FFmpeg` | 媒体探测、音频抽取和视频处理 |

调用 winget 时使用精确包 ID，不静默接受软件包自身的许可协议；若安装器要求用户确认或提升权限，显示其原生提示。先检测已有工具，避免对已满足的依赖重复安装。Python、uv、Node 和 Git 属于配置及启动所需工具；winget 不可用或安装失败时，输出中文错误和恢复步骤。FFmpeg 仍按现有行为作为可选项：不可用时提醒相关媒体功能受限，但允许工作台启动。

### 项目本地依赖

- 在项目目录创建 `.venv`，使用 Python 3.11。
- 增加 `requirements-workbench.txt`，以 `requirements.txt` 为基础并加入工作台所需的 `faster-whisper` 和 OpenCC 依赖；独立下载器继续使用原来的 `requirements.txt`。
- 通过 `uv pip install --system-certs` 安装工作台依赖。
- 安装 Playwright 管理的 Chromium；不要求用户另行安装 Google Chrome 才能首次启动。
- 记录 requirements 和 `frontend/package-lock.json` 的摘要。只有清单改变、环境版本不匹配或必要依赖缺失时才重新安装；这样第二次启动不需要重新解析 Python/npm 依赖。
- Node/npm 可用后，若前端依赖缺失或锁文件摘要变化则运行 `npm --prefix frontend ci`。
- 使用工作台依赖清单启动 `launcher.py`。启动器保留现有默认清单，保证 `start.bat`、macOS 和 Linux 的独立下载器入口不变。

首次配置完成后启动本地服务。后续运行先检查缺失项，仅补齐需要的组件，再启动。数据库、项目数据和素材目录保持原有位置，不随环境重建处理。

### 用户提示与文档

- 所有新手可见的配置步骤、进度和错误使用简明中文。
- 明确提示首次运行需要联网下载 Python 包、npm 包和 Chromium。
- winget 缺失时，给出安装 Windows App Installer/winget 的步骤，并说明可手动安装各工具后重试。
- README 将多条手动命令改为双击启动说明，并标出 Windows/winget 与联网要求。

## 备选方案

1. **winget 安装 Windows 工具，项目内安装 Python 包（推荐）**：复用 Windows 包管理器及仓库现有的 uv/Python 工作流；依赖易更新，项目无需携带大型工具包。首次安装需要网络，软件安装可能要求用户确认或管理员权限。
2. **将 Python、Node 和 FFmpeg 全部下载到项目目录**：不依赖 winget，也较少触碰系统 PATH；但下载源、校验、版本升级、磁盘占用和便携版维护都要由项目负责，首次实现和后续维护成本更高。
3. **只自动安装 Python，其余工具仍要求手动安装**：实现工作量较低，但不满足工作台前端和媒体工具都能自动准备的目标。

## 错误处理

- 所需工具缺失且 winget 不可用：停止启动，指出缺少什么、提供安装 winget 的步骤，并列出可选手动安装方式。
- 必需工具的 winget 安装失败：显示失败工具和包 ID，保留 winget 原始错误的关键内容，并提供重试命令；FFmpeg 按可选项处理。
- Python 虚拟环境或依赖安装失败：停止启动，显示失败步骤及对应命令；不要删除项目数据。
- npm 或 Chromium 下载失败：停止启动并指出网络依赖及重试入口。
- FFmpeg 安装失败：给出中文警告后继续启动。

## 验收要点

- 从没有 `.venv`、没有 `node_modules` 的克隆目录双击 `start-local.cmd`，能够自动配置并启动。
- 第二次双击可以复用既有环境并启动服务。
- 缺少 winget 时会得到清晰的中文补救步骤。
- Python、npm 或 Chromium 安装失败时不会误报服务已启动。
- FFmpeg 缺失只影响媒体相关功能并显示提醒。
- `start.bat` 与其他平台的独立下载器仍按 `requirements.txt` 工作。
- README 说明新电脑首次启动的网络、许可确认和权限需求。

## 参考

- [Microsoft Learn：winget install](https://learn.microsoft.com/en-us/windows/package-manager/winget/install)
- [Python 3.11 winget 包](https://github.com/microsoft/winget-pkgs/tree/master/manifests/p/Python/Python/3/11)
- [Node.js LTS winget 包](https://github.com/microsoft/winget-pkgs/tree/master/manifests/o/OpenJS/NodeJS/LTS)
- [uv winget 包](https://github.com/microsoft/winget-pkgs/tree/master/manifests/a/astral-sh/uv)
- [Git for Windows winget 包](https://github.com/microsoft/winget-pkgs/tree/master/manifests/g/Git/Git)
- [FFmpeg winget 包](https://github.com/microsoft/winget-pkgs/tree/master/manifests/g/Gyan/FFmpeg)
