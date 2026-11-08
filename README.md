# PoE2 交易提醒（AstrBot 移植版）

將 [poe2-trade-announcer-bot](https://github.com/Fabioepb/poe2-trade-announcer-bot) 的交易歷史提醒流程移植為 AstrBot 插件。插件使用 AstrBot 的平台適配器發送通知，不需要 Telegram Bot，也不需要在伺服器安裝或運行 PoE2 客戶端。

## 功能

- 每 15 分鐘（可調長）檢查一次當前賽季的 PoE2 交易歷史。
- 每位使用者私聊綁定自己的 PoE 帳號；Session ID、成交歷史、去重狀態和訂閱群聊彼此隔離。
- 一個使用者可在多個群訂閱；退訂只移除該使用者在當前對話的訂閱。
- 成交通知以繁體中文發送物品名稱、成交價格、時間和物品圖片，並標明對應的帳號暱稱。
- 每個帳號首次成功檢查只建立歷史基線，不會把舊成交當作新通知。
- 使用 AstrBot 插件 KV 保存使用者資料；舊版管理員全域 Session ID 與訂閱狀態仍保留相容。

物品名稱優先使用繁體介面資料，並用內置 Poe2DB 繁體名稱表補全可識別詞條；未收錄物品或通貨不輸出英文名稱。名稱表來源與許可見 [DATA_LICENSE.md](DATA_LICENSE.md)。

## 管理員設定

將插件目錄放入 AstrBot 的 `data/plugins/astrbot_plugin_poe2_trade_announcer`，安裝依賴並重載插件。管理員在插件設定中：

1. 設定 `league_name`，內容須與 Path of Exile 交易歷史 API 的賽季名稱一致。
2. 開啟 `monitor_enabled`。
3. 視需要調整 `poll_interval_minutes`，最低 15 分鐘。
4. `poe2_session_id` 僅為舊版管理員帳號相容欄位；新使用者不需要把自己的 Session ID 填在這裡。

## 群友綁定方式

每位想接收通知的群友都要各自完成一次：

1. 在瀏覽器登入 [pathofexile.com](https://www.pathofexile.com)。
2. 按 `F12` 開發者工具，在 Chrome 或 Edge 開啟「Application／應用程式」→「Cookies」→ `https://www.pathofexile.com`，找到 `POESESSID` 並複製 `Value` 欄位。瀏覽器介面名稱可能因語言或版本略有不同。
3. **只在與機器人的私聊中**傳送 `/poe2交易设置 <POESESSID>`；也可使用 `/poe2交易设置 POESESSID=<值>`。
4. 回到希望接收通知的群，傳送 `/poe2交易订阅`。要讓同一帳號在其他群通知，也在該群再訂閱一次。

`POESESSID` 等同帳號登入憑據。絕不可貼到群聊、截圖或程式碼倉庫。插件不會回覆或輸出此值，也不會將它寫入日誌；但 AstrBot、平台適配器、伺服器日誌或備份可能會保留收到的私聊訊息，且 KV 資料保存在 AstrBot 本機，主機管理員可能有權存取。請只在你信任的機器人上綁定。

## 指令

- `/poe2交易帮助`：查看綁定和使用步驟。
- `/poe2交易设置 <POESESSID>`：**僅限私聊**，單獨保存發送者的 Session ID。
- `/poe2交易订阅`：將發送者自己的帳號訂閱到目前群或私聊。
- `/poe2交易退订`：只取消發送者在目前對話中的訂閱。
- `/poe2交易状态`：查看自己的設定、訂閱和最近輪詢狀態，不顯示 Session ID。
- `/poe2交易历史`：查看自己的最近成交；群聊中僅限已訂閱該群的使用者。
- `/poe2交易清除`：**僅限私聊**，清除發送者在本插件中的 Session ID、個人歷史和所有訂閱。這不會使 Path of Exile 官網的登入會話失效；若憑據外洩，還需在遊戲網站登出或撤銷該會話。

## 行為與資料隔離

- 監控預設關閉；管理員未啟用監控或未設定賽季時，不會輪詢。
- 每位使用者按平台 ID 和發送者 ID 建立獨立資料區；通知、成交歷史、去重及訂閱目標不會串到其他使用者。
- 初次成功請求只建立該帳號的歷史基線，避免啟動時刷出舊交易。
- 群聊只接收已在該群訂閱的使用者帳號通知；同一群可有多位使用者各自訂閱。
- Cookie 以明文保存在 AstrBot 插件 KV；請保護伺服器檔案、管理員帳號和備份。
- 源項目許可證帶非商業使用限制；使用前請閱讀 [LICENSE](LICENSE)。
