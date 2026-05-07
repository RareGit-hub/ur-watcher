#!/usr/bin/env python3
"""UR Chintai property watcher — GitHub Actions + Playwright"""

import json, os, smtplib, sys
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import requests
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

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
        "prefecture_filter": ["関東"],  # Kanto region only
    },
]

MAX_RENT_MAN_YEN = 15.0            # Max monthly rent in 万円 (e.g. 13.0 = ¥130,000)
ALLOWED_MADORI   = ["1R・1K", "1DK", "2K", "1LDK", "2LDK", "2DK"]  # Room types — empty list means accept all

# ─────────────────────────────────────────────────────────────────────────────

GMAIL_ADDRESS      = os.environ.get("GMAIL_ADDRESS", "")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")
NOTIFY_EMAIL       = os.environ.get("NOTIFY_EMAIL", "")
LINE_NOTIFY_TOKEN  = os.environ.get("LINE_NOTIFY_TOKEN", "")
DEBUG_MODE         = os.environ.get("DEBUG_MODE", "false").lower() == "true"
STATE_FILE         = Path("seen_ids.json")


def load_seen() -> set:
    return set(json.loads(STATE_FILE.read_text())) if STATE_FILE.exists() else set()

def save_seen(seen: set) -> None:
    STATE_FILE.write_text(json.dumps(sorted(seen), ensure_ascii=False, indent=2))


def scrape_listings(url: str, label: str = "", source_type: str = "regular") -> list[dict]:
    props = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        ).new_page()
        try:
            print(f"  Loading [{source_type}] {url[:80]}...")
            page.goto(url, wait_until="networkidle", timeout=60_000)
            page.wait_for_timeout(4_000)

            if DEBUG_MODE:
                page.screenshot(path=f"debug_{source_type}.png", full_page=True)
                Path(f"debug_{source_type}.html").write_text(page.content(), encoding="utf-8")
                print(f"  Debug saved: debug_{source_type}.png / .html")

            if source_type == "tokubetsu":
                props = page.evaluate("""() => {
                    const results = [];
                    document.querySelectorAll('.js-tokubetsu-bukken-row').forEach(row => {
                        try {
                            const bukken = row.closest('.js-tokubetsu-bukken');
                            const block  = row.closest('.sec_tokubetsu_02, .js-tokubetsu-block');
                            const prefEl = block  && block.querySelector('h2, h3, [class*="title"]');
                            const nameEl = bukken && bukken.querySelector('h3, h4, [class*="name"], [class*="title"]');
                            const pref   = prefEl ? prefEl.textContent.trim() : '';
                            const name   = nameEl ? nameEl.textContent.trim() : '';
                            const text   = row.innerText || '';

                            // Two rents: [0] = normal rent, [1] = discounted rent
                            const allRents    = [...text.matchAll(/([\\d,]+)\\s*円/g)];
                            const normalYen   = allRents[0] ? parseInt(allRents[0][1].replace(/,/g,'')) : 0;
                            const discountYen = allRents[1] ? parseInt(allRents[1][1].replace(/,/g,'')) : normalYen;

                            const mado   = text.match(/([1-9][LDKSR]+|ワンルーム)/);
                            const sqm    = text.match(/([\\d.]+)\\s*㎡/);
                            const floor  = text.match(/([\\d]+)階/);
                            const period = text.match(/(\\d+年)/);
                            const link   = row.querySelector('a') || (bukken && bukken.querySelector('a'));
                            const href   = link ? (link.getAttribute('href') || '') : '';
                            const id     = ('tokubetsu_' + name + '_' + (mado ? mado[1] : '') + '_' + (floor ? floor[1] : '')).replace(/\\s/g,'');

                            results.push({
                                id,
                                name,
                                pref,
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
                props = page.evaluate("""() => {
                    const results = [];
                    const SELECTORS = [
                        '.casset-item', '.bukken-list-item', '[class*="casset"]',
                        '.item-cassette', 'li[class*="bukken"]', 'article'
                    ];
                    let items = [];
                    for (const sel of SELECTORS) {
                        items = document.querySelectorAll(sel);
                        if (items.length > 0) { console.log('Selector used:', sel); break; }
                    }
                    items.forEach(el => {
                        try {
                            const link = el.querySelector('a[href*="/chintai/"]') || el.querySelector('a');
                            if (!link) return;
                            const href = link.getAttribute('href') || '';
                            const id   = href.replace(/\\/$/, '').split('/').slice(-2).join('/');
                            const text = (el.innerText || '').replace(/\\s+/g, ' ');
                            const rent = text.match(/([\\d,]+)\\s*円/);
                            const mado = text.match(/([1-9][LDKSR]+|ワンルーム)/);
                            const sqm  = text.match(/([\\d.]+)\\s*㎡/);
                            const nameEl = el.querySelector('h2,h3,h4,[class*="name"],[class*="title"]');
                            results.push({
                                id,
                                name:             nameEl ? nameEl.textContent.trim() : id,
                                pref:             '',
                                normal_rent_yen:  0,
                                rent_yen:         rent ? parseInt(rent[1].replace(/,/g,'')) : 0,
                                rent_man:         rent ? parseInt(rent[1].replace(/,/g,'')) / 10000 : 0,
                                madori:           mado ? mado[1]            : '不明',
                                sqm:              sqm  ? parseFloat(sqm[1]) : 0,
                                discount_period:  '',
                                url: href.startsWith('http') ? href : 'https://www.ur-net.go.jp' + href,
                            });
                        } catch(e) {}
                    });
                    return results;
                }""")

            for p in props:
                p["label"] = label
            print(f"  → {len(props)} listings found")

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
    if not LINE_NOTIFY_TOKEN:
        print("LINE token not set — skipping"); return

    msg = f"\n🏠 {len(new_props)} new UR listing(s)\n"
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
        "https://notify-api.line.me/api/notify",
        headers={"Authorization": f"Bearer {LINE_NOTIFY_TOKEN}"},
        data={"message": msg}, timeout=10
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
