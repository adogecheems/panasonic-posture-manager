# 松下二合一平板电脑姿态管理工具：维护者指南

- 适用版本：0.3.0
- 文档状态：2026-11-02，已按当前代码整理
- 读者：项目维护者、贡献者与接手实现工作的开发者
- 用户说明：[README.md](README.md)

本文说明运行架构、内部契约、状态机、构建测试和发布约束。代码与测试是最终
事实来源；修改行为时请同时更新本文及受影响的用户文档。机型适配细节见
[docs/hardware-profiles.md](docs/hardware-profiles.md)，实机记录见
[docs/hardware-validation.md](docs/hardware-validation.md)。

## 1. 设计原则

1. **硬件优先**：物理平板开关开启时，任何手动或软件模式都不能覆盖硬件状态。
2. **权限隔离**：需要硬件写入和 root 的部分留在系统服务与 relay；显示、通知等
   桌面操作放在用户会话适配器；配置窗口只通过 D-Bus 读取状态和发出操作。
3. **目标与结果分离**：计算出的 `target` 不等于已经生效的 `applied`。只有带匹配
   generation 的回执才能确认目标完成。
4. **失效即撤销**：传感器失效、会话租约丢失或执行器失败时，释放软件接管并恢复
   原生显示与输入路径，不把旧状态继续当作成功。
5. **失败可恢复**：设备发现、显示事务和状态文件都要有重试或回滚路径；单次异常
   不应终止 daemon 的 100 ms 主循环。
6. **错误码稳定**：面向用户的文案与机器可读错误码分离，D-Bus detail 不直接显示
   在界面上。

## 2. 运行架构

0.3.0 由四个运行单元和一组开发工具组成。

| 单元 | 身份 | 职责 | 主要入口 |
| --- | --- | --- | --- |
| 系统服务 | root，系统单元 | D-Bus 外壳、传感器采集、硬件开关、策略、事务与 relay 生命周期 | `python -m panasonic_posture.service.daemon` |
| `relay` | root，daemon 子进程 | 创建虚拟平板开关、聚合物理/软件状态、抓取内置键盘与触控板、恢复原生状态 | `/usr/lib/panasonic-posture-manager/relay` |
| 会话适配器 | 登录用户，用户单元 | 保持会话租约、应用 Mutter 显示事务、显示通知、记录恢复日志 | `python -m panasonic_posture.session.agent` |
| 配置窗口 | 登录用户，GJS | 显示姿态、错误与设置，调用 D-Bus 方法 | `panasonic-posture-manager` → `data/ui/main.js` |
| 探针/测试 | 登录用户 | 只读收集 DMI、profile、输入设备与 IIO 信息；单元、集成和手动测试 | `panasonic-posture-manager-probe`、`make check`、`tests/**` |

数据流：

```text
加速度计 / 物理平板开关
           │
           ▼
  系统服务 daemon（root）
  sensor → policy → controller → engine
           │                         │
           │ D-Bus 状态              │ 管道：心跳 / JSON 行
           ▼                         ▼
  会话适配器（用户）              relay（root）
  Mutter / 通知 / 恢复日志        uinput / evdev
           │
           └──── SessionUpdate（租约 + generation 回执）───→ daemon

  UI（用户） ←── GetState / StateChanged / 操作方法 ──→ daemon
```

- 系统服务做出姿态决策，不依赖配置窗口是否打开。
- 软件接管真正应用前，系统服务要求一个已认证的会话适配器持有租约并回报当前
  generation 已就绪；显示事务由会话适配器执行。
- 配置窗口只使用 D-Bus，不直接接触设备、Mutter 或系统设置。
- relay 由 daemon 以 root 子进程启动；daemon 退出时内核会随进程释放 grab。

## 3. 目录与模块

