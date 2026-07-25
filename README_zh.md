# openCodexMicro

**把 Ulanzi D200 变成 Codex Micro，再加上足以完整操控 Codex Desktop 的常用按键。**

[English](README.md)

![Ulanzi D200 上的 openCodexMicro](docs/images/codex-keyboard-hero.png)

openCodexMicro 为 Codex Desktop 提供一块专用硬件控制面板：直接查看最近任务的运行状态，一键切换任务，并把最高频的 Codex 操作放到手边。

## 实现的功能

| 功能 | 行为 |
| --- | --- |
| 五个实时任务键 | 跟随 Codex 的 Most Recent 顺序，显示空闲、运行、完成、等待输入/审批或错误 |
| 一键切换任务 | 打开实体按键当前显示的准确任务 |
| Codex 常用控制 | Fast、Pin、New、Fork、Steer、Mic 和 Submit |
| Usage 显示 | 展示剩余周额度并自动刷新 |
| 时钟与 Focus | 保留 D200 固件时钟，按下可聚焦 Codex |
| 原生集成 | 使用 Codex app-server、rollout 事件和 `codex://` deep link，不开启浏览器调试端口 |
| 响应式显示 | 只传输发生变化的键，并始终优先处理实体输入 |
| 热插拔恢复 | D200 重新连接后恢复最后一次成功画面 |

![openCodexMicro 平面键位示意图](docs/images/open-codex-micro-layout.png)

![桌面环境中的 openCodexMicro](docs/images/codex-keyboard-workspace.png)

这是非官方项目，目前支持 **macOS、Codex Desktop 和 Ulanzi D200**。
整个项目由 Codex 纯 vibe coding 完成。

## 安装

要求：

- 已安装 Codex Desktop 的 macOS
- 通过 USB 连接的 Ulanzi D200
- Node.js 20+
- 支持 `venv` 的 Python 3.11+

克隆仓库后运行：

```bash
npm run setup
```

安装器会创建独立 Python 环境、安装 `hidapi` 和 Pillow、注册用户级 LaunchAgent，并启动 openCodexMicro。已有 CodexKeyboard 安装会自动迁移；旧的自定义主题会先复制，再清理旧运行目录。

第一次模拟快捷键时，macOS 可能请求辅助功能权限。请允许安装后的 Python 进程控制 `System Events`。

权限、日志、诊断、更新、迁移和卸载方法见 [安装与运行](docs/setup-and-operations.md)。

## 配置

openCodexMicro 直接跟随 Codex Desktop 的快捷键，不维护第二套快捷键系统。

快捷键覆盖：

```text
~/.codex/keybindings.json
```

Submit 和 Steer 行为：

```text
~/.codex/config.toml
```

主题覆盖：

```text
~/Library/Application Support/openCodexMicro/icon-theme.json
```

快捷键和 Desktop 设置会在按键时读取，通常无需重启驱动；主题变化需要刷新设备画面。

支持的 command、快捷键格式、Submit/Steer 行为、实体键位调整和主题选项见 [配置详解](docs/configuration.md)。

仓库还包含三个可复用的 Codex skill：

| Skill | 用途 |
| --- | --- |
| [`install-open-codex-micro`](skills/install-open-codex-micro/SKILL.md) | 安装或更新，确认是否修改快捷键，提示权限，并选择是否启动 daemon |
| [`customize-open-codex-micro-icons`](skills/customize-open-codex-micro-icons/SKILL.md) | 替换或生成任务键和功能键图标 |
| [`remap-open-codex-micro-keys`](skills/remap-open-codex-micro-keys/SKILL.md) | 移动现有按键或实现新的按键动作 |

## 文档

- [配置详解](docs/configuration.md)
- [安装与运行](docs/setup-and-operations.md)
- [架构说明](docs/architecture.md)
- [D200 协议说明](docs/d200-standalone.md)
- [工程约束](docs/errors.md)

## License

项目采用 [MIT License](LICENSE)。第三方归属见
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
