#!/usr/bin/env python3
"""JKK Availability Watcher — runs every 5 min via GitHub Actions"""

import json, os, re, smtplib
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import requests
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

load_dotenv()

START_URL   = "https://jhomes.to-kousya.or.jp/search/jkknet/service/akiyaJyoukenStartInit"
RESULTS_URL = "https://jhomes.to-kousya.or.jp/search/jkknet/service/akiyaJyoukenRef"
MYPAGE_URL  = "https://jhomes.to-kousya.or.jp/search/jkknet/service/mypageMenu"

WHITELIST_FILE = Path("jkk_whitelist.json")
SEEN_FILE      = Path("jkk_seen.json")
AUTOAPPLY_FILE = Path("jkk_autoapply.json")

MAX_RENT_YEN   = 160000
ALLOWED_MADORI = []

GMAIL_ADDRESS      = os.environ.get("GMAIL_ADDRESS", "").strip()
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "").strip()
NOTIFY_EMAIL       = os.environ.get("NOTIFY_EMAIL", "").strip()
LINE_CHANNEL_TOKEN = "".join(os.environ.get("LINE_CHANNEL_TOKEN", "").split())
LINE_USER_ID       = os.environ.get("LINE_USER_ID", "").strip()
JKK_ID             = os.environ.get("JKK_ID", "").strip()
JKK_PASSWORD       = os.environ.get("JKK_PASSWORD", "").strip()
JKK_DRY_RUN        = os.environ.get("JKK_DRY_RUN", "").lower() == "true"
JKK_TEST_PROPERTY  = os.environ.get("JKK_TEST_PROPERTY", "").strip()
LINE_USER_ID       = os.environ.get("LINE_USER_ID", "").strip()

# ─── Ward tiers ───────────────────────────────────────────────────────────────

WARD_TIERS = {
    "渋谷区": 1, "港区": 1, "目黒区": 1, "世田谷区": 1,
    "新宿区": 1, "千代田区": 1, "文京区": 1,
    "品川区": 2, "杉並区": 2, "中野区": 2, "豊島区": 2,
    "中央区": 2, "台東区": 2, "江東区": 2, "墨田区": 2, "大田区": 2,
    "板橋区": 3, "練馬区": 3, "北区": 3,
    "荒川区": 3, "足立区": 3, "葛飾区": 3, "江戸川区": 3,
}
WARD_BADGE = {1: "🟢", 2: "🟡", 3: "🔴", 0: "⚫"}
WARD_COLOR = {1: "#27ae60", 2: "#f39c12", 3: "#e74c3c", 0: "#7f8c8d"}

def get_ward_info(text: str) -> tuple:
    for ward, tier in WARD_TIERS.items():
        if ward in text:
            return WARD_BADGE[tier], ward, WARD_COLOR[tier], tier
    return WARD_BADGE[0], text.split()[0] if text else "不明", WARD_COLOR[0], 0

# ─── Helpers ──────────────────────────────────────────────────────────────────

FULLWIDTH = str.maketrans(
    '０１２３４５６７８９ＡＢＣＤＥＦＧＨＩＪＫＬＭＮＯＰＱＲＳＴＵＶＷＸＹＺ'
    'ａｂｃｄｅｆｇｈｉｊｋｌｍｎｏｐｑｒｓｔｕｖｗｘｙｚ　＋',
    '0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ'
    'abcdefghijklmnopqrstuvwxyz +'
)
def normalize(text): return text.translate(FULLWIDTH).strip()
def parse_rent(s):
    nums = re.findall(r'\d+', s.replace(',', ''))
    return int(nums[0]) if nums else 0
def safe_int(v, default=999):
    try: return int(v)
    except: return default
def is_allowed_madori(m):
    if not ALLOWED_MADORI: return True
    return any(a in normalize(m).upper() for a in ALLOWED_MADORI)
def make_id(p):
    return f"jkk_{p['name']}_{p['madori']}_{p['rent']}".replace(' ', '')

