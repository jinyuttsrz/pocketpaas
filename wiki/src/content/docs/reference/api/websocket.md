---
title: WebSocket
description: 容器日志实时流与 Web 终端的鉴权方式、消息协议与会话互斥规则。
---

## 接口清单

| 协议 | 路径 | 说明 |
|---|---|---|
| WS | `/ws/apps/{id}/logs?tail=N&service=<名称>` | 容器日志实时流，`tail` 回放历史，`service` 选择服务 |
| WS | `/ws/apps/{id}/shell?service=<名称>` | 容器 Web 终端，交互式 TTY |

`service` 省略时取公开服务；多容器 compose 可指定任意服务。`open` 消息会回传
`service` 与 `container`，便于前端确认当前连接目标。

## 鉴权

WebSocket 握手优先使用 Cookie，其次使用查询参数 `?token=<token>`。鉴权失败时连接被拒绝。

## 终端协议

- 客户端发送 `input`、`interrupt` 与 `resize` 三类消息，分别对应键盘输入、Ctrl+C 与终端尺寸变化。
- 服务端发送 `open`、`output`、`eof` 与 `error`；`output` 使用 base64 编码原始字节，避免 UTF-8 截断。
- 同一时间仅允许一个活跃终端会话；新会话建立时会自动断开旧会话。

## 相关页面

- 返回[接口总览](/reference/api/)
- 终端使用与排障：[项目文件与终端](/guide/files-terminal/)
