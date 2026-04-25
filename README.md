# 影片下載網頁系統 (Video Downloader Web App)

這是一個基於 Python (FastAPI) 與 Vue.js 開發的影片下載系統，支援透過 `yt-dlp` 與 `FFmpeg` 進行高畫質影片下載與音訊擷取，並提供即時的 WebSocket 進度顯示。

系統專為部署於 Synology NAS 設計，具備輕量化的密碼驗證、任務佇列、以及自動清除暫存檔的機制。

---

## 本地開發與測試指南 (Non-Docker)

如果你想在自己的電腦上（Windows/macOS/Linux）進行開發與測試，請依照以下步驟操作。

### 1. 建立 Python 虛擬環境 (venv) 與安裝依賴

首先，確保你的系統已安裝 Python 3.9 或以上版本。

```bash
# 1. 建立虛擬環境
python3 -m venv venv

# 2. 啟動虛擬環境
# Windows:
venv\Scripts\activate
# macOS/Linux:
source venv/bin/activate

# 3. 安裝 Python 依賴包
pip install -r requirements.txt
```

### 2. 手動配置本地環境的 FFmpeg 路徑

`yt-dlp` 需要依賴 `FFmpeg` 來進行影音合併（例如將最高畫質的無聲影片與音軌合併為 MP4）或擷取 MP3 音訊。

- **Windows:**
  1. 前往 [gyan.dev](https://www.gyan.dev/ffmpeg/builds/) 下載 FFmpeg release build。
  2. 解壓縮後，將 `bin` 資料夾的路徑（例如 `C:\ffmpeg\bin`）加入到系統的「環境變數 (Environment Variables)」 -> `Path` 中。
  3. 打開新的終端機，輸入 `ffmpeg -version` 確認安裝成功。

- **macOS:**
  ```bash
  brew install ffmpeg
  ```

- **Linux (Ubuntu/Debian):**
  ```bash
  sudo apt update
  sudo apt install ffmpeg
  ```

### 3. 直接執行進行下載測試

設定環境變數並啟動 FastAPI 伺服器：

```bash
# 設定登入密碼 (可自訂)
# Windows (PowerShell):
$env:AUTH_TOKEN="your_secure_password_here"
# macOS/Linux:
export AUTH_TOKEN="your_secure_password_here"

# 啟動伺服器
uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

伺服器啟動後，請打開瀏覽器前往 `http://localhost:8000`，輸入你剛剛設定的 `AUTH_TOKEN` 即可開始使用。

---

## Synology Docker 佈署指南

本系統非常適合透過 Docker 部署於 Synology NAS 上，確保環境隔離且不消耗過多資源。

### 1. 說明 docker-compose.yml 中的環境變數設定

在專案目錄下有一個 `docker-compose.yml` 檔案：

```yaml
version: '3.8'

services:
  video-downloader:
    build: .
    container_name: video-downloader
    restart: unless-stopped
    ports:
      - "8000:8000"
    environment:
      - PUID=1000
      - PGID=1000
      - AUTH_TOKEN=your_secure_password_here # <--- 請務必修改此處
    volumes:
      - ./frontend:/app/frontend
      - ./temp:/app/temp
      - ./config:/app/config
      - ./main.py:/app/main.py
```

- **`AUTH_TOKEN`**: 請將 `your_secure_password_here` 替換為你自己想要的登入密碼。這是進入網頁和執行 API 時的唯一認證方式。

### 2. 設定資料夾權限（PUID/PGID）

在 Synology NAS 中，Docker 容器預設以 root 執行，這可能會導致下載的檔案產生權限問題（你在 File Station 中無法刪除或編輯）。

為解決此問題，請透過 SSH 連入你的 NAS，輸入 `id` 指令查看你目前使用者的 `uid` 與 `gid`：
```bash
$ id
uid=1026(your_username) gid=100(users) groups=100(users),101(administrators)
```

然後將 `docker-compose.yml` 中的 `PUID` 與 `PGID` 修改對應的數值（例如 `PUID=1026`, `PGID=100`）。
這樣容器建立的暫存檔就會屬於你的 NAS 帳號。

### 3. Volume 映射與前端「免建置更新」

在 `docker-compose.yml` 中，我們設定了以下映射 (Volumes)：
- `./frontend:/app/frontend`
- `./temp:/app/temp`
- `./config:/app/config`
- `./main.py:/app/main.py`

**前端免建置更新的原理：**
因為我們採用了 Vue.js 3 (CDN 版本) 與 Tailwind CSS (CDN 版本)，**前端完全不需要進行 Node.js 或 Webpack 等編譯打包過程**。
透過將 Host 端的 `./frontend` 資料夾直接掛載到 Docker 內的 `/app/frontend`，當你想要修改網頁介面（例如修改 `index.html` 或 `app.js`）時，只需要在 NAS 的檔案管理員中直接編輯這些檔案，重新整理瀏覽器後，修改就會**立即生效**，完全不需要重啟 Docker 容器。

### 4. 啟動服務

1. 在 Synology NAS 上安裝「Container Manager」(舊稱 Docker)。
2. 透過 SSH 或「工作排程器」進入此專案目錄。
3. 執行以下指令建立並啟動容器：
   ```bash
   docker-compose up -d --build
   ```
4. 啟動後，在瀏覽器輸入 `http://<NAS_IP>:8000` 即可訪問系統。