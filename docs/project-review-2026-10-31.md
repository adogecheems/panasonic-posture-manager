# 全项目检阅报告

> **历史存档（仅用于追溯 v0.3.0 重构的动机与决策）。** 本报告针对 **0.2.1**（重构前）
> 代码，其中的文件路径、行号与模块布局在 0.3.0 中已全部变化；当前实现细节请看
> [agent.md](../agent.md)，用户文档请看 [README.md](../README.md)。
> 0.3.0 重构已逐项处理本报告标记的 P1/P2，并保留历史记录以便对照。

- **项目**：松下二合一平板电脑姿态管理工具（Panasonic 360° 翻转本 · GNOME Wayland）
- **检阅对象**：工作区全部 62 个文件，约 3,700 行代码（Python 系统服务 / C 特权执行器 / GJS+libadwaita UI / systemd+D-Bus+polkit / AUR 打包 / 文档）
- **版本**：0.2.1（2026-10-31）
- **检阅日期**：2026-10-31
- **方式**：4 个模块并行只读深度检阅 + Lead 独立架构/跨模块核验 + 自动构建/测试/静态分析/针对性复现实验；**未修改任何项目源码、测试或既有文档**（本报告为唯一新增文件）

---

## 一、结论摘要

```
P0（阻断/可直接利用）     : 0
P1（高，需优先修）        : 5   （其中 1 项为发布阻断）
P2（中）                  : 25
P3（低/打磨）             : 30+
文档—实现不一致           : 22 条
```

**总体判断**：这是一个**模块划分清楚、工程完成度较高、文档极其详尽**的项目。D-Bus 契约、状态机/代次确认协议、执行器心跳与开关聚合、显示恢复日志主路径、GTK4/libadwaita 用法均经核对正确；`make check` 的 58 项单测 + D-Bus 检查全部通过，C 代码 `-fanalyzer` 零告警。风险并不在主干设计，而集中在四类边界：

1. **generic profile（非 CFQV8 机型）路径不可用**——程序会把自己的 uinput 虚拟开关当作硬件开关，这是与本项目“其他 Panasonic 机型走通用适配”卖点直接冲突的 P1；
2. **daemon 级别的健壮性**——`refresh()` 缺少顶层异常隔离，单次异常即永久停掉 100 ms 状态机；硬件平板模式下策略队列内存无界增长；
3. **会话/显示事务的边界**——租约丢失后的高频 apply/restore、通知状态机、恢复日志损坏、平板模式锁/旋转顺序等；
4. **发布就绪度**——无 LICENSE、AUR 占位 URL/SKIP 校验和、打包硬化与依赖声明待补（其中前三项作者已在 `packaging/aur/README.md` 自述为 release blocker，但顶层文档未讲清）。

---

## 二、项目概览与架构评价

### 2.1 组成

| 层 | 文件 | 职责 |
| --- | --- | --- |
| 决策核心 | `service/controller.py`、`service/policy.py`、`service/presentation.py` | 手动/硬件优先、姿态稳定与迟滞、界面去抖（纯逻辑，无 IO） |
| 系统服务 | `service/daemon.py`、`service/actuator.py`、`service/session_access.py` | root D-Bus 服务、100 ms tick、租约/代次事务、logind+polkit 授权 |
| 硬件层 | `backend/relay.c`、`service/hardware_reader.py`、`service/iio_reader.py`、`service/hardware_profile.py` | 开关/输入能力发现与 grab、uinput 虚拟开关聚合、IIO/sysfs 传感器、DMI profile |
| 会话层 | `session/agent.py`、`session/display.py` | 无窗口用户适配器：租约心跳、通知确认、Mutter 显示事务与恢复日志 |
| UI | `ui/main.js`、`ui/illustration.js`、`po/zh_CN.po` | libadwaita 两页窗口、SVG 姿态图、中英文 |
| 集成/打包 | `packaging/**`、`Makefile`、`tests/**`、README/INSTALL/docs | systemd/D-Bus/polkit/AUR、58 项测试、手动集成测试 |

### 2.2 做得好的地方

- **职责边界清晰**：决策（controller/policy）与执行（daemon/actuator/relay）、特权面（root）与非特权面（session agent、UI）分离；UI 关闭后服务继续运行。
- **事务语义严谨**：`Target/generation/applied/acknowledge` 的“计算目标 ≠ 成功”模型正确；过期完成回调被拒绝，失败不会冒充成功；`tests/test_controller.py`、`test_feedback.py` 覆盖到位。
- **执行器契约对齐**：relay 的 3 s stdin 租约、1 s 心跳报告、`applied` JSON 字段与 `Actuator.keep/_accept/_exited` 完全一致；旧进程回调用 process identity 隔离。
- **安全设计方向正确**：switch-only 判定（无 `EV_KEY`）、内置键鼠排除 USB/蓝牙、grab 前检查按键、polkit+logind 活动本地会话校验、systemd `ProtectSystem/NoNewPrivileges/空 CapabilityBoundingSet`。
- **显示恢复设计合理**：只改内屏、拒绝镜像/外屏重排、journal 先写后改设置、适配器重启按所有权恢复；fake-bus 验证三种崩溃窗口恢复路径正确。
- **C 代码质量**：`-Wall -Wextra -Werror` 干净、`gcc -fanalyzer` 0 告警；无越界/整数溢出/资源泄漏；信号处理仅写 `volatile sig_atomic_t`；SIGKILL 下内核释放 grab / 销毁 uinput；`backend/relay` 与源码重编 SHA256 一致。
- **文档质量突出**：README/INSTALL/AUR README/docs 对行为、边界、已知限制、待人工验收项记录得比多数同类项目更完整；release blocker 有明确自述。
- **i18n 与资产**：当前 UI+通知 36/36 去重字符串全部有中文翻译；5 pose × 主题 × 高对比共 20 组 SVG 全部成功栅格化为 640×480。

### 2.3 风险画像

