import os, re, sys, csv, asyncio, smtplib, argparse, time, json
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime
from pathlib import Path

# 強制終端機輸出為 UTF-8
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

SCRIPT_DIR = Path(__file__).parent
MAX_SCROLL_COUNT = 5

# ==========================================
# Firebase 相關設定
# ==========================================
import firebase_admin
from firebase_admin import credentials, firestore

def init_firebase():
    # 從環境變數讀取 Firebase Service Account JSON 字串
    sa_json = os.environ.get("FIREBASE_SERVICE_ACCOUNT")
    if not sa_json:
        print("[!] 找不到 FIREBASE_SERVICE_ACCOUNT 環境變數，無法連線資料庫！")
        sys.exit(1)
        
    cred_dict = json.loads(sa_json)
    cred = credentials.Certificate(cred_dict)
    firebase_admin.initialize_app(cred)
    return firestore.client()

db = None # 延遲初始化

def get_all_users_tasks():
    users_ref = db.collection('users')
    docs = users_ref.stream()
    all_tasks = []
    for doc in docs:
        data = doc.to_dict()
        uid = doc.id
        tasks = data.get('tasks', [])
        for t in tasks:
            t['uid'] = uid # 綁定是哪個會員的任務
            all_tasks.append(t)
    return all_tasks

def get_task_state(uid, task_name):
    doc_ref = db.collection('states').document(uid)
    doc = doc_ref.get()
    if doc.exists():
        return doc.to_dict().get(task_name, {})
    return {}

def save_task_state(uid, task_name, run_time, seen_urls):
    doc_ref = db.collection('states').document(uid)
    doc_ref.set({
        task_name: {
            "last_run_time": run_time.isoformat(),
            "seen_urls": list(seen_urls)
        }
    }, merge=True)

# ==========================================
# Email 設定
# ==========================================
def load_email_config():
    sender = os.environ.get("GMAIL_SENDER", "lcy872024@gmail.com")
    pwd = os.environ.get("GMAIL_APP_PASSWORD")
    if not pwd:
        return None
    return {
        "smtp_host": "smtp.gmail.com",
        "smtp_port": 587,
        "sender": sender,
        "app_password": pwd
    }

# ==========================================
# 工具函式
# ==========================================
def _parse_minutes_ago(text):
    if not text: return None
    tl = text.strip().lower()
    for pat, mul in [(r"^(\d+)\s*分鐘", 1), (r"^(\d+)\s*小時", 60), (r"^(\d+)\s*天", 1440)]:
        m = re.match(pat, text)
        if m: return int(m.group(1)) * mul
    for pat, mul in [(r"^(\d+)\s*m$", 1), (r"^(\d+)\s*(min|mins)$", 1), (r"^(\d+)\s*h$", 60), (r"^(\d+)\s*(hr|hrs)$", 60), (r"^(\d+)\s*d$", 1440)]:
        m = re.match(pat, tl)
        if m: return int(m.group(1)) * mul
    if "昨天" in text or "yesterday" in tl:
        return 1440
    return None

def is_within_window(time_text, window_minutes):
    m = _parse_minutes_ago(time_text)
    return m is not None and m <= window_minutes + 60

def is_older_than_window(time_text, window_minutes):
    m = _parse_minutes_ago(time_text)
    if m is not None:
        return m > window_minutes + 120
    tl = time_text.strip().lower()
    if re.search(r"(january|february|march|april|may|june|july|august|september|october|november|december)", tl):
        return True
    if re.search(r"\d+月\d+日", time_text):
        return True
    return True

def normalize_url(url):
    return url.split("?")[0] if "?" in url else url

def matches_keywords(text, keywords_list):
    tl = text.lower()
    return any(kw.lower() in tl for kw in keywords_list)

def get_minutes_since_last_run(task_state):
    lrt = task_state.get("last_run_time")
    if not lrt:
        return None
    try:
        dt = datetime.fromisoformat(lrt)
        return int((datetime.now() - dt).total_seconds() / 60)
    except:
        return None

