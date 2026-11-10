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

要连接独立软件后台，运行以下命令；在 Windows 直接启动模块也会使用软件后台。

```bat
packaging\windows\run.cmd --language zh_CN
```

普通启动允许保存自动模式偏好，设备控制和校准保持禁用。后台是当前用户的子进程，不注册 Windows 系统服务、不要求管理员权限；未来的特权硬件控制须另行设计，不能直接加入此用户进程。

CFQV8-1L 的真实用户态开发入口为：

```bat
packaging\windows\configure-slate-device.cmd -Action Install
packaging\windows\run.cmd --native-backend --language zh_CN
```

第一条命令需要管理员权限，只把已核对的 `ACPI\INT33D3` 驱动配置设为 `LaptopSlateExclusiveAccess=0`，并在首次设置时记录安装前值；必须重启一次才会生效。`-Action Remove` 恢复安装前配置，`-Action Status` 只报告当前状态。便携包的同样操作由 `Configure Slate Device.cmd`、`Remove Slate Device Configuration.cmd` 和 `Slate Device Status.cmd` 提供。

原生入口不使用模拟端口、不启用输入过滤驱动，也不需要测试签名。只接受已采集 Windows 姿态证据的 Panasonic Corporation / CFQV8-1L、唯一且型号匹配的 WinRT 加速度计、原有 GPIO 开关与内屏选择检查，以及实例 `ACPI\INT33D3\2&DABA3FF&0`。设备访问只认 PDO 名称，先读取当前字节；一次写会使驱动切换状态，因此仅在目标不同时写一次并立即回读。绝不直接修改 `PriorityControl\ConvertibleSlateMode`、共享内存或用户 shell 偏好。当前开放自动、笔记本、帐篷、支架、手动软件平板和校准。软件平板只在显示后端具备持续刷新能力时出现在界面；输入目标和能力始终为 False，键盘和触控板保持可用，这是产品范围，不是待补功能。

软件平板的内部事务位于 `windows/tablet_display.py`，真实工厂显式提供系统简单方向映射；`SimpleOrientation` 的逆时针设备旋转按固定表转换为显示事务的顺时针象限，所有动态方向经已有日志、回读及恢复处理。共用 `SessionRuntime` 的可选 `DisplayRefresh` 与 apply/restore 串行，刷新后验证实例和代次；新目标先发送就绪回执再开始持续刷新，并以限速心跳续租，避免刷新饿死确认。资格丢失或故障时撤销结果并恢复，Linux 默认不启用此端口。真实工厂和模式能力已接入；隔离测试通过仍不代表四向真机旋转已验收。

原生偏好独立存于 `native-settings.json`。显示恢复日志固定为同目录 `screen-display.json`，slate 租约日志为同目录 `screen-display-slate.json`，并有独立的每用户控制器锁，换一个 `--settings` 文件也不能启动第二个原生显示写入者。显示仍走共用事务、写前日志、身份检查、回读和恢复，只拥有内屏方向与全局自动旋转锁。帐篷、支架和物理平板另行报告平板使用状态，笔记本报告普通使用状态；Windows 根据用户自己的 shell 选择决定是否切换。通知失败会阻止姿态确认，但当前 shell 仍为桌面且用户选择“不要切换”属于策略允许的结果，不被当作失败。每个姿态先锁定当前方向，再报告 slate 状态，最后应用显示，防止 Windows 异步响应时出现中间旋转画面。slate 操作在首次写前原子记录启动值；进程被强杀后，下次原生会话先读取租约和当前设备值，在首个姿态请求时才协调：设备已等于目标就沿用现状并清除租约，否则至多一次切到目标。未收到姿态请求便退出时恢复租约中的启动值；物理平板交接随后把旋转交还 Windows，正常退出时先恢复方向事务，再把平板指示器恢复到本次会话的基准值。

