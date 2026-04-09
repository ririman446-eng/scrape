#!/usr/bin/env python3
"""
darkchemsite.com → WooCommerce-compatible CSV scraper
Next.js App Router site — uses Playwright to click quantity options
and capture per-variant prices.

Usage:
    python scraper.py [--output products.csv] [--reviews reviews.csv]
                      [--delay 1.5] [--headless]

Requirements:
    pip install playwright beautifulsoup4 requests lxml
    python -m playwright install chromium
"""

import argparse
import csv
import json
import logging
import re
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urljoin

import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)

BASE_URL    = "https://www.darkchemsite.com"
SITEMAP_URL = f"{BASE_URL}/sitemap.xml"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
      "AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/124.0.0.0 Safari/537.36")


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass
class Review:
    product_sku: str
    reviewer: str
    date: str
    rating: int
    content: str
    verified: bool


@dataclass
class Variation:
    sku: str          # parent_sku-Xg
    label: str        # "25g", "50g" …
    regular_price: str
    in_stock: bool


@dataclass
class Product:
    url: str
    sku: str
    name: str
    category: str
    description: str
    regular_price: str   # price of first/default variant
    in_stock: bool
    stock_qty: str
    images: list
    variations: list = field(default_factory=list)
    reviews:    list = field(default_factory=list)


# ---------------------------------------------------------------------------
# Price helpers
# ---------------------------------------------------------------------------

def _parse_price(text: str) -> str:
    """Extract a decimal price string from text like '€400.00' or '250'."""
    text = text.replace(",", ".")
    m = re.search(r"(\d+(?:\.\d+)?)", text)
    return m.group(1) if m else ""


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip()


# ---------------------------------------------------------------------------
# Step 1 – collect product URLs
# ---------------------------------------------------------------------------

def product_urls_from_sitemap() -> list[str]:
    try:
        r = requests.get(SITEMAP_URL, headers={"User-Agent": UA}, timeout=15)
        if r.status_code != 200:
            return []
        root = ET.fromstring(r.text)
        ns = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9"}
        urls = []
        for loc in root.findall(".//s:loc", ns):
            u = (loc.text or "").strip()
            if re.search(r"/products/[^/\s?#]+$", u):
                if not u.startswith("https://www."):
                    u = u.replace("https://", "https://www.", 1)
                urls.append(u)
        log.info("Sitemap: %d product URLs", len(urls))
        return urls
    except Exception as e:
        log.warning("Sitemap error: %s", e)
        return []


def product_urls_from_listing(page) -> list[str]:
    log.info("Loading product listing page …")
    page.goto(f"{BASE_URL}/products", wait_until="domcontentloaded", timeout=90_000)
    page.wait_for_timeout(4000)
    hrefs = page.eval_on_selector_all(
        "a[href]", "els => els.map(e => e.getAttribute('href'))"
    )
    seen, urls = set(), []
    for h in hrefs:
        if h and re.search(r"^/products/[^/\s?#]+$", h):
            full = BASE_URL + h
            if full not in seen:
                seen.add(full)
                urls.append(full)
    log.info("Listing page: %d product URLs", len(urls))
    return urls


# ---------------------------------------------------------------------------
# Step 2 – scrape a single product page
# ---------------------------------------------------------------------------