| 风险领域 | 说明 |
| --- | --- |
| generic 机型路径 | 自建虚拟开关被当硬件；文档承诺“未知 Panasonic 机型可用”不成立 |
| daemon 韧性 | 无顶层异常保护、无日志、主循环可被同步 D-Bus/子进程阻塞数秒 |
| 会话事务 | 租约身份/丢失处理、通知状态机、恢复日志 schema、平板模式时序 |
| 传感器生命周期 | IIO→sysfs 单向降级；ClaimAccelerometer 与直接读 buffer 的竞态/语义 |
| 发布就绪 | LICENSE、AUR 占位符、systemd 硬化、依赖声明、clean-chroot 构建未跑 |

---

## 三、验证与复现记录

### 3.1 自动构建与测试

| 项 | 命令 | 结果 |
| --- | --- | --- |
| 构建 + 单测 + D-Bus | `make check` | **58 tests OK**；`dbus-run-session ... check_dbus.py` → `D-Bus checks passed`；exit 0 |
| C 编译 | `gcc -std=c11 -D_DEFAULT_SOURCE -O2 -Wall -Wextra -Werror` | 0 警告 |
| C 静态分析 | `gcc -fanalyzer -c backend/relay.c` | 0 告警 |
| 产物一致性 | 重编译 relay 与仓库 `backend/relay` SHA256 | 完全一致（无脏产物） |
| 测试数复核 | `grep -c 'def test_' tests/test_*.py` | 58，与 README/INSTALL 声称一致 |

### 3.2 集成/静态校验

| 项 | 结果 |
| --- | --- |
| `desktop-file-validate`（两份 desktop） | 均 exit 0、无警告 |
| `systemd-analyze verify`（两 unit，按 PKGBUILD 规则 sed 后） | 均 exit 0、无警告 |
| `systemd-analyze security --offline` | 系统服务 **5.5 MEDIUM**；用户会话服务 **9.2 UNSAFE** |
| `.SRCINFO` ↔ `PKGBUILD` | `makepkg --printsrcinfo` diff 无差异；`bash -n` 全部脚本通过 |
| gettext | `msgfmt --check-format`/`--check` 通过；51 条已译、0 fuzzy、当前代码字符串 36/36 有翻译；15 条 msgid 已无引用 |
| GJS UI | broadway 离屏构建两页并 `present()` 成功，无 GTK4/libadwaita API 误用；真实 `gjs -m ui/main.js` 在无显示时按预期失败 |
| 插图管线 | 5 pose × 2 主题 × 2 高对比 = **20/20** 成功渲染 640×480×4ch |
| 接口契约 | bus name、对象路径、接口、5 方法签名、polkit action、StateDirectory、gettext/assets/profiles 路径、desktop application_id 全部一致 |

### 3.3 Lead 针对性复现（关键证据）

```text
① H 模式 history 无界：Policy().update(..., hardware=True) × 30000 样本
   → len(history)=30000（脱离 H 模式后首样本即回到 1）
   按 IIO 约 20 样本/s ⇒ 约 16.5 MiB/h（root 常驻服务无 MemoryMax）

② GLib timeout 异常：PyGObject 回调 raise 一次后
   → 该 timeout source 被移除，后续不再回调，主循环继续运行
   ⇒ daemon.refresh 任一未捕获异常会永久停掉 100 ms 状态机且 systemd 不重启

③ sysfs capability 位图：event2 的 keys =
   "402000007 ff803078f800d001 feffffdfffcfffff fffffffffffffffe"
   → hardware_reader._bits() 返回 0（int(...,16) 遇空格失败）
   → 正确解析 = 152 个键位（KEY_A/KEY_Z 均置位）
   ⇒ 探针 --probe 把真实键盘 keys 报成 0；“无 EV_KEY”安全过滤对复合设备 fail-open

④ 帐篷提示确认范围：保持 P 模式后移动到 46°/60°/120°/150°
   → confirm() 均为 True（文档定义帐篷区为 65–115°）

⑤ 环境/设备只读核验：DMI = Panasonic CFQV8-1L；accel_3d 在 iio:device1（非文档写死的 device5）；
   /sys/class/input 中当前无 Intel HID switches（首次折叠前状态）；
   本沙箱无 /dev/input、/dev/uinput，包未安装，relay root 路径无法实机执行
```

### 3.4 限制

本机是 CFQV8-1L，但检阅运行在受限沙箱中：**无 `/dev/input`、无 `/dev/uinput`、uinput 未加载、包未安装、工作区无 `.git`**。因此涉及真实硬件、GNOME 会话、systemd 运行时的结论均标注“待实机验证”，不把代码推导当作已捕获事实。

---

## 四、发现总表

> 严重级别：P0 = 阻断/可直接利用；P1 = 高（影响主功能或安全边界，应优先修）；P2 = 中；P3 = 低/打磨。
>
> “待验证”= 代码/实验高置信，但受环境限制未在真实硬件或真实 GNOME 会话复现。

### 4.1 P0

**无。**

### 4.2 P1（5）

| ID | 位置 | 问题 | 状态 |
| --- | --- | --- | --- |
| P1-1 | `backend/relay.c:45-60,67-94,100-123,341-361,378-421,485-491`；`service/hardware_reader.py:17-32,52-56`；`service/daemon.py:211,223-242` | **generic profile 自检测**：relay 与 daemon 都不排除本程序创建的 uinput 虚拟开关（`Panasonic posture managed switch`，仅 EV_SW/tablet 位）。无 `--switch-name` 的 generic profile 下：物理开关存在 → relay 匹配 2 个设备 → exit 5/6，软件模式全不可用；物理开关缺席（文档明示的“首次折叠前”正常状态）→ 把自身输出当硬件读回，形成 ON 反馈环 / hardware 翻转振荡。CFQV8 profile 仅因名称过滤而侥幸避开 | 高置信，待实机 |
| P1-2 | `service/daemon.py:207-277,320`；`service/hardware_reader.py:19-22`；`service/actuator.py:92-99` | **refresh 无顶层异常隔离**：`GLib.timeout_add(100, self.refresh)` 回调内任何未捕获异常（非 UTF-8 设备名、`emit_signal` 在总线关闭竞态、`Actuator.release()` 内层二次异常等）都会让 GLib 移除周期源 → 控制循环永久停摆、`Restart=on-failure` 不触发（进程未退出） | 已复现 GLib 行为 |
| P1-3 | `service/policy.py:111,125-129,182-183` | **H（硬件平板）模式 history 无界增长**：样本在 `:111` 入队，`hardware is True` 分支在 `:125-129` 提前 return，唯一 prune 在 `:182-183` 之后才执行。360° 折叠成平板是长时间常态，实测约 16.5 MiB/h，root 服务无 MemoryMax | 已复现（30000 条） |
| P1-4 | `tests/manual/absent_switch_root.sh:11-14,32`；`tests/manual/check_absent_switch.py:22,66-70,72` | **手动测试 root 助手 /tmp 符号链接竞态**：固定 `/tmp/posture-absent-test` + `chmod 0777` + `touch`，跟随符号链接；管理员运行该手动测试并 pkexec 授权后可被本地攻击者预置 symlink 用于把任意目录改成 0777（经典本地提权链）。触发条件受限（需管理员主动运行测试） | 已确认代码路径 |
| P1-5 | 根目录无 `LICENSE/COPYING`；`packaging/aur/PKGBUILD:1,12-18,33-38`；`.SRCINFO:5,19-20`；`README.md:114-122`；`INSTALL.md:13-21` | **发布就绪阻断（作者已自述）**：无 LICENSE 而 `license=GPL-3.0-or-later` 为临时占位；`url/source` 为 `example.invalid`、`sha256sums=SKIP`、`Maintainer: TODO`。顶层 README/INSTALL 虽提示“先替换”，但未明确 AUR 仍是 skeleton；clean-chroot 构建从未跑过 | 文档已自述，未解决 |

