# 好好幣擴容（2026-11-16）

- 新增 `integrations.py`：`POST /api/integrations/hh/grants`（Bearer `YTDL_BRIDGE_SECRET`，`hmac.compare_digest`；空密鑰 = 404）。`authentication.py` 僅對 `POST` 此精確路徑略過使用者登入檢查，handler 自行驗密鑰，其他 `/api/` 路徑照舊。
- `user_store.py`：新表 `quota_grants`（`request_id` 主鍵 → 冪等；`subject` 為 OIDC `sub`，不綁 user_id）；`quota()` 非管理者 `limit = 基礎上限 + 有效擴容`，回傳新增 `base_limit`、`bonus`、`bonus_batches`；寫入時順手刪除到期超過 30 天的舊列。
- 前端：額度行下顯示擴容與最早到期時間（Asia/Taipei）；額度用完時提示到帳好嗎 App 好好幣商店加開；管理者清單顯示「含擴容 +b」。
- 取捨：擴容語意為「每日上限 +units 至到期」（連續 3 天每天多 1 部），不是一次性點數；不做到期清理排程（以 `expires_at > now` 判斷）。
- 測試：`tests/test_hh_bridge.py`。

# 支持者加成（2026-11-17）

- 新增 `PUT /api/integrations/hh/supporters`（同一組 Bearer `YTDL_BRIDGE_SECRET`；空密鑰 404、錯誤 401），內容 `{"supporters": [{"subject": "sub", "units": 1..100, "until": ISO-8601}]}`，最多 5000 筆。這是 Cloud 每 5 分鐘推送的**整份快照**：整張 `supporters` 表被取代，沒出現在名單的人（訂閱結束／被撤銷）立刻失去加成；`until` 已過的項目不寫入。
- `quota()` 的 `limit = 基礎上限 + 好好幣擴容 + 支持者加成`，回傳新增 `supporter_bonus`（`bonus` 為兩者總和）；管理者仍不限。
- 前端額度行顯示「支持者加成 +N（訂閱期間有效）」與擴容（分開計算）。
- 測試：`tests/test_hh_bridge.py`。
