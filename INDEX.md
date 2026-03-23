<!-- AUTO-DOC: Update me when files in this folder change -->

# Claw

OpenClaw 网关通信插件，负责网关 WS 连接、会话直通、slash 命令分流和事件回写。

## Files

| File | Role | Function |
|------|------|----------|
| __init__.py | Entry | 导出 `ClawPlugin` 供插件管理器加载 |
| main.py | Core | 实现 OpenClaw WS 客户端、触发词/私聊/AT 直通、slash 命令分流、图片/语音/视频/文件/引用/链接文章上下文附带、事件回写与 pending run 收敛；私聊 slash 可直接执行，群聊 slash 仅允许管理员在 `@机器人 + 唤醒词 + /命令` 格式下执行；真实网关方法继续按 RPC 调用，`/new`、`/reset`、`/model` 等 OpenClaw 原生命令改走 `agent` 并复用当前聊天对象的 `sessionKey`；当前会始终向 `agent` 透传 `to/groupId/accountId/sessionKey`，并在握手后通过 `channels.status` 缓存网关真实注册的渠道列表，仅在命中时才附带 `channel/replyChannel`，同时会话键保持 `agent:<agent>:<channel>:direct|group:<peer>` 规范形态；握手阶段会自动补齐 `tool-events` capability，避免网关把实时工具事件静默过滤；图片/语音/视频/文件消息统一封装为网关原生 WS `attachments`，同时写入 `content` 与 `source.base64` 两种附件内容表示，尽量兼容 Web UI / dashboard 上传格式；需要时会先把微信侧已下载的入站媒体 payload 落盘到 `files/claw-media/` 再读回 base64，不再把本地路径 `MEDIA:<path>` 或 `http://...` 公网链接透传给网关；群回复会用真实发送者 wxid 生成 `AtWxIDList`，并优先使用接收消息自带的发送者昵称（如 `PushContent`/`SenderName`），过滤掉 wxid 形态的伪昵称后再显式拼接 `@昵称`；原始网关事件体只会转发到显式配置的 `to-wxids`，不再按当前会话回推给微信用户；`龙虾 帮助/命令` 在本地返回常用命令说明；`chat` 无文本终态先等 `agent.wait/chat.history`，401/权限/账号停用等硬错误不再自动重试回打网关 |
| config.toml | Config | 插件配置，覆盖网关连接鉴权、角色权限、能力声明（默认带 `tool-events`）、微信来源渠道标识（`gateway-channel` / `gateway-account-id`，其中 `gateway-channel` 仍用于稳定 `sessionKey`，插件会结合 `channels.status` 判断是否把该渠道写进 `agent.channel`）、唤醒词/私聊/AT 直通、管理员 slash 直通、稳定 `sessionKey`、图片转发、引用上下文、去重、pending run 看门狗和事件主动转发；`Claw.EventForward.to-wxids` 留空时不会向微信转发原始网关事件体 |
| README.md | Doc | 当前可用能力、群聊 slash 约束、微信来源上下文透传策略、核心配置、事件转发边界和常用命令说明 |
