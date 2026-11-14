# AGENTS.md — 开发手册

本文件面向在本仓库工作的 AI 助手和维护者。执行顺序为：运行环境限制、用户本轮指令、本文件、README、代码与测试。行为以代码和测试为准；发现文档过期时，应在同一任务中修正。

文档分工：

- `README.md` 面向普通用户，只保留第 1 节“当前产品边界”允许的内容。
- `MAINTENANCE.md` 面向安装、排障、构建和发布维护者。
- `AGENTS.md` 只保留现行开发规则、边界和验证方法。

开始任务时先按第 2 节检查工作树，再用第 6 节定位代码和测试。涉及接口、权限或宿主操作时，先核对第 7、9 节；交付前按第 10、12 节验证并提交。历史变更与验证记录见 Git 提交正文。

## 1. 当前产品边界

- 用户文档只呈现支持范围、安装、日常操作、用户可见结果和用户可执行的排障步骤。内部协议、服务与进程架构、硬件探测、日志、命令行、API 以及构建发布细节不得进入 `README.md`，应留在 `MAINTENANCE.md`、本手册或代码资源中。
- Linux 支持 x86_64 GNOME Wayland；KDE Plasma 6 Wayland 为实验性支持，尚未经过实机验证。仓库为 Panasonic CFQV8、CFQV9、CFQV1 提供已验证机型配置；其他 360° 翻转本只有在硬件探测明确匹配时才可使用。
- Windows 支持 Windows 10/11 x64。Panasonic CFQV8、CFQV9、CFQV1 全系使用明确机型配置；其他机型使用与 Linux 相同的通用能力探测，不得恢复整机白名单。正式安装使用每机器 MSI。
- 两个平台的手动模式都只有自动、笔记本、帐篷、支架。平板是物理状态，不是手动模式。
- Windows 保持内置键盘和触控板可用，不发布 shell 平板模式，不自动打开屏幕键盘，不加载自研输入过滤驱动。
- Windows 正式用户界面只连接已安装的 LocalSystem 硬件服务。服务不可用时显示断线；不得回退到模拟状态、测试客户端或 CSM 推断。
- Windows 正式构建、发布和兼容契约只覆盖每机器 MSI；便携 ZIP 不属于发布产物或支持边界。
- 已删除的 `--native`、`--native-backend`、`--hardware-backend`、`--simulate-backend`、`--demo`、`--backend`、`--scenario` 和 `--settings` 入口不得恢复。

## 2. 工作流程

1. 先运行 `git status --short --untracked-files=all`，查看目标文件的 `git diff`。现有改动默认属于用户。
2. 用 `rg` 定位入口、调用方、测试和相关文档，确认谁做决定、哪个进程执行、如何确认成功。
3. 先运行目标测试记录基线。行为变更前阅读相关系统不变量；不要用旧设计日志中的历史实现替代当前代码。
4. 先复现问题或说明根因，再修根因。行为变化先补回归测试，覆盖正常路径、失败路径和安全边界。
5. 先跑目标测试，再按第 10 节扩大验证。禁止通过吞异常、跳过测试、放宽断言或删除分支来制造通过。
6. 同步更新受影响文档。设计变化应改写现行条款，不追加互相矛盾的长篇时间线。
7. 检查 diff、生成物和工作树，按第 12 节提交本轮改动。

## 3. 共用架构

- `policy.py` 判断姿态，`controller.py` 生成目标和代次，`service/engine.py` 管理租约与执行。`target` 是期望状态，`applied` 只能由匹配代次、租约和会话回执确认。
- 硬件物理姿态优先。传感器、租约或执行器失效时，必须撤销软件接管并恢复桌面原生状态。
- 显示写入统一经过 `session/transaction.py`：保留所有权检查、写前日志、回读、回滚和崩溃恢复。
- 平台适配实现 `service/ports.py`、`session/ports.py` 和 `frontend/ports.py`，不得用平台特例绕开共用事务。
- `frontend/` 只依赖标准库；页面只读取经过校验的快照。断线后旧连接的结果必须失效，写入不得自动重放。

## 4. Linux 运行边界

