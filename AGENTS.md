# AGENTS.md — Agent 执行手册

- 适用对象：在本仓库中工作的 AI coding agent
- 更新时间：2026-11-04
- 文档性质：约束性执行说明

> 本文件的目标不是让 agent“知道一些规则”，而是让每个任务按同一套方法定位、
> 修改、验证和交付。优先级：用户本轮明确指令 > 本文件 > `README.md` >
> 代码注释中的历史说明。若上层运行环境的限制更严格，以更严格者为准。
>
> 涉及宿主系统、真实设备、用户数据、权限边界或发布时，授权不明确就停止询问；
> 沉默、时间压力或“任务需要”都不构成授权。

## 1. 基本工作原则

1. **先证明，再修改。** 不根据文件名、旧文档或猜测下结论。至少追踪一次
   “入口 → 决策 → 副作用 → 状态回执 → 测试”的完整路径。
2. **代码和测试是事实源。** `README.md` 只负责用户可见事实；本文件与代码冲突
   时以代码和测试为准，并修正本文件。不得复制可能过期的状态说明。
3. **修根因，不做表面补丁。** 先复现或解释失败机制，再改最小相关路径；不要用
   吞异常、放宽断言、缩短超时或删除分支来“让测试变绿”。
4. **最小增量。** 只改完成任务所需的文件。不顺手重构、批量格式化、升级依赖或
   改动无关文档；不清理用户的未提交改动。
5. **只写可证实的结论。** 区分“测试通过”“仅静态检查”“目标机已验收”；未执行
   就明确说未执行，不把 fake 后端、单元测试或 CI 结果说成真机证据。
6. **设计决定必须留痕。** 改变行为、架构、权限、兼容性或持久化语义时，在同一
   改动中更新第 13 节的设计变更记录。

## 2. 每轮标准流程

### 2.1 开工

1. 查看工作树，先确认哪些改动属于用户：

   ```sh
   git status --short --untracked-files=all
   git diff -- <目标文件>
   ```

2. 用 `rg` 定位符号、字符串和测试，不要靠目录遍历猜实现：

   ```sh
   rg -n 'SetTentEnabled|tent_enabled' src data tests
   rg -n '^class Engine|^    def tick' src tests
   ```

3. 在编辑前明确以下五项；缺一项就继续读代码：
   - 用户操作的入口是什么？
   - 哪个纯逻辑模块做决定？
   - 哪个进程执行副作用？
   - 哪个结构/版本是兼容契约？
   - 哪个测试能证明行为？
4. 需要改变设计时，先读第 13 节的既有记录，确认是在扩展还是取代旧约束。
5. 先运行最接近的测试建立基线；测试已失败时，先区分既有失败与本轮问题。

### 2.2 实施

- 优先复用现有抽象和事务路径，不复制一套“差不多”的逻辑。
- 纯逻辑模块保持无 I/O、无时钟、无 D-Bus；副作用放在原有边界内。
- 新增或修改行为时同步增加可失败的测试；测试应覆盖正常路径、失败路径以及有
  安全意义的边界。
- UI 只消费经过校验的状态，并通过 D-Bus 调用服务；不要在 UI 里直接旋转屏幕、
  抓取输入或写系统文件。
- 文案改动同时处理英文源文案、`po/zh_CN.po`、错误码映射和 UI 契约。
- 不手工编辑生成物：`backend/relay`、`locale/**/*.mo`、`build/`、`dist/`、
  `__pycache__/`。

### 2.3 收尾

1. 先跑目标测试，再跑完整门槛。
2. 检查差异和生成物：

   ```sh
   git diff --check
   git diff --stat
   git status --short --untracked-files=all
   ```

3. 逐项核对第 10 节的完成定义。
4. 按第 11 节提交；若环境不允许提交，明确报告未提交原因和残留状态。

## 3. 项目事实与运行时结构

### 3.1 项目事实

- 产品面向 Linux 可转换设备，重点支持 Panasonic CFQV8、CFQV9、CFQV1；其他
  360° 翻转设备只能按硬件能力兼容，不等于已完成真机验证。
- 只支持 x86_64 Linux 上的 GNOME Wayland 和 KDE Plasma 6 Wayland。X11、
  Plasma 5 Wayland 和其他合成器均为 `unsupported`。
