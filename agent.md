# 松下二合一平板电脑姿态管理工具：维护者指南

- 适用版本：1.0.0
- 更新时间：2026-11-03
- 读者：维护者、贡献者和接手实现的开发者
- 用户文档：[README.md](README.md)

本文说明运行架构、内部契约、测试与发布约束。修改行为时，应同时检查受影响的
用户文档、测试和打包文件。机型适配见
[docs/hardware-profiles.md](docs/hardware-profiles.md)，实机证据见
[docs/hardware-validation.md](docs/hardware-validation.md)，0.2.1 检阅记录见
[docs/project-review-2026-10-31.md](docs/project-review-2026-10-31.md)（仅历史存档）。

## 1. 架构与原则

| 单元 | 身份 | 职责 |
| --- | --- | --- |
| 系统服务 | root，系统单元 | 传感器采集、硬件开关、策略、事务、D-Bus 服务和 relay 生命周期 |
| `relay` | root，daemon 子进程 | 聚合物理/软件平板状态，抓取内置输入，创建虚拟平板开关 |
| 会话适配器 | 登录用户，用户单元 | 维持租约、应用显示事务、显示提示、记录恢复日志 |
| 配置窗口 | 登录用户，GJS | 通过 D-Bus 展示状态并发送设置/确认操作 |
| 探针与测试 | 登录用户 | 只读收集 DMI/profile/设备信息；单元、集成和手动测试 |

```text
加速度计 / 物理平板开关
          │
          ▼
  系统服务（root）：sensor → policy → controller → engine
          │                                    │
          │ D-Bus 状态                         │ relay 管道
          ▼                                    ▼
  会话适配器（用户）                      relay（root）
  Mutter / KScreen / xrandr              uinput / evdev
  通知 / 恢复日志                              │
          └── SessionUpdate（租约和 generation 回执）──┘

  UI（用户）←── GetState / StateChanged / 操作方法 ──→ 系统服务
```

核心约束：

1. **硬件优先。** 物理平板开关开启时，软件模式不能覆盖。
2. **权限隔离。** 设备写入和特权操作留在系统服务与 relay；桌面操作放在会话
   适配器；UI 只使用 D-Bus。
3. **目标与结果分离。** `target` 只表示期望状态，只有匹配 generation 的
   `SessionUpdate` 才能确认 `applied`。
4. **失效即撤销。** 传感器失效、租约丢失或执行失败时，释放软件接管并恢复
   桌面原生状态。
5. **错误可诊断。** 内部错误写日志；用户界面只显示稳定错误码对应的本地化文案。

## 2. 主循环

系统服务每 `TICK_SECONDS = 0.1` 秒运行一次主循环：

1. `SensorManager` 读取最新有效加速度样本。优先使用 IIO；IIO 不可用或连续
   1 秒无样本时回退 sysfs，让回退在姿态失效窗口结束前完成。
2. `SwitchReader` 查询物理平板开关，`HardwareMonitor` 对未知状态做 0.5 秒去抖。
3. `Policy` 产生内部姿态，`Controller` 叠加手动模式和硬件优先规则，得到公开
   `Target`。
4. `Engine` 更新 generation、维护租约并调用 `Actuator`；软件平板目标只在会话
   适配器就绪后保持 relay。relay 释放由单 worker 串行处理，避免阻塞 100 ms tick。
5. 会话适配器轮询状态，把当前 generation 的显示事务提交到单 worker。成功后
   再次核对 generation，再上报 `ready/error` 并续租。
6. `StateSnapshot` 内容变化时才发送 `StateChanged`。

传感器源策略：

- 慢速 sysfs 活动期间，用影子 IIO 试读决定是否切换；试读不得中断现有样本流。
- 软件接管处于待确认、帐篷、支架或软件平板目标时，固定使用 sysfs，避免本机
  firmware 在 IIO 交接后静默导致姿态误退出。
- 硬件平板态也优先使用 sysfs，让 `iio-sensor-proxy`/桌面合成器使用 IIO。
- 不使用 `ClaimAccelerometer`。

## 3. 事务、租约与恢复

### 代次（generation）

- `target` 实际变化时递增 `generation`，同时清空 `applied` 和旧错误。
- `SetMode` 改变手动模式、校准或恢复传感器默认值时立即递增代次；确认提示后的
  目标变化由下一次 tick 统一递增。
