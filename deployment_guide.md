# FB Scraper SaaS 完整佈署與設定指南

本文件記錄了將本機爬蟲程式轉化為雲端 SaaS 平台的所有相關流程與設定。無論您未來是要重建專案、轉移主機，或是交接給其他開發者，請依循以下步驟進行設定。

---

## 壹、Google 寄信帳號設定 (Gmail)

系統後端寄發通知信時，需使用 Google 的「應用程式密碼」來通過 SMTP 驗證。

1. **登入 Google 帳號**：進入 [Google 帳戶安全性設定](https://myaccount.google.com/security)。
2. **開啟兩步驟驗證**：確保帳號已開啟「兩步驟驗證」。
3. **產生應用程式密碼**：
   * 在搜尋框搜尋「應用程式密碼 (App Passwords)」。
   * 應用程式名稱可隨意填寫（例如：`FB-Scraper-Bot`）。
   * 系統會產生一組 **16 位數的密碼**。請將此密碼複製保存備用（關閉視窗後將無法再次查看）。

---

## 貳、Firebase 專案設定 (資料庫與會員系統)

Firebase 負責處理前端網頁的會員登入 (Authentication) 以及任務資料的儲存 (Firestore)。

### 1. 建立專案與應用程式
1. 進入 [Firebase Console](https://console.firebase.google.com/)，點擊「新增專案」。
2. 輸入專案名稱（例如：`fb-event-notifier`），並可依需求決定是否開啟 Google Analytics。
3. 專案建立後，點擊左側選單上方的 `</>` (Web) 圖示，新增一個 Web 應用程式。
4. 註冊應用程式後，系統會提供一段 `firebaseConfig` 程式碼（包含 apiKey 等資訊）。請將這段設定貼到 `docs/index.html` 的對應位置。

### 2. Authentication (會員驗證)
1. 點擊左側選單 **Build -> Authentication**，點擊「Get Started」。
2. 切換到 **Sign-in method (登入方式)** 頁籤。
3. 啟用以下供應商：
   * **電子郵件/密碼**：直接啟用即可。
   * **Google**：啟用後，需在「專案支援電子郵件」下拉選單中選擇您的信箱。
   * **Facebook**：(選用) 需前往 Facebook 開發者後台申請 App ID 與 App Secret 填入。
4. 切換到 **Settings -> Authorized domains (授權網域)**，點擊「Add domain」，將您未來的 GitHub Pages 網址（例如 `cy872024.github.io`）加入白名單，否則 Google 登入會被阻擋。

### 3. Firestore Database (雲端資料庫)
1. 點擊左側選單 **Build -> Firestore Database**，點擊「Create database」。
2. 選擇伺服器位置（建議選擇 `asia-east1` 台灣或 `asia-northeast1` 東京）。
3. 初始設定選擇 **Test Mode (測試模式)**，允許前端直接讀寫資料庫。
   *(注意：測試模式通常只有 30 天效期，未來請修改 Security Rules 以確保安全)*。
4. 資料庫內會自動產生以下兩個 Collection (集合)：
   * `users`：存放各會員的排程任務與信箱。
   * `states`：存放爬蟲追蹤狀態（最後執行時間、已抓取過的網址）。

### 4. 取得後端服務帳戶金鑰 (Service Account)
為了讓 GitHub Actions 上的 Python 後端有權限讀取 Firebase 資料庫，我們需要一把超級鑰匙。
1. 點擊左上角 ⚙️ 齒輪 -> **Project settings (專案設定)**。
2. 切換到 **Service accounts (服務帳戶)** 頁籤。
3. 點擊 **Generate new private key (產生新的私密金鑰)**。
4. 系統會下載一個 `.json` 檔案。請用文字編輯器打開它，並將裡面的**所有內容複製下來**備用。

---

## 參、GitHub 專案設定 (自動化與網頁託管)

GitHub 負責存放程式碼、免費託管前端網頁 (GitHub Pages)，以及執行定時爬蟲任務 (GitHub Actions)。

### 1. 建立與推送程式碼
1. 在 GitHub 建立一個 **Public (公開)** 的 Repository。
2. 將本機程式碼 Push 到 GitHub `main` 分支。

### 2. GitHub Secrets (機密變數設定)
請勿將密碼明碼寫在程式碼中。請至 GitHub Repo 的 **Settings -> Secrets and variables -> Actions**，點擊「New repository secret」建立以下三個變數：

| Secret 名稱 | 內容說明 |
| :--- | :--- |
| `GMAIL_SENDER` | 您用來寄信的 Google 信箱（例如：`your-email@gmail.com`）。 |
| `GMAIL_APP_PASSWORD` | 剛剛在步驟「壹」取得的 16 位數 Google 應用程式密碼。 |
| `FIREBASE_SERVICE_ACCOUNT` | 剛剛在步驟「貳-4」取得並複製的整個 Firebase 金鑰 JSON 內容。 |

### 3. GitHub Actions (定時排程)
1. 程式碼中的 `.github/workflows/scraper.yml` 已設定好排程規則（預設為 `*/15 * * * *` 每 15 分鐘執行一次）。
2. GitHub 伺服器會依照排程自動啟動 Ubuntu 環境，安裝 Python、Playwright、讀取 Secret 並執行 `fb_scraper.py`。
3. 您可以到 Repo 的 **Actions** 頁籤隨時監控每次執行的 Log 與結果。

### 4. GitHub Pages (前端網頁發布)
1. 進入 Repo 的 **Settings -> Pages**。
2. 在 Build and deployment 區塊的 **Source** 選擇 `Deploy from a branch`。
3. 在 **Branch** 下拉選單選擇 `main`，資料夾選擇 `/docs`。
4. 點擊 Save。等待約 1~2 分鐘後，您的 Web App 就會正式發布在 `https://[您的帳號].github.io/[您的專案名]/` 上。

---

> [!NOTE]
> **日常維護提醒**
> * 若要新增或修改管理員權限，請修改 `docs/index.html` 中的 `ADMIN_EMAILS` 陣列。
> * 由於使用 GitHub Actions 免費資源進行頻繁爬蟲，若遇臉書改版或 GitHub 政策改變，可能需要維護 Python 的抓取邏輯或轉移主機。
