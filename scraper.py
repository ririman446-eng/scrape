#!/usr/bin/env python3
"""
WooCommerce product scraper for darkchemsite.com
Outputs products, variations, and reviews in WooCommerce-compatible CSV format.

Usage:
    python scraper.py [--output products.csv] [--reviews reviews.csv]
                      [--delay 1.5] [--max-pages 0] [--use-playwright]

Requirements:
    pip install -r requirements.txt
"""

import argparse
import csv
import json
import logging
import re
import sys
import time
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

BASE_URL = "https://www.darkchemsite.com"
PRODUCTS_URL = f"{BASE_URL}/products/"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Referer": BASE_URL + "/",
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
    rating: int          # 1-5
    title: str
    content: str
    verified: bool


@dataclass
class Variation:
    """A single product variation (child of a variable product)."""
    variation_id: int
    sku: str
    regular_price: str
    sale_price: str
    stock_qty: str
    in_stock: bool
    weight: str
    length: str
    width: str
    height: str
    image_url: str
    attributes: dict     # {"pa_size": "Large", "pa_color": "Red"}
    description: str


@dataclass
class Product:
    url: str
    product_id: int
    sku: str
    name: str
    type: str            # simple | variable | grouped | external
    status: str          # publish | draft | private
    featured: bool
    catalog_visibility: str   # visible | catalog | search | hidden
    short_description: str
    description: str
    regular_price: str
    sale_price: str
    date_sale_starts: str
    date_sale_ends: str
    tax_status: str      # taxable | shipping | none
    tax_class: str
    in_stock: bool
    stock_qty: str
    low_stock_amount: str
    backorders: str      # no | notify | yes
    sold_individually: bool
    weight: str
    length: str
    width: str
    height: str
    allow_reviews: bool
    purchase_note: str
    categories: list     # ["Cat1", "Cat1 > Sub1"]
    tags: list
    shipping_class: str
    images: list         # list of URLs
    download_limit: str
    download_expiry: str
    upsell_skus: list
    cross_sell_skus: list
    grouped_skus: list
    external_url: str
    button_text: str
    attributes: dict     # {"Size": {"values": ["S","M","L"], "visible": True, "variation": True}}
    meta_data: dict
    variations: list = field(default_factory=list)
    reviews: list = field(default_factory=list)


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

