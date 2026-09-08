"""
GoToShop.ua scraper — handles JS-driven pagination that a plain HTTP fetch
can't see (page 2/3 return the same HTML as page 1 unless the "next" button
is actually clicked and the page re-renders via JS/XHR).

Requires: pip install playwright --break-system-packages
           playwright install chromium

Usage:
    python gotoshop_scraper.py

Notes for Vova:
- I could not inspect the live rendered DOM myself (gotoshop.ua isn't on my
  sandbox's allowed network list, and a plain fetch doesn't execute the JS
  that drives pagination). So the CSS selectors below are best-effort,
  based on the *rendered text structure* I could see via a static fetch
  (product title as a link to /products/<id>/..., followed by a price line
  like "104.9грн" and sometimes a "-50% 209грн" discount line).
- If the pagination click or extraction doesn't work first try, open the
  page in a real browser, right-click the "next" arrow -> Inspect, and
  update NEXT_BUTTON_SELECTORS / PRODUCT_LINK_SELECTOR below to match what
  you actually see. The extraction logic (regex on visible text) should
  keep working even if the selectors around it change slightly.
"""

import json
import re
import time
from dataclasses import dataclass, asdict

from playwright.sync_api import sync_playwright, Page, TimeoutError as PWTimeout

# ---- Config -----------------------------------------------------------

SHOP_URLS = [
    "https://gotoshop.ua/zheltye-vody/shops/atb/",
    "https://gotoshop.ua/zheltye-vody/shops/varus/",
    "https://gotoshop.ua/zheltye-vody/shops/fajno-market/",
]

MAX_PAGES_PER_SHOP = 5  # safety cap; raise if a shop legitimately has more

# Candidate selectors for the "next" pagination control — tried in order.
NEXT_BUTTON_SELECTORS = [
    "text=next",
    "[aria-label='Next']",
    "[aria-label='next']",
    ".pagination-next",
    ".slick-next",
    "button:has-text('›')",
]

# Anchors to product detail pages double as the reliable "one card = one
# product" marker, since the exact card/container class is unknown to me.
PRODUCT_LINK_SELECTOR = "a[href*='/products/']"

PRICE_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*грн")
DISCOUNT_RE = re.compile(r"-(\d+)%")


@dataclass
class Product:
    name: str
    price: str | None
    old_price: str | None
    discount_pct: str | None
    url: str
    raw_text: str


def dismiss_age_gate(page: Page) -> None:
    """The alcohol age-verification modal blocks interaction if present."""
    for label in ["Підтвердити", "Confirm", "Подтвердить"]:
        try:
            btn = page.get_by_text(label, exact=False).first
            if btn.is_visible(timeout=1000):
                btn.click()
                page.wait_for_timeout(300)
                return
        except PWTimeout:
            continue
        except Exception:
            continue


def extract_products(page: Page) -> list[Product]:
    products: list[Product] = []
    links = page.query_selector_all(PRODUCT_LINK_SELECTOR)
    seen_urls = set()

    for link in links:
        href = link.get_attribute("href") or ""
        if not href or href in seen_urls:
            continue

        # Climb a few ancestors to capture the surrounding "card" text
        # (name + price + discount usually live in the same block).
        card = link
        card_text = ""
        for _ in range(4):
            handle = card.evaluate_handle("el => el.parentElement")
            card = handle.as_element()
            if card is None:
                break
            card_text = card.inner_text().strip()
            if PRICE_RE.search(card_text):
                break

        if not card_text:
            continue

        seen_urls.add(href)

        prices = PRICE_RE.findall(card_text)
        discount_match = DISCOUNT_RE.search(card_text)

        # Product name: first non-empty line that isn't a price/date/label.
        name = None
        for line in card_text.splitlines():
            line = line.strip()
            if not line or PRICE_RE.search(line) or DISCOUNT_RE.search(line):
                continue
            if any(skip in line for skip in ("Відгуки", "Переглянути", "Додати", "Увага!")):
                continue
            name = line
            break

        products.append(
            Product(
                name=name or "(не визначено)",
                price=prices[0] if prices else None,
                old_price=prices[1] if len(prices) > 1 else None,
                discount_pct=discount_match.group(1) if discount_match else None,
                url=href if href.startswith("http") else f"https://gotoshop.ua{href}",
                raw_text=card_text[:300],
            )
        )

    return products


def scrape_shop(page: Page, url: str) -> list[Product]:
    print(f"\n=== {url} ===")
    # networkidle times out on ad-heavy sites (background/tracker requests
    # never stop) — wait only for the DOM, then explicitly wait for a
    # product link to actually appear.
    page.goto(url, wait_until="domcontentloaded", timeout=45000)
    dismiss_age_gate(page)
    try:
        page.wait_for_selector(PRODUCT_LINK_SELECTOR, timeout=15000)
    except PWTimeout:
        print("  no product links appeared within 15s — page may have no food items, "
              "or selector needs adjusting")

    all_products: dict[str, Product] = {}
    for page_num in range(1, MAX_PAGES_PER_SHOP + 1):
        page.wait_for_timeout(500)  # let any lazy content settle
        found = extract_products(page)
        new_count = 0
        for p in found:
            if p.url not in all_products:
                all_products[p.url] = p
                new_count += 1
        print(f"  page {page_num}: {len(found)} cards on screen, {new_count} new")

        if new_count == 0 and page_num > 1:
            print("  no new products after clicking next -> stopping")
            break

        clicked = False
        for selector in NEXT_BUTTON_SELECTORS:
            try:
                btn = page.locator(selector).first
                if btn.is_visible(timeout=1000) and btn.is_enabled():
                    btn.click()
                    clicked = True
                    time.sleep(1.5)  # crude wait for re-render/XHR
                    break
            except Exception:
                continue

        if not clicked:
            print("  no clickable 'next' control found -> stopping pagination")
            break

    return list(all_products.values())


def main():
    results: dict[str, list[dict]] = {}
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(locale="uk-UA")
        for url in SHOP_URLS:
            try:
                products = scrape_shop(page, url)
            except Exception as e:
                print(f"  ERROR scraping {url}: {e}")
                products = []
            results[url] = [asdict(pr) for pr in products]
        browser.close()

    out_path = "data/gotoshop_results.json"
    import os
    os.makedirs("data", exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print(f"\nSaved {sum(len(v) for v in results.values())} products total -> {out_path}")


if __name__ == "__main__":
    main()
