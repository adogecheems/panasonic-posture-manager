# 硬件验证记录

> 文档说明（2026-11-02）：本文件按时间保留从 0.2.x 到 0.3.0 的硬件试验证据，
> **不是发布验收清单**。2026-10-31 及以前的内容属于旧实验后端；2026-11-02
> 的内容反映当时源码和临时安装包，但最终发布验收仍未完成。
> - 旧记录的模块路径（`service/*.py`、`session/*.py`、`ui/*.js`）与设备编号已
>   不适合 0.3.0；设备编号也会随启动变化，不应作为固定引用。
> - 当前 profile 格式、探针字段和验收清单见
>   [hardware-profiles.md](hardware-profiles.md)，实现契约见 [agent.md](../agent.md)，
>   用户行为见 [README.md](../README.md)。
> - CFQV8-1L 的最终物理验收以及 QV9/QV1 同族回归仍需按发布清单完成；最新状态见
>   [packaging/aur/README.md](../packaging/aur/README.md)。

历史记录：2026-10-31（Asia/Shanghai），机型 CFQV8-1L。以下各节来自旧实验后端。

## 已有证据

- 本机 Shell 资源中的 keyboard.js：自动软键盘启用条件包含 seat.get_touch_mode() 与最近输入设备是触摸屏；辅助功能强制启用是另一条条件，测试没有修改它。
- 用户实测反馈：虚拟 SW_TABLET_MODE 开启后，触摸文本框可使用 GNOME 软键盘；内置键盘仍能使用；没有方向锁时会自动旋转。
- 因此虚拟开关可作为 GNOME 触摸体验的候选入口，但不能据此声明内置输入停用已实现。
- 方向锁测试在 2026-10-31 01:10:20–01:12:00 完成：ON 45 秒、OFF 10 秒、ON 45 秒、发送 OFF、删除设备，辅助程序退出码 0。日志时间为 UTC，文档换算为 Asia/Shanghai。
- 测试结束读取 orientation-lock=false。独立程序测试证明 rotation-guard.py 在持有期间写入 true、管道 EOF 后恢复先前值。
- 本轮只记录到 HARDWARE 0，未观察到硬件 ON/OFF 交接。不能声称折叠展开验证通过。

## 尚待用户观察确认

- 锁定方向期间，实际画面是否保持不变，触摸文本框是否仍会弹软键盘。
- 真正完整折叠、展开后，软件触摸状态是否继续有效。
- 内置触摸板实际行为、外接输入行为。

## 已知实现限制

Mutter 的开关事件处理直接赋值 tablet_mode_switch_state；两个开关不构成自动逻辑或。现有试验故意不在硬件 OFF 后强行重发虚拟 ON，以暴露这一问题。相同 ON 写入也可能被输入层去重，不能当作修复。

内置键盘停用需独立验证可恢复的后端。禁止把屏幕键盘可用、虚拟开关输出 ON、内置输入停用三者混为一项成功状态。

方向锁试验只保持测试开始时的方向，没有验证固定 180°/0°、多屏布局与触摸坐标。

rotation-guard.py 是测试用独立进程：UI 退出时通过管道 EOF 恢复，并有 180 秒上限。若守护进程本身被 SIGKILL，仍不能保证恢复。对同值的用户设置修改无法检测。此实现不能直接代替正式后台的恢复日志与会话生命周期管理。

## 用户补充实测结果

2026-10-31：用户确认方向保持、软键盘正常；在第二次软件平板开启期间，完整折叠再展开后软键盘失效。

结论：软件平板与方向锁可组合使用；当前物理/虚拟双开关方案未通过交接验收，不能直接部署为正式后端。失效表现与 Mutter 最后收到的 OFF 覆盖软件 ON 一致，但本轮辅助日志未捕获硬件变化，事件路径尚未被直接证实。后续需同时观察内核硬件事件、虚拟输出和 GNOME touch-mode，不能把推断写成已捕获的事实。

下一阶段应验证唯一有效开关来源或消费者处显式聚合：有效目标为硬件 ON 或软件请求 ON。任何代理物理开关的实现须独立验证崩溃释放、退出恢复、休眠唤醒与启动时的状态同步；此前不部署屏蔽物理开关的常驻规则。

