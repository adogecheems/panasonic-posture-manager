# AGENTS.md — 开发手册

本文件面向在本仓库工作的 AI 助手和维护者。执行顺序为：运行环境限制、用户本轮指令、本文件、README、代码与测试。行为以代码和测试为准；发现文档过期时，应在同一任务中修正。

文档分工：

- `README.md` 面向普通用户，不写内部协议、服务和调试实现。
- `MAINTENANCE.md` 面向安装、排障、构建和发布维护者。
- `AGENTS.md` 只保留现行开发规则、边界和验证方法。

不在这里维护提交流水、历史测试数量、旧 SHA-256 或逐次真机日志。需要追溯时使用 Git 历史和提交正文。

## 1. 当前产品边界

- Linux 支持 x86_64 GNOME Wayland 与 KDE Plasma 6 Wayland。仓库为 Panasonic CFQV8、CFQV9、CFQV1 提供已验证机型配置；其他 360° 翻转本只有在硬件探测明确匹配时才可使用。
- Windows 支持 Windows 10/11 x64，当前只对 `Panasonic Corporation CFQV8-1L` 开放。正式安装使用每机器 MSI。
- 两个平台的手动模式都只有自动、笔记本、帐篷、支架。平板是物理状态，不是手动模式。
- Windows 保持内置键盘和触控板可用，不发布 shell 平板模式，不自动打开屏幕键盘，不加载自研输入过滤驱动。
- Windows 正式用户界面只连接已安装的 LocalSystem 硬件服务。服务不可用时显示断线；不得回退到模拟状态、测试客户端或 CSM 推断。
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
- 启动阶段只允许一次只读平板指示器/恢复记录基线。存在旧恢复记录或结果冲突时，用稳定笔记本/帐篷几何校验；五秒内仍不明确时使用非平板安全基线，不等待首个 ETW 事件。
- 运行期物理变化只来自 Intel HID Event Filter Event 115：`0xCC` 平板、`0xCD` 笔记本；`0xCA/0xCB` 不改变状态。
- `ACPI\INT33D3` 只用于向 Windows 发布软件帐篷/支架姿态，不能反推物理状态。所有写入、恢复和恢复记录清理必须先取得健康 ETW 门禁。
- 相同目标的 CSM 租约应直接复用；不同目标或真正释放时才恢复 `pending.original`。
- 平板指示器无法打开时可降级为仅显示模式；不得创建写入器、伪造 CSM 成功或触碰其恢复记录。

### 通信与会话

- 命名管道只接受通过令牌、登录实例、进程创建时间、本地控制台和会话状态校验的调用方。客户端不得创建服务端实例，也不能请求任意设备写入或命令。
- 只有可信主机预登记的精确会话进程可提交显示回执。界面不能自行取得回执资格。
- 会话进程使用非提权交互令牌，先证明回执资格，再创建显示和 CSM 后端。锁屏、用户切换或资格失效时撤销租约并恢复显示；单纯熄屏不等同于资格丢失。
- 服务端和客户端使用 overlapped I/O。鉴权和写回复不得阻塞唯一 owner loop。
- ETW 健康状态失效时立即撤销物理准入和当前会话租约，保留可恢复的显示日志。

### 显示、界面与启动

- Windows 显示只拥有内屏方向和全局自动旋转锁。没有活动内屏且有活动外屏时，可只读保留外屏拓扑；存在未恢复的内屏日志时，不得借此忽略旧所有权。
- 物理平板退出后先只读等待最多两秒，让 Windows 完成方向交接；窗口结束后再执行一次稳定目标事务。等待不得续期，也不能确认 `applied`。
- 软件笔记本、帐篷和支架先完成方向和旋转锁事务，再发布 CSM 姿态；CSM 返回后核对方向和锁，平台改写时只允许一次有界重发。
- UI 默认留在托盘，关闭窗口只隐藏；明确退出才停止后台并恢复。无托盘时必须保留可见退出路径。
- 姿态通知点击必须同时匹配已校验快照的实例、提示代次、姿态类型和可确认条件。匹配时直接确认且不打开主窗口；否则只打开界面。
- 「登录时启动」默认开启。首次正常启动写入当前用户的固定 Run 值并记录初始化标记；用户关闭后不得自动重开，冲突内容必须保留。
- 用户界面、会话进程和普通入口必须在访问用户状态前拒绝提权、远程会话和非本地控制台。
- 特权服务只使用受保护安装目录中的受限解释器和固定模块入口。源码目录和便携目录不能作为 LocalSystem 运行时。

## 6. 代码与测试地图

Python 路径以 `src/panasonic_posture/` 为起点，测试对应 `tests/test_*.py`。

