# 更新日志

正式版本遵循[语义化版本](https://semver.org/lang/zh-CN/)，日期格式为
`YYYY-MM-DD`。

## [未发布]

### 打包

- 新增 Debian/Ubuntu 的 `.deb` 和 Fedora 的 `.rpm` 预构建包。
- 新增 tag 驱动的 GitHub Actions 发布工作流：构建 Arch、Deb、RPM 三种包，
  上传 GitHub Release，并将校验后的元数据推送到 AUR；从下一个包含该工作流的
  发布 tag 起生效。
- 系统服务和用户会话单元改用各发行版都提供的 `/usr/bin/python3`。

## [2.0.0] - 2026-11-03

仅支持 Wayland 会话，并修正模式提示与校准按钮文案。

### 不兼容变更

- 取消 X11 会话支持，仅保留 GNOME Wayland 和 KDE Plasma 6 Wayland。

### 修复

- 帐篷提示只依据传感器识别的来源：支架、硬件平板来源直接进入帐篷，已确认
  帐篷后短暂离开再返回也不重复询问；服务启动时已处于帐篷或支架会直接采用
  识别姿态。手动选择不再覆盖来源，当前姿态始终显示识别结果。
- 修正“校准”和“恢复默认”按钮的忙碌文案。

### 文档与打包

- 统一版本、依赖和发布检查信息。
- 发布 GitHub Release 和 AUR 包 `panasonic-posture-manager`。

### 已知限制

- QV8 最终物理验收、QV9/QV1 回归和 Plasma 6 Wayland 真机验证尚未完成。
  真机验收状态详见[硬件验证记录](docs/hardware-validation.md)。

## [1.0.0] - 2026-11-03

首个公开发行版。

### 新增

- 支持 Panasonic CFQV8、CFQV9 和 CFQV1。
- 提供自动、笔记本、帐篷、支架和平板五种模式，以及帐篷/支架确认提示。
- 支持 GNOME Wayland、Plasma 6 Wayland、传感器校准和中文界面。
- 提供 AUR 打包、安装集成和安装包验收工具。

### 稳定性

- 改进传感器源切换、帐篷/支架退出判定和故障诊断。

### 已知限制

- QV8 最终物理验收、QV9/QV1 回归和部分桌面后端仍待逐机验证。
