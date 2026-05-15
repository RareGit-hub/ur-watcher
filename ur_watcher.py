#!/usr/bin/env python3
"""UR Chintai property watcher — Local + Playwright"""

import json, os, re, smtplib, urllib.parse
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import requests
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

load_dotenv()

# ─── CONFIGURATION ───────────────────────────────────────────────────────────

SEARCH_SOURCES = [
    {
        "url": "https://www.ur-net.go.jp/chintai/kanto/tokyo/result/?tdfk=13&walk=15&bus=1&block=kanto&tdfkNm=%E6%9D%B1%E4%BA%AC%E9%83%BD&tdfkCd=13&station_cd1=2334&station_cost1=40&station_change1=0",
        "label": "Regular",
        "type": "regular",
        "prefecture_filter": None,
    },
    {
        "url": "https://www.ur-net.go.jp/chintai/kanto/tokyo/result/?tdfk=13&walk=15&bus=1&block=kanto&tdfkNm=%E6%9D%B1%E4%BA%AC%E9%83%BD&tdfkCd=13&station_cd1=2248&station_cost1=40&station_change1=0",
        "label": "Regular",
        "type": "regular",
        "prefecture_filter": None,
    },
    {
        "url": "https://www.ur-net.go.jp/chintai/kanto/tokyo/result/?tdfk=13&walk=15&bus=1&block=kanto&tdfkNm=%E6%9D%B1%E4%BA%AC%E9%83%BD&tdfkCd=13&station_cd1=2590&station_cost1=40&station_change1=0",
        "label": "Regular",
        "type": "regular",
        "prefecture_filter": None,
    },
    {
        "url": "https://www.ur-net.go.jp/chintai/kanto/tokyo/result/?tdfk=13&walk=15&bus=1&block=kanto&tdfkNm=%E6%9D%B1%E4%BA%AC%E9%83%BD&tdfkCd=13&station_cd1=3260&station_cost1=40&station_change1=0",
        "label": "Regular",
        "type": "regular",
        "prefecture_filter": None,
    },
    {
        "url": "https://www.ur-net.go.jp/chintai/tokubetsu/",
        "label": "Special Listing (50% off rent)",
        "type": "tokubetsu",
        "prefecture_filter": ["東京", "神奈川"],
    },
]

MAX_RENT_MAN_YEN = 15.0
ALLOWED_MADORI   = ["1R・1K", "1DK", "1LDK", "2K", "2DK"]

# ─────────────────────────────────────────────────────────────────────────────

GMAIL_ADDRESS      = os.environ.get("GMAIL_ADDRESS", "").strip()
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "").strip()
NOTIFY_EMAIL       = os.environ.get("NOTIFY_EMAIL", "").strip()
LINE_CHANNEL_TOKEN = "".join(os.environ.get("LINE_CHANNEL_TOKEN", "").split())
LINE_USER_ID       = os.environ.get("LINE_USER_ID", "").strip()
DEBUG_MODE         = os.environ.get("DEBUG_MODE", "false").lower() == "true"
STATE_FILE         = Path("seen_ids.json")
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
    return WARD_BADGE[0], "", WARD_COLOR[0], 0




def load_seen() -> set:
    return set(json.loads(STATE_FILE.read_text(encoding="utf-8"))) if STATE_FILE.exists() else set()

def save_seen(seen: set) -> None:
    STATE_FILE.write_text(json.dumps(sorted(seen), ensure_ascii=False, indent=2), encoding="utf-8")