### 4.3 P2（25）

| ID | 位置 | 问题 |
| --- | --- | --- |
| P2-1 | `backend/relay.c:313-333`；`packaging/panasonic-posture-manager.service:10` | `--recover` 语义缺陷：折叠（物理 ON）状态下 systemd `ExecStopPost` 的 recover 不带 profile 名称，创建虚拟设备后二次读硬件数到自己（present=2）→ **反向发 OFF**；开关设备缺席（`present==0`）时 recover 直接空操作，不修复 GNOME 残留平板态 |
| P2-2 | `service/hardware_reader.py:9-14,29-32`；`service/hardware_profile.py:117-121,124-146` | sysfs capability 位图是多 chunk、空格分隔的 64-bit 格式，`int(...,16)` 解析失败返回 0：daemon 侧“无 EV_KEY”安全过滤 fail-open，探针把真实键盘/触摸板 keys 报 0（已实测），破坏新机型验收流程 |
| P2-3 | `backend/relay.c:187-206`；`docs/hardware-profiles.md:21,47`；`README.md:72` | profile 精确名称分支只做 `strcmp`，跳过能力校验与 USB/蓝牙排除，与文档“profile 精确名称仍要通过能力校验”“外接 USB/蓝牙设备被排除”的安全承诺矛盾 |
| P2-4 | `service/hardware_profile.py:53-72,85-114` | profile 加载缺类型/数值校验：非 dict `match` 抛未捕获 `AttributeError`（daemon 启动崩溃循环、probe 崩溃）；空 `match` 会匹配所有机型并遮蔽内置 cfqv8；NaN/Infinity/非正交 mount matrix 被接受，导致姿态永久 invalid |
| P2-5 | `service/policy.py:91-121,130-137,159,170` | 无效/超量程样本仍写 `last_geometry` 并驱动判定：单帧零重力 → 立即进入 D → 释放软件接管/恢复显示；单帧冲击 → 取消帐篷提示并 epoch+1，用户刚点下的确认被拒 |
| P2-6 | `service/policy.py:143-144,159,71-76` | 帐篷提示在 45–155° 保持区仍可确认（文档帐篷区 65–115°），已复现 46/60/120/150° confirm=True → T |
| P2-7 | `service/daemon.py:296-314`；`service/policy.py:122-124` | 休眠只清 history/timers，不清 `last_stamp/last_sample/stable_since/last_geometry/mode`；CLOCK_MONOTONIC 在 suspend 期间冻结，唤醒瞬间按休眠前姿态重建模式/提示 |
| P2-8 | `service/daemon.py:151-168,283-290,316-320,328-334`；`service/iio_reader.py:126-128,171-172` | 传感器获取单向降级：IIO 一旦失败永久走 ~2 Hz sysfs，不再重试 IIO；`ClaimAccelerometer` 只调一次、错误静默、不按需释放，且与直接占用 IIO buffer 存在启动竞态（可能干扰 iio-sensor-proxy/GNOME 自动旋转；README 也把该项列入待人工确认） |
| P2-9 | `service/daemon.py:72-80,216-222,226-245,320`；`service/actuator.py:85-99`；`service/session_access.py:5-23` | 主循环内同步执行 logind/polkit（最高 1.5+3 s）与 relay kill+recover（最高约 4 s），威胁 agent 1.8 s 调用超时与 3 s 租约 → 故障恢复期显示恢复/模式重放抖动 |
| P2-10 | `service/daemon.py:88-99,144-149`；`session/agent.py:16,45-46` | 会话租约无适配器身份认证：同用户进程可冒充 `SessionUpdate(ready=True)` 让 root relay 在显示未准备时停用输入、垄断租约造成 DoS（纵深防御缺陷，非跨用户提权） |
| P2-11 | `service/daemon.py:102-103,123-125,223`；`service/controller.py:42-71`；`session/agent.py:156-162` | `supported` 门禁不一致：非 Panasonic 机型仍会跑自动姿态策略、弹提示、`ConfirmTent` 无检查、agent 仍执行显示旋转；与“非 Panasonic 不启动特权控制/模式控制被禁用”的表述不符 |
| P2-12 | `service/daemon.py:211-245`；`service/controller.py:54-58`；`service/hardware_reader.py:44-51` | hardware 单帧 `None`（permission/read_error/ambiguous）无去抖 → target unavailable → 释放 relay/恢复显示；下一帧恢复后需重新累计 stable，出现可见方向回弹与输入重抱 |
| P2-13 | `session/agent.py:20-46,196-199`；`session/display.py:68-71` | 单实例名申请晚于 `Display()` 恢复日志与首个 tick：重复启动的第二个 agent 会先撤销活动实例的旋转锁/方向再退出，导致 daemon `applied` 与实际显示长期不一致 |
| P2-14 | `session/agent.py:99-191` | 租约/授权丢失后仍每 100 ms tick：`SessionUpdate` 持续失败 → 每 tick 一对 apply+restore（fake-bus 稳态 ≈10 对/s 的 Mutter 配置 + dconf 写），发生在不可见会话中；未使用 `state.session_connected` 门控 |
| P2-15 | `session/agent.py:73-97,172-178,62-71` | `shown_epoch` 在 Notify 成功前置位：通知服务瞬断或本地显示错误关闭提示后，同一 epoch 的提示永久不再出现，直到姿态 epoch 变化/锁屏循环 |
| P2-16 | `session/display.py:63-76,148-168` | `display.json` 为合法但非 dict 的 JSON（`[1,2]/5/"x"/true`）时，`owned.get()` 抛未捕获 `AttributeError` → 用户会话服务每 3 s 重启循环且无法自愈（需人工删文件） |
| P2-17 | `session/display.py:130-140,53-59,106-107` | 平板模式先写 `orientation-lock=false` 再 `rotate(0)`，与自身 docstring/README 的“先横屏后解锁”意图相反；释放锁后 gsd 自动旋转可抢改方向，随后单次回读失败 → `display-transform-not-applied` → 2/4/8/10 s 退避（瞬态 UX，故列 P2） |
| P2-18 | `ui/main.js:141-158,168-169,69`；`session/agent.py:137,152`；`session/display.py:13,17,32,39,48,94,107,136,139`；`service/actuator.py:59,71,99` | 用户可见错误是未翻译内部码（`internal-monitor-*`、`display-transform-not-applied`、`relay-exited-N` 等），中文 UI 显示英文/代码串，且 `error.message` 带 GDBus 前缀 |
| P2-19 | `packaging/panasonic-posture-manager.service:18-23`；`packaging/panasonic-posture-manager-session.service:6-11` | systemd hardening 不足：root 服务 5.5 MEDIUM、用户会话服务 9.2 UNSAFE。可补 `ProtectKernelLogs/Clock/Hostname/ControlGroups`、`RestrictNamespaces/Realtime/SUIDSGID`、`LockPersonality`、`ProtectProc/ProcSubset`、`UMask=0077`、用户服务 `NoNewPrivileges/RestrictAddressFamilies` 等（`PrivateDevices`、`ProtectKernelTunables` 与 uinput/IIO 冲突，需真机验证） |
| P2-20 | `packaging/aur/panasonic-posture-manager.install:1-21`；`INSTALL.md:37-43`；`packaging/aur/README.md:100-105` | `.install` 在事务中 `enable --now`/升级无条件 `restart`（与 dbus-reload ALPM hook 有启动竞态、可能打断正在使用的帐篷/输入接管）；`--global enable` 不启动当前已登录会话的适配器，但文档“安装后的当前会话无需操作”未说明需重新登录或手动 `systemctl --user start` |
| P2-21 | `Makefile:1-9`；`packaging/aur/PKGBUILD:40-47` | `CFLAGS = ...` 硬编码覆盖 makepkg/distro 的加固与优化标志、无 `LDFLAGS`、保留 `-Werror`（新编译器告警直接 FTBFS）；实测 `CFLAGS=... make -n` 仍用 Makefile 固定值 |
| P2-22 | `packaging/aur/PKGBUILD:19-28`；`packaging/panasonic-posture-manager-session.service` | 运行时直接依赖 `gnome-settings-daemon`（GSettings schema）、`gnome-shell`（ScreenSaver/通知）、Mutter（DisplayConfig）未声明；用户单元无 `ConditionEnvironment=XDG_CURRENT_DESKTOP=GNOME`，非 GNOME 会话中 `Gio.Settings.new` 可能 abort → 3 s 重启循环 |
| P2-23 | `packaging/panasonic-posture-manager.service:8,10`；`...-session.service:8`；两份 `work.mmoe.panasonic.PostureManager.desktop`；`packaging/aur/PKGBUILD:92-103` | 源码树模板仍用 `/usr/local` 前缀靠 AUR `sed` 改写；`packaging/` 下的 desktop 文件无人引用（与 AUR 版双份漂移），与“旧 /usr/local 布局已清理”的叙述冲突，维护易错 |
| P2-24 | `packaging/aur/PKGBUILD:35-38,114-117` | AUR 本地执行文件（两个 wrapper、desktop、`.install`）未纳入 `source=()`/`sha256sums`，以 root 安装的脚本无完整性校验；PKGBUILD 无 `check()`/`checkdepends`，打包流程不跑 58 项测试 |
| P2-25 | `README.md:114-122`；`INSTALL.md:13-21`；`packaging/aur/README.md:3-6,96-98,252-256` | 顶层文档给出可执行安装流程，AUR README 自述 skeleton/占位 URL/全量 makepkg 未跑；用户按顶层文档 `makepkg -si` 会在下载阶段失败且不清楚原因 |

