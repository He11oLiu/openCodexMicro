# D200 protocol notes

这些结论来自本项目的设备实验、`rs-ulanzi-d-200-linux`，以及
`companion-surface-d200` 的公开 USBPcap 实现。它们不是厂商 SDK 保证。

## HID endpoint

- VID: `0x2207`
- PID: `0x0019`
- Usage page: `0x000c`
- Report size: 1024 bytes
- hidapi 写入时在 1024 字节 report 前加 report ID `0`

## Commands

| Command | Purpose | Payload |
| --- | --- | --- |
| `0x0001` | 开始 profile 文件传输 | 首包包含 8 字节头和最多 1016 字节 ZIP |
| `0x000d` | 局部更新按键 | 只含变化键 manifest/PNG 的 ZIP |
| continuation | profile 后续内容 | 每包 1024 字节，不重复命令头 |
| `0x0006` | 固件模式/时钟保活 | `1|0|0|HH:MM:SS|0` |
| `0x000a` | 亮度 | ASCII 百分比 |
| `0x0101`, `0x0102` | 输入事件 | 设备上报按键索引与按下状态 |

包头：

```text
0..1  "||"
2..3  command, big endian
4..7  payload/文件总长度, little endian
8..   payload
```

## Profile

显示层是 ZIP：

```text
manifest.json
Images/<key>_<uuid>.png
sentinel.txt
dummy.txt
```

完整 profile 的 `manifest.json` 含 14 个键位。键 13 的 `Icon` 留空，由固件显示宽时钟。
状态或 Usage 改变时，openCodexMicro 使用 `0x000d`，manifest 只包含真正变化的键；固定键、未变化任务键和 Usage 不会重复传输。
若只发生 recent 映射变化而像素完全相同，则只提交新的按键映射，不写 HID。

局部更新仍是一个 ZIP 文件事务，已经开始后不能中断。传输期间的新状态只保留最终目标；
旧事务结束后基于最新已应用键摘要重新生成下一次局部更新。

部分固件会拒绝在特定 1024 字节边界出现 `0x00` 或 `0x7c` 的 ZIP。本项目通过调整 `dummy.txt` 重新生成归档，直到边界满足要求。

profile 文件事务具有时序要求：

- 开始后不能中断或与另一个 profile 交错；
- 包间隔过长会让固件放弃事务；
- macOS 同步 HID 写可能阻塞，因此每包前必须优先处理按键；
- USB 重连后先恢复最后成功的缓存 profile，再同步实时状态。

运行以下命令可在不打开 USB 的情况下验证归档和分包：

```bash
python3 standalone/d200.py --self-test
```

## Update limits

`0x000d` 减少的是 ZIP 中的键和 PNG，不是把 PNG 塞进一个 HID report。单个 196×196 PNG
通常仍需要多个 1024-byte report，且局部事务也不能与心跳或另一文件事务交错。
