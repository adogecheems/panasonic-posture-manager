# 机型适配说明

> 面向维护者。普通用户无需修改 profile。

Panasonic QV8、QV9、QV1 使用同族硬件画像。QV8 已完成主要功能实测；QV9/QV1
尚未逐机物理验收。其他翻转本按硬件能力兼容。

## 支持等级

| 等级 | 含义 |
| --- | --- |
| `validated` | 内置支持机型；不代表每台同型号设备都已完成验收 |
| `compatible` | 按硬件能力运行，功能不保证完整 |
| `unsupported` | 禁用软件姿态接管 |

## Profile 查找

依次查找，管理员覆盖优先：

1. `/etc/panasonic-posture-manager/profiles/`
2. `data/profiles/`（从源码目录运行时）
3. `/usr/share/panasonic-posture-manager/profiles/`

目录中的 `*.json` 按文件名排序，第一个有效匹配生效。正常安装只读取
`/usr/share`。

## 最小示例

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

## 字段

| 字段 | 说明 |
| --- | --- |
| `id` | profile 标识；缺省为文件名 |
| `match` | 必填；支持精确 `vendor`/`product`、`product_glob` 或产品字符串 |
| `version` | 可选；当前仅支持 `1` |
| `support_tier` | `validated`、`compatible` 或 `unsupported`；默认 `compatible` |
| `switch_name` | 可选；平板开关名称提示 |
| `keyboard_name` | 可选；内置键盘名称提示 |
| `touchpad_name` | 可选；内置触控板名称提示 |
| `accel_name` | 可选；默认 `accel_3d` |
| `mount_matrix` | 可选；行优先 3×3 矩阵，用于修正传感器方向 |

- `match` 字符串按精确产品名匹配；`product_glob` 使用不区分大小写的通配符。
- profile 中的名称只用于消歧，不能绕过设备能力检查。
- 无效 profile 会被跳过；没有匹配项时使用通用硬件发现。
- `mount_matrix` 需要 9 个有限数值；省略时读取设备矩阵或使用单位矩阵。

## 只读探针

```sh
panasonic-posture-manager-probe > /tmp/posture-probe.json
```

探针不需要 root，也不会打开输入设备。提交适配问题时请附完整 JSON，并说明
机型、系统版本和异常现象。

## 新机型验收

1. 运行探针，核对 profile、平板开关、内置输入和加速计。
2. 验证物理折叠/展开；首次折叠前状态为 `absent` 属于正常现象。
3. 验证内置键盘和触控板被正确接管，外接键鼠不受影响。
4. 检查五种模式、四个旋转方向；必要时补充 `mount_matrix` 或做用户校准。
5. 验证提示、锁屏、180° 触控、外接屏和休眠/唤醒。
6. 将结果记录到[实机验证记录](hardware-validation.md)，再决定是否列为
   `validated`。