class Fetcher:
    """Simple HTTP fetcher with retry logic, optional Playwright fallback."""

    def __init__(self, delay: float = 1.5, use_playwright: bool = False):
        self.delay = delay
        self.use_playwright = use_playwright
        self.session = requests.Session()
        self.session.headers.update(HEADERS)
        self._playwright = None

    # -- requests-based fetch -----------------------------------------------

    def get(self, url: str, retries: int = 4) -> Optional[BeautifulSoup]:
        # If Playwright mode is forced, skip requests entirely
        if self.use_playwright:
            return self._playwright_get(url)

        wait = 2
        last_exc = None
        for attempt in range(retries):
            try:
                log.debug("GET %s (attempt %d)", url, attempt + 1)
                resp = self.session.get(url, timeout=30, allow_redirects=True)
                if resp.status_code == 200:
                    time.sleep(self.delay)
                    return BeautifulSoup(resp.text, "html.parser")
                if resp.status_code in (429, 503):
                    log.warning("Rate limited (%s). Waiting %ds…", resp.status_code, wait)
                    time.sleep(wait)
                    wait *= 2
                    continue
                if resp.status_code == 403:
                    log.warning("403 on %s – trying Playwright fallback", url)
                    return self._playwright_get(url)
                log.error("HTTP %s for %s", resp.status_code, url)
                return None
            except requests.exceptions.ProxyError as exc:
                # Hard proxy block – no point retrying with requests
                log.warning("Proxy error for %s – switching to Playwright: %s", url, exc)
                return self._playwright_get(url)
            except requests.RequestException as exc:
                last_exc = exc
                log.warning("Request error (%s), retry in %ds", exc, wait)
                time.sleep(wait)
                wait *= 2
        log.error("All retries exhausted for %s (last error: %s)", url, last_exc)
        return None

    # -- Playwright fallback ------------------------------------------------

    def _playwright_get(self, url: str) -> Optional[BeautifulSoup]:
        try:
            from playwright.sync_api import sync_playwright  # noqa: PLC0415
        except ImportError:
            log.error(
                "Playwright not installed. Run: pip install playwright && "
                "python -m playwright install chromium"
            )
            return None

        log.info("Using Playwright for %s", url)
        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch(
                    headless=True,
                    args=[
                        "--no-sandbox",
                        "--disable-blink-features=AutomationControlled",
                        "--disable-infobars",
                        "--disable-dev-shm-usage",
                    ],
                )
                ctx = browser.new_context(
                    user_agent=HEADERS["User-Agent"],
                    locale="en-US",
                    timezone_id="Europe/London",
                    viewport={"width": 1280, "height": 900},
                    extra_http_headers={
                        "Accept": HEADERS["Accept"],
                        "Accept-Language": HEADERS["Accept-Language"],
                        "Upgrade-Insecure-Requests": "1",
                    },
                )
                # Hide webdriver flag
                ctx.add_init_script(
                    "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"
                )
                page = ctx.new_page()

                # Try domcontentloaded first (works on most sites)
                html = None
                for wait_event in ("domcontentloaded", "load"):
                    try:
                        page.goto(url, wait_until=wait_event, timeout=90_000)
                        # Give JS a moment to render product data
                        page.wait_for_timeout(2500)
                        html = page.content()
                        break
                    except Exception as inner:  # noqa: BLE001
                        log.warning("Playwright wait=%s failed: %s – retrying", wait_event, inner)

                browser.close()

            if not html:
                log.error("Playwright: no HTML captured for %s", url)
                return None

            time.sleep(self.delay)
            return BeautifulSoup(html, "html.parser")
        except Exception as exc:  # noqa: BLE001
            log.error("Playwright failed for %s: %s", url, exc)
            return None

    def close(self):
        self.session.close()


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

def _text(el) -> str:
    return el.get_text(separator=" ", strip=True) if el else ""


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def _rating_from_class(el) -> int:
    """Extract integer rating from WooCommerce star-rating element."""
    if not el:
        return 0
    for cls in el.get("class", []):
        m = re.search(r"rating-(\d)", cls)
        if m:
            return int(m.group(1))
    # fallback: count filled stars in aria label
    aria = el.get("aria-label", "")
    m = re.search(r"(\d)", aria)
    return int(m.group(1)) if m else 0


def _parse_price(soup_el) -> tuple[str, str]:
    """Return (regular_price, sale_price) from a .price element."""
    if not soup_el:
        return "", ""
    # Sale price present: <del> = regular, <ins> = sale
    regular = soup_el.select_one("del .woocommerce-Price-amount bdi")
    sale = soup_el.select_one("ins .woocommerce-Price-amount bdi")
    if regular and sale:
        return _clean(_text(regular)), _clean(_text(sale))
    # Single price
    single = soup_el.select_one(".woocommerce-Price-amount bdi")
    if single:
        return _clean(_text(single)), ""
    return _clean(_text(soup_el)), ""


def _absolute(url: str) -> str:
    if url and not url.startswith("http"):
        return urljoin(BASE_URL, url)
    return url or ""


# ---------------------------------------------------------------------------
# Product list page – collect all product URLs
# ---------------------------------------------------------------------------