- 系统服务以 root 身份持有系统 D-Bus 名，负责鉴权、姿态策略、租约、设备和状态发布。
- 只有 C relay 打开 `/dev/input` 和 `/dev/uinput`。软件接管时可抓取内置键盘和触控板，并可只读观察唯一直接触摸屏的接触开始；禁止记录坐标或键盘内容。
- 每个用户只运行一个会话适配器。它先取得 per-uid 总线名，再通过 `SessionUpdate` 上报准备状态并续租，通过显示事务处理旋转、方向锁和恢复。
- 登录、锁屏和熄屏使用既有的无人值守和电源上下文路径。平台不支持输入控制时，目标必须如实声明 `disable_internal_input=False`，不得伪造执行器回执。
- 界面只显示已知稳定错误码对应的本地化文案；traceback、自由文本和内部细节只进入有界诊断日志。

## 5. Windows 运行边界

### 硬件服务

- `HardwareRuntime` 以 LocalSystem 运行。启动顺序为 ETW 物理源、注入查询的共用 Engine、WinRT 传感器、RPC 端点和可信会话监管器。
- Windows 机型 profile 只选择已知设备与朝向语义，不授予硬件访问；未命中 profile 时回退到 `generic/compatible`，由实际传感器、ETW、CSM 和显示端口决定可用能力。
- 明确 QV8/QV9/QV1 profile 只允许一次只读平板指示器/恢复记录基线。存在旧恢复记录或结果冲突时，用稳定笔记本/帐篷几何校验；五秒内仍不明确时使用非平板安全基线，不等待首个 ETW 事件。通用能力探测机型必须先收到一次合法 ETW Event 115 才开放物理准入，不得用超时伪造成健康状态。
- 运行期物理变化只来自 Intel HID Event Filter Event 115：`0xCC` 平板、`0xCD` 笔记本；`0xCA/0xCB` 不改变状态。
- `ACPI\INT33D3` 按 ACPI 硬件 ID 唯一发现，不绑定固定实例后缀，只用于向 Windows 发布软件帐篷/支架姿态，不能反推物理状态。所有写入、恢复和恢复记录清理必须先取得健康 ETW 门禁。
- 相同目标的 CSM 租约应直接复用；不同目标或真正释放时才恢复 `pending.original`。
- 平板指示器无法打开时可降级为仅显示模式；不得创建写入器、伪造 CSM 成功或触碰其恢复记录。

### 通信与会话

- 命名管道只接受通过令牌、登录实例、进程创建时间、本地控制台和会话状态校验的调用方。客户端不得创建服务端实例，也不能请求任意设备写入或命令。
- 只有可信主机预登记的精确会话进程可提交显示回执。界面不能自行取得回执资格。
- 会话进程使用非提权交互令牌，先证明回执资格，再创建显示和 CSM 后端。锁屏、用户切换或资格失效时撤销租约并恢复显示；单纯熄屏不等同于资格丢失。
- 锁屏、睡眠和休眠都是姿态事务边界。服务 owner loop 发现时钟跳变、或授权/物理准入首次失效时，必须清除旧代次、租约和手动模式；恢复后自动模式只接受新的稳定姿态，稳定前不得发布临时笔记本目标。
- 服务端和客户端使用 overlapped I/O。鉴权和写回复不得阻塞唯一 owner loop。
- 会话启动的授权证明、显示恢复、后续显示写入和最终恢复/CSM 释放必须复用同一显示工作线程及其 RPC 通道；lease worker 使用另一通道，不得因主线程构造或恢复显示而耗尽有界通道。
- 会话的 lease 通道空闲时每约 3 秒发送一次轻量 `get_state` 保活，避免服务端空闲超时重置管道实例后在姿态切换瞬间产生瞬时断连。
- 硬件服务在已发布状态签名（代次、目标、`applied`、提示和物理状态等）变化时置位一个仅信号用途的全局命名事件（`windows/ipc/wake.py`，无数据、无权限放大）；界面观察器由此唤醒并立即经鉴权 RPC 取回经过校验的快照。事件不可用或丢失时，界面按 0.1 秒空闲刷新兜底，绝不以事件本身承载或确认状态。
- 会话资格只在快照瞬时过旧时失效（会话仍在前台且未锁屏）按瞬时故障处理：宽限期内不撤销就绪、不恢复显示、不释放 CSM 租约，也不向服务上报资格丢失。锁屏、挂起、控制台切换或观察器故障仍立即撤销。
- ETW 健康状态失效时立即撤销物理准入和当前会话租约，保留可恢复的显示日志。

### 显示、界面与启动