def get_stars(wl: dict) -> tuple:
    """Return (stars_str, reason_str)."""
    walk_nums = re.findall(r'\d+', wl.get('walk_1', '999'))
    walk = int(walk_nums[0]) if walk_nums else 999
    shibuya  = safe_int(wl.get('shibuya',  999))
    shinjuku = safe_int(wl.get('shinjuku', 999))
    best = min(shibuya, shinjuku)
    yr_m = re.search(r'(\d{4})年', wl.get('built', ''))
    year = int(yr_m.group(1)) if yr_m else 0

    if walk <= 15 and best <= 30 and year >= 2010:
        return '⭐⭐⭐', ''
    elif walk <= 15 and best <= 30:
        return '⭐⭐', f'築{year}年' if year else '築年不明'
    else:
        reasons = []
        if walk > 15:  reasons.append(f'徒歩{walk}分')
        if best > 30:  reasons.append(f'通勤{best}分')
        return '⭐', ' / '.join(reasons)

# ─── State ────────────────────────────────────────────────────────────────────

def load_whitelist():
    return json.loads(WHITELIST_FILE.read_text(encoding="utf-8")) if WHITELIST_FILE.exists() else {}
def load_seen():
    return set(json.loads(SEEN_FILE.read_text(encoding="utf-8"))) if SEEN_FILE.exists() else set()
def save_seen(seen):
    SEEN_FILE.write_text(json.dumps(sorted(seen), ensure_ascii=False, indent=2), encoding="utf-8")

# ─── Scraper ──────────────────────────────────────────────────────────────────

JS_EXTRACT = """() => {
    const results = [];
    let t = null;
    for (const tbl of document.querySelectorAll('table'))
        if (tbl.innerText.includes('住宅名') && tbl.innerText.includes('間取り')) { t = tbl; break; }
    if (!t) return results;
    const allRows = [...t.querySelectorAll('tr')];
    let hi = -1;
    for (let i = 0; i < allRows.length; i++)
        if (allRows[i].innerText.includes('住宅名') && allRows[i].innerText.includes('間取り')) { hi = i; break; }
    if (hi < 0) return results;
    for (let i = hi+1; i < allRows.length; i++) {
        const cells = [...allRows[i].querySelectorAll('td')];
        if (cells.length < 8) continue;
        const t = (idx) => (cells[idx]?.innerText||'').trim().replace(/[\\s]+/g,' ');
        const name=t(1), area=t(2), madori=t(5), sqm=t(6), rent=t(7), fee=t(8), units=t(9);
        if (name && madori && rent) results.push({name,area,madori,sqm,rent,fee,units});
    }
    return results;
}"""

JS_POST_FORWARD = """() => {
    const form = document.createElement('form');
    form.method = 'post';
    form.action = 'https://jhomes.to-kousya.or.jp/search/jkknet/service/akiyaJyoukenStartInit';
    [['redirect','true'],
     ['url','https://jhomes.to-kousya.or.jp/search/jkknet/service/akiyaJyoukenStartInit']]
    .forEach(([n,v]) => {
        const i = document.createElement('input');
        i.type='hidden'; i.name=n; i.value=v;
        form.appendChild(i);
    });
    document.body.appendChild(form);
    form.submit();
}"""


def _run_search_flow(page) -> object:
    """POST forwardForm → wait for search button → click 検索する → results page."""
    with page.expect_navigation(wait_until="domcontentloaded", timeout=20_000):
        page.evaluate(JS_POST_FORWARD)
    page.wait_for_selector(
        'a[onclick*="submitPage"], img[alt*="検索"]', timeout=15_000
    )
    with page.expect_navigation(wait_until="domcontentloaded", timeout=30_000):
        page.evaluate("""() => {
            const btn = document.querySelector('a[onclick*="submitPage"]')
                       || document.querySelector('img[alt*="検索"]');
            if (btn) btn.click();
        }""")
    page.wait_for_selector('table', timeout=20_000)
    if "akiyaJyoukenRef" not in page.url:
        raise Exception(f"Search failed — at {page.url}")
    print(f"  Results at: {page.url}")
    return page