## 捕获到完整交接（2026-10-31）

probe-result-handover.log 已捕获：软件 ON（elapsed=0）→ 物理 ON（18 秒）→ 物理 OFF（33 秒）；期间程序未发送软件 OFF，用户反馈展开后软键盘失效。本轮在约 73 秒结束并清理。物理事件与状态查询均记录到了 ON/OFF，弥补前轮缺少事件证据的问题。

用户提出是否因内置输入设备仍可用而禁止软键盘。对照本机 GNOME Shell 51.1 资源中的 KeyboardManager：

- 自动启用条件为 touch-mode 为真，且最近的非键盘输入设备为触摸屏。
- last-device-changed 中明确忽略 KEYBOARD_DEVICE，键盘存在或可用并不是这里的否决条件。
- 鼠标/触摸板成为最近设备时可使自动条件不成立；重新触摸文本框满足触摸输入这一条件，但仍需要 touch-mode。
- 结合本轮物理 OFF 事件及已检查的 Mutter 赋值逻辑，最符合证据的是物理 OFF 覆盖了有效 touch-mode。没有直接采集 Shell 内部属性，尚不把该内部值当作直接测量结果。

正式方案仍需解决开关聚合；只停用内置输入不能修复被覆盖的 touch-mode。辅助功能强制启用软键盘可绕过自动条件，但不等于完整的 GNOME 平板状态，也不作为此项目默认修复。

## 聚合试验通过一次正常交接

2026-10-31：用户反馈“行为非常正确”。aggregate-result.log 显示：接管物理开关成功；虚拟 ON；16 秒捕获物理 ON；31–32 秒捕获物理 OFF；随后保持软件 ON；约 57 秒结束，输出 OFF、释放物理接管、删除虚拟设备，退出码 0。退出后设备列表无本程序的虚拟开关。

结论：本机上，通过临时接管物理开关使 GNOME 只收到聚合后的有效状态，正常折叠展开路径得到实测支持。用户反馈涵盖本轮要求的交接与结束恢复观察。当前仅证明一个软件请求持续 ON 的正常路径；未完成硬件 ON 时撤销软件请求、软件 OFF 时硬件独立运行、崩溃、休眠、多次循环及设备重发现验收。内置键盘/触摸板停用仍未验收。可以据此推进可替换聚合后端，但尚不安装为生产常驻。

## 合并状态组合验收通过（正常路径）

2026-10-31：用户反馈“行为正确”。matrix-result.log 确认：

1. 硬件 OFF、软件 ON → 输出 ON。
2. 硬件 ON、软件 ON → 保持 ON。
3. 硬件 ON 时撤销软件请求 → 输出仍 ON，没有发送 OFF。
4. 硬件 OFF、软件 OFF → 输出 OFF。
5. 硬件 OFF 时重新启用软件请求 → 输出 ON。
6. 约 46 秒完成、发送 OFF、释放物理接管、删除虚拟设备，辅助程序退出码 0。退出后设备列表无试验虚拟设备。

用户确认这些操作的可见行为正确。正常路径聚合规则得到本机实测支持，可以继续工程接线，无需重复同一软键盘验收。异常终止、休眠与输入停用仍是独立未完成项。

## 内置输入合并验收通过（正常路径）

2026-10-31：用户反馈“正确”。input-result.log 捕获：软件 ON 时 INTERNAL_INPUT disabled；物理 ON 后撤销软件请求仍保持平板；展开至物理 OFF 后 INTERNAL_INPUT released、虚拟 OFF；软件重新 ON 后重新停用内置输入；结束时释放内置输入、输出 OFF、释放物理开关、删除虚拟设备，退出码 0。退出后设备列表未见试验虚拟设备。

用户确认本轮请求的操作行为正确。已实测支持正常路径的内置输入停用与恢复、GNOME 软键盘及开关聚合组合。是否实际连接外接键鼠测试没有单独记录，不能据一句反馈宣称外接设备实测覆盖；代码按非 USB/Bluetooth 的严格设备身份限定。