- Windows 显示只拥有内屏方向和全局自动旋转锁。没有活动内屏且有活动外屏时，可只读保留外屏拓扑；存在未恢复的内屏日志时，不得借此忽略旧所有权。
- 平板退出前不再预锁方向。真机日志证实 Windows 在释放 convertible-slate 指示器时会覆盖旋转锁并把面板转到桌面原生方向，预锁的方向无法存活，且预锁失败路径的回滚会额外产生一次可见转动。退出后只读等待最多两秒，等待期间每次补锁以阻止 Windows 覆盖锁，但不改方向；等待不因硬件状态短暂不可读而提前结束；目标稳定后一次写入方向。等待不得续期，也不能确认 `applied`。
- 物理平板接管显示时，不得把旧软件目标或启动值重新发布到 CSM。
- 已成功发布的 CSM 目标在显示回滚、平台改写或有限重试期间保留租约；只有会话资格丢失、CSM 通知自身失败、目标真正释放或进程退出时才恢复启动值。
- 软件帐篷和支架先发布 CSM 姿态并等待约 0.5 秒让 Windows 的响应落地，再一次性写入方向和旋转锁并核对；真机日志证明先写方向会导致每次切换两次可见转动。软件笔记本仍先完成方向和旋转锁事务，再发布 CSM。平台此后重新开启自动旋转时，由既有显示刷新线程按当前服务代次持续补锁；只有方向也被平台改写时才允许一次有界重发。
- UI 默认留在托盘，关闭窗口只隐藏；明确退出才停止后台并恢复。无托盘时必须保留可见退出路径。
- 姿态通知点击必须同时匹配已校验快照的实例、提示代次、姿态类型和可确认条件。匹配时直接确认且不打开主窗口；否则只打开界面。
- Windows 登录启动先建立只依赖 PyQt、硬件客户端和本地托盘资源的启动壳；完整 Fluent 页面在用户首次打开窗口时才加载。窗口未加载时的托盘通知仍须使用同一快照、代次和可确认条件，确认失败不得伪造成功。
- 「登录时启动」默认开启。首次正常启动写入当前用户的固定 Run 值并记录初始化标记；用户关闭后不得自动重开，冲突内容必须保留。
- 用户界面、会话进程和普通入口必须在访问用户状态前拒绝提权、远程会话和非本地控制台。
- 特权服务只使用受保护安装目录中的受限解释器和固定模块入口。源码目录和便携目录不能作为 LocalSystem 运行时。
- Windows MSI 为应用源码和纯 Python UI 依赖携带确定性 `unchecked-hash` 字节码；只读安装不得依赖运行期写 `__pycache__`。所有 `pythonw` 正常入口使用 `-B -I`，并使用与受保护运行时匹配的 Python 版本生成字节码。

## 6. 代码与测试地图

Python 路径以 `src/panasonic_posture/` 为起点，测试对应 `tests/test_*.py`。

### 6.1 平台路径归属

平台适配使用对称的 `linux/`、`windows/` 包；`service/`、`session/`、
`frontend/` 和顶层模型文件只保留两平台共享逻辑。修改前先按以下归属判断。