def _extract_all_pages(page) -> list[dict]:
    """Extract listings across all pages of the results."""
    props = []
    clicked_50 = page.evaluate("""() => {
        for (const el of document.querySelectorAll('a, input, button, td, span'))
            if ((el.innerText || el.value || '').trim() === '50件') { el.click(); return true; }
        return false;
    }""")
    if clicked_50:
        page.wait_for_selector('table', timeout=10_000)

    page_num = 1
    while True:
        print(f"  Page {page_num}...", end=" ", flush=True)
        rows = page.evaluate(JS_EXTRACT)
        props.extend(rows)
        print(f"{len(rows)} rows (total {len(props)})")
        if clicked_50: break

        next_num = page_num + 1
        has_next = page.evaluate(f"""() => {{
            for (const el of document.querySelectorAll('a'))
                if (el.innerText.trim() === '{next_num}') return true;
            return false;
        }}""")
        if not has_next: break

        try:
            with page.expect_navigation(wait_until="domcontentloaded", timeout=15_000):
                page.evaluate(f"""() => {{
                    for (const el of document.querySelectorAll('a'))
                        if (el.innerText.trim() === '{next_num}') {{ el.click(); return; }}
                }}""")
            page.wait_for_selector('table', timeout=10_000)
        except Exception as e:
            print(f"  Pagination error on page {next_num}: {e}")
            break

        page_num += 1
        if page_num > 20: break
    return props


def scrape_available() -> list[dict]:
    """Non-login scrape — fallback when JKK credentials not configured."""
    props = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=True, args=["--disable-blink-features=AutomationControlled"]
        )
        ctx = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            locale="ja-JP", timezone_id="Asia/Tokyo",
            extra_http_headers={"Accept-Language": "ja,en-US;q=0.9,en;q=0.8"},
        )
        page = ctx.new_page()
        page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
        try:
            page.goto(START_URL, wait_until="domcontentloaded", timeout=60_000)
            _run_search_flow(page)
            props = _extract_all_pages(page)
        except PWTimeout:
            print("  Timeout on JKK site")
        except Exception as e:
            print(f"  Error: {e}")
        finally:
            browser.close()
    return props

# ─── Whitelist matching ───────────────────────────────────────────────────────

def match_whitelist(name, whitelist):
    name = name.strip()
    if name in whitelist: return whitelist[name]
    for wl_name, wl_data in whitelist.items():
        if wl_name in name or name in wl_name: return wl_data
    return None

# ─── Notifications ────────────────────────────────────────────────────────────

def notify_line(matches: list[dict]) -> None:
    if not LINE_CHANNEL_TOKEN or not LINE_USER_ID:
        print("LINE not configured"); return
    msg = f"🏠 JKK: {len(matches)} whitelist listing(s)!\n"
    for m in matches[:5]:
        p, wl = m["prop"], m["whitelist"]
        stars, _ = get_stars(wl)
        wb, ward_name, _, _ = get_ward_info(p["area"])
        rent_total = parse_rent(p["rent"]) + parse_rent(p["fee"])
        s_mins  = wl.get("shibuya","");  s_xf = wl.get("shibuya_transfers","")
        n_mins  = wl.get("shinjuku",""); n_xf = wl.get("shinjuku_transfers","")
        commute = ""
        if str(s_mins) not in ("-",""): commute += f"渋谷{s_mins}分({s_xf}乗換) "
        if str(n_mins) not in ("-",""): commute += f"新宿{n_mins}分({n_xf}乗換)"
        msg += (
            f"\n■ {stars} {wb} {p['name']}\n"
            f"  {normalize(p['madori'])} {p['sqm']}㎡ / ¥{rent_total:,}/月\n"
            f"  🚶 {wl.get('station_1','')} {wl.get('walk_1','')}\n"
            f"  🚃 {commute.strip()}\n"
            f"  🏠 {wl.get('url', START_URL)}\n"
        )
    if len(matches) > 5:
        msg += f"\n...and {len(matches)-5} more — see email"
    r = requests.post(
        "https://api.line.me/v2/bot/message/push",
        headers={"Authorization": f"Bearer {LINE_CHANNEL_TOKEN}", "Content-Type": "application/json"},
        json={"to": LINE_USER_ID, "messages": [{"type": "text", "text": msg}]},
        timeout=10,
    )
    print("LINE:", "✓" if r.status_code == 200 else f"✗ {r.status_code} {r.text}")


