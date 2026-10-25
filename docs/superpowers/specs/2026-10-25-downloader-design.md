# 通用媒體下載與登入設計

> 此為第一階段歷史設計。使用者隔離、每日額度、網站互動登入與 ZIP 的目前行為，請以 `2026-10-25-users-site-login-zip.md` 及 README 為準。

現況：只啟動單一 worker；影片格式被 height 過濾；解析在事件迴圈同步執行；完成檔案以任意前綴猜測。

採用保留 FastAPI/Vue 介面並擴充 yt-dlp 的方案。相較於僅修補下拉選單，需一併修正下載選擇器、實際檔案辨識與排程；相較重寫為任意檔案下載管理器，此方案聚焦可驗證的媒體下載。

1. 單一排程器依目前設定派發最多 1–10 個任務，各任務在背景執行緒下載。上限調低不打斷既有工作，後續工作遵守新上限。解析亦移到背景執行緒。
2. 支援 yt-dlp 網站、HLS m3u8/TS、DASH MPD 與 HTTP 媒體直連。保留未知解析度影片與自動最佳選項，正確處理有聲影片、分離音軌及純音訊。MP4/MKV 採重新封裝，原始模式保留下載容器，音訊可選 MP3/M4A。
3. 每次解析與下載都帶同一組 Referer/User-Agent。分段並行獨立設定。失敗任務可重試。只認列經後處理確認的完成檔案；不將 part 或 fragment 當完成。
4. cookies 可選上傳 Netscape 檔案、執行主機的瀏覽器或停用。檔案為每次操作建立快照，避免併發改寫；有效性、來源測試與刪除可在設定操作。遠端網頁不能讀取用戶端其他網站 cookies。
5. 參考 BeeCount-Cloud 與 SwipeSmart 的 Synology OIDC：沿用 OIDC_AUTHORITY/CLIENT_ID/CLIENT_SECRET/SCOPE/REDIRECT_URI，code + query，PKCE 預設關閉且可設定啟用。驗證 state、nonce 與 ID token；HttpOnly session 同時保護 API、WebSocket、檔案。保留 token 登入，支援 SSO-only。可設定 subject allowlist。
6. 這是共用可信使用者下載工具：任務、設定、cookies 共用，並非多租戶系統。SSO 不會自動取得 YouTube 等網站 cookies。不支援 DRM、孤立 .m4s 片段拼接或任意網站登入自動化。

驗證：併發上限與動態變更、未知高度格式、影片/音訊選擇與輸出、cookie 驗證、API/WS/session 驗證、模擬 OIDC provider 與真實本地 HLS/TS 下載，最後以瀏覽器操作新增介面。