- 技术栈是 Python 3.11+、C11 relay、GJS/GTK 4/libadwaita；Python 运行时依赖为
  零，PyGObject 由发行版提供。
- 支持范围、安装方式和用户可见行为以 `README.md` 为准；机型支持等级以
  `data/profiles/` 和运行时 probe 结果为准。
- 发布版本必须保持 `pyproject.toml`、运行时 `__version__`、UI `APP_VERSION`、
  `packaging/aur/PKGBUILD`、`.SRCINFO` 和 `README.md` 一致；由
  `tests/test_release.py` 和 `release_version.py` 守护。
- D-Bus、状态、relay、profile、设置和显示日志版本是内部兼容契约；不要从旧
  README 或注释推断当前 schema。

### 3.2 数据流

```text
传感器 / 硬件开关
  → service/sensor* + hardware_monitor
  → policy（纯姿态状态机）
  → controller（目标 Target + generation/回执事务）
  → service/engine（唯一修改领域状态的状态机）
  → service/actuator → backend/relay（root，唯一触碰 /dev/input、/dev/uinput）
  → SessionUpdate(generation, ready, ...)

session/agent（用户会话，per-uid D-Bus 名）
  → session/runtime（拉取 GetState，维护租约、显示重试和通知）
  → session/transaction → display/plasma（显示事务与恢复日志）

UI（GJS/libadwaita，用户会话）
  → D-Bus 方法调用 + StateChanged/GetState 快照
  → data/ui/* 渲染，不直接操作系统
```

关键边界：

- **root 系统服务**拥有系统 D-Bus 名、读取传感器、做姿态决策、启动 relay。
- **C relay** 是唯一打开 `/dev/input`、`/dev/uinput` 并抓取内置键盘/触控板的
  组件；relay 的 stdout 是 JSON 行协议，日志只能写 stderr。
- **用户会话适配器**负责显示旋转、方向锁、通知和租约；不得获取 root 权限。
- **UI** 只通过 D-Bus 与系统交互；不得变成第二个设备控制面。
- **只读探针**可以读 sysfs/profile，但不得打开输入设备或写入内核。

## 4. 仓库地图

| 路径 | 职责 | 修改时必须注意 |
| --- | --- | --- |
| `src/panasonic_posture/protocol.py` | D-Bus 名/路径/XML/方法、状态/relay/profile 版本、错误码、时序和安装路径 | 兼容契约的单一事实源；同步 Python、C、JS 和契约测试 |
| `src/panasonic_posture/model.py` | 枚举、`Target`、`StateSnapshot`、严格状态解析 | UI 只读取快照字段；字段变更同步 UI 契约测试 |
| `src/panasonic_posture/policy.py` | 纯姿态识别状态机、阈值、迟滞、提示 epoch | 不加入 I/O、时钟或 D-Bus；优先补 `test_policy.py` |
| `src/panasonic_posture/controller.py` | 策略结果转 `Target`，维护 generation/ack | 只有匹配 generation 的回执才能成为 `applied` |
| `src/panasonic_posture/service/application.py` | 服务组合根和生命周期依赖装配 | 依赖通过构造注入，便于无 root 测试 |
| `src/panasonic_posture/service/engine.py` | 服务端事务状态机、租约、重试与恢复 | 唯一修改领域状态；不要绕开现有事务顺序 |
| `src/panasonic_posture/service/daemon.py` | 系统 D-Bus 外壳、方法分发、GLib 生命周期 | 方法回调只做有界工作，耗时任务走既有调度 |
| `src/panasonic_posture/service/access.py` | active-session/uid/per-uid 名/polkit 校验 | 安全边界；不得为测试或开发模式减弱生产路径 |
| `src/panasonic_posture/service/actuator.py` | relay 生命周期、heartbeat、超时和恢复 | 不在用户进程打开设备；保持错误映射稳定 |
| `src/panasonic_posture/service/sensor*.py`、`hardware_monitor.py` | 传感器源选择、失效检测、硬件状态缓存 | 失败要可诊断；不要伪造硬件状态 |
| `backend/` | C11 relay、uinput、输入抓取、物理开关/设备发现 | 用 `make check-c`；stdout 不得混入日志；发现歧义时 fail closed |
| `src/panasonic_posture/hardware/` | IIO/sysfs/profile/probe，只读硬件能力发现 | profile 只能收窄候选，不能扩大权限边界 |
| `src/panasonic_posture/session/agent.py` | 用户适配器进程壳、per-uid 总线名、信号生命周期 | 先取得总线名，再操作显示；单用户单实例 |
| `src/panasonic_posture/session/runtime.py` | 拉取状态、租约、重试、锁屏和通知协调 | 按 tick 串行推进；临时失败不能误撤销软件接管 |
| `src/panasonic_posture/session/display.py`、`plasma.py`、`desktop.py` | GNOME/Plasma 6 显示后端与桌面检测 | 只支持 Wayland；不得恢复 X11/xrandr/xinput 回退 |
| `src/panasonic_posture/session/transaction.py`、`journal.py` | 显示所有权、回读、回滚、崩溃恢复 | 所有显示写入必须复用该事务；恢复日志不得绕过 |
| `data/ui/` | GJS + GTK 4/libadwaita 界面、D-Bus 客户端和演示图 | 只展示用户可理解状态；不泄露硬件/调试字段 |
| `data/profiles/` | CFQV8/CFQV9/CFQV1 等机型 profile | schema 版本 1；格式和匹配规则变更需同步测试 |
| `tests/` | `unittest` 测试、D-Bus/UI 契约和手工验收夹具 | 只有目标机脚本才是真机证据 |
| `packaging/`、`Makefile`、`pyproject.toml` | systemd/D-Bus/polkit、各发行版包、构建入口和版本 | 不安装到开发宿主；版本元数据必须一致 |