- 回执携带 generation；旧回执直接忽略，不能把过期结果标记为成功。
- `SessionUpdate(generation, ready, error_code, error_detail)` 中，`ready=true`
  表示显示目标已应用；`ready=false` 且带错误码时进入可重试失败状态。
- 目标未变化时重复操作应幂等。
- 目标变化时会记录旧目标、新目标、策略模式、generation 和传感器来源，便于定位
  姿态抖动或传感器交接问题。

### 租约

| 项目 | 超时/周期 | 失效行为 |
| --- | --- | --- |
| 会话心跳 | 0.5 秒；有效期 3 秒 | 撤销软件显示目标，释放 relay |
| relay 心跳 | 0.5 秒；报告超时 2.5 秒 | daemon 判定执行器失效并重建 |
| relay 软件租约 | 3 秒 | relay 自行释放软件请求 |

- 每个 uid 使用独立系统总线名 `work.mmoe.panasonic.Posture.Session.u<uid>`，
  同一用户只能运行一个适配器。
- 快速切换用户时，新适配器可能要等旧租约过期；期间会收到
  `access-denied: session-lease-busy`。
- 正常释放通过关闭 relay stdin 完成。异常终止后由 `Actuator` 或 systemd
  `ExecStopPost` 执行 `relay --recover`。
- 休眠/唤醒会重置策略锚点、手动模式、generation 和设备发现状态；旧通知失效。

### 关键常量

| 常量 | 值 |
| --- | --- |
| `TICK_SECONDS` | 0.1 秒 |
| `LEASE_SECONDS` / `AGENT_HEARTBEAT_SECONDS` | 3.0 / 0.5 秒 |
| `RELAY_REPORT_TIMEOUT_SECONDS` | 2.5 秒 |
| `HARDWARE_UNKNOWN_DEBOUNCE_SECONDS` | 0.5 秒 |
| `IIO_NO_SAMPLE_SECONDS` / `SENSOR_RETRY_SECONDS` | 1.0 / 60.0 秒 |
| `PolicyConfig.stale_seconds` | 5.0 秒 |
| `DISPLAY_RETRY_BASE_SECONDS` / `MAX` | 2.0 / 10.0 秒 |
| `SLOW_TICK_WARN_SECONDS` | 0.25 秒 |
| 校准窗口 / 最少样本 | 2.0 秒 / 3 |
| 校准稳定性阈值 / 最大平放角 | 8° / 45° |

## 4. 姿态策略

`policy.py` 是纯逻辑，输入为时间、`SensorReading` 和硬件状态，不执行 I/O。

| 内部模式 | 含义 |
| --- | --- |
| `L` | 笔记本姿态，不接管 |
| `P` | 帐篷候选，等待确认 |
| `Q` | 支架候选，等待确认 |
| `T` | 已确认帐篷 |
| `S` | 支架 |
| `H` | 硬件平板，硬件优先 |
| `D` | 无可靠判断：样本失效或硬件状态未知 |

几何量由归一化重力向量计算：

- `pitch = atan2(-y, -z)`；`hypot(y, z) < 0.05` 时不可用。
- `roll = atan2(x, hypot(y, z))`。
- `flat = acos(clamp(-z))`；接近 180° 表示底座反向平放。
- 有效样本模长范围为 `7.5–12.0 m/s²`。

主要阈值：

| 项目 | 进入 | 保持/退出 |
| --- | --- | --- |
| 稳定窗口 | 0.45 秒，方向偏差 ≤ 8° | 历史窗口 0.75 秒，最多 128 个样本 |
| 姿态确认 | 0.6 秒 | 稳定离开后 0.7 秒回到 `L` |
| 帐篷确认区 | pitch 65–115°，\|roll\| ≤ 25° | 保持区 pitch 45–155°，\|roll\| ≤ 40° |
| 支架区 | flat ≥ 155° | flat ≥ 130°，且需稳定离开 2.0 秒后回到 `L` |
| 传感器失效 | — | 5.0 秒无有效样本进入 `D` |
| 帐篷免确认重进 | — | 确认后离开，5.0 秒内重进帐篷区 |

提示规则：

