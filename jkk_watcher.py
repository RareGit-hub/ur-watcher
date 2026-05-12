#!/usr/bin/env python3
"""
JKK Availability Watcher — runs every 15 min via GitHub Actions
Cross-references available listings against jkk_whitelist.json
"""

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

WHITELIST_FILE = Path("jkk_whitelist.json")
SEEN_FILE      = Path("jkk_seen.json")

MAX_RENT_YEN   = 160000
ALLOWED_MADORI = ["1R", "1K", "1DK", "1LDK", "2K", "2DK"]

GMAIL_ADDRESS      = os.environ.get("GMAIL_ADDRESS", "").strip()
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "").strip()
NOTIFY_EMAIL       = os.environ.get("NOTIFY_EMAIL", "").strip()
LINE_CHANNEL_TOKEN = "".join(os.environ.get("LINE_CHANNEL_TOKEN", "").split())
LINE_USER_ID       = os.environ.get("LINE_USER_ID", "").strip()

# ─── Helpers ─────────────────────────────────────────────────────────────────

FULLWIDTH = str.maketrans(
    '０１２３４５６７８９ＡＢＣＤＥＦＧＨＩＪＫＬＭＮＯＰＱＲＳＴＵＶＷＸＹＺ'
    'ａｂｃｄｅｆｇｈｉｊｋｌｍｎｏｐｑｒｓｔｕｖｗｘｙｚ　＋',
    '0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ'
    'abcdefghijklmnopqrstuvwxyz +'
)

def normalize(text: str) -> str:
    return text.translate(FULLWIDTH).strip()

def parse_rent(rent_str: str) -> int:
    nums = re.findall(r'\d+', rent_str.replace(',', ''))
    return int(nums[0]) if nums else 0

def is_allowed_madori(madori_raw: str) -> bool:
    m = normalize(madori_raw).upper().replace(' ', '')
    return any(a in m for a in ALLOWED_MADORI)

def make_id(prop: dict) -> str:
    return f"jkk_{prop['name']}_{prop['madori']}_{prop['rent']}".replace(' ', '')

# ─── State ────────────────────────────────────────────────────────────────────

def load_whitelist() -> dict:
    if not WHITELIST_FILE.exists():
        return {}
    return json.loads(WHITELIST_FILE.read_text(encoding="utf-8"))

def load_seen() -> set:
    if SEEN_FILE.exists():
        return set(json.loads(SEEN_FILE.read_text(encoding="utf-8")))
    return set()

def save_seen(seen: set):
    SEEN_FILE.write_text(
        json.dumps(sorted(seen), ensure_ascii=False, indent=2), encoding="utf-8"
    )

# ─── Scraper ─────────────────────────────────────────────────────────────────