`README.md` 是唯一用户总览；`USAGE.md` 是安装、打包与运维说明。
仓库没有面向用户的 `docs/`、`CHANGELOG` 或独立硬件验收文档，不要重新创建或引用。

## 5. 改动路由

> 下表中的 Python 源码路径省略公共前缀 `src/panasonic_posture/`，`test_*.py`
> 位于 `tests/`，其余路径从仓库根目录开始。

| 改动类型 | 先读 | 通常需要同步 | 最低验证 |
| --- | --- | --- | --- |
| 姿态识别、阈值、迟滞、提示时机 | `policy.py`、`controller.py`、`test_policy.py` | `model.py`、`engine.py`、UI 文案和对应测试 | `test_policy`、`test_controller` |
| 手动模式、`Target`、状态字段、generation/ack | `protocol.py`、`model.py`、`controller.py`、`engine.py` | daemon、session runtime、UI service/page、契约测试 | `test_protocol`、`test_model`、`test_engine`、`test_ui_contract` |
| 设置项或校准 | `service/settings.py`、`engine.py`、`protocol.py`、UI settings | daemon 方法、状态快照、翻译、README | `test_settings`、`test_engine`、`test_ui_contract`、`make check-i18n` |
| 传感器发现、校准数学、IIO/sysfs | `hardware/`、`service/sensor*.py`、`hardware_monitor.py` | profile、engine、探针摘要和测试 | `test_sensor`、`test_iio`、`test_sysfs`、`test_profile`、`test_calibration` |
| relay、物理开关、输入抓取 | `backend/`、`service/actuator.py`、`protocol.py` | C 主机测试、Python 错误映射、打包文件 | `make check-c`、`test_actuator`、`test_sysfs` |
| 显示事务、GNOME/Plasma 后端、锁屏 | `session/transaction.py`、`journal.py`、`display.py`、`plasma.py`、`runtime.py` | 正常/失败/重启恢复测试、UI 错误文案 | `test_display`、`test_display_plasma`、`test_journal`、`test_agent` |
| 通知、提示交互、锁屏状态 | `session/notify.py`、`lockstate.py`、`runtime.py`、`agent.py` | 翻译、策略 epoch、UI 契约 | `test_notify`、`test_lockstate`、`test_agent` |
| UI 布局、文案、插画或设置页 | `data/ui/`、`StateSnapshot`、现有 UI 测试 | `po/zh_CN.po`、错误码映射、README（仅用户可见变化） | `test_ui_contract`、`make check-i18n` |
| 打包、systemd、D-Bus/polkit、发布 | `Makefile`、`packaging/`、`pyproject.toml`、`release_version.py` | README/AUR 说明、版本元数据和打包测试 | `test_packaging_workflow`、`test_release`；构建后 `make check-package` |
| 纯文档 | 对应事实源和现有链接 | 只改必要文件，不复制过期状态 | 核对所有路径/命令；能执行的安全命令实际执行 |