def collect_product_urls(fetcher: Fetcher, max_pages: int = 0) -> list[str]:
    urls: list[str] = []
    page = 1
    while True:
        page_url = PRODUCTS_URL if page == 1 else f"{PRODUCTS_URL}page/{page}/"
        log.info("Fetching product list page %d: %s", page, page_url)
        soup = fetcher.get(page_url)
        if soup is None:
            break

        # WooCommerce standard: ul.products > li.product
        products = soup.select("ul.products li.product a.woocommerce-loop-product__link")
        if not products:
            # Fallback: any <a> inside a li.product that leads to /product/
            products = soup.select("li.product a[href*='/product/']")
        if not products:
            log.info("No products found on page %d – stopping pagination.", page)
            break

        seen = set()
        for a in products:
            href = _absolute(a.get("href", ""))
            if href and href not in seen:
                seen.add(href)
                urls.append(href)
        log.info("  Found %d products on page %d", len(seen), page)

        # Check for next page
        next_link = (
            soup.select_one("a.next.page-numbers")
            or soup.select_one(".woocommerce-pagination a.next")
            or soup.select_one("nav.woocommerce-pagination a[rel='next']")
        )
        if not next_link:
            break
        page += 1
        if max_pages and page > max_pages:
            log.info("Reached max_pages limit (%d).", max_pages)
            break

    log.info("Total product URLs collected: %d", len(urls))
    return list(dict.fromkeys(urls))  # deduplicate, preserve order


# ---------------------------------------------------------------------------
# Single product page parser
# ---------------------------------------------------------------------------