def notify_email(matches: list[dict]) -> None:
    if not all([GMAIL_ADDRESS, GMAIL_APP_PASSWORD, NOTIFY_EMAIL]):
        print("Email not configured"); return

    # Sort ⭐⭐⭐ → ⭐⭐ → ⭐
    star_order = {"⭐⭐⭐": 0, "⭐⭐": 1, "⭐": 2}
    matches = sorted(matches, key=lambda m: star_order.get(get_stars(m["whitelist"])[0], 3))

    # ── Individual cards (mobile-first, single column) ─────────────────────────
    cards = ""
    for m in matches:
        p, wl = m["prop"], m["whitelist"]
        stars, reason = get_stars(wl)
        wb, ward_name, wc, tier = get_ward_info(p["area"])
        rent_yen = parse_rent(p["rent"])
        fee_yen  = parse_rent(p["fee"])
        madori   = normalize(p["madori"])

        s_mins = wl.get("shibuya",""); s_xf = wl.get("shibuya_transfers","")
        n_mins = wl.get("shinjuku",""); n_xf = wl.get("shinjuku_transfers","")
        commute_rows = ""
        if str(s_mins) not in ("-",""):
            commute_rows += f"<tr><td style='padding:2px 0;color:#555;font-size:14px'>🚃 渋谷</td><td style='padding:2px 8px;font-size:14px'><strong>{s_mins}分</strong> <span style='color:#888;font-size:12px'>({s_xf}乗換)</span></td></tr>"
        if str(n_mins) not in ("-",""):
            commute_rows += f"<tr><td style='padding:2px 0;color:#555;font-size:14px'>🚃 新宿</td><td style='padding:2px 8px;font-size:14px'><strong>{n_mins}分</strong> <span style='color:#888;font-size:12px'>({n_xf}乗換)</span></td></tr>"

        walk_str  = f"{wl.get('station_1','')} 徒歩{wl.get('walk_1','')}分"
        yr_m = re.search(r'(\d{4})年', wl.get('built',''))
        built_str = f"築{yr_m.group(1)}年" if yr_m else ""

        reason_html = ""
        if reason:
            reason_html = f"<div style='padding:6px 14px;font-size:13px;color:#e67e22;border-top:1px solid #eee'>⚠️ {reason}</div>"

        border_color = {"⭐⭐⭐": "#27ae60", "⭐⭐": "#2980b9", "⭐": "#95a5a6"}.get(stars, "#ddd")
        building_url = wl.get('url', START_URL)

        cards += f"""
        <table width="100%" cellpadding="0" cellspacing="0"
               style="border:2px solid {border_color};border-radius:8px;
                      margin-bottom:18px;font-family:sans-serif;
                      border-collapse:separate;overflow:hidden">
          <!-- Header -->
          <tr>
            <td style="background:#f8f9fa;padding:10px 14px;border-bottom:1px solid #eee">
              <div style="margin-bottom:4px">
                <span style="background:#e67e22;color:#fff;border-radius:3px;
                             padding:2px 6px;font-size:11px">JKK</span>
                <span style="font-size:15px;margin-left:6px">{stars}</span>
                <span style="background:{wc};color:#fff;border-radius:10px;
                             padding:2px 8px;font-size:11px;margin-left:6px">{wb} {ward_name}</span>
              </div>
              <div style="font-size:16px;font-weight:bold;color:#222;
                          word-break:break-all">{p['name']}</div>
              <div style="color:#666;font-size:13px;margin-top:2px">{p['area']}</div>
            </td>
          </tr>
          <!-- Commute + walk + built -->
          <tr>
            <td style="padding:10px 14px;background:#fafafa;border-bottom:1px solid #eee">
              <table cellpadding="0" cellspacing="0">
                {commute_rows}
                <tr>
                  <td style="padding:2px 0;color:#555;font-size:14px">🚶</td>
                  <td style="padding:2px 8px;font-size:14px">{walk_str}</td>
                </tr>
                <tr>
                  <td style="padding:2px 0;color:#555;font-size:14px">🏗</td>
                  <td style="padding:2px 8px;font-size:14px">{built_str}</td>
                </tr>
              </table>
            </td>
          </tr>
          <!-- Rent -->
          <tr>
            <td style="padding:12px 14px">
              <span style="font-size:18px;font-weight:bold">{madori}</span>
              <span style="font-size:14px;color:#555;margin-left:6px">{p['sqm']}㎡</span>
              <br>
              <span style="font-size:20px;font-weight:bold;color:#c0392b">¥{rent_yen:,}</span>
              <span style="color:#888;font-size:13px"> + ¥{fee_yen:,} 共益費 · {p['units']}戸</span>
            </td>
          </tr>
          {reason_html}
          <!-- CTA -->
          <tr>
            <td style="padding:10px 14px;border-top:1px solid #eee">
              <a href="{building_url}"
                 style="display:inline-block;background:#e67e22;color:#fff;
                        padding:10px 18px;border-radius:4px;text-decoration:none;
                        font-weight:bold;font-size:15px">
                🏠 物件ページ → 申込
              </a>
              <br><br>
              <a href="{START_URL}" style="color:#2980b9;font-size:13px">JKK 検索へ</a>
            </td>
          </tr>
        </table>"""

    html = f"""<html><body style="font-family:sans-serif;max-width:600px;
    margin:0 auto;padding:12px;background:#fff">
    <h2 style="color:#e67e22;margin-bottom:16px;font-size:20px">
      🏠 {len(matches)}件 JKK空き物件あり
    </h2>
    {cards}
    <p style="color:#aaa;font-size:12px;margin-top:8px">
      {datetime.now().strftime('%Y-%m-%d %H:%M')} JST · max ¥{MAX_RENT_YEN:,}/mo
    </p>
    </body></html>"""

    msg = MIMEMultipart("alternative")
    msg["From"], msg["To"] = GMAIL_ADDRESS, NOTIFY_EMAIL
    msg["Subject"] = f"[JKK Alert] {len(matches)}件 whitelist物件あり"
    msg.attach(MIMEText(html, "html", "utf-8"))
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
            s.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
            s.send_message(msg)
        print("Email: ✓")
    except Exception as e:
        print(f"Email error: {e}")