以下内容属于兼容契约，修改前必须有明确授权并同步调用方：D-Bus 端点与方法、
状态快照版本 2、relay 协议版本 2、profile 版本 1、设置版本 1、显示日志版本 1、
错误码、per-uid 会话总线名和权限/polkit 语义。

## 6. 不可破坏的系统不变量

1. **硬件优先。** 物理平板开关开启时，软件模式不得覆盖；不得伪造或缓存出虚假的
   硬件状态。
2. **权限隔离。** 设备写入和特权操作留在系统服务与 relay；桌面操作留在用户会话
   适配器；UI 只通过 D-Bus 操作。
3. **目标与结果分离。** `target` 只表示期望状态；只有匹配 generation 的
   `SessionUpdate` 才能确认 `applied`，过期回执必须忽略。
4. **失效即撤销。** 传感器失效、租约丢失或执行失败时，释放软件接管并恢复桌面
   原生状态；不得遗留输入抓取、方向锁或未完成的显示事务。
5. **错误可诊断。** 用户可见内容只使用稳定错误码对应的本地化文案；内部详情只写
   日志，不进入 UI。Python 与 JS 错误码映射必须一致。
6. **事务化显示。** 所有显示后端都必须复用 `session/transaction.py` 的所有权、
   恢复日志、回读、回滚和崩溃恢复流程，不得绕过日志直接写入。
7. **设备发现不猜测。** relay 自建的虚拟开关必须被排除；物理开关、键盘、触控板
   和加速计存在歧义时拒绝选择；profile 不能绕过能力与总线校验。
8. **契约兼容。** D-Bus 端点、状态/relay/profile/设置/日志版本、错误码和
   per-uid 会话总线名都是兼容契约；破坏性变更必须同步版本、迁移和调用方。
9. **持久化安全。** 设置和显示恢复日志使用原子写入；损坏文件隔离为
   `.corrupt-<timestamp>`；schema 变更必须提供迁移。

## 7. 授权与禁区

### 7.1 可以直接执行

- 读取仓库、git 历史、测试和文档。
- 为实现用户请求修改源码、测试、仓库内配置和文档。
- 在仓库内运行只读检查、单元测试、集成测试和构建。
- 使用临时目录或测试夹具验证行为，避免写入真实用户或系统路径。

### 7.2 仅在用户当轮明确要求时执行

- 任何宿主系统副作用：安装/卸载软件、`sudo`/`pkexec`、启停 systemd 单元、
  修改 D-Bus/polkit/桌面设置、旋转屏幕、发送通知或改变锁屏状态。
- 任何真实设备操作：打开或写入 `/dev/input`、`/dev/uinput`，抓取输入，运行
  `tests/manual/` 或需要目标机的验收脚本。
- 修改认证、polkit、D-Bus 授权、租约、per-uid 总线名或特权进程职责。
- 不兼容地修改协议、状态/设置/日志 schema，提升 support tier，或声称真机验证通过。
- 修改发布元数据、tag、AUR 文件，或执行推送、建分支、PR、merge 和发布。
- 删除仓库文件、重命名公共文件、批量重写、引入新依赖或升级依赖。

### 7.3 禁止

- 未经明确请求执行 `git push`、建分支、打 tag、PR、merge 或 AUR 提交。
- 使用 `git reset --hard`、`git checkout --`、`git restore`、`git clean` 等方式
  覆盖、回滚或删除用户已有改动；不得擅自 stage 或 unstage。
- 把 `.backup/`、`.review/`、缓存或构建产物当作源码修改。
- 手工编辑生成物（`backend/relay`、`locale/**/*.mo`、`build/`、`dist/`、
  `__pycache__/`）。
