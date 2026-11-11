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

`preview.cmd` 加上 `--demo`，用于原有的内存演示。关闭窗口后设置清空，不操作真实设备，不能作为真机验证。

- **语言**　`--language en` 切换为英文，省略该参数则跟随环境语言。
- **场景**　例如 `packaging\windows\preview.cmd --scenario prompt` 打开确认提示。还可选择 `laptop`（笔记本）、`hardware`（物理平板）、`slow`（切换缓慢）、`error`（故障）或 `disconnected`（断线）。
- **资源**　默认从 `data/assets/` 加载，可用 `PANASONIC_POSTURE_DATA_DIR` 指定数据目录。源码预览优先使用 `locale/` 中已编译的翻译，也可直接读取 `po/zh_CN.po`。

Windows 默认连接独立、以 LocalSystem 运行的硬件服务；直接启动界面无需额外参数。

```bat
packaging\windows\run.cmd --language zh_CN
```

若服务未安装或不可用，界面只显示断线，不回退到软件后台、模拟或 CSM 推断。隔离的软件后台预览使用 `--backend`，它不读取真实设备，也不能声明硬件支持。

便携包或受保护安装目录中的服务入口为：

```bat
Install Hardware Service.cmd
Hardware Service Status.cmd
Remove Hardware Service.cmd
```

`Install Hardware Service.cmd` 会请求一次 UAC。它先验证当前包，若从普通用户目录启动，则把完整测试版本复制到 `%ProgramFiles%\PanasonicPostureManager`，更新时保留前一受保护目录；随后用受限 `runtime\python-hardware.exe` 验证整棵目录的 ACL、所有者、重解析点和搜索路径，再注册延迟自动启动的 `PanasonicPostureManagerHardware` 服务、配置失败重启并立即启动。服务固定以 LocalSystem 运行，不安装驱动、不修改 CSM 设备配置。状态和移除入口也请求管理员权限；移除服务不会删除受保护程序目录、用户设置或诊断日志。

CFQV8-1L 的硬件服务配置入口为：

```bat
packaging\windows\configure-slate-device.cmd -Action Install
packaging\windows\run.cmd --language zh_CN
```

第一条命令需要管理员权限，只把已核对的 `ACPI\INT33D3` 驱动配置设为 `LaptopSlateExclusiveAccess=0`，并在首次设置时记录安装前值；必须重启一次才会生效。`-Action Remove` 恢复安装前配置，`-Action Status` 只报告当前状态。便携包的同样操作由 `Configure Slate Device.cmd`、`Remove Slate Device Configuration.cmd` 和 `Slate Device Status.cmd` 提供。

硬件界面启动前台会先核对普通用户本地控制台身份和 BIOS 机型。身份不符或机型不是已采集的 Panasonic Corporation / CFQV8-1L 时直接显示中文或英文原因，不创建设置锁或用户状态文件；服务端仍会重复核对身份、传感器、开关和显示能力，界面预检不能替代实际设备资格。

硬件入口不启用输入过滤驱动，也不需要测试签名。物理平板状态只来自 Intel HID Event Filter 的 ETW ACPI 事件（有效值为 `0xCC` 平板、`0xCD` 笔记本），不读取 CSM 作为输入。`ACPI\INT33D3` 只作为向 Windows 发布姿态的 CSM 输出；首个有效 ETW 事件前状态保持未知，所有 CSM 写入、恢复和日志清理禁用。ETW 启动失败或运行中断时服务失败并保留恢复日志，不回退 CSM。当前不发布 shell 平板模式，因此软件帐篷/支架不会自动触发 Windows 屏幕键盘。当前开放自动、笔记本、帐篷、支架和校准；平板仅由物理折叠状态产生，不是手动模式。输入目标和能力始终为 False，键盘和触控板保持可用。

硬件会话进程只负责当前用户显示；它先证明服务端登记的回执资格，再创建显示和 CSM 后端。CSM 写入通过服务快照读取的 ETW 健康门禁，物理状态未知时拒绝写入。显示和 CSM 共用同一个已验证 RPC 客户端，清理顺序为先释放显示/CSM 所有权，再关闭 CSM 设备。

显示恢复日志固定为每用户 `screen-display.json`，CSM 恢复日志为相邻的 `screen-display-slate.json`。显示仍走共用事务、写前日志、身份检查、回读和恢复，只拥有内屏方向与全局自动旋转锁。帐篷、支架和笔记本通过加速度计识别；完全折叠由实体开关交给 Windows。CSM 恢复始终采用 journal 中记录的原始值；写入或恢复失败时保留日志，不推进内部基线。

私有软件后台在设置文件旁写入同名 `.log`，达到 256 KiB 后滚动为 `.log.1`，只保留一份旧日志。日志在通过本地非提权身份检查后创建，不记录输入内容、触摸坐标或触摸屏数据。