# ─── Main ─────────────────────────────────────────────────────────────────────

def load_autoapply() -> dict:
    if not AUTOAPPLY_FILE.exists(): return {}
    return json.loads(AUTOAPPLY_FILE.read_text(encoding="utf-8"))


def _make_ctx(pw):
    browser = pw.chromium.launch(
        headless=True, args=["--disable-blink-features=AutomationControlled"]
    )
    ctx = browser.new_context(
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        locale="ja-JP", timezone_id="Asia/Tokyo",
        extra_http_headers={"Accept-Language": "ja,en-US;q=0.9,en;q=0.8"},
    )
    return browser, ctx


def _new_page(ctx):
    p = ctx.new_page()
    p.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
    return p


def _login(page) -> bool:
    """Fill login credentials on the current page. Returns True if successful."""
    print("  [login] Filling credentials...")
    try:
        page.wait_for_selector(
            'input[name="loginRM.loginM.userId"]', timeout=10_000
        )
    except PWTimeout:
        print("  [login] No login form found on this page")
        return False

    page.fill('input[name="loginRM.loginM.userId"]', JKK_ID)
    page.fill('input[name="loginRM.loginM.password"]', JKK_PASSWORD)

    with page.expect_navigation(wait_until="domcontentloaded", timeout=20_000):
        page.evaluate("""() => {
            const btn =
                document.querySelector('img[alt*="ログイン"]') ||
                document.querySelector('input[type="image"]') ||
                document.querySelector('input[type="submit"]');
            if (btn) btn.click(); else document.querySelector('form').requestSubmit();
        }""")
    page.wait_for_timeout(2_000)
    print(f"  [login] After login: {page.url}")
    return True
    login_page.wait_for_selector('body', timeout=10_000)
    print(f"  [login] Logged in at: {login_page.url}")
    return login_page


def _click_detail(page, prop_name: str) -> bool:
    """Click the 詳細 button for the named property. Returns True if found."""
    print(f"  [apply] Clicking 詳細 for {prop_name}...")
    return page.evaluate(f"""() => {{
        for (const row of document.querySelectorAll('table tr')) {{
            const cells = [...row.querySelectorAll('td')];
            if (cells.length < 9) continue;
            if (cells[1]?.innerText.trim() !== '{prop_name}') continue;
            const btn = cells[cells.length-1]?.querySelector('input[type="image"], a');
            if (btn) {{ btn.click(); return true; }}
        }}
        return false;
    }}""")


