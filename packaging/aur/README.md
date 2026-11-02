# AUR 打包与发布说明（0.3.x）

> **当前状态：草稿，禁止提交到 AUR。**
>
> 源码、构建脚本与 wheel 构建流程已经就绪；但上游 Git 仓库与 `v0.3.0` tag 尚未
> 发布，clean chroot 验收、最终元数据复核和真机验收也未完成。在此之前，本地
> 构建成功不等于可以发布。

相关文档：用户说明见 [README.md](../../README.md)，实现契约见
[agent.md](../../agent.md)，机型验收见
[docs/hardware-profiles.md](../../docs/hardware-profiles.md)。

## 当前状态

| 项目 | 状态 |
| --- | --- |
| 许可证 | 已确定为 `AGPL-3.0-or-later`，`PKGBUILD`、`.SRCINFO`、`pyproject.toml` 与 `LICENSE` 一致 |
| 源码 | 从固定 Git tag `v0.3.0` 构建；公开仓库与 tag 尚未就绪 |
| 校验和 | VCS source 使用 `sha256sums=('SKIP')`；由 tag 固定源码版本，这不是发布阻断项 |
| 构建脚本 | `build()`、`check()`、`package()` 均已编写；尚未在 clean chroot 中实际运行 `makepkg` |
| 发布状态 | **不可提交 AUR，不能视为可复现发布** |

## 发布阻断项

1. **公开仓库与 tag 尚不可获取**：`PKGBUILD` 当前指向
   `https://github.com/adogecheems/panasonic-posture-manager.git`，并请求
   `v0.3.0` tag。必须创建公开仓库、推送对应 tag，并确认匿名用户可以取得源码；
   在此之前 `makepkg` 无法完成下载阶段。
2. **clean chroot 验收未完成**：必须使用干净 Arch chroot/devtools 环境构建，运行
   `make WERROR=1 check`，再检查安装、升级、D-Bus 自动激活、用户会话适配器、
   休眠/唤醒与卸载；同时运行 `namcap`。
3. **元数据未最终复核**：提交前确认 Maintainer、`pkgver`、`pkgrel`、依赖、
   optional dependencies、安装脚本和 `.SRCINFO` 全部一致。
4. **真机验收未完成**：CFQV8-1L 仍需补齐物理折叠、平板开关、帐篷模式提示点击、
   180° 触摸坐标、外接屏布局和休眠唤醒；此外还需至少一台非 CFQV8 机型按
   [docs/hardware-profiles.md](../../docs/hardware-profiles.md) 完成物理验收。

## 安装布局

| 源 | 安装位置 |
| --- | --- |
| Python wheel | `/usr/lib/pythonX.Y/site-packages/panasonic_posture/`（由 python-installer 决定） |
| wheel 入口点 | `/usr/bin/panasonic-posture-manager`、`/usr/bin/panasonic-posture-manager-probe` |
| `data/ui`、`data/assets`、`data/profiles` | `/usr/share/panasonic-posture-manager/` |
| `data/assets/work.mmoe.panasonic.PostureManager.png` | `/usr/share/icons/hicolor/512x512/apps/` |
| `backend/relay` | `/usr/lib/panasonic-posture-manager/relay` |
| `locale/**/*.mo` | `/usr/share/locale/` |
| 系统/用户 systemd unit | `/usr/lib/systemd/{system,user}/` |
| D-Bus policy/activation | `/usr/share/dbus-1/{system.d,system-services}/` |
| polkit action | `/usr/share/polkit-1/actions/` |
| desktop entry | `/usr/share/applications/` |
| `packaging/modules-load.d/uinput.conf` | `/usr/lib/modules-load.d/panasonic-posture-manager.conf` |
| `LICENSE` | `/usr/share/licenses/panasonic-posture-manager/LICENSE` |

### 构建与打包行为

- `build()` 从 `v${pkgver}` Git tag 取得源码，先删除可能误入源码树的
  `backend/relay`、`backend/*.gch`、中文 `.mo`、`build/`、`dist/` 与
  `src/*.egg-info`，再运行 `make` 和 PEP 517 wheel 构建。
- `check()` 运行 `make check`，覆盖 relay 主机侧测试、Python 单元测试、
  session-bus 集成测试和 i18n 检查。构建环境需要 gcc、gettext、PyGObject 与
  `dbus-run-session`。