下一步将同一执行后端接入服务事务，不再重复正常组合测试；异常终止（含 SIGKILL）、休眠唤醒、设备重发现、显示旋转/触摸映射和系统安装权限仍未完成。

## 2026-10-31 10 Hz 传感器接口探测与接入

- Panasonic ACPI 设备：`MAT0019` = HKEY（`panasonic_laptop`）、`MAT0028` = WLSW、`MAT0024` = PS/2 键盘、`MAT0040` = MIS2（无驱动）。没有发现独立的 Panasonic 加速度计 ACPI 接口。
- 加速计来自 Intel ISH HID sensor hub：`iio:device5`，`name=accel_3d`，`in_accel_sampling_frequency=10.000000`，三轴类型 `le:s32/32>>0`，单样本 12 字节，trigger 为 `accel_3d-dev5`。
- IIO 缓冲区只读探针：启用 x/y/z scan、length=64、watermark=1 后，约 1 秒读取 20 个样本；HID 每次成对推送，两个样本相同，实际约 10 Hz 独立样本。样本按 `<iii` 解析后乘以 `in_accel_scale`（9.806e-06）得到 g。
- 带 `in_timestamp` 的扫描布局按 20 字节读取返回 `EINVAL`，说明内核侧有额外对齐；正式服务不启用 timestamp，只用读取时刻。
- `hid_sensor_custom` 绑定 18 个 `HID-SENSOR-2000e1.*` 平台设备并创建 19 个 `/dev/HID-SENSOR-2000e1.*` 字符节点；只读探测 0.7 秒内没有数据，不能作为姿态源。
- 结论：正式服务优先使用 `accel_3d` 的 IIO 缓冲区，失败回退 sysfs；`GetState.sensor_source` 标记 `iio`/`sysfs`。

## 2026-11-02 采集重构的真机基线

本节记录源码重构期间在 CFQV8-1L 上的只读/基线测量，**不把它当作新包已安装的
最终验收**。当时系统仍运行已安装的 0.3.0-3 服务，源码修改要通过重新构建、安装和
重启服务后才进入生产路径。

- DMI 仍为 `Panasonic Corporation` / `CFQV8-1L`；探针解析到 profile `cfqv8`、
  21 个 input 设备、9 个 IIO 设备，加速度计为 `iio:device6`（`name=accel_3d`）。
- 对 `in_accel_{x,y,z}_raw` 连续串行读取 12 次：min 0.564 s、median 0.660 s、
  max 0.660 s。旧实现的 0.8 s watchdog 会频繁误判，因此源码已把阈值调到 1.8 s，
  并在每读完一个轴后检查超时，避免继续读取已过期的剩余轴。
- 同一时刻完整 `query_switch()` 30 次：min 8.38 ms、median 12.17 ms、
  max 17.58 ms；本机当前没有物理平板开关候选（`hardware_status=absent`）。
  新 `SwitchReader` 正常 tick 只复用已验证 event 节点，每秒才全量发现一次。
- 直接运行源码中的 `SensorManager`（`prefer_shared=True`，只读 sysfs）30.3 s：
  收到 45 个新样本（1.49 Hz），`sensor_error=None`；同线程轮询 `SwitchReader`
  297 次，开关缓存无候选，进程 CPU 时间 0.393 s。该运行不占用 IIO buffer，也不
  修改系统服务或硬件状态。
- 对当时已安装的 0.3.0-3 服务运行 `tests/manual/check_live.py`：帐篷、支架、
  平板与自动模式依次成功应用，relay 每次 grab 后正常释放，脚本结束恢复
  `manual=auto`、`hardware=false` 且方向锁为 `false`。这次覆盖的是已安装旧二
  进制；新源码仍需重装后重复同一事务与长时间采集观察。
- 已安装旧服务日志仍可见 sysfs/IIO 交替回退，说明“运行中进程”不能证明新采集
  节流、1.8 s watchdog 或 shadow IIO trial 已生效；这些改动由单元测试和
  `dbus-run-session` 源码集成覆盖，仍需重装服务后观察数分钟。
