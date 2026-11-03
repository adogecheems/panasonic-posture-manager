# 松下二合一平板电脑姿态管理工具

面向 Linux 二合一翻转本的姿态管理工具，重点支持 Panasonic QV 系列。程序根据
底座方向切换屏幕旋转和内置输入状态，也可手动选择模式。关闭窗口不会停止后台
服务。

> **当前版本：1.0.0。** 首个公开发行版；AUR 安装包仍在提交准备阶段。

![当前姿态界面（浅色主题）](docs/screenshots/current-light.png)

## 功能

- 自动识别笔记本、帐篷、支架和平板姿态。
- 帐篷与支架模式可选二次确认，减少误触发。
- 硬件平板开关优先于所有软件设置。
- 支持传感器方向校准，并可随时恢复默认。
- 提供中文、英文界面，跟随系统深浅色主题。

## 支持范围

- **正式支持机型**：Panasonic CFQV8-1L（QV8）、CFQV9-1L（QV9）、CFQV1-1L（QV1）
  及对应型号变体。三款使用同族硬件画像；目前 QV8 已完成主要功能实测，QV9/QV1
  尚未逐机物理验收。
- **兼容模式**：其他 Panasonic 360° 翻转本及其他品牌可转换设备按硬件能力自动
  适配，但不保证所有传感器、开关和内置输入设备都兼容。
- **系统**：Arch Linux x86_64。AUR 提交完成后可通过 AUR 安装；当前可直接从
  源码 tag 构建安装包。
- **桌面**：GNOME Wayland、KDE Plasma 6 Wayland。仅支持 Wayland 会话，X11 不受
  支持。最低声明为 GNOME 45、GTK 4.12、libadwaita 1.4。

桌面显示后端自动选择：

| 会话 | 后端 | 能力 |
| --- | --- | --- |
| GNOME Wayland | Mutter DisplayConfig | 显式旋转和方向锁 |
| Plasma 6 Wayland | KScreen（`kscreenctl` 或 `kscreen-doctor`） | 显式旋转和自动旋转策略 |
| 其他 Wayland 合成器 | 无 | 不支持屏幕姿态控制 |
| X11（包括 GNOME/Plasma 6 X11） | 无 | 不支持 |

界面基于 GTK 4/libadwaita。Plasma 6.8 及以后使用 `kscreenctl`；6.0–6.7 使用
`kscreen-doctor`。实机验证主要基于 GNOME 51.1；Plasma 6 Wayland 的测试状态见
[docs/hardware-validation.md](docs/hardware-validation.md)。

如需在某台机器上临时禁用自动控制，可在 `/etc/panasonic-posture-manager/profiles/`
放置匹配的 profile，并设置 `"support_tier": "unsupported"`。格式见
[docs/hardware-profiles.md](docs/hardware-profiles.md)。

## 模式

| 模式 | 适用场景 | 行为 |
| --- | --- | --- |
| 自动 | 日常使用 | 根据当前姿态自动切换 |
| 笔记本 | 打字、精细操作 | 横屏，内置键盘和触控板可用 |
| 帐篷 | 观影、触控 | 旋转 180°，停用内置键盘和触控板 |
| 支架 | 桌面观看、触控 | 保持横屏，停用内置键盘和触控板 |
| 平板 | 手持、平放 | GNOME/Plasma 6 交给系统自动旋转，停用内置键盘和触控板 |

帐篷、支架和软件平板模式由程序接管。硬件平板开关打开时，硬件状态优先，手动
模式暂不可用。

### 自动模式

- 短暂晃动不会触发切换；新姿态需要持续稳定。
- 帐篷提示默认开启。只有从笔记本姿态（自动或手动笔记本模式）进入帐篷时才
  询问；从其他任意非笔记本模式进入帐篷会直接切换，已经确认过帐篷后短暂离开
  再返回也不会重复询问。
- 支架提示默认关闭。开启后，从笔记本姿态（自动或手动笔记本模式）进入支架时
  先询问；从其他任意非笔记本模式进入支架会直接切换。
- 提示可通过桌面通知或配置窗口确认。锁屏期间不显示提示，解锁后如姿态仍合适
  会重新显示。
- 硬件平板开关优先。程序会解除自己写入的方向锁，把旋转交还系统；退出平板后
  再恢复原来的锁设置。
- 传感器短暂无数据不会释放软件模式；持续约 5 秒没有有效样本时才交还系统原生状态。
- 手动模式在后台服务重启后回到自动；帐篷和支架提示开关会保存。

## 安装

### 从源码构建

AUR 提交完成前，可以直接从 `v1.0.0` 源码 tag 构建并安装。需要 Arch Linux、
`base-devel` 和可用的网络连接：

```sh
git clone --branch v1.0.0 --depth 1 \
  https://github.com/adogecheems/panasonic-posture-manager.git
cd panasonic-posture-manager/packaging/aur
makepkg -si
```