```text
src/panasonic_posture/
  protocol.py        D-Bus 端点、版本、时序、错误码、安装路径等共享常量
  model.py           模式、姿态、目标、传感器样本、状态快照
  policy.py          纯姿态状态机与几何计算
  controller.py      手动模式、硬件优先、目标映射、generation/回执
  presentation.py    仅用于 UI 插图的姿态去抖
  hardware/          DMI/profile、IIO、sysfs 输入设备和只读探针
  service/           daemon、应用组装、engine、传感器、开关、执行器、设置
  session/           agent、会话状态机、显示事务、通知、恢复日志
data/
  ui/                GJS + GTK4/libadwaita 配置窗口
  assets/            姿态插图、应用图标
  profiles/          内置机型 profile（cfqv8.json）
backend/             C11 relay、输入抓取、虚拟开关与主机侧测试
packaging/           systemd、D-Bus、polkit、desktop、AUR 打包文件
tests/               单元、session-bus 集成、i18n 与需要真机的手动测试
docs/                机型适配、实机验证与历史评审记录
```

模块职责应保持单向：`policy`/`controller` 不读时钟和设备；`engine` 负责编排；
D-Bus 外壳只负责授权、分发与生命周期。新增 I/O 时优先注入依赖，避免难点只能
通过真实设备验证。

## 4. 每 tick 的处理链

系统服务每 `TICK_SECONDS = 0.1 s` 执行一次主循环，主要步骤如下：

1. `SensorManager` 读取最新有效加速度样本。优先 IIO 缓冲；IIO 不可用或超过
   2 秒无样本时回退 sysfs 轮询。sysfs 活动期间以影子试读方式重试 IIO，失败
   时每 60 秒再试。
2. `SwitchReader` 查询物理平板开关；`HardwareMonitor` 对未知值做 0.5 秒去抖。
3. `Policy` 根据姿态与硬件状态产生内部模式，`Controller` 叠加手动模式和硬件
   优先规则，得到公开的 `Target`。
4. `Engine` 维护 generation 与租约，驱动 `Actuator`；软件平板目标只有在会话
   适配器就绪后才保持 relay，其他情况释放软件接管。
5. 会话适配器轮询 `GetState`，对当前 generation 执行显示事务，然后通过
   `SessionUpdate` 回报 ready/error；同时发送心跳维持 3 秒租约。
6. `Engine` 生成新的 `StateSnapshot`；只有内容变化时才发出 `StateChanged`。

硬件平板状态和软件接管期间会优先使用 sysfs 传感器：前者让
`iio-sensor-proxy`/GNOME 读取 IIO 以支持原生旋转，后者避免固件偶发停流造成帐篷
模式误退出。实现不使用 `ClaimAccelerometer`。

## 5. 事务、租约与恢复

### 5.1 generation 与回执

- `target` 变化会递增 `generation` 并清空 `applied`；`SetMode` 在手动模式实际
  变化时立即递增 generation，`ConfirmTent` 成功后由下一次 tick 的目标变化递增。
  `SetTentPrompt` 只影响提示可见性、不改变显示目标，因此不会额外使在途回执失效。
- 回执携带 generation；旧 generation 的回执被忽略，不能把过期结果标成成功。
- `SessionUpdate(generation, ready, error_code, error_detail)` 的 `ready=true`
  表示会话侧显示目标已应用；`ready=false` 且带错误码时进入可重试失败状态。
- 目标未变化时重复操作应保持幂等，不额外产生事务。

### 5.2 会话租约

- 会话适配器每 0.5 秒发送一次心跳；daemon 以 3 秒有效期判断适配器是否仍在。
- 租约失效时，软件显示目标被撤销，relay 释放输入，`session_connected=false`。
- 每个 uid 使用独立系统总线名 `work.mmoe.panasonic.Posture.Session.u<uid>`；
  同一用户只能有一个适配器实例。
- 快速用户切换期间，旧租约仍有效时新用户会收到 `access-denied:
  session-lease-busy`，需等待旧租约自然过期。

### 5.3 relay 租约

- daemon 每 0.5 秒向 relay stdin 写入心跳；relay 侧软件租约为 3 秒。
- relay 超过 2.5 秒没有报告时，daemon 将其视为心跳超时并重建执行器。
- 正常释放通过关闭 stdin 触发，让 relay 自行清理；执行器只在信号终止或超时
  杀进程后主动运行 `relay --recover`。systemd 的 `ExecStopPost` 还会在服务停止
  时执行一次 recover，作为最后兜底。

### 5.4 帐篷确认与休眠

- 帐篷候选产生新的 epoch；通知和 UI 按钮都用 epoch 调用 `ConfirmTent`。
- 确认必须同时满足：epoch 匹配、硬件开关已知关闭、样本稳定、当前几何仍处于
  帐篷确认区。