- 在开发宿主上运行 `tests/manual/`，或把 fake 后端测试当成真机证据。
- 为了方便测试放宽授权、把 `--session-bus` 暴露为生产边界，或把设备写入移入
  用户进程。
- 恢复已移除的 X11/xrandr/xinput 路径、添加隐式桌面回退，或宣称未支持环境可用。
- 未运行验证却声称测试通过，或为通过测试而删除、跳过、弱化断言。
- 跳过本轮应做的提交，或把与本轮任务无关的用户改动混入提交。

## 8. 验证矩阵

### 8.1 环境与命令

- 使用仓库默认解释器 `/usr/bin/python3`；项目要求 Python 3.11+。
- 测试框架是标准库 `unittest`，不是 pytest；不要为此新增依赖。
- 运行范围从最小测试逐步扩大；代码改动交付前至少尝试完整的
  `make WERROR=1 check`。

```sh
# 单个 Python 测试模块
PYTHONPATH=src /usr/bin/python3 -m unittest -v tests.test_policy

# 所有 Python 单元测试
make check-unit

# C relay 主机侧测试
make check-c

# session-bus D-Bus 集成
make check-dbus

# gettext 覆盖与错误码映射
make check-i18n

# 完整门槛（C + 编译 + 单元 + D-Bus + i18n）
make WERROR=1 check
```

### 8.2 按改动选择测试

| 改动 | 目标测试 |
| --- | --- |
| 姿态、阈值、提示 epoch | `tests.test_policy`、`tests.test_controller` |
| 数据模型、协议、服务事务、鉴权 | `tests.test_model`、`tests.test_protocol`、`tests.test_engine`、`tests.test_application`、`tests.test_access` |
| relay 生命周期与错误映射 | `tests.test_actuator`，并运行 `make check-c` |
| 传感器、IIO、sysfs、profile、校准 | `tests.test_sensor`、`tests.test_hardware_monitor`、`tests.test_iio`、`tests.test_sysfs`、`tests.test_profile`、`tests.test_calibration` |
| 会话适配器、显示、日志、锁屏、通知 | `tests.test_agent`、`tests.test_desktop`、`tests.test_display`、`tests.test_display_plasma`、`tests.test_journal`、`tests.test_lockstate`、`tests.test_notify` |
| UI 契约、翻译、入口 | `tests.test_ui_contract`、`make check-i18n` |
| 打包、发布元数据 | `tests.test_packaging_workflow`、`tests.test_release` |

### 8.3 验证纪律

- 目标测试通过后再跑完整门槛；不要用完整测试代替对失败点的针对性验证。
- `unittest` 的跳过不是通过。涉及 UI 的测试在缺少 `gjs`/`gtk4-broadwayd` 时
  可能跳过；若本轮改了 UI，必须报告跳过项和残余风险。
- C 改动不能只靠 Python fake 测试；至少运行 `make check-c`。涉及 relay 与
  session 的集成路径时，还要运行 `make check-dbus`。
- UI 文案或错误码改动必须运行 `make check-i18n`；`.mo` 由 `make`/打包流程生成。
- 打包验证使用 `make check-package PACKAGE=/path/to/package.pkg.tar.zst`；仅静态
  检查时加 `PACKAGE_NO_INSTALL=1`，不得把验收包安装到开发宿主。
- 真机兼容性只能由目标机上的手动验收支持。agent 不得自行运行这些脚本；未执行时
  必须写出“未做真机验证”。
- 测试失败时修复根因，不跳过、不删除、不弱化断言。无法运行时报告准确命令、错误
  和残余风险，不得暗示已通过。

## 9. 编码与文档约定

- **Python：** 保持 3.11+ 兼容；纯逻辑模块不读时钟、不做 I/O；边界模块使用类型
  标注、dataclass/`StrEnum` 和现有错误码。运行时依赖保持为零，PyGObject 由系统
  发行版提供。
- **C：** C11；保持 `-Wall -Wextra -Werror` 干净。relay 的 stdout 只允许 JSON
  协议，日志写 stderr；打开字符设备时保留现有 hardening，发现失败要 fail closed。
- **GJS：** 使用现有 ES module、4 空格缩进和 `_()` 本地化入口；页面只读取
  `StateSnapshot` 中经过校验的字段。新增状态字段必须同步 `StateSnapshot` 与 UI
  契约测试。
