# Claw 插件（OpenClaw 网关通信）

通过 OpenClaw Gateway WebSocket 把微信会话直通到网关，支持唤醒词对话、管理员 slash 命令、图片/语音/视频/文件/引用上下文和事件转发。

## 功能概览

- 自动完成 `connect.challenge -> connect -> hello-ok` 握手并保持 WS 连接
- 对话统一直通网关：唤醒词、私聊免唤醒词、群聊 `@机器人`
- 管理员 slash 直通：私聊可直接发送 `/命令`；群聊需 `@机器人 + 唤醒词 + /命令`，真实网关 RPC method 走 RPC，`/new`、`/reset`、`/model`、`/think` 等 OpenClaw 原生命令走 `agent`
- `龙虾 帮助` / `龙虾 命令` 本地返回常用命令和功能说明
- 支持图片、语音、视频、文件、引用消息、链接文章上下文附带
- 图片、语音、视频、文件消息统一优先走网关原生 WS `attachments`；若微信侧已把媒体下载到内存，插件会直接转成 base64 附件；若只有本地文件，插件会读取后转成附件发给网关，不再把 `MEDIA:<path>` 或 `http://...` 发给网关
- 支持 `Claw.EventForward` 事件主动转发；仅在显式配置 `to-wxids` 时转发原始网关事件体

## 核心配置

编辑 `plugins/Claw/config.toml`：

- `ws-url`：网关 WS 地址
- `gateway-token` / `gateway-password`：共享鉴权，二选一
- `role` / `scopes`：角色和权限；需要 `/model` 等管理命令时保留 `operator.admin`
- `caps`：默认包含 `tool-events`，用于让网关把实时工具事件推回给 `Claw`
- `device-auth-enable`：建议开启，避免远端网关清空 scopes
- `trigger-words` / `trigger-match-mode`：唤醒词及匹配方式
- `private-auto-forward-enable`：私聊免唤醒词直通
- `at-auto-forward-enable`：群聊 `@机器人` 直通
- `slash-command-forward-enable`：管理员 slash 直通
- `default-agent-id`：固定使用指定 agent；留空时优先走网关默认 agent
- `gateway-channel` / `gateway-account-id`：声明网关侧微信来源渠道；`sessionKey` 可保留 `wx-869`，插件握手成功后会通过 `channels.status` 读取网关当前实际注册的渠道列表，只有识别到该渠道时才附带 `agent.channel/replyChannel`
- `trigger-use-session-key` / `trigger-session-prefix`：按聊天对象生成稳定 `sessionKey`；当前会输出 OpenClaw 可识别的 `agent:<agent>:<channel>:direct|group:<peer>`
- `stream-reply-enable`：是否回写流式增量；默认关闭，仅发送终态完整回复
- `image-forward-mode` / `image-host-path-prefix` / `image-public-base-url`：图片路径或公网链接传递方式
- `pending-run-ttl-seconds` / `pending-run-watchdog-*`：pending run 清理和看门狗参数
- `Claw.EventForward.to-wxids`：固定事件转发目标；留空时不向微信转发原始网关事件体

## 使用方式

- `龙虾 你好`：按唤醒词发起对话
- `龙虾 帮助` / `龙虾 命令`：查看本地帮助
- 私聊消息：启用 `private-auto-forward-enable` 后直接转发到网关
- 群聊 `@机器人`：启用 `at-auto-forward-enable` 后直接转发到网关
- 私聊 `/new`：为当前聊天对象开启新会话
- 私聊 `/reset`：重置当前会话
- 私聊 `/model`：查看或切换模型
- 私聊 `/think <level>`：设置思考强度
- 私聊 `/verbose <level>`：设置输出详细程度
- 私聊 `/reasoning <level>`：设置推理强度
- 私聊 `/compact`：压缩当前会话上下文
- 私聊 `/status`：查看当前状态
- 私聊 `/help`：查看 OpenClaw 内置帮助
- 私聊 `/stop`：停止当前回复
- 群聊管理员命令格式：`@机器人 龙虾 /new`

## 行为说明

- `agent` 请求始终透传 `to/groupId/accountId/sessionKey`；插件会在握手后调用 `channels.status` 缓存网关真实支持的渠道列表，只有命中该列表时才附带 `channel/replyChannel`，避免 `unknown channel` 直接被拒，同时允许自定义 `wechat` 渠道把渠道作用域工具正确挂进 agent 运行上下文
- `sessionKey` 使用 OpenClaw 规范形态：私聊 `agent:<agent>:wx-869:direct:<wxid>`，群聊 `agent:<agent>:wx-869:group:<chatroom id>`
- 默认只在终态回写完整文本，不回写流式 `delta`
- 握手默认声明 `tool-events`，确保网关侧工具调用事件不会因为 `caps: []` 被静默过滤
- 同一会话存在 pending run 时会跳过新的重复触发，避免队列堆积和多次回写
- `accepted` 后会同时等待 WS 事件和 `agent.wait` 兜底，只回写一次终态；`chat` 完成事件如果暂时没有文本，会优先等待 `agent.wait/chat.history` 收敛，不会立刻回打网关重试
- 图片、语音、视频、文件、链接文章、引用消息会按当前会话上下文附带给网关；群聊通常需要触发词、`@机器人` 或引用触发
- 图片、语音、视频、文件消息都会优先发送网关原生 `attachments`，附件对象同时携带 `content` 和 `source: { type: "base64", media_type, data }` 两种 base64 表达，兼容 Web UI / dashboard 上传格式
- 文件/语音/视频在缺少现成本地路径时，会复用微信框架已下载好的内存 payload 落盘到 `files/claw-media/`，再从落盘文件读取并转成 WS 附件
- 群聊中的 slash 文本不会直接发往网关；只有管理员同时满足 `@机器人 + 唤醒词 + /命令` 才会执行
- 插件不会下载或回传网关附件；仅对微信侧已经拿到的非图片入站媒体 payload 做本地落盘，供网关读取
- `401/未授权/权限不足/账号停用/配额不足` 一类硬错误不会自动重试，也不会再向网关发送 `[Gateway Retry Notice]`

## 说明

- slash 命令失败时不会退回成普通对话文本，避免把命令误发给模型
- 如需转发原始网关事件体，显式设置 `Claw.EventForward.to-wxids`
- 当前会话只接收正常对话回复，不再接收 `[ClawEvent] ...` 这类原始网关事件体
