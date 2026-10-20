"""İŞKUR kurum dışı kamu işçi alımı ilanlarını kontrol edip yeni ilanları Telegram'a bildirir.

Çalışma mantığı:
  1. Sayfayı gerçek bir tarayıcıyla (Playwright) açar, "Tüm İller" seçer.
  2. Listedeki ilanları okur (sayfalama varsa tüm sayfaları gezer).
  3. seen.json içindeki önceki ilanlarla karşılaştırır.
  4. Yeni ilanları Telegram'a yollar, seen.json'u günceller.

İlk çalıştırmada mevcut ilanlar sadece kaydedilir, bildirim yağmuru olmaz.
Liste hiç okunamazsa debug/ klasörüne sayfa kaydı bırakır ve hata ile çıkar.
"""

import hashlib
import json
import os
import sys
from pathlib import Path
from urllib.parse import urlparse

import requests
from playwright.sync_api import sync_playwright

URL = "https://www.iskur.gov.tr/ilanlar/kurumdisi-kamu-isci-alim-ilanlari/"
STATE_FILE = Path("seen.json")
DEBUG_DIR = Path("debug")
MAX_SEEN = 5000
MAX_PAGES = 30

TOKEN = os.environ["TELEGRAM_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
DEBUG = os.environ.get("DEBUG") == "1"

# Menü, alt bilgi ve gezinti bağlantıları ilan sayılmasın.
SKIP_PATH_PREFIXES = (
    "/kurumsal", "/kurumsal-bilgi", "/is-arayan", "/isveren", "/ozel-istihdam",
    "/il-mudurlukleri", "/iletisim", "/haberler", "/duyurular", "/sikca",
    "/hizmetler", "/en", "/ihaleler", "/yayinlarimiz", "/videolar",
)
SKIP_EXACT_PATHS = {
    "", "/", "/ilanlar",
    "/ilanlar/kurumdisi-kamu-isci-alim-ilanlari",
    "/ilanlar/kamu-memur-alim-ilanlari",
}
SKIP_HOSTS = ("facebook.com", "instagram.com", "x.com", "youtube.com", "teknofest.app", "apple.com", "google.com", "goo.gl")

SELECT_ALL_JS = """
() => {
  for (const s of document.querySelectorAll('select')) {
    for (const o of s.options) {
      if (o.text.trim().startsWith('Tüm İller')) {
        s.value = o.value;
        s.dispatchEvent(new Event('change', {bubbles: true}));
        return true;
      }
    }
  }
  return false;
}
"""

EXTRACT_JS = """
() => {
  const clean = s => s.replace(/\\s+/g, ' ').trim();
  const out = [];
  document.querySelectorAll('table tbody tr').forEach(tr => {
    const t = clean(tr.innerText);
    const a = tr.querySelector('a[href]');
    if (t) out.push({text: t, href: a ? a.href : '', mode: 'table'});
  });
  if (out.length) return out;
  document.querySelectorAll('a[href]').forEach(a => {
    if (a.closest('nav, header, footer')) return;
    const t = clean(a.innerText);
    if (t) out.push({text: t, href: a.href, mode: 'link'});
  });
  return out;
}
"""


def keep(item):
    if item["mode"] == "table":
        return True
    href = item["href"]
    if not href or href.startswith(("javascript", "#", "tel:", "mailto:")):
        return False
    u = urlparse(href)
    if any(h in (u.hostname or "") for h in SKIP_HOSTS):
        return False
    if (u.hostname or "").endswith("iskur.gov.tr"):
        path = u.path.rstrip("/")
        if path in SKIP_EXACT_PATHS or path.startswith(SKIP_PATH_PREFIXES):
            return False
    return True


def key_of(item):
    if item["href"] and item["mode"] == "link":
        return item["href"]
    base = item["href"] + "|" + item["text"] if item["href"] else item["text"]
    return hashlib.sha1(base.encode("utf-8")).hexdigest()


def dump_debug(page):
    DEBUG_DIR.mkdir(exist_ok=True)
    (DEBUG_DIR / "page.html").write_text(page.content(), encoding="utf-8")
    page.screenshot(path=str(DEBUG_DIR / "page.png"), full_page=True)


def fetch_items():
    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(locale="tr-TR", viewport={"width": 1366, "height": 900})
        page = ctx.new_page()
        page.set_default_timeout(45000)
        page.goto(URL, wait_until="networkidle", timeout=60000)
        page.wait_for_timeout(2000)

        page.evaluate(SELECT_ALL_JS)
        page.wait_for_timeout(3000)

        items = []
        for _ in range(MAX_PAGES):
            items += page.evaluate(EXTRACT_JS)
            nxt = page.query_selector(
                ".paginate_button.next:not(.disabled), a[rel=next], li.next:not(.disabled) a"
            )
            if not nxt:
                break
            nxt.click()
            page.wait_for_timeout(1500)

        items = [i for i in items if keep(i)]
        if DEBUG or not items:
            dump_debug(page)
        browser.close()

    unique = {}
    for i in items:
        unique.setdefault(key_of(i), i)
    return unique


def send(text):
    r = requests.post(
        f"https://api.telegram.org/bot{TOKEN}/sendMessage",
        json={"chat_id": CHAT_ID, "text": text, "disable_web_page_preview": True},
        timeout=30,
    )
    r.raise_for_status()


def send_new(new_items):
    header = f"🆕 İŞKUR kurum dışı işçi alımı: {len(new_items)} yeni ilan\n\n"
    chunk = header
    for item in new_items:
        line = item["text"][:600]
        if item["href"]:
            line += f"\n{item['href']}"
        line += "\n\n"
        if len(chunk) + len(line) > 3800:
            send(chunk.strip())
            chunk = ""
        chunk += line
    if chunk.strip():
        send(chunk.strip())


def main():
    items = fetch_items()
    if not items:
        print("Hiç ilan okunamadı; debug/ klasörüne sayfa kaydedildi.", file=sys.stderr)
        sys.exit(1)

    first_run = not STATE_FILE.exists()
    seen = json.loads(STATE_FILE.read_text("utf-8")) if not first_run else []
    seen_set = set(seen)
    new = [v for k, v in items.items() if k not in seen_set]
    print(f"Okunan: {len(items)}, yeni: {len(new)}, ilk çalıştırma: {first_run}")

    if first_run:
        send(f"✅ İlan botu kuruldu. Şu an listede {len(items)} ilan var, bunlar kaydedildi. Bundan sonra sadece yeni eklenenleri yollayacağım.")
    elif new:
        send_new(new)

    seen = (seen + [k for k in items if k not in seen_set])[-MAX_SEEN:]
    STATE_FILE.write_text(json.dumps(seen, ensure_ascii=False, indent=0), "utf-8")


if __name__ == "__main__":
    main()
