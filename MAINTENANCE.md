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
| Arch Linux | `sudo pacman -U ./panasonic-posture-manager-2.5.0-1-x86_64.pkg.tar.zst` |
| Ubuntu / Debian | `sudo apt install ./panasonic-posture-manager_2.5.0-1_amd64.deb` |
| Fedora | `sudo dnf install ./panasonic-posture-manager-2.5.0-1.x86_64.rpm` |
| Windows（简体中文） | 双击 `panasonic-posture-manager-2.5.0-zh-CN-windows-x64.msi`，完成后重启 |
| Windows（英文） | 双击 `panasonic-posture-manager-2.5.0-en-US-windows-x64.msi`，完成后重启 |

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

## Windows 图片资源

Windows 图片统一放在 `data/assets/windows/`：`icons/` 保存应用图标、ICO 和托盘使用的白底方形图；`branding/` 保存标题栏 Let's note 浅色与深色标志；`postures/` 保存姿态 SVG；`installer/` 保存 WiX 安装界面图片。

安装器只定制横幅和侧图；信息与警告提示使用 WiX 内置图标，不使用应用标志覆盖。

构建只将前三类按相同子目录复制到安装目录的 `data/assets/`，安装器图片由 WiX 直接引用，不进入应用负载。源码运行使用平台资源目录，安装运行使用负载资源目录，均通过 `windows/app/resources.py` 定位。

深色标题栏标志由浅色原图精确转换：保留尺寸、透明通道和蓝色笔画，将中性色笔画转为白色；修改浅色原图后在 Windows 运行 `powershell -NoProfile -File packaging/windows/prepare-branding.ps1` 重新生成，再核对两种主题的显示效果。

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

服务未安装或未运行时，界面只显示断线。重新运行 MSI 修复安装并重启；不要从源码或便携目录手工注册服务。
硬件服务使用普通自动启动，不使用 Windows 的延迟自动启动，以便登录后的桌面会话能尽早发现 RPC 端点。
MSI 在安装、修复和卸载时通过 `ServiceControl` 等待服务停止，确保替换运行时文件前释放服务持有的句柄。安装和修复注册服务后，由配置动作重新请求启动并最多等待 30 秒，启动失败会使安装失败；服务删除仅在卸载时执行。

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
| Windows | 在「已安装的应用」中卸载，完成后重启 |

Linux 卸载后会保留以下状态，确认屏幕方向已恢复后可手动删除：

- `/var/lib/panasonic-posture-manager/settings.json`
- `${XDG_STATE_HOME:-$HOME/.local/state}/panasonic-posture-manager/`

Windows MSI 会停止并移除服务，同时恢复安装前的平板指示器配置。用户日志和恢复记录可在确认应用已正常退出后，从 `%LOCALAPPDATA%\PanasonicPostureManager` 清理。

应用和安装器支持英文与简体中文；繁体中文及其他语言当前回退到英文。Linux desktop entry、polkit description/message 提供简体中文；systemd 不支持本地化 unit `Description`，因此系统服务描述保持英文。polkit 的 vendor 是不支持翻译的固定项目名。

## Windows 运维

正式安装支持 Windows 10/11 x64。Panasonic CFQV8、CFQV9、CFQV1 全系使用明确的已验证产品族配置；其他机型回退到与 Linux 相同的 `generic/compatible` 路径，由 WinRT 传感器、ETW 物理源、唯一 `ACPI\INT33D3` 输出和显示会话实际探测能力。MSI 包含受保护运行时、桌面界面、LocalSystem 硬件服务和可逆的 `ACPI\INT33D3` 非独占配置。

### 硬件识别与服务连接

- 物理姿态变化只接受 Intel HID Event Filter Event 115：`0xCC` 为平板，`0xCD` 为笔记本。
- 明确 QV8/QV9/QV1 配置在启动时只读取一次平板指示器和恢复记录，建立初始状态；无法明确时最多等待五秒，然后使用非平板安全基线。其他机型必须通过能力探测收到一次合法 ETW Event 115 后才开放物理准入。
- `ACPI\INT33D3` 按硬件 ID 唯一发现，不绑定某个固件实例后缀；它只用于发布软件帐篷/支架姿态，不用于反推运行期物理状态，也不会发布 shell 平板模式或自动打开屏幕键盘。
- 界面通过经过身份校验的本地命名管道连接硬件服务；服务不可用时显示断线，不回退到模拟数据。
- 界面和会话进程以普通用户运行；显示操作与硬件写入都必须匹配服务登记、会话资格、姿态代次和租约。
- 会话入口在创建用户日志、实例锁或恢复后端之前检查普通用户和本地控制台身份。只有会话观察快照过旧、且其余条件正常时才使用三秒宽限期；锁屏、挂起、控制台切换和观察器故障立即撤销。
- 服务停止时先撤销会话登记并等待清理；超过停止期限后由同一启动工作线程处理迟到结果，主循环继续关闭 RPC 和硬件资源。安装目录的保护句柄一直保留到子进程和启动器清理成功；原生调用持续阻塞时不会提前释放这些句柄，清理不完整会报告失败。

