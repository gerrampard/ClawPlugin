# Claw 插件（OpenClaw 网关通信）

该插件用于通过 **OpenClaw Gateway WebSocket** 与 `openclaw/openclaw` 通信，支持：

- 连接/断开网关（`connect.challenge` → `connect` → `hello-ok`）
- RPC `req/res` 调用（支持 `accepted → final` 等待最终态）
- 事件回推到当前会话（`watch on` 为本地回推路由）或转发到指定 wxid（EventForward）
- 触发词自动转发（如：`龙虾 你好`）并把网关回复回写到当前框架会话（默认仅发送终态完整回复，不回写流式增量，避免微信侧丢段）
- 默认不阻塞插件链路：Claw 触发后会后台调用网关并继续放行后续插件（可用 `propagate-to-other-plugins` 控制）

## 配置

编辑 `plugins/Claw/config.toml`：

- `enable`：是否启用插件（默认 `false`）
- `ws-url`：OpenClaw Gateway WS 地址（必填；为空时插件会自动禁用，避免误连）
- `gateway-token` / `gateway-password`：网关共享凭据（任选其一）
- `role` / `scopes`：按 operator 连接；`/model`、模型映射/切换等管理类 RPC 需包含 `operator.admin`
- `stream-reply-enable`：是否回写流式增量（默认 `false`，仅发送终态完整回复）
- `auto-trigger-enable`：开启触发词自动转发
- `trigger-words`：触发词列表，默认 `["龙虾"]`
- `trigger-match-mode`：触发词匹配模式（`prefix/contains/exact`，推荐 `prefix`）
- `trigger-use-session-key`：是否按会话生成固定 `sessionKey` 保持上下文
- `trigger-agent-id`：可选，指定 agent；不填则使用 OpenClaw 默认 agent
- `trigger-auto-default-agent`：当未指定 agentId 时自动使用网关 `health.defaultAgentId`（推荐开启，否则可能出现 “choose a session” 导致无回复）
- `device-auth-enable`：是否启用 device-auth（Ed25519 challenge 签名）。远端网关通常需要它才能保留 scopes（否则会出现 `missing scope: operator.write`）
- `pending-run-watchdog-enable` / `pending-run-watchdog-seconds`：pending run 看门狗。用于处理“agent accepted 但迟迟收不到任何事件/流式输出”的卡死场景：超过阈值会尝试用 `chat.history` 拉取终态文本兜底；仍无终态则继续等待至 `pending-run-ttl-seconds`，最终仅清理 pending-run（不回写“模型超时/重试失败”等提示到微信），避免污染会话。
- `image-forward-mode`：图片转发模式（`summary/base64/path`），建议网关可访问本地文件时使用 `path`
- `image-path-forward-enable`：是否在图片提示中附带 `[图片路径]`
- `image-host-path-prefix`：容器路径映射到宿主机路径前缀（如 `/home/sxkiss/桌面/bot/xbot`），用于把 `/app/files/...` 转成网关可读路径
- `image-public-base-url`：对外可访问媒体地址（如 `https://bot.example.com:9090`），配置后附带 `[图片链接]`
- `image-public-route-prefix`：媒体路由前缀，默认 `/media/files`，最终链接为 `<base><prefix>/<filename>`

## 触发词模式

启用后，普通文本不需要 `/claw` 命令前缀：

