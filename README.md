# ClipSoon

ClipSoon 是一款面向 macOS 和 Windows 的本地剪贴板历史工具。它像 Spotlight / Raycast 一样按需出现：复制内容后，通过全局快捷键呼出面板，搜索、预览并快速粘贴过去复制过的文本、图片或文件。

> 本地优先：历史数据仅保存在本机 SQLite 数据库和图片目录中，不上传网络。

## 界面预览

### 主窗口

![ClipSoon v0.9.3 主窗口](docs/images/clipsoon-main.jpg)

### 设置窗口

<img src="docs/images/clipsoon-settings.png" alt="ClipSoon v0.9.3 设置窗口" width="580">

## 功能特性

- 记录文本、图片和本地文件，相同内容再次复制时自动去重并提升到最近位置。
- 支持 Unicode 搜索、确定性匹配排序和“全部 / 文本 / 截图 / 文件”类型筛选。
- 紧凑单行列表，图片显示真实缩略图，右侧显示内容预览与类型信息；单个文本文件只读预览前 220 个字符，未完整展示时以 `...` 结尾。
- 图片缩略图和大图预览在后台加载，超大图片不会阻塞列表选中，已加载结果会缓存复用。
- 支持 Finder / 资源管理器式 `Shift`、`Ctrl` / `Command` 多选，以及右键删除或清空历史。
- 默认双击 `Ctrl` 呼出；可在设置中切换修饰键或录制自定义组合键。
- `Enter` 或双击列表项即可发送，`Esc` 或面板失去焦点后隐藏。
- 支持历史容量、保留天数、粘贴延迟、选择后自动粘贴与失焦隐藏等设置。
- 浅色、深色和跟随系统主题覆盖设置下拉列表、列表右键菜单等弹出组件，文字、悬停和选中状态均保持清晰对比。
- “记住上次状态”默认关闭；启用后默认在面板隐藏后的 3 秒内恢复类型 Tab、搜索内容、多选集合和当前焦点项，超时后回到“全部”的完整列表顶部。
- 鼠标点击搜索框左侧的放大镜即可打开设置，系统托盘菜单也保留设置入口。
- 底部状态栏空闲时保持简洁，仅在操作反馈、错误或需要授权时显示信息。

## 快捷操作

| 操作 | 效果 |
| --- | --- |
| 双击 `Ctrl` | 呼出 ClipSoon（默认） |
| `↑` / `↓` | 移动当前选择 |
| `Shift` + `↑` / `↓` | 连续多选 |
| `Ctrl` / `Command` + 鼠标点击 | 切换单个列表项的选中状态 |
| `Tab` / `Shift` + `Tab` | 正向 / 反向循环切换类型筛选 |
| `Enter` | 将当前内容发送到原应用 |
| `Esc` | 隐藏面板 |
| 点击放大镜 | 打开设置 |

## 系统要求

- Python `3.12`（开发和打包环境统一，当前不支持 Python 3.13）。
- macOS 13 或更高版本。
- Windows 10 / 11。

### macOS 权限

全局按键监听和跨应用自动粘贴需要在“系统设置 → 隐私与安全性 → 辅助功能”中允许 Terminal 或打包后的 ClipSoon。应用只在未授权时显示提示，并可直达对应的系统设置页。

### Windows 权限

Windows 不需要开启 macOS 式的辅助功能权限。如果目标应用以管理员身份运行，ClipSoon 也需要以相同权限运行才能向其发送粘贴按键。

## 使用源码启动

日常开发和功能验收应直接从当前源码启动，不需要先打包。项目要求使用 Python 3.12，建议在仓库根目录创建项目专用的 `.venv`。

下面出现的 Python 命令不能跨平台混用：