- 只有从笔记本姿态进入帐篷或支架时，启用的提示才会产生 `P`/`Q`。
- 从其他姿态进入帐篷或支架会直接切换；短暂经过 `L` 也不应重新生成提示。
- 确认必须同时满足：epoch 匹配、硬件开关已知关闭、样本稳定、当前几何仍在
  对应确认区。
- 关闭提示后，等待中的 `P`/`Q` 在下一次有效 tick 直接转为 `T`/`S`。

不变量：

- 无效样本不更新 `last_stamp`、`last_geometry` 或稳定性锚点。
- `hardware=True` 时进入 `H`，清空稳定性证据，手动模式不可覆盖。
- `D` 产生空目标并撤销软件接管；没有传感器证据时不能维持伪平板状态。
- `pose` 是界面展示值。样本失效时保留最近确认姿态，不产生独立的“等待数据”状态。
- 支架使用较宽的保持区、稳定离开门槛和 2 秒退出防抖，以适应约 2 Hz 的 sysfs
  采样。
- IIO 连续 1 秒无样本时开始回退 sysfs；策略保留上次决策至多 5 秒，为完整交接
  留出时间。
- 手动模式不持久化；`tent_prompt` 和 `stand_prompt` 写入设置文件。

## 5. 硬件发现

### profile

- 支持等级：`validated`（正式支持范围）、`compatible`（能力发现）和
  `unsupported`（显式禁用）。`validated` 不等于每台机器都完成了物理验收。
- 查找顺序：`/etc/panasonic-posture-manager/profiles/` → 源码树
  `data/profiles/` → `/usr/share/panasonic-posture-manager/profiles/`。
- 支持精确 `vendor`/`product`、字符串简写和大小写不敏感的 `product_glob`；
  第一个有效匹配生效。
- 损坏、版本不符或字段非法的 profile 会被记录并跳过，不阻止服务启动。
- 没有匹配时使用 generic 能力发现。profile 名称只用于消歧，不能绕过能力和
  总线校验。

### 设备规则

- 平板开关只接受 capability 恰好包含 `SW_TABLET_MODE` 且没有 `EV_KEY` 的
  switch-only 设备。lid/power 等复合开关不接管。
- relay 和 Python 发现必须排除自建设备：名称为
  `Panasonic posture managed switch`，phys 为
  `panasonic-posture-manager/relay`。
- 键盘需具备字母键能力；触控板需为多点触控、非 DIRECT。USB、Bluetooth 和
  外接 phys 设备被排除。
- 同一槽位存在多个匹配时拒绝猜测。relay 使用 `EVIOCGRAB` 持有设备，但不读取
  按键内容。
- 加速计优先匹配 profile 指定名称（默认 `accel_3d`）。名称不符时，只在恰好
  有一个完整三轴 IIO 设备时回退；多个候选同样拒绝猜测。

### 只读探针

```sh
panasonic-posture-manager-probe > /tmp/posture-probe.json
PYTHONPATH=src python -m panasonic_posture.hardware.probe --probe
```

探针无需 root，不打开 `/dev/input`，输出 DMI、命中的 profile、输入设备能力和
IIO 信息。机型适配报告应附完整 JSON。

## 6. D-Bus 与 relay 契约

### D-Bus

- 总线名：`work.mmoe.panasonic.Posture`
- 对象路径：`/work/mmoe/panasonic/Posture`
- 接口：`work.mmoe.panasonic.Posture`

| 方法/信号 | 签名 | 说明 |
| --- | --- | --- |
| `GetState` | `() → (s)` | 返回缓存状态 JSON，不推进 tick |
| `SetMode` | `(s) → ()` | `auto`/`laptop`/`tent`/`stand`/`tablet` |
| `Retry` | `() → ()` | 清除执行器/事务错误并重放目标 |
| `SetTentPrompt` | `(b) → ()` | 持久化帐篷提示设置 |
| `SetStandPrompt` | `(b) → ()` | 持久化支架提示设置 |
| `CalibrateSensor` | `() → ()` | 保存当前平放方向为校准基准 |
| `ResetSensorCalibration` | `() → ()` | 删除校准并恢复 profile 原始方向 |
| `ConfirmTent` | `(u) → ()` | 用 pending epoch 确认帐篷 |
| `ConfirmStand` | `(u) → ()` | 用 pending epoch 确认支架 |
| `SessionUpdate` | `(u b s s) → ()` | generation、ready、错误码、错误详情 |
| `StateChanged` | `(s)` 信号 | 状态 JSON 内容变化时发出 |