def scrape_listings(url: str, label: str = "", source_type: str = "regular") -> list[dict]:
    props = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        ).new_page()
        try:
            print(f"  Loading [{source_type}] {url}")
            page.goto(url, wait_until="domcontentloaded", timeout=120_000)
            page.wait_for_timeout(8_000)

            if DEBUG_MODE:
                page.screenshot(path=f"debug_{source_type}.png", full_page=True)
                Path(f"debug_{source_type}.html").write_text(page.content(), encoding="utf-8")

            if source_type == "tokubetsu":
                expand_btns = page.query_selector_all('.js-tokubetsu-tdfk-trigger')
                print(f"  Expanding {len(expand_btns)} prefecture sections...")
                for btn in expand_btns:
                    btn.click()
                    page.wait_for_timeout(300)
                page.wait_for_timeout(2_000)

                props = page.evaluate("""() => {
                    const results = [];
                    document.querySelectorAll('.js-tokubetsu-bukken-row').forEach(row => {
                        try {
                            const bukken    = row.closest('.js-tokubetsu-bukken');
                            const tdfkBlock = row.closest('[class*="js-tokubetsu-tdfk"]');
                            const prefEl    = tdfkBlock && tdfkBlock.querySelector('.js-tokubetsu-tdfk-name');
                            const pref      = prefEl ? prefEl.textContent.trim() : '';

                            // Use link text for property name (avoids picking up city headings)
                            const nameLink  = bukken && bukken.querySelector('a[href*="/chintai/"]');
                            const nameEl    = bukken && bukken.querySelector('h3, h4, [class*="name"], [class*="title"]');
                            const name      = nameLink ? nameLink.textContent.trim() :
                                             nameEl   ? nameEl.textContent.trim()   : '';

                            const text      = row.innerText || '';
                            const allRents  = [...text.matchAll(/([\\d,]+)\\s*円/g)]
                                .map(m => parseInt(m[1].replace(/,/g,'')))
                                .filter(v => v > 10000);
                            const normalYen   = allRents[0] || 0;
                            const discountYen = allRents[1] || normalYen;

                            const mado   = text.match(/([1-9][LDKSR]+|ワンルーム)/);
                            const sqm    = text.match(/([\\d.]+)\\s*㎡/);
                            const floor  = text.match(/([\\d]+)階/);
                            const period = text.match(/(\\d+年)/);
                            const link   = row.querySelector('a') || (bukken && bukken.querySelector('a'));
                            const href   = link ? (link.getAttribute('href') || '') : '';
                            const id     = ('tokubetsu_' + name + '_' + (mado ? mado[1] : '') + '_' + (floor ? floor[1] : '')).replace(/\\s/g,'');

                            // Nearest stations from row text
                            const stMatches = [...text.matchAll(/[「｢]([^」｣]+)[」｣]駅[^\\n]*?徒歩([\\d～〜]+分)/g)];
                            const nearestStations = stMatches.slice(0, 3).map(m => m[1] + '駅 徒歩' + m[2]);

                            results.push({
                                id, name, pref,
                                normal_rent_yen:  normalYen,
                                rent_yen:         discountYen,
                                rent_man:         discountYen / 10000,
                                madori:           mado   ? mado[1]            : '不明',
                                sqm:              sqm    ? parseFloat(sqm[1]) : 0,
                                discount_period:  period ? period[1]          : '',
                                commute_lines:    [],
                                nearest_stations: nearestStations,
                                url: href.startsWith('http') ? href : 'https://www.ur-net.go.jp' + href,
                            });
                        } catch(e) {}
                    });
                    return results;
                }""")
            else:
                PAGE_JS = """() => {
                    const results = [];
                    document.querySelectorAll('.js-log-item').forEach(room => {
                        try {
                            const detailLink = room.querySelector('a[href*="room"]');
                            if (!detailLink) return;
                            const card = room.closest('[class*="js-bukken-key"]');
                            if (!card) return;
                            const href     = detailLink.getAttribute('href') || '';
                            const cardText = card.innerText || '';
                            const text     = room.innerText || '';

                            // Extract ANY station-to-station commute pattern
                            const commuteMatches = [...cardText.matchAll(/[^\\s　、,\\n]+駅から[^\\s　、,\\n]+駅まで(\\d+)分（乗り換え(\\d+)回）/g)];
                            const commuteLines = commuteMatches.slice(0, 3).map(m => m[0]);

                            // Extract nearest station walking times
                            const stationMatches = [...cardText.matchAll(/([^\\s　「」\\n]+線)[「｢]([^」｣]+)[」｣]駅\\s*徒歩([\\d～〜]+分)/g)];
                            const nearestStations = stationMatches.slice(0, 3).map(m => m[2] + '駅 徒歩' + m[3]);

                            // Clean property name — filter out commute and station lines
                            const nameLine = cardText.split('\\n')
                                .map(s => s.trim())
                                .filter(s => s
                                    && !s.match(/駅から.+駅まで/)
                                    && !s.match(/駅\\s*徒歩/)
                                    && !s.includes('お気に入り')
                                    && !s.includes('住棟別')
                                    && s.length > 1
                                )[0] || '';

                            const rentMatch   = text.match(/([\\d,]+)\\s*円/);
                            const madoriMatch = text.match(/([1-9][LDKSR]+|ワンルーム)/);
                            const sqmMatch    = text.match(/([\\d.]+)\\s*㎡/);
                            const roomMatch   = text.match(/([\\d]+号棟[^\\d]*[\\d]+号室|[\\d-]+-[\\d]+号棟[^\\d]*[\\d]+号室|[\\d]+号室)/);
                            const rentYen     = rentMatch ? parseInt(rentMatch[1].replace(/,/g,'')) : 0;
                            const id          = href.replace(/\\.html$/, '').split('/').pop().replace(/\\s/g,'');

                            results.push({
                                id,
                                name: nameLine + (roomMatch ? ' ' + roomMatch[1].trim() : ''),
                                pref: '',
                                normal_rent_yen: 0,
                                rent_yen:  rentYen,
                                rent_man:  rentYen / 10000,
                                madori:    madoriMatch ? madoriMatch[1] : '不明',
                                sqm:       sqmMatch ? parseFloat(sqmMatch[1]) : 0,
                                discount_period: '',
                                commute_lines:    commuteLines,
                                nearest_stations: nearestStations,
                                url: href.startsWith('http') ? href : 'https://www.ur-net.go.jp' + href,
                            });
                        } catch(e) {}
                    });
                    return results;
                }"""

                page_num = 1
                while True:
                    print(f"    Page {page_num}...")
                    page_props = page.evaluate(PAGE_JS)
                    props.extend(page_props)
                    next_btn = page.query_selector('.item_next')
                    if not next_btn:
                        break
                    next_btn.click()
                    page.wait_for_timeout(3_000)
                    page_num += 1

            for p in props:
                p["label"] = label
            print(f"  -> {len(props)} listings found")

        except PWTimeout:
            print(f"  Timeout loading {url}")
        finally:
            browser.close()
    return props


