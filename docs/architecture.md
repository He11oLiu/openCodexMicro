# Architecture

openCodexMicro 默认直接消费 Codex renderer 已聚合的 Micro store；native
app-server/rollout 只在 Bridge 不可用时提供 local-only fallback。

## Codex state

```text
Codex renderer Micro store
  ├─ 本机与多个 SSH host 的任务
  ├─ Most Recent / status / selected
  └─ thread → project / host assignment
                    │
             CDP cached snapshot (500ms)
                    │
             CodexStateAdapter ──────────────> D200 profile queue ──> USB HID
                    │
          Bridge 不可用（连续五秒）
                    v
       NativeCodex(enable_remote=False)
          └─ 本机 app-server + rollout/kqueue

任务键 ──> Bridge HTTP ──> Micro event bus（含临时 client thread）
Fast/Fork/Submit ────────> Micro action down/up
Pin/New ──> Bridge HTTP ──> renderer 对应语义控件
Steer ───> Bridge HTTP ──> renderer 真实 Steer action
Mic ─────> Bridge HTTP ──> Micro ACT10 down/up
```

`CodexStateAdapter` 每 250ms 读取 sidecar 的内存缓存；sidecar 每 500ms 通过
CDP 更新一次。`/state` 本身不执行 CDP，因此 D200 polling 不会放大 renderer
负载。首次 snapshot 扫描 JS assets 与 React Fiber，定位 Micro bus、store node、
resolver、context map 和 rate-limit query clients，并保存到 renderer 的
`Symbol.for("codex-keyboard-micro-snapshot-source")`。后续 snapshot 直接读这些
引用；root 或 store 引用失效时才清除缓存并重新发现。本机实测热读取约
0.2–1.2ms。

Micro store 自己负责本机/远端统一排序、状态与 host assignment。D200 只取前五
个 slot，不再建立第二套 SSH/SQLite 聚合。slot 的 `client-new-thread:<uuid>`
临时键会原样经过 Bridge；Codex 晋升为正式 conversation UUID 后，下一个 snapshot
原子替换 D200 映射。

Bridge 连续不可用五秒后，adapter 才创建 `NativeCodex(enable_remote=False)`；
短暂的 sidecar 请求失败或 renderer 重载继续保留最后一个 Bridge framebuffer，
不会闪切 local-only。
fallback 使用本机 app-server inventory、rollout/kqueue lifecycle 与本机 deep
link，不创建 SSH 进程。Bridge 恢复后立即关闭 fallback。旧的远端 monitor 仍可
用 `--native-state` 显式诊断；远端 SQLite schema 不兼容时保留上一 inventory，
记录 host error，并扫描 rollout 降级运行。

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
Mic 通过 Micro 的 `ACT10` down/up 事件保留按下/抬起语义。Steer 聚焦当前可见
composer 并直接点击 renderer 的真实 Steer action；这会复用 Codex 内部的
本机/远端 host 路由，且不会把失败误退化成普通发送。

Bridge 模式下 Fast、Fork、Submit 同样使用 Micro action 的 down/up 事件。
Pin 和 New 没有独立的 Micro 固定槽位，因此 Bridge 调用当前 task 的 Pin/Unpin
按钮和 renderer 的 New chat 按钮。HTTP 超时或 503 后不使用 AppleScript 重放：
renderer 可能已经执行动作但响应丢失，重放会造成双重 New/Fork/Submit 或反向
切换。只有 adapter 明确进入 local-only source 后才使用配置快捷键。

普通方式启动 Codex 时没有 9222 endpoint。默认 D200 只显示本机 fallback 任务，
并使用 `codex://threads/<id>`；远端任务必须通过 Bridge 的 renderer 路由。

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
9. 每个新 HID 会话都清空整帧及所有逐键摘要，只通过正常的 input-priority
    事务上传一次当前最新完整 profile；不先同步重放旧缓存，也不后台重建第二份
    reconnect ZIP。

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