def _select_room_and_apply(page, max_rent: int) -> int | None:
    """Click 申込 for the cheapest room within budget. Returns rent or None."""
    print(f"  [apply] Selecting room (max ¥{max_rent:,})...")
    # Debug: show what numbers are found in the table
    debug = page.evaluate("""() => {
        const nums = [];
        for (const row of document.querySelectorAll('table tr'))
            for (const cell of row.querySelectorAll('td')) {
                const m = cell.innerText.match(/[\\d,]+/);
                if (m) {
                    const n = parseInt(m[0].replace(/,/g,''));
                    if (n >= 1000) nums.push(n);
                }
            }
        return [...new Set(nums)].sort((a,b)=>a-b).slice(0,20);
    }""")
    print(f"  [apply] Numbers found in table: {debug}")
    return page.evaluate(f"""() => {{
        let bestBtn = null, bestRent = {max_rent + 1};
        for (const row of document.querySelectorAll('table tr')) {{
            for (const cell of row.querySelectorAll('td')) {{
                const m = cell.innerText.match(/[\\d,]+/);
                if (!m) continue;
                const num = parseInt(m[0].replace(/,/g, ''));
                if (num >= 50000 && num <= {max_rent} && num < bestRent) {{
                    const btn = row.querySelector('img[alt="申込"], img[src*="mousikomi"]');
                    if (btn) {{ bestBtn = btn; bestRent = num; }}
                }}
            }}
        }}
        if (bestBtn) {{ bestBtn.click(); return bestRent; }}
        return null;
    }}""")


def _complete_application(page, ctx) -> bool:
    """Steps 5-8: eligibility → consent → details form → confirm → submit."""

    # ── Step 5: 申込資格確認 ──────────────────────────────────────────────────
    page.wait_for_selector('img[alt="申込資格について"]', timeout=15_000)
    print("  [apply] 申込資格確認...")
    try:
        with ctx.expect_page(timeout=8_000) as info_info:
            page.evaluate("""() => {
                document.querySelector('img[alt="申込資格について"]')?.click();
            }""")
        tab = info_info.value
        tab.wait_for_load_state("domcontentloaded", timeout=8_000)
        tab.close()
    except PWTimeout:
        pass

    # Click active 同意する (not the grey disabled version)
    page.wait_for_selector('img[alt="同意する"]', timeout=10_000)
    with page.expect_navigation(wait_until="domcontentloaded", timeout=20_000):
        page.evaluate("""() => {
            const btns = [...document.querySelectorAll('img[alt="同意する"]')];
            const active = btns.find(b => !b.src.includes('glay')) || btns[0];
            if (active) active.click();
        }""")

    # ── Step 6: 申込審査情報の確認 → 申込内容入力へ ───────────────────────────
    page.wait_for_selector('img[alt*="申込内容入力"]', timeout=15_000)
    print("  [apply] 申込審査情報の確認...")
    with page.expect_navigation(wait_until="domcontentloaded", timeout=20_000):
        page.evaluate("""() => {
            document.querySelector('img[alt*="申込内容入力"]')?.click();
        }""")

    # ── Step 7: 申込内容入力 → fill → 内容確認へ ─────────────────────────────
    page.wait_for_selector('input[name*="mskInputRM"]', timeout=15_000)
    print("  [apply] Filling form...")
    page.evaluate("""() => {
        [
            ['input[name="mskInputRM.mskInputM.chusyajoFlg"][value="0"]', 'click'],
            ['input[name="mskInputRM.mskInputM.hojinFlg"][value="0"]',    'click'],
            ['input[name="mskInputRM.mskInputM.shareFlg"][value="0"]',    'click'],
            ['input[name="mskInputRM.mskInputM.hoshoFlg"][value="2"]',    'click'],
        ].forEach(([sel, fn]) => document.querySelector(sel)?.[fn]());
    }""")
    try:
        page.select_option(
            'select[name="mskInputRM.mskInputM.jukyoCdH"]',
            label='UR(公団)賃貸住宅'
        )
    except Exception as e:
        print(f"  [apply] Housing select: {e}")

    page.wait_for_selector('img[alt*="内容確認"]', timeout=10_000)
    with page.expect_navigation(wait_until="domcontentloaded", timeout=20_000):
        page.evaluate("""() => {
            document.querySelector('img[alt*="内容確認"]')?.click();
        }""")

    # ── Step 8: 申込内容の確認 → 同意して申し込む ────────────────────────────
    page.wait_for_selector('img[alt*="同意して申し込む"], img[alt*="申し込む"]', timeout=15_000)
    if JKK_DRY_RUN:
        print(f"  [apply] 🛑 DRY RUN — skipping final submit at {page.url}")
        return True  # Treat as success for notification purposes
    print("  [apply] 同意して申し込む...")
    with page.expect_navigation(wait_until="domcontentloaded", timeout=30_000):
        page.evaluate("""() => {
            const btn = document.querySelector('img[alt*="同意して申し込む"]') ||
                        document.querySelector('img[alt*="申し込む"]');
            if (btn) btn.click();
        }""")

    # ── Check for 申込完了 ────────────────────────────────────────────────────
    print(f"  [apply] Final: {page.url}")
    return page.evaluate("""() =>
        ['申込完了','申込が完了','受付番号','お申し込みが完了']
            .some(s => document.body.innerText.includes(s))
    """)