`makepkg -si` 会安装构建/运行依赖、执行测试、生成安装包并交给 pacman 安装。
安装完成后注销并重新登录，用户会话适配器会随下一次图形登录启动。

### 从 AUR 安装

AUR 包提交完成后，可直接安装 `panasonic-posture-manager`。首次安装后：

1. 注销并重新登录，启动用户会话适配器；系统服务会由安装脚本或 D-Bus 自动启动。
2. 从应用菜单打开“松下二合一平板电脑姿态管理工具”。

桌面依赖：

- GNOME：`gnome-shell`（通知）、`mutter`、`gnome-settings-daemon`。
- Plasma 6 Wayland：`kscreen`（6.8+）或 `libkscreen`（6.0–6.7）。

AUR 包会将这些桌面组件声明为可选依赖。装好对应依赖后，通常无需额外配置。首次
使用时建议将设备完整折叠、展开一次，以便平板开关被识别。

## 使用

窗口包含“当前姿态”和“设置”两页。

- **当前姿态**：显示设备形态，并在需要时提供“进入帐篷模式”“进入支架模式”或
  “重试”。
- **模式**：可选择自动、笔记本、帐篷、支架或平板，切换后立即生效。
- **提示**：可分别开关帐篷和支架模式提示。
- **传感器校准**：将设备以笔记本形态平放在坚硬、水平的桌面上，保持约 2 秒，
  然后在“设置 → 传感器”中点击“校准”。需要撤销时点击“恢复默认”。

## 常见问题

**显示“后台服务未连接”**

先关闭并重新打开程序。仍然无效时，注销并重新登录；若问题持续，重启电脑。可用
以下命令检查服务：

```sh
systemctl status panasonic-posture-manager.service
systemctl --user status panasonic-posture-manager-session.service
```

**一直显示“正在等待桌面功能就绪”**

确认当前会话受支持。GNOME Wayland 需要 `mutter` 和 `gnome-settings-daemon`；
Plasma 6 Wayland 需要 `kscreenctl` 或 `kscreen-doctor`。X11 和其他 Wayland
合成器暂不支持。快速切换用户后，请回到本机桌面再操作。

**屏幕方向没有改变**

先点击“重试”。使用外接显示器时，请关闭镜像并检查显示器排列；GNOME 会拒绝
无法安全应用的内屏旋转。Plasma 可检查 `kscreenctl list-outputs --json` 或
`kscreen-doctor -j` 是否识别到内屏。若处于硬件平板模式，请检查桌面自身的自动
旋转设置。

**帐篷或支架提示没有出现**

确认对应提示开关已开启，并让设备保持稳定。提示通常只在从笔记本姿态进入目标
姿态时出现；从其他姿态切换会直接生效。锁屏时提示会隐藏，解锁后如姿态仍合适会
重新显示。

**内置键盘或触控板没有恢复**

在“设置”中选择“自动”。若仍未恢复，注销并重新登录，或重启电脑。

**系统没有识别到平板模式**

将设备完整折叠后展开一次。部分机型在首次折叠前还不会暴露平板开关（诊断状态为
`absent`），这属于正常现象。

## 已知限制

- CFQV8-1L 的物理折叠、帐篷提示点击、180° 触摸坐标、外接屏布局和休眠/唤醒仍
  在最终验收中；QV9/QV1 尚未逐机物理验收。
- 其他品牌或未适配的机型使用兼容模式；若探针显示开关、键盘、触控板或加速计不
  匹配，需要补充 profile 或做专项适配。
- 程序判断的是底座方向，不是铰链角度。少数特殊摆放方式可能被归入相近模式。
- GNOME 下镜像内屏、内屏未启用或外接屏布局冲突时，旋转会被拒绝。各桌面后端的
  外接屏布局仍需逐机验收。
- 硬件平板模式下的自动旋转取决于固件及 GNOME/Plasma 6 的实现，个别机型可能
  不可用。

## 卸载

正式包安装后，可执行：

```sh
sudo pacman -Rns panasonic-posture-manager
```

卸载不会删除已保存的偏好设置和显示恢复日志。完整清理方法见
[packaging/aur/README.md](packaging/aur/README.md)。

## 更多文档

- [agent.md](agent.md)：AI agent 的授权边界、系统不变量和验证门槛。
- [CHANGELOG.md](CHANGELOG.md)：版本更新记录。
- [docs/hardware-profiles.md](docs/hardware-profiles.md)：机型适配与 profile 格式。
- [docs/hardware-validation.md](docs/hardware-validation.md)：实机验证记录。
- [packaging/aur/README.md](packaging/aur/README.md)：打包状态和发布清单。

## 许可

本项目采用 GNU Affero General Public License v3.0 或更高版本
（`AGPL-3.0-or-later`）。许可证正文见 [LICENSE](LICENSE)。