### 4.4 P3（30）

| 组 | 条目 |
| --- | --- |
| 传感器/IIO | `iio_reader.py:29-40,43-55` 解码缺 `bits` 掩码（`le:s16/32>>0` 等负值解错）；`:118-119,159-161` 忽略 `*_index` 顺序；`:70-77` 多 `accel_3d` 设备静默取第一个，与 sysfs 回退的歧义拒绝不一致；`daemon.py:178-191` sysfs 回退忽略 `profile.accel_name` 与 IIO mount matrix；2 s staleness 在 `policy.py:12`/`daemon.py:249`/`presentation.py:5` 三处硬编码 |
| relay 运行期 | `relay.c:157-250,424-429` 已 grab 设备热插拔后不重校验（平板态下内部输入可能恢复）；`:221-249,423-427` 按键检查→grab 的 TOCTOU 且键盘+触摸板必须同时成功否则整机 exit 7；`:398-420` attach 瞬时失败 6 次即 exit 6；`:321-333` recover 先发 ON 造成约 150 ms 平板态抖动；`:281-285` `clock_gettime` 未检查/`timespec` 未初始化、open 无 `O_NOFOLLOW`、`strcpy`/`usleep` 可现代化；relay 自身不校验 DMI 厂商，`ExecStopPost` 无条件运行；`service:9` `ExecStartPre=+modprobe uinput` 无 `-`，模块缺失会阻断服务（建议 modules-load.d） |
| 策略/状态 | `policy.py:58-69,147-158` `paused/suppress` 生产不可达却被单测覆盖（假覆盖），且 suppressed 早退绕过 prune；`presentation.py:19-23` 长时间 unknown 时姿态图无限粘滞（hardware off 后可能常驻 tablet 图）；`daemon.py:56`/`policy.py:143-144`/`ui/main.js:148,164-165` ambiguous 文案“Using software tablet mode only”与实际（target unavailable）相反；`controller.py:56-58` manual 在 D 状态被撤销与 README“手动选择立即生效”需在文档说明；`daemon.py:104-110` SetMode 无条件 generation++/retry，无幂等语义 |
| 守护进程 | `daemon.py` 全文无 logging：IIO 回退、claim 失败、授权撤销、执行器异常在客户端未连接时不可观测；`:85-87` GetState 未鉴权，任意本地用户可读姿态/机型/错误；`:298-314` sleep_event 清 `actuator.error` 可能吞恢复失败、`control.error/pending_notification` 跨休眠残留；`:34,36,103,125,129` D-Bus 错误为裸英文内部串且语义复用（`Expired posture confirmation` 被多场景使用） |
| 会话/显示 | `display.py:161-167` restore 写回旋转锁不检查返回值/不回读，失败仍删 journal；`:82-87,148-150` journal 无 fsync、损坏不隔离、falsy 残留；`:40-49` Mutter `ApplyMonitorsConfig` 属性键名透传待验证（`underscanning` vs `is-underscanning`、`layout-mode`）；`:110-146` 未知 target mode 静默按横屏锁定；`agent.py:11-15`/`main.js:10-14` locale 根硬编码 `zh_CN` 且 py/js 重复实现；`agent.py:172-178` 提示“先展示后成功”见 P2-15 |
| UI | `ui/main.js:304-338,179-183` DBusProxy 创建失败后永不重建（重试只调 refresh）；`:110-177` `syncing` 无 try/finally，异常会永久静默设置操作；`:235-256,286-299` 确认/模式按钮无 in-flight 去重，双击产生虚假“过期确认”错误；`:76-87` 插图失败无可见回退，`illustration.js:22-44` 缓存 20 张 640×480 ≈25 MiB 不回收；`:205-212,287-295` 无障碍标签缺失（DrawingArea/CheckButton，待 Orca 实测） |
| i18n | `po/zh_CN.po` 15 条过期 msgid（旧“·”文案/Current mode 等）随 `.mo` 安装；无 POTFILES/xgettext/msgmerge/覆盖率检查，`make check` 只做格式检查 |
| 打包/文档 | `.install` 吞错与重复 daemon-reload、卸载文档未列 `/var/lib/panasonic-posture-manager` 与 `$XDG_STATE_HOME/panasonic-posture-manager` 残留；D-Bus policy 允许任意用户发任意接口（最小权限）；polkit `allow_active` 不限 seat0（daemon 二次校验，建议文档说明）；PKGBUILD 未显式声明 `kmod`/`dbus`（传递依赖）；`TimeoutStopSec=5` 边界待验证；缺 AppStream metainfo/man/文档安装；无 `RELEASE.md`/release tarball 流程、工作区无 `.git`（tag/CHANGELOG 无法验证）；手动测试固定 `/tmp/*.log`、pkexec 字符串拼接路径、absent 测试中断恢复风险未在文档提示；INSTALL 构建依赖未提 `dbus`/`python-gobject`（跑 `make check` 需要） |

