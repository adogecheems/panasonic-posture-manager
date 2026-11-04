# AGENTS.md — Agent 执行手册

适用于在本仓库中工作的 AI coding agent。用户本轮明确指令优先，其次是本文件、
`README.md` 和代码中的历史注释。上层运行环境的限制更严格时，从其限制。
当前行为以代码和测试为准。发现文档与实现不符时，同步修正文档。

## 1. 工作流程

1. 用 `git status --short --untracked-files=all` 和目标文件的 `git diff` 确认已有改动归属。
2. 用 `rg` 定位入口、符号和测试。编辑前追踪「入口 → 决策 → 副作用 → 状态回执 → 测试」，
   明确决策模块、执行进程、兼容契约和可验证的结果。纯文档任务核对对应的代码、测试、命令和链接。
3. 改变设计前读[设计变更记录](#13-设计变更记录)。先运行最近的测试建立基线，区分既有失败与本轮问题。
4. 先复现问题或解释失败机制，再修复根因。复用现有事务和抽象，只改任务所需文件。
   行为变更先写能复现问题的测试，覆盖正常、失败和有安全意义的边界。
5. 先跑目标检查，再按[验证要求](#8-验证)扩大范围。检查差异和生成物，按[提交规则](#11-git-与提交)交付。

## 2. 项目和文档范围

- 用户可见行为、机型和桌面支持范围写入 [README.md](README.md)。安装、构建、发布和运维写入 [USAGE.md](USAGE.md)。
  用户文档保留这两页。不要恢复旧的 `docs/`、`CHANGELOG` 或独立硬件验收文档。
- 机型支持等级核对 `data/profiles/` 和运行时 probe。其他 360° 翻转本按硬件能力判断兼容性。
  支持 x86_64 Linux 上的 GNOME Wayland 和 KDE Plasma 6 Wayland。
- 使用 Python 3.11+、C11 relay、GJS／GTK 4／libadwaita。Python 包的运行时依赖列表保持为空，PyGObject 由发行版提供。

## 3. 运行边界与状态回执

- **root 系统服务**拥有系统 D-Bus 名。`service/daemon.py` 验证、分发方法调用并发布状态，
  `service/engine.py` 编排领域状态、租约和执行事务。传感器和硬件观察结果经 `policy.py` 决策，
  由 `controller.py` 生成 `Target` 和 generation。
- **C relay** 由 `service/actuator.py` 管理，是唯一可打开 `/dev/input`、`/dev/uinput`
  和抓取内置键盘、触控板的组件。stdout 专用于 JSON 行协议，日志写 stderr。
- **用户会话适配器**先取得系统总线上的 per-uid 名，再操作显示。同一用户只运行一个实例。
  `session/runtime.py` 读取 `GetState`，通过显示事务处理旋转、方向锁和恢复，再用
  `SessionUpdate` 上报该 generation 的准备状态并维护租约。系统服务综合会话与 relay 的执行结果，
  经 `GetState`／`StateChanged` 发布确认状态。
- **UI** 只通过 D-Bus 发起操作并渲染已校验的状态快照。**只读探针**仅查看 sysfs 和 profile，
  禁止打开输入设备或写入内核。

## 4. 定位代码与测试

Python 路径以 `src/panasonic_posture/` 为起点，`test_*` 对应 `tests/test_*.py`。
按改动选择相关行，再跟踪实际调用方。

| 改动 | 先读 | 目标验证 |
| --- | --- | --- |
| 姿态、阈值、迟滞、提示 epoch | `policy.py`、`controller.py`、`presentation.py` | `test_policy`、`test_controller`、`test_presentation` |
| 手动模式、目标、状态及服务事务 | `protocol.py`、`model.py`、`controller.py`、`service/engine.py`；装配与方法入口见 `service/application.py`、`service/daemon.py` | `test_protocol`、`test_model`、`test_engine`、`test_application`、`test_ui_contract` |
| 鉴权、会话身份、租约 | `service/access.py`、`service/daemon.py`、`service/engine.py`、`session/agent.py`、`packaging/dbus/`、`packaging/polkit/` | `test_access`、`test_engine`、`test_agent`、`make check-dbus` |
| 设置、校准 | `service/settings.py`、`service/engine.py`、`calibration.py`、`protocol.py`、`data/ui/settingsPage.js` | `test_settings`、`test_calibration`、`test_engine`、`test_ui_contract`、`make check-i18n` |
| 传感器、IIO、sysfs、profile | `service/sensor*.py`、`service/hardware_monitor.py`、`hardware/`、`data/profiles/` | `test_sensor`、`test_hardware_monitor`、`test_iio`、`test_sysfs`、`test_profile`、`test_calibration` |
| relay、物理开关、输入抓取 | `backend/`、`service/actuator.py`、`protocol.py` | `make check-c`、`test_actuator`、`test_sysfs`；涉及会话时加 `make check-dbus` |
| 显示、恢复日志、桌面检测 | `session/{desktop,display,plasma,transaction,journal,runtime}.py` | `test_desktop`、`test_display`、`test_display_plasma`、`test_journal`、`test_agent` |
| 通知、提示交互、锁屏 | `session/{notify,lockstate,runtime,agent}.py`、`policy.py` | `test_notify`、`test_lockstate`、`test_agent`；提示语义变化加策略和 UI 契约测试 |
| UI 布局、文案、插画、设置页 | `data/ui/`、`data/assets/`、`model.py` 的 `StateSnapshot` | `test_ui_contract`、`make check-i18n` |
| 打包、systemd、发布 | `Makefile`、`packaging/`、`pyproject.toml`、`.github/workflows/release.yml` | `test_packaging_workflow`、`test_release`；构建 Arch 包后运行 `make check-package` |

## 5. 兼容契约与联动检查

- D-Bus 端点和方法、per-uid 总线名、权限／polkit 语义、错误码及版本化载荷属于兼容契约。
  修改前必须取得明确授权，按实际调用方同步 Python、C、GJS、打包配置和契约测试。
- 从定义确认版本。`protocol.py` 定义状态快照 v2、relay 协议 v2、profile v1。
  `service/settings.py` 和 `session/journal.py` 分别定义设置 v1、显示日志 v1。
  不兼容变更须同步版本、迁移和调用方。
- 用户可见行为变更同步 README。文案同步英文源文案、`po/zh_CN.po` 和 UI 契约。
  错误码同步 `protocol.py`、`session/errors.py`、`data/ui/errors.js`。界面只展示稳定错误码对应的本地化文案，内部详情用于日志诊断。
- 发布版本核对 `pyproject.toml`、`__version__`、UI `APP_VERSION`、`packaging/aur/PKGBUILD`、
  `packaging/aur/.SRCINFO`、`po/zh_CN.po` 和 README。由 `packaging/scripts/release_version.py`
  和 `tests/test_release.py` 校验。

## 6. 系统不变量

1. **硬件优先。** 物理平板开关开启时，软件模式不得覆盖；禁止伪造硬件状态。
2. **权限隔离。** 特权操作留在系统服务与 relay，桌面操作留在非特权会话适配器，UI 只调用 D-Bus。
3. **目标和结果分离。** `target` 表示期望状态。事务只有在收到匹配 generation 的会话就绪回执、
   租约有效且 relay 的相应操作成功后才可确认 `applied`。过期回执必须忽略。
4. **失效即撤销。** 传感器失效、租约丢失或执行失败时，释放软件接管并恢复桌面原生状态，
   避免遗留输入抓取、方向锁或未完成的显示事务。沿用现有的重试和恢复路径。
5. **显示事务化。** 所有显示写入经 `session/transaction.py`，保留所有权检查、恢复日志、回读、回滚及崩溃恢复。
6. **设备发现拒绝歧义。** 排除 relay 创建的虚拟开关。物理开关、内置输入和加速度计有歧义时拒绝选择。
   Profile 只能收窄候选，不能绕开能力和总线校验。
7. **持久化安全。** 设置和显示日志复用 `SettingsStore` 与 `Journal` 的原子写入与损坏隔离。
   损坏文件移至 `.corrupt-<timestamp>` 后缀路径。

## 7. 授权边界

### 可直接执行

读取仓库、历史和测试。按用户请求修改仓库内文件，运行安全检查、单元测试、隔离集成测试和构建。
用临时目录与 fixture 验证，避免读写真正的用户状态。

### 须由用户本轮明确授权

- 安装或卸载软件、`sudo`／`pkexec`、启停 systemd 单元、修改 D-Bus／polkit／桌面设置、
  旋转屏幕、发送通知或改变锁屏状态等宿主操作。
- 接触真实设备，包括打开或写入 `/dev/input`、`/dev/uinput`，抓取输入，或在目标机运行验收脚本。
- 修改鉴权、租约、per-uid 名称、特权进程职责或兼容契约。提升机型支持等级或声明真机验收通过还需要对应证据。
- 修改发布元数据、tag 或 AUR 文件，或执行推送、建分支、PR、merge 和发布。
- 删除仓库文件、重命名公共文件、批量重写、引入或升级依赖。

### 禁止

- 覆盖、回滚、清理用户改动，或暂存／取消暂存其改动。提交须遵守第 11 节。
- 将 `.backup/`、`.review/`、缓存或构建产物当作源码。禁止手工编辑 `backend/relay`、
  `locale/**/*.mo`、`build/`、`dist/`、`__pycache__/` 等生成物。
- 在开发宿主安装验收包或运行 `tests/manual/`；用自动化测试冒充真机验收。
- 为测试放宽生产授权，将 `--session-bus` 用作生产边界，或将设备写入移入用户进程。
- 恢复 X11／xrandr／xinput 路径、添加隐式桌面回退或把未支持环境宣称为可用。

## 8. 验证

使用默认解释器 `/usr/bin/python3` 和标准库 `unittest`。从第 4 节选择目标测试。
代码改动交付前至少尝试完整门槛。

```sh
PYTHONPATH=src /usr/bin/python3 -m unittest discover -s tests -p 'test_policy.py' -v
make check-unit
make check-c
make check-dbus
make check-i18n
make WERROR=1 check
```

- C 改动必跑 `make check-c`，涉及 relay 和会话联动时加 `make check-dbus`。
  UI 文案或错误码改动必跑 `make check-i18n`。
- D-Bus 集成测试使用临时会话总线，也会启动传感器探测。使用普通用户，在未映射真实传感器的隔离环境中运行。
- UI 测试可能因缺少 `gjs` 或 `gtk4-broadwayd` 等依赖而跳过。涉及 UI 时说明跳过项和残余风险。
- Arch 包验收用 `make check-package PACKAGE=/path/to/package.pkg.tar.zst`。
  静态检查加 `PACKAGE_NO_INSTALL=1`。完整检查只在隔离 pacman 根目录中安装，详见 [USAGE.md](USAGE.md#构建和验收)。
- 修复失败根因，禁止吞异常、跳过测试、放宽断言、缩短超时或删除分支来让检查通过。
  无法执行时记录命令、错误和未覆盖范围。未做真机验证时明确说明。

## 9. 实现约定

- **Python** 保持 3.11+ 兼容。纯逻辑无 I/O、时钟和 D-Bus。边界模块使用类型标注、现有
  dataclass／`StrEnum` 和错误码。D-Bus 回调执行有界工作，耗时任务交给现有调度。
- **C** 使用 C11 并通过 `-Wall -Wextra -Werror`。保留字符设备打开时的安全检查。
- **GJS** 沿用 ES module、四空格缩进及 `_()`。页面只读取校验过的快照字段，字段变更同步状态模型与契约测试。
- **测试** 在进程、总线、文件系统边界注入 fake，临时文件放入 `TemporaryDirectory`。
- **翻译** 以英文源文案为 msgid，确保 `po/POTFILES.in` 覆盖实际源文件。用构建流程生成 `.mo`。
- **注释** 说明原因、不变量或失败路径。

## 10. 完成定义

- [ ] 根因已核实，改动限于本轮任务，用户已有改动原样保留。
- [ ] 行为改动有回归测试，覆盖失败路径及安全边界。
- [ ] 目标检查和适用的完整检查已执行，已区分通过、失败、跳过及未验证项。
- [ ] 已核对兼容契约、版本、迁移、翻译、打包和文档联动；设计变更已记入第 13 节。
- [ ] 已执行 `git diff --check`、`git diff --stat` 和 `git status --short --untracked-files=all`。
- [ ] 已按第 11 节提交，或有明确原因无法提交。

用用户当前语言交付。说明改了什么、原因、涉及文件、实际验证和未验证项。未提交时说明原因和残留状态。

## 11. Git 与提交

- 开始时检查工作树和相关差异，默认现存改动属于用户。保留其内容和暂存状态。
- 完成并验证后，本轮改动须在交付前单独提交。用户明确要求不提交或上层环境禁止提交时除外。
  没有改动时不创建空提交。
- 只暂存本轮文件或 hunk。与用户改动混在同一文件时，仅暂存能安全分离的 hunk；无法分离时停止询问。
  有其他改动时禁止 `git add .`、`git commit -a` 或等价操作。
- 提交前检查 `git status`、`git diff --cached` 和 `git diff --cached --check`，
  排除生成物、缓存、密钥和无关文件。
- 使用简洁的英文 Conventional Commit 主题，如 `fix:`、`docs:`。正文写明改动和验证结果。
- 禁止 amend、改写已发布 tag 或直接修改 `.git/`。推送、建分支和打 tag 须符合第 7 节授权。

## 12. 必须停止并询问

- 第 7 节要求的明确授权缺失。沉默、时间压力或任务需要均不构成授权。
- 用户改动与本轮 hunk 无法安全分离。
- 必需验证依赖真实硬件、特权操作、缺失工具链或外部环境，且没有安全替代方案。
- 代码、测试与文档给出无法消解的冲突，或多种任务解释会显著改变范围、兼容性或导致不可逆结果。

可依现有模式解决的命名和样式选择自行处理，交付时说明有实质影响的假设。

## 13. 设计变更记录

以下记录是本仓库的设计决策历史。规则：

- 改变产品行为、系统架构、权限边界、协议/状态/持久化 schema、默认策略、UI 交互
  模型、兼容范围或发布语义时，必须在同一改动中追加记录。
- 每条至少包含：日期、变更内容、动机、影响的约束/不变量、需要同步的代码与测试。
- 新设计取代旧内容时，明确写出取代关系，并同步修改前文的对应条款，不能只追加
  一条互相矛盾的说明。
- 纯内部重构、格式化或无设计语义变化的依赖补丁无需新增记录；无法判断时按设计
  变更处理。

### 2026-11-04 - 精简发布与维护文档

- 变更：用户文档只保留 `README.md` 和 `packaging/aur/README.md`，移除更新日志、
  机型说明、验收记录和截图；发布版本校验不再要求更新日志条目。
- 动机：减少面向用户和维护者重复暴露的内部技术细节，降低文档同步负担。
- 影响：`pyproject.toml`、运行时版本和 AUR 元数据仍是发布版本的事实源；
  `README.md` 继续承担版本公告与支持范围，安装维护文档继续保留。
- 同步：`README.md`、`packaging/aur/README.md`、`release_version.py` 和发布测试。

### 2026-11-04 - 安装维护文档移至仓库根目录

- 变更：安装、打包与运维说明从 `packaging/aur/README.md` 移至 `USAGE.md`，
  `README.md` 的维护入口和发布输入检查同步改为新路径。
- 动机：让安装维护说明与用户总览并列，避免 AUR 子目录继续承担全项目维护文档
  的入口。
- 影响：取代“精简发布与维护文档”中的安装维护文档路径约束；仍只保留用户总览和
  安装维护两页文档，版本校验和发布语义不变。
- 同步：`README.md`、`USAGE.md`、`AGENTS.md`、`tests/test_release.py` 和 AUR
  打包注释。

### 2026-11-04 - 手动发布重建使用派发提交

- 变更：`workflow_dispatch` 手动发布改为构建派发时选定的提交，`tag` 输入只决定
  发布标签；`v*` 标签推送仍构建对应标签。Arch 容器脚本不再提前展开构建变量。
- 动机：允许在发布标签不可移动的前提下修复发布基础设施，并以同一版本号重建
  制品。
- 影响：正常标签发布不变；手动重建的 GitHub 安装包和 AUR 预构建包使用所选
  提交，发布标签和版本校验仍保持一致。AUR 源码包按版本标签取源码。
- 同步：`.github/workflows/release.yml`、`tests/test_packaging_workflow.py` 和
  本记录。

### 2026-11-04 - 帐篷模式总开关

- 变更：新增默认开启的 `tent_enabled` 设置。关闭后自动策略不再进入帐篷模式，
  已有帐篷接管立即撤销，手动帐篷选项同时禁用；设置通过 `SetTentEnabled`
  D-Bus 方法和状态快照对外提供。
- 动机：让不希望使用帐篷模式的用户可以彻底避免误触发，而不必依赖提示设置。
- 影响：`tent_enabled` 是向后兼容的状态字段，旧快照和旧设置缺少该字段时默认
  开启；设置文件 schema 仍为版本 1，不改变硬件优先和失效撤销约束。
- 同步：控制器、策略、状态模型、设置持久化、D-Bus、UI、翻译、README 和测试。

### 2026-11-04 - 主页优先显示已应用模式

- 变更：主页插画和标题优先读取 `applied.mode`，硬件平板映射为平板姿态；没有
  有效已应用模式时才回退到传感器识别的 `pose`。
- 动机：手动切换输出模式后，主页应立即反映实际生效的模式，而不是继续显示
  未变化的物理姿态。
- 影响：此项取代此前“主页只显示识别姿态”的显示规则；`target` 仍不直接用于
  展示，目标与结果分离的约束不变。
- 同步：`data/ui/posturePage.js` 和 UI 契约测试。

### 2026-11-04 - 手动模式过渡期同步主页插画

- 变更：手动选择已写入 `manual`、对应 `target` 一致，但新事务尚未确认
  `applied` 且没有错误时，主页插画和标题临时显示该手动模式；事务成功后仍以
  `applied` 为准，出现错误则立即回退到传感器识别的姿态。
- 动机：`SetMode` 会先清空 `applied`，原逻辑在事务期间回退到未变化的物理姿态，
  使用户看到的手动切换结果偶发停留在旧图标。
- 影响：补充“主页优先显示已应用模式”的过渡期规则；`target` 不作为展示来源，
  只用于确认当前 `manual` 选择已进入服务事务，目标与结果分离的约束不变。
- 同步：`data/ui/posturePage.js` 和 UI 契约测试。

### 2026-11-04 - 仅发布可确认的姿态提示

- 变更：策略新增“当前提示可确认”判定；控制器只有在硬件状态、稳定性和
  当前原始/平滑姿态仍满足目标确认区时，才把 `pending_notification` 和
  `pending_prompt` 对外发布。提示模式仍可在较宽的保持区内保留，设备回到确认区
  后以同一 epoch 重新出现。
- 动机：提示会在较宽保持区内持续存在，但确认只接受较窄的目标确认区。设备轻微
  偏出确认区时，通知仍可点击，服务端必然拒绝，随后又刷新同一提示，形成反复点按
  但模式无法切换的循环。
- 影响：`pending_notification`/`pending_prompt` 明确表示“现在可以执行”的提示，
  不再暴露无法成功的动作；内部保持区、目标与结果分离以及 D-Bus 状态 schema 均
  不变。
- 同步：`policy.py`、`controller.py`、策略/控制器测试和 README 使用说明。

### 2026-11-04 - 底部导航悬停反馈

- 变更：底部导航的未选中项在鼠标悬停时显示低透明度背景，不再把 hover 背景设为
  完全透明；选中态样式保持不变。
- 动机：此前的覆盖规则消除了悬停反馈，底部按钮在鼠标操作时缺少可点击提示。
- 影响：只改变悬停视觉反馈，不改变导航选择语义、布局或 D-Bus/状态契约。
- 同步：`data/ui/navigation.js` 和 UI 契约测试。

### 2026-11-04 - 显示恢复独立于目标代次

- 变更：GNOME 和 Plasma 共用同一套软件模式显示事务。恢复日志在旋转读回成功前
  同时保留上一已写入方向和本次写入意图。会话适配器独立跟踪显示是否需要恢复，
  后端故障或租约拒绝时发起恢复，临时失败后重试。
- 动机：切换期间发生写入失败、崩溃或服务端故障时，仍能恢复接管前的屏幕方向和
  方向锁。
- 影响：日志仍为版本 1，缺少可选的 `pending_transform` 字段时按旧记录恢复。
  恢复仅回退当前输出上与日志相符的旋转，继续遵守事务化显示和失效撤销约束。
- 同步：`session/transaction.py`、`session/journal.py`、`session/runtime.py`
  和显示、日志及会话测试。

### 2026-11-04 - 分离输入释放与模式确认

- 变更：笔记本和硬件平板目标在 relay 释放成功且收到同代次显示回执后确认，
  支持任意到达顺序。软件平板类目标仍需 relay 确认输入已接管。发布快照时，
  租约失效会清除已应用结果。
- 动机：避免迟到的显示回执丢失、未完成的接管提前确认，以及租约失效后状态残留。
- 影响：保持目标与结果分离，D-Bus 方法、状态版本和租约时序不变。
- 同步：`service/engine.py` 和服务事务测试。

### 2026-11-04 - 校验会话适配器的完整单元归属

- 变更：从 systemd 的 cgroup 记录匹配调用方 UID 对应的用户管理器及适配器
  单元名。管理器内外允许 systemd slice 路径，相似单元名和其他服务的委托子树
  不予接受。
- 动机：阻止同 UID 的其他单元仅凭名称包含适配器单元名通过身份检查。
- 影响：保留活跃本地 Wayland 会话、per-uid 总线名及现有租约检查。
- 同步：`service/access.py` 和会话身份测试。

### 2026-11-04 - Arch 发布制品使用检出提交的源码

- 变更：补全“手动发布重建使用派发提交”。Arch 任务以检出提交的源码归档构建
  GitHub 安装包，并用该安装包验证 AUR 预构建包。导出的 AUR 源码构建配方继续
  按版本标签获取源码。
- 动机：使同一轮发布的 Arch、Debian 和 RPM 安装包从同一提交构建。
- 影响：标签和版本校验沿用既有发布契约。
- 同步：`.github/workflows/release.yml` 和打包工作流测试。

### 2026-11-04 - 姿态处理故障撤销接管

- 变更：服务控制循环遇到异常时使原有模式回执失效，通过原有异步释放流程撤销输入接管。
  故障状态触发会话适配器恢复显示。重新接管需等待释放完成和新代次的显示回执。
- 动机：防止服务已报错时仍发布已应用模式，或继续依赖故障前的准备回执。
- 影响：沿用目标与结果分离、硬件优先和失效撤销约束，D-Bus 状态版本和租约时序不变。
- 同步：`service/engine.py`、服务事务测试和 README。

### 2026-11-04 - 按显示日志确认方向锁所有权

- 变更：恢复时比对当前方向锁或自动旋转策略与日志中的已写入值和待写入值。Plasma
  还要求输出身份匹配，GNOME 继续使用全局方向锁。接管期间被外部修改的设置，在
  下一次由程序写入时作为新的恢复基准。
- 动机：保留用户在接管期间修改的设置，同时覆盖方向锁写入前后发生崩溃的情况。
- 影响：日志仍为版本 1，`pending_lock` 为可选字段。旧日志按已记录的值判断，
  无法确认归属时保留当前桌面设置。
- 同步：`session/transaction.py`、`session/plasma.py`、`session/journal.py`、
  两种桌面的恢复测试、日志验证测试和 README。

### 2026-11-04 - 主循环异步等待鉴权

- 变更：界面操作和会话心跳使用各自有上限的串行鉴权队列，结果返回服务主循环后，
  才执行对应方法并回复。关闭服务时停止分发待处理的结果。
- 动机：防止慢的 logind 或 polkit 查询阻塞控制循环，也避免界面鉴权延迟占用会话心跳的队列。
- 影响：沿用现有鉴权检查、缓存、错误码与租约规则，状态更新继续由主循环串行处理。
- 同步：`service/daemon.py` 和鉴权分发测试。