def scrape_available() -> list[dict]:
    props = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        ).new_page()
        try:
            # Step 1: load start page (establishes session + CSRF tokens)
            print(f"  Loading search page...")
            page.goto(START_URL, wait_until="domcontentloaded", timeout=60_000)
            page.wait_for_timeout(6_000)

            # Step 2: POST the form to establish the search session server-side
            page.evaluate("() => document.querySelector('form').submit()")
            page.wait_for_timeout(8_000)
            print(f"  After submit URL: {page.url}")

            # Step 3: navigate directly to results page
            # The POST has set the session; a GET to akiyaJyoukenRef now shows results
            if "akiyaJyoukenRef" not in page.url:
                print(f"  Navigating to results page...")
                page.goto(RESULTS_URL, wait_until="domcontentloaded", timeout=60_000)
                page.wait_for_timeout(5_000)
            print(f"  Results loaded: {page.url}")

            # Step 3: set 50件 per page to avoid pagination if possible
            clicked_50 = page.evaluate("""() => {
                for (const el of document.querySelectorAll('a, input, button, td, span')) {
                    if ((el.innerText || el.value || '').trim() === '50件') {
                        el.click(); return true;
                    }
                }
                return false;
            }""")
            if clicked_50:
                page.wait_for_timeout(4_000)
                print("  Set to 50 per page")

            # Step 4: extract table across pages
            page_num = 1
            while True:
                print(f"  Page {page_num}...", end=" ")
                rows = page.evaluate("""() => {
                    const results = [];
                    let mainTable = null;
                    for (const t of document.querySelectorAll('table')) {
                        if (t.innerText.includes('住宅名') && t.innerText.includes('間取り')) {
                            mainTable = t; break;
                        }
                    }
                    if (!mainTable) return results;

                    const allRows = [...mainTable.querySelectorAll('tr')];
                    let headerIdx = -1;
                    for (let i = 0; i < allRows.length; i++) {
                        if (allRows[i].innerText.includes('住宅名') &&
                            allRows[i].innerText.includes('間取り')) {
                            headerIdx = i; break;
                        }
                    }
                    if (headerIdx < 0) return results;

                    for (let i = headerIdx + 1; i < allRows.length; i++) {
                        const cells = [...allRows[i].querySelectorAll('td')];
                        if (cells.length < 8) continue;
                        const t = (idx) =>
                            (cells[idx]?.innerText || '').trim().replace(/\\s+/g, ' ');

                        // columns: image(0) name(1) area(2) priority(3) type(4)
                        //          madori(5) sqm(6) rent(7) fee(8) units(9) button(10)
                        const name   = t(1);
                        const area   = t(2);
                        const madori = t(5);
                        const sqm    = t(6);
                        const rent   = t(7);
                        const fee    = t(8);
                        const units  = t(9);

                        if (name && madori && rent) {
                            results.push({ name, area, madori, sqm, rent, fee, units });
                        }
                    }
                    return results;
                }""")

                props.extend(rows)
                print(f"{len(rows)} rows (total {len(props)})")

                if clicked_50:
                    break  # all on one page

                # try next page button
                next_num = page_num + 1
                more = page.evaluate(f"""() => {{
                    for (const el of document.querySelectorAll(
                            'button[class*="MuiPaginationItem"], a, input')) {{
                        if ((el.innerText || el.value || '').trim() === '{next_num}') {{
                            el.click(); return true;
                        }}
                    }}
                    return false;
                }}""")
                if not more:
                    break
                page.wait_for_timeout(3_000)
                page_num += 1
                if page_num > 20:
                    break

        except PWTimeout:
            print("  Timeout on JKK site")
        finally:
            browser.close()

    return props

# ─── Whitelist matching ───────────────────────────────────────────────────────

def match_whitelist(name: str, whitelist: dict):
    name = name.strip()
    if name in whitelist:
        return whitelist[name]
    for wl_name, wl_data in whitelist.items():
        if wl_name in name or name in wl_name:
            return wl_data
    return None

# ─── Notifications ────────────────────────────────────────────────────────────

def notify_line(matches: list[dict]) -> None:
    if not LINE_CHANNEL_TOKEN or not LINE_USER_ID:
        print("LINE not configured"); return

    msg = f"🏠 JKK: {len(matches)} whitelist listing(s) available!\n"
    for m in matches[:5]:
        p, wl = m["prop"], m["whitelist"]
        rent_total = parse_rent(p["rent"]) + parse_rent(p["fee"])
        st = wl.get("station_1", "")
        wk = wl.get("walk_1", "")
        msg += (
            f"\n■ {p['name']} ({p['area']})\n"
            f"  {normalize(p['madori'])} {p['sqm']}㎡ / ¥{rent_total:,}/月\n"
            f"  🚶 {st} {wk}\n"
            f"  📋 {RESULTS_URL}\n"
        )
    if len(matches) > 5:
        msg += f"\n...and {len(matches)-5} more — see email"

    r = requests.post(
        "https://api.line.me/v2/bot/message/push",
        headers={
            "Authorization": f"Bearer {LINE_CHANNEL_TOKEN}",
            "Content-Type": "application/json",
        },
        json={"to": LINE_USER_ID, "messages": [{"type": "text", "text": msg}]},
        timeout=10,
    )
    print("LINE:", "✓" if r.status_code == 200 else f"✗ {r.status_code} {r.text}")