| 归属 | 路径 | 边界 |
| --- | --- | --- |
| 共享核心 | `model.py`、`contracts.py`、`policy.py`、`controller.py`、`presentation.py`、`calibration.py`、`mounting.py`、`machine_profile.py`、`tasks.py`、`product.py`、`localization.py`、`snapshot_codec.py`、`frontend/` | 不导入 `linux/` 或 `windows/`；平台入口负责提供身份、路径和 I/O |
| 共享服务 | `service/{engine,callbacks,ports,hardware_monitor,settings_schema,settings_store}.py` | 只依赖共享端口和核心；不得导入 Linux D-Bus、设备、显示或界面适配 |
| 共享会话 | `session/{runtime,ports,failures,transaction,journal_schema,journal_store}.py` | 由两个平台复用；不得导入 Linux 桌面适配或 Windows 实现 |
| Linux 特权服务 | `linux/service/{access,actuator,application,daemon,sensor,sensor_sources,settings}.py` | 可依赖共享层和 Linux 契约，不得导入 Windows 包 |
| Linux 硬件 | `linux/hardware/{iio,sysfs,probe,profile}.py` | Linux IIO、sysfs、DMI、profile 默认路径和设备探测；不得导入 Windows 包 |
| Linux 会话 | `linux/session/{agent,client,dbus_errors,desktop,display,errors,journal,lockstate,notify,plasma,screen_context}.py` | Linux D-Bus、Wayland/GNOME/KDE、默认状态目录和通知适配 |
| Linux 应用层 | `linux/app/` | 命令行入口；不得依赖 Windows |
| Linux 界面 | `linux/ui/` | PyGObject/GTK/libadwaita 界面 |
| Linux 契约 | `linux/contracts/` | 常量、协议投影、严格状态解码和 D-Bus XML；共享逻辑使用 `contracts.py`、`machine_profile.py` 和 `snapshot_codec.py` |
| Windows 公共层 | `windows/common/` | 安全身份、实例锁、诊断、预检、会话停止事件和通用操作映射；只能依赖共享层或 Windows common |
| Windows 应用层 | `windows/app/` | 启动、应用身份、本地化、资源和更新检查；不得依赖 Linux 或 Windows 服务入口 |
| Windows 硬件 | `windows/hardware/` | WinRT 传感器、ETW、CSM、输入、朝向、机型身份和设备探测 |
| Windows IPC | `windows/ipc/` | 命名管道、帧、请求/状态契约、RPC 和 UI 客户端；不得导入特权服务入口 |
| Windows 服务 | `windows/service/` | LocalSystem 组合根、SCM 入口、鉴权、可信运行时和会话监管 |
| Windows 会话 | `windows/session/` | 非提权会话进程、显示、资格校验和 CSM 通知；不得导入服务入口 |
| Windows 界面 | `windows/ui/` | PyQt6/Fluent Widgets、托盘、页面和本地资源 |
| Linux 原生后端 | `backend/linux/` | C relay、evdev、switch 和 uinput |
| Windows 原生后端 | `backend/windows/` | 原生输入控制和驱动辅助代码 |
| 数据与资源 | `data/profiles/` | 两平台共用的机型 profile |
| Linux 数据与资源 | `data/assets/linux/` | 姿态 SVG 和 Linux 图标 |
| Windows 数据与资源 | `data/assets/windows/` | `icons/` 应用与托盘图标、`branding/` 标题栏标志、`postures/` 姿态插图、`installer/` MSI 图片；安装负载只包含前三类 |
| 公共打包 | `packaging/common/` | 跨平台版本更新脚本 |
| Linux 打包 | `packaging/linux/` | AUR、Deb、RPM、systemd、D-Bus、polkit、desktop、模块加载、AUR CI 和 Linux 发布脚本 |
| Windows 打包 | `packaging/windows/` | 便携组装、MSI、WiX、硬件服务和 Windows 发布脚本 |

禁止在共享模块内用 `sys.platform` 选择服务职责；平台组合应由入口和适配器完成。
新增平台依赖必须同步更新 `test_platform_imports.py` 的双向边界断言。
`windows/` 根目录只保留 `__init__.py`；新增 Windows 模块必须归入上表职责域。
`linux/` 根目录只保留 `__init__.py`；新增 Linux 模块必须归入 `app/`、
`contracts/`、`hardware/`、`service/`、`session/` 或 `ui/`。
`backend/` 根目录只允许 `linux/` 和 `windows/`；`packaging/` 根目录只允许
`common/`、`linux/` 和 `windows/`。
Windows 域依赖由 `test_platform_imports.py` 强制：`app -> common`、
`hardware -> common/ipc`、`ipc -> common`、`service/session -> common/hardware/ipc`、
`ui -> app/common/ipc`；不得形成反向或跨层环。
Linux 域依赖同样由测试强制：`app -> ui`、`hardware/service -> contracts`
（service 另可依赖 hardware）、`session -> contracts`、`ui -> contracts/session`；
不得形成反向或跨层环。

### 6.2 按改动选择目标测试