def scrape_product(page, url: str) -> Optional[Product]:
    slug = url.rstrip("/").rsplit("/", 1)[-1]
    base_sku = slug.upper().replace("-", "_")

    # ── navigate ──────────────────────────────────────────────────────────
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=90_000)
        page.wait_for_timeout(3000)
    except Exception as e:
        log.warning("Navigation failed for %s: %s", url, e)
        return None

    # ── capture images early (before any clicking) ────────────────────────
    images = _get_images(page)

    # ── product name ──────────────────────────────────────────────────────
    name = ""
    for sel in ("h1", "[class*='product-title']", "[class*='productTitle']",
                "[class*='product_title']", "[class*='name']"):
        try:
            el = page.locator(sel).first
            if el.count():
                name = _clean(el.inner_text())
                if len(name) > 2:
                    break
        except Exception:
            pass
    if not name:
        name = slug.replace("-", " ").title()

    # ── category ──────────────────────────────────────────────────────────
    # The category appears on the line immediately before the product name
    # in the breadcrumb section of the body text:
    #   "Products\n/\ncannabinoids\n/\n4fadb precursor/kit"
    # We walk backwards from the product name skipping "/" and nav items.
    category = ""
    try:
        body_text = page.inner_text("body")
        _nav_words = {
            "home", "shop", "about", "contact us", "login", "products",
            "search products", "/", "email us", "chat with us on whatsapp",
            "skip to main content",
        }
        lines = [l.strip() for l in body_text.split("\n") if l.strip()]
        for i, line in enumerate(lines):
            if name and name.lower() in line.lower() and len(line) < len(name) + 10:
                # Walk back up to 6 lines looking for the category label
                for j in range(i - 1, max(0, i - 7), -1):
                    cand = lines[j]
                    if cand.lower() in _nav_words or cand == "/":
                        continue
                    if 2 < len(cand) < 50 and cand[0].isalpha():
                        category = cand.lower()
                        break
                if category:
                    break
    except Exception:
        pass

    # ── description ───────────────────────────────────────────────────────
    # Parse from body text: content between "Description" heading
    # and "Customer Reviews" section — avoids lorem ipsum placeholders.
    description = ""
    try:
        body_text = page.inner_text("body")
        m = re.search(
            r"\bDescription\b\s*\n([\s\S]+?)(?=\n\s*Customer Reviews|\n\s*Reviews\b|\Z)",
            body_text
        )
        if m:
            desc_text = _clean(m.group(1))
            # Reject if it's clearly a placeholder
            if "lorem ipsum" not in desc_text.lower() and len(desc_text) > 30:
                description = desc_text
    except Exception:
        pass

    # ── stock ─────────────────────────────────────────────────────────────
    in_stock  = True
    stock_qty = ""
    try:
        body_text = page.inner_text("body")
        m = re.search(r"In Stock\s*\((\d+)\s*available\)", body_text, re.I)
        if m:
            stock_qty = m.group(1)
            in_stock  = True
        elif re.search(r"out of stock", body_text, re.I):
            in_stock = False
    except Exception:
        pass

    # ── quantity buttons → per-variant prices ──────────────────────────────
    variations = _get_variations(page, base_sku)

    # Default price = first variation price (or standalone price if no variants)
    regular_price = variations[0].regular_price if variations else _get_standalone_price(page)

    # ── reviews ───────────────────────────────────────────────────────────
    reviews = _get_reviews(page, base_sku)

    return Product(
        url=url,
        sku=base_sku,
        name=name,
        category=category,
        description=description,
        regular_price=regular_price,
        in_stock=in_stock,
        stock_qty=stock_qty,
        images=images,
        variations=variations,
        reviews=reviews,
    )


# ---------------------------------------------------------------------------
# Sub-extractors
# ---------------------------------------------------------------------------

def _get_images(page) -> list[str]:
    imgs = []
    try:
        srcs = page.eval_on_selector_all(
            "img[src]", "els => els.map(e => e.src)"
        )
        for src in srcs:
            if any(x in src for x in ("/uploads/", "cloudinary", "_next/image")):
                # For _next/image, decode the actual URL from the query param
                if "_next/image" in src:
                    m = re.search(r"url=([^&]+)", src)
                    if m:
                        from urllib.parse import unquote
                        src = unquote(m.group(1))
                        if src.startswith("/"):
                            src = BASE_URL + src
                # Skip the site logo and any non-product images
                skip = ("/images/logo", "/logo.", "/favicon", "/icon",
                        "placeholder", "avatar", "banner", "hero")
                if any(x in src.lower() for x in skip):
                    continue
                # Only keep actual product image paths
                if not any(x in src for x in ("/uploads/products/", "cloudinary.com", "/uploads/")):
                    continue
                if src not in imgs:
                    imgs.append(src)
    except Exception:
        pass
    return imgs


def _get_standalone_price(page) -> str:
    """
    Read the currently displayed price from the known price element:
      <div class="text-2xl font-bold text-purple-400">€400.00<span ...>(25g)</span></div>
    """
    # Primary: the exact Tailwind class seen in the HTML
    for sel in (
        "div.text-2xl.font-bold",
        ".text-2xl",
        "[class*='text-2xl']",
        "[class*='price']",
        "[class*='amount']",
    ):
        try:
            el = page.locator(sel).first
            if el.count():
                p = _parse_price(el.inner_text())
                if p:
                    return p
        except Exception:
            pass
    # Fallback: first € price in body text
    try:
        m = re.search(r"[€$£](\d+(?:\.\d+)?)", page.inner_text("body"))
        if m:
            return m.group(1)
    except Exception:
        pass
    return ""


