"""
HTML preview generator for blog posts.
Generates a self-contained HTML page that can be rendered in an iframe.
"""

from .css_processor import process_html_for_builder


def generate_blog_preview_html(post_data: dict, base_url: str = "") -> str:
    """
    Generate a self-contained HTML page previewing a blog post.

    Args:
        post_data: Dict with keys like title, html_content, thumbnail,
                   meta_description, tags, categories, published_at, url_key
        base_url: Base URL for resolving relative image URLs

    Returns:
        Complete HTML string suitable for rendering in an iframe
    """
    title = post_data.get("title", "Untitled")
    html_content = post_data.get("html_content", "")
    # Live capture already produced Builder.io-ready HTML with the real
    # stylesheets inlined. Running process_html_for_builder over it would
    # strip those <style> blocks and collapse the layout again — the exact
    # bug we're trying to fix. Honour the flag set by the capture pipeline.
    already_processed = bool(post_data.get("_html_already_processed"))
    thumbnail = post_data.get("thumbnail", "")
    meta_description = post_data.get("meta_description", "")
    tags = post_data.get("tags", [])
    categories = post_data.get("categories", [])
    published_at = post_data.get("published_at", "")
    url_key = post_data.get("url_key", "")
    page_type = post_data.get("page_type", "")
    is_static_page = page_type == "static"

    # Build tags HTML
    tags_html = ""
    if tags:
        tag_items = "".join(f'<span class="tag">{t}</span>' for t in tags if t)
        tags_html = f'<div class="tags">{tag_items}</div>'

    # Build categories HTML
    categories_html = ""
    if categories:
        if isinstance(categories, list):
            cat_items = "".join(
                f'<span class="category">{c}</span>' for c in categories if c
            )
        else:
            cat_items = f'<span class="category">{categories}</span>'
        categories_html = f'<div class="categories">{cat_items}</div>'

    # Build thumbnail HTML
    thumbnail_html = ""
    if thumbnail:
        thumbnail_html = f'<div class="thumbnail"><img src="{thumbnail}" alt="{title}" /></div>'

    # Build meta line
    meta_parts = []
    if published_at:
        meta_parts.append(f'<span class="date">{published_at}</span>')
    if url_key:
        meta_parts.append(f'<span class="url-key">/{url_key}</span>')
    meta_html = (
        f'<div class="meta">{"  |  ".join(meta_parts)}</div>' if meta_parts else ""
    )

    body_padding = "0" if is_static_page else "24px"
    body_max_width = "1440px" if is_static_page else "900px"
    body_margin = "0 auto"
    header_html = ""
    if not is_static_page:
        header_html = f"""
    <div class="blog-header">
        <h1>{title}</h1>
        {meta_html}
        {f'<p class="description">{meta_description}</p>' if meta_description else ''}
        {categories_html}
        {tags_html}
    </div>
    {thumbnail_html}"""

    return f"""<!DOCTYPE html>
<html lang="zh-HK">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{title}</title>
    <style>
        * {{
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }}
        body {{
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
                         "Helvetica Neue", Arial, "Noto Sans TC", sans-serif;
            line-height: 1.7;
            color: #333;
            background: #fff;
            padding: {body_padding};
            max-width: {body_max_width};
            margin: {body_margin};
        }}
        .blog-header {{
            margin-bottom: 24px;
            border-bottom: 2px solid #e0e0e0;
            padding-bottom: 16px;
        }}
        .blog-header h1 {{
            font-size: 28px;
            line-height: 1.3;
            color: #1a1a1a;
            margin-bottom: 8px;
        }}
        .meta {{
            font-size: 13px;
            color: #888;
            margin-bottom: 8px;
        }}
        .meta .date {{
            margin-right: 8px;
        }}
        .meta .url-key {{
            font-family: monospace;
            background: #f5f5f5;
            padding: 2px 6px;
            border-radius: 3px;
            font-size: 12px;
        }}
        .description {{
            font-size: 15px;
            color: #666;
            font-style: italic;
            margin-bottom: 12px;
        }}
        .tags, .categories {{
            display: flex;
            flex-wrap: wrap;
            gap: 6px;
            margin-bottom: 8px;
        }}
        .tag {{
            background: #e3f2fd;
            color: #1565c0;
            padding: 2px 10px;
            border-radius: 12px;
            font-size: 12px;
        }}
        .category {{
            background: #f3e5f5;
            color: #7b1fa2;
            padding: 2px 10px;
            border-radius: 12px;
            font-size: 12px;
        }}
        .thumbnail {{
            margin-bottom: 20px;
        }}
        .thumbnail img {{
            width: 100%;
            max-height: 400px;
            object-fit: cover;
            border-radius: 8px;
        }}
        .blog-content {{
            font-size: 16px;
        }}
        .blog-content img {{
            max-width: 100%;
            height: auto;
            border-radius: 6px;
            margin: 12px 0;
        }}
        .blog-content h2 {{
            font-size: 22px;
            margin: 24px 0 12px;
            color: #1a1a1a;
        }}
        .blog-content h3 {{
            font-size: 18px;
            margin: 20px 0 10px;
            color: #333;
        }}
        .blog-content p {{
            margin-bottom: 14px;
        }}
        .blog-content ul, .blog-content ol {{
            margin: 10px 0 14px 24px;
        }}
        .blog-content li {{
            margin-bottom: 6px;
        }}
        .blog-content a {{
            color: #1565c0;
            text-decoration: underline;
        }}
        .blog-content table {{
            width: 100%;
            border-collapse: collapse;
            margin: 16px 0;
        }}
        .blog-content th, .blog-content td {{
            border: 1px solid #ddd;
            padding: 8px 12px;
            text-align: left;
        }}
        .blog-content th {{
            background: #f5f5f5;
            font-weight: 600;
        }}
        .blog-content blockquote {{
            border-left: 4px solid #1565c0;
            margin: 16px 0;
            padding: 8px 16px;
            background: #f8f9fa;
            font-style: italic;
        }}
    </style>
</head>
<body>
    {header_html}
    <div class="blog-content">
        {html_content if already_processed else process_html_for_builder(html_content)}
    </div>
</body>
</html>"""
