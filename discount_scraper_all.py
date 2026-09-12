"""
Unified discount scraper: ATB + Varus + Velmart + Fayno market.

All four are official retailer sites serving plain server-rendered HTML with
real URL-based pagination and no Cloudflare/bot-check (unlike gotoshop.ua,
which is deliberately excluded). Plain `requests` + BeautifulSoup, no
browser needed anywhere in this script.

Requires: pip install requests beautifulsoup4 playwright --break-system-packages
          playwright install chromium
          (playwright/chromium is only used for Varus — see below)

Usage:
    python discount_scraper_all.py

Every product also carries a `raw_text` field: the full, untouched text
block scraped for that item, before any name/price/discount splitting.
That field is the source of truth — the structured fields (price,
discount_pct, etc.) are a best-effort convenience on top of it. This
means a regex mismatch on any one site loses precision, not the product
itself: whatever reads this JSON downstream (e.g. the daily-digest task)
can fall back to reading raw_text directly, the same way it already
reads ATB/Varus pages today.

STATUS as of the last run I reviewed: ATB, Velmart, and Fayno market are
all verified against real HTML and produce clean structured data. Varus
required a different fix entirely — it's a Vue Storefront single-page
app whose server-rendered HTML has zero prices; products only appear
after client-side JS runs. So scrape_varus() now uses Playwright
(Chromium) to actually render the page, while the other three stay on
plain `requests` (no browser needed for them). This means the workflow
now needs `playwright install chromium` again, but ATB/Velmart/Fayno's
speed is unaffected — only Varus pays the browser-startup cost.

I still don't have a confirmed example of Varus's *rendered* DOM (only
confirmed that the *unrendered* HTML is empty of prices), so
VARUS_PRICE_RE is still a generic guess and scrape_varus_source() now
saves debug/varus_<category>.html from the POST-RENDER page content
every run — if this comes back with 0 products again, that file will
finally show the real rendered markup instead of the empty SPA shell.
"""

import json
import os
import re
import time
from dataclasses import dataclass, asdict

import requests
from bs4 import BeautifulSoup

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"
}
MAX_PAGES = 20


@dataclass
class Product:
    store: str
    category: str
    name: str
    price: str | None
    old_price: str | None
    discount_pct: str | None
    date_range: str | None
    url: str
    raw_text: str  # always the full, untouched text block for this product —
    # the fallback of record. The digest-writing step (Claude reading this
    # JSON) can parse this with its own judgment exactly like it already
    # does when fetching ATB/Varus pages directly, so a wrong/missing
    # structured field never means the product itself is lost.


def save_debug(store: str, slug: str, html: str) -> None:
    os.makedirs("debug", exist_ok=True)
    path = f"debug/{store}_{slug}.html"
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"    saved {path} for inspection")


# ---- ATB ----------------------------------------------------------------

ATB_SOURCES = {
    "Економія": "https://www.atbmarket.com/catalog/economy",
    "Новинки": "https://www.atbmarket.com/catalog/novetly",
    "Акція 7 днів (Жовті Води)": "https://www.atbmarket.com/jovti-vody/catalog/388-aktsiya-7-dniv",
}

# Real per-item text confirmed from a live run — no "(Гривня)" label exists
# in the actual markup (that was an artifact of a different extraction path
# I'd used earlier), and the price is split into two text nodes with a
# space where the decimal point would visually be:
#   "56. 50 грн /шт 99. 60"   (шт, with old price)
#   "219. 89 грн /кг 333. 99" (кг, with old price)
#   "90. 50 грн /шт"          (no discount -> no old price)
ATB_PRICE_RE = re.compile(
    r"(\d+)\.\s+(\d{2})\s*грн\s*/(шт|кг)(?:\s+(\d+)\.\s+(\d{2}))?"
)
ATB_DISCOUNT_RE = re.compile(r"-(\d+)%")
MAX_CONTAINER_TEXT_LEN = 3000  # guard against climbing all the way to a
# page-wide container when no match is found — better to give up with a
# smaller, wrong-but-bounded text than silently grab the whole catalog
# page (which is what happened when the old regex could never match).


