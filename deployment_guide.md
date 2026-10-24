# FB Scraper SaaS 部署、架構與維運指南

本文件以目前 Repository 內的程式為準，說明系統架構、Firebase、GitHub Pages、GitHub Actions、Gmail SMTP、部署驗證及常見問題。

> 最後核對日期：2026-10-24

## 1. 系統用途

系統讓使用者透過網頁設定 Facebook 公開粉絲專頁、關鍵字、收件人與排程。後端定時開啟粉專頁面、擷取近期貼文、比對關鍵字，並以 Gmail SMTP 寄出通知。

目前系統只處理 Facebook 公開頁面；Facebook 的登入牆、反自動化機制及頁面 DOM 改版，都可能造成抓取失敗。

## 2. 系統架構

```text
使用者瀏覽器
  └─ GitHub Pages：docs/index.html
       ├─ Firebase Authentication：登入
       └─ Cloud Firestore：讀寫 users/{uid}

GitHub Actions：scraper.yml
  └─ fb_scraper.py + Playwright/Chromium
       ├─ 讀取 users 集合中的所有任務
       ├─ 依排程抓取 Facebook 公開粉專
       ├─ 讀寫 states/{uid} 去重與計算回溯時間
       └─ Gmail SMTP 寄出符合關鍵字的貼文

GitHub Actions：deploy-firestore-rules.yml
  └─ 將 firestore.rules 發布至 Firebase
```

三條部署流程彼此獨立：

1. `docs/` 由 GitHub Pages 發布。
2. `fb_scraper.py` 由 `FB Scraper SaaS Engine` 執行。
3. `firestore.rules` 由 `Deploy Firestore Rules` 發布。

Pages 部署成功不代表 Firestore Rules 已發布，也不代表爬蟲或寄信成功。

## 3. Repository 檔案

| 路徑 | 用途 |
| --- | --- |
| `docs/index.html` | 管理介面、Firebase Web SDK、登入與任務編輯 |
| `fb_scraper.py` | 排程判斷、Facebook 抓取、關鍵字比對、Email 與狀態儲存 |
| `.github/workflows/scraper.yml` | 每 15 分鐘喚醒爬蟲，也支援手動強制執行 |
| `.github/workflows/deploy-firestore-rules.yml` | 發布 Firestore Security Rules |
| `firestore.rules` | 使用者與管理員的 Firestore 存取規則 |
| `firebase.json` | Firebase CLI 的 Rules 路徑設定 |
| `requirements.txt` | 本機 Python 依賴；Actions 目前另行安裝套件 |
| `config.json` | 舊版設定檔，目前執行程式不會讀取 |
| `fb_scraper.txt` | 早期需求記錄，目前執行程式不會讀取 |

任務的唯一有效來源是 Firestore `users` 集合，不是 `config.json`。

## 4. Firestore 資料模型

### `users/{uid}`

```json
{
  "email": "user@example.com",
  "last_login": "2026-10-24T09:00:00.000Z",
  "tasks": [
    {
      "task_name": "報名活動",
      "schedule": {
        "type": "daily",
        "time": "09:00",
        "interval_minutes": 15,
        "days_of_week": [0, 1, 2, 3, 4, 5, 6],
        "days_of_month": [1]
      },
      "recipients": ["user@example.com"],
      "keywords": ["報名", "活動"],
      "pages": [
        {
          "name": "國立臺灣科學教育館",
          "url": "https://www.facebook.com/ntsec"
        }
      ]
    }
  ]
}
```

`days_of_week` 使用 Python 規則：`0` 是星期一，`6` 是星期日。

### `states/{uid}`

同一份文件以任務名稱作為欄位：

```json
{
  "報名活動": {
    "last_run_time": "2026-10-24T09:05:00+08:00",
    "seen_urls": [
      "https://www.facebook.com/example/posts/123"
    ]
  }
}
```

修改任務名稱會形成新的狀態欄位，程式會將它視為新任務。

## 5. 建立 Firebase 專案

### 5.1 建立 Web App

