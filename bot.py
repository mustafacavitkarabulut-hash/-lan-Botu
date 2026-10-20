"""İŞKUR kurum dışı kamu işçi alımı ilanlarını kontrol edip yeni ilanları Telegram'a bildirir.

Çalışma mantığı:
  1. Sayfayı gerçek bir tarayıcıyla (Playwright) açar, harita (81 il) tamamen yüklenene kadar bekler.
  2. İlanı olan her ili (haritadaki sayıya göre) tek tek seçer ve açılan ilanları okur.
  3. seen.json içindeki önceki ilanlarla karşılaştırır.
  4. Yeni ilanları Telegram'a yollar, seen.json'u günceller.

İlk çalıştırmada mevcut ilanlar sadece kaydedilir, bildirim yağmuru olmaz.
İlk çalıştırmada ya da hiç ilan okunamazsa debug/ klasörüne sayfa ve ağ kaydı bırakır.
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
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

TOKEN = os.environ["TELEGRAM_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
DEBUG = os.environ.get("DEBUG") == "1"
FIRST_RUN = not STATE_FILE.exists()

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

PROVINCES_JS = """
() => [...document.querySelectorAll('svg#svg-turkiye-haritasi g[data-iladi]')].map(g => ({
  name: g.dataset.iladi,
  count: parseInt(g.dataset.count || '0', 10) || 0
}))
"""

SELECT_JS = """
(name) => {
  const s = document.querySelector('#city-dropdown');
  if (!s) return false;
  s.value = name;
  s.dispatchEvent(new Event('change', {bubbles: true}));
  if (window.jQuery) { try { window.jQuery(s).trigger('change'); } catch (e) {} }
  return true;
}
"""

CLICK_JS = """
(name) => {
  const g = [...document.querySelectorAll('svg#svg-turkiye-haritasi g[data-iladi]')]
    .find(x => x.dataset.iladi === name);
  if (!g) return false;
  const t = g.querySelector('path') || g;
  for (const type of ['mousedown', 'mouseup', 'click']) {
    t.dispatchEvent(new MouseEvent(type, {bubbles: true, cancelable: true}));
  }
  return true;
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
    if (a.closest('nav, header, footer, svg')) return;
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


def extract(page):
    return [i for i in page.evaluate(EXTRACT_JS) if keep(i)]


def dump_debug(page, net, tag=""):
    DEBUG_DIR.mkdir(exist_ok=True)
    (DEBUG_DIR / f"page{tag}.html").write_text(page.content(), encoding="utf-8")
    page.screenshot(path=str(DEBUG_DIR / f"page{tag}.png"), full_page=True)
    (DEBUG_DIR / "network.json").write_text(
        json.dumps(net, ensure_ascii=False, indent=1), encoding="utf-8"
    )


def make_response_logger(net):
    skip_types = ("image", "font", "css", "javascript")
    skip_ext = (".js", ".css", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".woff", ".woff2", ".ttf")

    def on_response(r):
        try:
            u = r.url
            if "iskur.gov.tr" not in u or u.lower().split("?")[0].endswith(skip_ext):
                return
            ct = r.headers.get("content-type", "")
            if any(x in ct for x in skip_types):
                return
            body = ""
            if any(x in ct for x in ("json", "html", "text", "xml")):
                try:
                    body = r.text()[:400]
                except Exception:
                    pass
            net.append({
                "method": r.request.method,
                "url": u,
                "status": r.status,
                "type": ct,
                "post": (r.request.post_data or "")[:300],
                "body": body,
            })
        except Exception:
            pass

    return on_response


def load_page(page):
    """Haritanın 81 ili da gelene kadar bekler (sayfa yarım kalırsa yeniden dener)."""
    for attempt in range(1, 4):
        try:
            page.goto(URL, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_function(
                "document.querySelectorAll('svg#svg-turkiye-haritasi g[data-iladi]').length >= 81",
                timeout=60000,
            )
            try:
                page.wait_for_load_state("load", timeout=30000)
            except Exception:
                pass
            return True
        except Exception as e:
            print(f"Yükleme denemesi {attempt} başarısız: {e}", file=sys.stderr)
    return False


def wait_for_new(page, baseline, collected, timeout_ms=15000):
    """İl seçildikten sonra yeni ilanlar görünene kadar bekler (yavaş açılan liste için).

    Önceki ilin ilanları ekranda kalırsa onlar 'yeni' sayılmaz; sadece bu ile ait
    yeni satırlar görününce biter. Listenin geri kalanı da yüklensin diye bir kez
    daha bekleyip tekrar okur.
    """
    waited = 0
    while waited < timeout_ms:
        page.wait_for_timeout(500)
        waited += 500
        new = [i for i in extract(page) if key_of(i) not in baseline and key_of(i) not in collected]
        if new:
            page.wait_for_timeout(1500)
            return [i for i in extract(page) if key_of(i) not in baseline and key_of(i) not in collected]
    return []


def fetch_items():
    net = []
    collected = {}
    expected = 0
    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(locale="tr-TR", viewport={"width": 1366, "height": 900}, user_agent=UA)
        page = ctx.new_page()
        page.set_default_timeout(45000)
        page.on("response", make_response_logger(net))

        ok = load_page(page)
        provinces = page.evaluate(PROVINCES_JS) if ok else []
        targets = [x for x in provinces if x["count"] > 0]
        expected = sum(x["count"] for x in targets)
        print(f"Sayfa yüklendi: {ok}, il sayısı: {len(provinces)}, ilanlı il: {len(targets)}, haritadaki ilan: {expected}")

        baseline = {key_of(i) for i in extract(page)} if ok else set()
        sample_saved = False

        for prov in targets:
            name = prov["name"]
            try:
                page.evaluate(SELECT_JS, name)
                new = wait_for_new(page, baseline, collected)
                if not new:
                    page.evaluate(CLICK_JS, name)
                    new = wait_for_new(page, baseline, collected)
                for i in new:
                    i["il"] = name
                    collected[key_of(i)] = i
                if not sample_saved:
                    dump_debug(page, net, tag="_ilk_il")
                    sample_saved = True
                print(f"  {name}: haritada {prov['count']}, okunan yeni {len(new)}")
            except Exception as e:
                print(f"  {name}: hata {e}", file=sys.stderr)

        if DEBUG or FIRST_RUN or not collected:
            dump_debug(page, net)
        browser.close()

    return collected, expected


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
        line = f"[{item.get('il', '?')}] " + item["text"][:600]
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
    items, expected = fetch_items()
    if not items:
        print("Hiç ilan okunamadı; debug/ klasörüne sayfa ve ağ kaydı bırakıldı.", file=sys.stderr)
        sys.exit(1)

    seen = json.loads(STATE_FILE.read_text("utf-8")) if not FIRST_RUN else []
    seen_set = set(seen)
    new = [v for k, v in items.items() if k not in seen_set]
    print(f"Okunan: {len(items)}, haritadaki: {expected}, yeni: {len(new)}, ilk çalıştırma: {FIRST_RUN}")

    if FIRST_RUN:
        msg = f"✅ İlan botu kuruldu. Haritada {expected} ilan görünüyor, {len(items)} tanesini okuyabildim ve kaydettim."
        if expected and len(items) < expected * 0.7:
            msg += " Okuduğum sayı haritadakinden belirgin şekilde az, okuma ayarını düzeltmem gerekebilir."
        send(msg + " Bundan sonra sadece yeni eklenenleri yollayacağım.")
    elif new:
        send_new(new)

    seen = (seen + [k for k in items if k not in seen_set])[-MAX_SEEN:]
    STATE_FILE.write_text(json.dumps(seen, ensure_ascii=False, indent=0), "utf-8")


if __name__ == "__main__":
    main()
