# Engineering notes

这里记录已经踩过、发布后仍必须保持的约束。

## Input latency

- 按键热路径只能读取 HID、记录时间并入动作队列。
- 不能在热路径等待 inventory、渲染、HTTP 完成或 HID 输出。
- 固定按钮预渲染并缓存；状态变化只保留最新目标。
- 局部 profile 只包含变化键；固定键、未变化任务键和 Usage 不得因单个状态变化重复传输。
- profile 事务一旦开始不能中断，但每个包之前仍要先读取按键。
- `Condition.wait_for(..., timeout=...)` 超时返回 `False`；必须继续等待，不能把超时当成状态变化，否则会每 500ms 重复渲染。
- 不上传按下动画。它会增加 HID 事务、制造约一秒以上的交互延迟。

日志超过 100ms 时会分别记录按键捕获、队列等待和动作完成时间。显示链路应另行记录状态检测与 profile 可见时间，不能把两者混成“事件驱动”。

## HID output

- `IOHIDDeviceSetReport` 可能异常变慢或失败；写入有有限重试和慢写日志。
- `0x000d` 是局部 ZIP 事务，不是单个 HID report；包间仍要短且稳定。
- USB 拔插是正常状态。守护进程应快速重新发现设备，并恢复最后成功 profile。
- USB 断开后磁盘摘要不能证明设备仍保有完整 framebuffer。重连必须清空整帧与
  逐键摘要并全量刷新；只有未观察到断线的普通 daemon 重启可以复用摘要。
- 30 秒固件时钟保活必须保留，但要避开按键和正在进行的 profile 包。

## State consistency

- Bridge 模式必须以 renderer Micro store 为唯一权威状态源；D200 不得同时把
  app-server、rollout、远端 SQLite 再聚合成另一套 Most Recent。
- Bridge sidecar 每约 500ms 刷新缓存，`/state` 只能读取缓存，不能为每个请求
  触发 CDP。首次发现后必须缓存 Micro bus、store node、resolver、context 和
  rate-limit query client；引用失效时才重新扫描资源与 React Fiber。
- `client-new-thread:<uuid>` 是合法临时键，必须作为一个 URL 编码 path segment
  贯穿 Python、HTTP 和 CDP；只能额外接受正式 UUID，禁止放宽为任意字符串。
- Bridge 不可用时默认只能启动 `NativeCodex(enable_remote=False)`。远端 SSH/SQLite
  monitor 仅允许显式诊断启用；旧 SQLite schema 失败时保留 inventory 并降级为
  rollout-only，不能清空远端列表。
- rollout 使用 kqueue 增量读取；禁止恢复 200ms 全量遍历。
- watcher 重配必须先注册新增 fd、再关闭废弃 fd，并在完成后 catch up；整批关闭两千个 watcher 不但有漏事件窗口，也会拖慢新会话。
- fork rollout 可能瞬间复制几十 MB 历史。新会话从首行 `session_meta` 识别身份，只解析文件尾部的当前生命周期；禁止为了显示一个键从头解析完整 fork。
- 只接收 `thread_source=user` 的本地 rollout；subagent 不能占据用户的 Most Recent 键位。
- 五个任务对齐 Codex Micro 默认 Most Recent 顺序。新建或重新活动的用户 rollout 本地直接晋升，`thread/list` 只负责补齐和对账，不能成为显示热路径。
- Codex 管理的每台活动 SSH host 只维持一条长连接；远端 rollout 必须通过 inotify 增量推送，禁止定时 SSH 扫目录。
- SSH 掉线保留最后 lifecycle，只设置 `hostOnline=false`；重连后 inventory 和 rollout 尾部重新对账，禁止把网络故障映射为任务 error。
- Bridge 导航必须使用 D200 当前槽位的明确 thread ID，通过 Codex Micro
  event bus 交给 Codex 自己解析 host/project assignment；禁止退回 pinned
  task 会占位的 `Command+1…9`。
- Steer 必须调用 renderer 暴露的真实 Steer action。找不到 action 时保持 no-op
  并记录失败；禁止回退到 Enter 组合键，因为它可能发送或排队。
- Mic 必须保留 down/up 两个物理阶段并映射到 Micro `ACT10`；不能只在按下时
  模拟一次快捷键。
- Bridge 已经接管动作时，HTTP 超时或 503 后不能再用 AppleScript 重放。
  renderer 可能已经执行但响应丢失，重放会造成双重 New/Fork/Submit，或把
  Fast、Pin、Mic 这类 toggle 立即切回原状态。只有 source 明确切换到
  local-only 后才允许快捷键路径。
- 长生命周期动作 dispatcher 必须逐动作捕获异常。Bridge 503、日志输出失败或
  单个 action bug 都不能让后续 Send、Steer、Mic 停止消费。
- Bridge 端口只能绑定 `127.0.0.1`。sidecar 不得监听 LAN，也不得把 CDP
  WebSocket 暴露给非本机来源。
- 非 Bridge 模式必须明确标记为 local-only；禁止默认建立远端 SSH monitor。
- inventory/recent 事件先写入 staged logical frame，经过短静默窗口只发布最终 revision；禁止首查和补查各自启动一次 HID 事务。
- profile 传完不等于画面已经提交。像素摘要、五键 thread 映射和缓存必须在固件激活命令成功后原子切换。
- 独立 app-server 不会自动订阅 Desktop 已加载线程。approval、input、error 等状态不能只依赖它的线程通知。
- Usage 每十分钟主动刷新是硬期限，持续文件事件不能饿死它；通知只用于提前刷新。

## Rendering

- 发布主题只包含并使用 `standalone/assets/generated/runtime/` 中实际引用的 196×196 PNG；未经处理的生成素材和未使用候选图不进入公开发布快照。
- 任务键使用 128 色，功能键使用 96 色。继续降低色数会明显破坏半透明边缘和渐变。
- 用户主题只做覆盖。缺失的覆盖回落到默认主题；默认主题自身缺失素材应立即报错。
- 右下角不上传背景图片，保留固件宽时钟。

## Configuration

- 快捷键解析遇到未知修饰键必须整体拒绝并回退，不能静默删除未知部分后触发另一个组合。
- 安装器不得覆盖有效的用户主题。无效主题必须先备份，再创建空覆盖模板。
- 运行时要求 Python 3.11 或更高版本；安装器必须在变更活动服务前验证版本。
