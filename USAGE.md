# 安装、打包与运维

支持范围、模式说明和常见问题见 [README](README.md)。

## 安装

### 预构建包

下载适合当前系统的安装包和同一发布版本的 `SHA256SUMS`。在下载目录执行
`sha256sum --ignore-missing --check SHA256SUMS`，确认所下载的安装包通过校验。 [GitHub Releases](https://github.com/adogecheems/panasonic-posture-manager/releases)

以下以 2.3.3 为例。安装时只执行当前发行版对应的命令，并使用下载文件的实际名称。
Ubuntu／Debian 包要求 GTK 4.12+ 和 libadwaita 1.4+，包管理器会处理依赖。

```sh
# Arch Linux
sudo pacman -U ./panasonic-posture-manager-2.3.3-1-x86_64.pkg.tar.zst

# Ubuntu / Debian
sudo apt install ./panasonic-posture-manager_2.3.3-1_amd64.deb

# Fedora
sudo dnf install ./panasonic-posture-manager-2.3.3-1.x86_64.rpm
```

### AUR

`panasonic-posture-manager` 从发布标签构建，`panasonic-posture-manager-bin` 使用 GitHub Releases 的 Arch 安装包。
两个包互斥。以下以 `yay` 为例，任选一条命令。

```sh
yay -S panasonic-posture-manager
yay -S panasonic-posture-manager-bin
```

### 首次启动和升级

安装包会将用户服务设为随图形会话启动。安装或升级后，注销并重新登录即可加载新安装的会话适配器。
首次安装后若要立即使用，可在当前 Wayland 会话中运行以下命令。

```sh
systemctl --user start panasonic-posture-manager-session.service
```

## 排查

系统服务负责姿态决策和内置输入，用户服务负责当前桌面的显示与通知。先看两项服务的状态，再查看本次启动的日志。

```sh
systemctl status panasonic-posture-manager.service
systemctl --user status panasonic-posture-manager-session.service

journalctl -b -u panasonic-posture-manager.service
journalctl --user -b -u panasonic-posture-manager-session.service
```

Plasma 6 的显示适配器优先使用 `kscreenctl`，找不到时使用 `kscreen-doctor`。若日志提示两者均缺失，请检查发行版对应的 KScreen 软件包。

排查机型兼容性时，可运行 `panasonic-posture-manager-probe`。探针以 JSON 输出匹配的机型配置、输入设备候选和加速度计信息，只读取 sysfs 与配置文件。

## 构建和验收

从仓库根目录以普通用户运行完整检查。D-Bus 集成测试使用临时会话总线，传感器探测仍可能访问宿主 sysfs，
因此请在未映射真实传感器的隔离环境中运行。需要 Python 3.11+、C 编译器、Make、gettext、PyGObject 和 `dbus-run-session`。
UI 测试还需要 PyGObject、PyCairo、GTK 4、libadwaita 和 `gtk4-broadwayd`。

```sh
make WERROR=1 check
```

### 平台适配

`policy.py`、`controller.py` 和 `calibration.py` 处理姿态决策。`service/engine.py` 负责
目标与执行回执，`session/runtime.py` 负责会话内的应用和恢复。复用这些模块时，按
`service/ports.py` 和 `session/ports.py` 提供平台实现。传感器适配器需将数据换算到策略
使用的设备坐标系，以 m/s² 提供三轴采样，时间戳与传给引擎的采样时钟一致。

Linux 入口分别由 `service/application.py` 和 `session/agent.py` 装配。共用常量与错误码在
`contracts.py`，Linux 通信、设备和安装配置在 `linux_contract.py`；`protocol.py` 保留旧导入入口。
`session/failures.py` 只定义异常与失败类别，D-Bus 客户端在边界将鉴权拒绝、暂时通信故障和
服务不可用分类；共用运行时不解析平台错误名或错误详情。

服务宿主完成鉴权后，向引擎传入带命名空间的 `SessionOwner`。Linux 继续以已校验的 UID
表示持有者，原有总线名、活跃会话和单元归属检查不变。输入执行器的状态通过
`satisfies(target)` 提供执行证据；Linux 仍要求软件平板信号和内置输入抓取均成功，错误报告
不能确认模式。物理平板开关仍是独立的硬件观察值。

`SensorInfo` 区分采样就绪、后端标识、设备身份和错误，`DeviceInfo` 只传递机型身份与当前
平台的支持等级。Linux 设备匹配留在 `hardware/profile.py`，纯安装方向矩阵运算在
`mounting.py`。其他平台不可继承 Linux 的已验证等级。`state_codec.py` 继续严格解析现有
Linux 状态 v2；新的内部传感器信息不扩展该线协议。
系统服务与 C relay 使用内部协议 v4，必须从同一个构建安装。字节 `1` 请求软件接管，
`0` 将已有接管改为物理开关转发，两者续接原有三秒租约；退出物理平板后的显示交接
由 relay 独立限制为两秒。命令变化递增 32 位 `request`，服务只接受匹配的执行回执。
共用输入接口的 `follow_hardware()` 表达同一操作，不依赖 Linux 传输。

软件输入接管有效时，relay 可只读观察唯一的直接触摸屏；不抓取触摸屏，不处理或记录坐标。
`applied` 回执的 `touch_serial` 只表示接触开始。服务将其转换为两秒内有效的状态 v2
可选字段 `touch_wake`（旧快照缺少时为 null）。已就绪会话通过 `SessionWake(u t)`
携带当前 generation 和触摸令牌请求唤醒，沿用 `SessionUpdate` 的适配器身份验证；服务再次
核对当前会话所有者、有效租约、已应用目标和触摸新鲜度。该调用不续租、不改变目标或回执。
relay 的 `w` 命令也不续租，并独立核对两秒接触窗口、输入抓取和物理开关，每次接触最多
发送一次 `KEY_WAKEUP`。该事件由独立的仅含唤醒键的虚拟设备发送，原有平板开关设备保持
仅含开关。没有触摸屏、发现有歧义、设备断开和唤醒设备创建失败时保持唤醒不可用；不以
普通按键或电源键代替。挂起、释放和会话失效沿用原有撤销流程。隔离测试不代表真机验收。

显示事务必须显式注入 `DisplayJournal`，不能自行选择用户目录。设置与日志的纯格式校验
分别在 `service/settings_schema.py` 和 `session/journal_schema.py`，现有 Linux 文件适配器
保留原子写入、权限和损坏隔离。异常类型独立于本地化加载，新平台需提供适合其系统的持久化
实现，不能直接复用 Unix 文件权限操作。

新增平台仍需实现本地会话与权限校验、输入控制、显示读写和恢复日志。
`test_platform_boundaries.py` 禁止导入 Linux 适配、配置、翻译加载及文件日志模块，使用
非 Linux 的会话标识、传感器来源和没有软件平板信号的输入替身验证共用流程。
这些测试无需真实设备或桌面，不代表 Windows 真机支持；当前安装包仍面向 Linux。

界面共用层位于 `frontend/`，只依赖 Python 标准库。`ports.UIClient` 提供模式、设置、
提示确认、校准和重试操作；请求完成只代表方法成功返回，已应用结果仍来自服务快照。
`state.PostureModel` 根据已校验的快照生成插画姿态、提示原因和两秒延迟提示的截止时间。
调用方注入单调时间，并用自己的主循环安排截止时间回调；相同代次的重复快照不延长等待。
`actions.SettingsModel` 按字段串行合并设置写入，`ActionRunner` 处理动作去重与旧连接
结果隔离。断线或关闭页面时必须撤销计时器并使这些交互对象失效。

Linux GTK 页面通过 `ui/service.py` 将这些操作映射到既有 D-Bus 契约；页面不构造
传输参数。新增 PyQt Fluent 前端可以复用 `frontend/`，提供自己的控件、资源与翻译加载、
客户端和调度器。客户端必须在界面所属主循环派发状态和 Future 完成回调，并限制 I/O
等待时间；不能从工作线程直接更新交互对象或控件。状态校验、身份认证和错误分类仍由
平台边界负责，不得把 UI 客户端当成会话就绪回执的来源。`test_frontend.py` 禁止 GUI
和 Linux 配置导入，覆盖共用交互及失败路径。Qt 前端目前提供独立预览，Windows 后端尚未实现。

### Windows 界面预览

当前只提供从源码运行的 PyQt6 Fluent 界面，不包含 Windows 服务、传感器、输入驱动或
屏幕控制。Python 核心的运行时依赖保持为空，界面依赖在独立的预览环境安装。
在 Windows 仓库根目录的命令提示符中运行（Python 3.11+，64 位）：

```bat
py -3 -m venv .venv-windows
.venv-windows\Scripts\python -m pip install -r packaging\windows\requirements-ui.txt
packaging\windows\preview.cmd --language zh_CN
```

`preview.cmd` 明确启用 `--demo`。不带演示数据启动时使用：

```bat
set PYTHONPATH=src
.venv-windows\Scripts\python -m panasonic_posture.fluent --language zh_CN
```

命令支持 `--language en`；省略时按环境语言选择。预览从源码目录读取 `data/assets/`，
优先加载 `locale/` 中已编译的翻译；Windows 源码预览没有编译翻译时读取同一份
`po/zh_CN.po`，无需安装 GNU gettext。可用 `PANASONIC_POSTURE_DATA_DIR` 指定资源目录。
启动时使用新建窗口，不提供系统托盘常驻或 Windows 单实例管理。

演示客户端只生成内存快照，模式请求与延迟完成使用不同状态；代次改变、断线或退出会
丢弃旧的完成回调。窗口采用与 Linux 相近的两页底部导航，设置按自动模式、传感器、
模式选择、关于排列；主页仅显示姿态与必要反馈，演示说明收在底部。设置页使用
Fluent 的 `SettingCardGroup`、`SwitchSettingCard` 和 `SettingCard`，底部沿用
`SegmentedWidget` 的页面选择与按钮。Qt 样式表负责设置组外缘与导航选中背景的圆角，
文字换行后的行高随窗口宽度调整。
演示场景通过启动参数选择，例如 `preview.cmd --scenario prompt`；可选值为 `laptop`、
`prompt`、`hardware`、`slow`、`error`、`disconnected`。模块入口的 `--scenario` 必须与
`--demo` 一起使用。
关闭后丢弃所有演示设置；校准对话框也只演示操作，不读取真实传感器。
未来的 Windows 客户端通过 `frontend.ports.UIClient` 注入 `fluent.app.Window`，其鉴权、
状态验证、通信和实际设备操作仍需独立实现，不得将演示客户端用于生产控制。

依赖版本固定在 `packaging/windows/requirements-ui.txt`。PyQt6 和 Fluent Widgets 的
上游许可文件与声明需随未来安装包一并核对、分发；本仓库许可仍为 AGPL-3.0-or-later。
这不是已签名的 Windows 发布制品，未完成 Windows 真机验收。

可在独立环境中执行 Qt 检查：

```sh
PYTHONPATH=src QT_QPA_PLATFORM=offscreen python -m unittest discover -s tests -p test_fluent.py -v
```

该命令在 Linux 使用离屏 Qt 平台，不访问真实显示或设备。系统解释器未安装可选界面
依赖时，完整检查会明确跳过 Qt 控件测试，纯 Python 演示客户端测试仍执行。

### 发行版打包

按目标发行版使用对应工具链。Arch 的 `PKGBUILD` 取用 `pkgver` 对应的发布标签。
Deb 和 RPM 目标默认将安装包输出到 `dist/packages/`，RPM 从干净工作树的 `HEAD` 打包。 [发布工作流与构建依赖](.github/workflows/release.yml)

| 目标 | 构建命令 |
| --- | --- |
| Arch | `(cd packaging/aur && makepkg --cleanbuild)` |
| Ubuntu／Debian | `make package-deb` |
| Fedora | `make package-rpm` |

在 Arch 构建环境中，将以下示例路径替换为实际的 Arch 源码构建包路径。仅检查归档时使用 `PACKAGE_NO_INSTALL=1`。
去掉该参数会在隔离的 pacman 根目录中检查安装、重装和卸载，需要 root 或可用的 `unshare`。
同时传入 `PREVIOUS_PACKAGE` 可用旧包验证升级。

```sh
make check-package PACKAGE=/path/to/package.pkg.tar.zst PACKAGE_NO_INSTALL=1
```

发布前在目标设备验收自动和手动切换、内置输入恢复、休眠唤醒及外接屏布局。

## 发布

1. 使项目、运行时、UI、AUR 元数据、翻译文件头和 README 中的版本一致。运行
   `/usr/bin/python3 packaging/scripts/release_version.py --tag v2.3.3`，把示例标签替换为待发布标签。
2. 推送版本标签后，工作流从标签对应的提交构建 Arch、Deb 和 RPM 安装包，生成 `SHA256SUMS`，上传至 GitHub Release，再更新两个 AUR 包。
3. 如果需要为已有标签重建制品，可手动运行发布工作流并填写 `tag`。工作流检出派发时选定的提交，`tag` 决定更新哪个发布版本，同名附件会被替换。AUR 源码配方仍指向发布标签，二进制包使用此次上传的 Arch 安装包。

## 卸载与清理

先在使用该程序的图形会话中关闭应用并停止用户服务。适配器退出时会尝试恢复桌面设置。

```sh
systemctl --user stop panasonic-posture-manager-session.service
```

使用原包管理器卸载已安装的包。Arch 用户如果安装的是 AUR 预构建包，将下面的包名换为 `panasonic-posture-manager-bin`。

```sh
# Arch Linux
sudo pacman -Rns panasonic-posture-manager

# Ubuntu / Debian
sudo apt remove panasonic-posture-manager

# Fedora
sudo dnf remove panasonic-posture-manager
```

卸载后会保留系统设置和传感器校准（`/var/lib/panasonic-posture-manager/settings.json`），以及各用户的显示恢复目录
（`${XDG_STATE_HOME:-$HOME/.local/state}/panasonic-posture-manager/`）。
如需清除历史状态，先确认桌面设置已恢复，再删除相应目录。删除系统状态会清除保存的偏好和校准。