def scrape_atb_source(session: requests.Session, category: str, base_url: str) -> list[Product]:
    products: dict[str, Product] = {}
    for page in range(1, MAX_PAGES + 1):
        url = base_url if page == 1 else f"{base_url}?page={page}"
        resp = session.get(url, headers=HEADERS, timeout=20)
        print(f"  [atb:{category}] page {page}: HTTP {resp.status_code}, {len(resp.text)} bytes")
        if resp.status_code != 200:
            print(f"  [atb:{category}] page {page}: stopping (non-200)")
            break

        soup = BeautifulSoup(resp.text, "html.parser")
        links = soup.select("a[href*='/product/']")
        print(f"  [atb:{category}] page {page}: {len(links)} raw /product/ links found")

        # Each product has 2 anchors (image + name) sharing the same href —
        # group them so we can prefer the one with visible text for the name.
        anchors_by_href: dict[str, list] = {}
        for a in links:
            anchors_by_href.setdefault(a["href"], []).append(a)

        new_count = 0
        for href, anchor_list in anchors_by_href.items():
            if href in products:
                continue

            name = None
            for a in anchor_list:
                t = a.get_text(strip=True)
                if t:
                    name = t
                    break
            if not name:
                img = anchor_list[0].find("img")
                if img and img.get("alt"):
                    name = img["alt"].strip()
            name = name or href.rstrip("/").rsplit("/", 1)[-1]

            # Climb from the first anchor, but bail out (keeping the last
            # bounded text) rather than let a never-matching regex escalate
            # all the way to a page-wide container.
            container = anchor_list[0]
            text = ""
            for _ in range(8):
                parent = container.find_parent()
                if parent is None:
                    break
                candidate = parent.get_text(" ", strip=True)
                if len(candidate) > MAX_CONTAINER_TEXT_LEN:
                    break  # stop climbing; keep whatever `text` already has
                container, text = parent, candidate
                if ATB_PRICE_RE.search(text):
                    break

            price_m = ATB_PRICE_RE.search(text) if text else None
            discount_m = ATB_DISCOUNT_RE.search(text) if text else None

            products[href] = Product(
                store="ATB",
                category=category,
                name=name,
                price=f"{price_m.group(1)}.{price_m.group(2)}" if price_m else None,
                old_price=f"{price_m.group(4)}.{price_m.group(5)}" if price_m and price_m.group(4) else None,
                discount_pct=discount_m.group(1) if discount_m else None,
                date_range=None,
                url=href if href.startswith("http") else f"https://www.atbmarket.com{href}",
                raw_text=text or name,
            )
            if price_m:
                new_count += 1

        print(f"  [atb:{category}] page {page}: {new_count} with a parsed price")
        if len(links) == 0:
            # Genuinely nothing on the page (not just a parsing miss) —
            # save it so we can tell a real block/redirect apart from a
            # regex problem.
            save_debug("atb", f"{category.replace(' ', '_')}_p{page}", resp.text)
            break
        if page > 1 and new_count == 0:
            break
        time.sleep(0.5)

    return list(products.values())


def scrape_atb(session: requests.Session) -> list[Product]:
    all_products: list[Product] = []
    for category, url in ATB_SOURCES.items():
        all_products.extend(scrape_atb_source(session, category, url))
    return all_products


# ---- Varus (requires a real browser — see module docstring) -------------
#
# Confirmed from real HTML: varus.ua is a Vue Storefront single-page app.
# The server-rendered HTML contains zero prices and zero "грн" mentions —
# products are fetched by client-side JS from an internal /api/catalog
# (Elasticsearch) endpoint after the page loads. This is architecturally
# different from ATB/Velmart/Fayno (all genuine server-rendered HTML) and
# from gotoshop.ua (blocked by Cloudflare) — Varus isn't blocking anything,
# it just has nothing to scrape without running its JavaScript. Requires
# Playwright + Chromium, unlike the rest of this script.

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

VARUS_SOURCES = {
    "Знижки до -40%": "https://varus.ua/dnipro/znizhki-do-40-na-tovari-dlya-litnogo-nastroyu",
    "Хіти з новою поштою": "https://varus.ua/dnipro/hiti-yaki-vozimo-novoyu-poshtoyu",
    "Тиждень покупок": "https://varus.ua/dnipro/weekly-shopping",
    "Ціна тижня": "https://varus.ua/dnipro/price-of-the-week",
}

# Generic — I have no confirmed rendered-DOM example yet, only the (empty)
# static HTML. This is deliberately loose; refine once debug/varus_*.html
# from THIS version (saved from the JS-rendered page, not the static one)
# comes back.
VARUS_PRICE_RE = re.compile(r"(\d+[.,]\d{2})\s*грн")
VARUS_DISCOUNT_RE = re.compile(r"-(\d+)%")


