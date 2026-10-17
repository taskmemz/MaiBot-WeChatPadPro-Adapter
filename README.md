# MaiBot-WeChatPadPro-Adapter

通过 [WeChatPadPro](https://github.com/WeChatPadPro/WeChatPadPro) 协议将微信个人号接入 MaiBot 的插件。

## 特性

- **消息收发**：文本、图片、语音、emoji、引用、链接、小程序提示等
- **群聊支持**：群消息识别、@机器人检测、@列表转发、群成员昵称解析
- **登录全自动**：未登录时自动生成二维码、轮询扫码状态、扫码成功后无缝转入消息接收
- **自动重连**：WebSocket 断线自动恢复
- **名单过滤**：私聊/群聊白名单/黑名单
- **双工网关**：基于 MaiBot Plugin SDK 2.x 的 `MessageGateway(duplex)`，收发统一在一个插件内完成

## 环境要求

- MaiBot 主程序（Plugin SDK ≥ 2.5.0）
- 已部署的 WeChatPadPro 服务端（请自行搜索部署方式，这里不提供）
- Python ≥ 3.10
- `requests`、`websockets`（由插件 requirements 提供）

## 安装

将本仓库克隆到 MaiBot 的 `plugins/` 目录：

```bash
git clone https://github.com/taskmemz/MaiBot-WeChatPadPro-Adapter.git plugins/MaiBot-WeChatPadPro-Adapter
```

> 插件目录名不影响加载，`_manifest.json` 中的 `id` 是唯一标识。
> 依赖（`requests`、`websockets`）请通过 MaiBot 的插件依赖管理安装，或手动 `pip install requests websockets`。

## 配置

复制配置模板：

```bash
cp template/config.toml config.toml
```

编辑 `config.toml`：

```toml
[plugin]
enabled = true
config_version = "1"

[wechatpad_server]
wechatpad_url = "http://127.0.0.1:1238"   # WeChatPadPro HTTP API
wechatpad_ws = "ws://127.0.0.1:1238"      # WebSocket 推送地址
admin_key = "你的ADMIN_KEY"               # 与 token 二选一
token = ""                                # 优先使用
connection_id = "default"
login_check_interval = 30                 # 未登录时扫码状态轮询间隔（秒）

[filters]
ignore_self_message = true
ignore_official_account = true
private_whitelist = []
private_blacklist = []
group_whitelist = []
group_blacklist = []
```

### 关于 token

适配器启动时：

1. 优先使用 `token` 配置项，并用 `GetLoginStatus` 探测其是否仍然有效；
2. 若为空或已失效（服务端返回「该链接不存在」），则使用 `admin_key` 调用
   `/admin/GenAuthKey1` 申请一个新的 token；
3. 两者都为空则启动失败。

> **注意**：WeChatPadPro 的 auth key 必须先经过一次 `GetLoginQrCodeNewX` 调用
> 才会与登录链路绑定，此时 `CheckLoginStatus` / 消息同步等接口才可用。
> 适配器内部会自动完成这个绑定。

### 关于登录

适配器启动时的登录流程：

1. 检查 `GetLoginStatus`：
   - `Code == 200` → 已登录，直接拉取资料并启动消息接收；
   - `Code == 300` → 未登录，进入扫码流程；
   - `Code == -2`（链接不存在）→ token 未绑定，自动重新申请并获取二维码。
2. 未登录时调用 `GetLoginQrCodeNewX` 生成二维码，通过网关状态上报
   （`metadata.login_qr_link` / `state: waiting_scan`）暴露给 Host；
3. 按 `login_check_interval` 轮询 `CheckLoginStatus`（`state`: 0 未扫码 /
   1 已扫码 / 2 登录成功 / 4 二维码过期，过期自动重新生成）；
4. 扫码成功后自动拉取账号资料、启动 WebSocket 推送并上报 `ready`。

也可以通过插件 Tool 主动操作：

- `get_login_status`：查询登录状态
- `get_login_qr`：获取二维码 base64 与链接
- `refresh_token`：用 AdminKey 重申请 token 并重新绑定链路

## 工作原理

```
微信客户端 ←→ WeChatPadPro 服务端 ←(WebSocket /GetSyncMsg)→ 本适配器
                                ↑(HTTP API)                 ↓
                                                    MaiBot Host (MessageGateway)
```

入站：`GetSyncMsg` 推送 → `inbound_codec` 转成 `MessageDict` → `ctx.gateway.route_message()`

出站：Host 调用 `@MessageGateway(duplex)` 方法 → `outbound_codec` 生成发送计划 → 调用 HTTP API

## 目录结构

```
├── _manifest.json     # 插件清单
├── plugin.py          # 插件主入口
├── config.py          # 配置模型（WebUI 可编辑）
├── constants.py       # 网关名 / 消息类型常量
├── template/config.toml
└── libs/wechatpad_api/
    ├── client.py          # HTTP API 封装
    ├── transport.py       # WebSocket 客户端（独立线程 + 自动重连）
    ├── inbound_codec.py   # WeChatPadPro → MessageDict
    ├── outbound_codec.py  # MessageDict → WeChatPadPro 发送计划
    └── util/message_utils.py
```

## 已知限制

- 出站图片仅支持 URL（WeChatPadPro `SendImageMessage` 只接受图片链接），base64 直发不受支持
- 引用消息入站时会解析出引用内容，但出站暂不会自动拼装引用 XML
- 视频号/小程序等复合消息仅以占位文本呈现
- 群成员昵称解析依赖 `GetChatroomMemberDetail`，API 限频时昵称回退为 wxid

## 许可证

GPL-3.0-or-later
