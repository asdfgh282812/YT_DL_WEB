# 通用媒體下載工具

FastAPI + Vue + yt-dlp 的媒體下載介面，支援獨立使用者、每日下載額度、網站登入 cookies，以及一鍵 ZIP。可處理 yt-dlp 支援的影片網站、HLS（m3u8／TS）、DASH（MPD）、媒體直連與播放清單；實際範圍取決於來源網站與授權。

## 使用流程

1. 登入此工具：一般使用者使用管理者建立的帳號密碼，或使用 Synology／OIDC SSO；系統管理者仍可用 `AUTH_TOKEN` 登入。
2. 貼上影片或播放清單網址並解析。若來源要求登入，按「查看教學並匯入 cookies.txt」。
3. 在本機 Chrome 登入影片網站，用擴充套件匯出 cookies.txt，再到工具的「Cookies 設定」匯入，重新解析或重試任務。
4. 選格式加入下載。完成後可個別取回，或按「一鍵下載 ZIP」取回目前帳號的全部完成項目。

## 使用者與每日部數

管理者可在頁首「使用者」中新增帳號、設定角色、重設密碼、停用帳號與調整額度。一般帳號沒有公開註冊入口。

- 管理者每日不限部數；同時下載的系統上限仍適用。
- 一般使用者預設每天 **5 部**。管理者可修改全體預設，或替個人設定每日上限；個人選「使用全體預設」時會跟隨新預設值。
- 0 代表不允許新增下載。上限不是一次批次的大小，播放清單中每一部都各計一部。
- 加入佇列時先預留額度，成功完成後記入當天；失敗或刪除尚未執行的排隊任務會歸還額度。重試失敗任務須重新預留。
- 每天 **Asia/Taipei 00:00** 以日期切換當日完成量。前一天尚未完成的預留繼續保留，完成時記入完成當天，避免跨午夜重複取得額度。
- 批次加入採原子檢查，額度不足時整批拒絕。多個分頁或同時送出的請求不能超收額度。
- 刪除已完成任務、登出、重新登入或重啟，不會清除已成功下載的當日計數。取回單檔／ZIP 不會再扣額度。
- 降低上限不會取消已接受的下載。停用帳號會撤銷 session，尚未執行的任務會停止並歸還預留，已執行的任務不強制中斷。

使用者只會看到自己的任務、檔案、ZIP 與網站登入資料。WebSocket 與下載端點同樣檢查所有者；管理者的任務畫面也不混入其他人的任務。伺服器下載參數、使用者管理與系統更新僅限管理者。

## cookies.txt 使用教學

工具已移除伺服器瀏覽器互動登入。SSO 用於登入此工具；影片網站所需的登入資料由個人匯入的 cookies.txt 提供。