- **测试：** 用 `unittest`；在进程、总线、文件系统边界注入 fake；临时文件放
  `TemporaryDirectory`；不要读写真实用户状态目录。先写会失败的回归测试，再修实现。
- **持久化：** 复用 `SettingsStore` 和 `Journal` 的原子写入、隔离与回滚，不另写
  一套 JSON 读写逻辑。
- **翻译：** 英文源文案作为 msgid；同步 `po/zh_CN.po` 和
  `src/panasonic_posture/session/errors.py` / `data/ui/errors.js` 的映射；
  `po/POTFILES.in` 必须覆盖准确的源文件。不要手工编辑 `.mo`。
- **文档：** 用户行为和支持范围写入 `README.md`；安装维护写入
  `USAGE.md`。不要恢复已删除的 `docs/`、`CHANGELOG` 或旧验收记录。
- **注释：** 解释不变量、失败路径和“为什么”，不要复述代码逐行做了什么。

## 10. 完成定义

在报告完成前逐项确认：

- [ ] 已定位并修复根因，而不是只改症状。
- [ ] 改动只覆盖任务所需文件，没有覆盖或格式化用户改动。
- [ ] 行为变化有对应测试；失败路径和安全边界已覆盖。
- [ ] 目标测试和必要的完整验证已实际运行，结果已如实记录。
- [ ] 兼容契约、版本号、迁移、翻译、打包和文档联动已检查。
- [ ] 设计变化已写入第 13 节，并与代码/测试在同一轮完成。
- [ ] `git diff --check`、差异和生成物检查通过。
- [ ] 本轮改动已按第 11 节提交；若环境禁止提交，已明确说明。

交付说明至少包含：改了什么、为什么改、受影响文件、实际运行的验证、未验证项及
原因。默认使用用户当前语言，简洁、直接地报告结果和残余风险。

## 11. Git 与提交

- 工作区初始可能已有用户改动，默认属于用户，必须原样保留。
- 修改前查看 `git status` 和相关 diff；不清理、不重排、不格式化无关文件。
- 同一文件混合本轮改动和用户改动时，只暂存本轮 hunk；无法安全分离时停止询问，
  不得整文件暂存。
- 本仓库约定：每轮工作完成并验证后，在交付前提交本轮改动。只有在用户明确要求
  不提交或上层执行环境禁止提交时，才保留未提交状态，并在交付中说明。
- 提交只包含本轮相关文件和 hunk。存在其他未提交改动时，禁止使用 `git add .`、
  `git commit -a` 或等价的批量暂存方式。
- 提交前检查 `git status`、`git diff --cached` 和 `git diff --cached --check`，
  确认没有生成物、缓存、密钥或无关文件。
- 提交信息沿用仓库现有的 Conventional Commits 风格，使用简洁英文主题；正文说明
  行为变化和验证结果。常用前缀：`feat:`、`fix:`、`test:`、`docs:`、
  `refactor:`、`build:`、`release:`。
- 已发布 tag 不可移动、删除或重写；不修改 `.git/`。
- 不 amend 已提交历史；不推送、不建分支、不打 tag，除非用户明确要求。
- 本轮没有改动时不创建空提交；提交失败或无法安全提交时，明确报告原因和残留状态。

## 12. 必须停止并询问

- 请求需要跨越 7.2 中的宿主系统、真实设备、权限或发布边界，但授权不明确。
- 需要不兼容地修改协议、状态/设置/日志 schema、认证或租约语义。
- 目标文件包含无法安全分离的用户改动，继续修改可能覆盖用户工作。
- 必需验证依赖真实硬件、特权操作、缺失工具链或外部环境，且没有安全替代方案。
- 代码、测试和文档对当前行为给出互相冲突的结论，无法确定唯一事实来源。
- 任务存在多种会显著改变范围、兼容性或产生不可逆结果的解释。

不要为命名偏好、样式选择或能由现有模式明确回答的小问题停下来询问；做合理假设，
在交付中说明即可。

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
- 影响：正常标签发布不变；手动重建的制品来源是所选分支或提交，发布标签和版本
  校验仍保持一致。
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