1. 進入 [Firebase Console](https://console.firebase.google.com/)。
2. 建立或選擇專案。本專案目前使用的 Project ID 是 `fb-event-notifier`。
3. Project settings → General → Your apps → 建立 Web App。
4. 將取得的 `firebaseConfig` 更新到 `docs/index.html`。

Firebase Web API Key 不是伺服器私密金鑰，可以出現在前端；資料安全必須由 Authentication 與 Firestore Rules 控制。

### 5.2 啟用 Authentication

Authentication → Sign-in method，依需求啟用：

- Email/Password
- Google
- Facebook（選用，另需 Meta App ID 與 Secret）

Authentication → Settings → Authorized domains 加入：

```text
cy872024.github.io
```

若 Repository 或 GitHub 帳號不同，請改成實際 GitHub Pages 網域。

### 5.3 建立 Firestore

1. Firestore Database → Create database。
2. 選擇正式使用的區域；建立後通常不能變更。
3. 不要長期使用 `allow read, write: if true` 或會在 30 天後過期的 Test Mode。
4. 使用 Repository 內的 `firestore.rules`。

現有 Rules：

- 已登入者可讀寫自己的 `users/{uid}`。
- `lcy872024@gmail.com` 可讀寫所有使用者資料。
- 使用者可讀自己的 `states/{uid}`，不能從前端修改。
- Firebase Admin SDK 使用 Service Account，會略過 Firestore Security Rules。

如要更換管理員，必須同步修改：

```text
docs/index.html     → ADMIN_EMAILS
firestore.rules     → isAdmin()
```

只修改前端 `ADMIN_EMAILS` 不會取得資料庫管理權限。

## 6. 建立與授權 Service Account

### 6.1 取得 JSON Key

Firebase Console → Project settings → Service accounts → Generate new private key。

下載的 JSON 是伺服器私密憑證：

- 不可 commit。
- 不可貼到 Issue、Action Log 或前端程式。
- 完整 JSON 內容應存入 GitHub Secret `FIREBASE_SERVICE_ACCOUNT`。

個人 Google 帳號改密碼不會使 Service Account Key 自動失效；Service Account 使用獨立金鑰。

### 6.2 部署 Rules 所需 IAM 角色

執行爬蟲與部署 Rules 使用同一個 Secret，但部署 Rules 還需要額外 IAM 權限。

在 Google Cloud Console → IAM 中，找到 JSON 的 `client_email`，授予：

```text
Service Usage Viewer     roles/serviceusage.serviceUsageViewer
Firebase Rules Admin     roles/firebaserules.admin
```

若缺少權限，`Deploy Firestore Rules` 會出現：

```text
403 Permission denied to get service [firestore.googleapis.com]
```

## 7. 設定 Gmail 通知

1. 寄件 Google 帳號啟用兩步驟驗證。
2. 建立一組 App Password，名稱可填 `FB-Scraper-Bot`。
3. 將 Gmail 地址存入 `GMAIL_SENDER`。
4. 將 16 字元 App Password 存入 `GMAIL_APP_PASSWORD`，不要使用一般登入密碼。

Google 帳號密碼變更後，既有 App Password 會被撤銷，必須重新產生並更新 GitHub Secret。

SMTP 顯示成功只代表 Gmail SMTP 接受郵件；首次通知可能進入垃圾郵件。請將郵件標示為「非垃圾郵件」，並建立「永不標示為垃圾郵件」的篩選器。

## 8. GitHub Secrets

Repository → Settings → Secrets and variables → Actions → New repository secret：

| 名稱 | 內容 |
| --- | --- |
| `FIREBASE_SERVICE_ACCOUNT` | 完整 Service Account JSON |
| `GMAIL_SENDER` | Gmail 寄件地址 |
| `GMAIL_APP_PASSWORD` | Gmail App Password |

更新 Secret 不需要修改或重新部署程式；下一次 Action 會讀取新值。

## 9. 發布 Firestore Rules

### 方法 A：GitHub Actions

推送以下任一檔案後會觸發 `Deploy Firestore Rules`：

```text
firestore.rules
firebase.json
.github/workflows/deploy-firestore-rules.yml
```

也可以在 Actions → Deploy Firestore Rules → Run workflow 手動執行。

成功 Log 應包含：

```text
firestore: released rules firestore.rules to cloud.firestore
Deploy complete!
```

### 方法 B：Firebase Console

Firestore Database → Rules，貼上 `firestore.rules` 的內容後按 Publish。

### 方法 C：本機 Firebase CLI

```powershell
npm install -g firebase-tools
firebase login
firebase use fb-event-notifier
firebase deploy --only firestore:rules
```

## 10. 發布管理網站

Repository → Settings → Pages：

```text
Source: Deploy from a branch
Branch: main
Folder: /docs
```

目前網址：

```text
https://cy872024.github.io/FB_SCRAPER/
```

推送 `docs/index.html` 後會觸發 `pages-build-deployment`。部署完成後使用 `Ctrl + Shift + R` 強制重新載入。

若頁面顯示：

```text
Missing or insufficient permissions
```

代表線上的 Firestore Rules 尚未發布、登入 UID 與文件 ID 不符，或 Rules 不允許該帳號；這不是 GitHub Pages 快取問題。

## 11. 爬蟲 Action 與排程

`.github/workflows/scraper.yml` 目前設定：

```yaml
schedule:
  - cron: '*/15 * * * *'
```

GitHub 每 15 分鐘喚醒一次，但真正是否執行任務仍由 `fb_scraper.py` 依台北時間與 Firestore 任務排程判斷。

排程類型：

- `interval`：距上次執行達指定分鐘數時執行，容許 5 分鐘誤差。
- `daily`：到達指定時間且當天尚未執行。
- `weekly`：到達指定時間、星期符合且當天尚未執行。
- `monthly`：到達指定時間、日期符合且當天尚未執行。

GitHub cron 使用 UTC，但應用程式內部使用 Asia/Taipei。外層 workflow 只是每 15 分鐘喚醒，因此使用者設定的時間仍以台灣時間解讀。

GitHub 的 scheduled workflow 可能延遲。公開 Repository 若 60 天沒有活動，GitHub 也可能自動停用定時 workflow；此時到 Actions 重新啟用，或提交 cron 變更。

## 12. 手動測試

Actions → FB Scraper SaaS Engine → Run workflow：

- `run_all = false`：依 Firestore 排程判斷。
- `run_all = true`：忽略時間，強制執行所有使用者的所有任務。

成功執行至少應看到：

```text
從資料庫撈取到 N 筆會員任務設定
本次將執行 N 個任務
抓取完畢，共 N 篇新貼文
發現 N 篇相關貼文
已寄出給 N 位收件人
已更新至 Firebase 資料庫
```

綠色 Action 只表示 Python 沒有以非零狀態結束，不保證一定抓到貼文或最終送達收件匣，仍應閱讀 Log。

## 13. 本機執行

建議使用 Python 3.10 或相容版本。

```powershell
python -m pip install -r requirements.txt
python -m pip install firebase-admin
python -m playwright install chromium
```

設定環境變數後執行：

```powershell
$env:FIREBASE_SERVICE_ACCOUNT = Get-Content -Raw .\service-account.json
$env:GMAIL_SENDER = "your-account@gmail.com"
$env:GMAIL_APP_PASSWORD = "新的應用程式密碼"

python .\fb_scraper.py --github-cron
```

強制執行所有任務：

```powershell
python .\fb_scraper.py --test
```

`service-account.json` 不應放入 Repository；用完後應清除目前終端的敏感環境變數。

## 14. 執行邏輯與限制

1. 首次執行預設只回溯最近 1,440 分鐘。
2. 後續回溯窗口是距離 `last_run_time` 的分鐘數。
3. URL 會移除 query string 後去重。
4. 只處理未出現在 `seen_urls` 的網址。
5. 貼文文字只要包含任一關鍵字即符合，不分英文大小寫。
6. 每個粉專最多捲動五輪。
7. 目前日期解析主要支援「分鐘、小時、天、昨天」及英文相對時間。
8. Facebook 完整日期、置頂文、登入牆或 DOM 變更可能導致漏文。
9. 現行程式會在掃描結束後更新 `seen_urls`；即使寄信失敗，也可能不再重寄同一網址。
10. Gmail SMTP 錯誤目前只記錄於 Log，不會讓整個 Action 自動變成失敗。

第 9、10 點是目前實作限制，維運時必須查看 Action Log，不能只看綠色勾勾。

## 15. 常見問題

### 網頁一直顯示「載入中」

確認已部署包含錯誤處理的最新版 `docs/index.html`，再開啟瀏覽器 Console。若是權限錯誤，發布 `firestore.rules`。

### 網頁顯示 `Missing or insufficient permissions`

1. 確認 `Deploy Firestore Rules` 成功。
2. Firebase Authentication 中的 UID 必須等於 Firestore `users/{uid}` 文件 ID。
3. 確認登入帳號及 Rules 中的管理員 Email。

### Rules Action 出現 403

替 Service Account 加入 `Service Usage Viewer` 與 `Firebase Rules Admin`，然後重新執行 Action。更改個人 Google 密碼不會解決 IAM 403。

### Action 成功但沒有 Email

1. Log 必須出現「發現 N 篇相關貼文」。
2. 確認 Firestore 任務中的 `recipients`，不是 `config.json`。
3. 檢查垃圾郵件、所有郵件與寄件備份。
4. Google 帳號改密碼後，更新 `GMAIL_APP_PASSWORD`。

### 粉專有關鍵字但沒有被列出

1. 確認該粉專存在於 Firestore 任務的 `pages`。
2. 確認關鍵字是陣列且無錯字。
3. 查看 Log 是否找到候選貼文。
4. 檢查網址是否已在 `states/{uid}` 的 `seen_urls`。
5. Facebook 可能改變 DOM、顯示登入牆或使用程式尚未支援的日期格式。

### 想重新測試已看過的貼文

可在 Firestore 的 `states/{uid}` 中刪除該任務的 `last_run_time` 與 `seen_urls`。這會造成近期文章重新寄送，測試時應只保留自己的收件地址。

## 16. 安全與維運建議

- 不要把 Service Account JSON、Gmail App Password 或使用者密碼提交到 Git。
- 定期輪替 Service Account Key；建立新 Key、更新 Secret、驗證成功後再停用舊 Key。
- 使用最小 IAM 權限，不要為了排除 403 直接授予 Owner。
- 不要使用公開 Firestore 規則。
- Facebook 抓取可能受其服務條款與技術限制約束；上線前應確認使用情境與合規要求。
- 定期查看 Actions，而不是只依賴 Email 判斷系統健康狀態。
- 修改 Facebook 擷取邏輯後，先以 `run_all` 手動測試少量粉專。
