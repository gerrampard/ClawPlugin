<!-- AUTO-DOC: Update me when files in this folder change -->

# Claw

OpenClaw 网关通信插件：提供 WS 握手、RPC 调用、事件回推与会话命令控制。

## Files

| File | Role | Function |
|------|------|----------|
| __init__.py | Entry | 导出 `ClawPlugin` 供插件管理器加载 |
| main.py | Core | 实现 OpenClaw WS 客户端、命令解析、文本/AT/图片/引用消息/链接文章转发、RPC/事件回写；自动从 `health.defaultAgentId` 解析默认 agent；支持管理员“斜杠命令直通”和“命令列表速查”（`/new`、`/reset` 本地重置会话 sessionKey，其余 `/xxx` 按 RPC 调用，失败不回退 agent 文本转发）；触发词/AT/私聊直转发均在后台发起网关调用，默认不阻塞插件链路（可用 `propagate-to-other-plugins` 控制）；同会话存在 pending run 时跳过再次触发避免队列堆积；accepted 后默认依赖 WS 事件回推，同时启动 `agent.wait` 兜底收敛终态（优先采用 agent.wait 返回文本，必要时才回退 `chat.history`），并对终态回写加锁去重避免 WS/兜底并发导致多次回复；默认仅在终态回写完整回复（可选开启流式增量回写）；显式错误/空回复/模型失败提示自动向网关重试一次（重试仅发送错误/重试提示，不附带原 prompt）；pending-run 看门狗仅做 stalled 标记并继续等待兜底（不回写模型超时错误到微信）；群聊图片/链接文章仅在引用且命中触发词或 @机器人 时附带，私聊引用消息按私聊直转规则处理；引用媒体优先解析为框架公网链接 `/media/files/{filename}`，引用小程序/播客等卡片透传为 `[分享卡片] xmlType=... url=...`；短窗去重避免重复触发；回复按 `max-reply-chars` 分片发送；不下载/回传网关媒体/附件（`MEDIA:`/attachments/mediaUrl 等仅作为文本） |
| config.toml | Config | 插件配置（支持私聊免触发词、AT 直转发、斜杠命令直通 OpenClaw、关键词命令列表速查、图片转发模式、路径/公网链接生成、引用上下文开关；群聊图片/链接文章仅在引用且命中触发词或 @机器人 时才附带；支持 `stream-reply-enable` 控制是否回写流式增量，默认关闭只发终态完整回复；slash 配置下 `/new`、`/reset` 为本地会话重置且不回退普通文本转发；支持 `pending-run-ttl-seconds` 配置 pending run 的最长等待/清理时长（默认 600 秒）；支持 pending run 看门狗 `pending-run-watchdog-*`：accepted 后长时间无事件/无流式输出仅标记 stalled 并等待 `agent.wait` 兜底（不再主动轮询 `chat.history`，避免多次回写/截断）；`scopes` 默认包含 `operator.admin` 支持 `/model`、模型映射/切换等管理 RPC（不需要可移除）） |
| README.md | Doc | 使用说明与命令示例 |
