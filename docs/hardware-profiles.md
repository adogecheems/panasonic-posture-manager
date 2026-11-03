# 硬件适配与 profile

更新：2026-11-03。

Panasonic QV8、QV9、QV1 使用同族硬件画像。QV8 已完成主要功能实测；QV9/QV1
纳入正式支持范围，但尚未逐机物理验收。其他翻转本按硬件能力适配，必要时补充 JSON
profile。

## 支持等级

| 等级 | 含义 |
| --- | --- |
| `validated` | 正式纳入支持范围的内置 profile。它不代表每台同型号设备都完成物理验收；验收结果单独记录。 |
| `compatible` | 按硬件能力运行，但没有经过完整验证的专用 profile。功能可能可用，不保证所有硬件兼容。 |
| `unsupported` | 显式禁用。系统服务报告模型不受支持，不建立软件姿态接管。 |

`validated` 和 `compatible` 的 `supported` 均为真；`unsupported` 为假。

## profile 查找

查找顺序：

1. `/etc/panasonic-posture-manager/profiles/`
2. 源码树 `data/profiles/`（从检出目录运行时）
3. `/usr/share/panasonic-posture-manager/profiles/`

管理员覆盖始终优先。源码树路径在安装后的 wheel 中不存在，因此正常安装只读取
`/usr/share`。目录中的 `*.json` 按文件名排序，第一个有效匹配生效。

## profile 格式

内置 profile 位于 `data/profiles/`。示例（字段值与当前 QV profile 接近，省略了
可选的 `mount_matrix`）：

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
  "accel_name": "accel_3d"
}
```

| 字段 | 说明 |
| --- | --- |
| `id` | profile 标识；缺省为文件名。必须是非空字符串。 |
| `match` | 必填。可写精确 `vendor`/`product`、`product_glob`，或直接写产品字符串。 |
| `version` | 可选，当前只接受 `1`。缺省视为 1。 |
| `support_tier` | `validated`、`compatible` 或 `unsupported`；缺省为 `compatible`。 |
| `switch_name` | 可选。平板开关名称提示；仍必须通过能力校验。 |
| `keyboard_name` | 可选。内置键盘名称提示；仍必须通过能力校验。 |
| `touchpad_name` | 可选。内置触控板名称提示；仍必须通过能力校验。 |
| `accel_name` | 可选，默认 `accel_3d`。用于优先选择 IIO/sysfs 加速计。 |
| `mount_matrix` | 可选，行优先 3×3 矩阵，用于修正传感器安装方向。 |

匹配规则：

- `"match": "CFQV8-1L"` 等价于只匹配该 `product`。精确字符串比较区分大小写。
- `product_glob` 使用 shell 通配符，不区分大小写；可写字符串或字符串数组。
- 未知 `match` 键会被忽略并写入 debug 日志。
- `match` 缺失、为 `null`、空对象或空字符串时，不匹配任何机器，并记录 WARNING。
- 非法 JSON、错误类型、未知版本或损坏字段会导致 profile 被跳过并记录日志，
  不会阻止服务启动。
- 没有 profile 命中时使用 generic 能力发现。profile 中的名称只用于消歧，不能
  绕过总线、phys 和能力校验。

`mount_matrix` 要求 9 个有限数值，行向量近似正交（绝对容差 `1e-3`）。未填写时，
程序优先读取设备的 `in_accel_mount_matrix`，否则使用单位矩阵。传感器校准由
用户触发的平放校准单独保存，不复写 profile。

## 设备发现与安全边界

- **平板开关**：只接管 capability 恰好包含 `SW_TABLET_MODE` 且没有 `EV_KEY`
  的 switch-only 设备。带 lid/power 键位的复合开关不抓取，以免吞掉其他事件。
- **虚拟开关**：relay 创建名为 `Panasonic posture managed switch`、phys 为
  `panasonic-posture-manager/relay` 的设备。Python 和 C 的设备发现都必须排除
  它，否则 generic 机型会把自己的输出误判为硬件开关。
- **内置键盘**：必须具备字母键能力，并排除 USB、Bluetooth 和外部 phys 设备。
- **内置触控板**：必须支持多点触控、不是 DIRECT 设备，并排除 USB/Bluetooth。
- **歧义处理**：同一槽位出现多个候选时拒绝猜测。profile 名称不能绕过能力检查。
- **加速计**：优先匹配 profile 名称；名称不匹配时，只接受唯一的完整三轴设备。
  多个候选时拒绝猜测。
- **输入读取**：relay 只检查设备身份、能力位和开关状态，不读取按键内容。

## 只读探针

安装后：

```sh
panasonic-posture-manager-probe > /tmp/posture-probe.json
```

源码开发：

```sh
PYTHONPATH=src python -m panasonic_posture.hardware.probe --probe
```

探针不需要 root，不打开 `/dev/input`，只读取 sysfs 和 profile。输出 schema
版本为 2：

| 字段 | 内容 |
| --- | --- |
| `version` / `read_only` | 输出版本和只读标记 |
| `dmi` | DMI `vendor`、`product` |
| `profile` | 命中的 profile、支持等级、最终设备名称和查找顺序 |
| `input` | 输入设备的节点、名称、phys、总线、能力和解析后的特征 |
| `iio` | IIO 设备的扫描类型、比例、频率、mount matrix 和 trigger |
| `summary` | 候选设备计数，便于快速浏览 |

提交机型适配 issue 时，请附完整探针 JSON，以及：

```sh
journalctl -u panasonic-posture-manager.service
gdbus call --system --dest work.mmoe.panasonic.Posture \
  --object-path /work/mmoe/panasonic/Posture \
  --method work.mmoe.panasonic.Posture.GetState
cat /sys/class/dmi/id/{sys_vendor,product_name}
```

## 新机型验收

1. 运行探针并保存 JSON。确认 DMI、profile、开关候选、内置输入和加速计。
2. 验证平板开关：物理折叠/展开能产生正确状态；首次折叠前诊断状态为 `absent`
   属于正常现象。
3. 验证内置键盘和触控板只抓取目标设备，外接 USB/Bluetooth 设备不受影响。
4. 检查帐篷、支架和四种旋转方向；必要时添加 `mount_matrix` 或做用户校准。
5. 验证通知、锁屏、180° 触控坐标、外接屏布局和休眠/唤醒。
6. 验证软件模式与硬件平板开关的优先级，以及租约丢失后的撤销。
7. 将 profile 放入 `data/profiles/`（随包发布）或
   `/etc/panasonic-posture-manager/profiles/`（本机实验）。

只有正式纳入长期支持范围时才使用 `validated`。物理验收结果应记录在
[hardware-validation.md](hardware-validation.md)，不能把字段值当作验收证明。

## 发布状态

`v1.0.0` 是历史发布 tag；`2.0.0` 包元数据已同步，但 `v2.0.0` tag、AUR 提交、
clean-chroot 构建和最终物理验收仍未完成。提交前请按
[../packaging/aur/README.md](../packaging/aur/README.md) 执行检查清单。
