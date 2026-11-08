# PoE2 交易提醒（AstrBot 移植版）

将 [poe2-trade-announcer-bot](https://github.com/Fabioepb/poe2-trade-announcer-bot) 的交易历史提醒流程移植为 AstrBot 插件。插件以 AstrBot 的平台适配器发送消息，不需要 Telegram Bot，也不需要在服务器安装或运行 PoE2 客户端。

## 功能

- 每 15 分钟（可调长）检查一次当前赛季的 PoE2 交易历史。
- 发现新成交后以繁体中文发送物品名称、成交价格、时间和物品图片。
- 支持订阅、退订、查看状态和最近成交记录。
- 初次成功检查只建立历史基线，不会把已有成交当作新通知。
- 订阅和成交状态使用 AstrBot 插件隔离 KV 存储。
- 群聊只有主动发送订阅命令后才会收到提醒。

物品名称优先使用繁体界面数据，并用内置 Poe2DB 繁体名称表补全可识别词条；未收录的物品或通货会以「未收錄物品／未收錄通貨」提示，不输出英文名称。名称表来源与许可见 [DATA_LICENSE.md](DATA_LICENSE.md)。

## 安装与配置

将插件目录放入 AstrBot 的 `data/plugins/astrbot_plugin_poe2_trade_announcer`，安装依赖并重载插件。然后在 AstrBot 插件配置页设置：

1. `POESESSID`：Path of Exile 账号的登录 Cookie。
2. `league_name`：当前交易赛季的精确名称。
3. `monitor_enabled`：配置完成后开启。
4. `poll_interval_minutes`：默认 15 分钟，最小 15 分钟。

**不要把 POESESSID 发给机器人或贴到群里。**持有该 Cookie 的人可以访问对应账号；只在受信任的 AstrBot 管理后台中填写。Cookie 失效时请在配置页更新。

## 指令

- `/poe2交易订阅`：订阅当前私聊或群聊；群聊成员都能看到成交内容。
- `/poe2交易退订`：取消当前会话订阅。
- `/poe2交易状态`：查看当前赛季、监控和订阅状态，不会显示 Cookie。
- `/poe2交易历史`：查看最近成交，仅限已订阅会话。

## 行为边界

- 监控默认关闭；没有 Cookie 或赛季名称时不会请求交易 API。
- 首次成功请求只记录基线，避免启动时刷出旧交易。
- 交易通知会发给所有已订阅会话；请只订阅你信任的群。
- Cookie 由 AstrBot 配置管理，但会保存在 AstrBot 的本地插件配置中；请限制服务器和备份访问权限。
- 源项目许可证带非商业使用限制；本移植版保留原版权和许可条件。使用前请阅读 [LICENSE](LICENSE)。