def parse_product(soup: BeautifulSoup, url: str) -> Optional[Product]:  # noqa: PLR0912, PLR0915
    """Parse a WooCommerce single-product page into a Product dataclass."""

    # ---- Basic info -------------------------------------------------------
    name = _clean(
        _text(soup.select_one(".product_title"))
        or _text(soup.select_one("h1.entry-title"))
    )
    if not name:
        log.warning("Could not find product name at %s", url)
        return None

    # product ID from body class: post-XXXX
    product_id = 0
    for cls in soup.body.get("class", []) if soup.body else []:
        m = re.match(r"postid-(\d+)", cls)
        if m:
            product_id = int(m.group(1))
            break

    # SKU
    sku = _clean(_text(soup.select_one(".sku")))

    # Product type from body class
    ptype = "simple"
    body_classes = " ".join(soup.body.get("class", [])) if soup.body else ""
    if "product-type-variable" in body_classes:
        ptype = "variable"
    elif "product-type-grouped" in body_classes:
        ptype = "grouped"
    elif "product-type-external" in body_classes:
        ptype = "external"

    # Status / visibility
    status = "publish"
    catalog_visibility = "visible"
    featured = bool(soup.select_one(".product.featured"))

    # ---- Prices -----------------------------------------------------------
    price_el = soup.select_one(".summary .price")
    regular_price, sale_price = _parse_price(price_el)

    # Sale schedule (rarely in HTML, often only in WP admin)
    date_sale_starts = ""
    date_sale_ends = ""

    # ---- Descriptions -----------------------------------------------------
    short_desc = ""
    short_desc_el = soup.select_one(".woocommerce-product-details__short-description")
    if short_desc_el:
        short_desc = short_desc_el.decode_contents().strip()

    description = ""
    desc_el = (
        soup.select_one("#tab-description .woocommerce-Tabs-panel")
        or soup.select_one("#tab-description")
        or soup.select_one(".woocommerce-product-details__description")
        or soup.select_one(".entry-content .description")
    )
    if desc_el:
        # Remove the tab title heading
        for h in desc_el.select("h2"):
            h.decompose()
        description = desc_el.decode_contents().strip()

    # ---- Tax --------------------------------------------------------------
    tax_status = "taxable"
    tax_class = ""

    # ---- Stock ------------------------------------------------------------
    in_stock = not bool(soup.select_one(".out-of-stock"))
    stock_qty = ""
    low_stock_amount = ""
    backorders = "no"
    sold_individually = False

    stock_el = soup.select_one(".stock")
    if stock_el:
        stock_text = _text(stock_el).lower()
        if "out of stock" in stock_text:
            in_stock = False
        m = re.search(r"(\d+)\s+in stock", stock_text)
        if m:
            stock_qty = m.group(1)

    # ---- Dimensions/weight ------------------------------------------------
    weight = ""
    length_ = ""
    width = ""
    height = ""
    for row in soup.select(".woocommerce-product-attributes tr, .shop_attributes tr"):
        label = _text(row.select_one("th")).lower()
        value = _text(row.select_one("td"))
        if "weight" in label:
            weight = value
        elif "dimension" in label:
            # e.g. "10 × 5 × 3 cm"
            parts = re.split(r"[×x×]", value)
            if len(parts) >= 3:
                length_ = parts[0].strip()
                width = parts[1].strip()
                height = re.sub(r"[^\d.]", "", parts[2])

    # ---- Reviews ----------------------------------------------------------
    allow_reviews = bool(soup.select_one("#reviews") or soup.select_one("#tab-reviews"))

    # ---- Purchase note ----------------------------------------------------
    purchase_note = ""
    note_el = soup.select_one(".woocommerce-product-details__purchase-note")
    if note_el:
        purchase_note = _clean(_text(note_el))

    # ---- Categories & tags ------------------------------------------------
    categories: list[str] = []
    cat_el = soup.select_one(".posted_in")
    if cat_el:
        categories = [_clean(_text(a)) for a in cat_el.select("a")]
    # Also try breadcrumbs for hierarchy
    breadcrumb_items = soup.select(
        ".woocommerce-breadcrumb span, "
        "nav.woocommerce-breadcrumb a, "
        ".breadcrumb a"
    )
    if breadcrumb_items and not categories:
        # skip Home and the product name itself
        crumbs = [_clean(_text(b)) for b in breadcrumb_items]
        crumbs = [c for c in crumbs if c and c.lower() not in ("home", name.lower())]
        if crumbs:
            categories = crumbs

    tags: list[str] = []
    tag_el = soup.select_one(".tagged_as")
    if tag_el:
        tags = [_clean(_text(a)) for a in tag_el.select("a")]

    # ---- Shipping class ---------------------------------------------------
    shipping_class = ""

    # ---- Images -----------------------------------------------------------
    images: list[str] = []
    for img in soup.select(
        ".woocommerce-product-gallery__image a, "
        ".woocommerce-product-gallery .wp-post-image"
    ):
        src = img.get("href") or img.get("data-large_image") or img.get("src", "")
        src = _absolute(src)
        if src and src not in images:
            images.append(src)
    if not images:
        for img in soup.select(".woocommerce-product-gallery img"):
            src = (
                img.get("data-large_image")
                or img.get("data-src")
                or img.get("src", "")
            )
            src = _absolute(src)
            if src and src not in images:
                images.append(src)

    # ---- Attributes -------------------------------------------------------
    attributes: dict = {}
    for row in soup.select(".woocommerce-product-attributes tr, .shop_attributes tr"):
        label_el = row.select_one("th")
        value_el = row.select_one("td")
        if not label_el or not value_el:
            continue
        label = _clean(_text(label_el))
        value = _clean(_text(value_el))
        if label.lower() in ("weight", "dimensions"):
            continue
        attributes[label] = {
            "values": [v.strip() for v in value.split(",")],
            "visible": True,
            "variation": False,
        }

    # ---- External product -------------------------------------------------
    external_url = ""
    button_text = ""
    ext_btn = soup.select_one("a.single_add_to_cart_button[href]")
    if ptype == "external" and ext_btn:
        external_url = _absolute(ext_btn.get("href", ""))
        button_text = _clean(_text(ext_btn))

    # ---- Upsells / cross-sells (rarely available in HTML) ----------------
    upsell_skus: list[str] = []
    cross_sell_skus: list[str] = []
    grouped_skus: list[str] = []

    # ---- Variations (JSON embedded in form) ------------------------------
    variations: list[Variation] = []
    var_form = soup.select_one("form.variations_form")
    if var_form:
        raw_json = var_form.get("data-product_variations", "[]")
        try:
            var_data = json.loads(raw_json)
        except json.JSONDecodeError:
            var_data = []

        for v in var_data:
            var_attrs = {}
            for k, val in v.get("attributes", {}).items():
                # Strip "attribute_" prefix that WC adds
                attr_name = re.sub(r"^attribute_", "", k)
                var_attrs[attr_name] = val

            var_img = ""
            if v.get("image", {}).get("url"):
                var_img = v["image"]["url"]

            dim = v.get("dimensions", {})
            variations.append(
                Variation(
                    variation_id=v.get("variation_id", 0),
                    sku=v.get("sku", ""),
                    regular_price=str(v.get("display_regular_price", "")),
                    sale_price=str(v.get("display_price", "")),
                    stock_qty=str(v.get("max_qty", "")),
                    in_stock=v.get("is_in_stock", True),
                    weight=str(v.get("weight", "")),
                    length=str(dim.get("length", "")),
                    width=str(dim.get("width", "")),
                    height=str(dim.get("height", "")),
                    image_url=var_img,
                    attributes=var_attrs,
                    description=_clean(
                        BeautifulSoup(
                            v.get("variation_description", ""), "html.parser"
                        ).get_text()
                    ),
                )
            )

        # Also mark which attributes are variation attributes
        for sel in var_form.select("select[name^='attribute_']"):
            attr_name = re.sub(r"^attribute_", "", sel.get("name", ""))
            # Map to readable label via the <label> or table
            label_el = soup.find("label", {"for": sel.get("id", "")})
            readable = _clean(_text(label_el)) if label_el else attr_name
            if readable not in attributes:
                options = [
                    o.get("value", "")
                    for o in sel.select("option")
                    if o.get("value")
                ]
                attributes[readable] = {
                    "values": options,
                    "visible": True,
                    "variation": True,
                }
            else:
                attributes[readable]["variation"] = True

    # ---- Reviews ----------------------------------------------------------
    reviews = _parse_reviews(soup, sku or str(product_id))

    # ---- Meta data --------------------------------------------------------
    meta_data: dict = {}
    # Try to pick up any open-graph or schema.org meta
    for meta in soup.select("meta[property^='og:'], meta[name^='twitter:']"):
        key = meta.get("property") or meta.get("name", "")
        meta_data[key] = meta.get("content", "")

    return Product(
        url=url,
        product_id=product_id,
        sku=sku,
        name=name,
        type=ptype,
        status=status,
        featured=featured,
        catalog_visibility=catalog_visibility,
        short_description=short_desc,
        description=description,
        regular_price=regular_price,
        sale_price=sale_price,
        date_sale_starts=date_sale_starts,
        date_sale_ends=date_sale_ends,
        tax_status=tax_status,
        tax_class=tax_class,
        in_stock=in_stock,
        stock_qty=stock_qty,
        low_stock_amount=low_stock_amount,
        backorders=backorders,
        sold_individually=sold_individually,
        weight=weight,
        length=length_,
        width=width,
        height=height,
        allow_reviews=allow_reviews,
        purchase_note=purchase_note,
        categories=categories,
        tags=tags,
        shipping_class=shipping_class,
        images=images,
        download_limit="",
        download_expiry="",
        upsell_skus=upsell_skus,
        cross_sell_skus=cross_sell_skus,
        grouped_skus=grouped_skus,
        external_url=external_url,
        button_text=button_text,
        attributes=attributes,
        meta_data=meta_data,
        variations=variations,
        reviews=reviews,
    )