def notify_email(matches: list[dict]) -> None:
    if not all([GMAIL_ADDRESS, GMAIL_APP_PASSWORD, NOTIFY_EMAIL]):
        print("Email not configured"); return

    rows = ""
    for m in matches:
        p, wl = m["prop"], m["whitelist"]
        rent_yen = parse_rent(p["rent"])
        fee_yen  = parse_rent(p["fee"])
        madori   = normalize(p["madori"])

        station_rows = ""
        for i in ["1", "2"]:
            st = wl.get(f"station_{i}", "")
            wk = wl.get(f"walk_{i}", "")
            if st:
                station_rows += (
                    f"<tr><td style='padding:3px 8px;color:#555'>🚶</td>"
                    f"<td style='padding:3px 8px'>{st} {wk}</td></tr>"
                )

        rows += f"""
        <tr><td colspan="2" style="padding:0">
        <table width="100%" style="border-collapse:collapse;border:2px solid #e67e22;
               border-radius:6px;margin-bottom:14px">
          <tr style="background:#fef3e2">
            <td colspan="2" style="padding:10px">
              <span style="background:#e67e22;color:#fff;border-radius:3px;
                           padding:2px 6px;font-size:11px">JKK</span>
              <strong style="margin-left:8px;font-size:15px">{p['name']}</strong>
              <span style="margin-left:8px;color:#666;font-size:12px">{p['area']}</span>
            </td>
          </tr>
          <tr>
            <td style="padding:8px 10px;width:50%;vertical-align:top">
              <table style="border-collapse:collapse">
                <tr><td style="padding:3px 8px;color:#555">間取り</td>
                    <td style="padding:3px 8px"><strong>{madori}</strong></td></tr>
                <tr><td style="padding:3px 8px;color:#555">面積</td>
                    <td style="padding:3px 8px">{p['sqm']}㎡</td></tr>
                <tr><td style="padding:3px 8px;color:#555">家賃＋共益費</td>
                    <td style="padding:3px 8px">
                      <strong>¥{rent_yen:,} ＋ ¥{fee_yen:,}／月</strong></td></tr>
                <tr><td style="padding:3px 8px;color:#555">募集戸数</td>
                    <td style="padding:3px 8px">{p['units']}戸</td></tr>
                <tr><td style="padding:3px 8px;color:#555">築年</td>
                    <td style="padding:3px 8px">{wl.get('built','不明')}</td></tr>
              </table>
            </td>
            <td style="padding:8px 10px;vertical-align:top">
              <table style="border-collapse:collapse">{station_rows}</table>
            </td>
          </tr>
          <tr>
            <td colspan="2" style="padding:8px 10px;border-top:1px solid #eee">
              <a href="{RESULTS_URL}"
                 style="background:#e67e22;color:#fff;padding:6px 14px;
                        border-radius:4px;text-decoration:none;font-weight:bold">
                📋 Apply online (JKK)
              </a>
              &nbsp;&nbsp;
              <a href="{wl.get('url', '')}" style="color:#2980b9">🏠 Building page</a>
            </td>
          </tr>
        </table></td></tr>"""

    html = f"""<html><body style="font-family:sans-serif;max-width:700px;
    margin:0 auto;padding:20px">
    <h2 style="color:#e67e22">
      🏠 {len(matches)} JKK Whitelist Listing(s) Now Available
    </h2>
    <table width="100%" style="border-collapse:collapse">{rows}</table>
    <p style="color:#888;font-size:0.85em;margin-top:16px">
      Checked: {datetime.now().strftime('%Y-%m-%d %H:%M')} JST ·
      Filters: max ¥{MAX_RENT_YEN:,}/mo · layouts: {', '.join(ALLOWED_MADORI)}
    </p>
    </body></html>"""

    msg = MIMEMultipart("alternative")
    msg["From"], msg["To"] = GMAIL_ADDRESS, NOTIFY_EMAIL
    msg["Subject"] = f"[JKK Alert] {len(matches)} whitelist listing(s) available"
    msg.attach(MIMEText(html, "html", "utf-8"))
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
            s.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
            s.send_message(msg)
        print("Email: ✓")
    except Exception as e:
        print(f"Email error: {e}")

# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    print(f"=== JKK Watcher {datetime.now():%Y-%m-%d %H:%M} ===")

    whitelist = load_whitelist()
    if not whitelist:
        print("No whitelist — run: python jkk_scan.py --build-whitelist"); return
    print(f"Whitelist: {len(whitelist)} properties")

    seen     = load_seen()
    available = scrape_available()
    print(f"Available listings: {len(available)}")

    new_matches = []
    all_ids = set()

    for prop in available:
        pid = make_id(prop)
        all_ids.add(pid)

        if pid in seen:
            continue

        wl = match_whitelist(prop["name"], whitelist)
        if not wl:
            continue

        if not is_allowed_madori(prop["madori"]):
            continue
        if parse_rent(prop["rent"]) > MAX_RENT_YEN:
            continue

        new_matches.append({"prop": prop, "whitelist": wl, "id": pid})
        print(f"  ✓ {prop['name']} {normalize(prop['madori'])} ¥{parse_rent(prop['rent']):,}")

    seen.update(all_ids)
    save_seen(seen)

    print(f"New matches: {len(new_matches)}")
    if not new_matches:
        print("No new listings — done."); return

    notify_line(new_matches)
    notify_email(new_matches)


if __name__ == "__main__":
    main()