def scrape_varus_source(browser, category: str, url: str) -> list[Product]:
    products: dict[str, Product] = {}
    page = browser.new_page(locale="uk-UA")
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=45000)
        try:
            # Wait for actual price text to show up post-render.
            page.wait_for_function(
                "document.body.innerText.includes('грн')", timeout=15000
            )
        except PWTimeout:
            print(f"  [varus:{category}] no 'грн' text appeared within 15s after render")

        html = page.content()
        save_debug("varus", category.replace(" ", "_"), html)  # always, per prior request

        soup = BeautifulSoup(html, "html.parser")
        # Scan all links, then climb to find a container with a price —
        # same bounded-climb approach as ATB, since I don't have a
        # confirmed rendered-DOM sample to write a tighter selector yet.
        links = [a for a in soup.find_all("a", href=True) if "/" in a["href"]]
        seen = set()
        for a in links:
            href = a["href"]
            if href in seen or href in products:
                continue
            container = a
            text = ""
            for _ in range(8):
                parent = container.find_parent()
                if parent is None:
                    break
                candidate = parent.get_text(" ", strip=True)
                if len(candidate) > MAX_CONTAINER_TEXT_LEN:
                    break
                container, text = parent, candidate
                if VARUS_PRICE_RE.search(text):
                    break
            price_m = VARUS_PRICE_RE.search(text) if text else None
            if not price_m:
                continue
            seen.add(href)
            discount_m = VARUS_DISCOUNT_RE.search(text)
            name = a.get_text(strip=True) or href.rsplit("/", 1)[-1]

            products[href] = Product(
                store="Varus",
                category=category,
                name=name,
                price=price_m.group(1).replace(",", "."),
                old_price=None,
                discount_pct=discount_m.group(1) if discount_m else None,
                date_range=None,
                url=href if href.startswith("http") else f"https://varus.ua{href}",
                raw_text=text,
            )
        print(f"  [varus:{category}] {len(products)} products extracted from rendered page")
    finally:
        page.close()

    return list(products.values())


def scrape_varus(_session: requests.Session) -> list[Product]:
    all_products: list[Product] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        for category, url in VARUS_SOURCES.items():
            all_products.extend(scrape_varus_source(browser, category, url))
        browser.close()
    return all_products


# ---- Velmart --------------------------------------------------------------

VELMART_CATEGORIES = {
    "М'ясо": "m-40582",
    "Риба": "m-40585",
    "Овочі, фрукти, квіти, соління": "m-40591",
    "Яйця, молочні, заморожені": "m-40565",
    "Сири": "m-45179",
    "Ковбасні вироби": "s-40590",
    "Хліб": "m-40579",
    "Кондитерські, кава, чай": "m-40573",
    "Бакалія": "m-40558",
    "Напої безалкогольні": "m-40549",
    "Напої алкогольні": "m-46340",  # filtered to beer only
}

# Real markup order confirmed from a live run: "<hrn> <kop> <name> з DD.MM по DD.MM"
# — price digits come FIRST as two separate text nodes, not concatenated.
VELMART_RE = re.compile(
    r"^(\d{2,4})\s+(\d{2})\s+(.+?)(?:\s+з\s+(\d{2}\.\d{2})\s+по\s+(\d{2}\.\d{2}))?$"
)


def scrape_velmart_category(session: requests.Session, category: str, segment: str) -> list[Product]:
    products: dict[str, Product] = {}
    for page in range(1, MAX_PAGES + 1):
        url = f"https://velmart.ua/aktsijni-propozytsiyi/?segment={segment}&paged={page}"
        resp = session.get(url, headers=HEADERS, timeout=20)
        if resp.status_code != 200:
            print(f"  [velmart:{category}] page {page}: HTTP {resp.status_code} -> stop")
            break

        soup = BeautifulSoup(resp.text, "html.parser")
        links = soup.select("a[href*='/aktsijni-propozytsiyi/'][href$='/']")
        product_links = [a for a in links if "?" not in a.get("href", "") and a.get_text(strip=True)]

        new_count = 0
        skipped_nomatch = 0
        for a in product_links:
            href = a["href"]
            if href in products:
                continue
            text = a.get_text(" ", strip=True)
            m = VELMART_RE.match(text)
            if not m:
                # Nav/category links (e.g. "Акційні пропозиції") don't match
                # the price-prefixed pattern — skip rather than store junk.
                skipped_nomatch += 1
                continue

            products[href] = Product(
                store="Velmart",
                category=category,
                name=m.group(3).strip(),
                price=f"{m.group(1)}.{m.group(2)}",
                old_price=None,
                discount_pct=None,
                date_range=f"{m.group(4)} - {m.group(5)}" if m.group(4) else None,
                url=href if href.startswith("http") else f"https://velmart.ua{href}",
                raw_text=text,
            )
            new_count += 1

        print(f"  [velmart:{category}] page {page}: {len(product_links)} links, "
              f"{new_count} new, {skipped_nomatch} skipped (nav/non-product)")
        if new_count == 0:
            break
        time.sleep(0.5)

    return list(products.values())