| 改动 | 先读 | 目标测试 |
| --- | --- | --- |
| 姿态、阈值、迟滞、提示代次 | `policy.py`、`controller.py`、`presentation.py` | `test_policy`、`test_controller`、`test_presentation` |
| 目标、状态和服务事务 | `protocol.py`、`model.py`、`controller.py`、`service/engine.py` | `test_protocol`、`test_model`、`test_engine`、`test_application` |
| Linux 鉴权、会话和租约 | `service/access.py`、`service/daemon.py`、`session/agent.py` | `test_access`、`test_agent`、`make check-dbus` |
| 设置、校准、持久化 | `service/settings*.py`、`calibration.py`、`ui/settings.py` | `test_settings`、`test_calibration`、`test_ui_contract` |
| 传感器、机型配置和设备发现 | `service/sensor*.py`、`hardware/`、`data/profiles/` | `test_sensor`、`test_iio`、`test_sysfs`、`test_profile` |
| C relay 与内置输入 | `backend/`、`service/actuator.py` | `make check-c`、`test_actuator` |
| Linux 显示与恢复 | `session/{desktop,display,plasma,transaction,journal,runtime}.py` | `test_desktop`、`test_display*`、`test_journal`、`test_agent` |
| GTK/Fluent 界面 | `ui/`、`fluent/`、`frontend/` | `test_ui_contract`、`test_fluent`、`test_frontend`、`make check-i18n` |
| Windows 硬件组合与 RPC | `windows/{hardware_application,hardware_runtime,hardware_host,hardware_protocol,hardware_pipe,hardware_rpc,hardware_server}.py` | `test_windows_architecture`、`test_windows_hardware*` |
| Windows ETW 与启动基线 | `windows/{etw_switch,slate_bootstrap,machine_identity,machine_profile}.py` | `test_windows_etw_switch`、`test_windows_slate_bootstrap` |
| Windows 显示、CSM 和会话 | `windows/{display,screen_session,session_agent,session_display,session_client,session_context,convertible_slate,slate_journal}.py` | `test_windows_display`、`test_windows_screen_session`、`test_windows_session_*`、`test_windows_convertible_slate` |
| Windows 服务与可信启动 | `windows/{hardware_service,trusted_runtime,session_supervisor,session_launch,session_stop,security}.py` | `test_windows_hardware_service`、`test_windows_trusted_runtime`、`test_windows_session_*` |
| Windows 界面、托盘和自启动 | `windows/{activation,instance,startup,update_check}.py`、`fluent/` | `test_windows_startup`、`test_fluent` |
| Windows 打包与发布 | `packaging/windows/`、`.github/workflows/` | `test_windows_msi`、`test_windows_packaging`、`test_packaging_workflow`、`test_release` |
| Linux 打包 | `Makefile`、`packaging/`、`pyproject.toml` | `test_packaging_workflow`、`test_release`、`make check-package` |

## 7. 兼容契约

- 状态快照 v2：`contracts.py`。
- C relay v4、硬件 profile v1：`linux_contract.py`。
- 设置 v1：`service/settings_schema.py`。
- 显示恢复日志 v1：`session/journal_schema.py`。
- Windows 硬件 RPC 信封与请求/状态 schema：`windows/hardware_protocol.py`、`request_contract.py`、`state_contract.py`。

以下变化必须先取得用户明确授权：D-Bus 端点和方法、per-uid 名称、鉴权/polkit 语义、错误码、版本化载荷、租约或特权进程职责。修改后同步 Python、C、打包配置、所有调用方和契约测试。

用户可见名称：

- 英文：`Panasonic 2-in-1 Tablet Posture Management Tool`
- 中文：`松下二合一平板电脑姿态管理工具`

`panasonic-posture-manager`、`PanasonicPostureManager`、AUMID、Run 键和管道名是内部标识，不随显示名称变化。UI 文案同步英文 msgid、`po/zh_CN.po` 和契约测试。发布版本同步 `pyproject.toml`、`__version__`、README、AUR、翻译文件头和其他发布元数据。

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

以下操作必须由用户本轮明确授权：

- 安装或卸载其他软件，执行 `sudo`/`pkexec`，启停 systemd 单元，修改 D-Bus、polkit 或桌面设置。
- 旋转屏幕、发送通知、改变锁屏状态或执行其他宿主操作。
- 打开或写入 `/dev/input`、`/dev/uinput`，抓取输入，或运行目标机验收脚本。
- 修改鉴权、租约、per-uid 名称、特权职责或兼容契约。
- 修改发布元数据、tag 或 AUR；推送、建分支、PR、merge 和发布。
- 删除仓库文件、重命名公共文件、批量重写、引入或升级依赖。

禁止：

- 覆盖、回滚或清理用户改动；擅自暂存/取消暂存用户改动。
- 手工编辑 `backend/relay`、`locale/**/*.mo`、`build/`、`dist/`、`__pycache__` 等生成物。
- 为验证在开发宿主安装测试包、运行 `tests/manual/`，或用自动化测试冒充真机验收。
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
- 版本、README、AUR 或发布元数据改动至少运行 `test_release.py`。
- D-Bus 集成测试使用临时会话总线，应使用普通用户并在未映射真实传感器的隔离环境中运行。
- UI 测试因缺少 PyGObject、GTK 或离屏后端而跳过时，必须记录跳过项和残余风险。
- Arch 包先运行 `make check-package PACKAGE=... PACKAGE_NO_INSTALL=1`；安装/升级检查只在隔离环境执行。

Windows 目标：

```powershell
$env:PYTHONPATH = "src"
python -m compileall -q src/panasonic_posture/windows src/panasonic_posture/fluent
python -m unittest discover -s tests -p "test_windows*.py" -v
python -m unittest discover -s tests -p "test_session_refresh.py" -v
python -m unittest discover -s tests -p "test_optional_input.py" -v
python -m unittest discover -s tests -p "test_frontend.py" -v
python -m unittest discover -s tests -p "test_fluent.py" -v
python tests/check_i18n.py
```

交付时说明实际命令、结果、跳过项、失败和未验证范围。未执行真机验证时必须明确写出。

## 11. 实现约定

- Python 保持 3.11+ 兼容；纯逻辑不直接依赖 I/O、时钟或 D-Bus；边界使用类型标注、dataclass/`StrEnum` 和稳定错误码。
- C 使用 C11，并通过 `-Wall -Wextra -Werror`；保留字符设备打开与输入权限检查。
- Linux UI 使用 PyGObject、GTK 4/libadwaita；Windows UI 使用 PyQt6 和 Fluent Widgets。
- 测试在进程、总线、文件系统、时钟和设备边界注入 fake；临时文件使用 `TemporaryDirectory`。
- 翻译以英文源文案为 msgid，确保 `po/POTFILES.in` 覆盖源文件；`.mo` 由构建生成。
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
