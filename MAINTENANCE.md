# 安装、打包与运维

机型、桌面要求和各姿态下的屏幕与键盘行为见[使用说明](README.md)。

## 安装

### 安装发行版软件包

从同一版本下载适合系统的安装包和 `SHA256SUMS`，放在同一目录。[GitHub Releases](https://github.com/adogecheems/panasonic-posture-manager/releases)

先在下载目录校验文件。输出中应有安装包的文件名，且结果为 `OK` 或「成功」。

```sh
sha256sum --ignore-missing --check SHA256SUMS
```

校验通过后，按发行版选择一条命令。以下示例使用 2.4.0，请按实际文件名替换。

| 发行版 | 安装命令 |
| --- | --- |
| Arch Linux | `sudo pacman -U ./panasonic-posture-manager-2.4.0-1-x86_64.pkg.tar.zst` |
| Ubuntu / Debian | `sudo apt install ./panasonic-posture-manager_2.4.0-1_amd64.deb` |
| Fedora | `sudo dnf install ./panasonic-posture-manager-2.4.0-1.x86_64.rpm` |

Ubuntu 和 Debian 包需要 GTK 4.12+ 和 libadwaita 1.4+，由 apt 安装依赖。

### AUR

两个包任选一个。使用 `yay` 时，从版本标签编译用 `yay -S panasonic-posture-manager`，直接安装发布好的 Arch 包用 `yay -S panasonic-posture-manager-bin`。

### 首次启动和升级

安装或升级后，注销并重新登录，桌面会自动启动新版用户服务。首次安装后想立即使用，也可以在当前 Wayland 会话中运行：

```sh
systemctl --user start panasonic-posture-manager-session.service
```

## 排查

**屏幕旋转或通知有问题**，先看当前用户的桌面服务。第一条命令看运行状态，第二条看本次开机以来的日志。

```sh
systemctl --user status panasonic-posture-manager-session.service
journalctl --user -b -u panasonic-posture-manager-session.service
```

Plasma 6 优先使用 `kscreenctl`，找不到时使用 `kscreen-doctor`。日志提示两者都缺失时，检查发行版的 KScreen 软件包。

**识别不到姿态，或键盘、触控板没有切换**，查看系统服务：

```sh
systemctl status panasonic-posture-manager.service
journalctl -b -u panasonic-posture-manager.service
```

检查机型兼容性可运行 `panasonic-posture-manager-probe`。它只读取 sysfs 和机型配置，以 JSON 列出匹配的机型、候选输入设备和加速度计。

## 卸载与清理

先关闭应用，在当前图形会话中停止用户服务，让它有机会恢复屏幕方向和方向锁。

```sh
systemctl --user stop panasonic-posture-manager-session.service
```

| 发行版 | 卸载命令 |
| --- | --- |
| Arch Linux | `sudo pacman -Rns panasonic-posture-manager` |
| Ubuntu / Debian | `sudo apt remove panasonic-posture-manager` |
| Fedora | `sudo dnf remove panasonic-posture-manager` |

若安装的是 AUR 预构建包，Arch 命令中的包名用 `panasonic-posture-manager-bin`。

卸载后会保留两处数据。要彻底清理，先确认桌面方向和方向锁已恢复，再按需删除。

- `/var/lib/panasonic-posture-manager/settings.json` 保存系统偏好和传感器校准。
- `${XDG_STATE_HOME:-$HOME/.local/state}/panasonic-posture-manager/` 保存当前用户的显示恢复记录。

## Windows 界面预览

在 Windows 的命令提示符中进入仓库根目录，使用 64 位 Python 3.11 或更高版本运行：

```bat
py -3 -m venv .venv-windows
.venv-windows\Scripts\python -m pip install -r packaging\windows\requirements-ui.txt
packaging\windows\preview.cmd --language zh_CN
```

脚本会加上 `--demo`，可在窗口中体验模式切换和校准。设置只保存在内存中。预览没有 Windows 设备后端或安装包，尚未在 Windows 真机上验收。

- **语言**　`--language en` 切换为英文，省略该参数则跟随环境语言。
- **场景**　例如 `packaging\windows\preview.cmd --scenario prompt` 打开确认提示。还可选择 `laptop`（笔记本）、`hardware`（物理平板）、`slow`（切换缓慢）、`error`（故障）或 `disconnected`（断线）。
- **资源**　默认从 `data/assets/` 加载，可用 `PANASONIC_POSTURE_DATA_DIR` 指定数据目录。源码预览优先使用 `locale/` 中已编译的翻译，也可直接读取 `po/zh_CN.po`。

要查看未连接后台服务时的界面，直接运行模块。只有演示模式接受 `--scenario`。

```bat
set PYTHONPATH=src
.venv-windows\Scripts\python -m panasonic_posture.fluent --language zh_CN
```

## 构建和验收

在仓库根目录用普通用户运行检查。D-Bus 集成测试会启动传感器探测，请在没有映射真实传感器的隔离环境中运行。准备 Python 3.11+、C 编译器、Make、gettext、PyGObject 和 `dbus-run-session`。GTK 界面测试还需要 PyCairo、GTK 4、libadwaita 和 `gtk4-broadwayd`。

```sh
make WERROR=1 check
```

在已安装预览依赖的 Linux 环境中，可以离屏测试 Qt 界面。缺少依赖时，控件测试会跳过，纯 Python 演示测试仍会运行。

```sh
PYTHONPATH=src QT_QPA_PLATFORM=offscreen python -m unittest discover -s tests -p test_fluent.py -v
```

### 发行版打包

| 目标 | 在仓库根目录运行 | 源码或产物 |
| --- | --- | --- |
| Arch | `(cd packaging/aur && makepkg --cleanbuild)` | 从 `pkgver` 对应的发布标签构建 |
| Ubuntu / Debian | `make package-deb` | 输出到 `dist/packages/` |
| Fedora | `make package-rpm` | 从干净工作树的 `HEAD` 构建，输出到 `dist/packages/` |

各发行版需要的工具见[发布工作流](.github/workflows/release.yml)。在 Arch 构建环境中，可先检查源码配方生成的包。将示例路径替换为实际文件名：

```sh
make check-package PACKAGE=/path/to/package.pkg.tar.zst PACKAGE_NO_INSTALL=1
```

去掉 `PACKAGE_NO_INSTALL=1` 可在隔离的 pacman 根目录中测试安装、重装和卸载，需要 root 或可用的 `unshare`。用 `PREVIOUS_PACKAGE=/path/to/old.pkg.tar.zst` 指定旧包可同时验证升级。发布前还需在目标设备上检查自动和手动切换、内置输入恢复、挂起唤醒和外接屏布局。

## 发布

1. 对齐项目、运行时、UI、AUR、翻译文件头和 README 的版本号。用待发布标签运行检查，例如 `/usr/bin/python3 packaging/scripts/release_version.py --tag v2.4.0`。
2. 推送版本标签。工作流从标签对应的提交构建 Arch、Deb 和 RPM 包，生成 `SHA256SUMS`，上传至 GitHub Release，再更新两个 AUR 包。
3. 重建已有版本时，手动运行工作流，选择要构建的提交，并在 `tag` 填写要更新的发布标签。同名附件会被替换。AUR 源码包仍从原标签构建，二进制包使用新上传的 Arch 包。

若将来分发 Windows 预览安装包，还需核对并附上 PyQt6 和 Fluent Widgets 的上游许可声明。

## 平台适配

Python 源码位于 `src/panasonic_posture/`。扩展平台时，复用共用层的姿态判断和恢复流程，在平台入口接入硬件、桌面和权限检查。各模块的职责和对应测试见[维护手册](AGENTS.md#4-定位代码与测试)。

| 要接入的部分 | 入口 | 实现要求 |
| --- | --- | --- |
| 传感器与输入 | `service/ports.py` | 将三轴加速度换算到设备坐标系，单位为 m/s²，时间戳与引擎采样时钟一致。宿主完成鉴权后传入 `SessionOwner`，执行器用 `satisfies(target)` 报告输入是否已切换。 |
| 桌面会话 | `session/ports.py` | 读取和设置屏幕方向、报告就绪状态，显式提供 `DisplayJournal`。持久化由平台实现，恢复前核对输出和设置的归属。 |
| 界面 | `frontend/ports.py` | 通过 `UIClient` 发起操作，从已校验的服务快照读取结果。在界面主循环派发状态和完成回调，断线后丢弃旧结果。 |

Linux 由 `service/application.py` 和 `session/agent.py` 组装这些组件。下面几处需要配套维护：

- **硬件与状态**　`hardware/profile.py` 负责 Linux 设备匹配，`SensorInfo` 描述传感器，`DeviceInfo` 记录当前平台的机型和支持等级。新平台需单独验证。状态快照为 v2，设置和显示日志均为 v1。
- **输入协议**　系统服务和 C relay 必须来自同一构建。relay v4 用 `1` 接管输入，`0` 转发物理开关，两者均续接三秒租约。服务按 32 位 `request` 序号匹配回执。从物理平板展开时，桌面的平板信号最多再保留两秒。
- **登录与亮屏**　会话从 logind 识别登录界面，从 Mutter 的 `PowerSaveMode` 或显示器的 DRM `dpms` 读取熄屏状态。此时通过 `SessionUpdateUnattended` 报告准备状态，旧服务回退到普通心跳。亮屏后，以下一次稳定姿态为准，再恢复日常提示。
- **触摸亮屏**　软件接管期间，relay 只观察唯一直接触摸屏的接触开始。会话用 `SessionWake` 提交当前代次和两秒内有效的 `touch_wake` 令牌。服务核对身份、租约和已应用模式后，relay 再检查触摸是否有效，每次接触最多发一次 `KEY_WAKEUP`。唤醒不会续租。
- **从挂起唤醒**　`SetSuspendTouchWake` 使用已有配置权限。系统服务先只读核对唯一固定 USB 触摸屏到根集线器及 PCI USB 控制器的唤醒路径；上游节点须已启用唤醒，否则报告不可用。服务只管理触摸屏自身的 `power/wakeup`，每五秒及挂起、恢复时核对设备。关闭选项或上游条件失效时，按 `/run/panasonic-posture-manager/` 中的设备实例记录恢复自己改过的设置。根集线器为共享节点，本机试验中开启后出现未触摸即自动恢复，程序因此不自动打开该节点。