def get_property_details(url: str) -> dict:
    """Scrape building age and renovation status from room detail page."""
    result = {
        "building_age": "不明",
        "renovation": False,
        "address": "",
        "maps_url": "",
        "sales_center": "",
    }

    if not url or "ur-net.go.jp" not in url:
        return result

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        ).new_page()
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=60_000)
            page.wait_for_timeout(5_000)

            data = page.evaluate("""() => {
                const text = document.body.innerText || '';

                // Building age using confirmed selector
                const ageEl = document.querySelector('.item_text.rep_years, [class*="rep_years"]');
                const buildingAge = ageEl ? ageEl.textContent.trim() : '不明';

                // Renovation
                const renovation = text.includes('リノベーション');

                // Address
                const addrMatch = text.match(/(東京都|神奈川県)[^\\n]{5,40}/);
                const address = addrMatch ? addrMatch[0].trim() : '';

                // Sales center
                const centerMatch = text.match(/(営業センター|管理センター)[^\\n]*/);
                const salesCenter = centerMatch ? centerMatch[0].trim().substring(0, 60) : '';

                return { building_age: buildingAge, renovation, address, sales_center: salesCenter };
            }""")

            result.update(data)

            if result.get("address"):
                result["maps_url"] = "https://maps.google.com/maps?q=" + urllib.parse.quote(result["address"])

        except Exception as e:
            print(f"  Detail scrape failed: {e}")
        finally:
            browser.close()

    return result


def get_ur_stars(p: dict, details: dict) -> tuple:
    """Rate a UR listing → (stars_str, reason_str)."""
    commute_mins = 999
    for line in p.get("commute_lines", []):
        m = re.search(r'まで(\d+)分', line)
        if m: commute_mins = min(commute_mins, int(m.group(1)))

    walk_mins = 999
    for st in p.get("nearest_stations", []):
        m = re.search(r'徒歩(\d+)', st)
        if m: walk_mins = min(walk_mins, int(m.group(1)))

    year_match = re.search(r'(\d{4})年', details.get("building_age", ""))
    built_year = int(year_match.group(1)) if year_match else 0
    renovated  = details.get("renovation", False)

    if commute_mins <= 30 and walk_mins <= 15 and (built_year >= 2010 or renovated):
        return "⭐⭐⭐", ""
    elif commute_mins <= 35 and walk_mins <= 15:
        if renovated: return "⭐⭐", "リノベ済"
        return "⭐⭐", f"築{built_year}年" if built_year else "築年不明"
    else:
        reasons = []
        if walk_mins > 15:  reasons.append(f"徒歩{walk_mins}分")
        if commute_mins > 35: reasons.append(f"通勤{commute_mins}分")
        if built_year and built_year < 2010 and not renovated: reasons.append(f"築{built_year}年")
        return "⭐", " / ".join(reasons)