def scrape_and_apply_session(autoapply: dict, seen: set) -> tuple[list, list]:
    """
    Single Playwright session: login → search → scrape → apply.
    Returns (available_listings, apply_results).
    Faster than two separate sessions — eliminates duplicate browser startup,
    login, and search flow.
    """
    available    = []
    apply_results = []

    with sync_playwright() as pw:
        browser, ctx = _make_ctx(pw)
        try:
            # Search first (no login needed)
            page = _new_page(ctx)
            _run_search_flow(page)
            available = _extract_all_pages(page)
            print(f"  Available: {len(available)} listings")

            # Identify autoapply matches that are new
            apply_queue = [
                p for p in available
                if match_whitelist(p["name"], autoapply)
                and make_id(p) not in seen
                and parse_rent(p["rent"]) <= MAX_RENT_YEN
                and (not ALLOWED_MADORI or is_allowed_madori(p["madori"]))
            ]

            # Sort by score and take only the top 1 (JKK allows one application at a time)
            def _score(prop):
                wl = match_whitelist(prop["name"], autoapply) or {}
                s  = safe_int(wl.get("shibuya",  999))
                sx = safe_int(wl.get("shibuya_transfers", 0), 0)
                n  = safe_int(wl.get("shinjuku", 999))
                nx = safe_int(wl.get("shinjuku_transfers", 0), 0)
                best_eff  = min(s + sx*5, n + nx*5)
                yr_m = re.search(r'(\d{4})年', wl.get("built", ""))
                year = int(yr_m.group(1)) if yr_m else 1990
                tier = next((t for w, t in WARD_TIERS.items()
                             if w in wl.get("location", "")), 0)
                rent = parse_rent(prop["rent"])
                c = max(0, (55 - best_eff) / 55 * 10)
                ny = max(0, (year - 1989) / (2025 - 1989) * 10)
                w = {1:10, 2:6.5, 3:3, 0:0}.get(tier, 0)
                p2 = max(0, (160000 - rent) / (160000 - 67000) * 10)
                return c*4 + ny*3 + w*2 + p2*1

            # Test override: force-apply for a specific property (for dry run testing)
            if JKK_TEST_PROPERTY:
                test_match = next((p for p in available
                                   if JKK_TEST_PROPERTY in p["name"]
                                   or p["name"] in JKK_TEST_PROPERTY), None)
                if test_match:
                    print(f"  TEST OVERRIDE: forcing apply for {test_match['name']}")
                    apply_queue = [test_match]
                else:
                    print(f"  TEST OVERRIDE: '{JKK_TEST_PROPERTY}' not in current listings")

            apply_queue.sort(key=_score, reverse=True)
            if len(apply_queue) > 1:
                skipped = [p["name"] for p in apply_queue[1:]]
                print(f"  Skipping {skipped} — applying for highest-ranked only")
                apply_queue = apply_queue[:1]

            print(f"  AutoApply queue: {len(apply_queue)} propert(ies)")

            for prop in apply_queue:
                pname  = prop["name"]
                result = {"name": pname, "madori": normalize(prop["madori"]),
                          "success": False, "rent": 0, "error": ""}
                try:
                    # Re-search for fresh results page
                    _run_search_flow(page)

                    if not _click_detail(page, pname):
                        result["error"] = "Not found (already taken?)"
                        apply_results.append(result); continue

                    page.wait_for_selector('table', timeout=15_000)

                    # Click 申込 — server redirects to login
                    max_rent = 9_999_999 if JKK_TEST_PROPERTY else MAX_RENT_YEN
                    rent = _select_room_and_apply(page, max_rent)
                    if not rent:
                        result["error"] = "No room within budget"
                        print(f"  ✗ {pname}: No room within budget")
                        apply_results.append(result); continue

                    result["rent"] = rent
                    page.wait_for_load_state("domcontentloaded", timeout=15_000)

                    # Login on the redirect page
                    if not _login(page):
                        result["error"] = "Login failed"
                        apply_results.append(result); continue

                    # Complete application form
                    success = _complete_application(page, ctx)
                    result["success"] = success
                    if not success:
                        result["error"] = "Did not reach 申込完了"
                    print(f"  {'✓' if success else '✗'} {pname}: "
                          f"{'Applied!' if success else result['error']}")

                except Exception as e:
                    result["error"] = str(e)
                    print(f"  ✗ {pname}: {e}")

                apply_results.append(result)

        except Exception as e:
            print(f"  Session error: {e}")
        finally:
            browser.close()

    return available, apply_results