写方法完成参数校验并记录状态后先返回，后续高优先级空闲 tick 会更新目标。授权要求：

- 写方法要求 seat0 上的活动本地图形会话，并通过 polkit action
  `work.mmoe.panasonic.Posture.configure`。
- `SessionUpdate` 还会验证 sender uid、per-uid 总线名、活动本地会话和用户单元
  `panasonic-posture-manager-session.service`。
- `--session-bus` 仅用于开发和集成测试，不能作为生产授权边界。

### 状态 JSON

`version=2`，主要字段：

| 分组 | 字段 |
| --- | --- |
| 身份 | `version`、`instance`、`generation`、`profile`、`model`、`vendor` |
| 姿态与模式 | `pose`、`manual`、`hardware`、`hardware_status` |
| 设置与提示 | `tent_prompt`、`stand_prompt`、`pending_notification`、`pending_prompt` |
| 事务 | `target`、`applied` |
| 运行状态 | `backend`、`error_code`、`error_detail`、`session_connected` |
| 支持与传感器 | `supported`、`support_tier`、`sensor_source`、`sensor_error`、`sensor_calibrated` |

`target` 包含 `mode`、`tablet`、`rotation` 和 `disable_internal_input`。错误正文为
`<code>: <detail>`，D-Bus error name 固定为 `work.mmoe.panasonic.Posture.Error`。
UI 和会话适配器只本地化 `protocol.ErrorCode` 中的稳定代码。

### relay

源码在 `backend/`，使用 C11，安装到
`/usr/lib/panasonic-posture-manager/relay`。stdin/stdout 协议版本为 2。

stdin：

- `1`：刷新 3 秒软件租约。
- 换行：忽略。
- EOF：释放软件请求并正常退出。
- 其他字节：协议错误，清理后以退出码 10 退出。

stdout 每行一个 JSON：

```json
{"protocol":2,"kind":"ready","physical":true}
{"protocol":2,"kind":"physical","present":false}
{"protocol":2,"kind":"applied","hardware":false,"tablet":true,"input_disabled":true,"physical":true,"error":null}
{"protocol":2,"kind":"released","exit":0}
```

- `hardware` 是聚合前的物理开关状态；`tablet` 是物理或软件请求的聚合结果。
- `input_disabled` 表示内置键盘和触控板已成功抓取。
- `error="input-grab-failed"` 表示平板目标已建立，但输入抓取仍失败；relay 每秒重试。
- `physical=false` 表示尚未发现物理开关，不代表硬件损坏。

`--recover` 先读取物理开关，再创建虚拟设备并先发 ON，随后发送最终状态
（物理 ON→ON，OFF/缺席/不可读→OFF），最后卸载设备。正常 EOF 清理不需要 recover。

| 退出码 | 含义 |
| --- | --- |
| 0 | 正常退出或恢复完成 |
| 2 | 参数错误或非 root |
| 3 | 物理开关不可读或歧义 |
| 4 | 无法创建 uinput 设备 |
| 5 | 物理开关存在但无法 grab |
| 8/9/10 | 写事件失败 / poll 异常 / stdin 协议错误 |

relay 每秒重新枚举开关，内置输入每 2 秒复核身份。物理开关暂时消失时，relay
解除监控但继续响应软件请求。

## 7. 会话、显示与通知

### 后端选择

| 会话 | 后端 | 行为 |
| --- | --- | --- |
| `XDG_CURRENT_DESKTOP` 含 `GNOME` | `gnome` | Wayland/X11 均使用 Mutter DisplayConfig |
| `KDE`/`PLASMA` + Wayland，版本为空或 6 | `plasma` | 优先 `kscreenctl`，否则 `kscreen-doctor` |
| 其他 X11 | `x11` | `xrandr` 查询和旋转；可选 `xinput` 触摸映射 |
| 其他 Wayland 或无法识别 | `unsupported` | 返回 `display-unavailable`，适配器继续运行 |