def _parse_reviews(soup: BeautifulSoup, product_ref: str) -> list[Review]:
    reviews: list[Review] = []
    # WooCommerce review wrapper: #reviews ol.commentlist > li.review
    for li in soup.select("#reviews ol.commentlist li.review, #tab-reviews ol.commentlist li.review"):
        reviewer = _clean(_text(li.select_one(".comment-author strong, b.fn")))
        email = ""  # email is never exposed in HTML
        date_el = li.select_one("time.woocommerce-review__published-date")
        date = date_el.get("datetime", _text(date_el)) if date_el else ""
        rating_el = li.select_one(".star-rating")
        rating = _rating_from_class(rating_el)
        if rating == 0 and rating_el:
            # Try aria-label: "Rated 4 out of 5"
            aria = rating_el.get("aria-label", "")
            m = re.search(r"(\d+)\s+out of", aria)
            rating = int(m.group(1)) if m else 0
        title = _clean(_text(li.select_one(".woocommerce-review__title, strong.review-title")))
        content_el = li.select_one(".description p, .comment-text p")
        content = _clean(_text(content_el))
        verified = bool(li.select_one(".woocommerce-review__verified"))
        reviews.append(
            Review(
                product_sku=product_ref,
                reviewer=reviewer,
                email=email,
                date=date,
                rating=rating,
                title=title,
                content=content,
                verified=verified,
            )
        )
    return reviews


