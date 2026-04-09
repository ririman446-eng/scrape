#!/usr/bin/env python3
"""
darkchemsite.com → WooCommerce-compatible CSV scraper
Site is a Next.js app (not WordPress). Scraper uses Playwright to render
pages, extracts __NEXT_DATA__ JSON where available, and falls back to
parsing visible page text.

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
from urllib.parse import urljoin, urlparse, unquote

import requests
from bs4 import BeautifulSoup

BASE_URL = "https://www.darkchemsite.com"
PRODUCTS_URL = f"{BASE_URL}/products"
SITEMAP_URL = f"{BASE_URL}/sitemap.xml"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass
class Review:
    product_sku: str
    reviewer: str
    email: str
    date: str
    rating: int
    title: str
    content: str
    verified: bool


@dataclass
class Variation:
    sku: str
    label: str          # e.g. "10g", "50g", "100g"
    regular_price: str
    sale_price: str
    in_stock: bool
    stock_qty: str
    attributes: dict


@dataclass
class Product:
    url: str
    sku: str
    name: str
    slug: str
    category: str
    short_description: str
    description: str
    regular_price: str
    sale_price: str
    in_stock: bool
    stock_qty: str
    images: list
    tags: list
    attributes: dict
    variations: list = field(default_factory=list)
    reviews: list = field(default_factory=list)


# ---------------------------------------------------------------------------
# Playwright browser helper
# ---------------------------------------------------------------------------

class Browser:
    def __init__(self, headless: bool = False, delay: float = 1.5):
        self.headless = headless
        self.delay = delay
        self._pw = None
        self._browser = None
        self._ctx = None

    def start(self):
        from playwright.sync_api import sync_playwright
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(
            headless=self.headless,
            args=[
                "--no-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
            ],
        )
        self._ctx = self._browser.new_context(
            user_agent=HEADERS["User-Agent"],
            locale="en-US",
            timezone_id="Europe/Paris",
            viewport={"width": 1280, "height": 900},
            extra_http_headers={
                "Accept-Language": "en-US,en;q=0.9",
            },
        )
        self._ctx.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"
        )
        log.info("Browser started (headless=%s)", self.headless)

    def get(self, url: str, settle_ms: int = 3000) -> tuple[str, dict]:
        """
        Navigate to url, wait for content, return (html, next_data_dict).
        next_data_dict is populated from __NEXT_DATA__ script tag if present.
        """
        page = self._ctx.new_page()
        try:
            for wait in ("domcontentloaded", "load"):
                try:
                    page.goto(url, wait_until=wait, timeout=90_000)
                    break
                except Exception as e:
                    log.warning("goto wait=%s failed: %s", wait, e)

            page.wait_for_timeout(settle_ms)
            html = page.content()

            # Extract __NEXT_DATA__ if present
            next_data = {}
            try:
                nd_content = page.eval_on_selector(
                    "#__NEXT_DATA__", "el => el.textContent"
                )
                next_data = json.loads(nd_content)
            except Exception:
                pass

            # Also try to intercept RSC / window data
            if not next_data:
                try:
                    rsc = page.evaluate("() => window.__NEXT_DATA__")
                    if rsc:
                        next_data = rsc
                except Exception:
                    pass

            time.sleep(self.delay)
            return html, next_data
        finally:
            page.close()

    def close(self):
        if self._browser:
            self._browser.close()
        if self._pw:
            self._pw.stop()


# ---------------------------------------------------------------------------
# Step 1: collect product URLs
# ---------------------------------------------------------------------------

def get_product_urls_from_sitemap() -> list[str]:
    """Try to get product URLs from sitemap.xml."""
    try:
        resp = requests.get(SITEMAP_URL, headers=HEADERS, timeout=15)
        if resp.status_code != 200:
            return []
        # Parse XML, collect /products/{slug} URLs
        root = ET.fromstring(resp.text)
        ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
        urls = []
        for loc in root.findall(".//sm:loc", ns):
            u = (loc.text or "").strip()
            # Match /products/something (not just /products)
            if re.search(r"/products/[^/\s?]+$", u):
                if not u.startswith("https://www."):
                    u = u.replace("https://", "https://www.")
                urls.append(u)
        log.info("Sitemap gave %d product URLs", len(urls))
        return urls
    except Exception as e:
        log.warning("Sitemap fetch failed: %s", e)
        return []


def get_product_urls_from_listing(browser: Browser) -> list[str]:
    """Scrape product URLs from the /products listing page."""
    log.info("Loading product listing: %s", PRODUCTS_URL)
    html, _ = browser.get(PRODUCTS_URL, settle_ms=4000)
    soup = BeautifulSoup(html, "html.parser")

    urls = []
    seen = set()
    # Find all links matching /products/{slug}
    for a in soup.find_all("a", href=True):
        href = a["href"]
        # Normalise relative to absolute
        if href.startswith("/"):
            href = BASE_URL + href
        if re.search(r"/products/[^/\s?#]+$", href):
            clean = href.split("?")[0].split("#")[0]
            if clean not in seen and clean != PRODUCTS_URL:
                seen.add(clean)
                urls.append(clean)

    log.info("Listing page gave %d product URLs", len(urls))
    return urls


def collect_product_urls(browser: Browser) -> list[str]:
    # Try sitemap first (fast, no JS needed)
    urls = get_product_urls_from_sitemap()
    if not urls:
        urls = get_product_urls_from_listing(browser)
    return list(dict.fromkeys(urls))  # deduplicate


# ---------------------------------------------------------------------------
# Step 2: parse individual product pages
# ---------------------------------------------------------------------------

def _slug_to_sku(slug: str) -> str:
    return slug.upper().replace("-", "_")


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip()


def _extract_price(text: str) -> str:
    """Pull a numeric price string from text like '€250.00' or '$199'."""
    m = re.search(r"[\d]+(?:[.,]\d+)?", text.replace(",", "."))
    return m.group(0) if m else ""


def _parse_next_data_product(next_data: dict, slug: str) -> Optional[dict]:
    """
    Walk the Next.js __NEXT_DATA__ tree looking for product info.
    Returns a flat dict of extracted fields or None.
    """
    if not next_data:
        return None

    # Try common paths in Next.js page data
    candidates = []

    def _walk(obj, depth=0):
        if depth > 10:
            return
        if isinstance(obj, dict):
            # Check if this dict looks like a product
            keys = set(obj.keys())
            if any(k in keys for k in ("name", "title", "price", "description", "slug")):
                candidates.append(obj)
            for v in obj.values():
                _walk(v, depth + 1)
        elif isinstance(obj, list):
            for item in obj:
                _walk(item, depth + 1)

    _walk(next_data)

    # Find the best candidate matching our slug
    for c in candidates:
        c_slug = c.get("slug", c.get("id", c.get("handle", "")))
        if str(c_slug).lower() in slug.lower() or slug.lower() in str(c_slug).lower():
            return c

    # Return the richest candidate if no slug match
    if candidates:
        return max(candidates, key=lambda x: len(str(x)))

    return None


def parse_product_page(html: str, next_data: dict, url: str) -> Optional[Product]:
    slug = url.rstrip("/").rsplit("/", 1)[-1]
    soup = BeautifulSoup(html, "html.parser")

    # ------------------------------------------------------------------
    # Try to get structured data from __NEXT_DATA__ first
    # ------------------------------------------------------------------
    nd = _parse_next_data_product(next_data, slug)

    # Also check for JSON-LD
    jsonld = {}
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
            if isinstance(data, list):
                for d in data:
                    if d.get("@type") in ("Product", "ItemPage"):
                        jsonld = d
                        break
            elif data.get("@type") in ("Product", "ItemPage"):
                jsonld = data
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Extract fields — priority: __NEXT_DATA__ > JSON-LD > HTML text
    # ------------------------------------------------------------------

    # Name
    name = ""
    if nd:
        name = nd.get("name", nd.get("title", ""))
    if not name:
        name = jsonld.get("name", "")
    if not name:
        for sel in ("h1", ".product-title", ".product-name", "[class*='title']", "[class*='name']"):
            el = soup.select_one(sel)
            if el:
                name = _clean(el.get_text())
                if len(name) > 3:
                    break
    name = _clean(name) or slug.replace("-", " ").title()

    # Category
    category = ""
    if nd:
        cat = nd.get("category", nd.get("categories", ""))
        if isinstance(cat, list):
            category = cat[0] if cat else ""
        else:
            category = str(cat)
    if not category:
        # Try breadcrumb
        for sel in (".breadcrumb a", "nav[aria-label*='breadcrumb'] a", "[class*='breadcrumb'] a"):
            crumbs = soup.select(sel)
            if len(crumbs) >= 2:
                category = _clean(crumbs[-1].get_text())
                break
    category = _clean(category)

    # Description
    description = ""
    short_description = ""
    if nd:
        description = nd.get("description", nd.get("details", nd.get("longDescription", "")))
        short_description = nd.get("shortDescription", nd.get("summary", ""))
    if not description:
        description = jsonld.get("description", "")
    if not description:
        for sel in (
            "[class*='description']", "[class*='detail']",
            "[class*='about']", "article", "main p",
        ):
            els = soup.select(sel)
            if els:
                text = " ".join(_clean(e.get_text()) for e in els[:5])
                if len(text) > 30:
                    description = text
                    break

    # Price
    regular_price = ""
    sale_price = ""
    if nd:
        regular_price = str(nd.get("price", nd.get("regularPrice", nd.get("basePrice", ""))))
        sale_price = str(nd.get("salePrice", nd.get("discountPrice", "")))
        if sale_price == regular_price:
            sale_price = ""
    if not regular_price:
        offers = jsonld.get("offers", {})
        if isinstance(offers, list) and offers:
            offers = offers[0]
        regular_price = str(offers.get("price", ""))
    if not regular_price:
        # Scan page text for price pattern
        for sel in ("[class*='price']", "[class*='cost']", "[class*='amount']"):
            el = soup.select_one(sel)
            if el:
                regular_price = _extract_price(el.get_text())
                if regular_price:
                    break

    # Stock
    in_stock = True
    stock_qty = ""
    if nd:
        stock_raw = nd.get("stock", nd.get("stockQuantity", nd.get("quantity", nd.get("inventory", ""))))
        if stock_raw is not None:
            try:
                stock_qty = str(int(stock_raw))
                in_stock = int(stock_raw) > 0
            except (ValueError, TypeError):
                in_stock = str(stock_raw).lower() not in ("0", "false", "out of stock", "")
        in_stock_raw = nd.get("inStock", nd.get("available", nd.get("isAvailable", None)))
        if in_stock_raw is not None:
            in_stock = bool(in_stock_raw)
    # HTML fallback for stock
    for sel in ("[class*='stock']", "[class*='availability']", "[class*='inventory']"):
        el = soup.select_one(sel)
        if el:
            t = el.get_text().lower()
            if "out" in t or "unavailable" in t:
                in_stock = False
            elif "in stock" in t or "available" in t:
                in_stock = True
            m = re.search(r"(\d+)", t)
            if m and not stock_qty:
                stock_qty = m.group(1)
            break

    # Images
    images = []
    if nd:
        for key in ("images", "image", "photos", "gallery", "media"):
            img_data = nd.get(key)
            if img_data:
                if isinstance(img_data, str):
                    images.append(img_data)
                elif isinstance(img_data, list):
                    for item in img_data:
                        if isinstance(item, str):
                            images.append(item)
                        elif isinstance(item, dict):
                            for k in ("url", "src", "href", "path"):
                                if item.get(k):
                                    images.append(item[k])
                                    break
                break
    if not images:
        for img in soup.select("img[src*='upload'], img[src*='product'], img[src*='cloudinary']"):
            src = img.get("src", "")
            if src and src not in images:
                images.append(src)
    # Make absolute
    images = [
        urljoin(BASE_URL, img) if img.startswith("/") else img
        for img in images if img
    ]

    # Tags
    tags = []
    if nd:
        t = nd.get("tags", nd.get("keywords", []))
        if isinstance(t, list):
            tags = [str(x) for x in t]
        elif isinstance(t, str):
            tags = [x.strip() for x in t.split(",") if x.strip()]

    # Attributes (from Next.js data)
    attributes = {}
    if nd:
        for key in ("attributes", "specs", "specifications", "properties", "details"):
            attr_data = nd.get(key)
            if attr_data and isinstance(attr_data, dict):
                attributes = attr_data
                break
            elif attr_data and isinstance(attr_data, list):
                for item in attr_data:
                    if isinstance(item, dict):
                        k = item.get("name", item.get("key", item.get("label", "")))
                        v = item.get("value", item.get("val", ""))
                        if k:
                            attributes[k] = v
                break

    # Variations
    variations = _extract_variations(nd, soup, slug, regular_price)

    # Reviews
    reviews = _extract_reviews(nd, soup, slug)

    sku = _slug_to_sku(slug)
    if nd:
        sku = nd.get("sku", nd.get("id", nd.get("_id", sku)))
        sku = str(sku).upper().replace(" ", "_")

    return Product(
        url=url,
        sku=sku,
        name=name,
        slug=slug,
        category=category,
        short_description=_clean(short_description),
        description=_clean(description),
        regular_price=regular_price,
        sale_price=sale_price,
        in_stock=in_stock,
        stock_qty=stock_qty,
        images=images,
        tags=tags,
        attributes=attributes,
        variations=variations,
        reviews=reviews,
    )


def _extract_variations(nd: dict, soup: BeautifulSoup, slug: str, base_price: str) -> list[Variation]:
    variations = []
    if not nd:
        return variations

    # Look for variation arrays in next data
    for key in ("variations", "variants", "options", "quantities", "sizes"):
        var_list = nd.get(key)
        if var_list and isinstance(var_list, list):
            for i, v in enumerate(var_list):
                if not isinstance(v, dict):
                    continue
                var_sku = str(v.get("sku", v.get("id", f"{slug}-var-{i+1}"))).upper()
                label = str(v.get("label", v.get("name", v.get("option", v.get("size", v.get("weight", f"Option {i+1}"))))))
                price = str(v.get("price", v.get("regularPrice", base_price)))
                sale = str(v.get("salePrice", v.get("discountPrice", "")))
                if sale == price:
                    sale = ""
                qty = str(v.get("stock", v.get("quantity", v.get("inventory", ""))))
                in_stock = bool(v.get("inStock", v.get("available", True)))

                # Collect all non-standard keys as attributes
                attrs = {
                    k: str(val)
                    for k, val in v.items()
                    if k not in ("sku", "id", "_id", "price", "salePrice", "regularPrice",
                                 "discountPrice", "stock", "quantity", "inventory",
                                 "inStock", "available", "image", "images")
                }

                variations.append(Variation(
                    sku=var_sku,
                    label=label,
                    regular_price=price,
                    sale_price=sale,
                    in_stock=in_stock,
                    stock_qty=qty,
                    attributes=attrs,
                ))
            if variations:
                break

    # HTML fallback: look for quantity/weight selectors
    if not variations:
        for sel in ("select", "[role='listbox']", "[class*='option']", "[class*='variant']"):
            opts = soup.select(f"{sel} option") or soup.select(f"{sel} [class*='option']")
            if opts:
                for i, opt in enumerate(opts):
                    label = _clean(opt.get_text())
                    val = opt.get("value", "")
                    if label and label.lower() not in ("select", "choose", "--", ""):
                        price = _extract_price(label) or base_price
                        variations.append(Variation(
                            sku=f"{_slug_to_sku(slug)}-{i+1}",
                            label=label,
                            regular_price=price,
                            sale_price="",
                            in_stock=True,
                            stock_qty="",
                            attributes={"Option": label},
                        ))
                if variations:
                    break

    return variations


def _extract_reviews(nd: dict, soup: BeautifulSoup, product_ref: str) -> list[Review]:
    reviews = []

    # From Next.js data
    if nd:
        for key in ("reviews", "comments", "ratings", "testimonials"):
            rev_list = nd.get(key)
            if rev_list and isinstance(rev_list, list):
                for r in rev_list:
                    if not isinstance(r, dict):
                        continue
                    reviewer = str(r.get("author", r.get("name", r.get("reviewer", r.get("username", "")))))
                    date = str(r.get("date", r.get("createdAt", r.get("timestamp", ""))))
                    rating_raw = r.get("rating", r.get("score", r.get("stars", 0)))
                    try:
                        rating = int(float(str(rating_raw)))
                    except (ValueError, TypeError):
                        rating = 0
                    content = _clean(str(r.get("comment", r.get("body", r.get("content", r.get("text", ""))))))
                    title = _clean(str(r.get("title", r.get("subject", ""))))
                    verified = bool(r.get("verified", r.get("verifiedPurchase", False)))
                    if reviewer or content:
                        reviews.append(Review(
                            product_sku=product_ref,
                            reviewer=reviewer,
                            email="",
                            date=date,
                            rating=rating,
                            title=title,
                            content=content,
                            verified=verified,
                        ))
                if reviews:
                    break

    # HTML fallback
    if not reviews:
        for container_sel in (
            "[class*='review']", "[class*='comment']", "[class*='testimonial']",
        ):
            containers = soup.select(container_sel)
            for el in containers:
                text = _clean(el.get_text())
                if len(text) < 20:
                    continue
                # Try to find a star rating
                rating = 0
                for star_el in el.select("[class*='star'], [class*='rating']"):
                    m = re.search(r"(\d)", star_el.get_text() + " ".join(star_el.get("class", [])))
                    if m:
                        rating = int(m.group(1))
                        break
                reviews.append(Review(
                    product_sku=product_ref,
                    reviewer="",
                    email="",
                    date="",
                    rating=rating,
                    title="",
                    content=text[:500],
                    verified=False,
                ))
            if reviews:
                break

    return reviews


# ---------------------------------------------------------------------------
# WooCommerce CSV output
# ---------------------------------------------------------------------------

PRODUCT_COLUMNS = [
    "ID", "Type", "SKU", "Name", "Published", "Is featured?",
    "Visibility in catalog", "Short description", "Description",
    "Date sale price starts", "Date sale price ends",
    "Tax status", "Tax class",
    "In stock?", "Stock", "Low stock amount", "Backorders allowed?",
    "Sold individually?",
    "Weight (kg)", "Length (cm)", "Width (cm)", "Height (cm)",
    "Allow customer reviews?", "Purchase note",
    "Sale price", "Regular price",
    "Categories", "Tags", "Shipping class",
    "Images",
    "Download limit", "Download expiry",
    "Parent", "Grouped products", "Upsells", "Cross-sells",
    "External URL", "Button text",
    "Position",
    "Attribute 1 name", "Attribute 1 value(s)", "Attribute 1 visible", "Attribute 1 global",
    "Attribute 2 name", "Attribute 2 value(s)", "Attribute 2 visible", "Attribute 2 global",
    "Attribute 3 name", "Attribute 3 value(s)", "Attribute 3 visible", "Attribute 3 global",
]

REVIEW_COLUMNS = [
    "comment_post_ID", "product_sku", "comment_author", "comment_author_email",
    "comment_date", "comment_content", "comment_approved",
    "rating", "title", "verified",
]


def _b(v: bool) -> str:
    return "1" if v else "0"


def _product_row(p: Product, position: int, parent_sku: str = "") -> dict:
    ptype = "variable" if p.variations else "simple"
    if parent_sku:
        ptype = "variation"

    # Collect attribute values for variation-capable attributes
    attr1_name, attr1_vals = "", ""
    if p.variations:
        # Use the label as Attribute 1
        all_labels = list(dict.fromkeys(v.label for v in p.variations if v.label))
        attr1_name = "Option"
        attr1_vals = " | ".join(all_labels)
    elif p.attributes:
        k = next(iter(p.attributes))
        attr1_name = k
        v = p.attributes[k]
        attr1_vals = v if isinstance(v, str) else " | ".join(str(x) for x in v) if isinstance(v, list) else str(v)

    attr2_name, attr2_vals = "", ""
    if len(p.attributes) > 1:
        k = list(p.attributes.keys())[1]
        attr2_name = k
        v = p.attributes[k]
        attr2_vals = v if isinstance(v, str) else " | ".join(str(x) for x in v) if isinstance(v, list) else str(v)

    return {
        "ID": "",
        "Type": ptype,
        "SKU": p.sku,
        "Name": p.name,
        "Published": "1",
        "Is featured?": "0",
        "Visibility in catalog": "visible",
        "Short description": p.short_description or p.description[:200],
        "Description": p.description,
        "Date sale price starts": "",
        "Date sale price ends": "",
        "Tax status": "taxable",
        "Tax class": "",
        "In stock?": _b(p.in_stock),
        "Stock": p.stock_qty,
        "Low stock amount": "",
        "Backorders allowed?": "0",
        "Sold individually?": "0",
        "Weight (kg)": "",
        "Length (cm)": "",
        "Width (cm)": "",
        "Height (cm)": "",
        "Allow customer reviews?": "1",
        "Purchase note": "",
        "Sale price": p.sale_price,
        "Regular price": p.regular_price,
        "Categories": p.category,
        "Tags": ", ".join(p.tags),
        "Shipping class": "",
        "Images": " | ".join(p.images),
        "Download limit": "",
        "Download expiry": "",
        "Parent": parent_sku,
        "Grouped products": "",
        "Upsells": "",
        "Cross-sells": "",
        "External URL": "",
        "Button text": "",
        "Position": str(position),
        "Attribute 1 name": attr1_name,
        "Attribute 1 value(s)": attr1_vals,
        "Attribute 1 visible": "1" if attr1_name else "",
        "Attribute 1 global": "1" if attr1_name else "",
        "Attribute 2 name": attr2_name,
        "Attribute 2 value(s)": attr2_vals,
        "Attribute 2 visible": "1" if attr2_name else "",
        "Attribute 2 global": "1" if attr2_name else "",
        "Attribute 3 name": "",
        "Attribute 3 value(s)": "",
        "Attribute 3 visible": "",
        "Attribute 3 global": "",
    }


def _variation_row(v: Variation, parent: Product, position: int) -> dict:
    row = _product_row(parent, position, parent_sku=parent.sku)
    row["Type"] = "variation"
    row["SKU"] = v.sku
    row["Regular price"] = v.regular_price
    row["Sale price"] = v.sale_price
    row["In stock?"] = _b(v.in_stock)
    row["Stock"] = v.stock_qty
    row["Short description"] = v.label
    row["Description"] = ""
    row["Categories"] = ""
    row["Tags"] = ""
    row["Images"] = ""
    row["Attribute 1 name"] = "Option"
    row["Attribute 1 value(s)"] = v.label
    row["Attribute 1 visible"] = "1"
    row["Attribute 1 global"] = "1"
    row["Attribute 2 name"] = ""
    row["Attribute 2 value(s)"] = ""
    row["Attribute 2 visible"] = ""
    row["Attribute 2 global"] = ""
    return row


def write_products_csv(products: list[Product], path: str):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=PRODUCT_COLUMNS, extrasaction="ignore")
        w.writeheader()
        for pos, p in enumerate(products, 1):
            w.writerow(_product_row(p, pos))
            for vpos, v in enumerate(p.variations, 1):
                w.writerow(_variation_row(v, p, vpos))
    log.info("Products CSV → %s  (%d products, %d variation rows)",
             path, len(products), sum(len(p.variations) for p in products))


def write_reviews_csv(products: list[Product], path: str):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=REVIEW_COLUMNS, extrasaction="ignore")
        w.writeheader()
        for p in products:
            for r in p.reviews:
                w.writerow({
                    "comment_post_ID": "",
                    "product_sku": p.sku,
                    "comment_author": r.reviewer,
                    "comment_author_email": r.email,
                    "comment_date": r.date,
                    "comment_content": r.content,
                    "comment_approved": "1",
                    "rating": str(r.rating),
                    "title": r.title,
                    "verified": _b(r.verified),
                })
    total = sum(len(p.reviews) for p in products)
    log.info("Reviews CSV → %s  (%d reviews)", path, total)


def write_json(products: list[Product], path: str):
    import dataclasses
    with open(path, "w", encoding="utf-8") as f:
        json.dump([dataclasses.asdict(p) for p in products], f, indent=2, ensure_ascii=False)
    log.info("JSON dump → %s", path)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Scrape darkchemsite.com → WooCommerce CSV"
    )
    parser.add_argument("--output", default="products.csv")
    parser.add_argument("--reviews", default="reviews.csv")
    parser.add_argument("--json-out", default="products.json")
    parser.add_argument("--delay", type=float, default=1.5)
    parser.add_argument("--headless", action="store_true",
                        help="Run browser headless (default: visible window)")
    parser.add_argument("--product-url", action="append", default=[], dest="product_urls")
    args = parser.parse_args()

    browser = Browser(headless=args.headless, delay=args.delay)
    browser.start()
    products: list[Product] = []

    try:
        if args.product_urls:
            urls = args.product_urls
        else:
            urls = collect_product_urls(browser)

        if not urls:
            log.error("No product URLs found.")
            sys.exit(1)

        for i, url in enumerate(urls, 1):
            log.info("[%d/%d] %s", i, len(urls), url)
            html, nd = browser.get(url, settle_ms=3000)
            product = parse_product_page(html, nd, url)
            if product:
                products.append(product)
                log.info("  → %s | price=%s | vars=%d | reviews=%d",
                         product.name, product.regular_price,
                         len(product.variations), len(product.reviews))
            else:
                log.warning("  → parse failed for %s", url)

    finally:
        browser.close()

    if not products:
        log.error("No products scraped.")
        sys.exit(1)

    write_products_csv(products, args.output)
    write_reviews_csv(products, args.reviews)
    write_json(products, args.json_out)
    log.info("Done. %d products scraped.", len(products))


if __name__ == "__main__":
    main()
