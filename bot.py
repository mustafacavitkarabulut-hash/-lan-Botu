"""İŞKUR kurum dışı kamu işçi alımı ilanlarını kontrol edip yeni ilanları Telegram'a bildirir.

Çalışma mantığı:
  1. Ana sayfadaki haritadan hangi illerde kaç ilan olduğunu okur.
  2. İlanı olan her ilin sayfasını (?idId=...&il=...) açıp ilan tablosunu okur.
  3. seen.json içindeki önceki ilanlarla karşılaştırır.
  4. Yeni ilanları Telegram'a yollar, seen.json'u günceller.

Sayfalar önce düz HTTP ile alınır; olmazsa Playwright tarayıcısına geçilir.
İlk çalıştırmada (ya da kayıt formatı değişince) mevcut ilanlar sadece kaydedilir.
"""

import hashlib
import html
import json
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import quote, urlencode, urljoin

import requests

URL = "https://www.iskur.gov.tr/ilanlar/kurumdisi-kamu-isci-alim-ilanlari/"
STATE_FILE = Path("seen.json")
STATE_VERSION = 2
DEBUG_DIR = Path("debug")
MAX_SEEN = 5000
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

TOKEN = os.environ["TELEGRAM_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

MAP_RE = re.compile(r'<g\s+id="([^"]+)"[^>]*?data-iladi="([^"]*)"[^>]*?data-count="(\d*)"', re.S)
ROW_RE = re.compile(r"<tr\b([^>]*)>(.*?)</tr>", re.S | re.I)
CELL_RE = re.compile(r"<td\b[^>]*>(.*?)</td>", re.S | re.I)
TAG_RE = re.compile(r"<[^>]+>")
DATE_RE = re.compile(r"\d{2}\.\d{2}\.\d{4}(?:\s+\d{2}:\d{2})?")
HREF_RE = re.compile(r'href="([^"]+)"', re.I)
FILE_RE = re.compile(r'data-file-url="([^"]*)"', re.I)


# ---------------------------------------------------------------- sayfa alma

def valid_main(text):
    return len(MAP_RE.findall(text)) >= 81 and "</html>" in text.lower()


def valid_province(text):
    return "table-title" in text and "</html>" in text.lower()


class Fetcher:
    def __init__(self):
        self.s = requests.Session()
        self.s.headers.update({
            "User-Agent": UA,
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "tr-TR,tr;q=0.9",
        })
        self._pw = self._browser = self._page = None

    def get(self, params, valid):
        err = None
        for attempt in range(1, 4):
            try:
                r = self.s.get(URL, params=params, timeout=(10, 60))
                r.raise_for_status()
                if valid(r.text):
                    return r.text
                err = "sayfa eksik geldi"
            except Exception as e:  # noqa: BLE001
                err = e
            print(f"  HTTP denemesi {attempt} başarısız: {err}", file=sys.stderr)
            time.sleep(2 * attempt)
        print("  Düz HTTP olmadı, tarayıcıya geçiliyor.", file=sys.stderr)
        return self._browser_get(params, valid)

    def _browser_get(self, params, valid):
        from playwright.sync_api import sync_playwright

        if self._page is None:
            self._pw = sync_playwright().start()
            self._browser = self._pw.chromium.launch()
            ctx = self._browser.new_context(locale="tr-TR", user_agent=UA)
            self._page = ctx.new_page()
            self._page.set_default_timeout(45000)
        full = URL + ("?" + urlencode(params) if params else "")
        for attempt in range(1, 4):
            try:
                self._page.goto(full, wait_until="load", timeout=60000)
                text = self._page.content()
                if valid(text):
                    return text
            except Exception as e:  # noqa: BLE001
                print(f"  Tarayıcı denemesi {attempt} başarısız: {e}", file=sys.stderr)
            time.sleep(2)
        raise RuntimeError(f"Sayfa alınamadı: {full}")

    def close(self):
        try:
            if self._browser:
                self._browser.close()
            if self._pw:
                self._pw.stop()
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------- ayrıştırma

def text_of(fragment):
    return re.sub(r"\s+", " ", html.unescape(TAG_RE.sub(" ", fragment))).strip()


def parse_map(main_html):
    provinces = {}
    for slug, name, count in MAP_RE.findall(main_html):
        provinces[slug] = (name, int(count or 0))
    return [(slug, name, count) for slug, (name, count) in provinces.items()]


def parse_province(page_html, il, slug):
    start = page_html.find("table-title")
    if start < 0:
        return []
    end = page_html.find("</table>", start)
    seg = page_html[start:end if end > 0 else None]
    page_link = URL + "?" + urlencode({"idId": slug, "il": il})
    rows = []
    for attrs, body in ROW_RE.findall(seg):
        cells = CELL_RE.findall(body)
        if len(cells) < 2:
            continue
        date_txt = text_of(cells[0])
        m = DATE_RE.search(date_txt)
        date = m.group(0) if m else date_txt
        title = text_of(cells[1])
        if not title:
            continue
        fm = FILE_RE.search(attrs)
        link = fm.group(1) if fm and fm.group(1) else ""
        if not link:
            hm = HREF_RE.search(body)
            link = hm.group(1) if hm else ""
        link = quote(urljoin(URL, link), safe=":/%?=&#") if link else page_link
        key = hashlib.sha1(f"{il}|{title}|{link}".encode("utf-8")).hexdigest()
        rows.append({"key": key, "il": il, "title": title, "date": date, "link": link})
    return rows


# ---------------------------------------------------------------- Telegram

def send(text):
    r = requests.post(
        f"https://api.telegram.org/bot{TOKEN}/sendMessage",
        json={"chat_id": CHAT_ID, "text": text, "disable_web_page_preview": True},
        timeout=30,
    )
    r.raise_for_status()


def send_new(new_items):
    chunk = f"🆕 İŞKUR kurum dışı işçi alımı: {len(new_items)} yeni ilan\n\n"
    for it in new_items:
        line = f"📍 {it['il']}\n{it['title'][:300]}\n"
        if it["date"]:
            line += f"Son tarih: {it['date']}\n"
        line += f"{it['link']}\n\n"
        if len(chunk) + len(line) > 3800:
            send(chunk.strip())
            chunk = ""
        chunk += line
    if chunk.strip():
        send(chunk.strip())


# ---------------------------------------------------------------- durum

def load_state():
    """Kayıtlı ilan anahtarlarını döndürür; dosya yoksa ya da eski formattaysa None."""
    if not STATE_FILE.exists():
        return None, False
    try:
        d = json.loads(STATE_FILE.read_text("utf-8"))
    except Exception:  # noqa: BLE001
        return None, True
    if isinstance(d, dict) and d.get("v") == STATE_VERSION:
        return list(d.get("keys", [])), True
    return None, True


def save_state(keys):
    STATE_FILE.write_text(
        json.dumps({"v": STATE_VERSION, "keys": keys[-MAX_SEEN:]}, ensure_ascii=False),
        "utf-8",
    )


# ---------------------------------------------------------------- ana akış

def main():
    f = Fetcher()
    items, failed = {}, []
    try:
        main_html = f.get(None, valid_main)
        provinces = parse_map(main_html)
        targets = [p for p in provinces if p[2] > 0]
        expected = sum(p[2] for p in targets)
        print(f"İl: {len(provinces)}, ilanlı il: {len(targets)}, haritadaki ilan: {expected}")

        for slug, name, count in targets:
            try:
                page_html = f.get({"idId": slug, "il": name}, valid_province)
            except Exception as e:  # noqa: BLE001
                print(f"  {name}: alınamadı ({e})", file=sys.stderr)
                failed.append(name)
                continue
            rows = parse_province(page_html, name, slug)
            if len(rows) != count:
                print(f"  {name}: haritada {count}, tabloda {len(rows)}", file=sys.stderr)
            if not rows and count > 0:
                DEBUG_DIR.mkdir(exist_ok=True)
                (DEBUG_DIR / f"{slug}.html").write_text(page_html, encoding="utf-8")
            for r in rows:
                items[r["key"]] = r
            time.sleep(0.4)
    finally:
        f.close()

    print(f"Okunan ilan: {len(items)}, haritadaki: {expected}, alınamayan il: {len(failed)}")
    if expected > 0 and not items:
        print("Hiç ilan okunamadı.", file=sys.stderr)
        sys.exit(1)

    seen, existed = load_state()
    first_run = seen is None
    seen_set = set(seen or [])
    new = [v for k, v in items.items() if k not in seen_set]

    if first_run:
        msg = (
            f"✅ İlan botu {'yeniden ' if existed else ''}kuruldu. Haritada {expected} ilan görünüyor, "
            f"{len(items)} tanesini okuyup kaydettim."
        )
        if failed:
            msg += f" Şu illere ulaşamadım, sonraki kontrolde tekrar denerim: {', '.join(failed)}."
        send(msg + " Bundan sonra sadece yeni eklenenleri yollayacağım.")
        seen = []
    elif new:
        send_new(new)

    save_state(seen + [k for k in items if k not in seen_set])
    print(f"Yeni: {len(new)}, ilk çalıştırma: {first_run}")


if __name__ == "__main__":
    main()