# ---------------------------------------------------------------------------
# WooCommerce CSV export
# ---------------------------------------------------------------------------

# Columns match WooCommerce product importer (Tools > Import)
PRODUCT_CSV_COLUMNS = [
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
]

# Up to 10 attribute columns (extensible)
MAX_ATTRIBUTES = 10


def _bool(v: bool) -> str:
    return "1" if v else "0"


def _list_to_csv_val(lst: list) -> str:
    return ", ".join(str(x) for x in lst)


def _build_attribute_columns(all_products: list[Product]) -> list[str]:
    """Collect all attribute names used across all products to make stable columns."""
    seen: dict[str, int] = {}
    for p in all_products:
        for name in p.attributes:
            if name not in seen:
                seen[name] = len(seen) + 1
    cols = []
    for name, idx in sorted(seen.items(), key=lambda x: x[1]):
        if idx > MAX_ATTRIBUTES:
            break
        cols += [
            f"Attribute {idx} name",
            f"Attribute {idx} value(s)",
            f"Attribute {idx} visible",
            f"Attribute {idx} global",
        ]
    return cols


def _product_to_row(
    p: Product,
    attr_names: list[str],
    parent_sku: str = "",
    position: int = 0,
) -> dict:
    row = {
        "ID": str(p.product_id) if p.product_id else "",
        "Type": p.type,
        "SKU": p.sku,
        "Name": p.name,
        "Published": "1" if p.status == "publish" else "0",
        "Is featured?": _bool(p.featured),
        "Visibility in catalog": p.catalog_visibility,
        "Short description": p.short_description,
        "Description": p.description,
        "Date sale price starts": p.date_sale_starts,
        "Date sale price ends": p.date_sale_ends,
        "Tax status": p.tax_status,
        "Tax class": p.tax_class,
        "In stock?": _bool(p.in_stock),
        "Stock": p.stock_qty,
        "Low stock amount": p.low_stock_amount,
        "Backorders allowed?": _bool(p.backorders == "yes"),
        "Sold individually?": _bool(p.sold_individually),
        "Weight (kg)": p.weight,
        "Length (cm)": p.length,
        "Width (cm)": p.width,
        "Height (cm)": p.height,
        "Allow customer reviews?": _bool(p.allow_reviews),
        "Purchase note": p.purchase_note,
        "Sale price": p.sale_price,
        "Regular price": p.regular_price,
        "Categories": _list_to_csv_val(p.categories),
        "Tags": _list_to_csv_val(p.tags),
        "Shipping class": p.shipping_class,
        "Images": _list_to_csv_val(p.images),
        "Download limit": p.download_limit,
        "Download expiry": p.download_expiry,
        "Parent": parent_sku,
        "Grouped products": _list_to_csv_val(p.grouped_skus),
        "Upsells": _list_to_csv_val(p.upsell_skus),
        "Cross-sells": _list_to_csv_val(p.cross_sell_skus),
        "External URL": p.external_url,
        "Button text": p.button_text,
        "Position": str(position),
    }

    # Attribute columns
    idx = 1
    for attr_name in attr_names:
        if idx > MAX_ATTRIBUTES:
            break
        attr = p.attributes.get(attr_name)
        if attr:
            row[f"Attribute {idx} name"] = attr_name
            row[f"Attribute {idx} value(s)"] = " | ".join(attr["values"])
            row[f"Attribute {idx} visible"] = _bool(attr.get("visible", True))
            row[f"Attribute {idx} global"] = "1"
        else:
            row[f"Attribute {idx} name"] = ""
            row[f"Attribute {idx} value(s)"] = ""
            row[f"Attribute {idx} visible"] = ""
            row[f"Attribute {idx} global"] = ""
        idx += 1

    return row