# ==========================================
# 爬蟲引擎 (單一任務)
# ==========================================
async def scrape_task(task: dict):
    from playwright.async_api import async_playwright

    uid = task.get("uid")
    task_name = task.get("task_name", "未知任務")
    pages = [(p.get("name"), p.get("url")) for p in task.get("pages", []) if p.get("name") and p.get("url")]
    keywords_list = task.get("keywords", [])
    recipients = task.get("recipients", [])

    if not pages:
        print(f"[-] 任務 {task_name} 沒有設定粉專，跳過。")
        return

    now = datetime.now()
    task_state = get_task_state(uid, task_name)
    mins = get_minutes_since_last_run(task_state)
    prev_seen = set(normalize_url(u) for u in task_state.get("seen_urls", []))
    window = mins if mins is not None else 1440

    print("=" * 58)
    print(f"     執行任務: [{uid[:6]}...] {task_name}")
    print(f"     回溯窗口: {window} 分鐘" + (" (首次執行)" if mins is None else ""))
    print("=" * 58)

    all_posts = []
    all_urls = set()
    pw = await async_playwright().start()
    browser = await pw.chromium.launch(headless=True)
    context = await browser.new_context(
        locale="zh-TW", timezone_id="Asia/Taipei",
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/125.0.0.0 Safari/537.36",
        viewport={"width": 1280, "height": 900},
    )

    for page_name, page_url in pages:
        print(f"\n[*] {page_name}")
        page = await context.new_page()
        local_seen = set()

        try:
            await page.goto(page_url, wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(2)

            # 關閉登入彈窗
            try:
                cb = await page.query_selector("div[role='dialog'] div[aria-label='Close'], div[role='dialog'] div[aria-label='關閉']")
                if cb:
                    await cb.click()
                else:
                    await page.keyboard.press("Escape")
                await asyncio.sleep(0.5)
                await page.evaluate("document.querySelectorAll('div[role=\"dialog\"]').forEach(d => d.remove());")
            except:
                pass

            found_older = False
            empty = 0
            for scroll in range(MAX_SCROLL_COUNT):
                print(f"    [捲動] 第 {scroll+1} 次...")
                
                try:
                    await page.evaluate("""() => {
                        document.querySelectorAll('div[role="button"]').forEach(b => {
                            if (b.innerText && (b.innerText.includes('查看更多') || b.innerText.includes('See more'))) b.click();
                        });
                    }""")
                    await asyncio.sleep(1.5)
                except:
                    pass

                raw = await page.evaluate("""() => {
                    const r = [];
                    for (const link of document.querySelectorAll('a[href*="/posts/"], a[href*="/permalink/"], a[href*="/pfbid"]')) {
                        const href = link.href || '', lt = (link.innerText||'').trim();
                        if (lt.length > 0 && lt.length < 30) {
                            let c = link.closest('div[role="article"]');
                            if (!c) { let p = link.parentElement; for (let i=0;i<10&&p;i++) { if (p.querySelectorAll('a[href*="/posts/"],a[href*="/pfbid"]').length>=1 && (p.innerText||'').length>50) { c=p; break; } p=p.parentElement; } }
                            if (c) { const t=(c.innerText||'').trim(); if(t.length>20) r.push({ts:lt,url:href,text:t.substring(0,500)}); }
                        }
                    }
                    const s = new Set(); return r.filter(x => { if(s.has(x.url)) return false; s.add(x.url); return true; });
                }""")

                nc = 0
                for item in raw:
                    url, ts, text = item["url"], item["ts"], item["text"]
                    if "comment_id" in url or "reply_comment_id" in url: continue
                    url = normalize_url(url)
                    if url in local_seen or url in all_urls: continue
                    local_seen.add(url)
                    all_urls.add(url)

                    if url in prev_seen: continue

                    if is_within_window(ts, window):
                        print(f"    [V] 新貼文 ({ts})")
                        all_posts.append({"page": page_name, "time": ts, "url": url, "text": text})
                        nc += 1
                    elif is_older_than_window(ts, window):
                        found_older = True

                if found_older: break
                if nc == 0:
                    empty += 1
                    if empty >= 2: break

                await page.keyboard.press("PageDown")
                await page.keyboard.press("PageDown")
                await asyncio.sleep(2)

        except Exception as e:
            print(f"    [!] 錯誤: {e}")
        finally:
            await page.close()

    await browser.close()
    await pw.stop()

    print(f"\n[統計] 抓取完畢，共 {len(all_posts)} 篇新貼文")

    # 過濾關鍵字並寄信
    event_posts = []
    for p in all_posts:
        if matches_keywords(p["text"], keywords_list):
            event_posts.append(p)

    if event_posts and recipients:
        print(f"[寄信] 發現 {len(event_posts)} 篇相關貼文，準備寄信...")
        send_email_notification(task_name, recipients, event_posts)
    else:
        print("[寄信] 無符合條件之貼文，不寄信。")

    # 儲存狀態回 Firebase
    save_task_state(uid, task_name, now, all_urls.union(prev_seen))
    print("[狀態] 已更新至 Firebase 資料庫")


def send_email_notification(task_name, recipients, posts):
    ec = load_email_config()
    if not ec:
        print("[!] 找不到寄信密碼 (GMAIL_APP_PASSWORD 環境變數)，無法寄信！")
        return

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    html = f"<h2>[{task_name}] FB 活動報名貼文通知</h2><p>系統執行時間: {now_str}</p>"
    html += f"<p>本次共發現 <b>{len(posts)}</b> 篇符合您關鍵字的貼文：</p>"
    
    html += '<table border="1" style="border-collapse: collapse; width: 100%; font-family: sans-serif;">'
    html += '<tr style="background-color: #f2f2f2;"><th>粉專</th><th>貼文時間</th><th>內容摘要</th><th>連結</th></tr>'
    
    for p in posts:
        summary = p['text'].replace('\n', ' ')[:80] + "..."
        html += f"<tr><td>{p['page']}</td><td>{p['time']}</td><td>{summary}</td><td><a href='{p['url']}'>點此查看</a></td></tr>"
    html += "</table><br><br><small>此為 SaaS 系統自動發送，請勿直接回覆。</small>"

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"[{task_name}] Facebook 新貼文通知 ({len(posts)}篇)"
    msg["From"] = f"FB Scraper SaaS <{ec['sender']}>"
    msg["To"] = ", ".join(recipients)
    msg.attach(MIMEText(html, "html", "utf-8"))

    try:
        server = smtplib.SMTP(ec["smtp_host"], ec["smtp_port"])
        server.starttls()
        server.login(ec["sender"], ec["app_password"])
        server.send_message(msg)
        server.quit()
        print(f"[成功] 已寄出給 {len(recipients)} 位收件人")
    except Exception as e:
        print(f"[!] 寄信失敗: {e}")

# ==========================================
# GitHub Actions 排程器
# ==========================================
def run_github_cron(force_all=False):
    print("==========================================================")
    print("     Facebook Scraper SaaS (Firebase 雲端模式)")
    print("==========================================================")
    
    global db
    db = init_firebase()
    
    now = datetime.now()
    current_day = now.weekday()
    current_hour = now.hour
    
    all_tasks = get_all_users_tasks()
    print(f"[*] 從資料庫撈取到 {len(all_tasks)} 筆會員任務設定。")
    
    tasks_to_run = []
    
    for task in all_tasks:
        if force_all:
            tasks_to_run.append(task)
            continue
            
        schedule = task.get("schedule", {})
        days = schedule.get("days_of_week", [])
        time_str = schedule.get("time", "")
        
        try:
            task_hour = int(time_str.split(":")[0])
        except:
            continue
            
        if current_day in days and current_hour == task_hour:
            uid = task.get("uid")
            task_name = task.get("task_name")
            task_state = get_task_state(uid, task_name)
            last_run = task_state.get("last_run_time", "")
            
            # 如果今天同一個小時已經跑過，防呆跳過
            current_hour_prefix = now.strftime("%Y-%m-%dT%H")
            if last_run.startswith(current_hour_prefix):
                continue
                
            tasks_to_run.append(task)

    if not tasks_to_run:
        print("[資訊] 本次喚醒無任何任務達到執行時間。")
        return

    print(f"[啟動] 本次將執行 {len(tasks_to_run)} 個任務...")
    
    # 依序執行（若需平行處理可改用 asyncio.gather）
    for task in tasks_to_run:
        asyncio.run(scrape_task(task))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--test", action="store_true", help="強制執行資料庫中「所有」會員的任務一次")
    parser.add_argument("--github-cron", action="store_true", help="依據資料庫排程執行")
    args = parser.parse_args()
    
    if args.test:
        run_github_cron(force_all=True)
    else:
        run_github_cron(force_all=False)
