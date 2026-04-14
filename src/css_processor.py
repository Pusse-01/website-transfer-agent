"""
CSS processing for migrated blog content.

The source website uses Magento Page Builder which relies on:
- data-pb-style attributes with CSS selectors like #html-body [data-pb-style=XXX]
- data-content-type attributes for layout (row, column-group, column, text, etc.)
- Inline styles on some elements

When migrating to Builder.io, the #html-body selector doesn't exist, so the
embedded <style> rules don't apply and the layout collapses. This module fixes
that by rewriting the CSS selectors and adding base Page Builder layout styles.
"""

import re
from bs4 import BeautifulSoup


# Base CSS that replicates Magento Page Builder layout behavior
PAGEBUILDER_BASE_CSS = """
/* Magento Page Builder base layout styles */
[data-content-type="row"] {
    box-sizing: border-box;
}
[data-content-type="row"][data-appearance="contained"] {
    max-width: 100%;
    margin: 0 auto;
}
[data-content-type="row"][data-appearance="contained"] > [data-element="inner"] {
    max-width: 100%;
}
[data-content-type="column-group"] {
    display: flex;
    flex-wrap: wrap;
}
.pagebuilder-column-group {
    display: flex;
    flex-wrap: wrap;
    width: 100%;
}
.pagebuilder-column-line {
    display: flex;
    flex-wrap: wrap;
    width: 100%;
}
.pagebuilder-column {
    box-sizing: border-box;
}
[data-content-type="text"] {
    margin-bottom: 0;
    word-wrap: break-word;
}
[data-content-type="text"] p {
    margin-bottom: 10px;
}
[data-content-type="heading"] {
    margin-bottom: 10px;
}
[data-content-type="image"] {
    margin-bottom: 10px;
}
[data-content-type="image"] img {
    max-width: 100%;
    height: auto;
}

/* Table styles */
table {
    border-collapse: collapse;
    margin-bottom: 16px;
}
table td, table th {
    padding: 8px 12px;
    vertical-align: top;
}

/* Image sizing */
img {
    max-width: 100%;
    height: auto;
}

/* General typography */
p { margin-bottom: 10px; line-height: 1.6; }
h2 { margin: 20px 0 12px; }
h3 { margin: 16px 0 10px; }
a { color: #236fa1; }
mark { padding: 2px 4px; }
"""


def process_html_for_builder(html_content: str) -> str:
    """
    Process scraped HTML so it renders correctly in Builder.io.

    - Rewrites #html-body [data-pb-style=X] selectors to just [data-pb-style=X]
    - Adds base Page Builder layout CSS
    - Wraps everything in a styled container

    If the HTML already carries a full-page snapshot produced by
    :mod:`src.page_extractor` (detected via the ``magento-embed`` /
    ``magento-embed-snapshot`` wrapper classes), we leave it untouched: those
    fragments already bundle the source site's compiled CSS and would only be
    harmed by extra scoping rules.
    """
    if not html_content:
        return html_content

    if 'magento-embed-snapshot' in html_content or 'class="magento-embed"' in html_content:
        return html_content

    soup = BeautifulSoup(html_content, "html.parser")

    # Extract and fix existing <style> blocks
    existing_styles = []
    for style_tag in soup.find_all("style"):
        css_text = style_tag.string or ""
        # Remove #html-body prefix so selectors work without Magento's body ID
        fixed_css = re.sub(r'#html-body\s+', '', css_text)
        existing_styles.append(fixed_css)
        style_tag.decompose()

    # Build the combined CSS
    combined_css = PAGEBUILDER_BASE_CSS + "\n" + "\n".join(existing_styles)

    # Build the final HTML with styles included
    body_html = str(soup)

    styled_html = f"""<div class="migrated-blog-content">
<style>
.migrated-blog-content {{
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
                 "Helvetica Neue", Arial, "Noto Sans TC", "PingFang HK", sans-serif;
    font-size: 16px;
    line-height: 1.7;
    color: #333;
    max-width: 100%;
    overflow-x: hidden;
}}
.migrated-blog-content img {{
    max-width: 100%;
    height: auto;
}}
{combined_css}
</style>
{body_html}
</div>"""

    return styled_html
