"""
Product widget renderer for Magento CMS pages.

After Playwright captures a live page, product carousels rendered by Magento's
Page Builder have persistent layout issues:
  - Slick carousel state: non-active slides hidden, later rows collapse
  - Missing price/description: hidden by JS-controlled CSS classes
  - Image cropping from pixel-based slide widths snapshotted at 1280px

This module:
  1. Scans captured HTML for [data-content-type="products"] blocks
  2. Extracts product SKUs from captured card href URLs
  3. Fetches fresh product data (name, image, price) from Magento's GraphQL API
  4. Replaces each block with a clean, self-contained product carousel

The replacement carousel uses vanilla CSS/JS — no Slick dependency — and is
reliable at any viewport inside Builder.io's Custom Code container.
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# GraphQL product query
# ---------------------------------------------------------------------------
_PRODUCT_QUERY = """
query FetchProducts($skus: [String]) {
  products(filter: { sku: { in: $skus } }, pageSize: 50) {
    items {
      sku
      name
      url_key
      price_range {
        minimum_price {
          final_price { value currency }
          regular_price { value currency }
          discount { amount_off percent_off }
        }
      }
      small_image { url label }
    }
  }
}
"""


# ---------------------------------------------------------------------------
# SKU extraction
# ---------------------------------------------------------------------------
# Magento SKUs appear at the end of product URL keys, e.g.:
#   /hk/zh/goldfully金寶麗-pm_08389     (after .html strip by capture JS)
#   /hk/zh/goldfully金寶麗-pm_08389.html (before strip)
# Pattern: hyphen + 2-5 lowercase letters + underscore + 3+ digits
_SKU_RE = re.compile(r"-([a-z]{1,5}_\d{3,})(?:[.\"'/?#\s]|$)", re.IGNORECASE)


def _extract_skus(widget_soup) -> list[str]:
    """Pull unique Magento SKUs from product link href attributes."""
    skus: list[str] = []
    for a in widget_soup.find_all("a", href=True):
        m = _SKU_RE.search(a["href"])
        if m:
            sku = m.group(1).upper()
            if sku not in skus:
                skus.append(sku)
    return skus


# ---------------------------------------------------------------------------
# GraphQL fetch
# ---------------------------------------------------------------------------
def _fetch_products(graphql_url: str, skus: list[str]) -> list[dict]:
    """Return product dicts for `skus`. Returns [] on any error."""
    if not skus:
        return []
    try:
        import requests as _req
    except ImportError:
        logger.warning("requests not available — skipping product fetch")
        return []
    try:
        resp = _req.post(
            graphql_url,
            json={"query": _PRODUCT_QUERY, "variables": {"skus": skus}},
            headers={
                "Content-Type": "application/json",
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/121.0.0.0 Safari/537.36"
                ),
            },
            timeout=20,
        )
        data = resp.json()
        return (data.get("data") or {}).get("products", {}).get("items") or []
    except Exception as e:
        logger.warning("Product GraphQL fetch failed: %s", e)
        return []


# ---------------------------------------------------------------------------
# HTML escaping helper
# ---------------------------------------------------------------------------
def _esc(text: str) -> str:
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


# ---------------------------------------------------------------------------
# CSS for the replacement carousel
# ---------------------------------------------------------------------------
_CAROUSEL_CSS = """\
.pr-widget {
  width: 100%;
  box-sizing: border-box;
  font-family: "PingFang HK","PingFang TC","Noto Sans TC","Microsoft JhengHei",
               "Helvetica Neue",Arial,sans-serif;
}
.pr-widget-header {
  display: flex;
  justify-content: flex-end;
  gap: 6px;
  margin-bottom: 8px;
}
.pr-arrow-btn {
  background: #fff;
  border: 1px solid #ccc;
  border-radius: 50%;
  width: 32px;
  height: 32px;
  cursor: pointer;
  font-size: 16px;
  display: flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
  padding: 0;
}
.pr-arrow-btn:hover { background: #f0f0f0; }
.pr-viewport {
  overflow: hidden;
  width: 100%;
}
.pr-track {
  display: flex;
  gap: 12px;
  transition: transform 0.35s ease;
  will-change: transform;
}
.pr-card {
  flex: 0 0 calc(25% - 9px);
  min-width: 0;
  box-sizing: border-box;
  background: #fff;
  border: 1px solid #ebebeb;
  border-radius: 6px;
  overflow: hidden;
  text-decoration: none;
  color: inherit;
  display: block;
}
.pr-card:hover { box-shadow: 0 2px 10px rgba(0,0,0,.10); }
.pr-card-img {
  width: 100%;
  aspect-ratio: 1 / 1;
  background: #f8f8f8;
  overflow: hidden;
}
.pr-card-img img {
  width: 100%;
  height: 100%;
  object-fit: contain;
  display: block;
}
.pr-card-body {
  padding: 8px 10px 12px;
}
.pr-card-name {
  font-size: 13px;
  line-height: 1.4;
  color: #333;
  margin: 0 0 8px;
  display: -webkit-box;
  -webkit-line-clamp: 3;
  -webkit-box-orient: vertical;
  overflow: hidden;
}
.pr-price-wrap {
  display: flex;
  align-items: baseline;
  flex-wrap: wrap;
  gap: 3px 6px;
}
.pr-price-final {
  font-size: 15px;
  font-weight: 700;
  color: #e53e3e;
}
.pr-price-old {
  font-size: 12px;
  color: #999;
  text-decoration: line-through;
}
.pr-price-badge {
  font-size: 11px;
  background: #e53e3e;
  color: #fff;
  border-radius: 3px;
  padding: 1px 5px;
  font-weight: 600;
}
@media (max-width: 767px) {
  .pr-card { flex: 0 0 calc(50% - 6px); }
}
"""

# Inline JS for the carousel (one copy per carousel, keyed by wrap_id).
_CAROUSEL_JS_TEMPLATE = """\
(function(){
  var W = document.getElementById("{wrap_id}");
  if (!W) return;
  var track = W.querySelector(".pr-track");
  var cards = track ? track.querySelectorAll(".pr-card") : [];
  if (!track || !cards.length) return;
  var btnP = W.querySelector(".pr-btn-prev");
  var btnN = W.querySelector(".pr-btn-next");
  var idx = 0;
  function perView() { return window.innerWidth < 768 ? 2 : 4; }
  function maxIdx() { return Math.max(0, cards.length - perView()); }
  function go(n) {
    idx = Math.max(0, Math.min(maxIdx(), n));
    var vp = W.querySelector(".pr-viewport");
    var cardW = vp ? (vp.offsetWidth - (perView()-1)*12) / perView() : 0;
    track.style.transform = "translateX(-" + idx*(cardW+12) + "px)";
  }
  if (btnP) btnP.addEventListener("click", function(){ go(idx-1); });
  if (btnN) btnN.addEventListener("click", function(){ go(idx+1); });
  window.addEventListener("resize", function(){ go(Math.min(idx, maxIdx())); });
})();
"""


def _render_carousel(
    products: list[dict],
    site_base_url: str,
    wrap_id: str,
) -> str:
    """Build self-contained HTML for one product carousel."""
    base = site_base_url.rstrip("/")
    cards: list[str] = []

    for p in products:
        name = p.get("name") or ""
        url_key = p.get("url_key") or ""
        href = f"{base}/hk/zh/{url_key}" if url_key else "#"

        img_data = p.get("small_image") or {}
        img_url = img_data.get("url") or ""
        img_alt = img_data.get("label") or name

        mp = (p.get("price_range") or {}).get("minimum_price") or {}
        fp_d = mp.get("final_price") or {}
        rp_d = mp.get("regular_price") or {}
        disc = mp.get("discount") or {}

        fp = fp_d.get("value") or 0
        rp = rp_d.get("value") or 0
        pct = disc.get("percent_off") or 0
        curr = fp_d.get("currency") or "HKD"
        sym = "HK$" if curr == "HKD" else f"{curr}\xa0"

        fp_str = f"{sym}{fp:,.0f}"
        rp_str = f"{sym}{rp:,.0f}"
        has_disc = pct > 0.5 and rp > fp

        if has_disc:
            price_html = (
                f'<span class="pr-price-old">{rp_str}</span>'
                f'<span class="pr-price-final">{fp_str}</span>'
                f'<span class="pr-price-badge">-{pct:.0f}%</span>'
            )
        else:
            price_html = f'<span class="pr-price-final">{fp_str}</span>'

        img_html = (
            f'<img src="{_esc(img_url)}" alt="{_esc(img_alt)}"'
            f' loading="eager" referrerpolicy="no-referrer">'
            if img_url else ""
        )

        cards.append(
            f'<a href="{href}" class="pr-card">'
            f'<div class="pr-card-img">{img_html}</div>'
            f'<div class="pr-card-body">'
            f'<p class="pr-card-name">{_esc(name)}</p>'
            f'<div class="pr-price-wrap">{price_html}</div>'
            f'</div>'
            f'</a>'
        )

    js = _CAROUSEL_JS_TEMPLATE.replace("{wrap_id}", wrap_id)

    return (
        f'<div class="pr-widget" id="{wrap_id}">'
        f'<div class="pr-widget-header">'
        f'<button class="pr-arrow-btn pr-btn-prev">&#8249;</button>'
        f'<button class="pr-arrow-btn pr-btn-next">&#8250;</button>'
        f'</div>'
        f'<div class="pr-viewport"><div class="pr-track">'
        + "\n".join(cards)
        + "</div></div>"
        f"</div>"
        f"<script>{js}</script>"
    )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------
def rewrite_product_widgets(
    html: str,
    graphql_url: str,
    site_base_url: str,
) -> str:
    """
    Replace Magento [data-content-type="products"] blocks with clean HTML.

    Finds every product widget in `html`, fetches live product data from the
    Magento GraphQL endpoint, and substitutes a self-contained carousel that
    renders correctly at any viewport without Slick.

    Falls back to the original HTML on any error or if no products are found.
    """
    if not html or 'data-content-type="products"' not in html:
        return html

    try:
        from bs4 import BeautifulSoup
    except ImportError:
        logger.warning("beautifulsoup4 not available — skipping product rewrite")
        return html

    try:
        soup = BeautifulSoup(html, "html.parser")
        widgets = soup.find_all(attrs={"data-content-type": "products"})
        if not widgets:
            return html

        # Shared CSS injected once, before the first widget.
        css_injected = False
        counter = 0

        for widget in widgets:
            skus = _extract_skus(widget)
            if not skus:
                logger.debug("product widget: no SKUs found, leaving as-is")
                continue

            products = _fetch_products(graphql_url, skus)
            if not products:
                logger.warning(
                    "product widget: GraphQL returned nothing for %d SKUs (%s…)",
                    len(skus), skus[0] if skus else "",
                )
                continue

            # Preserve page order (Slick clone dedup: skus is already unique).
            order = {s: i for i, s in enumerate(skus)}
            products_sorted = sorted(
                products,
                key=lambda p: order.get((p.get("sku") or "").upper(), 999),
            )

            counter += 1
            wrap_id = f"pr-widget-{counter}"

            carousel_html = _render_carousel(products_sorted, site_base_url, wrap_id)

            if not css_injected:
                carousel_html = f"<style>{_CAROUSEL_CSS}</style>" + carousel_html
                css_injected = True

            widget.replace_with(BeautifulSoup(carousel_html, "html.parser"))
            logger.info(
                "product widget %d: replaced %d cards (SKUs: %s)",
                counter, len(products_sorted), ", ".join(skus[:4]),
            )

        return str(soup)

    except Exception as e:
        logger.warning("rewrite_product_widgets error: %s", e)
        return html