- 休眠/唤醒会重置策略锚点、手动模式和事务 generation，并重新发现设备；旧
  通知不能在新 epoch 下继续有效。

### 5.5 关键时序

| 常量 | 值 | 作用 |
| --- | --- | --- |
| `TICK_SECONDS` | 0.1 s | 服务与会话主循环 |
| `LEASE_SECONDS` | 3.0 s | 会话租约有效期 |
| `AGENT_HEARTBEAT_SECONDS` | 0.5 s | 会话心跳周期 |
| `RELAY_HEARTBEAT_SECONDS` | 0.5 s | relay 心跳周期 |
| `RELAY_REPORT_TIMEOUT_SECONDS` | 2.5 s | relay 报告超时 |
| `HARDWARE_UNKNOWN_DEBOUNCE_SECONDS` | 0.5 s | 开关未知状态去抖 |
| `IIO_NO_SAMPLE_SECONDS` | 2.0 s | IIO 停流回退阈值 |
| `SENSOR_RETRY_SECONDS` | 60.0 s | 回退后重试 IIO 的周期 |
| `DISPLAY_RETRY_BASE_SECONDS` / `MAX` | 2.0 / 10.0 s | 显示事务退避上限 |
| `SLOW_TICK_WARN_SECONDS` | 0.25 s | 慢 tick 警告阈值 |

## 6. 姿态策略

`policy.py` 是纯逻辑，输入为 `now + SensorReading + hardware`，不产生 I/O。

| 内部模式 | 含义 |
| --- | --- |
| `L` | 笔记本/普通模式，不接管 |
| `P` | 已进入帐篷区域，等待用户确认 |
| `T` | 已确认帐篷模式 |
| `S` | 支架模式 |
| `H` | 硬件平板模式，硬件优先 |
| `D` | 无可靠判断：样本失效或硬件状态未知 |

几何量由归一化重力向量计算：

- `pitch = atan2(-y, -z)`；当 `hypot(y, z) < 0.05` 时不可用。
- `roll = atan2(x, hypot(y, z))`。
- `flat = acos(clamp(-z))`；接近 180° 表示底座反向平放。
- 有效样本模长必须在 `7.5–12.0 m/s²` 之间。

默认阈值：

| 项目 | 进入 | 保持/退出 |
| --- | --- | --- |
| 稳定窗口 | 0.45 s，方向偏差 ≤ 8° | — |
| 姿态确认 | 0.6 s | 离开保持区 0.7 s 后回到 `L` |
| 帐篷确认区 | pitch 65–115°，\|roll\| ≤ 25° | 保持区 pitch 45–155°，\|roll\| ≤ 40° |
| 支架 | flat ≥ 155° | flat ≥ 140° |
| 传感器失效 | — | 超过 2.0 s 无有效样本进入 `D` |
| 帐篷免确认重进 | — | 确认后离开并在 5.0 s 内重进帐篷区 |

不变量：

- 无效样本不得更新 `last_stamp`、`last_geometry` 或稳定性锚点。
- 每次 `update()` 返回前都必须剪枝平滑历史；硬上限为 128 个样本。
- `hardware=True` 时进入 `H` 并清空稳定性证据，硬件状态不可被手动模式覆盖。
- `D` 状态产生空目标，撤销软件接管；没有传感器证据时不能维持伪平板状态。
- 手动模式不持久化；`tent_prompt` 偏好写入设置文件。

## 7. 硬件发现与 profile

### 7.1 机型门禁与 profile

- 只有 DMI `sys_vendor` 包含 `Panasonic`（大小写不敏感）才启用自动控制；其他
  机型保持 `supported=false`，UI 可打开但不能切换模式。
- profile 查找顺序：`/etc/panasonic-posture-manager/profiles/` →
  `/usr/share/panasonic-posture-manager/profiles/` → 源码树 `data/profiles/`。
- 支持精确 `vendor`/`product` 匹配与只匹配 `product` 的字符串简写；优先使用
  第一个有效的匹配文件。
- profile 版本当前为 1；`mount_matrix` 为行优先 3×3 正交矩阵。损坏或非法的
  profile 被记录并跳过，不阻止服务启动。