---

## 五、P1 详解

### P1-1 generic profile 自检测虚拟开关（功能与文档承诺冲突）

- **机制**：relay 在 `runtime` 创建只含 `EV_SW/SW_TABLET_MODE`、名为 `Panasonic posture managed switch` 的 uinput 设备；`tablet_switch()`/`hardware()` 只按能力（`switch_name == NULL` 时不校验名称）与 `switch_monitor()` 选择设备，**没有排除子进程自己的虚拟开关**。daemon 侧 `hardware_reader.query()` 同理只按名称过滤（generic 的 `switch_name` 为 `None`）。
- **后果**：generic profile（未知 Panasonic 机型 / 用户 profile 未写 `switch_name`）下，物理开关存在时 relay 匹配物理+虚拟两个设备 → 启动阶段 exit 5 或主循环 exit 6，帐篷/支架/平板软件模式全不可用；首次折叠前（物理开关缺席）时把虚拟开关状态当硬件读回，形成 ON 反馈环并误报 `physical:true`，daemon 侧 hardware 翻转 → release → 重建 relay 的约 1–2 s 振荡。
- **证据**：代码路径完整；`docs/hardware-validation.md:55` 记录“退出后设备列表无本程序的虚拟开关”，反证虚拟设备确实出现在输入设备列表中；mock `query(None)` 返回 `(True,'ok')`、`query('Intel HID switches')` 返回 `(False,'absent')`；CFQV8 靠 `service/profiles/cfqv8.json` 的 `switch_name` 过滤避开。
- **修复**：为虚拟设备设置稳定身份（如 `UI_SET_PHYS("panasonic-posture-manager/relay")` + 常量 name），在 `tablet_switch/hardware/switch_monitor/acquire_inputs` 与 `hardware_reader.query` 中统一排除；`--recover` 也复用同一排除逻辑；补“物理+虚拟两节点”回归测试。
- **验证**：非 CFQV8 松下机型，或本机临时不带 `--switch-name` 运行 relay，观察 `hardware()/query()` 是否计入虚拟设备。

### P1-2 refresh 缺少异常隔离 → 状态机永久停摆

- **机制**：`refresh()` 是 100 ms GLib timeout 回调，内部无 try/except。已确认 PyGObject 在 timeout 回调抛异常后会移除该 source（实验：回调第二次 raise 后不再执行，主循环继续）。
- **可达异常路径**：`hardware_reader` 的严格 UTF-8 设备名解码（`UnicodeDecodeError` 非 `OSError` 不被捕获）；`connection.emit_signal()` 在总线连接关闭竞态可能抛 `GLib.Error`；`Actuator.release()` 的 except 块内 `kill/wait/recover` 可再次抛异常。
- **后果**：进程存活、D-Bus 方法仍能调用 `refresh()`，但周期状态机、姿态判定、执行器心跳全部停止；`GetState` 冻结在最后一次状态，systemd 不会重启。对用户表现为“服务在，但姿态/模式不再更新”。
- **修复**：`refresh()`（以及所有 GLib 回调）加顶层 `try/except Exception`，记录日志并 `return GLib.SOURCE_CONTINUE`；sysfs 字符串读取统一 `errors='replace'`；`Actuator.release()` 内层兜底。
- **验证**：接入名字含非法 UTF-8 字节的输入设备，或注入总线断连，观察 100 ms tick 是否存活。

