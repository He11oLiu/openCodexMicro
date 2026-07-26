# Native Codex Micro USB discovery probe

This disposable POC asks macOS to expose a vendor-defined virtual HID with the
identity expected by the installed ChatGPT app:

- Vendor ID `0x303a`
- Product ID `0x8360`
- Usage page `0xff00`
- 64-byte Work Louder reports (report ID `6` plus 63 payload bytes)

It does not inject keyboard or mouse input. If macOS permits creation, the probe
logs output reports written by ChatGPT so the native device handshake can be
observed.

## What the installed app actually matches

The installed ChatGPT app's `CodexMicroService` first asks its native
`hid-topology-watcher` for HID interfaces, then filters on:

| Field | Accepted value |
| --- | --- |
| Vendor ID | `0x303a` |
| Product ID | `0x8360` (Codex Micro), `0x8297` or `0x8298` (Creator Micro V2) |
| Usage page | `0xff00` |

It then opens the interface through `@worklouder/device-kit-oai`; this is not a
generic keyboard path. The transport uses 64-byte reports:

```text
byte 0      report ID 6
byte 1      channel (1 debug, 2 JSON-RPC)
byte 2      UTF-8 chunk length, max 61
byte 3..63  payload
```

Agent key notifications use method `v.oai.hid` and key names `AG00` through
`AG05`. ChatGPT writes status lighting using `v.oai.rgbcfg` and
`v.oai.thstatus`, and reads `device.status`.

## Local result

Current macOS SDKs require the restricted
`com.apple.developer.hid.virtual.device` entitlement. Merely putting that key in
an ad-hoc signature does not grant it.

The probe was tested on this Mac:

- unsigned: creation returned `NULL`;
- ad-hoc signed with the entitlement: AMFI terminated the executable
  (`SIGKILL`, exit 137).

This rules out a zero-install, ordinary user-process virtual HID. The viable
native implementations are:

1. a properly signed virtual-HID DriverKit/system-extension product; or
2. a small physical USB-device proxy (for example RP2040/ESP32-S3 with
   TinyUSB), which exposes the Work Louder interface to macOS while the existing
   daemon bridges D200 key/display events.

The second option is the shortest reliable prototype because ChatGPT sees a
real HID and needs no launch flags, accessibility clicks, or renderer injection.
The D200's existing firmware exposes a different fixed interface
(`2207:0019`, usage `0x000c`), so changing only the host daemon cannot make that
physical interface match.

## Reproduce the macOS boundary

```sh
clang -fblocks -framework CoreFoundation -framework IOKit \
  virtual_codex_micro.c -o virtual_codex_micro
codesign --force --sign - \
  --entitlements virtual-hid.entitlements.plist virtual_codex_micro
./virtual_codex_micro
```
