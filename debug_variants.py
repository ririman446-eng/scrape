"""Run this once to find the real HTML structure of quantity buttons."""
from playwright.sync_api import sync_playwright
import re, json

with sync_playwright() as pw:
    browser = pw.chromium.launch(headless=False)
    page = browser.new_context(
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36"
    ).new_page()
    page.goto("https://www.darkchemsite.com/products/4fadb",
              wait_until="domcontentloaded", timeout=90000)
    page.wait_for_timeout(5000)

    # Find every leaf element whose text looks like a quantity (25g, 50g, etc.)
    qty_elements = page.evaluate("""() => {
        var re = /^\\d+\\s*(g|mg|ml|kg)s?$/i;
        var results = [];
        document.querySelectorAll('*').forEach(function(el) {
            var t = el.innerText ? el.innerText.trim() : '';
            if (re.test(t) && el.children.length === 0) {
                results.push({
                    tag: el.tagName,
                    cls: el.className,
                    text: t,
                    parent_tag: el.parentElement ? el.parentElement.tagName : '',
                    parent_cls: el.parentElement ? el.parentElement.className : '',
                    grandparent_cls: (el.parentElement && el.parentElement.parentElement)
                                     ? el.parentElement.parentElement.className : ''
                });
            }
        });
        return results;
    }""")

    print("=== Quantity-like leaf elements ===")
    for x in qty_elements:
        print(x)

    # Find the price element and its structure
    price_elements = page.evaluate("""() => {
        var re = /[€$£]\\d/;
        var results = [];
        document.querySelectorAll('*').forEach(function(el) {
            var t = el.innerText ? el.innerText.trim() : '';
            if (re.test(t) && el.children.length <= 2 && t.length < 30) {
                results.push({
                    tag: el.tagName,
                    cls: el.className,
                    text: t
                });
            }
        });
        return results;
    }""")

    print()
    print("=== Price-like elements ===")
    for x in price_elements[:15]:
        print(x)

    # Dump HTML around 'Choose quantity'
    html = page.content()
    browser.close()

m = re.search(r'.{0,300}[Cc]hoose.{0,800}', html, re.S)
if m:
    print()
    print("=== HTML around 'Choose quantity' ===")
    print(m.group(0)[:2000])
else:
    print("'Choose quantity' not found in HTML")

with open("debug_report.txt", "w", encoding="utf-8") as f:
    f.write("QUANTITY ELEMENTS:\n")
    f.write(json.dumps(qty_elements, indent=2))
    f.write("\n\nPRICE ELEMENTS:\n")
    f.write(json.dumps(price_elements[:15], indent=2))
    if m:
        f.write("\n\nHTML AROUND CHOOSE QUANTITY:\n")
        f.write(m.group(0)[:3000])
print("\nSaved to debug_report.txt")