def _variation_to_row(
    v: Variation,
    parent_sku: str,
    parent_name: str,
    attr_names: list[str],
    position: int,
) -> dict:
    row = {
        "ID": str(v.variation_id) if v.variation_id else "",
        "Type": "variation",
        "SKU": v.sku,
        "Name": parent_name,
        "Published": "1",
        "Is featured?": "0",
        "Visibility in catalog": "visible",
        "Short description": v.description,
        "Description": "",
        "Date sale price starts": "",
        "Date sale price ends": "",
        "Tax status": "taxable",
        "Tax class": "",
        "In stock?": _bool(v.in_stock),
        "Stock": v.stock_qty,
        "Low stock amount": "",
        "Backorders allowed?": "0",
        "Sold individually?": "0",
        "Weight (kg)": v.weight,
        "Length (cm)": v.length,
        "Width (cm)": v.width,
        "Height (cm)": v.height,
        "Allow customer reviews?": "0",
        "Purchase note": "",
        "Sale price": v.sale_price,
        "Regular price": v.regular_price,
        "Categories": "",
        "Tags": "",
        "Shipping class": "",
        "Images": v.image_url,
        "Download limit": "",
        "Download expiry": "",
        "Parent": parent_sku,
        "Grouped products": "",
        "Upsells": "",
        "Cross-sells": "",
        "External URL": "",
        "Button text": "",
        "Position": str(position),
    }

    # Attribute columns for variation
    idx = 1
    for attr_name in attr_names:
        if idx > MAX_ATTRIBUTES:
            break
        # Normalise: WC stores as lowercase "attribute_pa_xxx" → strip prefix
        val = ""
        for k, kv in v.attributes.items():
            if k.lower().replace("pa_", "") == attr_name.lower().replace("pa_", ""):
                val = kv
                break
        row[f"Attribute {idx} name"] = attr_name if val else ""
        row[f"Attribute {idx} value(s)"] = val
        row[f"Attribute {idx} visible"] = "1" if val else ""
        row[f"Attribute {idx} global"] = "1" if val else ""
        idx += 1

    return row


# WooCommerce reviews import CSV (via plugins like "Import Export Suite")
REVIEW_CSV_COLUMNS = [
    "comment_post_ID", "product_sku", "comment_author", "comment_author_email",
    "comment_date", "comment_content", "comment_approved",
    "rating", "title", "verified",
]


def write_products_csv(products: list[Product], filepath: str) -> None:
    attr_names = []
    seen: dict[str, int] = {}
    for p in products:
        for name in p.attributes:
            if name not in seen:
                seen[name] = len(seen) + 1
    attr_names = [k for k, _ in sorted(seen.items(), key=lambda x: x[1])][:MAX_ATTRIBUTES]

    attr_cols = []
    for idx, name in enumerate(attr_names, 1):
        attr_cols += [
            f"Attribute {idx} name",
            f"Attribute {idx} value(s)",
            f"Attribute {idx} visible",
            f"Attribute {idx} global",
        ]

    all_columns = PRODUCT_CSV_COLUMNS + attr_cols

    with open(filepath, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=all_columns, extrasaction="ignore")
        writer.writeheader()
        for pos, product in enumerate(products, 1):
            row = _product_to_row(product, attr_names, position=pos)
            writer.writerow(row)
            # Write variation rows directly after the parent
            for v_pos, var in enumerate(product.variations, 1):
                vrow = _variation_to_row(
                    var, product.sku or str(product.product_id),
                    product.name, attr_names, v_pos
                )
                writer.writerow(vrow)

    log.info("Products CSV written to %s", filepath)