### P1-3 硬件平板模式 policy.history 无界增长

- **机制**：每个有效样本在 `policy.py:111` append 到 `history`；`hardware is True` 在 `:125-129` 提前 return；唯一 prune 在常规决策路径末尾 `:182-183`。
- **数据**：20 Hz（IIO 读取侧约 19–20 样本/s）× 约 16.5 MiB/h；8 小时折叠使用约 100+ MiB，root 常驻服务未设 `MemoryMax`。sysfs 回退（~2 Hz）下仍有约 1.6 MiB/h。脱离平板模式后首样本即回落到约 8 条，说明泄漏只发生在持续 H 模式。
- **修复**：剪枝抽成 `_prune(now)` 在所有 return 前调用，或在 H 分支直接 `history.clear()`；补“各分支循环后 history 有界”的不变量测试。
- **已在修复清单第一优先级**：改动最小、收益明确。

### P1-4 手动测试 root 助手 /tmp 符号链接竞态（安全）

- **机制**：`check_absent_switch.py` 使用固定目录 `/tmp/posture-absent-test`（`mkdir(exist_ok=True)` 会跟随已存在的符号链接），再通过 pkexec 启动 `absent_switch_root.sh`，后者 `mkdir -p` + `chmod 0777` + `touch` 同一路径。
- **后果**：本地攻击者可预先将 `/tmp/posture-absent-test` 指向任意目录；管理员按文档运行该手动测试并完成授权后，root 助手会把目标目录改成 0777、写入标记文件 → 经典本地提权条件。触发需要管理员主动运行手动测试，属条件受限的真实风险；同类问题还有固定 `/tmp/posture-absent-test.log` 与 pkexec 字符串拼接路径。
- **修复**：父进程用 `tempfile.mkdtemp(prefix=..., dir=<私有目录>)`（0700）创建并显式传路径给 root 助手；助手校验 `[ -d ] && [ ! -L ]` 与属主/权限，不符即退出；删除 `chmod 0777`；日志用 `mkstemp`；pkexec 用参数数组/引号转义。
- **说明**：由于只在手动测试路径触发，按“发布前修复的安全项”处理；若按安全评审惯例可视为 P1。

### P1-5 发布就绪阻断（作者已自述，尚未解决）

- **无 LICENSE/COPYING**：`PKGBUILD:14-18` 的 `GPL-3.0-or-later` 为临时占位；`package()` 中 `if [[ -f LICENSE ]]` 永假。
- **占位符**：`url/source=example.invalid`、`sha256sums=SKIP`、`Maintainer: TODO`；`.SRCINFO` 同步。
- **文档矛盾**：顶层 README/INSTALL 虽提示“先替换”，但 AUR README 明确 “skeleton only — do not submit”；`makepkg -si` 会在下载阶段失败。
- **建议**：作者确认许可证 → 加 `LICENSE` → 更新 `license`/`.SRCINFO`；产出真实 release tarball → `updpkgsums` → 填 URL/sha256/维护者 → clean chroot（devtools）构建 + `namcap`；顶层文档加显眼状态框并链接 AUR README 的 release blockers。

---

## 六、重点 P2 补充说明

- **P2-1 recover 语义**：不仅有“折叠时反向 OFF”，还有“设备缺席时空操作”。`ExecStopPost` 的 recover 不带 profile 名称是直接原因；建议优先按 P1-1 排除自建设备，使 recover 不依赖名称；并覆盖“折叠态 restart”“缺席态 SIGKILL”两个实机场景。
- **P2-2 capability 位图**：这是 Python 层与 C 层对同一设备结论可能相反的直接原因；建议抽一个正确的内核 bitmap 解析器（64-bit chunk、左高右低；32-bit 平台注意 word size），或直接改用 `EVIOCGBIT`；用实机字符串做 fixture 单测。
- **P2-5/P2-12**：单帧噪声/硬件 unknown 都会造成“释放输入 + 方向回弹 + 重新应用”，建议统一“无效样本不更新有效锚点、持续超过 stale 才撤销”的语义。
- **P2-8**：README/行为表称 360° 平板模式“允许系统自动旋转”，但 daemon 直接占用 IIO buffer 并与 iio-sensor-proxy 的 claim 生命周期混用；该冲突正是 README “仍需人工确认”清单里的自动旋转项，属未验证高风险，建议在真机专门验收（停 iio-sensor-proxy / 观察 `sensor_source`）。
- **P2-9/P2-10**：是同一根因的两面——root 服务与不可信会话进程之间缺少异步/身份边界；修复可同时改善故障期抖动与安全承诺。
- **P2-17 平板模式时序**：修复方式是在锁仍为 True 时先 `rotate(0)` 再释放锁（与 docstring 一致），并给回读失败加 100–300 ms 延迟重试；补操作顺序断言测试。
- **P2-19/P2-21**：发布前建议完成一轮 systemd 硬化 + 构建 flags 修正，因为二者都属于“改动小、长期收益大”的项；注意 `PrivateDevices`、`ProtectKernelTunables` 与 uinput/IIO 的冲突需真机回归。

---

## 七、文档—实现不一致清单