### 登录、界面与安装资源

- 硬件服务在已发布状态签名变化时置位一个仅信号用途的全局命名事件，界面观察器醒来后立即通过鉴权 RPC 刷新快照；事件不可用或丢失时界面按 0.1 秒空闲刷新兜底。窗口只在首次进入设置页时构建设置页面，首开窗口只加载主页。
- 登录启动先创建仅依赖 PyQt、硬件客户端和托盘资源的启动壳；完整 Fluent 窗口首次打开时才加载。窗口尚未创建时，托盘通知仍按当前实例、提示代次和可确认条件直接确认。
- 升级时只把能够确认属于本应用的已知旧 Run 命令迁移到当前固定命令并回读；初始化标记仍保留。用户已关闭的空值不得自动重开，未知内容不得覆盖或删除。
- MSI 同时携带应用源码与纯 Python UI 依赖的确定性 `unchecked-hash` 字节码；`pythonw` 入口和开始菜单快捷方式使用 `-B -I`，安装目录不需要在运行期写入 `__pycache__`。字节码必须由与受保护运行时相同版本的 Python 生成。
- MSI 开始菜单快捷方式与界面进程使用同一个固定 AUMID；快捷方式重建后必须回读校验，避免通知来源回退为 `Python`。
- 开始菜单快捷方式直接位于 `Programs` 根级，不创建同名应用文件夹；升级时只迁移已知的旧应用文件夹内链接，并仅在旧文件夹为空时清理该文件夹。

### 显示恢复与姿态交接

- 所有显示写入使用恢复日志。锁屏、用户切换、硬件源失效或写入失败时，程序撤销确认并恢复原方向。
- 恢复日志读取、损坏隔离或删除失败时保留失败状态，不把旧所有权当成不存在。服务启动基线只读检查 CSM 恢复记录，损坏记录由合格用户会话隔离。正式会话同时接入内屏事务刷新和仅外屏的只读拓扑校验；已有内屏恢复记录会阻止忽略该记录而选择仅外屏路径。
- 平板退出前不再预锁方向：Windows 在释放 convertible-slate 指示器时会覆盖旋转锁并转向桌面原生方向，预锁只会带来额外的回滚转动。退出后只读等待最多两秒并持续补锁，等待不因硬件状态短暂不可读而提前结束，目标稳定后一次写入方向。软件帐篷和支架先发布 CSM 并等待 Windows 响应落地，再一次写入方向和锁；笔记本仍先写方向再发布 CSM。显示回滚或平台改写期间保留已成功发布的 CSM 租约，只有资格丢失、CSM 通知失败或真正释放才恢复启动值。物理接管时不得把旧软件目标或启动值重新写入 CSM。
- 会话启动时授权证明、显示恢复、后续显示写入和最终恢复/CSM 释放共用显示工作线程；lease worker 使用独立 RPC 通道，主线程不构造或恢复显示，也不会占用第三个有界通道。重复释放 CSM 保持幂等，不再重复查询硬件或重写启动值。
- 锁屏、睡眠或休眠会结束旧姿态事务。服务在恢复会话请求前检查主循环时钟跳变；资格或物理源恢复后，自动模式必须等新的稳定姿态，不能先发布临时笔记本目标再跳向帐篷。
- 服务直接订阅挂起和恢复通知：挂起及首次恢复通知立即作废物理开关缓存，owner loop 清除旧事务；恢复必须等待新的合法 ETW Event 115，不能重读启动基线或超时伪造状态。无新事件时保持不可用并保留恢复记录，重新产生物理翻转事件后可恢复。丢失通知时由主循环时钟跳变作废缓存兜底；正常通知路径保留恢复通知后、首次恢复轮询前已收到的新物理事件。
- 自动恢复和用户恢复通知属于同一轮唤醒：会话与服务只处理一次恢复，直到下一次挂起重新开放处理，避免已恢复的显示再次撤销。

Windows 产品保持内置键盘和触控板可用，不加载自研输入过滤驱动，不要求测试签名。源码目录不能作为特权运行时；正式服务只由 MSI 安装。