- Plasma 5 Wayland 明确拒绝；Plasma 6 X11 使用通用 X11 后端。
- `PANASONIC_POSTURE_DISPLAY_BACKEND` 可覆盖检测结果。
- `PANASONIC_POSTURE_OUTPUT` 可指定 X11/Plasma 输出名。
- `PANASONIC_POSTURE_TOUCH_DEVICE` 可指定 X11 `xinput` 设备名，逗号分隔。

### 显示事务

- GNOME 通过 Mutter `GetCurrentState`/`ApplyMonitorsConfig` 修改内屏旋转，内屏按
  `is-builtin` 查找。
- 所有后端都复用 `session/transaction.py` 的所有权、恢复日志、回读、回滚和崩溃
  恢复流程。写入前先持久化日志；失败后回滚，无法回滚则留待下次重试。
- 镜像、内屏未启用、多个内屏或会改变外接屏布局的 GNOME 配置会被拒绝。
- 帐篷旋转 180°；笔记本、支架和软件平板旋转 0°。GNOME/Plasma 的软件平板先
  转回并锁定 0°，再释放自动旋转；X11 只能保持显式横屏。
- 硬件平板先恢复软件接管前的方向和触摸映射，再解除方向锁，把旋转交还系统。
  离开硬件平板或适配器退出时恢复用户原设置。
- X11 触摸映射按 `INPUT_PROP_DIRECT` 识别候选，将同一 phys 的触摸屏与手写笔
  作为一组；多个无关触摸屏时停用自动映射。事务保存并恢复原始坐标矩阵。

### 提示与锁屏

- 显示目标成功且 `pending_notification` 非空时，适配器按 `pending_prompt` 显示
  帐篷或支架通知。
- 通知失败按 1/2/4 秒重试；同一 `(epoch, prompt)` 成功显示后不重复弹出。
- 锁屏时关闭提示；解锁后若候选仍存在则重新显示。
- 锁屏检测优先使用 logind `LockedHint`，然后回退到 freedesktop/GNOME
  ScreenSaver 接口。通知使用 `org.freedesktop.Notifications`。
- 显示失败按 2–10 秒指数退避，用户可通过 `Retry` 立即重放。

## 8. 设置、数据与 UI

| 路径 | 所有者 | 内容 |
| --- | --- | --- |
| `/etc/panasonic-posture-manager/profiles/` | 管理员 | 本机 profile 覆盖，优先级最高 |
| `/usr/share/panasonic-posture-manager/profiles/` | 包 | 内置 profile |
| `/var/lib/panasonic-posture-manager/settings.json` | 系统服务 | 提示开关和传感器校准矩阵 |
| `$XDG_STATE_HOME/panasonic-posture-manager/display.json` | 登录用户 | 显示事务恢复日志 |

- 设置文件和恢复日志使用原子写入；损坏文件会隔离为 `.corrupt-<timestamp>`，
  不阻止组件启动。
- 设置 schema 版本为 1；`sensor_calibration` 是可选字段。
- UI 位于 `data/ui/`，由 GJS + GTK 4/libadwaita 实现。
- `service.js` 管理系统总线代理、`StateChanged` 和重连；`errors.js` 将稳定错误码
  转换为本地化文案。
- 姿态插图只做展示去抖，不参与硬件决策。
- 源码树从 `data/` 和 `locale/` 读取资源；安装后使用
  `/usr/share/panasonic-posture-manager/` 和系统 locale。
- `PANASONIC_POSTURE_UI_DIR`、`PANASONIC_POSTURE_LOCALE_DIR` 可用于开发覆盖。

修改 UI 时保持以下约束：设置项立即反馈并合并连续写入；一次性操作防重复；错误
不暴露内部术语；交互控件有可访问性标签；关闭窗口不影响后台服务。

## 9. 构建与测试

```sh
make                     # 构建 relay 和中文 .mo
make check               # C、compileall、unittest、D-Bus 集成和 i18n
make check-c             # 仅构建并运行 relay 主机侧测试
make WERROR=1 check      # 发布/验收用，C 警告视为错误
make check-package PACKAGE=/path/to/package.pkg.tar.zst  # 校验 AUR 包
make clean               # 清理构建产物和 __pycache__
```

测试分层：

- `tests/test_*.py`：标准库 unittest，覆盖策略、控制器、engine、校准、设置、
  权限、传感器、硬件发现、显示、通知、恢复日志和 UI 契约。
