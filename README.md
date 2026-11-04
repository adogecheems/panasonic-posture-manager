# 松下二合一平板电脑姿态管理工具

在 Linux 翻转本上自动切换屏幕方向和内置输入，重点支持 Panasonic QV 系列。
关闭窗口后继续在后台运行。

> 当前版本：2.1.1。支持 x86_64 Linux 上的 GNOME Wayland 和 KDE Plasma 6
> Wayland。

## 功能

- 自动识别笔记本、帐篷、支架和平板姿态。
- 允许手动切换姿态模式。
- 可在设置中关闭帐篷模式。
- 帐篷和支架模式可选二次确认，减少误触发。
- 支持传感器方向校准。
- 提供中文、英文界面，跟随系统深浅色主题。

## 支持范围

- **设备**：Panasonic CFQV8、CFQV9、CFQV1；其他 360° 翻转本按硬件能力兼容，
  但不保证全部功能可用。
- **系统**：x86_64 Linux，GNOME Wayland 或 KDE Plasma 6 Wayland。
- **安装包**：Arch Linux、Ubuntu 24.04+/Debian 13+ 和当前 Fedora 稳定版。

暂不支持 X11、Plasma 5 和其他 Wayland 桌面。

## 安装

从 [GitHub Releases](https://github.com/adogecheems/panasonic-posture-manager/releases)
下载与系统匹配的安装包。Arch Linux 用户也可以使用 AUR helper 安装
`panasonic-posture-manager` 或 `panasonic-posture-manager-bin`。

### 预构建包

将示例版本替换为下载文件中的实际版本后执行：

```sh
version=2.1.1

# Arch Linux
sudo pacman -U "./panasonic-posture-manager-${version}-1-x86_64.pkg.tar.zst"

# Ubuntu 24.04+ / Debian 13+
sudo apt install "./panasonic-posture-manager_${version}-1_amd64.deb"

# 当前 Fedora 稳定版
sudo dnf install "./panasonic-posture-manager-${version}-1.x86_64.rpm"
```

### AUR

源码构建包和预构建包二选一，不能同时安装：

```sh
yay -S panasonic-posture-manager      # 从源码构建
yay -S panasonic-posture-manager-bin  # 安装预构建包
```

安装后注销并重新登录。首次使用前，建议将设备完整折叠、展开一次。

## 使用

| 模式 | 适用场景 | 效果 |
| --- | --- | --- |
| 自动 | 日常使用 | 根据设备姿态自动切换 |
| 笔记本 | 打字、精细操作 | 横屏，内置键盘和触控板可用 |
| 帐篷 | 观影、触控 | 旋转 180°，停用内置键盘和触控板 |
| 支架 | 桌面观看、触控 | 保持横屏，停用内置键盘和触控板 |
| 平板 | 手持、平放 | 交给系统自动旋转，停用内置键盘和触控板 |

帐篷模式默认开启，可在设置中关闭；关闭后不再自动识别或手动选择帐篷模式。
帐篷提示默认开启，支架提示默认关闭，可在设置中修改。锁屏时不显示提示，解锁后
若姿态仍合适会再次询问。

校准传感器时，将设备以笔记本形态平放在坚硬、水平的桌面上，保持约 2 秒，
再到“设置 → 传感器”点击“校准”。需要撤销时点击“恢复默认”。

## 常见问题

**后台服务未连接**：重新打开程序；仍然无效时注销并重新登录，或重启电脑。

**屏幕方向没有变化**：先点击“重试”。使用外接显示器时，请关闭镜像并检查
显示器排列。

**帐篷或支架提示没有出现**：确认提示已开启，并让设备保持稳定；提示通常只在
从笔记本姿态进入目标姿态时出现。

**点按提示后模式没有切换**：确认提示只会在设备仍处于可确认姿态时出现；若设备
已偏离目标区域，提示会暂时消失，恢复到目标姿态后会再次出现。

**内置键盘或触控板没有恢复**：在设置中选择“自动”；仍然无效时注销并重新登录。

**平板模式没有被识别**：将设备完整折叠、展开一次。

安装维护、服务状态和清理方式见[安装、打包与运维](packaging/aur/README.md)。

## 许可

本项目采用 GNU Affero General Public License v3.0 或更高版本
（`AGPL-3.0-or-later`），详见 [LICENSE](LICENSE)。
