"""
Unified discount scraper: ATB + Varus + Velmart + Fayno market.

All four are official retailer sites serving plain server-rendered HTML with
real URL-based pagination and no Cloudflare/bot-check (unlike gotoshop.ua,
which is deliberately excluded). Plain `requests` + BeautifulSoup, no
browser needed anywhere in this script.

Requires: pip install requests beautifulsoup4 --break-system-packages

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
verified against real HTML and were re-fixed after a first run exposed
parsing bugs (wrong climb depth for ATB, wrong price-order assumption for
Velmart, wrong template assumption for Fayno's bundle promos). Varus is
still UNVERIFIED — I have never seen its real markup. scrape_varus() now
unconditionally saves debug/varus_<category>.html on page 1 of every
source regardless of match count, so the next run will produce that file
even if parsing succeeds by luck. If Varus still shows 0, share that
debug file and I'll fix VARUS_PRICE_RE precisely instead of guessing again.
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

# "107.50 грн/шт (Гривня) 188.90"  or  "79.95 грн/кг (Гривня) 98.95"  or
# "90.50 грн/шт (Гривня)" (no discount -> no old price)
ATB_PRICE_RE = re.compile(r"([\d]+\.\d{2})\s*грн/(шт|кг)\s*\(Гривня\)\s*([\d]+\.\d{2})?")
ATB_DISCOUNT_RE = re.compile(r"-(\d+)%")


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
        new_count = 0
        for a in links:
            href = a["href"]
            if href in products:
                continue
            # Climb until we find an ancestor whose text also contains the
            # price pattern — deeper cap than before (10, not 4) since real
            # nesting depth for this template turned out to be greater than
            # first assumed; this was the actual cause of the 0-result run.
            container = a
            text = ""
            for _ in range(10):
                parent = container.find_parent()
                if parent is None:
                    break
                container = parent
                text = container.get_text(" ", strip=True)
                if ATB_PRICE_RE.search(text):
                    break
            price_m = ATB_PRICE_RE.search(text)
            name = a.get_text(strip=True) or href.rsplit("/", 1)[-1]
            discount_m = ATB_DISCOUNT_RE.search(text) if text else None

            products[href] = Product(
                store="ATB",
                category=category,
                name=name,
                price=price_m.group(1) if price_m else None,
                old_price=price_m.group(3) if price_m else None,
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


# ---- Varus (UNVERIFIED — see module docstring) ---------------------------

VARUS_SOURCES = {
    "Знижки до -40%": "https://varus.ua/dnipro/znizhki-do-40-na-tovari-dlya-litnogo-nastroyu",
    "Хіти з новою поштою": "https://varus.ua/dnipro/hiti-yaki-vozimo-novoyu-poshtoyu",
    "Тиждень покупок": "https://varus.ua/dnipro/weekly-shopping",
    "Ціна тижня": "https://varus.ua/dnipro/price-of-the-week",
}

# Best-effort guess, modeled on ATB/Fayno price patterns. VERIFY against
# debug/varus_*.html if this returns 0 — the real markup may differ.
VARUS_PRICE_RE = re.compile(r"([\d]+[.,]\d{2})\s*грн")
VARUS_DISCOUNT_RE = re.compile(r"-(\d+)%")


def scrape_varus_source(session: requests.Session, category: str, base_url: str) -> list[Product]:
    products: dict[str, Product] = {}
    for page in range(1, MAX_PAGES + 1):
        params = {"sort": "final_price:desc"}
        if "weekly-shopping" in base_url or "price-of-the-week" in base_url:
            params["has_promotion_in_stores"] = "1"
        if page > 1:
            params["page"] = page

        resp = session.get(base_url, headers=HEADERS, params=params, timeout=20)
        print(f"  [varus:{category}] page {page}: HTTP {resp.status_code}, {len(resp.text)} bytes, final url {resp.url}")
        if resp.status_code != 200:
            print(f"  [varus:{category}] page {page}: stopping (non-200)")
            break

        soup = BeautifulSoup(resp.text, "html.parser")
        links = soup.select("a[href*='varus.ua']") or soup.find_all("a", href=True)
        product_links = [a for a in links if VARUS_PRICE_RE.search(a.get_text(" ", strip=True))]

        new_count = 0
        for a in product_links:
            href = a.get("href", "")
            if not href or href in products:
                continue
            text = a.get_text(" ", strip=True)
            price_m = VARUS_PRICE_RE.search(text)
            discount_m = VARUS_DISCOUNT_RE.search(text)
            name = re.split(r"[\d]+[.,]\d{2}\s*грн", text)[0].strip() or text[:120]

            products[href] = Product(
                store="Varus",
                category=category,
                name=name,
                price=price_m.group(1).replace(",", ".") if price_m else None,
                old_price=None,
                discount_pct=discount_m.group(1) if discount_m else None,
                date_range=None,
                url=href if href.startswith("http") else f"https://varus.ua{href}",
                raw_text=text,
            )
            new_count += 1

        print(f"  [varus:{category}] page {page}: {len(links)} total links, "
              f"{len(product_links)} candidates, {new_count} new")
        # Always dump page 1 (not just on a zero-match) — this is the
        # source's first real HTML I've seen, so I want it regardless of
        # whether the guessed selector happened to match anything.
        if page == 1:
            save_debug("varus", category.replace(" ", "_"), resp.text)
        if new_count == 0:
            break
        time.sleep(0.5)

    return list(products.values())


def scrape_varus(session: requests.Session) -> list[Product]:
    all_products: list[Product] = []
    for category, url in VARUS_SOURCES.items():
        all_products.extend(scrape_varus_source(session, category, url))
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