Windows 服务将检测偏好、提示偏好和传感器校准原子写入 `%ProgramData%\PanasonicPostureManager\State\settings.json`。MSI 在启动服务前为 State 目录设置仅 SYSTEM 和管理员可写的 ACL；损坏设置按共享设置存储规则隔离。

Windows RPC v1 增加无参数的 `start_control` 和 `stop_control`，仍要求当前本地控制台身份鉴权，可信会话进程不能调用这两个 UI 操作。`stop_control` 的 `accepted=False` 表示正常等待恢复，客户端须继续查询，不能提前宣告退出成功。监管器向会话进程发送既有停止信号，保留恢复期间的 RPC 资格，进程成功退出并完成资源清理后才返回 True；超时强制结束或非零退出码均报告失败。停止只约束发起请求的登录实例，重新打开界面会发送一次 `start_control`；重连不重放启动或停止请求。

MSI 只有在安装前没有产品配置标记时才注册 CSM 配置移除回滚。升级和修复沿用旧安装的恢复记录，失败回滚不得撤销该配置。

## 开发运行

Linux 依赖 Python 3.11+、C11 编译器、Make、gettext、PyGObject、GTK 4、libadwaita 和桌面 Wayland 组件。执行 `make` 构建 relay 和翻译后运行：

```sh
PYTHONPATH=src /usr/bin/python3 -m panasonic_posture.linux.app.cli
```

Windows 源码运行使用 64 位 Python 3.11+。Windows UI 固定使用 PyQt6 6.8.1 和 Qt 6.8.2；如需升级 Qt，必须先重新核对 Windows 支持范围和完成 UI 回归测试：
`PyQt6-Frameless-Window` 在 Windows 上还依赖 `pywin32`；MSI 构建会验证 `win32api`、`win32gui`、`win32comext` 和配套 DLL，CI 解包后会实际导入 UI 组件。

```bat
py -3 -m venv .venv-windows
.venv-windows\Scripts\python -m pip install -r packaging\windows\requirements-ui.txt
packaging\windows\run.cmd --language zh_CN
```

支持的参数只有 `--language en|zh_CN` 和 Windows 专用的 `--background`。源码界面同样只连接已安装的硬件服务。

## 构建与验证

### 测试

Linux 完整检查：

```sh
make WERROR=1 check
```

常用分类：

```sh
make check-unit
make check-c
make check-dbus
make check-i18n
```

只检查两平台的静态导入边界时，运行
`python -m unittest discover -s tests -p "test_platform_imports.py" -v`
（同样需要 `PYTHONPATH=src`）。该检查每轮只解析一次源码，并覆盖绝对导入、
相对导入和 `from 包 import 子模块`，不加载平台适配器或访问硬件。

Windows CI 与本地检查：

共享界面交互逻辑在 `test_frontend.py` 验证；Windows 资源与语言选择在
`test_windows_resources.py` 验证，两者都不需要 Qt。`test_windows_ui.py`
保留入口、断线客户端和隔离控件测试；参数矩阵直接调用入口，帮助命令保留独立进程检查。

```powershell
$env:PYTHONPATH = "src"
python -m compileall -q src/panasonic_posture/windows
python -m unittest discover -s tests -p "test_windows*.py" -v
python -m unittest discover -s tests -p "test_session_refresh.py" -v
python -m unittest discover -s tests -p "test_frontend.py" -v
python -m unittest tests.test_localization tests.test_i18n_check -v
python tests/check_i18n.py
```

### 打包

Windows 首次构建前先按下方“Windows 构建依赖”准备 Python 依赖和 WiX。

| 平台 | 命令 | 输出 |
| --- | --- | --- |
| Arch | `(cd packaging/linux/aur && makepkg --cleanbuild)` | 当前目录 |
| Debian / Ubuntu | `make package-deb` | `dist/packages/` |
| Fedora | `make package-rpm` | `dist/packages/` |
| Windows MSI（正式） | 分别使用 `--culture zh-CN` 和 `--culture en-US` 运行 `python packaging\windows\build_msi.py --dependencies build\windows-dependencies` | `dist/windows/` 下两个带 culture 的文件名 |
| Windows MSI（安装测试） | 运行 `python packaging\windows\build_msi.py --dependencies build\windows-dependencies --test-build` | `dist/windows/` 下带 `build<N>` 的文件名 |

Windows 的正式构建、发布和兼容契约只覆盖两个本地化 MSI。`panasonic-posture-windows-portable.zip` 不属于发布产物；开发时若通过 `packaging/windows/build.py` 生成，只用于诊断，其文件名、目录布局、校验文件和兼容性均不受支持。