恢复旧日志时若发现 Windows 原生自动旋转已接管，或 ETW 确认处于物理平板，即使当前方向恰好等于程序过去的写入，也先持久化撤销完成/待写方向所有权，再恢复用户方向锁并清理日志，不把旧软件方向覆盖回去。CSM 指示值不能证明物理姿态；物理状态未知且未能证明原生接管时拒绝恢复写入。固定模式在写入前和回执前重查 ETW 资格与原生旋转，未发生实际方向写入也必须复核，不能因方向恰好相同就确认成功。

硬件界面默认常驻：关闭窗口只隐藏界面，托盘提供打开与退出；`--background` 初次启动直接留在托盘。没有系统托盘时显示窗口与说明，保留可见的退出路径。每用户另有固定 UI 锁，再次启动只更新时间戳提示唤回旧窗口；该空文件只表达显示窗口，不读取内容或接收硬件回执、命令与设置。界面在触碰用户状态文件前核对非提权本地控制台身份，不能让远程或提权实例抢占本地 UI。托盘生命周期使用 [Qt 系统托盘 API](https://doc.qt.io/qt-6/qsystemtrayicon.html)。

「登录时启动」只在用户开启时写入 HKCU 的 `Software\Microsoft\Windows\CurrentVersion\Run` 中本项目的 `PanasonicPostureManager` 值；关闭时只删除与当前固定命令完全相同的值，发现其他内容则保留并显示冲突。不创建服务、计划任务或提权项，不更改 Windows 启动应用管理器的禁用选择。界面显示本项目启动登记是否存在，Windows 自身的启动管理或登录延迟仍可能阻止即时运行。命令使用绝对路径、正确引用与无控制台解释器，并拒绝超过 260 UTF-16 单元的命令；限制依据 [Microsoft Run 键说明](https://learn.microsoft.com/en-us/windows/win32/setupapi/run-and-runonce-registry-keys)。移动便携文件夹前应先在旧位置关闭此项，移动后重新开启。使用 `--settings` 的独立测试实例不开放自启动。

源码自启动由固定 `packaging/windows/launch-hardware.py` 补齐自身源码路径，不依赖登录时的工作目录、`PYTHONPATH` 或激活虚拟环境；便携包用自身 `runtime/pythonw.exe -I launch.py --background`。首次开发测试保持自启动关闭。姿态通知复用已校验快照、会话就绪和提示 epoch，只在可确认时发送一次；点击通知时，仅当服务实例、epoch、提示类型和当前可确认条件仍完全匹配才经页面现有事务确认并立即切换，不打开主窗口，否则只打开界面。Windows 可能同时交付通知点击和托盘激活，因此同一次点击只确认一次；实际显示可能受 Windows 通知设置影响，未用通知组件的“支持消息”标志冒充可见性验收。

授权真机可运行 `tests/windows_etw_switch_smoke.py` 和 `tests/windows_launcher_smoke.py`。前者只读 ETW 事件，后者验证受保护运行时、会话启动和回收；两者都不能替代安装后的服务、真实显示或通知验收。

只有硬件客户端接受真实来源与能力字段；软件预览客户端继续拒绝这些快照。显示回执由服务端核对登记进程、代次和租约，过期、锁屏及失去本地会话资格的回执不能续租。正常关闭先请求会话恢复，再等待回收；重新启动先恢复残留日志，不重放手动选择。

`tests/windows_sensor_parent_smoke.py --directory <新证据目录>` 只读采样，并验证强制终止采样父进程后辅助进程退出。2026-11-11 的 ETW 真机探针已确认 `0xCC/0xCD` 映射；尚未完成接入服务后的真实显示、CSM 写入与姿态矩阵验收。

需要验证私有软件事务时显式打开软件预览后台：

```bat
packaging\windows\run.cmd --backend --language zh_CN
```

内存演示仍使用 `--demo`，关闭窗口即清空。内部 Simulation 只作为隔离测试夹具，不再对用户开放，也不能声明真实设备已应用。

软件预览设置存于 `%LOCALAPPDATA%\PanasonicPostureManager\settings.json`；`--settings` 可指定独立测试文件。只保存偏好与校准矩阵，不保存手动模式、已应用结果或旧会话。沿用设置 v1、原子替换和 `.corrupt-<timestamp>` 损坏隔离。每个设置文件只允许一个后台实例。

软件预览通过子进程继承的私有标准管道通信，无网络监听或共享管道名。消息限制为 64 KiB，最多 32 个未完成请求，默认五秒超时。状态与完成回调在界面线程处理。断线立即使旧请求失效，重连采用有上限的退避，不重放写入。此通信能力只授予父进程，不替代硬件服务的会话鉴权。管道继承与句柄关闭依据 [Python 子进程文档](https://docs.python.org/3.11/library/subprocess.html)。

软件后台在产生首个状态前因启动条件失败退出时，可向父进程发送一个无实例、无自由文本的 `fatal` 回执。父进程只接受固定原因集 `backend-startup-failed`；未知或畸形原因仍按协议错误断开。技术 traceback 只进入后台日志，不进入界面。

在非 Windows 开发环境中，必须显式指定测试设置路径；默认不启动后台：

```sh
PYTHONPATH=src .venv-windows/bin/python -m panasonic_posture.fluent --backend --settings /tmp/posture-preview/settings.json --language zh_CN
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
.venv-windows\Scripts\python -m panasonic_posture.windows.sensor --orientation --seconds 10
```

此命令输出逐行 JSON，时间换算至 Python 单调时钟，单位为 m/s²，`mount_verified=false` 表示仍沿用传感器原始轴向，不能当作机型姿态校验。它逐次枚举 Standard 加速度计，只接受唯一且与默认传感器标识一致的设备，不设置 ReportInterval 或 ReadingTransform。WinRT 在刚打开时可能先返回很久以前的缓存；旧帧不会发布，启动最多等待 20 秒，新鲜样本间隔不能超过两秒。重复时间戳不更新样本时间。断线后间隔两秒重试，保持原设备身份和最后的原始时间戳；更换设备必须重新建立并验证安装方向。

`windows.sensor.WindowsSensor` 实现共用 SensorBackend 的只读接口，构造时要求显式传入安装方向矩阵。它校验采样与接收时间、墙钟和单调时钟的相对变化，遇到时钟跳变重新连接并建立时间基准；错误或过期时立即返回无效读数。普通软件后台不启用该端口；硬件服务组合根用 `ProfileSensor` 包装它，不采集 SimpleOrientation。传感器接口与固定坐标系见 [Microsoft Accelerometer 文档](https://learn.microsoft.com/en-us/uwp/api/windows.devices.sensors.accelerometer)。

`--orientation` 显式附加系统 SimpleOrientationSensor 的四象限/朝上/朝下快照，默认采样路径保持不变。方向源同样要求唯一、身份固定且 ReadingTransform 为原始值 0；不修改该属性或采样配置。方向快照有独立的观察时间，使用已经校验的时钟基准换算到单调时钟，不能把较早的加速度测量时间冒充方向观察时间，也不把观察时间当成方向变更事件时间；只有加速度计出现不同的有效采样时间时才发布，缓存不能续期。方向失败只使方向端口失效，不破坏仍正常的重力姿态采样。接口语义见 [Microsoft 简单方向说明](https://learn.microsoft.com/en-us/windows/uwp/devices-sensors/use-the-orientation-sensor)。

方向采集作为只读诊断和未来可选后端保留：要求显式四象限映射和连续的新鲜样本达到稳定窗口，平放朝上/朝下保持当前方向，失效与断档清除候选。CFQV8 原生只读验证在约两秒取得 14 个不同观察时间的有效快照，当前平放为值 4（朝上）。`tests/windows_sensor_parent_smoke.py --orientation --directory <新证据目录>` 已验证附加方向采集时，强制结束父进程仍会回收辅助进程；当前产品不会消费该方向。

2026-11-08，同一 Windows 10 虚拟机的普通用户下，持续采样约 3.4 秒取得 23 个不同时间戳的样本，退出时回收读取子进程。测试覆盖启动旧缓存、缓存不续期、设备更换、断线重试、时钟跳变与退出清理；不代表传感器安装方向或整机姿态已验收。

### 独立 Windows 硬件主机开发边界

这套特权主机及驱动链路保留为可选输入扩展。当前交付优先完成普通用户会话中的姿态与显示功能，允许暂不禁用内置键盘和触控板；不能因输入驱动或签名未就绪而阻塞其余用户态功能，也不能伪造输入完成回执来绕过现有引擎。

共用控制器允许平台在构造时显式声明 `input_control=False`，此时软件姿态目标如实保持 `disable_internal_input=False`；Linux 和已有平台默认仍为 True。引擎对这些无需输入的目标仍要求释放事务完成、有效租约与同代次显示回执，不能仅凭选择模式就确认。`windows/display_only_input.py` 表示从未取得输入所有权，任何意外接管或唤醒请求都会失败，不提供模拟驱动就绪回执。Windows 硬件服务使用这条路径，软件预览仍只保存偏好。

`windows/hardware_host.py`、`hardware_ipc.py` 和 `security.py` 是独立硬件主机的请求分发、通信与身份校验组件，沿用共用 Engine。`hardware_runtime.py` 提供组合根：先启动 ETW 物理源，再把其只读查询注入 Engine，随后启动 WinRT 传感器，最后创建 RPC 端点并启动可信会话监管器；`HardwareServer.poll()` 负责唯一一次引擎 tick。`hardware_service.py` 把该运行时适配到 SCM，只有在 `runtime\python-hardware.exe -B -I` 和受保护安装目录校验通过后才注册运行。服务不安装自研输入驱动，也不由普通界面自动启用；除了 SCM 状态与停止通知，不提供任意文件路径、设备开关或执行命令接口。

物理源准入是硬件主机的安全边界。首个有效 `0xCC/0xCD` 事件前物理值保持未知，CSM 写入、恢复和日志清理全部禁用。ETW 启动失败或运行中断时服务保持只读、拒绝会话准入并记录诊断，不把 CSM 当作回退；已有恢复日志保留，等待下次物理源可用时按原始值恢复。组合根没有自动安装入口，也没有把物理源失败回退成软件平板状态。

开发界面直接运行 `packaging\windows\run.cmd` 就连接 LocalSystem 所有的硬件端点。界面不会隐式安装或启动系统服务；服务由便携包中的管理员入口显式安装。服务不可用时只显示断线，不回退到软件后台或 CSM 推断。

`windows/hardware_client.py` 在一个有界工作线程中通信，每半秒请求状态，界面主循环先接收已校验快照再完成对应 Future；断线先使状态失效，再拒绝未完成请求。过期结果、服务实例变化、无效硬件能力声明都拒绝发布，重连不重放写入。UI 客户端不能提交显示回执。`windows/session_client.py` 供共用 SessionRuntime 的两个工作线程使用独立连接，防止显示/状态请求占用续租通道；所有连接绑定同一服务实例，实例变化使其他在途结果失效。回执仍由服务核对预登记的确切会话进程，客户端不能自行取得此资格。

隔离联测覆盖界面到硬件消息契约，以及硬件客户端、共用会话运行时、Windows 显示事务、恢复日志和 Engine 的链路。系统 API 与输入端口使用 fake；测试确认显示提交后才发送就绪回执、显示失败不获取输入、会话资格被拒绝后释放输入并恢复方向。这些结果不代表安装后的真实会话或内核输入验收。

`backend/windows/driver.c`、`control.c` 和相关测试仅保留为隔离研究记录，不属于 Windows 产品路径，不进入便携包、默认构建或安装流程。Windows 产品不计划签名、加载或安装自研输入过滤驱动，也不会修改测试签名、Secure Boot 或启动配置；键盘和触控板由 Windows 继续管理。

`windows/actuator.py` 实现服务端输入执行接口，只有 LocalSystem 可尝试打开固定的控制设备。它逐项核对二进制回执的长度、版本、序号、随机标识、就绪位和三秒租约；发送时间作为时限基准，迟到回执不能延长确认状态。忙状态等待后续空闲，已过期的续租不能当作重新接管；协议错误、关闭失败及释放失败沿用共用引擎的撤销和恢复路径。普通软件后台不加载此端口，当前仍未接入实际内核输入。

可在上述构建命令末尾添加 `--control-fixture`，同时生成只含纯 C 控制逻辑的非特权 DLL，然后运行跨语言检查：

```bat
set PYTHONPATH=src
python tests\windows_actuator_smoke.py --control-library build\windows-driver\posture-control-fixture.dll
```

此检查不安装或加载内核驱动、不打开输入设备、不改变启动安全配置。2026-11-09 的本机检查通过 Python/C ABI、获取/续租/过期、忙状态及释放，并通过共用 Engine 核对：输入驱动就绪不足以确认模式，仍须匹配代次的已认证显示回执；会话租约失效后撤销输入。这是控制逻辑和组件联测，不能替代真实输入接管与故障释放验收。

### Windows 原生显示与物理开关开发验证

`windows/etw_switch.py` 只消费 Intel HID Event Filter 的 Event 115 ACPI 通知，`0xCC` 为物理平板、`0xCD` 为笔记本；`0xCA/0xCB` 和重复/歧义载荷不产生状态。首个有效事件前返回未知，不读取 CSM、shell 模式或注册表作为基线。ETW 启动失败或事件源中断时返回 `permission`/`read_error`，硬件运行时会撤销物理准入、停止会话并保持只读，不回退 CSM。

`windows/display.py` 通过 CCD 识别唯一内屏，拒绝镜像或输出身份变化，复用 `session/transaction.py` 和显式恢复日志执行方向写入、回读与恢复。写入先用 `CDS_TEST` 验证，不持久化显示注册表配置。固定方向会在取得并回读全局方向锁后写入；`NativeDisplay.lock()` 在相关 shell 导出不可用或回读失败时拒绝确认。实体开关仍处于平板而传感器姿态已经开始展开时，`prepare_hardware_release()` 先记录并锁定当前翻转横屏，避免随后开关释放触发 `2→0→2`。等待期仍只读跟踪最终方向；稳定目标只在方向不一致时执行一次事务。实体 PDO 和硬件姿态查询保持独立，当前不发布软件 shell 平板状态。旧版日志中的 shell 字段只作兼容读取并在恢复时丢弃。方向与方向锁保存在同一恢复日志，旧版日志缺少新增字段时仍兼容读取。`SetDisplayAutoRotationPreferences` 仅作用于当前进程，不能替代这里的系统方向锁；参见 [Microsoft API 文档](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-setdisplayautorotationpreferences)。

没有活动内屏、但有一个或多个活动外屏时，原生工厂使用只读的 `ExternalOnlyDisplay`。它记录并复核对活动外屏身份，姿态操作和持续刷新只验证拓扑稳定后返回，不取得旋转锁、不写 CCD、不创建显示日志；姿态和平板状态通知仍按共用链路处理。固定软件姿态由共用刷新通道限速复核，外屏拔出、替换或内屏重新出现时撤销成功并走恢复/重试。若设置目录已有未恢复的内屏显示日志，工厂拒绝进入只读外接模式，等待内屏恢复或用户按原恢复流程处理，不能静默丢弃旧所有权。内外屏同时存在仍要求内屏唯一且不镜像。

已授权的真机可显式运行独立内屏事务检查，屏幕会短暂倒转。检查保留恢复日志，另启恢复进程，并验证写入进程被强制结束后的恢复；每次使用新的证据目录：

```bat
set PYTHONPATH=src
python tests\windows_display_only_smoke.py --exercise-display --directory build\display-only-evidence-new
```

该检查验证帐篷倒转、完整回读、正常回正，以及强杀显示 worker 后由持久日志恢复。它不打开平板指示器、不修改 shell、不取得输入所有权。

ETW 物理源检查：

```bat
set PYTHONPATH=src
python tests\windows_etw_switch_smoke.py --directory build\etw-evidence-new
```

操作者必须实际折叠和展开一次。脚本只读 Event 115，不写 CSM、shell 或注册表；它不能替代硬件服务接入后的显示与 CSM 写入验收。

无驱动 ETW 物理开关探针必须在 LocalSystem 会话中运行。它只监听现有 Intel HID Event Filter 的 ACPI 通知，不写 CSM、shell 或注册表；运行期间应实际折叠和展开一次，并检查事件 115 的 `0xCC/0xCD` 映射：

```bat
set PYTHONPATH=src
python tests\windows_etw_switch_smoke.py --directory build\etw-switch-evidence-new --seconds 30
```

探针结果只证明事件源是否可用，不能替代生产服务装配。普通用户启动 ETW 必须被拒绝；事件缺失、权限失败或映射不确定时，生产路径不得写 CSM。`windows/etw_switch.py` 已接入 `HardwareRuntime`，并由显式安装的 LocalSystem 服务承载；首个真实服务事件和后续显示/CSM 事务仍需单独验收。

2026-11-11 在 CFQV8-1L 上以临时 SYSTEM 计划任务执行该探针，物理折叠/展开期间 CSM 为 `1→0→1`，Event 115 收到 `0xCC` 和 `0xCD`；前者映射为平板、后者映射为笔记本，`0xCA/0xCB` 只作为通知序列而不改变物理状态。任务成功退出并删除，探针未写 CSM。该证据验证了 ETW 物理事件源，不等于生产后台已接线或完整姿态矩阵通过。

2026-11-09，CFQV8-1L、Windows 10 专业版 19045 x64 上取得笔记本、帐篷、支架和完全折叠的真实采样：笔记本 z 约 -9.8 m/s²，帐篷 y 约 -9.3 m/s²，支架 z 约 +9.9 m/s²。2026-11-10 以实体姿态和界面结果复核后确认该固件/驱动映射为 `0=折叠平板，1=笔记本电脑`；此前两次仅按数值变化推断方向的阶段结论均已作废。此证据确认当前采样的 y/z 方向与策略一致，不代表完整安装矩阵、持续会话控制或整机验收。2880×1920 内屏的倒转、回读、正常恢复和写入进程被结束后的日志恢复通过，最终方向与原方向一致。驱动反汇编和探测确认 `msgpiowin32` 接受一个字节时执行 toggle、读取返回当前字节，并发出 CSM 状态变化；软件姿态若写该设备会覆盖真实硬件状态，因此生产入口保持它只读。软件平板状态模拟当前暂停，屏幕键盘和壳层响应不作为可用能力声明。2026-11-11 时序实测在 `03:45:24.853` 开关释放并变为桌面方向，`03:45:25.757` 恢复帐篷方向，残余窗口约 0.9 秒。

同机从 `C:\util2\drivers\SynTP_d20194747` 安装了签名有效、硬件 ID 精确匹配的厂商驱动；安装后为 Synaptics HID TouchPad / Synaptics HID Device 19.5.26.18，用户确认移动、点击、滚动正常。自研输入过滤驱动没有加载；用户选择保留启动安全配置，测试签名仍关闭。这些证据不提升 Windows 机型支持等级。

Windows 源码虚拟环境的 `python.exe/pythonw.exe` 可能是转发启动器。软件客户端直接持有实际解释器进程，并从当前源码/安装位置加载标准库后台，避免结束启动器后实际后台仍占用设置锁；重连仍不重放写入。

硬件通道独立于软件预览的私有标准管道，使用 `windows-hardware` JSON v1 信封和本地主机命名管道。服务端要求 LocalSystem，端点所有者也是 LocalSystem，拒绝远程客户端；交互用户只获读写权限，明确排除与 FILE_APPEND_DATA 共用数值的 FILE_CREATE_PIPE_INSTANCE。首实例独占端点名称，最多四个连接，每条消息至多 64 KiB，连接请求五秒超时。客户端核对端点所有者，不自动重放请求；服务实例和每连接递增请求序号用于拒绝旧请求。

身份来自管道模拟令牌与实际客户端进程令牌，必须匹配 SID、登录 LUID 和会话 ID，并核对 PID、进程创建时间、当前本地控制台以及未锁定的活跃会话。界面可以提交模式和偏好选择；只有由可信主机预先登记的确切会话进程才能提交显示回执，不能通过 RPC 自行注册 PID。身份检查放在独立有界后台队列，主循环继续推进引擎；一秒内未获得新的会话确认即失去资格，慢查询结果不能延长有效期。失联、退出、锁屏或用户切换通过共用 `Engine.revoke_session` 撤销对应所有者并走原有释放事务。它不改变 Linux 鉴权、租约时长或 D-Bus 方法。

接口依据：[Microsoft 命名管道权限](https://learn.microsoft.com/en-us/windows/win32/ipc/named-pipe-security-and-access-rights)、[WTS 会话状态](https://learn.microsoft.com/en-us/windows/win32/api/wtsapi32/ns-wtsapi32-wtsinfoex_level1_w)。

隔离通信检查为 `tests/windows_hardware_smoke.py`：仅在已获授权的 Windows 测试虚拟机中分别以 LocalSystem 和普通交互用户运行 server/client，显式传入同一临时 `--directory`。该 fixture 使用不支持硬件的共用引擎和空输入端口，不打开输入设备。2026-11-08 的原生检查已通过端点所有者、控制台令牌、拒绝不支持模式、拒绝未登记回执、拒绝客户端创建服务端实例及拒绝用户自建冒充端点六项检查，普通客户端未提权。模拟状态和此通信证据均不代表输入驱动验收。

2026-11-09，在上述 CFQV8 真机上，以受保护且校验过哈希的代码/运行时快照执行一次性 SYSTEM 任务：GPIO 笔记本状态与交互用户一致，WinRT 取得同一加速度计的五份新鲜样本，普通非提权客户端再次通过六项原生通信检查；快照没有发布支持或已应用模式。Session 0 无交互显示输出，显示查询返回不可用，未尝试显示写入。任务已结束并移除，没有保留提权助手或改变启动安全配置。

### Windows 原生会话开发验证

`windows/session_agent.py` 将原生会话观察、硬件客户端、共用 SessionRuntime 和 Windows 显示事务装配为独立的普通用户进程。它拒绝 SYSTEM、非交互和已提权令牌；必须先通过主机对确切进程身份的显示回执检查，才能创建显示后端或恢复日志事务。启动握手复用显示工作线程，不额外占用第三条 RPC 通道。普通 UI 不启动此进程；`hardware_runtime.py` 和已安装的 SCM 主机在物理准入通过后负责信任启动，真实显示、CSM 和 SYSTEM 生命周期仍需单独验收。

开发入口为 `python -m panasonic_posture.windows.session_agent --journal <当前用户的恢复日志路径>`；必须由可信硬件主机预登记该确切进程，手动运行不能自行取得回执资格。缺少服务或未获登记时在显示后端构造之前退出，不能把命令能运行当作安装成功。

主机内部的会话进程替换操作不在 RPC 方法表中。替换前撤销旧所有者，清除身份缓存并退休所有在途管道鉴权；未完成的旧身份查询仍占用原有工作槽，结果不会用于新进程。这样可在不增加客户端权限的前提下为可信启动器更换确切进程。

`windows/trusted_runtime.py` 为安装后的主机/会话启动提供文件检查：按句柄检查所有者和有效 DACL，拒绝普通用户写入、重解析点、空 DACL、未支持的权限条目及遍历失败；保留禁止删除共享的路径句柄。解释器搜索路径须精确限制为安装目录中的标准库、DLL 和应用，不允许 `import site` 或用户路径。此要求独立于可搬移的软件预览包，不能直接把现有预览 ZIP 提权运行。当前源码目录会被拒绝作为特权运行时。

会话进程支持主机提供的 `--stop-event`，只打开并等待 SYSTEM 所有的随机事件，不取得设置事件的权限。信号到达时先执行共用运行时的显示恢复，再关闭事件句柄；名称冲突或普通用户冒充的事件会被拒绝。

`windows/session_launch.py` 只供可信主机调用：读取活跃本地控制台的用户令牌，必要时只接受同一 SID、登录实例和会话的非提权关联令牌，使用该用户的环境和 LocalAppData 日志路径。只启动已固定并校验的解释器与会话模块，不继承主机句柄；以挂起状态创建进程，绑定关闭即终止子进程的 Job，核对真实 PID、创建时间、令牌和控制台资格。放行前再次核对资格和停止事件。没有提供 RPC 命令、可执行路径、PID 或令牌参数入口；主机不代替用户写恢复日志。接口依据：[CreateProcessAsUserW](https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-createprocessasuserw)、[用户环境](https://learn.microsoft.com/en-us/windows/win32/api/userenv/nf-userenv-createenvironmentblock)、[用户目录的令牌权限](https://learn.microsoft.com/en-us/windows/win32/api/shlobj_core/nf-shlobj_core-shgetknownfolderpath)。

`windows/session_supervisor.py` 在主机循环中先登记确切进程，再安排工作线程放行；WTS、创建和资源清理占用一个有界工作槽，不阻塞引擎。控制台用户变化时先撤销旧角色、发出停止信号，等待最多五秒的显示恢复窗口，再清理旧进程，之后才能启动替代进程。过期创建结果、停止期间返回的进程和清理未完成的进程都不能被当作可用会话；主机停止须继续轮询到协调器确认关闭，不能只取消 Future 就报告清理成功。

上述启动和协调的单元测试覆盖结构布局、标志、句柄、令牌、登记/放行顺序、切换与退出。2026-11-09，CFQV8 / Windows 10 Pro 19045 上另以哈希固定、ACL 与所有者均验证的隔离运行时执行两轮真实 SYSTEM 任务：创建非提权控制台用户进程、挂起期间登记确切身份、放行与取得租约、停止前撤销租约、停止信号及进程回收全部通过。普通用户客户端不能提交显示回执或写入受保护运行目录；引擎始终不支持硬件且没有 applied 结果，没有打开输入或采样设备、加载驱动或改变启动安全配置。任务已自动移除，清理目录的 UAC 请求被取消，因此保留 `C:\Program Files\PostureManager.Validation.e19cebe6fe87` 的只读快照，无常驻测试助手。此结果不等同于完整硬件服务安装、换用户、锁屏交接或强制终止主机后的 Job 回收验收。

显式复现入口为 `tests/windows_launcher_smoke.py --prepare-runtime <新证据目录>`：普通用户生成标准库、应用与 fixture 的 SHA-256 清单，不提权、不启动设备。审阅并固定输出的 helper 哈希与清单哈希后，经 UAC 在管理员进程中执行 `tests/windows_launcher_admin.ps1 -Action Run -TaskName PostureManager.Validation.<12位小写十六进制> -Manifest <清单绝对路径> -ManifestHash <已固定哈希>`。管理员先逐文件复制并验证哈希，设置受保护 ACL 和可信所有者，再用安装解释器验证整个运行时；任务仅以 SYSTEM 运行无设备 fixture，最多两分钟并自动撤销任务。`ready.json` 出现后十八秒内，普通用户运行 `tests/windows_launcher_smoke.py --ordinary-instance <ready中的实例> --root <受保护目录>`，将结果保存在普通用户自己的证据目录。读取并保存结果后，`-Action Cleanup -TaskName <同一名称>` 仅删除经绝对路径、标记、任务已移除及无重解析点检查的精确目录。`RunStaged` 可在同一验证目录复测，仍先验证实际 ACL 和隔离解释器；这些脚本不更改全局 PowerShell 执行策略，不能通过临时放宽生产权限来运行。

`windows/session_context.py` 用独立的单工作线程读取本进程会话，结合本地活跃控制台、锁屏状态，以及 `GUID_SESSION_DISPLAY_STATUS` 的系统电源回调。电源上下文与控制资格分开：读数超过一秒、未知或查询失败会使控制资格失效，已知的单纯熄屏则不使其失效。睡眠使旧查询失效，恢复时重新注册电源观察，取得当前值。回调只更新有锁的状态，不执行显示、输入或租约操作。依据：[会话显示电源状态](https://learn.microsoft.com/en-us/windows/win32/power/power-setting-guids)、[注册后的初始电源回调](https://learn.microsoft.com/en-us/windows/win32/api/powersetting/nf-powersetting-powersettingregisternotification)。

控制资格失效时，适配器通过既有 `display-unavailable` 错误回执撤销软件输入，沿用共用运行时的方向恢复；不能只发无错误的未就绪心跳，因为引擎会为软件方向切换保留已有输入。独立通道维持允许续租的会话上下文，恢复时先等待旧未就绪心跳完成，再允许新的就绪回执。显示事务执行中失去资格会恢复并报告失败，即使恢复期间很快重新取得资格，也要重新完成事务。单纯熄屏不作为撤销条件，普通原生入口复用共享的直接姿态策略并持续租约；真实锁屏、亮屏和睡眠恢复变化仍须真机验收。Windows 保持系统键盘和触控板可用，因此不实现 Linux 输入禁用路径的专用触摸唤醒。上述能力不代表 LocalSystem 硬件会话、输入禁用或服务安装已完成。

以下检查只观察，不锁屏、熄屏、休眠、唤醒或旋转；每次使用新的证据目录：

```bat
set PYTHONPATH=src
python tests\windows_session_smoke.py --observe-only --directory build\session-evidence-new
```

已授权的交互式锁屏和显示器电源切换检查会锁定本机并短暂关闭显示器，不会挂起系统：

```bat
set PYTHONPATH=src
python tests\windows_power_transition_smoke.py --exercise-transitions --directory build\power-transition-evidence-new
```

运行时需要操作者完成解锁，随后脚本移动 1 像素唤醒显示器。结果分别记录锁屏时控制资格失效、解锁恢复，以及已知熄屏时控制资格保持。

2026-11-10，CFQV8 / Windows 10 Pro 19045、普通非提权进程下主动完成锁屏→解锁和熄屏→亮屏转换：锁屏状态 `locked=true` 时控制资格为 false，解锁后恢复 true；已知熄屏时 `display_on=false`、控制资格仍为 true，指针移动后亮屏恢复。通知注册全部释放。未主动挂起系统，未运行完整原生后台，未修改平板指示器配置。

同日只读传感器及方向采集、精确父进程退出清理、显示事务正常倒转/回正和强杀 worker 后恢复均通过，最终内屏回到原方向和方向锁，证据目录中的显示日志均已清除。完整原生后台仍因 `ACPI\INT33D3` 仍为独占访问而无法启动，错误提示需要 `LaptopSlateExclusiveAccess=0` 并重启；未执行管理员配置。

同机的只读原生文件检查接受系统盘和 Program Files 的受保护祖先目录，拒绝用户拥有的源码目录作为特权运行时；普通用户临时创建的冒充停止事件也被实际拒绝。临时事件句柄已关闭，没有调整文件权限、安装服务、加载驱动或操作显示。

### Windows 便携预览包

在 Windows 的独立构建环境中，使用与预览依赖兼容的 64 位 Python 3.11+：

```bat
python -m pip install --target build\windows-dependencies -r packaging\windows\requirements-ui.txt
python packaging\windows\build.py --dependencies build\windows-dependencies
```

构建器从当前 Python 基础安装和独立依赖目录读取运行时、Qt/Fluent 及随包许可，连同源码、插画、翻译和维护说明直接流式写入 ZIP，同时计算逐文件校验，减少临时磁盘写入和内存占用。预览只使用 Qt Widgets，因此排除 PyQt6 自带但未使用的 QML 资源，避免 Windows 传统解压工具遗漏超长路径文件。完整归档生成后才替换已有包。`runtime/python._pth` 和 `runtime/pythonw._pth` 只服务 UI，隔离注册表和 `PYTHONPATH`，并显式包含随包 Qt 站点目录。硬件服务和会话代理使用额外的 `runtime/python-hardware.exe` 与 `runtime/python-hardware._pth`，搜索路径严格限制为 `Lib`、`DLLs` 和 `../app`，不处理 `site` 或 UI 包。构建器不再生成 DLL 级 `_pth`，避免它覆盖受限硬件解释器；参见 [Python Windows 搜索路径说明](https://docs.python.org/3.11/using/windows.html#finding-modules)。基础安装的第三方包、测试数据或配置不进入归档。

输出为 `dist/windows/panasonic-posture-windows-preview.zip` 和 SHA-256 校验文件。解压后运行 `Hardware.cmd` 或默认 `Panasonic Posture Manager.cmd` 都会连接 LocalSystem 硬件服务；`Install Hardware Service.cmd` 负责显式安装和启动，未安装时界面只显示断线。`--backend` 是隔离软件预览，`--demo` 是内存演示。ZIP 内 `MANIFEST.json` 记录逐文件校验值，第三方许可保留在 `runtime/Lib/site-packages/*.dist-info/`、Python 许可为 `runtime/LICENSE.txt`。更新时服务入口保留前一受保护目录，用户设置继续位于 LocalAppData。

2026-11-11，在 CFQV8-1L / Windows 10 Pro 19045 上从当前测试包实际安装 `PanasonicPostureManagerHardware`：服务为延迟自动启动、以 LocalSystem 运行，`sc qc` 显示受保护 `%ProgramFiles%` 解释器和固定 `--serve` 参数，失败动作配置为 5/15/60 秒重启。服务进入 Running，命名管道 `\\.\pipe\PanasonicPostureManager.Hardware.v1` 存在；普通用户再次执行 `GetState` 得到 `backend=ready`、`sensor_source=windows-winrt`、`supported=true`。受保护安装目录 3010 个清单文件全部一致，受限解释器预检通过。首次有效物理 ETW 事件前 `hardware_status=absent`，因此本轮未执行物理切换、显示写入、CSM 写入或会话准入验收。

`.github/workflows/windows-preview.yml` 在 Windows 上运行 Python 3.11/3.13/3.14 的后台、界面、只读探针解析和翻译测试，构建 ZIP，再用解压包自带的 Python 验证 Qt、中文资源、模式事务和后台退出。流程仅保存构建制品，不发布 Release、不改版本或 AUR。提交这份流程不代表远端构建已执行。

Windows 主页和设置页把已校验快照中的稳定错误码映射为与 Linux 相同的简短用户文案；用户点击确认、返回自动、重试、切换设置或校准时的失败 Future 也只在错误对象携带已知稳定码时显示对应原因，未知异常和技术 detail 不进入界面。原始错误保留在诊断日志中。离屏验证使用已安装的系统字体：测试辅助函数将 `QT_QPA_FONTDIR` 指向 `%SystemRoot%\Fonts`，避免通用离屏插件找不到字体，导致文字、导航宽度与原生窗口不同。界面测试会检查字体确实已加载，保留布局与操作断言。源码检查明确按 UTF-8 读取文件，不依赖 Windows 系统区域编码。

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