def write_reviews_csv(products: list[Product], filepath: str) -> None:
    with open(filepath, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=REVIEW_CSV_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for product in products:
            for review in product.reviews:
                writer.writerow({
                    "comment_post_ID": str(product.product_id),
                    "product_sku": review.product_sku,
                    "comment_author": review.reviewer,
                    "comment_author_email": review.email,
                    "comment_date": review.date,
                    "comment_content": review.content,
                    "comment_approved": "1",
                    "rating": str(review.rating),
                    "title": review.title,
                    "verified": "1" if review.verified else "0",
                })
    log.info("Reviews CSV written to %s", filepath)


def write_json(products: list[Product], filepath: str) -> None:
    """Write full product data as JSON for debugging / downstream use."""
    import dataclasses

    def _serial(obj):
        if dataclasses.is_dataclass(obj):
            return dataclasses.asdict(obj)
        raise TypeError(f"Object {obj!r} not JSON serialisable")

    with open(filepath, "w", encoding="utf-8") as f:
        json.dump([dataclasses.asdict(p) for p in products], f, indent=2, ensure_ascii=False)
    log.info("JSON dump written to %s", filepath)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Scrape darkchemsite.com products → WooCommerce CSV"
    )
    parser.add_argument("--output", default="products.csv", help="Products CSV output path")
    parser.add_argument("--reviews", default="reviews.csv", help="Reviews CSV output path")
    parser.add_argument("--json", default="products.json", help="Full JSON output path")
    parser.add_argument(
        "--delay", type=float, default=1.5,
        help="Seconds to wait between requests (default: 1.5)"
    )
    parser.add_argument(
        "--max-pages", type=int, default=0,
        help="Maximum listing pages to scrape (0 = all)"
    )
    parser.add_argument(
        "--use-playwright", action="store_true",
        help="Force Playwright for JS rendering (auto-enabled on 403)"
    )
    parser.add_argument(
        "--product-url", action="append", default=[],
        dest="product_urls",
        help="Scrape a specific product URL (can be repeated; skips listing scrape)"
    )
    args = parser.parse_args()

    fetcher = Fetcher(delay=args.delay, use_playwright=args.use_playwright)
    products: list[Product] = []

    try:
        # Determine which product URLs to scrape
        if args.product_urls:
            product_urls = args.product_urls
            log.info("Scraping %d user-supplied product URLs", len(product_urls))
        else:
            product_urls = collect_product_urls(fetcher, max_pages=args.max_pages)

        if not product_urls:
            log.error("No product URLs found. Exiting.")
            sys.exit(1)

        for i, url in enumerate(product_urls, 1):
            log.info("[%d/%d] Scraping product: %s", i, len(product_urls), url)
            soup = fetcher.get(url)
            if soup is None:
                log.warning("Skipping (failed to fetch): %s", url)
                continue
            product = parse_product(soup, url)
            if product:
                products.append(product)
                log.info(
                    "  -> %s | type=%s | variations=%d | reviews=%d",
                    product.name, product.type,
                    len(product.variations), len(product.reviews),
                )
            else:
                log.warning("  -> Could not parse product at %s", url)

    finally:
        fetcher.close()

    if not products:
        log.error("No products scraped.")
        sys.exit(1)

    log.info("Scraped %d products total.", len(products))
    write_products_csv(products, args.output)
    write_reviews_csv(products, args.reviews)
    write_json(products, args.json)
    log.info("Done.")


if __name__ == "__main__":
    main()
