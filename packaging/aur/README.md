# AUR 打包与发布说明

> **状态：AUR 提交准备中。** 1.0.0 源码 tag、包元数据和本地安装包验收已收口；
> clean-chroot 构建、`namcap`、AUR 提交和最终真机验收尚未完成。

相关文档：

- 用户说明：[../../README.md](../../README.md)
- AI agent 边界：[../../agent.md](../../agent.md)
- 机型适配：[../../docs/hardware-profiles.md](../../docs/hardware-profiles.md)
- 实机记录：[../../docs/hardware-validation.md](../../docs/hardware-validation.md)

## 当前状态

| 项目 | 状态 |
| --- | --- |
| 许可证 | `AGPL-3.0-or-later`；`LICENSE`、`PKGBUILD`、`.SRCINFO` 和 `pyproject.toml` 一致 |
| 上游仓库 | 可匿名克隆 |
| 版本 tag | `v1.0.0` 指向 1.0.0 发布提交 |
| 构建脚本 | `build()`、`check()`、`package()` 已编写 |
| clean chroot | 尚未运行 `makepkg`、`namcap` 和完整安装验收 |
| 安装包验收 | 本地 `makepkg` 与 `make check-package` 已通过；clean-chroot 复验待完成 |
| AUR 提交 | 尚未提交 |
| 真机验收 | CFQV8-1L 最终物理验收、QV9/QV1 回归和桌面后端验收未完成 |

## AUR 提交前待办

1. **clean chroot 未验收。** 需要在干净 Arch/devtools 环境中构建，运行
   `make WERROR=1 check`、`makepkg`、`make check-package` 和 `namcap`，再检查真实
   安装脚本、D-Bus 激活、会话启动、休眠/唤醒和卸载。
2. **AUR 尚未提交。** 将 `PKGBUILD`、`.SRCINFO`、安装脚本和打包说明提交到 AUR；
   `v1.0.0` 已是不可变发布 tag，不要移动或重写。
3. **真机验收未完成。** 详见
   [../../docs/hardware-validation.md](../../docs/hardware-validation.md)。

## 安装布局

| 源 | 安装位置 |
| --- | --- |
| Python wheel | `/usr/lib/pythonX.Y/site-packages/panasonic_posture/`，由 `python-installer` 决定 |
| wheel 入口点 | `/usr/bin/panasonic-posture-manager`、`/usr/bin/panasonic-posture-manager-probe` |
| `data/ui`、`data/assets`、`data/profiles` | `/usr/share/panasonic-posture-manager/` |
| 应用图标 | `/usr/share/icons/hicolor/512x512/apps/work.mmoe.panasonic.PostureManager.png` |
| `backend/relay` | `/usr/lib/panasonic-posture-manager/relay` |
| 翻译目录 | `/usr/share/locale/` |
| systemd unit | `/usr/lib/systemd/{system,user}/` |
| D-Bus policy/activation | `/usr/share/dbus-1/{system.d,system-services}/` |
| polkit action | `/usr/share/polkit-1/actions/` |
| desktop entry | `/usr/share/applications/` |
| uinput modules-load 配置 | `/usr/lib/modules-load.d/panasonic-posture-manager.conf` |
| `LICENSE` | `/usr/share/licenses/panasonic-posture-manager/LICENSE` |

## 打包行为

- `build()` 从固定 tag 获取源码，删除可能误入源码树的构建产物，再运行 `make` 和
  PEP 517 wheel 构建。
- `check()` 运行 `make check`，覆盖 relay 主机侧测试、Python 单元测试、session-bus
  集成和 i18n。构建环境需要 gcc、gettext、PyGObject 和 `dbus-run-session`。
- `package()` 使用 `python-installer` 安装 wheel，再安装 UI、relay、翻译、systemd、
  D-Bus、polkit、desktop、modules-load 和许可证文件。
- 构建前会删除源码树中可能误入的预构建 relay、旧 `.mo`、`build/` 和 `dist/`；
  最终只安装本次构建生成的可执行文件和翻译文件。
- 安装树不允许出现 `/usr/local` 引用。
- 源码由不可变 tag 固定，因此 `sha256sums=('SKIP')` 可以接受；若 tag 被重写，
  可复现构建即失效。

## 本地检查

以下命令只验证构建和 wheel；它不会安装 UI、relay、systemd、D-Bus 或 polkit 文件。

```sh
make clean
make WERROR=1 check
python -m build --wheel --no-isolation
stage="$(mktemp -d)"
trap 'rm -rf -- "$stage"' EXIT
python -m installer --destdir="$stage" dist/*.whl
```

完整布局必须在 tag 就绪后，从仓库根目录使用 clean chroot 验证：

```sh
(cd packaging/aur && makepkg --cleanbuild)
make check-package
namcap packaging/aur/panasonic-posture-manager-*.pkg.tar.zst
```