- 输入：`龙虾 你好`
- 插件行为：自动调用 OpenClaw `agent` 方法
- 回写：将 `result.payloads[*].text` 聚合后回复到原会话（群聊优先 @发送者）
- 群聊图片：仅在框架侧记录本地缓存路径，不会直接触发网关调用；当该会话随后发送文本（触发词/@/私聊直转）时，自动附带最近图片上下文。
- 引用消息：群聊仅当“@当前机器人”或“命中触发词”时才转发到网关；私聊引用消息会按 `private-auto-forward-enable` 的规则直转。引用的图片/视频/语音/文件会优先解析为框架公网链接（`http://l.sxkiss.top:9090/media/files/<filename>`，要求已落地到 `files/`）；引用的小程序/音乐/播客等卡片会以 `[分享卡片] xmlType=... url=...` 形式透传核心字段，便于网关侧识别。
- 去重：为避免框架侧同一 `MsgId` 被重复触发（常见于 @/引用消息预检查），Claw 默认开启短窗去重（`dedup-enable/dedup-window-seconds`），防止重复发起网关请求导致“二次请求/多次回复/回复不全”。
- pending-run 防堆积：同一会话（同 route_id）若已存在 pending run，Claw 会跳过本次触发（不再重复发起网关请求），以避免网关队列堆积与后续多次回写；同时继续放行后续插件（由 `propagate-to-other-plugins` 控制）。
- 模型错误重试：当识别到 `timeout/network_error/empty response/model error` 时，Claw 会自动重试一次；重试时仅向网关发送英文错误/重试提示（`retry-hint-to-gateway-enable=true`），**不会附带本次对话内容**，由网关基于同一 `sessionKey` 上下文自行重试；不会先在框架里发“正在重试”提示。
- 网关 RPC 超时幂等重试：触发词转发调用 `agent` 方法若出现 RPC 超时，会使用同一 `idempotencyKey` 自动重试一次，避免“网关已受理但响应慢”导致的重复 run/重复回复。
- 兜底终态：accepted 后除 WS 实时事件外，插件会后台调用 `agent.wait` 等待完成并优先使用其返回的终态文本；仅当仍取不到文本时才回退 `chat.history`（避免 history 体积裁剪导致“回复不全”）。
- 终态回写幂等：WS 终态与 `agent.wait` 兜底并发时，只会回写一次终态，避免“多次回复/丢段/看起来截断”。
- pending run 看门狗：若 accepted 后长时间没有任何事件/流式输出进展（可配置 `pending-run-watchdog-seconds`），仅标记为 stalled 并继续等待 `agent.wait` 兜底终态（不再主动轮询 `chat.history`，避免二次回写与截断）。
- 长文本发送：回复改为按 `max-reply-chars` 分片发送，不再单条硬截断成 `...(已截断)`。
- 会话与身份：`sessionKey` 以 `wxid/chatroom id` 命名（并附带短哈希防冲突），且每次转发都会在 prompt 头部附带 `chat_id/sender_wxid/sender_name`，方便网关识别消息来源。
- 网关媒体/附件：插件不再下载/回传网关返回的任何媒体/附件（包括 `MEDIA:` 指令、`attachments`、`mediaUrl(s)` 等），仅转发文本。

## 命令

以下以默认命令前缀 `/claw` 为例：

- `/claw status`：查看连接状态
- `/claw connect`：建立连接
- `/claw disconnect`：断开连接
- `/claw methods`：查看网关暴露的方法列表（来自 `hello-ok.features.methods`）
- `/claw events`：查看网关事件列表（来自 `hello-ok.features.events`）
- `/claw watch on|off|list|clear`：开启/关闭事件回推（仅影响本地回推路由）
- `/claw call <method> [json]`：调用任意方法
- `/claw callf <method> [json]`：调用并等待最终态（跳过 `status=accepted` 的 ack）
- `/claw send <to>::<message>`：调用 `send`
- `/claw chat <to>::<message>`：调用 `chat.send`（等待最终态）
- `/claw agent <prompt>`：调用 `agent`（等待最终态），可在配置中设置默认 `agentId/to/channel/accountId`
- `/claw last`：查看最近事件快照
- `/new`、`/reset`：管理员快速重置当前会话上下文（本地切换新 `sessionKey`，不会转成普通文本发给网关）

## 网关返回结构（按官方源码）

- `agent` 请求是 `accepted -> final` 双阶段：accepted 响应包含 `runId`，final 响应包含最终 `status` 与可选文本载荷。
- `chat` 事件常见状态：`delta`（流式增量）/`final`（完成）/`error`（失败）；插件默认只在终态发送完整文本，不回写 `delta`。
- `chat.history` 返回 `messages` 会做体积裁剪，超大消息会被替换为占位文本（不是完整原文）。
- 入参支持 `agent.attachments`（`type/mimeType/fileName/content(base64)`）。

## 备注

- 如需自动事件转发，可启用 `Claw.EventForward` 并设置 `to-wxids`。
- `image-forward-mode=base64` 时，插件也会同时附带 `[图片路径]`，便于网关走本地路径读取兜底。
- 图片消息不再向网关透传微信 CDN 参数（如 `aeskey/fileNo`），仅发送框架本地缓存路径或摘要信息。
- 若网关无法读取本地路径，建议配置 `image-public-base-url` 让插件输出可公网访问的 `[图片链接]`。
- 管理后台已提供公开媒体路由 `/media/files/{filename}`（仅 `files/` 目录单文件访问）。
- 插件不会修改主配置文件，仅依赖自身 `config.toml`。
- slash 命令执行失败（包括缺少 `operator.admin`）时，不会再回退成普通 `agent` 文本转发，避免“/命令被当对话内容”。