def _get_variations(page, base_sku: str) -> list[Variation]:
    """
    Quantity options live in:
      <select class="w-full bg-gray-700 ...">
        <option value="25">25g</option>
        <option value="50">50g</option>
        ...
      </select>
    Select each option value, wait for the price div to update, capture price.
    """
    # Find the quantity <select> (identified by its Tailwind bg-gray-700 class)
    select = page.locator("select.bg-gray-700, select[class*='bg-gray']").first
    if not select.count():
        select = page.locator("select").first   # any select as fallback
    if not select.count():
        log.debug("No <select> found on this page.")
        return []

    # Collect all options
    options = page.evaluate("""(sel) => {
        var el = document.querySelector(sel);
        if (!el) return [];
        return Array.from(el.options).map(function(o) {
            return {value: o.value, text: o.text.trim()};
        });
    }""", "select.bg-gray-700, select[class*='bg-gray'], select")

    if not options:
        return []

    variations = []
    for opt in options:
        value = opt["value"]
        label = opt["text"]       # e.g. "25g"
        if not label:
            continue
        try:
            select.select_option(value=value)
            page.wait_for_timeout(600)           # let price div re-render
            price = _get_standalone_price(page)
            var_sku = f"{base_sku}-{label.replace(' ', '').upper()}"
            variations.append(Variation(
                sku=var_sku,
                label=label,
                regular_price=price,
                in_stock=True,
            ))
            log.debug("  Variation: %s → €%s", label, price)
        except Exception as e:
            log.warning("  Could not click variant %s: %s", label, e)

    return variations


def _get_reviews(page, product_sku: str) -> list[Review]:
    """
    Parse visible review block.
    Pattern observed:
        {initial_letter}
        {Reviewer Name}
        {Month Day, Year}
        {review text}
        Verified Purchase   ← optional
    """
    reviews = []
    try:
        body = page.inner_text("body")
    except Exception:
        return reviews

    # Split on "Customer Reviews" section
    parts = re.split(r"Customer Reviews", body, flags=re.I)
    if len(parts) < 2:
        return reviews

    review_block = parts[1]

    # Each review starts with a single letter (the avatar initial)
    # then name, then date, then content, then optionally "Verified Purchase"
    pattern = re.compile(
        r"^([A-Z])\n"                           # avatar initial
        r"(.+?)\n"                              # reviewer name
        r"((?:January|February|March|April|May|June|July|August|"
        r"September|October|November|December)\s+\d{1,2},\s+\d{4})\n"
        r"([\s\S]+?)"                           # review content
        r"(?=\n[A-Z]\n|\nDarkChemSite|\Z)",     # stop at next review or footer
        re.MULTILINE,
    )

    for m in pattern.finditer(review_block):
        reviewer = _clean(m.group(2))
        date     = _clean(m.group(3))
        content  = _clean(m.group(4))
        verified = "Verified Purchase" in content
        content  = _clean(content.replace("Verified Purchase", ""))

        reviews.append(Review(
            product_sku=product_sku,
            reviewer=reviewer,
            date=date,
            rating=5,       # site doesn't show numeric stars in text; default 5
            content=content,
            verified=verified,
        ))

    log.debug("  Reviews found: %d", len(reviews))
    return reviews


# ---------------------------------------------------------------------------
# WooCommerce CSV
# ---------------------------------------------------------------------------

PRODUCT_COLS = [
    "ID","Type","SKU","Name","Published","Is featured?",
    "Visibility in catalog","Short description","Description",
    "Date sale price starts","Date sale price ends",
    "Tax status","Tax class",
    "In stock?","Stock","Low stock amount","Backorders allowed?","Sold individually?",
    "Weight (kg)","Length (cm)","Width (cm)","Height (cm)",
    "Allow customer reviews?","Purchase note",
    "Sale price","Regular price",
    "Categories","Tags","Shipping class","Images",
    "Download limit","Download expiry",
    "Parent","Grouped products","Upsells","Cross-sells",
    "External URL","Button text","Position",
    "Attribute 1 name","Attribute 1 value(s)","Attribute 1 visible","Attribute 1 global",
]

REVIEW_COLS = [
    "product_sku","comment_author","comment_author_email",
    "comment_date","comment_content","comment_approved",
    "rating","verified",
]


def _b(v): return "1" if v else "0"


def _parent_row(p: Product, pos: int) -> dict:
    ptype = "variable" if p.variations else "simple"
    qty_values = " | ".join(v.label for v in p.variations)
    return {
        "ID": "", "Type": ptype, "SKU": p.sku, "Name": p.name,
        "Published": "1", "Is featured?": "0",
        "Visibility in catalog": "visible",
        "Short description": (p.description[:200] if p.description else ""),
        "Description": p.description,
        "Date sale price starts": "", "Date sale price ends": "",
        "Tax status": "taxable", "Tax class": "",
        "In stock?": _b(p.in_stock), "Stock": p.stock_qty,
        "Low stock amount": "", "Backorders allowed?": "0", "Sold individually?": "0",
        "Weight (kg)": "", "Length (cm)": "", "Width (cm)": "", "Height (cm)": "",
        "Allow customer reviews?": "1", "Purchase note": "",
        "Sale price": "", "Regular price": p.regular_price,
        "Categories": p.category, "Tags": "", "Shipping class": "",
        "Images": ", ".join(p.images),
        "Download limit": "", "Download expiry": "",
        "Parent": "", "Grouped products": "", "Upsells": "", "Cross-sells": "",
        "External URL": "", "Button text": "", "Position": str(pos),
        "Attribute 1 name": "Quantity" if p.variations else "",
        "Attribute 1 value(s)": qty_values,
        "Attribute 1 visible": "1" if p.variations else "",
        "Attribute 1 global": "0" if p.variations else "",
    }