- 没有命中 profile 时使用 generic 能力发现；profile 中的名称只用于消歧，
  不能绕过总线、phys 和能力校验。

### 7.2 平板开关

- 只接管 capability 恰好包含 `SW_TABLET_MODE`、没有 `EV_KEY` 的 switch-only
  设备；lid/power 等复合开关不接管。
- relay 和 Python 发现逻辑必须排除自身虚拟设备：名称为
  `Panasonic posture managed switch`，phys 为
  `panasonic-posture-manager/relay`。
- 无 phys 的设备（例如部分 Intel HID 开关）是合法情况，不能误判为
  `read_error`。
- 开关状态为 `ok`、`absent`、`ambiguous`、`permission` 或 `read_error`；
  首次折叠前出现 `absent` 属正常现象。

### 7.3 内置键盘与触控板

- 只选择内置设备；USB、Bluetooth 和带外部 phys 的设备被排除。
- 键盘需具备字母键能力；触控板需为多点触控、非 DIRECT。
- 同一槽位出现多个匹配时拒绝猜测并报告失败；profile 名称仍要经过能力校验。
- relay 只读取能力位与开关状态，不读取按键内容；使用 `EVIOCGRAB` 持有设备，
  设备变化后重新发现并重试。

### 7.4 加速度计

- 默认寻找名称为 `accel_3d` 的唯一 IIO 设备。
- IIO 读取约 10 Hz；sysfs 回退读取约 2 Hz，并受 1.8 秒慢读 watchdog 约束。
- sysfs 活动时使用影子 IIO 试读，试读不会中断现有样本流；只有持续有效才切换。
- 传感器错误写入 `sensor_error`，不终止服务主循环。

### 7.5 只读探针

```sh
panasonic-posture-manager-probe > /tmp/posture-probe.json
```

开发树中无需安装：

```sh
PYTHONPATH=src python -m panasonic_posture.hardware.probe --probe
```

探针只读 sysfs 和 profile，不打开 `/dev/input`，输出 DMI、命中的 profile、
输入设备能力与 IIO 信息。收集机型适配报告时应附此 JSON。

## 8. D-Bus 契约

系统总线端点：

- bus name：`work.mmoe.panasonic.Posture`
- object path：`/work/mmoe/panasonic/Posture`
- interface：`work.mmoe.panasonic.Posture`

| 方法 | 签名 | 说明 |
| --- | --- | --- |
| `GetState` | `() → (s)` | 返回缓存状态 JSON；不推进 tick |
| `SetMode` | `(s) → ()` | `auto`/`laptop`/`tent`/`stand`/`tablet` |
| `Retry` | `() → ()` | 清除执行器/事务错误并重放当前目标 |
| `SetTentPrompt` | `(b) → ()` | 持久化帐篷提示偏好 |
| `ConfirmTent` | `(u) → ()` | 用 pending epoch 确认帐篷模式 |
| `SessionUpdate` | `(u b s s) → ()` | generation、ready、错误码、错误详情 |
| `StateChanged` | `(s)` 信号 | 状态 JSON 内容变化时发出 |

授权：

- 写方法要求调用者属于 seat0 上的活动本地图形会话，并通过 polkit action
  `work.mmoe.panasonic.Posture.configure`（`allow_active=yes`）。
- `SessionUpdate` 单独验证：sender uid、该 uid 的 per-uid 名称 owner、该 uid 的
  seat0 活动本地会话、sender 进程属于用户单元
  `panasonic-posture-manager-session.service`。
- `--session-bus` 仅为开发/集成测试模式，会跳过上述授权，不能作为生产边界。
- D-Bus 策略只允许 root own 主名称，允许用户 own
  `work.mmoe.panasonic.Posture.Session.*` 子树。

状态 JSON 版本为 2，核心字段：