| # | 位置 | 不一致 |
| --- | --- | --- |
| D1 | `docs/hardware-profiles.md:15`、`README.md:26,62` | “未知模型使用 generic profile，功能可用” ↔ P1-1 实际不可用 |
| D2 | `docs/hardware-profiles.md:21,47`、`README.md:72` | “profile 精确名称仍要通过能力校验/排除外接设备” ↔ `relay.c:187-206` 未校验 |
| D3 | `docs/hardware-profiles.md:46` | “mount_matrix 不填时读取 IIO in_accel_mount_matrix” ↔ sysfs 回退路径不读 |
| D4 | `docs/hardware-profiles.md:13`、`README.md:59` | 文档写“16/32 位” ↔ 实现支持 8/16/32/64 位存储 |
| D5 | `docs/hardware-profiles.md:20` | “未知总线……拒绝接管” ↔ `relay.c:146-150` 只排除 USB/BT |
| D6 | `docs/hardware-profiles.md:9,22` | “安装期 DMI 校验/非 Panasonic 不启动执行器” ↔ PKGBUILD/`.install` 无 DMI 检查，`ExecStopPost` 无条件运行 relay |
| D7 | `docs/hardware-profiles.md:51` | 过时的“探针源码安装为 `/usr/local/bin/`”（INSTALL 明确无自安装器，AUR 为 `/usr/bin`） |
| D8 | `docs/hardware-validation.md:83` | 写死 `iio:device5`/`accel_3d-dev5`；本次启动 `accel_3d` 在 `iio:device1`，应标注“当次启动记录值” |
| D9 | `README.md:73` | “systemd 停止后另有一次开关状态修复” ↔ P2-1 在缺席/折叠两种场景不成立或会反向修复 |
| D10 | `README.md:48,153` vs `README.md:122`、`INSTALL.md:37` | “由用户启用会话适配器” ↔ “不需要用户手动 enable”（`.install` 自动 `--global enable`） |
| D11 | `README.md:118` | “否则 makepkg 会失败”不精确：`SKIP` 本身不失败、缺 LICENSE 也不失败，只有占位 URL 下载会失败 |
| D12 | `packaging/aur/README.md:74-77,165-166` | profile 查找顺序漏掉 `/usr/share/panasonic-posture-manager/profiles`，并把加载逻辑归错给 `daemon.py`（实际在 `hardware_profile.py`） |
| D13 | `packaging/aur/README.md:45` vs `PKGBUILD:45` | 称 `build()` 移除任意 `locale/**/*.mo`，实际只删一个固定文件 |
| D14 | `packaging/aur/README.md:235-237` | 静态校验记录“21 input devices”，当前只读复核为 16（依赖当时外设） |
| D15 | `README.md:5,153`、`INSTALL.md:5`、`docs/hardware-validation.md:3` | “本机已安装并运行/已迁移 /usr 布局”在当前检阅环境无 pacman 安装记录，无法复核（可能为不同/已清理环境） |
| D16 | `INSTALL.md:81` | “设置页不再写‘与触摸板’”对 UI 成立，但 `po` 中仍保留旧文案 msgid 随 `.mo` 安装 |
| D17 | `docs/hardware-validation.md:10`、`README.md:11,157` | “自动旋转可用”为原型期观察，生产 daemon 占用 IIO buffer 的冲突尚未验收，README 也列为待人工确认 |
| D18 | `README.md:40,151` | “保留旧点击键兼容/旧点击键有单测”表述易误解：通知只发布 `default` 一个动作，`tent` 仅 handler 接受 |
| D19 | `INSTALL.md:9` | 构建依赖未写 `dbus`（`dbus-run-session`）与 `python-gobject`，按此安装跑不了文末 `make check` |
| D20 | `INSTALL.md:68-75`、`README.md:166-174` | 手动测试脚本不随包安装，需保留源码树；absent 测试会临时移除 `/dev/input/event*`，中断/断电恢复风险未提示 |
| D21 | AUR README “Expected release tree” | 把 `locale/` 注释为安装产物但 `build()` 会删除重建；缺少 release tarball 生成流程/版本 tag/CHANGELOG 说明 |
| D22 | `packaging/aur/README.md:9` vs 源码树模板 | “nothing under /usr/local” 对最终包成立，但模板仍含 `/usr/local` 前缀、AUR README 未解释 `sed` 重写机制；死 desktop 文件加剧困惑 |

---

## 八、测试与验证盲区

1. **C relay 零自动化测试**：`make check` 不覆盖 relay；P1-1/P2-1 及热插拔/TOCTOU/恢复语义均无法被发现。
2. **daemon 主循环零覆盖**：`test_feedback`/`test_sleep` 全 mock，`check_dbus.py` 走 session bus（`authorized()` 直接放行、actuator 分支被跳过）；`refresh` 的事务/ack/租约/异常路径没有测试。
3. **授权安全路径未测**：logind seat0 + polkit、跨用户拒绝、`GetState` 访问控制、租约冒充、跨会话，均无测试。
4. **`hardware_reader`/`session_access` 无单测**：absent/ambiguous/permission/read_error/自建设备/多 chunk 位图均未覆盖。
5. **`Display` 类与 `Agent.tick/notify` 无测试**：apply/restore/journal/损坏日志/锁写入顺序、租约丢失、通知失败重试均无自动化（`tests/test_display.py` 只测纯函数，`test_agent.py` 只测 `action()`）。
6. **UI 零自动化**：无 GJS 测试、无 po 覆盖率检查、无 headless GTK 回归；`make check` 不加载 JS。
7. **profile 边界**：畸形 JSON 类型、空 match、NaN/Inf、目录优先级、损坏 profile 跳过均无测试。
8. **IIO 解析**：仅 4 个用例；无 shift≠0、bits<storage 掩码、8/64 位存储、`*_index` 顺序、mount matrix、短读/迭代中异常、setup 部分失败恢复。
9. **休眠/唤醒、故障注入**：`test_sleep` 不覆盖唤醒后的 policy 决策顺序；`check_faults.py` 只断言最终 D-Bus 状态，不检查 GNOME 实际 touch-mode。
10. **打包链路**：无 clean-chroot 构建、无 `namcap`、无 `check()`；`.SRCINFO` 同步仅静态核对。
11. **手动测试覆盖不到关键状态**：本机当前开关设备缺席，`check_absent_switch.py` 会 SKIP；P2-1 的“折叠态停止”没有被现有手动测试覆盖。

---

## 九、已确认无问题 / 可信赖部分