| 改动 | 先读 | 目标测试 |
| --- | --- | --- |
| 姿态、阈值、迟滞、提示代次 | `policy.py`、`controller.py`、`presentation.py` | `test_policy`、`test_controller`、`test_presentation` |
| 目标、状态和服务事务 | `linux/contracts/protocol.py`、`model.py`、`controller.py`、`service/engine.py` | `test_linux_protocol`、`test_model`、`test_engine`、`test_linux_application` |
| Linux 鉴权、会话和租约 | `linux/service/access.py`、`linux/service/daemon.py`、`linux/session/agent.py` | `test_linux_access`、`test_linux_agent`、`make check-dbus` |
| 设置、校准、持久化 | `linux/service/settings*.py`、`service/settings*.py`、`calibration.py`、`linux/ui/settings.py` | `test_linux_settings`、`test_calibration`、`test_linux_ui_contract` |
| 传感器、机型配置和设备发现 | `linux/service/sensor*.py`、`linux/hardware/`、`machine_profile.py`、`data/profiles/` | `test_linux_sensor`、`test_linux_iio`、`test_linux_sysfs`、`test_linux_profile` |
| Linux C relay 与内置输入 | `backend/linux/`、`linux/service/actuator.py` | `make check-c`、`test_linux_actuator` |
| Windows 原生输入 | `backend/windows/`、`windows/hardware/actuator.py` | `test_windows_actuator` |
| Linux 显示与恢复 | `linux/session/{desktop,display,plasma}.py`、`session/{transaction,journal_store,runtime}.py`、`linux/session/journal.py` | `test_linux_desktop`、`test_linux_display*`、`test_journal`、`test_linux_agent` |
| 平台界面 | `linux/ui/`、`windows/ui/`、共享 `frontend/` | `test_linux_ui_contract`、`test_windows_ui`、`test_frontend`、`make check-i18n` |
| Windows 硬件组合与 RPC | `windows/service/{application,runtime,host,server}.py`、`windows/ipc/` | `test_windows_architecture`、`test_windows_hardware*` |
| Windows ETW 与启动基线 | `windows/hardware/{etw_switch,slate_bootstrap,machine_identity,machine_profile}.py` | `test_windows_etw_switch`、`test_windows_slate_bootstrap` |
| Windows 显示、CSM 和会话 | `windows/hardware/{convertible_slate,slate_journal}.py`、`windows/session/` | `test_windows_display`、`test_windows_screen_session`、`test_windows_session_*`、`test_windows_convertible_slate` |
| Windows 服务与可信启动 | `windows/service/{daemon,trusted_runtime,supervisor,launch}.py`、`windows/common/{identity,security,stop_event}.py` | `test_windows_hardware_service`、`test_windows_trusted_runtime`、`test_windows_session_*` |
| Windows 界面、托盘和自启动 | `windows/app/{activation,identity,startup,update_check}.py`、`windows/common/instance.py`、`windows/ui/` | `test_windows_startup`、`test_windows_ui` |
| Windows 打包与发布 | `packaging/windows/`、`.github/workflows/` | `test_windows_msi`、`test_windows_packaging`、`test_packaging_workflow`、`test_release` |
| 仓库/打包布局 | `backend/`、`packaging/`、`Makefile` | `test_repository_layout` |
| Linux 打包 | `Makefile`、`packaging/linux/`、`packaging/common/`、`pyproject.toml` | `test_packaging_workflow`、`test_release`、`make check-package` |

## 7. 兼容契约

- 状态快照 v2：`contracts.py`。
- C relay v4：`linux/contracts/`。
- 硬件 profile v1：`machine_profile.py`；Linux 默认查找位于 `linux/hardware/profile.py`。
- 设置 v1：`service/settings_schema.py`。
- 显示恢复日志 v1：`session/journal_schema.py`。
- Windows 硬件 RPC 信封与请求/状态 schema：`windows/ipc/{protocol,requests,state}.py`。

以下变化必须先取得用户明确授权：D-Bus 端点和方法、per-uid 名称、鉴权/polkit 语义、错误码、版本化载荷、租约或特权进程职责。修改后同步 Python、C、打包配置、所有调用方和契约测试。

用户可见名称：

- 英文：`Panasonic 2-in-1 Tablet Posture Management Tool`
- 中文：`松下二合一平板电脑姿态管理工具`

`panasonic-posture-manager`、`PanasonicPostureManager`、AUMID、Run 键和管道名是内部标识，不随显示名称变化。UI 文案同步英文 msgid、`po/zh_CN.po` 和契约测试。发布版本同步 `pyproject.toml`、`__version__`、README、AUR、翻译文件头和其他发布元数据。

运行期只支持英文和简体中文；`zh_CN`、`zh_Hans`、`zh_SG` 使用同一份目录，繁体中文及其他语言回退英文。Windows 正式发布同时提供 `zh-CN` 和 `en-US` MSI，二者同一版本共享 ProductCode，产物文件名必须包含 culture。测试安装的同一 build 跨 culture 共享 ProductCode，不同 build 使用不同 ProductCode；所有安装包始终共享 UpgradeCode，以便通过 major upgrade 替换新负载。