def scrape_velmart(session: requests.Session) -> list[Product]:
    all_products: list[Product] = []
    for category, segment in VELMART_CATEGORIES.items():
        prods = scrape_velmart_category(session, category, segment)
        if category == "Напої алкогольні":
            prods = [p for p in prods if "пиво" in p.name.lower()]
            print(f"  [velmart] kept {len(prods)} beer items from alcohol category")
        all_products.extend(prods)
    return all_products


# ---- Fayno market -----------------------------------------------------

# Two promo templates confirmed from a live run:
#   "-NN%" template: "<name> -NN% <price> грн <old_price> грн <name again> <dates>"
#   "N+M" bundle template: "N+M <price> грн <bonus_price> грн за кожну ... <name> <dates>"
# Handle both generically instead of assuming one fixed order.
FAYNO_DATE_RE = re.compile(r"(\d{2}\.\d{2}\.\d{4})\s*-\s*(\d{2}\.\d{2}\.\d{4})")
FAYNO_DISCOUNT_RE = re.compile(r"-(\d+)%")
FAYNO_PRICE_ALL_RE = re.compile(r"([\d]+[.,]\d+)\s*грн")


def parse_fayno_block(text: str):
    date_m = FAYNO_DATE_RE.search(text)
    discount_m = FAYNO_DISCOUNT_RE.search(text)
    prices = FAYNO_PRICE_ALL_RE.findall(text)

    if discount_m and discount_m.start() > 3:
        # "-NN%" template — name sits before the marker.
        name = text[: discount_m.start()].strip()
    else:
        # Bundle template (or discount marker right at the start) — name
        # sits between the last price mention and the date range.
        last_price_end = 0
        for pm in FAYNO_PRICE_ALL_RE.finditer(text):
            last_price_end = pm.end()
        end = date_m.start() if date_m else len(text)
        name = text[last_price_end:end].strip()

    return {
        "name": name or text[:120],
        "price": prices[0] if prices else None,
        "old_price": prices[1] if len(prices) > 1 else None,
        "discount_pct": discount_m.group(1) if discount_m else None,
        "date_range": f"{date_m.group(1)} - {date_m.group(2)}" if date_m else None,
    }


def scrape_fayno(session: requests.Session) -> list[Product]:
    resp = session.get("https://fayno.market/discounts", headers=HEADERS, timeout=20)
    soup = BeautifulSoup(resp.text, "html.parser")

    section_links = sorted({
        a["href"] for a in soup.select("a[href*='/discounts/']")
        if a.get("href", "").count("/") == 4
    })
    print(f"  [fayno] found {len(section_links)} promotion sections")

    products: dict[str, Product] = {}
    for section_url in section_links:
        full_url = section_url if section_url.startswith("http") else f"https://fayno.market{section_url}"
        resp = session.get(full_url, headers=HEADERS, timeout=20)
        soup = BeautifulSoup(resp.text, "html.parser")
        section_name = soup.title.string.strip() if soup.title else section_url

        links = soup.select(f"a[href^='{section_url}/']")
        new_count = 0
        for a in links:
            href = a["href"]
            if href in products:
                continue
            text = a.get_text(" ", strip=True)
            parsed = parse_fayno_block(text)

            products[href] = Product(
                store="Fayno market",
                category=section_name,
                name=parsed["name"],
                price=parsed["price"],
                old_price=parsed["old_price"],
                discount_pct=parsed["discount_pct"],
                date_range=parsed["date_range"],
                url=href if href.startswith("http") else f"https://fayno.market{href}",
                raw_text=text,
            )
            new_count += 1
        print(f"  [fayno:{section_name}] {len(links)} links, {new_count} new")
        time.sleep(0.5)

    return list(products.values())


# ---- Main ---------------------------------------------------------------

def main():
    session = requests.Session()
    results: dict[str, list[dict]] = {}

    print("=== ATB ===")
    results["atb"] = [asdict(p) for p in scrape_atb(session)]

    print("\n=== Varus ===")
    results["varus"] = [asdict(p) for p in scrape_varus(session)]

    print("\n=== Velmart ===")
    results["velmart"] = [asdict(p) for p in scrape_velmart(session)]

    print("\n=== Fayno market ===")
    results["fayno_market"] = [asdict(p) for p in scrape_fayno(session)]

    os.makedirs("data", exist_ok=True)
    out_path = "data/all_stores_results.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)

    total = sum(len(v) for v in results.values())
    for store, items in results.items():
        print(f"  {store}: {len(items)} products")
    print(f"\nSaved {total} products total -> {out_path}")


if __name__ == "__main__":
    main()