def _variation_row(v: Variation, p: Product, pos: int) -> dict:
    return {
        "ID": "", "Type": "variation", "SKU": v.sku, "Name": p.name,
        "Published": "1", "Is featured?": "0",
        "Visibility in catalog": "visible",
        "Short description": v.label, "Description": "",
        "Date sale price starts": "", "Date sale price ends": "",
        "Tax status": "taxable", "Tax class": "",
        "In stock?": _b(v.in_stock), "Stock": "",
        "Low stock amount": "", "Backorders allowed?": "0", "Sold individually?": "0",
        "Weight (kg)": "", "Length (cm)": "", "Width (cm)": "", "Height (cm)": "",
        "Allow customer reviews?": "0", "Purchase note": "",
        "Sale price": "", "Regular price": v.regular_price,
        "Categories": "", "Tags": "", "Shipping class": "", "Images": "",
        "Download limit": "", "Download expiry": "",
        "Parent": p.sku, "Grouped products": "", "Upsells": "", "Cross-sells": "",
        "External URL": "", "Button text": "", "Position": str(pos),
        "Attribute 1 name": "Quantity",
        "Attribute 1 value(s)": v.label,
        "Attribute 1 visible": "1",
        "Attribute 1 global": "0",
    }


def write_products_csv(products: list, path: str):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=PRODUCT_COLS, extrasaction="ignore")
        w.writeheader()
        for pos, p in enumerate(products, 1):
            w.writerow(_parent_row(p, pos))
            for vpos, v in enumerate(p.variations, 1):
                w.writerow(_variation_row(v, p, vpos))
    total_vars = sum(len(p.variations) for p in products)
    log.info("products.csv → %d products, %d variation rows", len(products), total_vars)


def write_reviews_csv(products: list, path: str):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=REVIEW_COLS, extrasaction="ignore")
        w.writeheader()
        for p in products:
            for r in p.reviews:
                w.writerow({
                    "product_sku": p.sku,
                    "comment_author": r.reviewer,
                    "comment_author_email": "",
                    "comment_date": r.date,
                    "comment_content": r.content,
                    "comment_approved": "1",
                    "rating": str(r.rating),
                    "verified": _b(r.verified),
                })
    total = sum(len(p.reviews) for p in products)
    log.info("reviews.csv → %d reviews", total)


def write_json(products: list, path: str):
    import dataclasses
    with open(path, "w", encoding="utf-8") as f:
        json.dump([dataclasses.asdict(p) for p in products], f, indent=2, ensure_ascii=False)
    log.info("products.json written")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="darkchemsite.com → WooCommerce CSV")
    ap.add_argument("--output",   default="products.csv")
    ap.add_argument("--reviews",  default="reviews.csv")
    ap.add_argument("--json-out", default="products.json")
    ap.add_argument("--delay",    type=float, default=1.2)
    ap.add_argument("--headless", action="store_true")
    ap.add_argument("--product-url", action="append", default=[], dest="product_urls")
    args = ap.parse_args()

    from playwright.sync_api import sync_playwright

    products: list[Product] = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=args.headless,
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
        )
        ctx = browser.new_context(
            user_agent=UA,
            locale="en-US",
            timezone_id="Europe/Paris",
            viewport={"width": 1280, "height": 900},
        )
        ctx.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"
        )
        page = ctx.new_page()

        # ── collect URLs ──────────────────────────────────────────────────
        if args.product_urls:
            urls = args.product_urls
        else:
            urls = product_urls_from_sitemap()
            if not urls:
                urls = product_urls_from_listing(page)

        if not urls:
            log.error("No product URLs found. Exiting.")
            sys.exit(1)

        # ── scrape each product ───────────────────────────────────────────
        for i, url in enumerate(urls, 1):
            log.info("[%d/%d] %s", i, len(urls), url)
            p = scrape_product(page, url)
            if p:
                products.append(p)
                log.info("  ✓ %s | €%s | variants=%d | reviews=%d",
                         p.name, p.regular_price, len(p.variations), len(p.reviews))
            else:
                log.warning("  ✗ failed: %s", url)
            time.sleep(args.delay)

        browser.close()

    if not products:
        log.error("No products scraped.")
        sys.exit(1)

    write_products_csv(products, args.output)
    write_reviews_csv(products, args.reviews)
    write_json(products, args.json_out)
    log.info("Done — %d products total.", len(products))


if __name__ == "__main__":
    main()
