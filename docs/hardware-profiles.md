# 硬件适配与 profile

更新：2026-11-03。正式支持 Panasonic QV8/QV9/QV1 系列；CFQV8-1L 已完成主要
功能实测，QV9/QV1 按同族硬件画像纳入支持范围但尚未逐机物理验收。其他翻转本
走能力发现，必要时补 JSON profile。

## 支持策略

| 层 | 策略 |
| --- | --- |
| 支持等级 | `validated`：正式纳入支持范围的内置 QV8/QV9/QV1 profile，不代表已逐机物理验收；`compatible`：无专用 profile 时按能力发现，支持但不保证；`unsupported`：profile 显式关闭，不产生特权目标、不通知。 |
| 机型匹配 | 只依据 profile 匹配，不依据厂商白名单。内置 profile 使用 `product_glob` 匹配 QV 型号及其 `CF-`/SKU 变体。 |
| 平板开关 | 只接管 **switch-only** 设备：capability 恰好包含 `SW_TABLET_MODE` 且没有 `EV_KEY`；带 lid/power 的开关不接管。 |
| 内置键盘 | 非 USB/蓝牙、具备字母键能力；profile 名称只用于消歧，仍必须通过能力校验。 |
| 内置触摸板 | 多点触摸、非 DIRECT、非 USB/蓝牙；profile 名称同上。 |
| 加速计 | 优先使用 profile 指定名称（默认 `accel_3d`）的 IIO 设备缓冲区（约 10 Hz）；名称不匹配时，仅在恰好存在一个完整三轴 IIO 加速计时安全回退。失败/2 s 无样本回退 sysfs（约 2 Hz），每 60 s 重试 IIO。软件姿势接管与硬件平板态固定使用 sysfs。 |
| 虚拟开关 | relay 创建 `name="Panasonic posture managed switch"`、`phys="panasonic-posture-manager/relay"` 的设备；所有发现逻辑必须排除它（generic 机型安全的关键）。`--recover` 先发一次已知 ON、再发最终状态（物理 ON→ON，OFF/缺席/不可读→OFF），确保 GNOME 观察到残留软件 ON 的清除。 |
| 显示 | 后端按桌面会话选择：GNOME/Mutter 按 `is-builtin`，Plasma 6/KScreen 按 Panel 类型与内置标记，X11/RandR 按 `eDP`/`LVDS`/`DSI` 连接器名并在单屏时回退唯一活动输出。不能假定所有桌面都提供自动旋转锁；X11 仅做显式旋转，安装 `xorg-xinput` 时还会同步唯一 `INPUT_PROP_DIRECT` 物理组的触摸矩阵。 |

## 安全边界（重要）

- 开关设备必须只有平板开关位、没有键位；复合设备不会被 grab（避免吞掉 lid/
  power 等无关事件），软件平板模式仍可用。
- profile 里的精确名称**不会绕过**能力/总线校验：自动发现要求已知的内置设备特征
  （名称/phys 或触摸板属性），USB/蓝牙按 bus/phys 排除；同一槽位出现多项匹配时
  拒绝接管，而不是猜测。
- 外接 USB/蓝牙键盘、触摸板被排除；程序不读取按键内容。
- relay 必须排除自己创建的虚拟设备，否则“物理开关缺席（首次折叠前）”会被自身
  输出误判为硬件 ON，generic 机型也会因两个匹配设备失败。

## profile 格式

内置 profile 源码在 `data/profiles/`，安装到
`/usr/share/panasonic-posture-manager/profiles/`；管理员覆盖放在
`/etc/panasonic-posture-manager/profiles/`（优先）。查找顺序：
`/etc/panasonic-posture-manager/profiles/` → 源码树 `data/profiles/`（从检出目录
运行时）→ `/usr/share/panasonic-posture-manager/profiles/`。源码树优先可避免旧版
已安装 profile 遮蔽当前开发中的验证等级；安装包中源码树路径不存在，因此正常安装
仍读取 `/usr/share`。

字段（实现：`src/panasonic_posture/hardware/profile.py`）：

```json
{
  "id": "cfqv8",
  "match": {
    "product_glob": ["CFQV8", "CFQV8-*", "CF-QV8", "CF-QV8-*"]
  },
  "support_tier": "validated",
  "switch_name": "Intel HID switches",
  "keyboard_name": "AT Translated Set 2 keyboard",
  "touchpad_name": "Synaptics TM3562-001",
  "accel_name": "accel_3d",
  "mount_matrix": [1, 0, 0, 0, 1, 0, 0, 0, 1]
}
```

- `match` 可以是对象（`vendor`/`product` 精确字符串，或 `product_glob` 通配符
  字符串/数组），也可以是**裸字符串**简写：`"match": "CFQV8-1L"` 等价于只匹配
  该 `product`。通配符不区分大小写；未知 `match` 键会被忽略（debug 日志）；
  `match` 为 list/int 等非法类型时整个 profile 视为无效并被跳过。
- `match` 缺省/null/空对象/空白字符串按“不匹配任何机型”处理并记录 WARNING
  （不会磁吸所有机器、不会遮蔽内置 profile）；没有任何 profile 命中时使用
  generic 能力发现。