def matches(p: dict, prefecture_filter: list | None = None) -> bool:
    if MAX_RENT_MAN_YEN and p["rent_man"] > MAX_RENT_MAN_YEN:
        return False
    if ALLOWED_MADORI and p["madori"] not in ALLOWED_MADORI:
        return False
    if prefecture_filter:
        if not any(f in p.get("pref", "") for f in prefecture_filter):
            return False
    return True


def notify_line(new_props: list[dict]) -> None:
    if not LINE_CHANNEL_TOKEN or not LINE_USER_ID:
        print("LINE credentials not set — skipping"); return

    msg = f"🏠 {len(new_props)} new UR listing(s)\n"
    for p in new_props[:5]:
        is_discount = p.get("normal_rent_yen", 0) and p["normal_rent_yen"] != p["rent_yen"]
        rent_str = (
            f"¥{p['rent_yen']:,}/mo (normally ¥{p['normal_rent_yen']:,} — {p.get('discount_period','')} discount)"
            if is_discount else f"¥{p['rent_yen']:,}/mo"
        )
        commute = p.get("commute_lines", [])
        commute_str = f"\n  🚃 {commute[0]}" if commute else ""
        nearest = p.get("nearest_stations", [])
        nearest_str = f"\n  🚶 {nearest[0]}" if nearest else ""
        # Quick star for LINE (no building age available yet)
        c_mins = 999
        for line in commute:
            m = re.search(r'まで(\d+)分', line)
            if m: c_mins = min(c_mins, int(m.group(1)))
        w_mins = 999
        for st in nearest:
            m = re.search(r'徒歩(\d+)', st)
            if m: w_mins = min(w_mins, int(m.group(1)))
        quick_stars = "⭐⭐" if c_mins <= 35 and w_mins <= 15 else "⭐"
        msg += f"\n■ {quick_stars} [{p['label']}] {p['name']}\n  {p['madori']} {p['sqm']}㎡ / {rent_str}{commute_str}{nearest_str}\n  {p['url']}\n"
    if len(new_props) > 5:
        msg += f"\n...and {len(new_props)-5} more (see email)"

    r = requests.post(
        "https://api.line.me/v2/bot/message/push",
        headers={"Authorization": f"Bearer {LINE_CHANNEL_TOKEN}", "Content-Type": "application/json"},
        json={"to": LINE_USER_ID, "messages": [{"type": "text", "text": msg}]},
        timeout=10,
    )
    print("LINE:", "✓" if r.status_code == 200 else f"✗ {r.status_code} {r.text}")


