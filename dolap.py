"""Dolap'ta belirlenen aramadaki yeni telefon ilanlarını Telegram'a bildirir.

Ayarlar ortam değişkenlerinden okunur (workflow dosyasında):
  DOLAP_URL   : Dolap'ta filtreyi uyguladığın arama sayfasının linki
  MAX_PRICE   : en yüksek fiyat (TL), varsayılan 1000
  MIN_PRICE   : en düşük fiyat (TL), varsayılan 0
İlk çalıştırmada mevcut ilanlar sadece kaydedilir, bildirim atılmaz.
"""

import json
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import urljoin

import requests

URL = os.environ.get("DOLAP_URL", "https://dolap.com/ara?q=telefon&sira=yeni").strip()
MAX_PRICE = int(os.environ.get("MAX_PRICE", "1000"))
MIN_PRICE = int(os.environ.get("MIN_PRICE", "0"))
STATE_FILE = Path("dolap_seen.json")
DEBUG_DIR = Path("debug")
MAX_SEEN = 3000
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

TOKEN = os.environ["TELEGRAM_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

PRICE_RE = re.compile(r"(\d{1,3}(?:\.\d{3})*|\d+)(?:,\d+)?\s*(?:TL|₺)", re.I)
# Telefon aramasında çıkan aksesuarları ele
SKIP_WORDS = (
    "kılıf", "kilif", "ekran koruyucu", "şarj", "sarj", "kablo", "kulaklık",
    "kulaklik", "adaptör", "tutucu", "stand", "powerbank", "airpods",
)

JS_EXTRACT = """
() => {
  const out = [];
  const seen = new Set();
  for (const a of document.querySelectorAll('a[href*="/urun/"]')) {
    const href = a.href;
    if (seen.has(href)) continue;
    seen.add(href);
    let node = a, text = a.innerText || '';
    for (let i = 0; i < 4 && !/(TL|₺)/.test(text) && node.parentElement; i++) {
      node = node.parentElement;
      text = node.innerText || '';
    }
    out.push({href, text});
  }
  return out;
}
"""


def parse_price(text):
    prices = []
    for m in PRICE_RE.findall(text):
        try:
            prices.append(int(m.replace(".", "")))
        except ValueError:
            pass
    return min(prices) if prices else None


def fetch_items():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        ctx = browser.new_context(locale="tr-TR", user_agent=UA, viewport={"width": 1280, "height": 2000})
        page = ctx.new_page()
        page.set_default_timeout(45000)
        raw = []
        html = ""
        for attempt in range(1, 4):
            try:
                page.goto(URL, wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(4000)
                # tembel yüklenen ilanlar için biraz aşağı kaydır
                for _ in range(3):
                    page.mouse.wheel(0, 2500)
                    page.wait_for_timeout(1200)
                raw = page.evaluate(JS_EXTRACT)
                html = page.content()
                if raw:
                    break
            except Exception as e:  # noqa: BLE001
                print(f"  Deneme {attempt} başarısız: {e}", file=sys.stderr)
            time.sleep(3)
        if not raw:
            DEBUG_DIR.mkdir(exist_ok=True)
            (DEBUG_DIR / "dolap.html").write_text(html or "", encoding="utf-8")
            try:
                page.screenshot(path=str(DEBUG_DIR / "dolap.png"))
            except Exception:  # noqa: BLE001
                pass
        browser.close()

    items = {}
    for r in raw:
        link = urljoin(URL, r["href"]).split("?")[0]
        text = re.sub(r"\s+", " ", r["text"]).strip()
        price = parse_price(text)
        title = PRICE_RE.sub("", text).strip(" -·|")[:140] or link.rsplit("/", 1)[-1]
        items[link] = {"link": link, "title": title, "price": price}
    return items


def send(text):
    r = requests.post(
        f"https://api.telegram.org/bot{TOKEN}/sendMessage",
        json={"chat_id": CHAT_ID, "text": text, "disable_web_page_preview": False},
        timeout=30,
    )
    r.raise_for_status()


def load_state():
    if not STATE_FILE.exists():
        return None
    try:
        return list(json.loads(STATE_FILE.read_text("utf-8")).get("keys", []))
    except Exception:  # noqa: BLE001
        return None


def save_state(keys):
    STATE_FILE.write_text(json.dumps({"keys": keys[-MAX_SEEN:]}, ensure_ascii=False), "utf-8")


def wanted(it):
    t = it["title"].lower()
    if any(w in t for w in SKIP_WORDS):
        return False
    if it["price"] is None:
        return False
    return MIN_PRICE <= it["price"] <= MAX_PRICE


def main():
    items = fetch_items()
    print(f"Sayfada ürün: {len(items)}")
    if not items:
        print("Hiç ürün okunamadı (engellenmiş ya da sayfa yapısı farklı olabilir).", file=sys.stderr)
        sys.exit(1)

    matching = {k: v for k, v in items.items() if wanted(v)}
    print(f"Fiyat/filtre uyan: {len(matching)}")

    seen = load_state()
    seen_set = set(seen or [])
    new = [v for k, v in matching.items() if k not in seen_set]

    if seen is None:
        send(
            f"✅ Dolap botu kuruldu. Şu an filtreye uyan {len(matching)} ilan var, "
            f"hepsini kaydettim. Bundan sonra {MAX_PRICE} TL ve altındaki yeni telefon "
            f"ilanlarını buraya yollayacağım."
        )
        seen = []
    else:
        for it in new:
            send(f"📱 Yeni Dolap ilanı: {it['price']} TL\n{it['title']}\n{it['link']}")
            time.sleep(0.5)

    save_state(seen + [k for k in matching if k not in seen_set])
    print(f"Yeni: {len(new)}")


if __name__ == "__main__":
    main()