1. 在電腦 Chrome 安裝 [Get cookies.txt LOCALLY](https://chromewebstore.google.com/detail/get-cookiestxt-locally/cclelndahbckbenkjhflpdbgdldlbecc)，按「加到 Chrome」。
2. 在本機開啟目標網站（例如 YouTube），登入自己的帳號，確認影片可以播放。
3. 保持在目標網站分頁，從 Chrome 右上角的擴充功能圖示開啟 Get cookies.txt LOCALLY。將 **Export Format** 設為 **Netscape**，按 **Export** 或 **Export As**，儲存 `.txt` 檔案。
4. 回到下載工具，開啟「Cookies 設定」，選取檔案並按「匯入個人 cookies」。只接受 Netscape 格式，檔案上限 **2 MB**。
5. 匯入成功後，關閉設定視窗，重新按「解析網址」；已失敗的任務則按「重試」。

只需匯出要下載網站的 cookies。新檔案會取代上一份個人匯入檔案。cookies 包含登入憑證，請妥善保管，只上傳至信任的下載工具，不要提交到 GitHub。登入失效時，重新登入網站、匯出並匯入；成功讀取檔案不代表網站一定接受該登入狀態。

個人 cookies 由 `SESSION_SECRET` 衍生的金鑰加密保存，僅供該帳號解析與下載，不由 API 傳回原始值。舊版已保存的個人網站 cookies 仍可使用及移除；同一網站有多份資料時，採適用且較新的資料。

舊版 `config/cookies.txt`／執行主機的瀏覽器來源只供既有系統管理者使用，不能供其他帳號取用。移除系統管理者的個人匯入資料時，也會移除舊 cookies.txt，避免刪除後繼續套用。讀取執行主機瀏覽器來源需自行提供可存取的瀏覽器設定檔，容器不再內建瀏覽器。

擴充套件介面參考：[原作者程式碼](https://github.com/kairi003/Get-cookies.txt-LOCALLY/blob/master/src/popup.html)。來源網站的 IP、登入狀態或授權限制仍可能造成下載失敗。

## 一鍵 ZIP

「一鍵下載 ZIP」只包含目前使用者已完成且檔案仍存在的項目。採 ZIP_STORED 串流，影片不重複壓縮、不在伺服器另存完整 ZIP，也不先在網頁記憶體累積所有影片。支援 Zip64 與瀏覽器原生下載進度。

同名檔案會自動加上 `(2)`、`(3)`；檔名保留中文並移除路徑、控制字元與不適用的檔名字元。打包中的檔案不會被暫存清理或刪除 API 移除。

## 下載能力

- 最多同時執行 1–10 個下載任務，調高設定即時派發；調低設定等待既有任務完成。
- 每部 HLS／DASH 的分段連線數獨立設定為 1–16。
- 未標記解析度的 HLS 仍可選影片；必要時以 ffprobe 探測，純音軌來源顯示提示。
- 影片輸出 MP4、MKV、原始容器；音訊輸出 MP3、M4A。MP4／MKV 為重新封裝，編碼不相容時可選 MKV。
- 可提供播放頁 Referer 與 User-Agent，解析與下載一致套用。
- 解析在背景執行，不阻塞進度。輸出須完成後處理並通過媒體檢查，`.part` 或分段不會被當作完成。
- 失敗任務可重試。需要登入的失敗任務提供 cookies.txt 教學與匯入入口；匯入後再按任務的「重試」。

## 本機啟動

需要 Python 3.12+、可執行的 FFmpeg 與 ffprobe。YouTube JS challenge 建議備 Node 22+ 或 Deno。

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
test -f .env || cp .env.example .env
# 編輯 .env，至少設定一組長隨機 AUTH_TOKEN
.venv/bin/uvicorn main:app --env-file .env --host 127.0.0.1 --port 8000 --workers 1 --no-access-log
```

開啟 http://localhost:8000，展開「管理者 Token 登入」完成首次登入，再到「使用者」建立一般帳號，也可替系統管理者 `admin` 設定帳號密碼。

必須保持 `--workers 1`：排程與任務清單是單程序狀態，程序內會依設定同時下載多部。使用者、登入 session、每日完成記錄和 cookies 存在 SQLite，重啟仍保留；任務清單目前不跨重啟保存，遺留的未完成預留會釋放。完成／失敗任務及孤立暫存檔兩小時後清除。

工具登入自登入時間起預設有效 **7 天**，可用 `SESSION_MAX_AGE` 設定秒數（3600～2592000，即 1 小時～30 天），修改後重啟，新登入套用新的期限。SSO 的 ID token 在登入時仍驗證有效期限、簽章、state 與 nonce；通過後建立本工具獨立的登入期限，不會隨短效 ID token 到期而提早登出。資料庫僅保存隨機登入識別碼的雜湊，不保存 SSO 權杖。維持原本的 `config/` 掛載與簽章金鑰，容器重建後即可延續登入；升級前只存在記憶體的舊登入需重新登入一次。

主動登出、帳號停用或重設密碼會讓舊登入失效。管理者調整角色或額度會立即反映；IdP 端登出不會自動撤銷此工具的登入。即時連線中斷時網頁會重連；若收到登入相關的連線拒絕，先透過 HTTP 確認，只有工具明確回傳未登入才切回登入頁。

## Docker／NAS

```sh
test -f .env || cp .env.example .env
# 設定 AUTH_TOKEN、對外網址與需要的 OIDC 欄位
# 更新部署時重新建置，以套用程式與依賴調整
docker compose -f compose.yaml up -d --build
```

`compose.yaml` 與 `docker-compose.yml` 均提供 `video-downloader` 服務；部署時明確指定使用哪一份，連接埠和掛載位置依該檔案設定。映像包含 Node、FFmpeg 與系統依賴，已移除 Playwright／Chromium。`config/` 和 `temp/` 為持久掛載，來源程式碼打包於映像，因此更新需要 rebuild。舊的 `SITE_LOGIN_*` 環境變數不再使用，可從既有 `.env` 移除。

對外使用 HTTPS 時設定 `PUBLIC_BASE_URL=https://你的下載工具網域`、`SESSION_HTTPS_ONLY=true`。避免在代理存取日誌紀錄 OIDC callback 的授權碼。登入與 cookies 匯入涉及敏感資料，正式服務應使用 HTTPS。

備份 `config/users.sqlite3` 和 `config/.session-secret`；若使用環境變數 `SESSION_SECRET`，請一併保存該值。變更／遺失金鑰會使既有 cookies 無法解密。資料庫應放在 NAS 本機磁碟／Docker volume，維持 SQLite 檔案鎖可用。

## Synology／OIDC SSO

沿用 BeeCount-Cloud 與 SwipeSmart 的設定名稱與 Synology 相容的 Authorization Code + `response_mode=query`，預設不啟用 PKCE。Authlib 驗證 state、nonce、簽章、issuer、audience 和有效期；使用者識別綁定 issuer 與 sub。

1. 在同一個 IdP 建立下載工具專用 client，不共用其他應用的 client secret。
2. 登記 callback：`https://你的下載工具網域/auth/sso/callback`。
3. 設定環境變數並重啟：

```dotenv
PUBLIC_BASE_URL=https://download.example.com
SESSION_HTTPS_ONLY=true
OIDC_AUTHORITY=https://nas.example.com/sso/webman/sso
OIDC_CLIENT_ID=media-downloader
OIDC_CLIENT_SECRET=由下載工具專用client取得
OIDC_SCOPE=openid profile email
OIDC_USE_PKCE=false
OIDC_TOKEN_AUTH_METHOD=client_secret_post
OIDC_ADMIN_SUBJECTS=你的管理者sub
```

`OIDC_REDIRECT_URI` 留空時使用 `{PUBLIC_BASE_URL}/auth/sso/callback`。需要 PKCE 的 IdP 可設 `OIDC_USE_PKCE=true`；需要 basic client authentication 的可設 `OIDC_TOKEN_AUTH_METHOD=client_secret_basic`。

也支援 `/api/v1/auth/sso/callback`，方便沿用既有 Synology client 設定；此時將 `OIDC_REDIRECT_URI` 設為完整網址，並與 IdP 登記的 callback 完全一致。兩個路徑使用相同的 state、nonce 與 ID token 驗證。若回傳頁顯示「請先登入系統」，請確認已更新並重新建置容器，舊版尚未支援 `/api/v1/` 回呼路徑。

新 SSO 使用者預設為一般帳號，每日 5 部或管理者指定的新預設。`OIDC_ADMIN_SUBJECTS` 是固定授權為管理者的 sub 清單，每次登入會確保清單內帳號為管理者；若要從網頁降權，請先從此清單移除。停用仍會阻止登入。其他帳號的角色與個別額度可由管理者在網頁調整。

`OIDC_ALLOWED_SUBJECTS` 可另外限制允許進入工具的帳號，留空允許該 IdP 的有效帳號。`AUTH_MODE=sso` 關閉本機帳密及 Token 登入；啟用前務必配置可用的 SSO 管理者。工具內登出不會登出其他 IdP 應用；影片網站的登入狀態由個人 cookies 提供。

## 串流排查

- 音軌子 m3u8 不含影片，請貼包含影片的主播放清單。沒有 720p／1080p 標記不等於只有音訊。
- 403 可能是登入、來源限制、IP 限制或簽章連結過期。先確認能在網站播放，必要時提供 Referer／User-Agent 或重新取得連結。
- 孤立 `.m4s` 通常需要初始化段及完整 manifest，請提供 MPD／m3u8。DRM 保護不支援。
- cookies 只能提供該帳號原有的存取權限，不會繞過影片本身的授權或地區限制。

## 驗證

```sh
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest tests -q
node --check frontend/app.js
node --test tests/test_frontend.cjs
```

測試包括真實本地 HLS／DASH／媒體直連，原子額度、跨日、持久計數、角色與所有者檢查、ZIP 內容與檔名，以及 cookies.txt 匯入後下載受保護影片。Python 測試不需要 Chromium；Playwright 僅列於開發依賴，供人工介面驗證使用。

官方參考：[yt-dlp](https://github.com/yt-dlp/yt-dlp)、[Authlib Starlette](https://docs.authlib.org/en/latest/oauth2/client/web/starlette.html)、[zipstream-ng](https://github.com/pR0Ps/zipstream-ng)。