- `tests/integration/check_dbus.py`：在 `dbus-run-session` 下验证 D-Bus 方法、
  `SessionUpdate`、设置落盘和错误路径。
- `tests/integration/check_agent.py`：用真实适配器状态机和假显示/通知后端验证
  apply、心跳和清理流程。
- `tests/package/check_package.py`：校验已构建 AUR 包的元数据、文件布局、源码
  一致性、服务配置和命令入口，并在临时 pacman 根目录中执行全新安装、重装或
  升级、卸载。默认不触碰宿主机；`PACKAGE_NO_INSTALL=1` 只做静态检查。
- `tests/manual/`：需要目标机、真实安装和授权的测试。这些脚本可能临时移除输入
  节点或注入进程故障，只能在目标机上运行。

开发运行：

```sh
PYTHONPATH=src python -m panasonic_posture.service.daemon --session-bus
gjs -m data/ui/main.js
PYTHONPATH=src python -m panasonic_posture.hardware.probe --probe
```

修改 C 代码后至少运行 `make check-c`。提交前运行 `make WERROR=1 check`。真实
硬件行为必须补充手动验收记录，不能用 fake 后端测试代替。

### 桌面后端验收

先记录会话并确认适配器在线：

```sh
printf '%s | %s | %s\n' "$XDG_CURRENT_DESKTOP" "$XDG_SESSION_TYPE" "$KDE_SESSION_VERSION"
systemctl --user status panasonic-posture-manager-session.service
```

每个桌面至少完成“笔记本 → 帐篷 → 支架 → 平板 → 笔记本”，检查旋转、恢复日志、
通知、锁屏、外接屏和休眠/唤醒。普通 X11 还需验证每个旋转角的四角触摸、多指和
手写笔映射。不要用 fake 后端结果替代真机兼容性结论。

## 10. 打包与发布

- wheel 提供 Python 包和两个命令：`panasonic-posture-manager`、
  `panasonic-posture-manager-probe`。
- `data/{ui,assets,profiles}` 安装到 `/usr/share/panasonic-posture-manager/`。
- `backend/relay` 安装到 `/usr/lib/panasonic-posture-manager/relay`。
- systemd、D-Bus、polkit、desktop、modules-load 和翻译文件进入发行版标准目录。
- 包不写入 `/usr/local`；`package()` 会拒绝包含该路径的文本文件。

`v1.0.0` 源码 tag 已发布，包元数据和本地安装包验收已通过。AUR 包仍处于提交
准备阶段：clean-chroot 构建、`namcap`、全新安装/升级/卸载和最终物理验收尚未
完成。完整状态和命令见 [packaging/aur/README.md](packaging/aur/README.md)。

发布前必须：

1. 保持 `v1.0.0` tag 不可变，并持续确认 `pkgver`、tag 和 `pyproject.toml` 版本一致。
2. 在干净 Arch chroot 中运行 `make WERROR=1 check`、wheel 构建、`makepkg`、
   `make check-package` 和 `namcap`；再验证实际安装脚本、D-Bus 激活、会话启动
   和卸载。
3. 提交 AUR 包，并复核 `.SRCINFO`、维护者、依赖和安装路径。
4. 补齐 CFQV8-1L 最终物理验收和 QV9/QV1 同族回归。
5. 分别验证 GNOME X11、Plasma 6 Wayland 和普通 X11 的旋转、恢复、通知和
   外接屏行为。

## 11. 修改检查清单

1. 是否改变 `protocol.py` 中的 D-Bus、relay、状态或错误码契约？若是，Python、C
   和文档必须同步。
2. 是否改变姿态阈值、租约或重试时序？更新策略测试和本文常量表。
3. 是否改变设备发现？同步更新 Python、C、探针和 profile 文档。
4. 是否改变用户可见行为？更新 README；UI 文案更新 `po/zh_CN.po` 并重建 `.mo`。
5. 是否影响显示、通知或恢复日志？补充正常、失败和崩溃恢复测试。
6. 是否影响安装路径、权限或打包？更新 `packaging/` 和发布清单。

提交前运行：

```sh
make WERROR=1 check
```

涉及真实硬件的改动还应在目标机完成手动验收，并在
`docs/hardware-validation.md` 记录日期、机型和结果。