每次安装测试必须使用独立的 `build<N>` 编号，用户可见版本为 `<版本>-build<N>`，例如 `2.5.0-build1`；MSI 内部四段版本为 `2.5.0.1`。测试构建通过 `packaging/windows/build_msi.py --test-build` 自动取下一个编号或显式指定编号，不得伪装成正式版本；正式发布包不得带 `build` 后缀。

## 8. 系统不变量

1. 硬件状态优先，禁止伪造或从软件输出反推物理姿态。
2. 特权硬件操作留在系统服务或 relay，桌面操作留在非特权会话进程，UI 只使用状态/操作接口。
3. 目标与结果分离；过期回执、错误实例和失效租约不能确认成功。
4. 失败即撤销；传感器、租约、会话资格或执行失败时必须恢复方向、锁和输入。
5. 所有显示写入必须可恢复；等待平台交接不等于已应用。
6. 设备发现存在歧义时拒绝选择；机型 profile 只能收窄候选，不能绕过能力校验。
7. 设置和恢复日志使用原子写入与损坏隔离，不得静默覆盖损坏文件。
8. 测试可用 fixture 模拟系统边界，但不得用模拟结果声明真机验收通过。

## 9. 授权边界

无需额外授权：

- 读取仓库、历史和测试。
- 按用户请求修改仓库内文件。
- 运行静态检查、单元测试、隔离集成测试和构建。
- 使用临时目录和 fixture，避免触碰真实用户状态或设备。

项目更新任务的既定授权：用户说“更新”时，默认要求构建、验证并把最新安装包实际安装到本机，最后核对已安装版本。该授权只覆盖本项目正常更新所需的提权安装，不包括卸载、真机验收、直接操作设备或其他宿主操作。

用户明确要求“快速安装”或“测试安装且不要检查”时，授权范围是构建并安装带递增 `build<N>` 标识的本项目 MSI，不执行安装后的版本、服务、日志、启动或真机检查。该模式不得省略构建自身的必要校验，也不得把未执行的安装后检查表述为通过。

以下操作必须由用户本轮明确授权：

- 安装或卸载其他软件，执行 `sudo`/`pkexec`，启停 systemd 单元，修改 D-Bus、polkit 或桌面设置。
- 旋转屏幕、发送通知、改变锁屏状态或执行其他宿主操作。
- 打开或写入 `/dev/input`、`/dev/uinput`，抓取输入，或运行目标机验收脚本。
- 修改鉴权、租约、per-uid 名称、特权职责或兼容契约。
- 修改发布元数据、tag 或 AUR；推送、建分支、PR、merge 和发布。
- 删除仓库文件、重命名公共文件、批量重写、引入或升级依赖。

禁止：

- 覆盖、回滚或清理用户改动；擅自暂存/取消暂存用户改动。
- 手工编辑 `backend/linux/relay`、`locale/**/*.mo`、`build/`、`dist/`、`__pycache__` 等生成物。
- 为验证在开发宿主安装测试包（用户本轮明确要求快速测试安装时除外）、运行 `tests/manual/`，或用自动化测试冒充真机验收。
- 为测试放宽生产鉴权、把设备写入移入用户进程，或把 `--session-bus` 当作生产边界。
- 恢复 X11/xrandr/xinput 路径或增加隐式桌面回退。

## 10. 验证

Linux 默认使用 `/usr/bin/python3` 和标准库 `unittest`。先跑目标测试，再按改动范围扩大：

```sh
PYTHONPATH=src /usr/bin/python3 -m unittest discover -s tests -p 'test_policy.py' -v
make check-unit
make check-c
make check-dbus
make check-i18n
make WERROR=1 check
```

- C 改动至少运行 `make check-c`；涉及 relay 与会话联动时加 `make check-dbus`。
- UI 文案或错误码改动必须运行 `make check-i18n`。
- i18n 门禁同时校验运行时 source msgid、错误码映射、desktop、WiX 和 polkit 元数据；扩展这些资源时必须同步更新 `tests/check_i18n.py` 及其回归测试。
- 版本、README、AUR 或发布元数据改动至少运行 `test_release.py`。
- D-Bus 集成测试使用临时会话总线，应使用普通用户并在未映射真实传感器的隔离环境中运行。
- UI 测试因缺少 PyGObject、GTK 或离屏后端而跳过时，必须记录跳过项和残余风险。
- Arch 包先运行 `make check-package PACKAGE=... PACKAGE_NO_INSTALL=1`；安装/升级检查只在隔离环境执行。

