# 安装、打包与运维

机型、桌面要求和各姿态下的屏幕与键盘行为见[使用说明](README.md)。

## 安装

### 安装发行版软件包

从同一版本下载适合系统的安装包和 `SHA256SUMS`，放在同一目录。[GitHub Releases](https://github.com/adogecheems/panasonic-posture-manager/releases)

先在下载目录校验文件。输出中应有安装包的文件名，且结果为 `OK` 或「成功」。

```sh
sha256sum --ignore-missing --check SHA256SUMS
```

校验通过后，按发行版选择一条命令。以下示例使用 2.4.0，请按实际文件名替换。

| 发行版 | 安装命令 |
| --- | --- |
| Arch Linux | `sudo pacman -U ./panasonic-posture-manager-2.4.0-1-x86_64.pkg.tar.zst` |
| Ubuntu / Debian | `sudo apt install ./panasonic-posture-manager_2.4.0-1_amd64.deb` |
| Fedora | `sudo dnf install ./panasonic-posture-manager-2.4.0-1.x86_64.rpm` |

Ubuntu 和 Debian 包需要 GTK 4.12+ 和 libadwaita 1.4+，由 apt 安装依赖。

### AUR

两个包任选一个。使用 `yay` 时，从版本标签编译用 `yay -S panasonic-posture-manager`，直接安装发布好的 Arch 包用 `yay -S panasonic-posture-manager-bin`。

### 首次启动和升级

安装或升级后，注销并重新登录，桌面会自动启动新版用户服务。首次安装后想立即使用，也可以在当前 Wayland 会话中运行：

```sh
systemctl --user start panasonic-posture-manager-session.service
```

## 排查

**屏幕旋转或通知有问题**，先看当前用户的桌面服务。第一条命令看运行状态，第二条看本次开机以来的日志。

```sh
systemctl --user status panasonic-posture-manager-session.service
journalctl --user -b -u panasonic-posture-manager-session.service
```

Plasma 6 优先使用 `kscreenctl`，找不到时使用 `kscreen-doctor`。日志提示两者都缺失时，检查发行版的 KScreen 软件包。

**识别不到姿态，或键盘、触控板没有切换**，查看系统服务：

```sh
systemctl status panasonic-posture-manager.service
journalctl -b -u panasonic-posture-manager.service
```

检查机型兼容性可运行 `panasonic-posture-manager-probe`。它只读取 sysfs 和机型配置，以 JSON 列出匹配的机型、候选输入设备和加速度计。

## 卸载与清理

先关闭应用，在当前图形会话中停止用户服务，让它有机会恢复屏幕方向和方向锁。

```sh
systemctl --user stop panasonic-posture-manager-session.service
```

| 发行版 | 卸载命令 |
| --- | --- |
| Arch Linux | `sudo pacman -Rns panasonic-posture-manager` |
| Ubuntu / Debian | `sudo apt remove panasonic-posture-manager` |
| Fedora | `sudo dnf remove panasonic-posture-manager` |

若安装的是 AUR 预构建包，Arch 命令中的包名用 `panasonic-posture-manager-bin`。

卸载后会保留两处数据。要彻底清理，先确认桌面方向和方向锁已恢复，再按需删除。

- `/var/lib/panasonic-posture-manager/settings.json` 保存系统偏好和传感器校准。
- `${XDG_STATE_HOME:-$HOME/.local/state}/panasonic-posture-manager/` 保存当前用户的显示恢复记录。

## Windows 界面预览

在 Windows 的命令提示符中进入仓库根目录，使用 64 位 Python 3.11 或更高版本运行：

```bat
py -3 -m venv .venv-windows
.venv-windows\Scripts\python -m pip install -r packaging\windows\requirements-ui.txt
packaging\windows\preview.cmd --language zh_CN
```

`preview.cmd` 加上 `--demo`，用于原有的内存演示。关闭窗口后设置清空。实际设备控制尚未接入，尚未在 Windows 真机上验收。

- **语言**　`--language en` 切换为英文，省略该参数则跟随环境语言。
- **场景**　例如 `packaging\windows\preview.cmd --scenario prompt` 打开确认提示。还可选择 `laptop`（笔记本）、`hardware`（物理平板）、`slow`（切换缓慢）、`error`（故障）或 `disconnected`（断线）。
- **资源**　默认从 `data/assets/` 加载，可用 `PANASONIC_POSTURE_DATA_DIR` 指定数据目录。源码预览优先使用 `locale/` 中已编译的翻译，也可直接读取 `po/zh_CN.po`。

要连接独立软件后台，运行以下命令；在 Windows 直接启动模块也会使用软件后台。

```bat
packaging\windows\run.cmd --language zh_CN
```

普通启动允许保存自动模式偏好，设备控制和校准保持禁用。后台是当前用户的子进程，不注册 Windows 系统服务、不要求管理员权限；未来的特权硬件控制须另行设计，不能直接加入此用户进程。

需要验证完整软件事务时显式打开后台模拟模式：

```bat
packaging\windows\run.cmd --simulate-backend --language zh_CN
packaging\windows\run.cmd --simulate-backend --scenario prompt
```

后台模拟通过共用姿态策略、服务引擎、会话运行时和显示事务执行，输入和显示端口仅修改模拟状态。`slow` 暂缓显示回执，`error` 注入显示失败，`hardware` 模拟物理开关优先；`disconnected` 仅供原有 `--demo` 使用。模拟结果不代表真实设备已应用。

普通设置存于 `%LOCALAPPDATA%\PanasonicPostureManager\settings.json`，模拟设置为同目录下的 `simulation.json`；`--settings` 可指定独立测试文件。只保存偏好与校准矩阵，不保存手动模式、已应用结果或旧会话。沿用设置 v1、原子替换和 `.corrupt-<timestamp>` 损坏隔离。每个设置文件只允许一个后台实例。

界面通过子进程继承的私有标准管道通信，无网络监听或共享管道名。消息限制为 64 KiB，最多 32 个未完成请求，默认五秒超时。状态与完成回调在界面线程处理。断线立即使旧请求失效，重连采用有上限的退避，不重放写入；关闭窗口会回收后台。此通信能力只授予父进程，不替代未来系统服务的会话鉴权。管道继承与句柄关闭依据 [Python 子进程文档](https://docs.python.org/3.11/library/subprocess.html)。

在非 Windows 开发环境中，必须显式指定测试设置路径；默认不启动软件后台：

```sh
PYTHONPATH=src .venv-windows/bin/python -m panasonic_posture.fluent --simulate-backend --settings /tmp/posture-preview/simulation.json --language zh_CN
```

### Windows 加速度计只读检查

在 Windows 源码目录中，用已准备好的 Python 环境运行：

```bat
set PYTHONPATH=src
.venv-windows\Scripts\python -m panasonic_posture.windows.probe --samples 5 --interval-ms 200
```

探针通过系统自带的 Windows PowerShell 读取 WinRT 默认加速度计，输出设备标识、原始 g 值、按标准重力 9.80665 换算的 m/s² 值，以及原始采样时间戳。它不修改采样间隔、不读取触摸坐标、不抓取输入、不旋转屏幕，也不改变后台支持状态。接口和单位参见 [Microsoft 加速度计说明](https://learn.microsoft.com/en-us/windows/apps/develop/devices-sensors/sensors)。

`--interval-ms` 是诊断读取之间的等待时间，不保证设备以该频率产生新样本。报告中的 `distinct_timestamps` 可用于发现重复缓存；这些墙钟时间戳尚未转换到服务引擎的时钟，不能直接作为姿态策略样本。诊断不会验证传感器安装方向，也不代表已应用某种模式。无传感器、权限不足、输出损坏或读取超时会以非零状态退出，不用模拟数据补齐。

探针最多读取 100 份样本，间隔限制为 10～1000 毫秒；只可在明确授权的 Windows 测试环境中运行。便携包中可用 `runtime\python.exe -m panasonic_posture.windows.probe --samples 5` 执行同一检查。

持续采样检查使用同一系统 WinRT API，无须添加 Python 依赖：

```bat
.venv-windows\Scripts\python -m panasonic_posture.windows.sensor --seconds 10
```

此命令输出逐行 JSON，时间换算至 Python 单调时钟，单位为 m/s²，`mount_verified=false` 表示仍沿用传感器原始轴向，不能当作机型姿态校验。它逐次枚举 Standard 加速度计，只接受唯一且与默认传感器标识一致的设备，不设置 ReportInterval 或 ReadingTransform。WinRT 在刚打开时可能先返回很久以前的缓存；旧帧不会发布，启动最多等待 20 秒，新鲜样本间隔不能超过两秒。重复时间戳不更新样本时间。断线后间隔两秒重试，保持原设备身份和最后的原始时间戳；更换设备必须重新建立并验证安装方向。

`windows.sensor.WindowsSensor` 实现共用 SensorBackend 的只读接口，构造时要求显式传入安装方向矩阵。它校验采样与接收时间、墙钟和单调时钟的相对变化，遇到时钟跳变重新连接并建立时间基准；错误或过期时立即返回无效读数。它尚未接入普通软件后台，也不改变设备支持等级。传感器接口与固定坐标系见 [Microsoft Accelerometer 文档](https://learn.microsoft.com/en-us/uwp/api/windows.devices.sensors.accelerometer)。

2026-11-08，同一 Windows 10 虚拟机的普通用户下，持续采样约 3.4 秒取得 23 个不同时间戳的样本，退出时回收读取子进程。测试覆盖启动旧缓存、缓存不续期、设备更换、断线重试、时钟跳变与退出清理；不代表传感器安装方向或整机姿态已验收。

### 独立 Windows 硬件主机开发边界

`windows/hardware_host.py`、`hardware_ipc.py` 和 `security.py` 是独立硬件主机的请求分发、通信与身份校验组件，沿用共用 Engine；当前没有注册系统服务、安装输入驱动，也不由普通预览自动启用。它们不提供任意文件路径、设备开关或执行命令接口。真正输入执行端口、可信物理开关和显示会话适配器未装配前，不得发布硬件支持或已应用模式。

`backend/windows/driver.c` 是尚未完成 WDK 编译和硬件验证的 KMDF 输入过滤驱动草稿，不进入便携包、默认构建或安装流程。`control.c` 及其独立 C 测试描述租约和空闲接管边界；这些测试不能替代驱动编译、设备匹配和失效释放的真机验证。

硬件通道独立于软件预览的私有标准管道，使用 `windows-hardware` JSON v1 信封和本地主机命名管道。服务端要求 LocalSystem，端点所有者也是 LocalSystem，拒绝远程客户端；交互用户只获读写权限，明确排除与 FILE_APPEND_DATA 共用数值的 FILE_CREATE_PIPE_INSTANCE。首实例独占端点名称，最多四个连接，每条消息至多 64 KiB，连接请求五秒超时。客户端核对端点所有者，不自动重放请求；服务实例和每连接递增请求序号用于拒绝旧请求。

身份来自管道模拟令牌与实际客户端进程令牌，必须匹配 SID、登录 LUID 和会话 ID，并核对 PID、进程创建时间、当前本地控制台以及未锁定的活跃会话。界面可以提交模式和偏好选择；只有由可信主机预先登记的确切会话进程才能提交显示回执，不能通过 RPC 自行注册 PID。身份检查放在独立有界后台队列，主循环继续推进引擎；一秒内未获得新的会话确认即失去资格，慢查询结果不能延长有效期。失联、退出、锁屏或用户切换通过共用 `Engine.revoke_session` 撤销对应所有者并走原有释放事务。它不改变 Linux 鉴权、租约时长或 D-Bus 方法。

接口依据：[Microsoft 命名管道权限](https://learn.microsoft.com/en-us/windows/win32/ipc/named-pipe-security-and-access-rights)、[WTS 会话状态](https://learn.microsoft.com/en-us/windows/win32/api/wtsapi32/ns-wtsapi32-wtsinfoex_level1_w)。

隔离通信检查为 `tests/windows_hardware_smoke.py`：仅在已获授权的 Windows 测试虚拟机中分别以 LocalSystem 和普通交互用户运行 server/client，显式传入同一临时 `--directory`。该 fixture 使用不支持硬件的共用引擎和空输入端口，不打开输入设备。2026-11-08 的原生检查已通过端点所有者、控制台令牌、拒绝不支持模式、拒绝未登记回执、拒绝客户端创建服务端实例及拒绝用户自建冒充端点六项检查，普通客户端未提权。模拟状态和此通信证据均不代表输入驱动验收。

### Windows 便携预览包

在 Windows 的独立构建环境中，使用与预览依赖兼容的 64 位 Python 3.11+：

```bat
python -m pip install --target build\windows-dependencies -r packaging\windows\requirements-ui.txt
python packaging\windows\build.py --dependencies build\windows-dependencies
```

构建器从当前 Python 基础安装和独立依赖目录读取运行时、Qt/Fluent 及随包许可，连同源码、插画、翻译和维护说明直接流式写入 ZIP，同时计算逐文件校验，减少临时磁盘写入与内存占用。完整归档生成后才替换已有包。`runtime/python._pth` 隔离用户站点、注册表和 `PYTHONPATH`；参见 [Python Windows 搜索路径说明](https://docs.python.org/3.11/using/windows.html#finding-modules)。基础安装的第三方包、测试数据或配置不进入归档。

输出为 `dist/windows/panasonic-posture-windows-preview.zip` 和 SHA-256 校验文件。解压后运行 `Panasonic Posture Manager.cmd`，或用 `Simulation.cmd` 启动后台模拟。ZIP 内 `MANIFEST.json` 记录逐文件校验值，第三方许可保留在 `runtime/Lib/site-packages/*.dist-info/`，Python 许可为 `runtime/LICENSE.txt`。更新时解压到新目录，用户设置继续位于 LocalAppData。

`.github/workflows/windows-preview.yml` 在 Windows 上运行 Python 3.11/3.13/3.14 的后台、界面、只读探针解析和翻译测试，构建 ZIP，再用解压包自带的 Python 验证 Qt、中文资源、模式事务和后台退出。流程仅保存构建制品，不发布 Release、不改版本或 AUR。提交这份流程不代表远端构建已执行。

Windows 离屏验证使用已安装的系统字体：测试辅助函数将 `QT_QPA_FONTDIR` 指向 `%SystemRoot%\Fonts`，避免通用离屏插件找不到字体，导致文字、导航宽度与原生窗口不同。界面测试会检查字体确实已加载，保留布局与操作断言。源码检查明确按 UTF-8 读取文件，不依赖 Windows 系统区域编码。

2026-11-08，在 Windows 10 Enterprise LTSC 2021（19044）x64 虚拟机、Python 3.14.10、普通用户非提权令牌下，后台 20 项、探针解析 5 项、共用前端 11 项、Fluent 25 项、打包组装 2 项测试通过。加速度计只读探针取得 5 份时间戳不同的真实读数，静止合模长约 9.68～9.76 m/s²。这份证据只覆盖该环境的软件和传感器读取；虚拟机中的输入直通不可靠，未验证键盘/触控板接管、物理开关、原生屏幕旋转或真实触控手感。

同一虚拟机中还完成了便携包构建和解压验证：包内 `python.exe` 与 `pythonw.exe` 均能加载 Qt 和中文资源、完成模拟模式事务并回收后台；原生 Windows 平台渲染的主页和设置页也已检查。归档约 109 MiB，5490 个清单文件通过校验，77 个 Python 源文件与工作树一致。

## 构建和验收

在仓库根目录用普通用户运行检查。D-Bus 集成测试会启动传感器探测，请在没有映射真实传感器的隔离环境中运行。准备 Python 3.11+、C 编译器、Make、gettext、PyGObject 和 `dbus-run-session`。GTK 界面测试还需要 PyCairo、GTK 4、libadwaita 和 `gtk4-broadwayd`。

```sh
make WERROR=1 check
```

在已安装预览依赖的 Linux 环境中，可以离屏测试 Qt 界面。缺少依赖时，控件测试会跳过，纯 Python 演示测试仍会运行。

```sh
PYTHONPATH=src QT_QPA_PLATFORM=offscreen python -m unittest discover -s tests -p test_fluent.py -v
```

### 发行版打包

| 目标 | 在仓库根目录运行 | 源码或产物 |
| --- | --- | --- |
| Arch | `(cd packaging/aur && makepkg --cleanbuild)` | 从 `pkgver` 对应的发布标签构建 |
| Ubuntu / Debian | `make package-deb` | 输出到 `dist/packages/` |
| Fedora | `make package-rpm` | 从干净工作树的 `HEAD` 构建，输出到 `dist/packages/` |

各发行版需要的工具见[发布工作流](.github/workflows/release.yml)。在 Arch 构建环境中，可先检查源码配方生成的包。将示例路径替换为实际文件名：

```sh
make check-package PACKAGE=/path/to/package.pkg.tar.zst PACKAGE_NO_INSTALL=1
```

去掉 `PACKAGE_NO_INSTALL=1` 可在隔离的 pacman 根目录中测试安装、重装和卸载，需要 root 或可用的 `unshare`。用 `PREVIOUS_PACKAGE=/path/to/old.pkg.tar.zst` 指定旧包可同时验证升级。发布前还需在目标设备上检查自动和手动切换、内置输入恢复、挂起唤醒和外接屏布局。

## 发布

1. 对齐项目、运行时、UI、AUR、翻译文件头和 README 的版本号。用待发布标签运行检查，例如 `/usr/bin/python3 packaging/scripts/release_version.py --tag v2.4.0`。
2. 推送版本标签。工作流从标签对应的提交构建 Arch、Deb 和 RPM 包，生成 `SHA256SUMS`，上传至 GitHub Release，再更新两个 AUR 包。
3. 重建已有版本时，手动运行工作流，选择要构建的提交，并在 `tag` 填写要更新的发布标签。同名附件会被替换。AUR 源码包仍从原标签构建，二进制包使用新上传的 Arch 包。

Windows 便携包必须包含 Python、PyQt6、Qt、Fluent Widgets 及其传递依赖的许可。构建时拒绝缺少许可元数据的依赖目录。

## 平台适配

Python 源码位于 `src/panasonic_posture/`。扩展平台时，复用共用层的姿态判断和恢复流程，在平台入口接入硬件、桌面和权限检查。各模块的职责和对应测试见[维护手册](AGENTS.md#4-定位代码与测试)。

| 要接入的部分 | 入口 | 实现要求 |
| --- | --- | --- |
| 传感器与输入 | `service/ports.py` | 将三轴加速度换算到设备坐标系，单位为 m/s²，时间戳与引擎采样时钟一致。宿主完成鉴权后传入 `SessionOwner`，执行器用 `satisfies(target)` 报告输入是否已切换。 |
| 桌面会话 | `session/ports.py` | 读取和设置屏幕方向、报告就绪状态，显式提供 `DisplayJournal`。持久化由平台实现，恢复前核对输出和设置的归属。 |
| 界面 | `frontend/ports.py` | 通过 `UIClient` 发起操作，从已校验的服务快照读取结果。在界面主循环派发状态和完成回调，断线后丢弃旧结果。 |

Linux 由 `service/application.py` 和 `session/agent.py` 组装这些组件。下面几处需要配套维护：

- **硬件与状态**　`hardware/profile.py` 负责 Linux 设备匹配，`SensorInfo` 描述传感器，`DeviceInfo` 记录当前平台的机型和支持等级。新平台需单独验证。状态快照为 v2，设置和显示日志均为 v1。
- **输入协议**　系统服务和 C relay 必须来自同一构建。relay v4 用 `1` 接管输入，`0` 转发物理开关，两者均续接三秒租约。服务按 32 位 `request` 序号匹配回执。从物理平板展开时，桌面的平板信号最多再保留两秒。
- **登录与亮屏**　会话从 logind 识别登录界面，从 Mutter 的 `PowerSaveMode` 或显示器的 DRM `dpms` 读取熄屏状态。此时通过 `SessionUpdateUnattended` 报告准备状态，旧服务回退到普通心跳。亮屏后，以下一次稳定姿态为准，再恢复日常提示。
- **触摸亮屏**　软件接管期间，relay 只观察唯一直接触摸屏的接触开始。会话用 `SessionWake` 提交当前代次和两秒内有效的 `touch_wake` 令牌。服务核对身份、租约和已应用模式后，relay 再检查触摸是否有效，每次接触最多发一次 `KEY_WAKEUP`。唤醒不会续租。


已移除从系统挂起状态触摸唤醒的实验选项。旧设置文件中的 `suspend_touch_wake` 字段会被忽略，
下次保存时清除；状态快照不再发布该选项，`SetSuspendTouchWake` 方法已移除。
