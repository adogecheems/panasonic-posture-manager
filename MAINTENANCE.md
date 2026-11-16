# 安装、维护与构建

日常使用、模式说明和常见问题见[使用说明](README.md)。本页面向负责安装、排障、构建和发布的维护者。

## 安装

从 [GitHub Releases](https://github.com/adogecheems/panasonic-posture-manager/releases) 下载同一版本的安装包和 `SHA256SUMS`，放在同一目录并先校验：

```sh
sha256sum --ignore-missing --check SHA256SUMS
```

Linux 校验输出中应包含目标安装包，结果为 `OK`。Windows 可在下载目录打开 PowerShell，运行以下命令，将所选文件的 Hash 与 `SHA256SUMS` 中同名文件的摘要逐位核对。

```powershell
Get-FileHash -Algorithm SHA256 -Path .\*.msi | Format-List Path, Hash
```

| 平台 | 安装命令 |
| --- | --- |
| Arch Linux | `sudo pacman -U ./panasonic-posture-manager-3.0.1-1-x86_64.pkg.tar.zst` |
| Ubuntu / Debian | `sudo apt install ./panasonic-posture-manager_3.0.1-1_amd64.deb` |
| Fedora | `sudo dnf install ./panasonic-posture-manager-3.0.1-1.x86_64.rpm` |
| Windows（简体中文） | 双击 `panasonic-posture-manager-3.0.1-zh-CN-windows-x64.msi`，按提示重启 |
| Windows（英文） | 双击 `panasonic-posture-manager-3.0.1-en-US-windows-x64.msi`，按提示重启 |

文件名以实际发布版本为准。Ubuntu/Debian 需要 GTK 4.12+ 和 libadwaita 1.4+，依赖由包管理器安装。

Arch 用户也可通过 AUR 安装。以下两条命令任选一条，第一条从源码构建，第二条安装预构建包。

```sh
yay -S panasonic-posture-manager
yay -S panasonic-posture-manager-bin
```

安装或升级 Linux 包后注销并重新登录。需要立即重启当前用户会话服务时运行：

```sh
systemctl --user restart panasonic-posture-manager-session.service
```

## 排查

### Linux

查看当前用户的桌面会话服务：

```sh
systemctl --user status panasonic-posture-manager-session.service
journalctl --user -b -u panasonic-posture-manager-session.service
```

查看系统硬件服务：

```sh
systemctl status panasonic-posture-manager.service
journalctl -b -u panasonic-posture-manager.service
```

KDE Plasma 6 Wayland 为实验性支持，尚未完成真实 Plasma 会话或真机验收；自动化测试通过不代表兼容性验收通过。Plasma 6 需要 `kscreenctl` 或 `kscreen-doctor`；两者都缺失时检查发行版的 KScreen 软件包。

只读硬件探测：

```sh
panasonic-posture-manager-probe
```

探测只读取 sysfs 和机型配置，不改设备状态，也不会提升支持等级。

### Windows

确认硬件服务状态和管理日志：

```powershell
Get-Service PanasonicPostureManagerHardware
Get-Content "$env:ProgramData\PanasonicPostureManager\Logs\hardware-service.log" -Tail 100
```

桌面会话日志位于：

```text
%LOCALAPPDATA%\PanasonicPostureManager\native-display.log
```

| 现象 | 维护操作 |
| --- | --- |
| 服务缺失或界面断线 | 在本地控制台以普通用户打开界面。若服务缺失或启动失败，检查服务日志，用 MSI 修复并按提示重启。特权服务只能从受保护的安装目录运行。 |
| 服务运行但桌面未就绪 | 等待最多五秒。若显示「重试」，查看桌面会话日志，区分显示失败与硬件尚未就绪。 |
| 唤醒后未识别姿态 | 将设备放稳。QV8/QV9/QV1 可在笔记本或帐篷姿态恢复识别，其他兼容机型需要新的物理折叠事件。 |
| 以平板姿态启动时未识别平板 | QV8/QV9/QV1 在没有旧 CSM 恢复记录且没有新鲜非平板几何冲突时，使用一次只读指示器查询建立启动平板基线，无需再次折叠。旧记录、读取失败或冲突仍走安全校验；通用机型仍需合法物理事件。此查询不用于睡眠恢复。 |
| 平板姿态下内置键盘不能唤醒，但电源键可以 | 先短按电源键唤醒，再展开为笔记本姿态比较。应用的 Windows 输入端口不禁用键盘，也不配置键盘唤醒；完全折回时的唤醒能力由设备及 Windows 管理。微软的 [Modern Standby 唤醒来源说明](https://learn.microsoft.com/en-us/windows-hardware/design/device-experiences/modern-standby-wake-sources)要求不可见的内置键盘不唤醒设备。尚需真机区分单纯熄屏和实际睡眠。 |
| 锁屏后方向变化 | 已运行的会话会切回自动模式并继续调整方向。服务刚重启时，先解锁以启动桌面会话。 |

检测偏好与校准位于 `%ProgramData%\PanasonicPostureManager\State\settings.json`，由服务写入。外观设置和显示日志位于当前用户的 `%LOCALAPPDATA%\PanasonicPostureManager`。排查恢复失败时保留恢复记录和日志。

## 卸载

Linux 先停止用户会话，让程序恢复屏幕方向和输入状态：

```sh
systemctl --user stop panasonic-posture-manager-session.service
```

| 平台 | 卸载命令 |
| --- | --- |
| Arch Linux | `sudo pacman -Rns panasonic-posture-manager` |
| Arch Linux（`-bin` 包） | `sudo pacman -Rns panasonic-posture-manager-bin` |
| Ubuntu / Debian | `sudo apt remove panasonic-posture-manager` |
| Fedora | `sudo dnf remove panasonic-posture-manager` |
| Windows | 在「已安装的应用」中卸载，按提示重启 |

Linux 卸载后会保留以下状态，确认屏幕方向已恢复后可手动删除：

- `/var/lib/panasonic-posture-manager/settings.json`
- `${XDG_STATE_HOME:-$HOME/.local/state}/panasonic-posture-manager/`

Windows MSI 会停止并移除服务，同时恢复安装前的平板指示器配置。用户日志和恢复记录可在确认应用已正常退出后，从 `%LOCALAPPDATA%\PanasonicPostureManager` 清理。

## 开发运行

Linux 依赖 Python 3.11+、C11 编译器、Make、gettext、PyGObject、GTK 4、libadwaita 和桌面 Wayland 组件。执行 `make` 构建 relay 和翻译后运行：

```sh
PYTHONPATH=src /usr/bin/python3 -m panasonic_posture.linux.app.cli
```

Windows 使用 64 位 Python 3.11+。在本地控制台用普通用户启动界面，并连接已安装的硬件服务。

```bat
py -3 -m venv .venv-windows
.venv-windows\Scripts\python -m pip install -r packaging\windows\requirements-ui.txt
packaging\windows\run.cmd --language zh_CN
```

界面支持 `--language en|zh_CN`，Windows 另支持 `--background`。

## 构建与验证

### 测试

Linux 完整检查：

```sh
make WERROR=1 check
```

按改动选择目标测试、Windows 完整检查和跳过项记录方法，见 [AGENTS.md 第 6、10 节](AGENTS.md)。

### CI

每次 push、PR 和手动触发都会运行 Linux、Windows 工作流，不按修改路径过滤，因此文档、版本元数据和跨平台资源也会接受检查。

Linux 分为两类作业：Python 3.11/3.13 矩阵运行指定的兼容性测试，Make 步骤显式使用矩阵解释器；Ubuntu 24.04 系统 Python 作业安装发行版 PyGObject、GTK 和 libadwaita，执行完整 `make WERROR=1 PYTHON=/usr/bin/python3 check`，包括全量单测、私有 Broadway 界面测试和临时会话总线上的 D-Bus 集成检查。桌面测试前先检查 GI 和 Broadway 依赖，避免依赖缺失导致界面测试被跳过。

Windows 使用 Python 3.13 x64，运行 Windows、共享界面、会话刷新和翻译检查，构建中英文 MSI，执行管理解包及内置运行时的 UI 导入检查，并上传安装包。此流程不包含实际安装、服务运行或真机姿态验收。

发布工作流在自身源码提交上复用 Linux 工作流；Linux 检查和四路打包全部成功后才允许上传 Release 附件，随后更新 AUR。手动重建仍检查所选源码提交，不借用其他提交或此前运行的检查结果。自动化检查不代替真实桌面会话和真机验收。

### 打包

Windows 首次构建前先按下方“Windows 构建依赖”准备 Python 依赖和 WiX。

| 平台 | 命令 | 输出 |
| --- | --- | --- |
| Arch | `(cd packaging/linux/aur && makepkg --cleanbuild)` | 当前目录 |
| Debian / Ubuntu | `make package-deb` | `dist/packages/` |
| Fedora | `make package-rpm` | `dist/packages/` |
| Windows MSI（正式） | `python packaging\windows\build_msi.py --dependencies build\windows-dependencies --culture all` | `dist/windows/` 下两个带 culture 的文件名 |
| Windows MSI（安装测试） | 运行 `python packaging\windows\build_msi.py --dependencies build\windows-dependencies --test-build` | `dist/windows/` 下带 `build<N>` 的文件名 |

Windows 正式发布提供简体中文和英文 MSI。每次安装测试使用新的 `--test-build` 编号（也可指定，如 `--test-build 7`）。自动编号依据同版本已有构建，请保留输出目录。测试版高于相同基础版本的正式包，安装该正式包前需先卸载测试版。

MSI 完整交互界面在检测旧版本前显示 WiX 的非模态准备页，并在注册表搜索、安装空间计算期间保持可见，随后进入欢迎或维护页面。不要仅为消除快速机器上的短暂准备页而移除它，否则初始化较慢时会出现无窗口等待。此页面只覆盖 MSI 界面序列；Windows Installer 启动界面序列前的系统准备耗时不在其控制范围内。静默安装保持无界面。

静默安装指定 `INSTALLFOLDER` 时，请使用本地固定磁盘上的完整绝对路径，末级目录为 `PanasonicPostureManager`。服务固定安装到 `Program Files\PanasonicPostureManagerService`。

#### Windows 构建依赖

使用 Windows x64 CPython 3.11+ 完整安装；Windows CI 只使用与 MSI 内置运行时一致的 Python 3.13，执行完整测试、双语言 MSI 构建和解包验证；其他 Python 版本的源码兼容性按需检查。先确认 `python --version` 能输出版本；若 `Get-Command python` 指向 `WindowsApps` 且无法运行，请使用已安装解释器的绝对路径。建议建立仓库专用虚拟环境，避免修改宿主 Python：

```powershell
# python 应为可运行的 x64 CPython；也可替换为其绝对路径。
python -m venv .venv-windows
& .\.venv-windows\Scripts\python.exe -m pip install -r packaging\windows\requirements-ui.txt
& .\.venv-windows\Scripts\python.exe -m pip install --no-compile --target build\windows-dependencies -r packaging\windows\requirements-ui.txt
dotnet tool install --global wix --version 5.0.2
wix extension add -g WixToolset.UI.wixext/5.0.2
wix extension add -g WixToolset.Util.wixext/5.0.2
& .\.venv-windows\Scripts\python.exe packaging\windows\build_msi.py --dependencies build\windows-dependencies --check-environment
& .\.venv-windows\Scripts\python.exe packaging\windows\build_msi.py --dependencies build\windows-dependencies --culture all
```

切换解释器时重新创建依赖目录。`--check-environment` 校验运行时、UI 依赖及许可证，实际构建另行验证 WiX 扩展和 MSI。修改安装器时保留依赖许可证，含非 ASCII 文本的 PowerShell 脚本使用带 BOM 的 UTF-8。

Windows 品牌图片位于 `data/assets/windows/`。修改浅色标题栏标志后，运行 `packaging/windows/prepare-branding.ps1` 生成深色版本并核对两种主题。

#### Arch 包验证

在隔离环境中检查已构建的包：

```sh
make check-package PACKAGE=/path/to/package.pkg.tar.zst PACKAGE_NO_INSTALL=1
```

省略 `PACKAGE_NO_INSTALL=1` 会测试安装、重装和卸载，需要 root 或可用的 `unshare`。用 `PREVIOUS_PACKAGE=/path/to/old.pkg.tar.zst` 可测试升级。

## 发布

1. 同步版本号、中英文 README、AUR 配方和翻译文件头。运行 `python packaging/common/release_version.py --tag v3.0.1`，标签换成本次版本。
2. 在 `docs/releases/v<版本>.md` 编写本次发布说明，按「简体中文」「English」分节。对照上个发布版本，写清哪些用户受影响、使用体验有何变化，以及是否需要升级后操作。两种语言应覆盖相同事实，明确区分 Windows、Linux 和实验性支持。[3.0.0 示例](docs/releases/v3.0.0.md)
3. 审核双语正文，通过完整检查并取得发布授权后推送版本标签。工作流用该文件创建 Release，附上 Arch、Deb、RPM、两种语言的 Windows MSI 和 `SHA256SUMS`，随后更新两个 AUR 包。
4. 重建已有版本时，从所选源码提交手动运行发布工作流并指定原标签。同名附件会被替换，已有发布正文会保留。若重建影响用户，须同步更新线上中英文说明并注明实际构建提交。