Windows 目标：

```powershell
$env:PYTHONPATH = "src"
python -m compileall -q src/panasonic_posture/windows
python -m unittest discover -s tests -p "test_windows*.py" -v
python -m unittest discover -s tests -p "test_session_refresh.py" -v
python -m unittest discover -s tests -p "test_frontend.py" -v
python -m unittest tests.test_localization tests.test_i18n_check -v
python tests/check_i18n.py
```

用户明确要求快速测试安装时，必须用 `build_msi.py --test-build` 生成带 `build<N>` 的测试 MSI，只需确认构建和安装命令成功；安装后检查可以省略，并在交付中明确写出未检查范围。

交付时说明实际命令、结果、跳过项、失败和未验证范围。未执行真机验证时必须明确写出。

## 11. 实现约定

- Python 保持 3.11+ 兼容；纯逻辑不直接依赖 I/O、时钟或 D-Bus；边界使用类型标注、dataclass/`StrEnum` 和稳定错误码。
- C 使用 C11，并通过 `-Wall -Wextra -Werror`；保留字符设备打开与输入权限检查。
- Linux UI 使用 PyGObject、GTK 4/libadwaita；Windows UI 使用 PyQt6 和 Fluent Widgets。
- 测试在进程、总线、文件系统、时钟和设备边界注入 fake；临时文件使用 `TemporaryDirectory`。
- 产品行为测试必须调用生产对象；fixture 自测不能替代或计作产品行为覆盖。共享规则集中在共享层测试，平台测试保留适配和集成差异。
- 相同流程、仅输入或结果不同的案例优先使用带名称的 `subTest`；独立状态场景必须重新建立对象，避免前一个案例掩盖后一个。
- 纯逻辑和资源测试不依赖 GUI。命令行参数矩阵直接调用入口，保留必要的独立进程冒烟检查；真实控件、导入隔离和进程生命周期仍在隔离进程验证。
- 翻译以英文源文案为 msgid，确保 `po/POTFILES.in` 覆盖源文件；`.mo` 由构建生成。
- XML、D-Bus 接口和 WiX 描述只放在独立资源文件中；需要动态生成时使用标准 XML API，禁止在 Python、C、脚本或测试代码中内联 XML 文档。
- 注释解释原因、不变量和失败路径；不复述代码。
- 文档只保留当前有效信息。设计或行为变化时直接更新对应条款，并在提交正文记录动机和验证；不新建按日期的流水账。

## 12. 完成与提交

完成前确认：

- [ ] 根因已验证，改动限于本轮任务，用户已有改动保持原样。
- [ ] 行为变化有回归测试，覆盖失败和安全边界。
- [ ] 目标测试及适用的完整检查已执行，并区分通过、失败、跳过和未验证。
- [ ] 兼容契约、版本、翻译、打包和文档联动已核对。
- [ ] 已运行 `git diff --check`、`git diff --stat` 和 `git status --short --untracked-files=all`。

Git 规则：

- 完成并验证后，本轮改动应单独提交；用户明确要求不提交或环境禁止提交时除外。
- 只暂存本轮文件或可安全分离的 hunk。存在其他改动时禁止 `git add .`、`git commit -a` 或等价操作。
- 提交前检查 `git diff --cached` 和 `git diff --cached --check`，排除生成物、缓存、密钥和无关文件。
- 使用简洁的英文 Conventional Commit 主题，如 `docs:`、`fix:`；正文说明改动和验证结果。
- 禁止 amend、改写已发布 tag 或直接修改 `.git/`。

## 13. 必须停止并询问

- 缺少第 9 节要求的明确授权。
- 用户改动与本轮 hunk 无法安全分离。
- 必需验证依赖真实硬件、特权操作或缺失工具链，且没有安全替代方案。
- 代码、测试与文档存在无法消解的冲突，或不同解释会显著改变范围、兼容性或造成不可逆结果。

命名和局部实现风格可按现有模式自行决定；有实质影响的假设应在交付时说明。
