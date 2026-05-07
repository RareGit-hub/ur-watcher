#!/usr/bin/env python3
"""UR Chintai property watcher — Local + Playwright"""

import json, os, smtplib
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
        "prefecture_filter": ["東京", "神奈川"],  # Tokyo and Kanagawa only
    },
]

MAX_RENT_MAN_YEN = 18.0
ALLOWED_MADORI   = ["1R・1K", "1DK", "2K", "1LDK", "2LDK", "2DK"]

# ─────────────────────────────────────────────────────────────────────────────

GMAIL_ADDRESS      = os.environ.get("GMAIL_ADDRESS", "").strip()
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "").strip()
NOTIFY_EMAIL       = os.environ.get("NOTIFY_EMAIL", "").strip()
LINE_CHANNEL_TOKEN = "".join(os.environ.get("LINE_CHANNEL_TOKEN", "").split())  # removes all whitespace/newlines
LINE_USER_ID       = os.environ.get("LINE_USER_ID", "").strip()
DEBUG_MODE         = os.environ.get("DEBUG_MODE", "false").lower() == "true"
STATE_FILE         = Path("seen_ids.json")


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
                # Expand all prefecture accordions before scraping
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
                            const nameEl    = bukken && bukken.querySelector('h3, h4, [class*="name"], [class*="title"]');
                            const pref      = prefEl ? prefEl.textContent.trim() : '';
                            const name      = nameEl ? nameEl.textContent.trim() : '';
                            const text      = row.innerText || '';

                            // Filter out management fees (always under 10,000) to get actual rents
                            const allRents    = [...text.matchAll(/([\\d,]+)\\s*円/g)]
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

                            results.push({
                                id, name, pref,
                                normal_rent_yen:  normalYen,
                                rent_yen:         discountYen,
                                rent_man:         discountYen / 10000,
                                madori:           mado   ? mado[1]            : '不明',
                                sqm:              sqm    ? parseFloat(sqm[1]) : 0,
                                discount_period:  period ? period[1]          : '',
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
                            const href   = detailLink.getAttribute('href') || '';
                            const name   = card.innerText.split('\\n').map(s => s.trim()).filter(s => s)[0] || '';
                            const text   = room.innerText || '';
                            const rentMatch   = text.match(/([\\d,]+)\\s*円/);
                            const madoriMatch = text.match(/([1-9][LDKSR]+|ワンルーム)/);
                            const sqmMatch    = text.match(/([\\d.]+)\\s*㎡/);
                            const roomMatch   = text.match(/([\\d]+号棟[^\\d]*[\\d]+号室|[\\d]+号室)/);
                            const rentYen     = rentMatch ? parseInt(rentMatch[1].replace(/,/g,'')) : 0;
                            const id          = href.replace(/\\.html$/, '').split('/').pop().replace(/\\s/g,'');
                            results.push({
                                id,
                                name: name + (roomMatch ? ' ' + roomMatch[1].trim() : ''),
                                pref: '',
                                normal_rent_yen: 0,
                                rent_yen:  rentYen,
                                rent_man:  rentYen / 10000,
                                madori:    madoriMatch ? madoriMatch[1] : '不明',
                                sqm:       sqmMatch ? parseFloat(sqmMatch[1]) : 0,
                                discount_period: '',
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
        msg += f"\n■ [{p['label']}] {p['name']}\n  {p['madori']} {p['sqm']}㎡ / {rent_str}\n  {p['url']}\n"
    if len(new_props) > 5:
        msg += f"\n...and {len(new_props)-5} more (see email)"

    r = requests.post(
        "https://api.line.me/v2/bot/message/push",
        headers={
            "Authorization": f"Bearer {LINE_CHANNEL_TOKEN}",
            "Content-Type": "application/json",
        },
        json={
            "to": LINE_USER_ID,
            "messages": [{"type": "text", "text": msg}]
        },
        timeout=10,
    )
    print("LINE:", "✓" if r.status_code == 200 else f"✗ {r.status_code} {r.text}")


def notify_email(new_props: list[dict]) -> None:
    if not all([GMAIL_ADDRESS, GMAIL_APP_PASSWORD, NOTIFY_EMAIL]):
        print("Email credentials not set — skipping"); return

    rows = ""
    for p in new_props:
        is_discount = p.get("normal_rent_yen", 0) and p["normal_rent_yen"] != p["rent_yen"]
        rent_cell = (
            f"<s style='color:#aaa'>¥{p['normal_rent_yen']:,}</s><br>"
            f"<b style='color:#c0392b'>¥{p['rent_yen']:,}</b>"
            f"<br><small>({p.get('discount_period','')} discount)</small>"
            if is_discount else f"¥{p['rent_yen']:,}"
        )
        label_color = "#c0392b" if "Special" in p["label"] else "#2980b9"
        rows += f"""<tr>
            <td style="padding:8px;border:1px solid #ddd">
                <span style="background:{label_color};color:#fff;border-radius:3px;padding:2px 6px;font-size:11px">{p['label']}</span><br>
                {p['name']}
            </td>
            <td style="padding:8px;border:1px solid #ddd;text-align:center">{p['madori']}</td>
            <td style="padding:8px;border:1px solid #ddd;text-align:center">{p['sqm']}㎡</td>
            <td style="padding:8px;border:1px solid #ddd;text-align:right">{rent_cell}</td>
            <td style="padding:8px;border:1px solid #ddd;text-align:center"><a href="{p['url']}">View</a></td>
        </tr>"""

    html = f"""<html><body style="font-family:sans-serif;max-width:800px;margin:0 auto;padding:20px">
    <h2 style="color:#2c3e50">🏠 {len(new_props)} New UR Listing(s)</h2>
    <table style="width:100%;border-collapse:collapse">
        <thead><tr style="background:#f0f4f8">
            <th style="padding:8px;border:1px solid #ddd;text-align:left">Property</th>
            <th style="padding:8px;border:1px solid #ddd">Layout</th>
            <th style="padding:8px;border:1px solid #ddd">Size</th>
            <th style="padding:8px;border:1px solid #ddd">Rent/mo</th>
            <th style="padding:8px;border:1px solid #ddd">Link</th>
        </tr></thead>
        <tbody>{rows}</tbody>
    </table>
    <p style="color:#888;font-size:0.85em;margin-top:16px">
        Checked: {datetime.now().strftime('%Y-%m-%d %H:%M')} JST<br>
        Filters: max ¥{int(MAX_RENT_MAN_YEN * 10000):,}/mo · layouts: {', '.join(ALLOWED_MADORI) or 'any'}
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