- `package()` 使用 `python-installer` 安装 wheel，再安装 UI、relay、locale、
  systemd、D-Bus、polkit、desktop 与模块加载文件。
- 包内不会写入 `/usr/local`；`package()` 会用 `grep -rIq` 检查并拒绝包含该路径
  的文本文件。

tag 必须指向准备发布的提交。发布后不要移动或重写该 tag，否则同一 `pkgver` 可能
构建出不同内容。

## 本地构建与检查（不安装到系统）

在仓库根目录：

```sh
make clean
make WERROR=1 check
python -m build --wheel --no-isolation
stage="$(mktemp -d)"
trap 'rm -rf -- "$stage"' EXIT
python -m installer --destdir="$stage" dist/*.whl
```

最后一条只验证 Python wheel 的入口点与元数据，不会安装 UI、relay、systemd、
D-Bus 或 polkit 文件。完整布局需要在公开仓库和 `v0.3.0` tag 就绪后，在 clean
chroot 中运行 `makepkg --cleanbuild` 验证，不能用本地工作树替代发布源码。

## 首次安装后的当前会话

`.install` 会立即启用并启动系统服务，并 `systemctl --global enable` 用户适配器；
pacman 不会替当前图形会话启动新的用户单元。首次安装后重新登录一次，或执行：

```sh
systemctl --user start panasonic-posture-manager-session.service
```

检查状态：

```sh
systemctl status panasonic-posture-manager.service
systemctl --user status panasonic-posture-manager-session.service
gdbus call --system --dest work.mmoe.panasonic.Posture \
  --object-path /work/mmoe/panasonic/Posture \
  --method work.mmoe.panasonic.Posture.GetState
```

确认 `session_connected` 为 `true`，并且配置窗口不再显示后台不可用或等待桌面
就绪的提示。若系统服务正常但当前窗口仍未连上，关闭并重新打开配置窗口。

## 旧版与卸载

0.2.x 若曾使用 `/usr/local` 手工安装，pacman 不会清理旧文件。升级前先停用旧
服务，删除旧 unit、wants 链接与 `/usr/local` 下的程序文件，再确认
`systemctl cat panasonic-posture-manager.service` 指向 `/usr/lib` 中的新 unit。
不要在旧 unit 仍启用时安装新包，否则 systemd 可能同时加载两份代码。

卸载包：

```sh
sudo pacman -Rns panasonic-posture-manager
```

运行期数据不由 pacman 删除。需要彻底清理时：

```sh
sudo rm -rf -- /var/lib/panasonic-posture-manager
state_root="${XDG_STATE_HOME:-${HOME:?HOME must be set}/.local/state}"
rm -rf -- "$state_root/panasonic-posture-manager"
```

`journalctl` 日志按用户需要另行 vacuum；删除 `/var/lib` 前请确认不再需要保留
设置。包卸载时会停用系统服务，但不会删除这些运行期数据。

## 发布检查清单

- [ ] 创建公开仓库，确认源码 URL 可匿名克隆，并让 `v0.3.0` tag 指向最终提交。
- [ ] 确认 `pkgver`、源码 tag、`pyproject.toml` 版本三者一致。
- [ ] 确认 `AGPL-3.0-or-later` 在 `LICENSE`、`PKGBUILD`、`.SRCINFO` 与
  `pyproject.toml` 中一致。
- [ ] 复核 Maintainer、依赖、可选依赖、安装脚本和所有安装路径。
- [ ] 运行 `makepkg --printsrcinfo > .SRCINFO`，确认没有未同步的元数据。
- [ ] 在 clean chroot 中运行 `make WERROR=1 check`、wheel 构建、
  `makepkg --cleanbuild` 和 `namcap`。
- [ ] 验证全新安装、升级、D-Bus 自动激活、用户会话适配器、休眠唤醒和卸载。
- [ ] 确认包内没有 `/usr/local` 引用、预构建 relay、`.mo`、`build/` 或 `dist/`。
- [ ] 在 CFQV8-1L 补齐物理折叠、平板开关、帐篷模式提示点击、180° 触摸坐标、外接屏
  布局和休眠唤醒验收。
- [ ] 在至少一台其他目标机型完成
  [docs/hardware-profiles.md](../../docs/hardware-profiles.md) 的物理验收。
