# 安装、打包与运维

> 面向安装维护。普通用户请先看 [README](README.md)。

本页说明程序安装、发布和日常维护。程序面向 Panasonic CFQV8、CFQV9、CFQV1，
运行于 GNOME Wayland 或 KDE Plasma 6 Wayland。

## 安装

### 发行版安装包

从 GitHub Releases 下载与系统匹配的安装包，并将下面的 `<version>` 替换为
文件中的实际版本：

```sh
# Arch Linux
sudo pacman -U ./panasonic-posture-manager-<version>-1-x86_64.pkg.tar.zst

# Ubuntu 24.04+ / Debian 13+
sudo apt install ./panasonic-posture-manager_<version>-1_amd64.deb

# 当前 Fedora 稳定版
sudo dnf install ./panasonic-posture-manager-<version>-1.x86_64.rpm
```

Debian/Ubuntu 包需要 GTK 4.12+ 和 libadwaita 1.4+。其他发行版尚未验证。

### AUR

源码构建包和预构建包二选一，不能同时安装：

```sh
yay -S panasonic-posture-manager      # 从源码构建
yay -S panasonic-posture-manager-bin  # 安装预构建包
```

### 首次启动

安装后注销并重新登录。若只想让当前会话立即生效，可以启动用户服务：

```sh
systemctl --user start panasonic-posture-manager-session.service
```

首次使用前，建议将设备完整折叠、展开一次。

## 使用与排查

- **后台服务未连接**：重新打开程序；仍然无效时注销并重新登录。
- **屏幕方向没有变化**：点击“重试”；使用外接屏时关闭镜像并检查显示器布局。
- **键盘或触控板未恢复**：在设置中选择“自动”；仍然无效时注销并重新登录。
- **平板模式未被识别**：将设备完整折叠、展开一次后再试。

查看后台状态：

```sh
systemctl status panasonic-posture-manager.service
systemctl --user status panasonic-posture-manager-session.service
```

## 打包与发布

发布前先运行完整检查，再按 `PKGBUILD` 构建并验收安装包：

```sh
make WERROR=1 check
(cd packaging/aur && makepkg --cleanbuild)
```

推送 `v*` tag 后，发布流程会自动生成 Arch、Debian/Ubuntu 和 Fedora 包及
`SHA256SUMS`，并更新 AUR。发布前应在目标设备确认姿态切换、屏幕方向、内置输入
恢复和外接屏布局；自动化检查不能代替真机验收。

## 卸载与清理

使用安装时的包管理器卸载：

```sh
sudo pacman -Rns panasonic-posture-manager
# 或
sudo apt remove panasonic-posture-manager
# 或
sudo dnf remove panasonic-posture-manager
```

设置、校准和恢复日志不会随软件卸载自动删除。需要彻底清理时执行：

```sh
sudo rm -rf -- /var/lib/panasonic-posture-manager
rm -rf -- "${XDG_STATE_HOME:-$HOME/.local/state}/panasonic-posture-manager"
```

## 许可

本项目采用 GNU Affero General Public License v3.0 或更高版本，详见 `LICENSE`。
