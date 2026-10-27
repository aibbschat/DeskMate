# 小桔的 QQ 好友通道（私聊版）

目标：小桔 = 萧哥 QQ 里的一个好友。只私聊，不进群。

链路：

    QQ 好友私聊 → NapCat（OneBot 11 正向 WebSocket）→ qq_friend.py
                 → 合并连发（等你把话说完）→ run_turn（小桔完整管线）
                 → 按句末标点拆条 + 随机延迟 → send_private_msg

代码：deskmate/connectors/qq_friend.py
测试：pytest tests/test_qq_friend.py（离线，fake WS，不需要 NapCat）

## 1. 前置

- QQ NT（9.9.x）——/Applications/QQ.app 已有。
- NapCat Shell 版（目前机器上还没装）：https://github.com/NapNeko/NapCatQQ
  装完用 launcher-user.bat（Windows）或对应启动脚本拉起，务必用小号。
- 小号加萧哥主号为好友。
- 不要拿主号：NapCat 接管整个客户端，非官方客户端的封号判定不透明。

## 2. NapCat 侧

在 NapCat 的「网络配置」里开 正向 WebSocket：

- 端口：3001
- Token：设一个（填到下面配置里；走 Authorization: Bearer <token>）
- 只勾正向 WS 就够了，不需要 HTTP，也不需要反向 WS。

## 3. 配置

~/.config/coworker/qq_friend.json：

    {
      "ws_url": "ws://127.0.0.1:3001",
      "access_token": "NapCat 里设的 token",
      "owners": ["萧哥主号的 QQ 号"],
      "quiet_seconds": 2.5,
      "max_wait_seconds": 30,
      "max_parts": 4,
      "reply_delay_min": 0.8,
      "reply_delay_max": 2.5
    }

字段：

- owners：白名单，空数组 = 谁都不理（安全默认，和 connectors 的 allowlist 一个原则）。
- quiet_seconds：同一个人连发时，静默这么久才打包成一段一起答。
- max_wait_seconds：最长等多久，防止对方一直发导致永远不答。
- max_parts：一次回复最多拆成几条。
- reply_delay_min/max：条与条之间的随机间隔（秒）。

环境变量可以覆盖：QQ_FRIEND_WS_URL、QQ_FRIEND_TOKEN、QQ_FRIEND_OWNERS（逗号分隔）。

## 4. 启动

启动后端前设 QQ_FRIEND_ENABLE=1；coworker/server/app.py 的 lifespan 会拉起通道，
配置缺失只记日志、不影响主服务。会话 id 固定为 qq-friend（跨重启续上对话）。

## 5. 行为约定

- 只处理 message_type=private；群消息（含 @）一律忽略。
- 只理 owners 里的 QQ 号；其他人发来只记一行日志。
- 图片/表情/语音进来降级成 [图片] / [表情] / [语音] 占位符。
- 回复按 。！？!?…～ 和换行拆条；不超过 4 字的碎片并进上一条；最多 4 条。
- send_image() 已经写好（file:// 绝对路径），表情包接入时直接调。
- 断线按 5 秒重连；单条处理异常只记日志，循环不死。

## 6. 表情（已接，2026-10-27 实测）

小桔输出里的 ![名字](emote:peek) 或 [[emote:peek]] 会在发送端被换成 QQ 图片：

1. 按 id 找 surfaces/gui/src/assets/emotes/{id}.webp；
2. 压到 512px 内的 PNG；
3. 转 base64 塞进消息段——NapCat 跑在 QQ 的 App Sandbox 里，file:// 指向任何
   外部路径都会 EPERM（retcode 1200），QQ 侧静默丢掉，只剩文字；
4. 找不到 id 就跳过，绝不把占位符当文字发出去。

发送端会检查 retcode：OneBot 的 HTTP 层永远 200，错误只在 status/retcode 里。

## 7. 常驻

NapCat 常驻用 launchd（~/Library/LaunchAgents/com.xiaocheng.napcat.plist）：

    /Applications/QQ.app/Contents/MacOS/QQ --no-sandbox -q 346102848

带 -q 走快速登录，不用每次扫码；日志在 ~/Library/Logs/napcat.log。
重启：launchctl kickstart -k gui/$(id -u)/com.xiaocheng.napcat。

注意：macOS arm64 上 NativePacketClient 加载不了（签名 Team ID 不匹配，见
NapCat issue #2066），文字和图片消息不受影响，发文件/戳一戳之类的可能不行。

## 8. 还没做（按需要再加）

- 长任务的即时反馈：engine 跑一轮可能几十秒，QQ 那边会冷场。
  要的话加一句「我先看下」的即时回执（在 _answer() 里起一个延时任务）。
- 审批交互：QQ 没有按钮，得降级成「回 1/2/3」。
- 表情包：send_image() 就位，缺的是从 emotes manifest 挑图的策略。