- 同机 `make check` 在源码树通过：509 项 Python unittest、relay C 协议测试、
  session-bus D-Bus 集成与 i18n 检查。真机物理折叠/展开、帐篷通知点按、180°
  触摸、带外接显示器的旋转以及休眠唤醒仍需人工操作确认。

## 2026-11-02 重构收尾验证

重构最后把会话层拆为 `session/agent.py`（总线身份/GLib 进程壳）与
`session/runtime.py`（显示/租约/通知状态机），并新增真实适配器的隔离总线端到端
检查。以下证据来自当前源码与当前 wheel，仍不是已安装特权服务的验收。

- 源码探针与暂存 wheel 探针均返回只读 JSON v1：DMI `Panasonic Corporation` /
  `CFQV8-1L`，profile `cfqv8`，21 个 input、9 个 IIO、0 个平板开关候选；
  两次退出码均为 0。
- 当前源码 `SensorManager(prefer_shared=True)` + `SwitchReader` 连续运行 15.14 s：
  22 个新样本（约 1.45 Hz），来源 `sysfs`，`sensor_error=None`；开关轮询 150 次，
  无候选节点，结果 `False/absent`。该轮为只读，不占用 IIO buffer，也不改变服务、
  显示或硬件状态。
- 干净源码树运行 `make WERROR=1 check` 通过：509 项 Python unittest、relay C 协议
  测试、两组 `dbus-run-session` 集成和 i18n 53/53。新增 `check_agent.py` 让真实
  `Agent`/`SessionRuntime` 在隔离总线上完成 `GetState` → 假显示 apply →
  `SessionUpdate` 续租 → 退出时关闭通知/恢复显示；不接触 Mutter、通知服务、系统
  总线或 root relay。
- 当前系统仍运行安装版 0.3.0-3 服务（daemon/用户适配器进程未替换）。因此真实
  relay、物理开关折叠/展开、帐篷通知点按、180° 触摸、外接屏旋转和休眠唤醒仍需
  授权重新构建/安装并重启两个 unit 后做最终人工验收。

## 2026-11-02 已安装修复版真机验证

本节记录本机临时验证包 `0.3.0-5` 的实际安装结果。它当时只用于验证工作树，
不是 AUR 发布包。许可证现已确定为 `AGPL-3.0-or-later`；固定 Git tag 使用
`sha256sums=('SKIP')` 是正常配置。当前发布阻断项是公开仓库与 `v0.3.0` tag、
clean chroot 验收、元数据复核，以及 CFQV8-1L 最终物理验收和其他机型验收；
最新状态以 [packaging/aur/README.md](../packaging/aur/README.md) 为准。

- 安装包中的 `service/sensor.py` 与工作树 SHA-256 完全一致；daemon 重启后加载
  `sysfs` 回退路径，用户适配器租约自动恢复，`backend=ready`、`session_connected=true`。
- `make WERROR=1 check` 在干净树通过：514 项 Python unittest、relay C 协议测试、
  两组 `dbus-run-session` 集成和 i18n 53/53。relay 另通过 ASan/UBSan，四个 C
  源文件通过 `gcc -fanalyzer`。
- 首次安装后观察到 IIO/sysfs 来源在约 0.1–1.6 秒间隔反复翻转，IIO buffer
  `enable` 同步启停。根因是 shadow trial 辅助线程完成时自行清除了 `_trial`：
  采集线程若在两次 sysfs 读取之间没有及时取回结果，就会丢失成功交接或失败退避，
  随即再次启动 trial。修复后 trial 结果由采集线程独占消费，取消路径也会关闭
  尚未消费的已交接源。
- 修复版连续观察 100 秒：IIO 仅在相对时刻 44.372 s 试用一次，44.625 s 交接为
  `iio`，47.414 s 因 firmware 无后续样本报错，49.189 s 回退 `sysfs`；整个窗口
  只有这两次 `buffer/enable` 变化，没有快速循环。另一次 140 秒观察未再出现
  0.1–1.6 秒级来源翻转；journal 中后续试用间隔约 60–65 秒。
- 本机 IIO firmware 仍表现为“试用能拿到首个样本、随后约 2 秒无样本”，因此
  稳态通常停留在 sysfs；这是已设计的健康回退，不是采集线程停止。sysfs 恢复后
  `sensor_error` 清空，姿态继续更新。