| 字段 | 说明 |
| --- | --- |
| `instance` | 每次 daemon 启动生成的随机标识 |
| `generation` | 当前目标代次 |
| `pose` | 界面展示姿态：`laptop`/`tent`/`stand`/`tablet`/`unknown` |
| `hardware` / `hardware_status` | 物理开关有效值/未知与原始诊断状态 |
| `manual` | 当前手动模式；硬件平板态时回到 `auto` |
| `tent_prompt` | 帐篷提示是否开启 |
| `target` | 当前期望目标：mode、tablet、rotation、disable_internal_input |
| `applied` | 最近一次成功确认的目标，可能为 `null` |
| `pending_notification` | 等待确认的帐篷 epoch 或 `null` |
| `backend` | `ready`/`error`/`development`/`unsupported` |
| `error_code` / `error_detail` | 稳定错误码与内部诊断文本 |
| `session_connected` | 3 秒租约内是否收到适配器心跳 |
| `supported` | 是否为受支持的 Panasonic 机型 |
| `sensor_source` / `sensor_error` | `starting`/`iio`/`sysfs` 与回退原因 |
| `profile` / `model` / `vendor` | 命中 profile 与 DMI 信息 |

错误消息固定使用 D-Bus error name `work.mmoe.panasonic.Posture.Error`，正文格式
为 `<code>: <detail>`。错误码定义集中在 `protocol.ErrorCode`，覆盖权限、模式、
relay、传感器、显示与内部错误；UI 和会话适配器只本地化稳定 code。

## 9. relay 协议与行为

源码位于 `backend/`，采用 C11 编译，默认安装到
`/usr/lib/panasonic-posture-manager/relay`。stdin/stdout 是内部协议 v2：

stdin：

| 输入 | 行为 |
| --- | --- |
| `1` | 刷新 3 秒软件租约 |
| 换行 | 忽略 |
| 其他字节 | 协议错误，清理后以退出码 10 退出 |
| EOF | 释放软件请求并优雅退出 |

stdout 每行一个 JSON 对象：

```json
{"protocol":2,"kind":"ready","physical":true}
{"protocol":2,"kind":"physical","present":false}
{"protocol":2,"kind":"applied","hardware":false,"tablet":true,"input_disabled":true,"physical":true,"error":null}
{"protocol":2,"kind":"released","exit":0}
```

- `hardware` 是聚合前的物理开关状态；`tablet` 是物理或软件请求的聚合结果。
- `input_disabled` 表示内置键盘与触控板已成功抓取。
- `error="input-grab-failed"` 表示目标仍为平板，但输入抓取尚未成功；relay 继续
  运行并每秒重试。
- `physical:false` 表示尚未发现物理开关，不代表硬件损坏。

`--recover` 用于异常终止后的残留修复：先读一次物理开关，再创建虚拟设备并先发
ON、随后发最终状态（物理 ON→ON，OFF/缺席/不可读→OFF），最后销毁设备退出。
正常运行和 EOF 清理不需要额外 recover。

退出码：

| 码 | 含义 |
| --- | --- |
| 0 | 正常退出或恢复完成 |
| 2 | 参数错误或非 root 运行 |
| 3 | 物理开关不可读/歧义 |
| 4 | 无法创建 uinput 虚拟开关 |
| 5 | 物理开关存在但无法 grab |
| 8/9/10 | 写事件失败 / poll 异常 / stdin 协议错误 |

运行期每秒重新枚举开关，内置输入每 2 秒复核身份。物理开关暂时消失时会解除
monitor 并继续以软件请求运行；该期间仍排除 relay 自身创建的虚拟设备。

## 10. 会话显示与通知

### 10.1 显示事务

- 通过 `org.gnome.Mutter.DisplayConfig` 的 `GetCurrentState` /
  `ApplyMonitorsConfig` 修改内屏旋转。
- 内屏按 `is-builtin` 查找；镜像、未启用、多个内屏或会改变外接屏布局的场景
  拒绝执行并返回稳定错误码。
- 帐篷使用 180° 变换；笔记本、支架和软件平板使用 0°。软件平板先保持旋转锁
  转回 0°，再释放旋转锁交给 GNOME 自动旋转。
- 每次写入前先持久化恢复日志；失败时回滚，无法回滚时保留日志，下次启动或重试
  继续恢复。日志位于
  `$XDG_STATE_HOME/panasonic-posture-manager/display.json`。
- 旋转锁读写会校验返回值并回读确认；其他程序同时在改同一值时可能无法区分。

### 10.2 通知与锁屏

- 显示目标成功且 `pending_notification` 非空时，会话适配器显示帐篷提示。
- 通知动作 `default` 或 `tent` 都把当前 epoch 发给 `ConfirmTent`。
- 通知发送失败按 1/2/4 秒重试；同一 epoch 成功显示后不重复弹出。
- 锁屏时关闭提示；解锁后如果候选仍存在，会重新显示。

