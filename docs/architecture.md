# Architecture

openCodexMicro 的状态链路始终是事件驱动的 native 集成；任务导航额外提供可选的
loopback-only Codex Micro bridge。

## Codex state

```text
本机 app-server ── inventory / usage ───────────┐
本机 rollout ── kqueue 增量 ───────────────────┤
Codex 全局状态 ── kqueue ── host/thread 映射 ─┤
远端 rollout/SQLite ── SSH + inotify 增量 ────┼─> 统一 Most Recent ──> D200
                                                │
任务键 ──> bridge sidecar ── CDP ── Micro bus ─┤
      └─> 本机 deep link / SSH Dock Recent 回退 ┘
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

`NativeCodex` 用 app-server 获取本机初始 inventory 和 Usage，用 macOS kqueue 监听本机 rollout 与 Codex 全局状态。全局状态发现 Codex App 管理的 SSH host、project 和 thread assignment；每台活动主机只维持一条 SSH 长连接。远端 helper 用 SQLite 做连接时 inventory/事件对账，用 Linux inotify 把 rollout 的字节增量推回本机，不做 SSH 目录轮询或每秒 `thread/list`。

本机和远端 rollout 复用同一个 lifecycle 解析器。新用户 rollout 从 `session_meta` 直接识别，过滤 subagent，并只解析文件尾部的当前生命周期。远端断线时保留最后状态并标记 `hostOnline=false`，重连后重新发送 inventory 和文件尾增量，不把掉线误报成任务失败。

recent/结构事件先进入 staged logical framebuffer，在 50ms 静默窗口内合并 order 与 status，然后只提交一个 revision。后台任务重新启动也从已监听 rollout 直接晋升；`thread/list` 退到事件触发的补齐和对账路径。所有 host 按最近活动时间统一排序，对齐 Codex Micro 默认 Most Recent 模式。Usage 每十分钟有独立的硬期限；rate-limit 通知只能提前刷新。

五个任务键保持独立的全局 Most Recent 顺序；统一排序发生在截断五槽之前，
所以五个更近的本机任务会自然挤掉更旧的 SSH 任务，反之亦然。

`Codex Bridge.app` 用三个参数启动真实 Codex：

```text
--remote-debugging-address=127.0.0.1
--remote-debugging-port=9222
--remote-allow-origins=http://127.0.0.1:9222
```

bridge sidecar 只监听 `127.0.0.1:17373`，持久连接 Codex 主 renderer。它启用
Codex Micro gate，找到内部 event bus，先派发 `connected` 设备状态，再按
D200 的明确 thread ID 派发官方 `codex-micro-hid-event`。Codex 自己解析保存的
thread → host/project assignment，因此同一接口可切换本机和 SSH 任务。该路径
不移动鼠标、不打开菜单，也不受 pinned task 或 `Command+1…9` 数量限制。

普通方式启动 Codex 时没有 9222 endpoint。任务键会在当前 daemon 生命周期内
弹一次说明；本机任务使用 `codex://threads/<id>`，SSH 任务使用 Dock **Recent**
菜单中精确且唯一的标题回调。SSH 回退依赖 Accessibility，并可能短暂显示 Dock
菜单；标题缺失或重名时必须拒绝，不能猜测坐标。

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
- 构建并安装 loopback bridge sidecar LaunchAgent；
- 生成、签名并安装 `~/Applications/Codex Bridge.app`；
- 在 Mic command 缺失时补充 `Command+Alt+M`，保留已有用户覆盖。

卸载器删除 openCodexMicro 以及已识别的旧 CodexKeyboard LaunchAgent 和运行目录。