每次安装测试必须使用新的 build 编号。`--test-build` 会根据 `dist/windows/` 中同版本产物自动选择下一个编号，也可用 `--test-build 7` 显式指定。用户可见版本为 `2.5.0-build7`，MSI 内部四段版本为 `2.5.0.7`；ProductCode 由正式版本和 build 编号共同确定，同一 build 的中英文安装包共享 ProductCode，不同 build 使用不同 ProductCode，UpgradeCode 始终不变。这样后续测试构建会执行 major upgrade 并替换缺少文件版本资源的新负载。安装包的 `BUILD-VERSION` 和关于页显示该测试版本，正式发布构建不带此后缀。

安装测试的 MSI 版本高于同一基础版本的正式包。在测试机上安装同基础版本的正式包前，应先卸载测试构建，或先提升正式版本号。

Windows MSI 默认构建简体中文界面；正式发布同时生成简体中文和英文安装包。两种文化使用相同的版本 ProductCode 和 UpgradeCode，文件名分别包含 `zh-CN` 或 `en-US`，不能相互覆盖。安装与卸载共用同一套方形应用标志、欢迎/完成页背景、微软雅黑字体、最终确认页和重启提示，界面资源位于 `packaging/windows/msi/`；卸载流程提供明确的维护说明、卸载确认、带品牌背景的执行页和卸载专用完成提示。主程序可安装到用户选择的本地固定磁盘目录，输入上级目录时自动补全 `PanasonicPostureManager` 叶目录；安装器拒绝磁盘根、Windows/系统目录、固定服务目录、重解析点、过长路径和已有陌生文件的目录。静默安装必须在 `INSTALLFOLDER` 中给出包含该叶目录的完整绝对路径。硬件运行时和服务固定安装到 `Program Files\PanasonicPostureManagerService`，不随主程序路径变化。

含非 ASCII 文本的 PowerShell 脚本必须保存为带 BOM 的 UTF-8；Windows PowerShell 5.1 会按系统 ANSI 代码页解释无 BOM 的 UTF-8，可能破坏安装自定义动作的字符串边界。打包测试会检查 `packaging/windows/` 下的脚本。

#### Windows 构建依赖

```bat
python -m pip install --target build\windows-dependencies -r packaging\windows\requirements-ui.txt
dotnet tool install --global wix --version 5.0.2
wix extension add -g WixToolset.UI.wixext/5.0.2
wix extension add -g WixToolset.Util.wixext/5.0.2
```

#### Arch 包验证

在隔离环境中检查已构建的包：

```sh
make check-package PACKAGE=/path/to/package.pkg.tar.zst PACKAGE_NO_INSTALL=1
```

省略 `PACKAGE_NO_INSTALL=1` 会测试安装、重装和卸载，需要 root 或可用的 `unshare`。用 `PREVIOUS_PACKAGE=/path/to/old.pkg.tar.zst` 可测试升级。

## 发布

1. 同步 `pyproject.toml`、`__version__`、README、AUR、翻译文件头和其他发布元数据。
2. 运行版本一致性检查：

```sh
python packaging/common/release_version.py --tag v2.5.0
```

3. 通过完整测试后推送版本标签。发布工作流会构建 Arch、Deb、RPM 和简体中文/英文 Windows MSI，生成 `SHA256SUMS`，上传 GitHub Release，并更新两个 AUR 包。
4. 重建已有版本时手动运行发布工作流，指定源码提交和标签；同名附件会被替换。

包内必须保留 Python、PyQt6、Qt、Fluent Widgets 及其传递依赖的许可。

## 扩展平台

先阅读 [AGENTS.md](AGENTS.md) 的模块地图和系统不变量。新平台至少需要实现：

| 部分 | 接口 | 要求 |
| --- | --- | --- |
| 传感器与输入 | `service/ports.py` | 统一到设备坐标和时间基准；不得伪造硬件状态。 |
| 桌面会话 | `session/ports.py` | 提供方向、方向锁、显示恢复日志和就绪回执。 |
| 界面通信 | `frontend/ports.py` | 校验服务快照；断线后丢弃旧结果。 |

共用层负责姿态策略、目标/结果分离、租约、恢复事务和状态发布。平台层只负责硬件、桌面和权限适配，不能用平台特例绕过共用事务。

现行协议版本为：状态快照 v2、C relay v4、硬件 profile v1、设置 v1、显示恢复日志 v1。修改兼容契约前须取得明确授权，并同步所有调用方、打包配置和契约测试。