### 10.3 失败恢复

- 显示失败按 2–10 秒指数退避，允许用户通过 `Retry` 立即重放。
- 租约被拒绝时停止显示事务，等待重新认证或旧租约过期。
- 后台错误出现时，适配器先撤销自己持有的显示修改，避免留下错误方向。
- 适配器退出时尽力关闭通知并恢复显示；未完成的恢复由日志在下次启动时继续。

## 11. 设置与运行期数据

| 路径 | 所有者 | 内容 |
| --- | --- | --- |
| `/etc/panasonic-posture-manager/profiles/` | 管理员 | 机型覆盖 profile，优先级最高 |
| `/usr/share/panasonic-posture-manager/profiles/` | 包 | 内置 profile |
| `/var/lib/panasonic-posture-manager/settings.json` | 系统服务（root） | 持久化偏好，当前为帐篷提示开关 |
| `$XDG_STATE_HOME/panasonic-posture-manager/display.json` | 登录用户 | 显示旋转锁恢复日志 |

设置文件采用原子写入；损坏的显示日志会被隔离为 `.corrupt-<timestamp>`，不会阻止
会话适配器启动。手动模式不在磁盘上保存。

## 12. 配置窗口

UI 位于 `data/ui/`，由 GJS 和 GTK4/libadwaita 实现，入口为 `main.js`。

- 当前姿态页：插图、姿态名称、必要的错误/等待提示、帐篷确认和重试按钮。
- 设置页：帐篷提示开关与五种模式单选。
- `service.js` 负责系统总线代理、`StateChanged` 订阅与重连。
- `errors.js` 把稳定错误码转换为本地化用户文案，不显示内部 detail。
- 姿态插图只做展示去抖，不参与硬件决策。
- 界面文本使用 gettext；源字符串从 `po/zh_CN.po` 生成 `.mo`。

源码树下会从仓库 `data/` 和 `locale/` 读取资源；安装后使用
`/usr/share/panasonic-posture-manager/` 与系统 locale。`PANASONIC_POSTURE_UI_DIR`
和 `PANASONIC_POSTURE_LOCALE_DIR` 可用于开发覆盖。

改 UI 时保持这些约束：操作期间禁用对应按钮、设置以服务状态为准、错误不暴露内部
术语、交互控件有可访问性标签、关闭窗口不影响后台。

## 13. 构建与测试

```sh
make                     # 构建 backend/relay 与中文 .mo
make check               # C 测试 + compileall + unittest + D-Bus 集成 + i18n
make check-c             # 仅以 -Werror 构建并运行 relay 主机侧测试
make WERROR=1 check      # 发布/验收用，C 警告视为错误
make clean               # 清理构建产物与 __pycache__
```

测试分层：

- `tests/test_*.py`：标准库 unittest，覆盖策略、控制器、engine、设置、访问控制、
  传感器、硬件发现、显示、通知、日志和 UI 契约。
- `tests/integration/check_dbus.py`：在 `dbus-run-session` 下验证 D-Bus 方法、
  SessionUpdate、设置落盘与错误路径。
- `tests/integration/check_agent.py`：使用真实适配器状态机与假显示/通知后端，
  验证 apply → 心跳 → 清理流程。
- `tests/manual/`：需要真实安装、目标机和授权的手动测试。手动测试可能临时
  移除输入节点或注入进程故障，只能在目标机上运行；中断后可用
  `udevadm trigger` 或重启恢复设备节点。

开发运行：

```sh
PYTHONPATH=src python -m panasonic_posture.service.daemon --session-bus
gjs -m data/ui/main.js
```

只读硬件探针：

```sh
PYTHONPATH=src python -m panasonic_posture.hardware.probe --probe
```

修改 C 代码后至少运行 `make check-c`；提交验收前运行 `make WERROR=1 check`。
需要真实设备的行为必须同时补充手动验收记录，不能用 fake 测试代替。

## 14. 打包与发布

安装映射：

- wheel 提供 Python 包与两个命令：`panasonic-posture-manager`、
  `panasonic-posture-manager-probe`。