def notify_email(new_props: list[dict]) -> None:
    if not all([GMAIL_ADDRESS, GMAIL_APP_PASSWORD, NOTIFY_EMAIL]):
        print("Email not configured"); return

    # Fetch all details, compute stars+ward, then sort
    enriched = []
    for p in new_props:
        print(f"  Fetching details for {p['name']}...")
        details = get_property_details(p["url"])
        stars, reason = get_ur_stars(p, details)
        wb, ward_name, wc, tier = get_ward_info(details.get("address", "") + p.get("name",""))
        enriched.append((stars, reason, wb, ward_name, wc, p, details))

    star_order = {"⭐⭐⭐": 0, "⭐⭐": 1, "⭐": 2}
    enriched.sort(key=lambda x: star_order.get(x[0], 3))

    # ── Summary table ──────────────────────────────────────────────────────────
    summary_rows = ""
    for stars, reason, wb, ward_name, wc, p, details in enriched:
        is_discount = p.get("normal_rent_yen", 0) and p["normal_rent_yen"] != p["rent_yen"]
        rent_str = f"¥{p['rent_yen']:,}" + (" 🔖" if is_discount else "")
        yr_m = re.search(r'(\d{4})年', details.get("building_age", ""))
        yr = yr_m.group(1) if yr_m else "不明"
        commute = p.get("commute_lines", [])
        commute_short = re.search(r'まで(\d+)分', commute[0]).group(0)[2:] if commute and re.search(r'まで(\d+)分', commute[0]) else "?"
        nearest = p.get("nearest_stations", [])
        walk_short = re.search(r'徒歩(\d+)', nearest[0]).group(0) if nearest and re.search(r'徒歩(\d+)', nearest[0]) else "?"
        summary_rows += f"""
        <tr style="border-bottom:1px solid #eee">
          <td style="padding:7px 10px;font-weight:bold;font-size:13px">{p['name']}</td>
          <td style="padding:7px 8px;text-align:center">{stars}</td>
          <td style="padding:7px 8px;text-align:center">
            <span style="background:{wc};color:#fff;border-radius:10px;padding:2px 7px;font-size:11px">{wb} {ward_name}</span>
          </td>
          <td style="padding:7px 8px;text-align:center;white-space:nowrap">{commute_short}</td>
          <td style="padding:7px 8px;text-align:center;white-space:nowrap">{walk_short}</td>
          <td style="padding:7px 8px;text-align:right;white-space:nowrap;font-size:13px">{rent_str}</td>
          <td style="padding:7px 8px;text-align:center;font-size:12px">{yr}</td>
        </tr>"""

    summary_table = f"""
    <table width="100%" style="border-collapse:collapse;border:1px solid #ddd;
           border-radius:8px;margin-bottom:24px;font-size:13px;overflow:hidden">
      <tr style="background:#2c3e50;color:#fff">
        <th style="padding:9px 10px;text-align:left">物件</th>
        <th style="padding:9px 8px">★</th>
        <th style="padding:9px 8px">エリア</th>
        <th style="padding:9px 8px">通勤</th>
        <th style="padding:9px 8px">徒歩</th>
        <th style="padding:9px 8px;text-align:right">家賃</th>
        <th style="padding:9px 8px">築年</th>
      </tr>
      {summary_rows}
    </table>"""

    # ── Individual cards ───────────────────────────────────────────────────────
    cards = ""
    for stars, reason, wb, ward_name, wc, p, details in enriched:
        is_discount = p.get("normal_rent_yen", 0) and p["normal_rent_yen"] != p["rent_yen"]
        rent_cell = (
            f"<s style='color:#aaa'>¥{p['normal_rent_yen']:,}</s> → "
            f"<strong style='color:#c0392b;font-size:16px'>¥{p['rent_yen']:,}</strong>"
            f"<span style='color:#888;font-size:12px'> ({p.get('discount_period','')} discount)</span>"
            if is_discount else
            f"<strong style='color:#c0392b;font-size:16px'>¥{p['rent_yen']:,}</strong>"
        )
        label_color = "#c0392b" if "Special" in p["label"] else "#2980b9"
        reno_badge  = ("<span style='background:#27ae60;color:#fff;border-radius:3px;"
                       "padding:1px 5px;font-size:11px;margin-left:4px'>🔧 リノベ済</span>"
                       if details.get("renovation") else "")

        # Commute line
        commute_parts = []
        for line in p.get("commute_lines", []):
            commute_parts.append(f"<strong>{line}</strong>")
        commute_html = " &nbsp;·&nbsp; ".join(commute_parts[:2]) if commute_parts else "通勤情報なし"

        # Walk + build year
        nearest = p.get("nearest_stations", [])
        walk_html = " &nbsp;·&nbsp; ".join(f"<strong>{st}</strong>" for st in nearest[:2]) if nearest else ""
        yr_m = re.search(r'(\d{4})年', details.get("building_age", ""))
        built_str = f"🏗 {yr_m.group(1)}年" if yr_m else f"🏗 {details.get('building_age','不明')}"
        if details.get("renovation"): built_str += " (リノベ済)"

        # Skip reason
        reason_html = (f"<div style='padding:4px 14px 8px;font-size:12px;color:#e67e22'>⚠️ {reason}</div>"
                       if reason else "")

        # Maps link
        maps_link = (f'<a href="{details["maps_url"]}" style="color:#2980b9;font-size:13px">📍 Google Maps</a>'
                     if details.get("maps_url") else "")

        # Sales center
        sc_html = (f"<div style='margin-top:4px;color:#666;font-size:12px'>🏢 {details['sales_center']}</div>"
                   if details.get("sales_center") else "")

        border_color = {"⭐⭐⭐": "#27ae60", "⭐⭐": "#2980b9", "⭐": "#95a5a6"}.get(stars, "#ddd")

        cards += f"""
        <div style="border:2px solid {border_color};border-radius:8px;margin-bottom:18px;font-family:sans-serif;overflow:hidden">
          <div style="background:#f8f9fa;padding:10px 14px;border-bottom:1px solid #eee">
            <span style="background:{label_color};color:#fff;border-radius:3px;padding:2px 6px;font-size:11px">{p['label']}</span>
            {reno_badge}
            <span style="font-size:16px;margin-left:6px">{stars}</span>
            <strong style="font-size:15px;margin-left:6px">{p['name']}</strong>
            <span style="background:{wc};color:#fff;border-radius:10px;padding:2px 8px;font-size:11px;margin-left:8px">{wb} {ward_name}</span>
          </div>
          <div style="padding:9px 14px;background:#fafafa;border-bottom:1px solid #eee;font-size:13px">
            🚃 {commute_html}
          </div>
          <div style="padding:6px 14px;background:#fafafa;border-bottom:1px solid #eee;font-size:13px">
            🚶 {walk_html} &nbsp;·&nbsp; {built_str}
          </div>
          <div style="padding:10px 14px">
            <span style="font-size:16px;font-weight:bold">{p['madori']}</span>
            &nbsp; {p['sqm']}㎡ &nbsp;·&nbsp;
            {rent_cell}
          </div>
          {reason_html}
          <div style="padding:8px 14px;border-top:1px solid #eee">
            <a href="{p['url']}"
               style="background:{label_color};color:#fff;padding:7px 14px;border-radius:4px;
                      text-decoration:none;font-weight:bold;font-size:14px">
              🔗 物件詳細を見る
            </a>
            &nbsp;&nbsp;{maps_link}
            {sc_html}
          </div>
        </div>"""

    html = f"""<html><body style="font-family:sans-serif;max-width:620px;margin:0 auto;padding:16px;background:#fff">
    <h2 style="color:#2c3e50;margin-bottom:16px">🏠 {len(enriched)} New UR Listing(s)</h2>
    {summary_table}
    {cards}
    <p style="color:#aaa;font-size:11px;margin-top:8px">
      {datetime.now().strftime('%Y-%m-%d %H:%M')} JST ·
      max ¥{int(MAX_RENT_YEN * 10000):,}/mo · {', '.join(ALLOWED_MADORI) or 'any layout'}
    </p>
    </body></html>"""

    msg = MIMEMultipart("alternative")
    msg["From"], msg["To"] = GMAIL_ADDRESS, NOTIFY_EMAIL
    msg["Subject"] = f"[UR Alert] {len(new_props)} new matching listing(s)"
    msg.attach(MIMEText(html, "html", "utf-8"))
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
            s.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
            s.send_message(msg)
        print("Email: ✓")
    except Exception as e:
        print(f"Email error: {e}")


def main():
    print(f"=== UR Watcher {datetime.now():%Y-%m-%d %H:%M} ===")
    seen = load_seen()

    all_listings: dict[str, dict] = {}
    for source in SEARCH_SOURCES:
        for prop in scrape_listings(source["url"], source["label"], source["type"]):
            prop["prefecture_filter"] = source["prefecture_filter"]
            all_listings[prop["id"]] = prop

    new_props = [
        p for p in all_listings.values()
        if p["id"] not in seen and matches(p, p.get("prefecture_filter"))
    ]
    for pid in all_listings:
        seen.add(pid)
    save_seen(seen)

    print(f"Total unique scraped: {len(all_listings)} | New matching: {len(new_props)}")

    if not new_props:
        print("No new listings — done."); return

    notify_line(new_props)
    notify_email(new_props)


if __name__ == "__main__":
    main()