恢复旧日志时若发现 Windows 原生自动旋转已接管，或唯一 GPIO 开关确认处于物理平板，即使当前方向恰好等于程序过去的写入，也先持久化撤销完成/待写方向所有权，再恢复用户方向锁并清理日志，不把旧软件方向覆盖回去。旧版日志中的 shell 所有权字段只作兼容读取并丢弃，恢复不会改写当前 shell。转交保存失败则保留原日志与内存记录，等待重试；物理状态不明且未能证明原生接管时拒绝恢复写入。固定模式在写入前和回执前重查物理资格与原生旋转，未发生实际方向写入也必须复核，不能因方向恰好相同就确认成功。

真实后台入口默认常驻：关闭窗口只隐藏界面，托盘提供打开与退出；`--native-backend --background` 初次启动直接留在托盘。没有系统托盘时显示窗口与说明，保留可见的退出路径。每用户另有固定 UI 锁，再次启动只更新时间戳提示唤回旧窗口；该空文件只表达显示窗口，不读取内容或接收硬件回执、命令与设置。原生 UI 和后台在触碰用户状态文件前核对非提权本地控制台身份，不能让远程或提权实例抢占本地 UI；锁屏的本地会话可启动，控制仍等待原有资格检查。托盘生命周期使用 [Qt 系统托盘 API](https://doc.qt.io/qt-6/qsystemtrayicon.html)。

「登录时启动」只在用户开启时写入 HKCU 的 `Software\Microsoft\Windows\CurrentVersion\Run` 中本项目的 `PanasonicPostureManager` 值；关闭时只删除与当前固定命令完全相同的值，发现其他内容则保留并显示冲突。不创建服务、计划任务或提权项，不更改 Windows 启动应用管理器的禁用选择。界面显示本项目启动登记是否存在，Windows 自身的启动管理或登录延迟仍可能阻止即时运行。命令使用绝对路径、正确引用与无控制台解释器，并拒绝超过 260 UTF-16 单元的命令；限制依据 [Microsoft Run 键说明](https://learn.microsoft.com/en-us/windows/win32/setupapi/run-and-runonce-registry-keys)。移动便携文件夹前应先在旧位置关闭此项，移动后重新开启。使用 `--settings` 的独立测试实例不开放自启动。

源码自启动由固定 `packaging/windows/launch-native.py` 补齐自身源码路径，不依赖登录时的工作目录、`PYTHONPATH` 或激活虚拟环境；便携包用自身 `runtime/pythonw.exe -I launch.py --native-backend --background`。首次开发测试保持自启动关闭。姿态通知复用已校验快照、会话就绪和提示 epoch，只在可确认时发送一次；点击通知时，仅当后台实例、epoch、提示类型和当前可确认条件仍完全匹配才经页面现有事务确认，否则只打开界面。实际显示可能受 Windows 通知设置影响，未用通知组件的“支持消息”标志冒充可见性验收。

授权真机可运行 `tests/windows_resident_smoke.py --exercise-resident --directory <新证据目录>`。CFQV8 已通过真实后台隐藏后继续运行、另一个进程用公开入口唤回同一窗口、无源码环境变量的后台登录入口，以及明确退出后的清理；验证没有改动本机登录启动登记，也没有注销登录或声称原生通知已目视验收。

只有显式原生客户端接受真实来源与能力字段，普通和模拟客户端仍拒绝这些快照。内部显示回执由有界队列交回引擎线程处理，过期、锁屏及失去本地会话资格的回执不能续租；原有 LocalSystem 管道和鉴权完全不变。正常关闭先请求后台恢复，最多等待六秒，失去响应再终止；重新启动先恢复残留日志，不重放手动选择。WinRT 辅助进程核对父进程 PID 和创建时间，父进程被强制终止后自行退出。

原生验证脚本 `tests/windows_screen_smoke.py --exercise-screen --directory <新证据目录>` 会实际倒转内屏、测试正常退出与强制退出后的恢复，仅在获授权且已解锁的真机运行；`--locked-session` 验证锁屏拒绝与正常退出。`tests/windows_sensor_parent_smoke.py --directory <新证据目录>` 只读采样，并验证强制终止采样父进程后辅助进程退出。四向软件平板使用 `tests/windows_software_tablet_smoke.py --exercise-software-tablet --directory <新证据目录>`：操作者依次把设备置于正向、逆时针 90°、倒置和逆时针 270°，脚本核对系统方向值、内屏显示变换、已确认模式和错误状态，并要求目视确认屏幕保持竖直，最后验证正常退出恢复。2026-11-09，CFQV8 已通过锁屏拒绝、真实采样及父进程退出清理，并在解锁后通过完整真实后台的笔记本、帐篷倒转、支架回正、偏好保存、校准及重置、正常退出恢复、强制退出重连恢复；最终内屏回读为原有正常方向，恢复日志已清除。此次未改变物理折叠姿态、测试四方向软件平板或禁用输入，不能据此宣称完整姿态验收通过。

需要验证完整软件事务时显式打开后台模拟模式：

```bat
packaging\windows\run.cmd --simulate-backend --language zh_CN
packaging\windows\run.cmd --simulate-backend --scenario prompt
```

后台模拟通过共用姿态策略、服务引擎、会话运行时和显示事务执行，输入和显示端口仅修改模拟状态。`slow` 暂缓显示回执，`error` 注入显示失败，`hardware` 模拟物理开关优先；`disconnected` 仅供原有 `--demo` 使用。模拟结果不代表真实设备已应用。

普通设置存于 `%LOCALAPPDATA%\PanasonicPostureManager\settings.json`，模拟设置为同目录下的 `simulation.json`；`--settings` 可指定独立测试文件。只保存偏好与校准矩阵，不保存手动模式、已应用结果或旧会话。沿用设置 v1、原子替换和 `.corrupt-<timestamp>` 损坏隔离。每个设置文件只允许一个后台实例。

界面通过子进程继承的私有标准管道通信，无网络监听或共享管道名。消息限制为 64 KiB，最多 32 个未完成请求，默认五秒超时。状态与完成回调在界面线程处理。断线立即使旧请求失效，重连采用有上限的退避，不重放写入；普通与模拟预览关闭窗口会回收后台，真实入口从托盘退出才回收。此通信能力只授予父进程，不替代未来系统服务的会话鉴权。管道继承与句柄关闭依据 [Python 子进程文档](https://docs.python.org/3.11/library/subprocess.html)。

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
.venv-windows\Scripts\python -m panasonic_posture.windows.sensor --orientation --seconds 10
```

此命令输出逐行 JSON，时间换算至 Python 单调时钟，单位为 m/s²，`mount_verified=false` 表示仍沿用传感器原始轴向，不能当作机型姿态校验。它逐次枚举 Standard 加速度计，只接受唯一且与默认传感器标识一致的设备，不设置 ReportInterval 或 ReadingTransform。WinRT 在刚打开时可能先返回很久以前的缓存；旧帧不会发布，启动最多等待 20 秒，新鲜样本间隔不能超过两秒。重复时间戳不更新样本时间。断线后间隔两秒重试，保持原设备身份和最后的原始时间戳；更换设备必须重新建立并验证安装方向。

`windows.sensor.WindowsSensor` 实现共用 SensorBackend 的只读接口，构造时要求显式传入安装方向矩阵。它校验采样与接收时间、墙钟和单调时钟的相对变化，遇到时钟跳变重新连接并建立时间基准；错误或过期时立即返回无效读数。普通软件后台不启用该端口；显式 `--native-backend` 的已验证机型工厂会附带方向采集，设备支持等级仍以运行时探针和机型配置为准。传感器接口与固定坐标系见 [Microsoft Accelerometer 文档](https://learn.microsoft.com/en-us/uwp/api/windows.devices.sensors.accelerometer)。

`--orientation` 显式附加系统 SimpleOrientationSensor 的四象限/朝上/朝下快照，默认采样路径保持不变。方向源同样要求唯一、身份固定且 ReadingTransform 为原始值 0；不修改该属性或采样配置。方向快照有独立的观察时间，使用已经校验的时钟基准换算到单调时钟，不能把较早的加速度测量时间冒充方向观察时间，也不把观察时间当成方向变更事件时间；只有加速度计出现不同的有效采样时间时才发布，缓存不能续期。方向失败只使方向端口失效，不破坏仍正常的重力姿态采样。接口语义见 [Microsoft 简单方向说明](https://learn.microsoft.com/en-us/windows/uwp/devices-sensors/use-the-orientation-sensor)。

纯方向判定要求显式四象限映射和连续的新鲜样本达到稳定窗口，平放朝上/朝下保持当前方向，失效与断档清除候选；软件平板显示执行使用固定表把系统简单方向的逆时针值转换为顺时针显示变换，不用原始加速度轴猜测方向映射。CFQV8 原生只读验证在约两秒取得 14 个不同观察时间的有效快照，当前平放为值 4（朝上）；四向旋转仍需实际摆放与显示配合验证。`tests/windows_sensor_parent_smoke.py --orientation --directory <新证据目录>` 已验证附加方向采集时，强制结束父进程仍会回收辅助进程。

2026-11-08，同一 Windows 10 虚拟机的普通用户下，持续采样约 3.4 秒取得 23 个不同时间戳的样本，退出时回收读取子进程。测试覆盖启动旧缓存、缓存不续期、设备更换、断线重试、时钟跳变与退出清理；不代表传感器安装方向或整机姿态已验收。

### 独立 Windows 硬件主机开发边界

这套特权主机及驱动链路保留为可选输入扩展。当前交付优先完成普通用户会话中的姿态与显示功能，允许暂不禁用内置键盘和触控板；不能因输入驱动或签名未就绪而阻塞其余用户态功能，也不能伪造输入完成回执来绕过现有引擎。

共用控制器允许平台在构造时显式声明 `input_control=False`，此时软件姿态目标如实保持 `disable_internal_input=False`；Linux 和已有平台默认仍为 True。引擎对这些无需输入的目标仍要求释放事务完成、有效租约与同代次显示回执，不能仅凭选择模式就确认。`windows/display_only_input.py` 表示从未取得输入所有权，任何意外接管或唤醒请求都会失败，不提供模拟驱动就绪回执。这条路径由显式 `--native-backend` 接入，普通预览仍只保存偏好。

`windows/hardware_host.py`、`hardware_ipc.py` 和 `security.py` 是独立硬件主机的请求分发、通信与身份校验组件，沿用共用 Engine；当前没有注册常驻系统服务、安装自研输入驱动，也不由普通预览自动启用。它们不提供任意文件路径、设备开关或执行命令接口。真正输入执行端口、可信物理开关和显示会话适配器未装配前，不得发布硬件支持或已应用模式。

开发界面可用 `packaging\windows\run.cmd --hardware-backend` 显式连接 LocalSystem 所有的硬件端点，不能同时指定演示、软件后台、模拟场景或本地设置路径。它不安装或启动系统服务；目前缺少完整的服务安装与可信会话启动流程。服务不可用时只显示断线，不回退到软件或模拟后台。

`windows/hardware_client.py` 在一个有界工作线程中通信，每半秒请求状态，界面主循环先接收已校验快照再完成对应 Future；断线先使状态失效，再拒绝未完成请求。过期结果、服务实例变化、无效原生能力声明都拒绝发布，重连不重放写入。UI 客户端不能提交显示回执。`windows/session_client.py` 供共用 SessionRuntime 的两个工作线程使用独立连接，防止显示/状态请求占用续租通道；所有连接绑定同一服务实例，实例变化使其他在途结果失效。回执仍由服务核对预登记的确切会话进程，客户端不能自行取得此资格。

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

`windows/switch.py` 先核对唯一、已启动的 GPIO 指示器、系统驱动和内部 ACPI 总线，才读取 `SM_CONVERTIBLESLATEMODE`。不存在、歧义、设备替换或读取错误时返回未知状态；不写入该物理状态。当前仅识别已核对的 INT33D3 路径；CFQV8 笔记本姿态的交互用户与 SYSTEM 读取一致，SYSTEM 会话中的折叠变化和其他开关仍未验证。接口依据：[GetSystemMetrics](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-getsystemmetrics)。

`windows/display.py` 通过 CCD 识别唯一内屏，拒绝镜像或输出身份变化，复用 `session/transaction.py` 和显式恢复日志执行方向写入、回读与恢复。写入先用 `CDS_TEST` 验证，不持久化显示注册表配置。固定方向会在取得并回读全局方向锁后写入；`NativeDisplay.lock()` 在相关 shell 导出不可用或回读失败时拒绝确认。`windows/tablet_mode.py` 通过 ImmersiveShell COM 边界只读观察 Windows 桌面／平板模式；产品路径不调用强制 setter，旧版日志中的 shell 字段只作兼容读取并在恢复时丢弃。方向与方向锁保存在同一恢复日志，旧版日志缺少新增字段时仍兼容读取。`SetDisplayAutoRotationPreferences` 仅作用于当前进程，不能替代这里的系统方向锁；参见 [Microsoft API 文档](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-setdisplayautorotationpreferences)。

已授权的真机可显式运行以下检查，屏幕会短暂倒转。检查保留恢复日志，另启恢复进程，并验证写入进程被强制结束后的恢复；每次使用新的证据目录：

```bat
set PYTHONPATH=src
python tests\windows_display_smoke.py --exercise-rotation --directory build\display-evidence-new
```

2026-11-09，CFQV8-1L、Windows 10 专业版 19045 x64 上取得笔记本、帐篷、支架和完全折叠的真实采样：笔记本 z 约 -9.8 m/s²，帐篷 y 约 -9.3 m/s²，支架 z 约 +9.9 m/s²；物理开关在折叠/展开时按 1→0→1 变化。此证据确认当前采样的 y/z 方向与策略一致，不代表完整安装矩阵、持续会话控制或整机验收。2880×1920 内屏的倒转、回读、正常恢复和写入进程被结束后的日志恢复通过，最终方向与原方向一致。驱动反汇编和只读设备探测确认 `msgpiowin32` 接受一个字节时执行 toggle、读取返回当前字节，并发出 CSM 状态变化；设备仍处于独占时普通用户和 SYSTEM 都无法安全绕过该独占策略，因此通过可逆配置并要求重启。shell 选择为“不要切换”时保留桌面的结果属于 Windows 自己的策略，程序不调用强制 setter。

同机从 `C:\util2\drivers\SynTP_d20194747` 安装了签名有效、硬件 ID 精确匹配的厂商驱动；安装后为 Synaptics HID TouchPad / Synaptics HID Device 19.5.26.18，用户确认移动、点击、滚动正常。自研输入过滤驱动没有加载；用户选择保留启动安全配置，测试签名仍关闭。这些证据不提升 Windows 机型支持等级。

Windows 源码虚拟环境的 `python.exe/pythonw.exe` 可能是转发启动器。软件客户端直接持有实际解释器进程，并从当前源码/安装位置加载标准库后台，避免结束启动器后实际后台仍占用设置锁；重连仍不重放写入。

硬件通道独立于软件预览的私有标准管道，使用 `windows-hardware` JSON v1 信封和本地主机命名管道。服务端要求 LocalSystem，端点所有者也是 LocalSystem，拒绝远程客户端；交互用户只获读写权限，明确排除与 FILE_APPEND_DATA 共用数值的 FILE_CREATE_PIPE_INSTANCE。首实例独占端点名称，最多四个连接，每条消息至多 64 KiB，连接请求五秒超时。客户端核对端点所有者，不自动重放请求；服务实例和每连接递增请求序号用于拒绝旧请求。

身份来自管道模拟令牌与实际客户端进程令牌，必须匹配 SID、登录 LUID 和会话 ID，并核对 PID、进程创建时间、当前本地控制台以及未锁定的活跃会话。界面可以提交模式和偏好选择；只有由可信主机预先登记的确切会话进程才能提交显示回执，不能通过 RPC 自行注册 PID。身份检查放在独立有界后台队列，主循环继续推进引擎；一秒内未获得新的会话确认即失去资格，慢查询结果不能延长有效期。失联、退出、锁屏或用户切换通过共用 `Engine.revoke_session` 撤销对应所有者并走原有释放事务。它不改变 Linux 鉴权、租约时长或 D-Bus 方法。

接口依据：[Microsoft 命名管道权限](https://learn.microsoft.com/en-us/windows/win32/ipc/named-pipe-security-and-access-rights)、[WTS 会话状态](https://learn.microsoft.com/en-us/windows/win32/api/wtsapi32/ns-wtsapi32-wtsinfoex_level1_w)。

隔离通信检查为 `tests/windows_hardware_smoke.py`：仅在已获授权的 Windows 测试虚拟机中分别以 LocalSystem 和普通交互用户运行 server/client，显式传入同一临时 `--directory`。该 fixture 使用不支持硬件的共用引擎和空输入端口，不打开输入设备。2026-11-08 的原生检查已通过端点所有者、控制台令牌、拒绝不支持模式、拒绝未登记回执、拒绝客户端创建服务端实例及拒绝用户自建冒充端点六项检查，普通客户端未提权。模拟状态和此通信证据均不代表输入驱动验收。

2026-11-09，在上述 CFQV8 真机上，以受保护且校验过哈希的代码/运行时快照执行一次性 SYSTEM 任务：GPIO 笔记本状态与交互用户一致，WinRT 取得同一加速度计的五份新鲜样本，普通非提权客户端再次通过六项原生通信检查；快照没有发布支持或已应用模式。Session 0 无交互显示输出，显示查询返回不可用，未尝试显示写入。任务已结束并移除，没有保留提权助手或改变启动安全配置。

### Windows 原生会话开发验证

`windows/session_agent.py` 将原生会话观察、硬件客户端、共用 SessionRuntime 和 Windows 显示事务装配为独立的普通用户进程。它拒绝 SYSTEM、非交互和已提权令牌；必须先通过主机对确切进程身份的显示回执检查，才能创建显示后端或恢复日志事务。启动握手复用显示工作线程，不额外占用第三条 RPC 通道。普通 UI 不启动此进程；可信启动组件已加入，完整服务装配、安装及 SYSTEM 联合验证仍未完成。

开发入口为 `python -m panasonic_posture.windows.session_agent --journal <当前用户的恢复日志路径>`；必须由可信硬件主机预登记该确切进程，手动运行不能自行取得回执资格。缺少服务或未获登记时在显示后端构造之前退出，不能把命令能运行当作安装成功。

主机内部的会话进程替换操作不在 RPC 方法表中。替换前撤销旧所有者，清除身份缓存并退休所有在途管道鉴权；未完成的旧身份查询仍占用原有工作槽，结果不会用于新进程。这样可在不增加客户端权限的前提下为可信启动器更换确切进程。

`windows/trusted_runtime.py` 为安装后的主机/会话启动提供文件检查：按句柄检查所有者和有效 DACL，拒绝普通用户写入、重解析点、空 DACL、未支持的权限条目及遍历失败；保留禁止删除共享的路径句柄。解释器搜索路径须精确限制为安装目录中的标准库、DLL 和应用，不允许 `import site` 或用户路径。此要求独立于可搬移的软件预览包，不能直接把现有预览 ZIP 提权运行。当前源码目录会被拒绝作为特权运行时。

会话进程支持主机提供的 `--stop-event`，只打开并等待 SYSTEM 所有的随机事件，不取得设置事件的权限。信号到达时先执行共用运行时的显示恢复，再关闭事件句柄；名称冲突或普通用户冒充的事件会被拒绝。

`windows/session_launch.py` 只供可信主机调用：读取活跃本地控制台的用户令牌，必要时只接受同一 SID、登录实例和会话的非提权关联令牌，使用该用户的环境和 LocalAppData 日志路径。只启动已固定并校验的解释器与会话模块，不继承主机句柄；以挂起状态创建进程，绑定关闭即终止子进程的 Job，核对真实 PID、创建时间、令牌和控制台资格。放行前再次核对资格和停止事件。没有提供 RPC 命令、可执行路径、PID 或令牌参数入口；主机不代替用户写恢复日志。接口依据：[CreateProcessAsUserW](https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-createprocessasuserw)、[用户环境](https://learn.microsoft.com/en-us/windows/win32/api/userenv/nf-userenv-createenvironmentblock)、[用户目录的令牌权限](https://learn.microsoft.com/en-us/windows/win32/api/shlobj_core/nf-shlobj_core-shgetknownfolderpath)。

`windows/session_supervisor.py` 在主机循环中先登记确切进程，再安排工作线程放行；WTS、创建和资源清理占用一个有界工作槽，不阻塞引擎。控制台用户变化时先撤销旧角色、发出停止信号，等待最多五秒的显示恢复窗口，再清理旧进程，之后才能启动替代进程。过期创建结果、停止期间返回的进程和清理未完成的进程都不能被当作可用会话；主机停止须继续轮询到协调器确认关闭，不能只取消 Future 就报告清理成功。

上述启动和协调的单元测试覆盖结构布局、标志、句柄、令牌、登记/放行顺序、切换与退出。2026-11-09，CFQV8 / Windows 10 Pro 19045 上另以哈希固定、ACL 与所有者均验证的隔离运行时执行两轮真实 SYSTEM 任务：创建非提权控制台用户进程、挂起期间登记确切身份、放行与取得租约、停止前撤销租约、停止信号及进程回收全部通过。普通用户客户端不能提交显示回执或写入受保护运行目录；引擎始终不支持硬件且没有 applied 结果，没有打开输入或采样设备、加载驱动或改变启动安全配置。任务已自动移除，清理目录的 UAC 请求被取消，因此保留 `C:\Program Files\PostureManager.Validation.e19cebe6fe87` 的只读快照，无常驻测试助手。此结果不等同于完整硬件服务安装、换用户、锁屏交接或强制终止主机后的 Job 回收验收。

显式复现入口为 `tests/windows_launcher_smoke.py --prepare-runtime <新证据目录>`：普通用户生成标准库、应用与 fixture 的 SHA-256 清单，不提权、不启动设备。审阅并固定输出的 helper 哈希与清单哈希后，经 UAC 在管理员进程中执行 `tests/windows_launcher_admin.ps1 -Action Run -TaskName PostureManager.Validation.<12位小写十六进制> -Manifest <清单绝对路径> -ManifestHash <已固定哈希>`。管理员先逐文件复制并验证哈希，设置受保护 ACL 和可信所有者，再用安装解释器验证整个运行时；任务仅以 SYSTEM 运行无设备 fixture，最多两分钟并自动撤销任务。`ready.json` 出现后十八秒内，普通用户运行 `tests/windows_launcher_smoke.py --ordinary-instance <ready中的实例> --root <受保护目录>`，将结果保存在普通用户自己的证据目录。读取并保存结果后，`-Action Cleanup -TaskName <同一名称>` 仅删除经绝对路径、标记、任务已移除及无重解析点检查的精确目录。`RunStaged` 可在同一验证目录复测，仍先验证实际 ACL 和隔离解释器；这些脚本不更改全局 PowerShell 执行策略，不能通过临时放宽生产权限来运行。

`windows/session_context.py` 用独立的单工作线程读取本进程会话，结合本地活跃控制台、锁屏状态，以及 `GUID_SESSION_DISPLAY_STATUS` 的系统电源回调。电源上下文与控制资格分开：读数超过一秒、未知或查询失败会使控制资格失效，已知的单纯熄屏则不使其失效。睡眠使旧查询失效，恢复时重新注册电源观察，取得当前值。回调只更新有锁的状态，不执行显示、输入或租约操作。依据：[会话显示电源状态](https://learn.microsoft.com/en-us/windows/win32/power/power-setting-guids)、[注册后的初始电源回调](https://learn.microsoft.com/en-us/windows/win32/api/powersetting/nf-powersetting-powersettingregisternotification)。

控制资格失效时，适配器通过既有 `display-unavailable` 错误回执撤销软件输入，沿用共用运行时的方向恢复；不能只发无错误的未就绪心跳，因为引擎会为软件方向切换保留已有输入。独立通道维持允许续租的会话上下文，恢复时先等待旧未就绪心跳完成，再允许新的就绪回执。显示事务执行中失去资格会恢复并报告失败，即使恢复期间很快重新取得资格，也要重新完成事务。单纯熄屏不作为撤销条件。Linux 的熄屏直接姿态切换、通知和触摸亮屏仍须完整适配；不能将当前链路宣称为功能等价或直接放宽鉴权。

以下检查只观察，不锁屏、熄屏、休眠、唤醒或旋转；每次使用新的证据目录：

```bat
set PYTHONPATH=src
python tests\windows_session_smoke.py --observe-only --directory build\session-evidence-new
```

2026-11-09，CFQV8 / Windows 10 Pro 19045、普通非提权进程下已取得本地会话和当前显示关闭的真实回调，分别记录电源上下文及控制资格，退出后通知注册全部释放。另确认缺少硬件服务时，会话进程在构造显示后端之前退出。没有为了验证改变锁屏、电源或方向；解锁、亮屏和睡眠恢复的真实变化尚未主动验收。隔离测试覆盖这些变化、迟到查询、线程与回执顺序，以及共用 Engine 的输入释放，不能替代安装后的真实会话与内核输入验收。

同机的只读原生文件检查接受系统盘和 Program Files 的受保护祖先目录，拒绝用户拥有的源码目录作为特权运行时；普通用户临时创建的冒充停止事件也被实际拒绝。临时事件句柄已关闭，没有调整文件权限、安装服务、加载驱动或操作显示。

### Windows 便携预览包

在 Windows 的独立构建环境中，使用与预览依赖兼容的 64 位 Python 3.11+：

```bat
python -m pip install --target build\windows-dependencies -r packaging\windows\requirements-ui.txt
python packaging\windows\build.py --dependencies build\windows-dependencies
```

构建器从当前 Python 基础安装和独立依赖目录读取运行时、Qt/Fluent 及随包许可，连同源码、插画、翻译和维护说明直接流式写入 ZIP，同时计算逐文件校验，减少临时磁盘写入与内存占用。预览只使用 Qt Widgets，因此排除 PyQt6 自带但未使用的 QML 资源，避免 Windows 传统解压工具遗漏超长路径文件。完整归档生成后才替换已有包。`runtime/python._pth` 隔离用户站点、注册表和 `PYTHONPATH`；参见 [Python Windows 搜索路径说明](https://docs.python.org/3.11/using/windows.html#finding-modules)。基础安装的第三方包、测试数据或配置不进入归档。

输出为 `dist/windows/panasonic-posture-windows-preview.zip` 和 SHA-256 校验文件。解压后运行 `Native.cmd` 使用真实传感器、物理开关和内屏事务；普通 `Panasonic Posture Manager.cmd` 保持不接触硬件的软件后台预览，`Simulation.cmd` 启动后台模拟。ZIP 内 `MANIFEST.json` 记录逐文件校验值，第三方许可保留在 `runtime/Lib/site-packages/*.dist-info/`，Python 许可为 `runtime/LICENSE.txt`。更新时解压到新目录，用户设置继续位于 LocalAppData。

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
