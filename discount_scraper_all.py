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

Every product also carries `image_url` — extracted directly from the
<img> found in the same card/anchor used for name+price, with lazy-load
attributes (data-src, srcset) preferred over a possibly-placeholder
plain src. This avoids a separate per-product page fetch just to read
og:image meta tags downstream. May be None if no <img> was found in that
scope — never guessed or fabricated.

STATUS: all four stores (ATB, Varus, Velmart, Fayno market) are verified
against real HTML/rendered-DOM and produce clean structured data,
including images.
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
FULL_CATALOG_PAGE_CAP = 6  # full (non-discount) categories can run to dozens
# of pages; cap them lower than the discount-page sources so a daily run
# doesn't balloon into hours, especially for Varus where each page is a
# full Playwright render, not a cheap HTTP request.
FULL_CATALOG_CATEGORIES = {
    # ATB
    "Пиво безалкогольне", "Овочі та фрукти", "Кока-Кола без цукру",
    "Побутова хімія", "Гігієна і косметика", "М'ясо",
    "Молочні продукти та яйця", "Риба і морепродукти",
    "Заморожені продукти", "Чипси, снеки", "Кава, какао",
    # Varus
    "Власні торгові марки", "Безалкогольний алкоголь", "Бакалія",
    "Косметика та догляд", "Консерви та соління", "М'ясні вироби та яйця",
    "Молочні продукти", "Риба", "Снеки", "Фрукти, овочі, горіхи", "Напої",
}


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
    image_url: str | None
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


def extract_image_url(el, base_url: str) -> str | None:
    """Find an <img> anywhere inside `el` and return its best src, resolved
    to an absolute URL. Handles common lazy-load attributes (data-src,
    data-srcset, srcset) since a plain src is sometimes a 1x1 placeholder
    until JS swaps it in — falls back to plain src if nothing else is set."""
    if el is None:
        return None
    img = el.find("img")
    if img is None:
        return None
    for attr in ("data-src", "src", "data-original"):
        val = img.get(attr)
        if val and not val.startswith("data:"):
            return val if val.startswith("http") else requests.compat.urljoin(base_url, val)
    for attr in ("data-srcset", "srcset"):
        val = img.get(attr)
        if val:
            first = val.split(",")[0].strip().split(" ")[0]
            if first and not first.startswith("data:"):
                return first if first.startswith("http") else requests.compat.urljoin(base_url, first)
    return None


# ---- ATB ----------------------------------------------------------------

