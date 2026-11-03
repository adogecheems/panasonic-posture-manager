# 安装、打包与运维

> 面向维护者。普通用户请先看 [README](../../README.md)。

**2.1.0 发布流程。** 推送 `v2.1.0` tag 后，GitHub Actions 会生成三种预构建包、
GitHub Release 和 AUR 元数据；真机验收状态以文末清单为准。已有 2.0.0 release
不会被改写。

## 预构建包

从 2.1.0 起，每个 release 附带三种 x86_64 包和 `SHA256SUMS`；已有 `v2.0.0`
release 不含新增的 `.deb`/`.rpm` 资产。

- `panasonic-posture-manager-<version>-1-x86_64.pkg.tar.zst`（Arch Linux）
- `panasonic-posture-manager_<version>-1_amd64.deb`（Ubuntu 24.04+/Debian 13+）
- `panasonic-posture-manager-<version>-1.x86_64.rpm`（当前 Fedora 稳定版）

完整安装命令见[用户文档](../../README.md#预构建包)。Debian 包声明 GTK 4.12+
和 libadwaita 1.4+ 依赖；RPM 在 Fedora 构建，其他 RPM 发行版尚未验证。

## 自动发布

`.github/workflows/release.yml` 在推送匹配 `v[0-9]*` 的 tag 时运行，也可以在
Actions 页面手动指定包含该工作流的 tag 重跑；不含工作流的历史 tag 无法重跑：

1. 校验 tag 与 `pyproject.toml`、运行时、UI、AUR 元数据中的版本完全一致。
2. 在 Arch 容器中运行完整检查、构建 `.pkg.tar.zst` 并执行包验收。
3. 在 Ubuntu 24.04 容器中构建 `.deb`，在 Fedora 容器中构建 `.rpm`。
4. 三种包全部成功后，上传 release 资产并生成 `SHA256SUMS`。
5. 将 Arch 构建阶段生成的 `PKGBUILD`、安装脚本和 `SRCINFO` 推送到 AUR。

首次启用前，在仓库 Actions secrets 中添加 `AUR_SSH_PRIVATE_KEY`。该私钥必须
是已登记到 AUR 账户的 SSH key；工作流使用仓库内固定的
`packaging/ci/aur_known_hosts` 校验 AUR 主机。没有该 secret 时 AUR 任务会
明确失败，不会静默跳过。

工作流不会修改版本号。发布新版本前，先同步更新所有版本文件、更新日志和文档，
提交后再推送 tag；否则版本校验会阻止构建和发布。

## 安装后

首次安装后注销并重新登录，或启动当前会话的用户服务：

```sh
systemctl --user start panasonic-posture-manager-session.service
```

检查系统服务和会话状态：

```sh
systemctl status panasonic-posture-manager.service
systemctl --user status panasonic-posture-manager-session.service
gdbus call --system --dest work.mmoe.panasonic.Posture \
  --object-path /work/mmoe/panasonic/Posture \
  --method work.mmoe.panasonic.Posture.GetState
```

`session_connected` 应为 `true`。若服务正常但窗口仍显示离线，重新打开窗口。

## 故障排查

- **后台服务未连接**：重新打开程序；仍无效时注销并重新登录，再检查上面的
  systemd 服务状态。
- **一直等待桌面就绪**：仅支持 GNOME Wayland 和 Plasma 6 Wayland。GNOME 需要
  `mutter`、`gnome-shell` 和 `gnome-settings-daemon`；Plasma 需要
  `kscreenctl` 或 `kscreen-doctor`。
- **屏幕方向不变**：点击“重试”；使用外接屏时关闭镜像并检查布局。Plasma 可用
  `kscreenctl list-outputs --json` 或 `kscreen-doctor -j` 检查内屏。
- **提示没有出现**：确认对应开关已开启，并保持姿态稳定；提示只在从笔记本
  姿态进入目标姿态时出现。
- **内置键盘或触控板未恢复**：在设置中选择“自动”；仍无效时注销并重新登录。
- **平板开关未识别**：完整折叠、展开一次后再试。

## 构建与验收

```sh
make clean
make WERROR=1 check
python3 -m build --wheel --no-isolation
(cd packaging/aur && makepkg --cleanbuild)
make check-package \
  PACKAGE=packaging/aur/panasonic-posture-manager-2.1.0-1-x86_64.pkg.tar.zst
namcap packaging/aur/panasonic-posture-manager-*.pkg.tar.zst
```

在对应发行版工具链中也可以直接构建另外两种包：

```sh
make package-deb   # Ubuntu 24.04+，需要 dpkg-dev/python3-build
make package-rpm   # Fedora，需要 rpm-build/python3-devel
```

输出位于 `dist/packages/`。RPM 构建以已提交的 `HEAD` 制作用于 `rpmbuild` 的
源码归档，因此工作区必须干净。

修改 `PKGBUILD` 后，用 `makepkg --printsrcinfo > .SRCINFO` 同步 AUR 元数据。

`make WERROR=1 check` 覆盖 C、Python、会话总线和翻译检查。`make check-package`
核对版本、文件布局、systemd/D-Bus/polkit 配置，并在隔离环境中验证安装、
升级和卸载；它不会执行安装钩子或访问真实硬件。仅做静态检查时加
`PACKAGE_NO_INSTALL=1`，验证升级时加 `PREVIOUS_PACKAGE=旧包路径`。

`PKGBUILD` 固定使用当前版本 tag，构建前清除预构建产物。创建 `v2.1.0` tag 后，
必须从固定 tag 源重跑 clean-rootfs 构建和验收。

## 安装布局

| 内容 | 位置 |
| --- | --- |
| 程序入口 | `/usr/bin/` |
| Relay | `/usr/lib/panasonic-posture-manager/relay` |
| UI、资源与 profile | `/usr/share/panasonic-posture-manager/` |
| systemd、D-Bus、polkit、desktop | `/usr/lib/systemd/`、`/usr/share/dbus-1/`、`/usr/share/polkit-1/`、`/usr/share/applications/` |
| 许可证 | `/usr/share/licenses/panasonic-posture-manager/` |

## 卸载与清理

```sh
sudo pacman -Rns panasonic-posture-manager
```

pacman 不会删除设置、校准和恢复日志。需要彻底清理时：

```sh
sudo rm -rf -- /var/lib/panasonic-posture-manager
state_root="${XDG_STATE_HOME:-${HOME:?HOME must be set}/.local/state}"
rm -rf -- "$state_root/panasonic-posture-manager"
```

## 发布清单

### 2.1.0

- [x] 2.1.0 版本、许可证和打包元数据一致。
- [x] 本地 `.deb`、`.rpm` 构建和文件布局检查通过。
- [ ] GitHub Actions 已发布 2.1.0 三种包和 `SHA256SUMS`。
- [ ] 2.1.0 AUR 元数据已推送。

### 历史版本

- [x] 版本、许可证和打包元数据一致。
- [x] `v2.0.0` tag 已通过本地与 clean-rootfs 构建、包验收和 `namcap`。
- [x] 在隔离环境验证全新安装、1.0.0 → 2.0.0 升级和卸载。
- [x] 发布 [GitHub Release](https://github.com/adogecheems/panasonic-posture-manager/releases/tag/v2.0.0)
  和 [AUR 包](https://aur.archlinux.org/packages/panasonic-posture-manager)。
- [ ] 在真实系统验证 D-Bus 激活、会话启动和服务状态。
- [ ] 验证休眠/唤醒、异常恢复、锁屏通知和外接屏布局。
- [ ] 验证 GNOME Wayland 和 Plasma 6 Wayland。
- [ ] 完成 CFQV8 物理验收和 QV9/QV1 同族回归。
