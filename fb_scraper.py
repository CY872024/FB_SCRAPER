import io, json, os, re, sys, csv, asyncio, smtplib, subprocess, argparse, time
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timedelta
from dataclasses import dataclass
from pathlib import Path

# 強制終端機輸出為 UTF-8，避免 Windows 上的 cp950 編碼錯誤
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

SCRIPT_DIR = Path(__file__).parent
CONFIG_FILE = SCRIPT_DIR / "config.json"
STATE_FILE = SCRIPT_DIR / "last_run.json"

MAX_SCROLL_COUNT = 5

# ─── 設定檔與狀態 ────────────────────────────────────────────────────────────────

def load_config():
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def load_state():
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                state = json.load(f)
                if "last_run_time" in state: # 舊版格式，直接放棄並初始化
                    return {}
                return state
        except:
            pass
    return {}

def get_task_state(state, task_name):
    return state.get(task_name, {})

def save_task_state(state, task_name, run_time, seen_urls):
    state[task_name] = {
        "last_run_time": run_time.isoformat(),
        "seen_urls": list(seen_urls)
    }
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=4)

def load_email_config():
    config = load_config()
    ec = config.get("email", {})
    if not ec.get("enabled"):
        return None
    
    # 優先從環境變數讀取密碼 (GitHub Actions Secrets)
    env_pwd = os.environ.get("GMAIL_APP_PASSWORD")
    if env_pwd:
        ec["app_password"] = env_pwd
        
    if not ec.get("app_password") or ec.get("app_password") == "請填入 Gmail 應用程式密碼":
        return None
    return ec

def get_minutes_since_last_run(task_state):
    lrt = task_state.get("last_run_time")
    if not lrt:
        return None
    try:
        dt = datetime.fromisoformat(lrt)
        return int((datetime.now() - dt).total_seconds() / 60)
    except:
        return None

# ─── 工具函式 ────────────────────────────────────────────────────────────────────

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

# ─── 階段一：抓取貼文 → 存 CSV ────────────────────────────────────────────────

