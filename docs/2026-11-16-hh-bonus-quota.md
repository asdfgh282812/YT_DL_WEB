# 好好幣擴容（2026-11-16）

- 新增 `integrations.py`：`POST /api/integrations/hh/grants`（Bearer `YTDL_BRIDGE_SECRET`，`hmac.compare_digest`；空密鑰 = 404）。`authentication.py` 僅對 `POST` 此精確路徑略過使用者登入檢查，handler 自行驗密鑰，其他 `/api/` 路徑照舊。
- `user_store.py`：新表 `quota_grants`（`request_id` 主鍵 → 冪等；`subject` 為 OIDC `sub`，不綁 user_id）；`quota()` 非管理者 `limit = 基礎上限 + 有效擴容`，回傳新增 `base_limit`、`bonus`、`bonus_batches`；寫入時順手刪除到期超過 30 天的舊列。
- 前端：額度行下顯示擴容與最早到期時間（Asia/Taipei）；額度用完時提示到帳好嗎 App 好好幣商店加開；管理者清單顯示「含擴容 +b」。
- 取捨：擴容語意為「每日上限 +units 至到期」（連續 3 天每天多 1 部），不是一次性點數；不做到期清理排程（以 `expires_at > now` 判斷）。
- 測試：`tests/test_hh_bridge.py`。