def notify_line_apply(apply_results: list[dict]) -> None:
    if not LINE_CHANNEL_TOKEN or not LINE_USER_ID: return
    msg = "🤖 JKK Auto-Apply Results:\n"
    for r in apply_results:
        icon = "✅" if r["success"] else "❌"
        rent = f"¥{r['rent']:,}" if r["rent"] else ""
        err  = f" ({r['error']})" if not r["success"] else ""
        msg += f"{icon} {r['name']} {r['madori']} {rent}{err}\n"
    requests.post(
        "https://api.line.me/v2/bot/message/push",
        headers={"Authorization": f"Bearer {LINE_CHANNEL_TOKEN}",
                 "Content-Type": "application/json"},
        json={"to": LINE_USER_ID, "messages": [{"type": "text", "text": msg}]},
        timeout=10,
    )

def main():
    print(f"=== JKK Watcher {datetime.now():%Y-%m-%d %H:%M} ===")
    whitelist = load_whitelist()
    autoapply = load_autoapply()
    if not whitelist:
        print("No whitelist — run: python jkk_scan.py --build-whitelist"); return
    print(f"Whitelist: {len(whitelist)} | AutoApply: {len(autoapply)} properties")

    seen = load_seen()
    apply_results = []

    # ── Single session when credentials available (login + scrape + apply) ────
    if JKK_ID and JKK_PASSWORD:
        print("  JKK credentials found — using single logged-in session")
        available, apply_results = scrape_and_apply_session(autoapply, seen)
    else:
        print("  No JKK credentials — scrape only (no auto-apply)")
        available = scrape_available()

    print(f"Available listings: {len(available)}")

    new_matches = []
    all_ids = set()
    for prop in available:
        pid = make_id(prop)
        all_ids.add(pid)
        if pid in seen: continue
        wl = match_whitelist(prop["name"], whitelist)
        if not wl: continue
        if ALLOWED_MADORI and not is_allowed_madori(prop["madori"]): continue
        if parse_rent(prop["rent"]) > MAX_RENT_YEN: continue
        new_matches.append({"prop": prop, "whitelist": wl, "id": pid})
        stars, _ = get_stars(wl)
        print(f"  ✓ {stars} {prop['name']} {normalize(prop['madori'])} ¥{parse_rent(prop['rent']):,}")

    seen.update(all_ids)
    save_seen(seen)
    print(f"New matches: {len(new_matches)}")

    if apply_results:
        notify_line_apply(apply_results)
    if not new_matches:
        print("No new listings — done."); return
    notify_line(new_matches)
    notify_email(new_matches)

if __name__ == "__main__":
    main()


# ─── Auto-Apply Bot ───────────────────────────────────────────────────────────