- `python3.12` 是 macOS 安装 Python 3.12 后常见的命令名。
- `py -3.12` 是 Windows 的 Python Launcher 命令，用来明确选择已安装的 Python 3.12；如果系统中只有 `py` 而没有 `python` 或 `python3.12`，这是正常情况。
- 创建 `.venv` 后，安装依赖和启动源码都使用该虚拟环境里的 Python：macOS 为 `.venv/bin/python`，Windows 为 `.venv\Scripts\python.exe`。两者是平台相关路径，不可互换。

### macOS

首次克隆并安装开发依赖：

```bash
git clone git@github.com:Helchan/ClipSoon.git
cd ClipSoon
python3.12 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -e '.[dev,package]'
```

以后可以双击仓库根目录的 `run.command`，也可以在终端运行：

```bash
./run.command
```

如果系统阻止执行，先运行一次：

```bash
chmod +x run.command scripts/run_macos.command
```

从 Terminal 或 PyCharm 启动源码时，全局按键监听和跨应用自动粘贴所需的辅助功能权限应授予实际启动 ClipSoon 的 Terminal 或 PyCharm。

### Windows

在 CMD 中首次安装开发依赖：

```bat
git clone git@github.com:Helchan/ClipSoon.git
cd ClipSoon
py -3.12 -m venv .venv
.venv\Scripts\python.exe -m pip install --upgrade pip
.venv\Scripts\python.exe -m pip install -e ".[dev,package]"
```

