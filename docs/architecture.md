# Architecture

openCodexMicro 只保留 native 集成路径，不开启 Chromium 调试端口，也不运行本地 HTTP bridge。

## Codex state

```text
Codex app-server ── inventory / usage ─┐
rollout JSONL ── kqueue events ────────┼─> NativeCodex snapshot
codex://threads/<id> <── task key ─────┘
                                      │
                                      v
                        openCodexMicro state
                                      │
                                      v
                              D200 profile queue
                                      │
                                      v
                                  USB HID
```

`NativeCodex` 用 app-server 获取初始 inventory 和 Usage，用 macOS kqueue 监听 rollout。新用户 rollout 从 `session_meta` 直接识别，过滤 subagent，并只解析文件尾部的当前生命周期；因此新对话不需要等待 state DB 才能进入最左键。状态变化通过 revision/condition 唤醒驱动，不做 200ms 文件遍历或每秒 `thread/list`。

recent/结构事件先进入 staged logical framebuffer，在 50ms 静默窗口内合并 order 与 status，然后只提交一个 revision。后台任务重新启动也从已监听 rollout 本地晋升；`thread/list` 退到事件触发的补齐和对账路径。任务对齐 Codex Micro 默认 Most Recent 模式。Usage 每十分钟有独立的硬期限；rate-limit 通知只能提前刷新。任务按键使用 `codex://` deep link，桌面动作读取 Codex 自己的 keybindings 后通过 macOS 键盘事件触发。

## D200 input and output

按键读取永远优先于显示：

1. HID 读循环捕获按下/抬起并立即放入动作队列。
2. 动作线程向 Codex 分发，不等待渲染或 profile。
3. 状态线程只更新“最新目标”。
4. 渲染线程比较每个键的图片摘要，通过 `0x000d` 构建只含变化键的 sparse ZIP。
5. 连续变化覆盖旧目标，不排队重放中间状态。
6. HID 输出事务开始后不中断；每个包之前仍先处理按键。
7. ZIP 传输完成后，用固件激活命令一次提交像素、图片摘要和按键到 thread 的映射。
8. 事务期间若目标已经变化，当前 framebuffer 激活后直接构建最终目标，不重放中间版本。
9. 局部画面可见后，低优先级线程才重建完整的 USB 重连缓存。

固定功能键从磁盘预渲染素材读取并缓存。只有变化的任务键或 Usage 会进入线上的 sparse profile；未变化任务键与固定键不重新传输。D200 固件时钟不属于图片 profile，驱动每 30 秒发送 mode 1 保活。

## Installation boundary

安装器 `scripts/install.mjs`：

- 复制 Python 驱动、主题和素材；
- 创建隔离 venv 并安装 `hidapi`、Pillow；
- 要求 Python 3.11 或更高版本；
- 迁移旧 CodexKeyboard 主题并清理旧服务；
- 写入 openCodexMicro 用户级 LaunchAgent。

卸载器删除 openCodexMicro 以及已识别的旧 CodexKeyboard LaunchAgent 和运行目录。