## 安装包验收

构建出 `.pkg.tar.zst` 后，运行：

```sh
make check-package PACKAGE=/path/to/panasonic-posture-manager-1.0.0-1-x86_64.pkg.tar.zst
```

如果提供了上一版安装包，还应验证真实升级路径：

```sh
make check-package \
  PACKAGE=/path/to/panasonic-posture-manager-1.0.0-1-x86_64.pkg.tar.zst \
  PREVIOUS_PACKAGE=/path/to/panasonic-posture-manager-0.3.0-8-x86_64.pkg.tar.zst
```

只做静态包检查、不执行 pacman 事务时：

```sh
make check-package PACKAGE=/path/to/package.pkg.tar.zst PACKAGE_NO_INSTALL=1
```

验收脚本会核对：

- `.PKGINFO`、`.SRCINFO` 和 `pyproject.toml` 的版本、许可证、依赖与安装脚本是否
  一致；
- 包内文件集合、权限、ELF/脚本类型，以及 Python 模块、UI、资源、profile、
  翻译和命令入口是否完整；
- 包内文件是否与当前源码逐字节一致；
- systemd unit、D-Bus 激活文件与策略、polkit 策略、desktop entry 和
  `modules-load.d` 配置；
- 在临时 pacman 根目录中执行全新安装、重装，或在提供 `PREVIOUS_PACKAGE` 时执行
  升级，并验证卸载不残留包文件。普通用户环境下会使用 `unshare --map-root-user`，
  不写入宿主机的 `/usr`。

该检查不会执行 `.install` 中的 systemctl 命令，也不会启动系统服务、D-Bus
激活、用户会话适配器或访问真实硬件。这些仍须在 clean chroot 或目标机上按
“发布检查清单”手工验收。`namcap` 仍需单独运行。

## 首次安装后的会话

`.install` 会立即启用并启动系统服务，并全局启用用户会话单元。pacman 不会为当前
已登录会话启动新的用户单元，因此首次安装后需要重新登录，或执行：

```sh
systemctl --user start panasonic-posture-manager-session.service
```

检查服务：

```sh
systemctl status panasonic-posture-manager.service
systemctl --user status panasonic-posture-manager-session.service
gdbus call --system --dest work.mmoe.panasonic.Posture \
  --object-path /work/mmoe/panasonic/Posture \
  --method work.mmoe.panasonic.Posture.GetState
```

确认 `session_connected` 为 `true`，且配置窗口不再提示后台不可用或等待桌面就绪。
若系统服务正常但窗口仍离线，关闭并重新打开窗口。

## 升级与卸载

如果在 0.2.x 时期使用过 `/usr/local` 手工安装，pacman 不会清理旧文件。升级前
先停用旧服务，删除旧 unit、wants 链接和 `/usr/local` 下的程序文件，再确认
`systemctl cat panasonic-posture-manager.service` 指向 `/usr/lib`。

卸载：

```sh
sudo pacman -Rns panasonic-posture-manager
```

运行期数据不会被 pacman 删除。需要彻底清理时：

```sh
sudo rm -rf -- /var/lib/panasonic-posture-manager
state_root="${XDG_STATE_HOME:-${HOME:?HOME must be set}/.local/state}"
rm -rf -- "$state_root/panasonic-posture-manager"
```

`journalctl` 日志需按需另行清理。删除 `/var/lib` 前请确认不再需要提示设置和
传感器校准。

## 发布检查清单

- [x] 确认上游仓库可匿名克隆，`v1.0.0` tag 指向最终提交。
- [x] 确认 `pkgver`、源码 tag 和 `pyproject.toml` 版本一致。
- [x] 确认许可证在 `LICENSE`、`PKGBUILD`、`.SRCINFO` 和 `pyproject.toml` 中一致。
- [x] 复核 Maintainer、依赖、可选依赖、安装脚本和全部安装路径。
- [x] 运行 `makepkg --printsrcinfo`，确认 `.SRCINFO` 与 `PKGBUILD` 同步。
- [x] 在本地构建 wheel 和 AUR 包，运行 `make check-package` 验证包内容与隔离安装。
- [ ] 在 clean chroot 中运行 `make WERROR=1 check`、wheel 构建、`makepkg`、
  `make check-package` 和 `namcap`。
- [ ] 验证全新安装、升级、D-Bus 自动激活、用户会话适配器、休眠/唤醒和卸载。
- [ ] 验证 GNOME Wayland/X11、Plasma 6 Wayland 和普通 X11 的旋转、恢复、锁屏
  通知、外接屏路径，以及 X11 每个旋转角的触摸、多指和手写笔映射。
- [ ] 确认发布源码没有预构建 relay、残留 `.mo`、`build/` 或 `dist/`，安装树没有
  `/usr/local` 引用。
- [ ] 补齐 CFQV8-1L 物理验收和 QV9/QV1 同族回归。