如果 PyCharm Terminal 使用 PowerShell，则虚拟环境中的可执行文件需要使用 `./` 对应的 Windows 写法 `.\`：

```powershell
git clone git@github.com:Helchan/ClipSoon.git
Set-Location ClipSoon
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -e ".[dev,package]"
```

以后可以双击仓库根目录的 `run.bat`，也可以在 CMD 中运行：

```bat
run.bat
```

也可以不经过启动脚本，直接用虚拟环境里的 Python 启动当前源码：

```bat
.venv\Scripts\python.exe -m clipsoon --show
```

PowerShell 对应命令为：

```powershell
.\.venv\Scripts\python.exe -m clipsoon --show
```

如果 Windows 找不到 `py`，可将第一条创建环境的命令改为 Python 3.12 的实际安装路径，例如：

```bat
"C:\Program Files\Python312\python.exe" -m venv .venv
```

两个平台的启动入口都会先关闭本项目的旧打包实例和旧源码实例，再使用当前 `.venv` 执行 `python -m clipsoon --show`，避免因为旧进程未退出而验证到过期代码。不要在 PyCharm 断点调试期间再次运行启动脚本，否则脚本可能会结束正在调试的旧源码进程。

## 开发与断点调试

### PyCharm 项目解释器

用 PyCharm 打开仓库根目录，在 `Settings / Preferences → Project: ClipSoon → Python Interpreter` 中添加已有的本地解释器：

- macOS：`<项目目录>/.venv/bin/python`
- Windows：`<项目目录>\.venv\Scripts\python.exe`

不要选择系统 Python 或其他项目的虚拟环境。

### PyCharm 启动配置

在 `Run → Edit Configurations` 中新增 Python 配置：

| 配置项 | 值 |
| --- | --- |
| Name | `ClipSoon Debug` |
| Run | `Module name` |
| Module name | `clipsoon` |
| Parameters | `--show` |
| Working directory | 仓库根目录 |
| Python interpreter | 当前项目的 `.venv` |

建议在 `Environment variables` 中为调试实例设置独立数据目录，避免调试数据与正式使用的数据混在一起：

- macOS：`CLIPSOON_DATA_DIR=/Users/<用户名>/Library/Application Support/ClipSoon-dev`
- Windows：`CLIPSOON_DATA_DIR=C:\Users\<用户名>\AppData\Local\ClipSoon-dev`

配置完成后使用 PyCharm 的 Debug 启动并设置断点。常用调试入口：

- `clipsoon/app.py`：应用启动、窗口显示、设置和生命周期。
- `clipsoon/system.py`：剪贴板监听、全局快捷键和自动粘贴。
- `clipsoon/core.py`：设置、历史模型和 SQLite 数据。
- `clipsoon/search.py`：搜索与匹配排序。
- `clipsoon/ui.py`：窗口、列表、预览和设置界面。

ClipSoon 是系统托盘常驻应用，主窗口隐藏不代表进程退出。如果启动立即返回退出码 `2`，通常表示已有实例持有单实例锁；先从托盘退出旧实例，再重新调试。应用日志位于所用数据目录下的 `logs/clipsoon.log`。

## 测试

macOS：

```bash
.venv/bin/ruff check .
QT_QPA_PLATFORM=offscreen .venv/bin/pytest -q
.venv/bin/coverage run -m pytest
.venv/bin/coverage report
```

Windows CMD：

```bat
.venv\Scripts\python.exe -m ruff check .
set QT_QPA_PLATFORM=offscreen
.venv\Scripts\python.exe -m pytest -q
.venv\Scripts\python.exe -m coverage run -m pytest
.venv\Scripts\python.exe -m coverage report
```

Windows PowerShell：

```powershell
.\.venv\Scripts\python.exe -m ruff check .
$env:QT_QPA_PLATFORM = "offscreen"
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m coverage run -m pytest
.\.venv\Scripts\python.exe -m coverage report
```

也可以在 PyCharm 中右键 `tests` 目录运行 pytest；对应测试配置应设置环境变量 `QT_QPA_PLATFORM=offscreen`。

## 打包

需要生成可分发产物时，使用仓库根目录下的平台脚本：

- macOS：双击 `build_macos.command`，产物为 `dist/ClipSoon.app`。
- Windows：双击 `build_windows.bat`，产物为 `dist\ClipSoon\ClipSoon.exe`。

Windows 包需要在 Windows 10 / 11 主机上生成。两个脚本都使用 PyInstaller one-dir，避免 one-file 每次启动时的临时解包开销。macOS 脚本会执行 ad-hoc 签名和严格签名校验；正式对外分发仍需要 Developer ID 签名与公证。

## 自动发布

推送 `v*` 版本标签后，[GitHub Actions](.github/workflows/release.yml) 会自动构建并发布：

- Windows x64：`ClipSoon-vX.Y.Z-windows-x64.zip`。
- macOS Apple Silicon（M1 / M2 / M3 / M4）：`ClipSoon-vX.Y.Z-macOS-arm64.zip`。
- 两个包的 SHA-256 校验文件：`SHA256SUMS.txt`。

发布前先将 `pyproject.toml` 和 `clipsoon/__init__.py` 中的版本保持一致，提交并推送到 `main`，然后执行：

```bash
git tag v0.9.3
git push origin v0.9.3
```

Release 会使用标签名生成说明并附加两个平台包。工作流使用 Windows x64 runner 和 macOS 15 ARM64 runner，并在发布前校验 Git 标签、运行时版本与项目版本一致。macOS 产物当前为 ad-hoc 签名，未使用 Developer ID 且未执行 Apple 公证。

## 项目结构

```text
clipsoon/
├── app.py       # 应用装配与生命周期
├── core.py      # 模型、设置与 SQLite 历史库
├── search.py    # Unicode 搜索与匹配排序
├── system.py    # 剪贴板、快捷键与平台边界
└── ui.py        # PySide6 界面
tests/            # 自动化测试
docs/             # 产品规格、架构、竞品调研和验收记录
```

## 文档

- [产品规格与验收](docs/产品规格与验收.md)
- [架构设计](docs/架构设计.md)
- [竞品调研](docs/竞品调研.md)
- [验收报告](docs/验收报告.md)

## 隐私

ClipSoon 的核心功能不发起网络请求。文本和文件记录保存在本机 SQLite 数据库中，图片以 PNG 文件保存在应用数据目录。你可随时在设置中暂停记录、打开数据目录或清空未置顶历史。