- 可选 `version` 字段：缺省视为 1；存在但 ≠1 时该 profile 被跳过并记录日志。
- 可选 `support_tier`：`validated`、`compatible`（缺省）或 `unsupported`。
  `validated` 是正式支持范围声明，不是逐机物理验收记录。其他值会使 profile
  失效。`unsupported` 适合管理员对已知问题机型做显式禁用。
- `switch_name` / `keyboard_name` / `touchpad_name` 可选：不填则按能力发现；
  填写后仍会校验能力与该设备是否外接。
- `accel_name` 默认 `accel_3d`，作为优先匹配名称；若内核驱动使用其它名称，
  唯一的完整三轴 IIO 加速计会被自动选用。多个候选时拒绝猜测。
- `mount_matrix` 为行优先 3×3 矩阵，用于不同传感器安装方向；不填时若 IIO 设备
  提供 `in_accel_mount_matrix` 则读取之，否则按单位矩阵处理。要求 9 个有限值、
  近似正交（正交性容差 `abs <= 1e-3`；非正交/NaN/Infinity 的 profile 会被
  拒绝并跳过，记录 WARNING）。
- 非法 JSON、非对象、字段类型错误、损坏的 profile 会被跳过并记录日志，不会
  阻止服务启动；匹配到的第一个有效 profile 生效。

## 只读探针

安装后（AUR 包）：

```sh
panasonic-posture-manager-probe > /tmp/panasonic-posture-manager-probe.json
```

源码开发（无需安装）：

```sh
PYTHONPATH=src python -m panasonic_posture.hardware.probe --probe
```

探针不需要 root，只读 sysfs/JSON，不打开 `/dev/input`，输出 JSON
（`panasonic-posture-manager-probe [--probe]`）：

| 字段 | 内容 |
| --- | --- |
| `version` / `read_only` | 探针输出 schema 版本（当前 2）与只读标记。 |
| `dmi` | `vendor`、`product`（来自 `/sys/class/dmi/id`）。 |
| `profile` | 命中的 id/vendor/product/`support_tier`/`supported`、最终生效的开关/键盘/触摸板/加速计名称、mount matrix、`search_order`。 |
| `input` | 每个输入设备的 `node/sysfs/name/phys/bustype/bustype_value/vendor/product/virtual`；原始能力 `capabilities{ev,key,rel,abs,msc,sw,led,snd,ff}` 及兼容字段 `switches/keys/abs/props/properties`（整数）；解析后的 `features{event_switch,event_key,event_abs,tablet_switch,switch_only,keyboard_keys,multitouch,direct,pointer,buttonpad,external_bus}`；读取失败时带 `capabilities_error`/`properties_error`。 |
| `iio` | 每个 IIO 设备：`device/name/scan_types/scan_indices/scale/offset/sampling_frequency/mount_matrix/trigger/buffer_enabled`。 |
| `summary` | 便于快速浏览的计数/候选判断：`input_count`、`tablet_switch_candidates`、`internal_keyboard_candidates`、`internal_touchpad_candidates`、`iio_count`。 |

给新机型报 issue 时请附：完整 probe JSON、`journalctl -u
panasonic-posture-manager.service`、`hardware_status`/`sensor_source`/`error_code`
（可用 `gdbus call ... GetState` 获取）以及 `cat /sys/class/dmi/id/{sys_vendor,product_name}`。

## 新机型验收清单

1. 运行 probe，保留 JSON；确认 `support_tier`（无专用 profile 时为 `compatible`）、平板开关候选，以及 profile 指定名称或唯一的三轴 IIO 加速计。
2. 确认 switch-only 设备可被 grab；带 lid/power 的设备不使用。
3. 确认内置键盘/触摸板是唯一的非 USB/蓝牙匹配；外接键鼠不受影响。
4. 验证帐篷/支架角度与传感器轴向；必要时补充 `mount_matrix`。
5. 验证 360° 物理折叠、通知点按、180° 触摸坐标、休眠/唤醒、外接显示器布局。
6. 验证软件模式（帐篷/支架/平板）与硬件开关的优先级、租约丢失后的撤销。
7. 再添加 profile 到 `data/profiles/`（随包发布）或 `/etc/.../profiles/`
   （本机实验）。只有正式纳入维护与支持范围时才将 `support_tier` 设为
   `validated`；物理验收结果必须单独记录，不能把该字段当作逐机验收证明。

## AUR

`packaging/aur/` 是 `/usr` 布局的 pacman 包，不运行 `/usr/local` 自安装器；
许可证已确定为 `AGPL-3.0-or-later`，构建时从固定 Git tag 拉取源码并在本地
编译。上游仓库与版本 tag 尚未公开；发布前需按 `packaging/aur/README.md` 创建
公开仓库与版本 tag、完成 clean-chroot 和 CFQV8-1L 最终物理验收，并补齐 QV9/QV1
同族回归。
历史硬件验证记录见上游开发期文档（`docs/hardware-validation.md`）。
