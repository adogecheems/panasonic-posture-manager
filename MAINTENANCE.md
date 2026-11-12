# 安装、维护与构建

日常使用、模式说明和常见问题见[使用说明](README.md)。本页面向负责安装、排障、构建和发布的维护者。

## 安装

从 [GitHub Releases](https://github.com/adogecheems/panasonic-posture-manager/releases) 下载同一版本的安装包和 `SHA256SUMS`，放在同一目录并先校验：

```sh
sha256sum --ignore-missing --check SHA256SUMS
```

输出中应包含目标安装包，结果为 `OK`。

| 平台 | 安装命令 |
| --- | --- |
| Arch Linux | `sudo pacman -U ./panasonic-posture-manager-2.4.0-1-x86_64.pkg.tar.zst` |
| Ubuntu / Debian | `sudo apt install ./panasonic-posture-manager_2.4.0-1_amd64.deb` |
| Fedora | `sudo dnf install ./panasonic-posture-manager-2.4.0-1.x86_64.rpm` |
| Windows | 双击 `panasonic-posture-manager-2.4.0-windows-x64.msi`，完成后重启 |

文件名以实际发布版本为准。Ubuntu/Debian 需要 GTK 4.12+ 和 libadwaita 1.4+，依赖由包管理器安装。

AUR 提供源码包和预构建包：

```sh
yay -S panasonic-posture-manager
yay -S panasonic-posture-manager-bin
```

安装或升级 Linux 包后注销并重新登录。需要立即重启当前用户会话服务时运行：

```sh
systemctl --user restart panasonic-posture-manager-session.service
```

## 排查

### Linux

查看当前用户的桌面会话服务：

```sh
systemctl --user status panasonic-posture-manager-session.service
journalctl --user -b -u panasonic-posture-manager-session.service
```

查看系统硬件服务：

```sh
systemctl status panasonic-posture-manager.service
journalctl -b -u panasonic-posture-manager.service
```

Plasma 6 需要 `kscreenctl` 或 `kscreen-doctor`；两者都缺失时检查发行版的 KScreen 软件包。

只读硬件探测：

```sh
panasonic-posture-manager-probe
```

探测只读取 sysfs 和机型配置，不改设备状态，也不会提升支持等级。

### Windows

确认硬件服务状态和管理日志：

```powershell
Get-Service PanasonicPostureManagerHardware
Get-Content "$env:ProgramData\PanasonicPostureManager\Logs\hardware-service.log" -Tail 100
```

桌面会话日志位于：

```text
%LOCALAPPDATA%\PanasonicPostureManager\native-display.log
```

服务未安装或未运行时，界面只显示断线。重新运行 MSI 修复安装并重启；不要从源码或便携目录手工注册服务。

## 卸载

Linux 先停止用户会话，让程序恢复屏幕方向和输入状态：

```sh
systemctl --user stop panasonic-posture-manager-session.service
```

| 平台 | 卸载命令 |
| --- | --- |
| Arch Linux | `sudo pacman -Rns panasonic-posture-manager` |
| Arch Linux（`-bin` 包） | `sudo pacman -Rns panasonic-posture-manager-bin` |
| Ubuntu / Debian | `sudo apt remove panasonic-posture-manager` |
| Fedora | `sudo dnf remove panasonic-posture-manager` |
| Windows | 在「已安装的应用」中卸载，完成后重启 |

Linux 卸载后会保留以下状态，确认屏幕方向已恢复后可手动删除：

- `/var/lib/panasonic-posture-manager/settings.json`
- `${XDG_STATE_HOME:-$HOME/.local/state}/panasonic-posture-manager/`

Windows MSI 会停止并移除服务，同时恢复安装前的平板指示器配置。用户日志和恢复记录可在确认应用已正常退出后，从 `%LOCALAPPDATA%\PanasonicPostureManager` 清理。

## Windows 运维

正式安装支持 Windows 10/11 x64，当前只对 `Panasonic Corporation CFQV8-1L` 开放。MSI 包含受保护运行时、桌面界面、LocalSystem 硬件服务和可逆的 `ACPI\INT33D3` 非独占配置。

运行期边界：

- 物理姿态变化只接受 Intel HID Event Filter Event 115：`0xCC` 为平板，`0xCD` 为笔记本。
- 启动时只读取一次平板指示器和恢复记录，建立初始状态；无法明确时最多等待五秒，然后使用非平板安全基线。
- `ACPI\INT33D3` 只用于发布软件帐篷/支架姿态，不用于反推运行期物理状态，也不会发布 shell 平板模式或自动打开屏幕键盘。
- 界面通过经过身份校验的本地命名管道连接硬件服务；服务不可用时显示断线，不回退到模拟数据。
- 界面和会话进程以普通用户运行；显示操作与硬件写入都必须匹配服务登记、会话资格、姿态代次和租约。
- MSI 开始菜单快捷方式与界面进程使用同一个固定 AUMID；快捷方式重建后必须回读校验，避免通知来源回退为 `Python`。
- 所有显示写入使用恢复日志。锁屏、用户切换、硬件源失效或写入失败时，程序撤销确认并恢复原方向。
- 锁屏、睡眠或休眠会结束旧姿态事务。服务在恢复会话请求前检查主循环时钟跳变；资格或物理源恢复后，自动模式必须等新的稳定姿态，不能先发布临时笔记本目标再跳向帐篷。

Windows 产品保持内置键盘和触控板可用，不加载自研输入过滤驱动，不要求测试签名。源码目录不能作为特权运行时；正式服务只由 MSI 安装。

## 开发运行

Linux 依赖 Python 3.11+、C11 编译器、Make、gettext、PyGObject、GTK 4、libadwaita 和桌面 Wayland 组件。执行 `make` 构建 relay 和翻译后运行：

```sh
PYTHONPATH=src /usr/bin/python3 -m panasonic_posture.cli
```

Windows 源码运行使用 64 位 Python 3.11+：

```bat
py -3 -m venv .venv-windows
.venv-windows\Scripts\python -m pip install -r packaging\windows\requirements-ui.txt
packaging\windows\run.cmd --language zh_CN
```

支持的参数只有 `--language en|zh_CN` 和 Windows 专用的 `--background`。源码界面同样只连接已安装的硬件服务。

## 构建与验证

Linux 完整检查：

```sh
make WERROR=1 check
```

常用分类：

```sh
make check-unit
make check-c
make check-dbus
make check-i18n
```

Windows CI 与本地检查：

```powershell
$env:PYTHONPATH = "src"
python -m compileall -q src/panasonic_posture/windows src/panasonic_posture/fluent
python -m unittest discover -s tests -p "test_windows*.py" -v
python -m unittest discover -s tests -p "test_session_refresh.py" -v
python -m unittest discover -s tests -p "test_optional_input.py" -v
python -m unittest discover -s tests -p "test_frontend.py" -v
python -m unittest discover -s tests -p "test_fluent.py" -v
python tests/check_i18n.py
```

发行版打包：

| 平台 | 命令 | 输出 |
| --- | --- | --- |
| Arch | `(cd packaging/aur && makepkg --cleanbuild)` | 当前目录 |
| Debian / Ubuntu | `make package-deb` | `dist/packages/` |
| Fedora | `make package-rpm` | `dist/packages/` |
| Windows 便携包 | `python packaging\windows\build.py --dependencies build\windows-dependencies` | `dist/windows/` |
| Windows MSI | `python packaging\windows\build_msi.py --dependencies build\windows-dependencies` | `dist/windows/` |

Windows MSI 默认构建简体中文界面；需要英文界面时追加 `--culture en-US`。两种文化都使用同一套方形应用标志、欢迎/完成页背景、安装许可、主程序安装位置选择、最终确认页和重启提示，界面资源位于 `packaging/windows/msi/`。主程序可安装到用户选择的本地固定磁盘目录，输入上级目录时自动补全 `PanasonicPostureManager` 叶目录；安装器拒绝磁盘根、Windows/系统目录、固定服务目录、重解析点、过长路径和已有陌生文件的目录。静默安装必须在 `INSTALLFOLDER` 中给出包含该叶目录的完整绝对路径。硬件运行时和服务固定安装到 `Program Files\PanasonicPostureManagerService`，不随主程序路径变化。

Windows 构建前准备依赖和 WiX：

```bat
python -m pip install --target build\windows-dependencies -r packaging\windows\requirements-ui.txt
dotnet tool install --global wix --version 5.0.2
wix extension add -g WixToolset.UI.wixext/5.0.2
```

便携 ZIP 只用于开发和诊断，不包含服务安装入口。正式用户只安装 MSI。

Arch 产物可在隔离环境中继续检查：

```sh
make check-package PACKAGE=/path/to/package.pkg.tar.zst PACKAGE_NO_INSTALL=1
```

省略 `PACKAGE_NO_INSTALL=1` 会测试安装、重装和卸载，需要 root 或可用的 `unshare`。用 `PREVIOUS_PACKAGE=/path/to/old.pkg.tar.zst` 可测试升级。

## 发布

1. 同步 `pyproject.toml`、`__version__`、README、AUR、翻译文件头和其他发布元数据。
2. 运行版本一致性检查：

```sh
python packaging/scripts/release_version.py --tag v2.4.0
```

3. 通过完整测试后推送版本标签。发布工作流会构建 Arch、Deb、RPM 和 Windows MSI，生成 `SHA256SUMS`，上传 GitHub Release，并更新两个 AUR 包。
4. 重建已有版本时手动运行发布工作流，指定源码提交和标签；同名附件会被替换。

包内必须保留 Python、PyQt6、Qt、Fluent Widgets 及其传递依赖的许可。

## 扩展平台

先阅读 [AGENTS.md](AGENTS.md) 的模块地图和系统不变量。新平台至少需要实现：

| 部分 | 接口 | 要求 |
| --- | --- | --- |
| 传感器与输入 | `service/ports.py` | 统一到设备坐标和时间基准；不得伪造硬件状态。 |
| 桌面会话 | `session/ports.py` | 提供方向、方向锁、显示恢复日志和就绪回执。 |
| 界面通信 | `frontend/ports.py` | 校验服务快照；断线后丢弃旧结果。 |

共用层负责姿态策略、目标/结果分离、租约、恢复事务和状态发布。平台层只负责硬件、桌面和权限适配，不能用平台特例绕过共用事务。

现行协议版本为：状态快照 v2、C relay v4、硬件 profile v1、设置 v1、显示恢复日志 v1。修改兼容契约前须取得明确授权，并同步所有调用方、打包配置和契约测试。