- `data/{ui,assets,profiles}` 安装到 `/usr/share/panasonic-posture-manager/`。
- `backend/relay` 安装到 `/usr/lib/panasonic-posture-manager/relay`。
- systemd、D-Bus、polkit、desktop、模块加载配置与翻译文件分别进入发行版标准目录。
- 包不写入 `/usr/local`；`package()` 会拒绝包含该路径的文本文件。

当前 `packaging/aur` 使用**固定 Git tag 的源码构建**，权威状态见
[packaging/aur/README.md](packaging/aur/README.md)。许可证已确定为
`AGPL-3.0-or-later`；发布前还必须解决：

1. 公开上游仓库，并推送 `v0.3.0` tag。
2. 在干净 Arch chroot 中完成 `make WERROR=1 check`、wheel 构建、`makepkg`、
   `namcap`、全新安装、升级、D-Bus 激活、会话启动和卸载验证。
3. 重新生成并复核 `.SRCINFO`、维护者和依赖列表。
4. 补齐 CFQV8-1L 的最终物理验收，并至少在一台非 CFQV8 机型上完成验收。

首次安装后，当前已登录的 GNOME 会话需要重新登录，或手动启动用户会话单元，之后
由图形会话自动拉起。完整打包说明与发布清单以 `packaging/aur/README.md` 为准。

## 15. 安全不变量

- root daemon 的 systemd 单元使用最小权限集合；relay 是唯一打开
  `/dev/uinput` 并 grab `/dev/input` 的组件。系统服务会以只读方式打开平板开关的
  event 节点来查询状态。
- relay 不读取按键内容，仅检查能力位、设备身份和开关状态。
- 自动发现与 profile 精确名称路径都必须排除外接 USB/蓝牙设备，并校验设备能力。
- 所有设备发现都要排除 relay 自建虚拟开关，防止反馈环。
- 写操作必须经过 logind 活动本地会话检查与 polkit；`SessionUpdate` 必须通过
  四重适配器身份验证。
- 手动测试的 root 助手不得使用固定 `/tmp` 路径或宽松权限；父进程使用私有临时
  目录，助手校验非符号链接、属主和权限。
- 用户可见错误只使用稳定 code 的本地化文案；内部 detail 仅进日志。

## 16. 已知限制与验收项

- **机型覆盖**：只有 CFQV8-1L 进行了成体系的实机验证，且最终物理验收尚未完成；
  其他 Panasonic 360° 机型要按 `docs/hardware-profiles.md` 检查传感器轴、
  mount matrix、开关、内置输入身份、休眠与 180° 触摸。
- **自动旋转**：硬件平板态与 GNOME/iio-sensor-proxy 的共存仍需在目标机上观察；
  关注 `sensor_source` 与原生旋转行为。
- **显示与休眠**：外接屏布局冲突、旋转锁被其他程序修改、Mutter 行为变化和唤醒
  后的恢复都需要真机回归。
- **多用户**：快速切换时旧适配器租约可能需要最多 3 秒过期；新适配器会短暂
  收到 `access-denied`。
- **手动模式**：后台服务重启后回到自动模式，这是刻意设计，不是持久化故障。
- **发布**：公开仓库与 `v0.3.0` tag、clean-chroot 验收、CFQV8-1L 最终物理验收
  和非 CFQV8 机型验收仍未完成，在此之前不得发布 AUR 包。

## 17. 修改检查清单

修改代码前先确认影响面：

1. 是否改变 `protocol.py` 中的 D-Bus、relay、状态或错误码契约？若是，Python、C
   和文档必须同步更新。
2. 是否改变姿态阈值、租约或重试时序？更新策略测试与本文时序表。
3. 是否改变设备发现规则？同时更新 Python、C、探针与 `docs/hardware-profiles.md`。
4. 是否改变用户可见行为？更新 README；UI 文案更新 `po/zh_CN.po` 并重新生成 `.mo`。
5. 是否影响显示、通知或恢复日志？补对应单元测试与故障路径测试。
6. 是否影响打包文件、权限或安装路径？更新 `packaging/` 和发布检查清单。

提交前运行：

```sh
make WERROR=1 check
```

涉及真实硬件的改动还需在目标机完成相应手动验收，并在
`docs/hardware-validation.md` 记录日期、机型和结果。