- **代次/确认主干**：`Controller.update` 仅在 target 变化时递增 generation；`acknowledge` 拒绝过期代次；SetMode/sleep_event 主动递增使在途 ack 失效；测试覆盖过期回调与失败上报。
- **执行器契约**：relay 3 s 租约、1 s 心跳、`applied` JSON 字段与 `Actuator` 完全一致；旧进程回调隔离。
- **并发模型**：Controller/Policy/Presentation 仅主线程访问；采样面 `vector/stamp/read_error` 有锁，未发现数据竞争。
- **D-Bus 契约**：XML 与 agent/UI 调用签名一致；错误名、polkit action、D-Bus 策略/激活、systemd 单元路径自洽。
- **Mutter DisplayConfig 变体类型**：与 libmutter-51 内嵌类型串一致；旋转/布局纯函数测试覆盖镜像、外屏 parity、单屏竖屏等。
- **GTK4/libadwaita API**：broadway 无头实际构建两页并 present，无 API 误用/废弃告警；`ARGV`、主题信号、`activatable_widget` 等均正确。
- **插图/i18n 当前状态**：20/20 变体渲染成功；36/36 去重字符串中文可译；通知 action 校验与旧 key 兼容有单测。
- **显示恢复主路径**：journal 先写后改、三种崩溃窗口恢复、失败保留 journal 重试均正确。
- **C 内存/信号安全**：`-fanalyzer` 0 告警；无越界/整数溢出/资源泄漏；SIGKILL 下内核回收 grab/uinput。
- **包装契约**：安装路径、bus name、对象路径、5 方法签名、polkit action、StateDirectory、gettext/assets/profiles 路径、desktop application_id 全部一致；desktop-file-validate/systemd-analyze verify 通过。
- **安全方向**：switch-only、排除 USB/蓝牙、grab 前查按键、polkit+logind 活动会话、systemd 基础硬化（NoNewPrivileges/ProtectSystem/空 CapabilityBoundingSet）均在正确方向上。

---

## 十、改进路线图

### 阶段 0：发布阻断（必须先做）

1. 确认许可证 → 添加 `LICENSE` → 更新 `PKGBUILD`/`.SRCINFO` 的 `license`。
2. 产出真实 release tarball → `updpkgsums` → 替换 URL/sha256/Maintainer → `makepkg --printsrcinfo > .SRCINFO`；clean chroot 构建 + `namcap`。
3. 修复手动测试 root 助手的 `/tmp` 符号链接竞态（P1-4），并在文档标注“测试脚本不随包安装、异常中断恢复方法”。
4. 顶层 README/INSTALL 与 AUR README 对齐“skeleton/暂不可直接安装”的状态说明（P2-25/D11/D15）。

### 阶段 1：P1 功能/健壮性（建议 1–2 天内）

5. relay/硬件层统一排除自建 uinput 设备（含 `--recover`、`ExecStopPost` 路径），补回归测试（P1-1/P2-1）。
6. `refresh()` 及所有 GLib 回调加顶层异常保护；sysfs 字符串读取 `errors='replace'`；`Actuator.release()` 内层兜底（P1-2）。
7. 修复 policy history 无界增长（P1-3），并加不变量测试。
8. 顺带修复 capability bitmap 解析（P2-2，与 P1-1 同属“设备身份/位图”问题域）。

### 阶段 2：发布前应完成

9. recover 语义重写（缺席也强制已知终态；二读前排除自建设备；`ExecStopPost` 带 profile 或复用同一发现）。
10. 统一无效样本/hardware unknown 去抖语义（P2-5/P2-12）；修休眠锚点（P2-7）；IIO 周期重试与 claim 生命周期（P2-8）。
11. 主循环去阻塞（授权缓存、active_session 异步化、actuator release/recover 工作线程化）（P2-9）。
12. 租约身份校验（system bus well-known name/token）（P2-10）；supported 门禁一致化（P2-11）。
13. 会话层：单实例顺序、租约丢失退避、通知状态机、恢复日志 schema + fsync、平板模式锁/旋转顺序、错误码本地化（P2-13～P2-18）。
14. systemd 硬化、`.install`/当前会话/dbus 重载、Makefile CFLAGS/LDFLAGS、GNOME 依赖声明、packaging 模板统一（P2-19～P2-24）。
15. 补 `check()` 与关键测试：relay 设备身份/恢复、daemon refresh 事务、硬件位图 fixture、Display/Agent fake-bus、po 覆盖率。

### 阶段 3：打磨与文档

16. P3 列表逐项：IIO 解码/index、relay 热插拔/TOCTOU/attach 降级/C 加固、presentation 粘滞、日志与错误码、UI 代理重建/syncing/in-flight/插图回退/无障碍、locale 探测、po 清理、卸载残留文档、AppStream/man/RELEASE 流程。
17. 按第七节 D1–D22 修正文档，并建立“文档 vs 代码/安装”定期核对表（路径、版本、计数、设备编号、安装状态最易漂移）。

---

## 十一、检阅方法、覆盖与限制

- **方法**：4 个模块并行只读深度阅读（服务核心 / 硬件与 C 执行器 / 会话与 UI / 打包与文档）× 交叉核对；Lead 独立复读核心文件、跑全量测试与静态分析、复现关键实验、汇总去重并统一严重级别。
- **覆盖**：工作区 62 个文件全部纳入；重点源码 3,700 行逐行检阅；测试源码与手动脚本全部阅读；文档全部阅读。
- **限制**：本机为 CFQV8-1L，但检阅沙箱无 `/dev/input`、`/dev/uinput`、uinput 未加载、包未安装、无 `.git`，且无法对真实 Mutter/GSettings/通知服务做破坏性验证；因此以下均标注“待实机验证”：relay 运行时、generic 自检测、ExecStopPost 折叠恢复、IIO/claim 竞态、平板模式时序、会话切换抖动、真实休眠唤醒、180° 触摸映射、真实通知点击。未修改任何源码；未运行 `makepkg`（只读要求）；`shellcheck`/`namcap` 环境不可用。
- **建议下一步**：先把阶段 0 + 阶段 1 的 8 个动作落地并复跑 `make check`，然后在实机上按 README“验证”清单补做 P2 级别的场景（尤其“折叠态 restart”“首次折叠前 SIGKILL”“自动旋转与 IIO 共存”）。

---

*报告完。所有结论均基于只读操作与可复现实验；标注“待实机验证”的条目不应被当作已捕获的运行事实。*