- 修复版 `tests/manual/check_live.py` 通过：帐篷、支架、平板、自动模式依次应用，
  relay 每次 grab 后释放，最终恢复 `manual=auto`、`hardware=false` 和方向锁关闭。
  `tests/manual/check_faults.py` 也通过：relay 被杀后报告 relay 错误并恢复，
  daemon 被 SIGKILL 后由 systemd 重启并回到自动笔记本模式。
- 当前仍无物理平板开关候选（`hardware_status=absent`）。物理折叠/展开、帐篷通知
  点按、180° 触摸坐标、外接显示器布局和真实休眠/唤醒仍需人工操作确认；
  `check_absent_switch.py` 在本机没有可移除的 switch 节点，不作为新增证据。

## 2026-11-02 帐篷模式静态误退出的根因修复

- 现象：帐篷模式保持不动时偶发释放 relay、退出帐篷并重新出现确认提示。服务
  journal 显示释放时刻与约 60 秒一次的 IIO shadow trial 对齐。
- 根因：本机 firmware 在 IIO 试读中可能先返回 1 个样本、随后静默约 2 秒。旧
  trial 在这一帧后立即把采集线程从 sysfs 切到 IIO，sysfs 样本流中断超过策略
  stale 窗口；策略释放软件接管。恢复后若已超过 5 秒免确认重进窗口，就会重新
  生成帐篷确认。无需真实姿态变化即可触发。
- 修复：IIO trial 不再以“首个样本”为交接条件，必须至少取得 2 个样本且样本
  跨度达到试读窗口一半；不健康 trial 只记录失败并保留 sysfs 连续采样，不发布
  试读样本，也不改变 `sensor_source`。
- 回归覆盖：`tests/test_sensor.py` 新增“单样本后静默”场景，验证 sysfs 样本流
  无中断、来源保持 `sysfs`、失败试读不产生 `sensor_error`。当前修复仍需重新
  构建/安装服务后在真机观察至少数个 60 秒重试周期。

## 2026-11-02 修复版仍复现：切换后的 IIO 再次静默

- 上述 `0.3.0-7` 真机验证仍复现。journal 在 `23:08:31`、`23:10:43` 记录
  `IIO: accel_3d produced no samples`，同一秒 relay 报 `stdin hangup` 并释放；
  `23:08:36` relay 重新取得，符合“退出后又要求确认/自动恢复”的用户现象。
- 第二次根因：持续试读窗口只能证明试读当时健康。本机 IIO 数据流在交接后仍可能
  再静默；一旦停止超过 2 s stale 窗口，策略仍会释放软件接管。仅在 trial 结束时
  加严样本数无法保证后续持续可用。
- 修复：软件姿势接管处于 `P`（待确认）、`T`（帐篷）、`S`（支架）或 target 为
  软件平板时，engine 将传感器设为 `prefer_shared`，固定使用持续可读的 sysfs，
  不启动周期 IIO 抢占；硬件平板态保持同样策略以让 iio-sensor-proxy 使用 buffer。
  回到普通笔记本姿态后才恢复 IIO 优先。
- 回归覆盖：`tests/test_engine.py` 验证软件帐篷/待确认使用 sysfs，回到普通姿态后
  恢复 IIO 优先；`tests/test_sensor.py` 继续覆盖 shadow trial 与 sysfs 连续采样。
- `0.3.0-8` 安装后从 `23:17:46` 至 `23:20:46` 保持真实 relay 帐篷目标 3 分钟：
  每 15 s 采样均为 `target/applied.mode=tent`、`sensor_source=sysfs`、
  `pending_notification=null`、`error_code=null`；窗口内只有一次 relay 启动，
  没有 IIO 试读或 relay 释放。`23:20:47` 恢复自动模式时才释放 relay；
  恢复前后的 IIO 警告仅在普通笔记本姿态出现，不再影响帐篷目标。
- 打包内 `engine.py`/`sensor.py` 与工作树 SHA-256 一致；完整 `make check` 通过
  523 项 Python unittest、relay C 测试、两组 D-Bus 集成和 i18n 49/49。