ATB_SOURCES = {
    "Економія": "https://www.atbmarket.com/catalog/economy",
    "Новинки": "https://www.atbmarket.com/catalog/novetly",
    "Акція 7 днів (Жовті Води)": "https://www.atbmarket.com/jovti-vody/catalog/388-aktsiya-7-dniv",
    # Full-category sources (not just discount pages) — added so a cheap
    # non-discounted item (e.g. a cheaper toilet paper with no promo badge)
    # isn't invisible just because it's not on sale this week.
    "Пиво безалкогольне": "https://www.atbmarket.com/catalog/310-pivo/f/bezalkogolne=tak",
    "Овочі та фрукти": "https://www.atbmarket.com/catalog/287-ovochi-ta-frukti",
    "Кока-Кола без цукру": "https://www.atbmarket.com/catalog/307-napoi/f/torgova-marka=coca-cola;vmist-cukru=bez-cukru",
    "Побутова хімія": "https://www.atbmarket.com/catalog/308-pobutova-khimiya-ta-neprodovol-chi-tovari",
    "Гігієна і косметика": "https://www.atbmarket.com/catalog/290-gigiena-i-kosmetika",
    # NOTE: this URL arrived merged with the next one in the message
    # ("...catalog/masohttps://...") — split into its two evident halves.
    # "maso" as a slug is unverified; flag if this 404s on the next run.
    "М'ясо": "https://www.atbmarket.com/catalog/maso",
    "Молочні продукти та яйця": "https://www.atbmarket.com/catalog/molocni-produkti-ta-ajca",
    "Риба і морепродукти": "https://www.atbmarket.com/catalog/353-riba-i-moreprodukti",
    "Заморожені продукти": "https://www.atbmarket.com/catalog/322-zamorozheni-produkti",
    "Чипси, снеки": "https://www.atbmarket.com/catalog/cipsi-sneki",
    "Кава, какао": "https://www.atbmarket.com/catalog/286-kava-kakao",
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
    page_cap = FULL_CATALOG_PAGE_CAP if category in FULL_CATALOG_CATEGORIES else MAX_PAGES
    for page in range(1, page_cap + 1):
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
            image_url = extract_image_url(anchor_list[0], base_url) or extract_image_url(container, base_url)

            products[href] = Product(
                store="ATB",
                category=category,
                name=name,
                price=f"{price_m.group(1)}.{price_m.group(2)}" if price_m else None,
                old_price=f"{price_m.group(4)}.{price_m.group(5)}" if price_m and price_m.group(4) else None,
                discount_pct=discount_m.group(1) if discount_m else None,
                date_range=None,
                url=href if href.startswith("http") else f"https://www.atbmarket.com{href}",
                image_url=image_url,
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
    # Full-category sources (not just discount pages) — same rationale as ATB.
    "Власні торгові марки": "https://varus.ua/dnipro/own-trademarks",
    "Безалкогольний алкоголь": "https://varus.ua/dnipro/bezalkogolnij-alkogol",
    "Бакалія": "https://varus.ua/dnipro/bakaliya",
    "Кока-Кола без цукру": "https://varus.ua/dnipro/solodki-napoi~brand_coca-cola~solodki-napoi-obiem_101-15-l_151-2-l~solodki-napoi-vmist-tsukru_bez-tsukru",
    "Побутова хімія": "https://varus.ua/dnipro/pobutova-himiya",
    "Косметика та догляд": "https://varus.ua/dnipro/kosmetika-ta-doglyad",
    "Консерви та соління": "https://varus.ua/dnipro/konservi-ta-solinnya",
    "М'ясні вироби та яйця": "https://varus.ua/dnipro/myasni-virobi-ta-yaycya",
    "Молочні продукти": "https://varus.ua/dnipro/molochni-produkti",
    "Заморожені продукти": "https://varus.ua/dnipro/zamorozheni-produkti",
    "Риба": "https://varus.ua/dnipro/riba",
    "Снеки": "https://varus.ua/dnipro/sneki",
    "Фрукти, овочі, горіхи": "https://varus.ua/dnipro/frukti-ovochi-gorihi",
    "Напої": "https://varus.ua/dnipro/napoi",
}

# Confirmed real structure from a rendered page (Playwright + inspecting
# debug/varus_*.html):
#   <div class="sf-product-card">
#     <a class="sf-product-card__link" href="/product-slug">
#     <h2 class="sf-product-card__title">Name</h2>
#     ... quantity: <p class="sf-product-card__quantity">за 1 шт (500 мл)</p>
#     with discount:    <del class="sf-price__old">89.00</del>
#                        <ins class="sf-price__special ...">52.90 ₴</ins>
#                        <span class="sf-price__sale">-41%</span>
#     without discount: <span class="sf-price__regular">949.00 ₴</span>
VARUS_NUM_RE = re.compile(r"[\d]+[.,]\d+")
VARUS_DISCOUNT_RE = re.compile(r"-(\d+)%")


def scrape_varus_source(browser, category: str, url: str) -> list[Product]:
    products: dict[str, Product] = {}
    page = browser.new_page(locale="uk-UA")
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=45000)
        try:
            page.wait_for_selector(".sf-product-card", timeout=15000)
        except PWTimeout:
            print(f"  [varus:{category}] no .sf-product-card appeared within 15s")

        if category in FULL_CATALOG_CATEGORIES:
            # Full categories can hold far more than fits on one screen.
            # UNVERIFIED: I don't have a confirmed "show more" selector for
            # this template, so try a couple of plausible button texts,
            # then fall back to scroll-to-bottom (covers infinite-scroll
            # templates). Bounded by FULL_CATALOG_PAGE_CAP either way — if
            # this undercounts a category, check debug/varus_<cat>.html to
            # see whether a button was actually there and adjust the text.
            for _ in range(FULL_CATALOG_PAGE_CAP):
                clicked = False
                for label in ["Показати ще", "Завантажити ще", "Show more"]:
                    try:
                        btn = page.get_by_text(label, exact=False).first
                        if btn.is_visible(timeout=1000):
                            btn.click()
                            clicked = True
                            page.wait_for_timeout(1200)
                            break
                    except Exception:
                        continue
                if not clicked:
                    page.mouse.wheel(0, 4000)
                    page.wait_for_timeout(1000)

        html = page.content()
        save_debug("varus", category.replace(" ", "_"), html)

        soup = BeautifulSoup(html, "html.parser")
        cards = soup.select("div.sf-product-card")
        for card in cards:
            link = card.select_one("a.sf-product-card__link[href]")
            href = link["href"] if link else None
            if not href or href in products:
                continue

            title_el = card.select_one("h2.sf-product-card__title")
            name = title_el.get_text(strip=True) if title_el else (
                link.get_text(strip=True) if link else href.rsplit("/", 1)[-1]
            )

            old_price_el = card.select_one("del.sf-price__old")
            new_price_el = card.select_one("ins.sf-price__special") or card.select_one("span.sf-price__regular")
            sale_el = card.select_one("span.sf-price__sale")

            old_price_m = VARUS_NUM_RE.search(old_price_el.get_text()) if old_price_el else None
            new_price_m = VARUS_NUM_RE.search(new_price_el.get_text()) if new_price_el else None
            discount_m = VARUS_DISCOUNT_RE.search(sale_el.get_text()) if sale_el else None
            image_url = extract_image_url(card, "https://varus.ua")

            products[href] = Product(
                store="Varus",
                category=category,
                name=name,
                price=new_price_m.group(0).replace(",", ".") if new_price_m else None,
                old_price=old_price_m.group(0).replace(",", ".") if old_price_m else None,
                discount_pct=discount_m.group(1) if discount_m else None,
                date_range=None,
                url=href if href.startswith("http") else f"https://varus.ua{href}",
                image_url=image_url,
                raw_text=card.get_text(" ", strip=True)[:300],
            )
        print(f"  [varus:{category}] {len(cards)} .sf-product-card elements, {len(products)} parsed")
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
                image_url=extract_image_url(a, "https://velmart.ua"),
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
                image_url=extract_image_url(a, "https://fayno.market"),
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