async def scrape_phase(task_name: str):
    from playwright.async_api import async_playwright

    config = load_config()
    tasks = config.get("tasks", [])
    task = next((t for t in tasks if t.get("task_name") == task_name), None)
    
    if not task:
        print(f"[!] 找不到任務: {task_name}")
        sys.exit(1)

    pages = [(p.get("name"), p.get("url")) for p in task.get("pages", []) if p.get("name") and p.get("url")]
    keywords_list = task.get("keywords", [])

    now = datetime.now()
    state = load_state()
    task_state = get_task_state(state, task_name)
    mins = get_minutes_since_last_run(task_state)
    prev_seen = set(normalize_url(u) for u in task_state.get("seen_urls", []))
    window = mins if mins is not None else 1440

    print("=" * 58)
    print(f"     Facebook 任務: {task_name}")
    print(f"     執行時間: {now.strftime('%Y-%m-%d %H:%M')}")
    print(f"     回溯窗口: {window} 分鐘" + (" (首次執行)" if mins is None else ""))
    print(f"     已知貼文: {len(prev_seen)} 篇 / 粉絲專頁: {len(pages)} 個")
    print("=" * 58)

    all_posts = []
    all_urls = set()
    pw = await async_playwright().start()
    browser = await pw.chromium.launch(headless=True)
    context = await browser.new_context(
        locale="zh-TW", timezone_id="Asia/Taipei",
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
        viewport={"width": 1280, "height": 900},
    )

    for page_name, page_url in pages:
        print(f"\n{'='*60}\n[*] {page_name}\n    {page_url}\n{'='*60}")
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
                    print("    [i] 已關閉登入彈窗")
                else:
                    await page.keyboard.press("Escape")
                await asyncio.sleep(0.5)
                await page.evaluate("""() => {
                    document.querySelectorAll('div[role="dialog"]').forEach(d => d.remove());
                    document.body.style.overflow = 'auto';
                    document.documentElement.style.overflow = 'auto';
                }""")
            except:
                pass

            found_older = False
            empty = 0
            for scroll in range(MAX_SCROLL_COUNT):
                print(f"    [捲動] 第 {scroll+1} 次...")
                
                # 展開「查看更多」按鈕，以獲取完整貼文內容
                try:
                    await page.evaluate("""() => {
                        document.querySelectorAll('div[role="button"]').forEach(b => {
                            if (b.innerText && (b.innerText.includes('查看更多') || b.innerText.includes('See more'))) {
                                b.click();
                            }
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
                    if "comment_id" in url or "reply_comment_id" in url:
                        continue
                    url = normalize_url(url)
                    if url in local_seen or url in all_urls:
                        continue
                    local_seen.add(url)
                    all_urls.add(url)

                    if url in prev_seen:
                        continue

                    if is_within_window(ts, window):
                        print(f"    [V] 新貼文 ({ts})")
                        all_posts.append({"page": page_name, "time": ts, "url": url, "text": text})
                        nc += 1
                    elif is_older_than_window(ts, window):
                        print(f"    [>] 超出窗口 ({ts})")
                        found_older = True

                if found_older:
                    print("    [停止] 已到達較舊貼文")
                    break
                
                if nc == 0:
                    empty += 1
                    if empty >= 2:
                        print("    [停止] 無新貼文")
                        break

                await page.keyboard.press("PageDown")
                await page.keyboard.press("PageDown")
                await asyncio.sleep(2)

        except Exception as e:
            print(f"    [!] 錯誤: {e}")
        finally:
            await page.close()

    print(f"\n[統計] {len(pages)} 個粉專 → {len(all_posts)} 篇新貼文")

    # 寫入 CSV
    csv_file = SCRIPT_DIR / f"fb_posts_{now.strftime('%Y%m%d_%H%M')}.csv"
    with open(csv_file, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["粉專名稱", "標題", "摘要", "時間", "貼文網址", "活動相關"])
        for p in all_posts:
            title = p["text"].replace("\n"," ")[:50]
            summary = p["text"].replace("\n"," ")[:150]
            is_event = "是" if matches_keywords(p["text"], keywords_list) else "否"
            w.writerow([p["page"], title, summary, p["time"], p["url"], is_event])

    print(f"[儲存] {csv_file}")
    
    save_task_state(state, task_name, now, all_urls.union(prev_seen))
    print(f"[狀態] 已更新 {STATE_FILE}")

    # 輸出 CSV 檔名供主程序讀取
    print(f"__CSV__:{csv_file}")

    sys.exit(0)


# ─── 階段二：讀 CSV → 寄 Email ────────────────────────────────────────────────

def notify_phase(csv_path: str, task: dict):
    if not Path(csv_path).exists():
        return

    ec = load_email_config()
    if not ec:
        print("[寄信] Email 未啟用或未設定")
        return

    task_name = task.get("task_name", "未知任務")
    recipients = task.get("recipients", ec.get("recipients", []))

    if not recipients:
        print("[寄信] 未設定收件人")
        return

    rows = []
    event_rows = []
    with open(csv_path, "r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append(row)
            if row.get("活動相關") == "是":
                event_rows.append(row)

    print(f"\n[寄信] 共 {len(rows)} 篇貼文，{len(event_rows)} 篇活動相關")

    if not event_rows:
        print("[寄信] 無活動報名貼文，本次不寄信")
        return

    # 組 HTML
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    html = f"<h2>[{task_name}] FB 活動報名貼文通知</h2><p>執行時間: {now_str}</p>"
    html += f"<p>共 <b>{len(rows)}</b> 篇新貼文，<b>{len(event_rows)}</b> 篇活動相關</p>"
    
    html += '<table border="1" style="border-collapse: collapse; width: 100%; font-family: sans-serif;">'
    html += '<tr style="background-color: #f2f2f2;"><th>粉專</th><th>標題</th><th>時間</th><th>連結</th></tr>'
    
    for r in event_rows:
        html += f"<tr><td>{r['粉專名稱']}</td><td>{r['標題']}</td><td>{r['時間']}</td><td><a href='{r['貼文網址']}'>點此查看</a></td></tr>"
    html += "</table><br><br><small>此為系統自動發送，請勿直接回覆。</small>"

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"[{task_name}] Facebook 新活動貼文通知 ({len(event_rows)}篇)"
    msg["From"] = ec["sender"]
    msg["To"] = ", ".join(recipients)
    msg.attach(MIMEText(html, "html", "utf-8"))

    try:
        server = smtplib.SMTP(ec["smtp_host"], ec["smtp_port"])
        server.starttls()
        server.login(ec["sender"], ec["app_password"])
        server.send_message(msg)
        server.quit()
        print(f"[寄信] 已寄出至: {', '.join(recipients)}")
    except Exception as e:
        print(f"[!] 寄信失敗: {e}")


# ─── 主程式協調器 ────────────────────────────────────────────────────────────

def run_once(task):
    task_name = task.get("task_name")
    print(f"\n[啟動] 執行任務: {task_name}")
    
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    
    result = subprocess.run(
        [sys.executable, "-u", __file__, "--scrape", "--task", task_name],
        capture_output=True, text=True, encoding="utf-8", errors="replace", env=env
    )

    if result.stdout:
        print(result.stdout, end="")
    if result.stderr:
        print(result.stderr, end="", file=sys.stderr)

    csv_path = None
    for line in (result.stdout or "").splitlines():
        if line.startswith("__CSV__:"):
            csv_path = line.split(":", 1)[1].strip()
            break

    if not csv_path or not Path(csv_path).exists():
        print(f"[!] 未找到 CSV 檔案，跳過寄信 ({task_name})")
        return

    print(f"\n[階段二] 處理通知 ({task_name})...")
    notify_phase(csv_path, task)
    print(f"\n[完成] 任務 {task_name} 執行結束!")


def run_local_scheduler():
    print("==========================================================")
    print("     Facebook 爬蟲多任務排程器 (Local 模式)")
    print("==========================================================")
    print("[提示] 程式會掛在背景，時間到自動執行。按 Ctrl+C 停止。\n")
    
    while True:
        try:
            now = datetime.now()
            current_day = now.weekday() # 0 = Monday, 6 = Sunday
            current_time_str = now.strftime("%H:%M")
            
            tasks = load_config().get("tasks", [])
            state = load_state()
            
            for task in tasks:
                schedule = task.get("schedule", {})
                days = schedule.get("days_of_week", [])
                time_str = schedule.get("time", "")
                
                if current_day in days and current_time_str == time_str:
                    task_name = task.get("task_name")
                    task_state = get_task_state(state, task_name)
                    last_run = task_state.get("last_run_time", "")
                    
                    # 避免同一分鐘內重複執行
                    if last_run.startswith(now.strftime("%Y-%m-%dT%H:%M")):
                        continue
                        
                    print(f"\n>>> [時間到達] 準備執行: {task_name} ({time_str})")
                    run_once(task)
                    state = load_state() # 重新讀取狀態
                    
            time.sleep(10) # 每 10 秒檢查一次
            
        except KeyboardInterrupt:
            print("\n[排程器] 已停止")
            break
        except Exception as e:
            print(f"\n[!] 排程器發生錯誤: {e}")
            time.sleep(60)

def run_github_cron():
    print("==========================================================")
    print("     Facebook 爬蟲 (GitHub Actions 雲端模式)")
    print("==========================================================")
    now = datetime.now()
    current_day = now.weekday()
    current_hour = now.hour
    
    tasks = load_config().get("tasks", [])
    state = load_state()
    
    triggered_any = False
    for task in tasks:
        schedule = task.get("schedule", {})
        days = schedule.get("days_of_week", [])
        time_str = schedule.get("time", "")
        
        try:
            task_hour = int(time_str.split(":")[0])
        except:
            continue
            
        # GitHub Actions 啟動時間可能會有幾分鐘誤差，因此比對「小時」即可
        if current_day in days and current_hour == task_hour:
            task_name = task.get("task_name")
            task_state = get_task_state(state, task_name)
            last_run = task_state.get("last_run_time", "")
            
            current_hour_prefix = now.strftime("%Y-%m-%dT%H")
            if last_run.startswith(current_hour_prefix):
                print(f"[跳過] {task_name} 本小時已執行過。")
                continue
                
            print(f"\n>>> [時間到達] 準備執行: {task_name} (設定時間 {time_str})")
            run_once(task)
            state = load_state()
            triggered_any = True
            
    if not triggered_any:
        print("[資訊] 本次喚醒無任何任務需要執行。")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--scrape", action="store_true")
    parser.add_argument("--task", type=str)
    parser.add_argument("--test", action="store_true", help="強制執行所有任務一次")
    parser.add_argument("--github-cron", action="store_true", help="GitHub Actions 排程模式")
    args = parser.parse_args()
    
    if args.scrape and args.task:
        # 子程序模式：只做抓取，結束時 sys.exit(0)
        asyncio.run(scrape_phase(args.task))
    elif args.test:
        print("[手動模式] 強制執行所有任務一次")
        tasks = load_config().get("tasks", [])
        for task in tasks:
            run_once(task)
    elif args.github_cron:
        run_github_cron()
    else:
        # 排程器模式
        run_local_scheduler()
