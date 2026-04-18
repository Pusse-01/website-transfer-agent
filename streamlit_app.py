"""
Website Transfer Agent - Streamlit Dashboard

Unified pipeline for migrating both blog posts and static CMS pages
from Magento to Builder.io.

Features:
- Upload Excel files (Blog Post List or Static Page List)
- Auto-detects page type and reads accordingly
- Single-button-click migration pipeline
- Live progress tracking with per-page logging
- Preview scraped content before upload
- Export results to Excel with agent status

Usage:
    streamlit run streamlit_app.py
"""

import io
import json
import os
import tempfile
from datetime import datetime
from pathlib import Path

import streamlit as st
import streamlit.components.v1 as components
from dotenv import load_dotenv

from src.scraper import BlogScraper, StaticPageScraper
from src.html_preview import generate_blog_preview_html
from src.builder_client import BuilderClient
from src.image_handler import ImageHandler
from src.excel_reader import read_blog_list, read_static_page_list, detect_excel_type
from src.excel_writer import export_blog_results
from src.migration_agent import MigrationAgent
from src.state_persistence import save_run, load_run, load_latest_run, list_runs
from src.live_capture import capture_live_fragment, is_available as live_capture_available
from src.llm_layout_fixer import (
    is_enabled as llm_fix_enabled,
    refine_layout_with_screenshots,
)

load_dotenv()

# ---------------------------------------------------------------------------
# Page config
# ---------------------------------------------------------------------------
st.set_page_config(
    page_title="Website Transfer Agent",
    page_icon="",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ---------------------------------------------------------------------------
# Session state defaults
# ---------------------------------------------------------------------------
DEFAULTS = {
    "scraped_posts": {},
    "selected_post": None,
    "scrape_log": [],
    "upload_results": {},
    "builder_entries": [],
    "migration_results": None,
    "loaded_pages": [],
    "excel_type": None,
    "pipeline_running": False,
    "pipeline_log": [],
    "current_run_id": None,
    # Batch-preview selections (indices into `loaded_pages`)
    "batch_scrape_selection": [],
    # Keys in `scraped_posts` marked for batch publish
    "batch_publish_selection": [],
}

for key, default in DEFAULTS.items():
    if key not in st.session_state:
        st.session_state[key] = default

# ---------------------------------------------------------------------------
# Auto-load last run from disk on first page load
# ---------------------------------------------------------------------------
if not st.session_state.get("_state_loaded"):
    latest = load_latest_run()
    if latest and latest.get("migration_results"):
        st.session_state.migration_results = latest["migration_results"]
        st.session_state.pipeline_log = latest.get("pipeline_log", [])
        st.session_state.current_run_id = latest["run_id"]
        st.session_state["results_excel_path"] = latest.get("results_excel_path", "")
        st.session_state["log_file_path"] = latest.get("log_file_path", "")
    st.session_state["_state_loaded"] = True

# ---------------------------------------------------------------------------
# Sidebar - Configuration
# ---------------------------------------------------------------------------
with st.sidebar:
    st.header("Configuration")

    source_url = st.text_input(
        "Source Website URL",
        value=os.getenv("SOURCE_BASE_URL", "https://www.pricerite.com.hk"),
    )
    blog_path = st.text_input(
        "Blog Path",
        value=os.getenv("SOURCE_BLOG_PATH", "/blog/"),
    )

    st.divider()
    st.subheader("Builder.io")
    builder_blog_model = st.text_input(
        "Blog Post Model Name",
        value=os.getenv("BUILDER_BLOG_MODEL", "blog-post"),
        help="Builder.io model API name for blog posts",
    )
    builder_page_model = st.text_input(
        "Static Page Model Name",
        value=os.getenv("BUILDER_PAGE_MODEL", "page"),
        help="Builder.io model API name for static pages (check Models in Builder.io settings)",
    )
    builder_public_key = st.text_input(
        "Public API Key",
        value=os.getenv("BUILDER_PUBLIC_KEY", "6be3ec8a86714634979b0d3ca2064d06"),
        help="Used for reading content from Builder.io",
    )
    builder_private_key = st.text_input(
        "Private API Key",
        value=os.getenv("BUILDER_PRIVATE_API_KEY", ""),
        type="password",
        help="Required for uploading to Builder.io",
    )

    st.divider()
    st.subheader("High-Fidelity Capture")
    _live_avail = live_capture_available()
    use_live_capture = st.checkbox(
        "Use live browser capture (recommended)",
        value=_live_avail,
        disabled=not _live_avail,
        help=(
            "Renders each page in Chromium and pulls in the real stylesheets the "
            "browser uses. This is the only way to preserve carousels, sliders, "
            "hover states, and fonts. If unchecked, the legacy GraphQL/HTML "
            "scraper is used."
        ),
    )
    if not _live_avail:
        st.caption("Playwright not installed — run `pip install playwright && playwright install chromium`.")

    with st.expander("Magento admin login (only for non-public pages)", expanded=False):
        st.caption(
            "Not required for CMS pages like `/hk/zh/intro-fur-tips` — those are already public. "
            "Provide credentials only if the target URL is behind admin auth."
        )
        magento_admin_url = st.text_input(
            "Admin URL",
            value=os.getenv("MAGENTO_ADMIN_URL", "https://www.pricerite.com.hk/adminControl/"),
        )
        magento_username = st.text_input("Username", value=os.getenv("MAGENTO_USERNAME", ""))
        magento_password = st.text_input(
            "Password",
            value=os.getenv("MAGENTO_PASSWORD", ""),
            type="password",
        )
        magento_otp = st.text_input(
            "OTP (authenticator code)",
            value="",
            help="Leave blank if not using 2FA. If you have 2FA enabled, enter the current code right before hitting Scrape.",
        )

    st.divider()
    st.subheader("Status")

    total_pages = len(st.session_state.loaded_pages)
    st.metric("Pages Loaded", total_pages)

    if st.session_state.migration_results:
        r = st.session_state.migration_results
        cols = st.columns(2)
        cols[0].metric("Published", r.get("success", 0))
        cols[1].metric("Failed", r.get("failed", 0))
        cols = st.columns(2)
        cols[0].metric("Skipped", r.get("skipped", 0))
        cols[1].metric("Needs Review", r.get("needs_review", 0))


# ---------------------------------------------------------------------------
# Helper: extract HTML from Builder.io blocks
# ---------------------------------------------------------------------------
def extract_html_from_blocks(blocks: list) -> str:
    """Recursively extract HTML content from Builder.io blocks."""
    html_parts = []
    for block in blocks:
        comp = block.get("component", {})
        name = comp.get("name", "")

        if name == "Custom Code":
            code = comp.get("options", {}).get("code", "")
            if code:
                html_parts.append(code)
        elif name == "Text":
            text = comp.get("options", {}).get("text", "")
            if text:
                html_parts.append(text)
        elif name == "Image":
            opts = comp.get("options", {})
            img_url = opts.get("image", "")
            alt = opts.get("altText", "")
            if img_url:
                html_parts.append(f'<img src="{img_url}" alt="{alt}" style="max-width:100%" />')

        children = block.get("children", [])
        if children:
            html_parts.append(extract_html_from_blocks(children))

        if name == "Columns":
            columns = comp.get("options", {}).get("columns", [])
            for col in columns:
                col_blocks = col.get("blocks", [])
                if col_blocks:
                    html_parts.append(extract_html_from_blocks(col_blocks))

    return "\n".join(html_parts)


def builder_entry_to_preview_data(entry: dict) -> dict:
    """Convert a Builder.io entry to our standard post_data format for preview."""
    data = entry.get("data", {})
    blocks = data.get("blocks", [])
    html_content = extract_html_from_blocks(blocks)
    page_url = data.get("url", "") or ""
    page_type = "blog" if page_url.startswith("/blog/") else "static"

    raw_tags = data.get("tags", [])
    tags = []
    for t in raw_tags:
        if isinstance(t, dict):
            tags.append(t.get("tag", ""))
        elif isinstance(t, str):
            tags.append(t)

    return {
        "title": data.get("title", entry.get("name", "")),
        "html_content": html_content,
        "thumbnail": data.get("coverImage", ""),
        "thumbnail_alt": data.get("coverImageAlt", ""),
        "meta_description": data.get("description", ""),
        "tags": tags,
        "categories": [],
        "url_key": data.get("slug", ""),
        "published_at": data.get("publishDate", ""),
        "source": "builder.io",
        "page_type": page_type,
    }


# ---------------------------------------------------------------------------
# Main content
# ---------------------------------------------------------------------------
st.title("Website Transfer Agent")
st.caption("Migrate blog posts and static pages from Magento to Builder.io")

(
    tab_input, tab_preview, tab_pipeline, tab_rerun,
    tab_builder, tab_results, tab_logs,
) = st.tabs([
    "1. Upload Excel", "2. Preview Pages", "3. Run Migration",
    "4. Rerun Specific URLs",
    "5. Builder.io Browser", "6. Results & Export", "7. Logs",
])

# ========================== TAB 1: UPLOAD EXCEL ==========================
with tab_input:
    st.subheader("Upload Page Lists")

    st.markdown("""
    Upload one or both Excel files:
    - **Blog Post List** - Contains blog posts with URL keys, categories, status
    - **Static Page List** - Contains static CMS pages organized by category sheets

    The system auto-detects the file type and only includes published/relevant pages.
    """)

    col_blog, col_static = st.columns(2)

    with col_blog:
        st.markdown("#### Blog Post List")
        blog_file = st.file_uploader(
            "Upload blog post Excel",
            type=["xlsx", "xls"],
            key="blog_excel_upload",
        )
        if blog_file:
            with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as tmp:
                tmp.write(blog_file.read())
                blog_tmp_path = tmp.name

            try:
                blog_posts = read_blog_list(blog_tmp_path)
                if blog_posts:
                    st.success(f"Found {len(blog_posts)} published blog posts")
                    preview = [
                        {
                            "Priority": p.get("priority", ""),
                            "Title": str(p.get("title", ""))[:50],
                            "URL Key": p.get("url_key", ""),
                        }
                        for p in blog_posts[:20]
                    ]
                    st.dataframe(preview, width="stretch")
                    if len(blog_posts) > 20:
                        st.caption(f"... and {len(blog_posts) - 20} more")

                    # Store in session
                    for p in blog_posts:
                        p["page_type"] = "blog"
                    st.session_state.loaded_pages = [
                        p for p in st.session_state.loaded_pages if p.get("page_type") != "blog"
                    ] + blog_posts
                else:
                    st.warning("No published blog posts found in this file.")
            except Exception as e:
                st.error(f"Error reading blog Excel: {e}")
            finally:
                os.unlink(blog_tmp_path)

    with col_static:
        st.markdown("#### Static Page List")
        static_file = st.file_uploader(
            "Upload static page Excel",
            type=["xlsx", "xls"],
            key="static_excel_upload",
        )
        if static_file:
            with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as tmp:
                tmp.write(static_file.read())
                static_tmp_path = tmp.name

            try:
                static_pages = read_static_page_list(static_tmp_path)
                if static_pages:
                    st.success(f"Found {len(static_pages)} static pages")
                    preview = [
                        {
                            "Category": p.get("category", ""),
                            "Title": str(p.get("title", ""))[:50],
                            "URL Key": p.get("url_key", ""),
                        }
                        for p in static_pages[:20]
                    ]
                    st.dataframe(preview, width="stretch")
                    if len(static_pages) > 20:
                        st.caption(f"... and {len(static_pages) - 20} more")

                    for p in static_pages:
                        p["page_type"] = "static"
                    st.session_state.loaded_pages = [
                        p for p in st.session_state.loaded_pages if p.get("page_type") != "static"
                    ] + static_pages
                else:
                    st.warning("No static pages found in this file.")
            except Exception as e:
                st.error(f"Error reading static page Excel: {e}")
            finally:
                os.unlink(static_tmp_path)

    # OR: Single file auto-detect
    st.divider()
    st.markdown("**Or upload a single file (auto-detect type):**")
    auto_file = st.file_uploader(
        "Upload Excel file",
        type=["xlsx", "xls"],
        key="auto_excel_upload",
    )
    if auto_file:
        with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as tmp:
            tmp.write(auto_file.read())
            auto_tmp_path = tmp.name

        try:
            excel_type = detect_excel_type(auto_tmp_path)
            st.info(f"Detected file type: **{excel_type}**")

            if excel_type == "blog":
                blog_posts = read_blog_list(auto_tmp_path)
                if blog_posts:
                    st.success(f"Found {len(blog_posts)} published blog posts")
                    for p in blog_posts:
                        p["page_type"] = "blog"
                    st.session_state.loaded_pages = blog_posts
            elif excel_type == "static":
                static_pages = read_static_page_list(auto_tmp_path)
                if static_pages:
                    st.success(f"Found {len(static_pages)} static pages")
                    for p in static_pages:
                        p["page_type"] = "static"
                    st.session_state.loaded_pages = static_pages
            else:
                st.error("Could not determine the file type. Please use the separate uploaders above.")
        except Exception as e:
            st.error(f"Error: {e}")
        finally:
            os.unlink(auto_tmp_path)

    # Summary
    if st.session_state.loaded_pages:
        st.divider()
        blog_count = sum(1 for p in st.session_state.loaded_pages if p.get("page_type") == "blog")
        static_count = sum(1 for p in st.session_state.loaded_pages if p.get("page_type") == "static")
        st.markdown(f"**Total loaded:** {len(st.session_state.loaded_pages)} pages "
                    f"({blog_count} blog, {static_count} static)")


# ========================== TAB 2: PREVIEW ==========================
with tab_preview:
    st.subheader("Preview Individual Pages")

    if not st.session_state.loaded_pages and not st.session_state.scraped_posts:
        st.info("No pages loaded. Go to **Upload Excel** tab first.")
    else:
        # Allow scraping individual pages for preview
        pages = st.session_state.loaded_pages
        if pages:
            page_labels = [
                f"[{p.get('page_type', '?').upper()}] {p.get('title', p.get('url_key', 'Unknown'))}"
                for p in pages
            ]

            selected_idx = st.selectbox(
                "Select a page to preview",
                range(len(pages)),
                format_func=lambda i: page_labels[i],
            )
            selected_page = pages[selected_idx]

            col_info, col_btn = st.columns([3, 1])
            with col_info:
                st.markdown(f"**URL Key:** `{selected_page.get('url_key', '')}`")
                st.markdown(f"**Type:** {selected_page.get('page_type', '')}")
                if selected_page.get("primary_url"):
                    st.markdown(f"**Source URL:** {selected_page['primary_url']}")

            with col_btn:
                scrape_preview = st.button("Scrape & Preview", type="primary", width="stretch")

            if scrape_preview:
                url_key = selected_page.get("url_key", "")
                page_type = selected_page.get("page_type", "blog")
                primary_url = selected_page.get("primary_url", "")
                if not primary_url:
                    if page_type == "blog":
                        primary_url = f"{source_url.rstrip('/')}{blog_path}{url_key}"
                    else:
                        primary_url = f"{source_url.rstrip('/')}/{url_key}"

                post_data = None
                captured_live = False

                # Preferred path: live browser capture — pulls real stylesheets
                # so carousels, sliders, hover states survive intact.
                if use_live_capture and primary_url:
                    with st.spinner(f"Rendering {primary_url} in headless Chromium..."):
                        try:
                            login = None
                            if magento_username and magento_password:
                                login = {
                                    "admin_url": magento_admin_url,
                                    "username": magento_username,
                                    "password": magento_password,
                                    "otp": magento_otp or "",
                                }
                            capture = capture_live_fragment(primary_url, login=login)
                            if capture.ok:
                                post_data = {
                                    "title": capture.title,
                                    "html_content": capture.html_fragment,
                                    "thumbnail": capture.og_image,
                                    "og_image": capture.og_image,
                                    "meta_title": capture.meta_title,
                                    "meta_description": capture.meta_description,
                                    "url_key": url_key,
                                    "images": capture.images,
                                    "source": "live_capture",
                                    "page_type": page_type,
                                    "_html_already_processed": True,
                                }
                                captured_live = True
                                st.success(
                                    f"Live capture OK — {capture.css_rule_count} CSS rules from "
                                    f"{capture.content_selector_used!r}"
                                )
                            else:
                                st.warning(f"Live capture failed ({capture.error}); falling back to legacy scraper.")
                        except Exception as e:
                            st.warning(f"Live capture exception: {e} — falling back to legacy scraper.")

                # Fallback: legacy GraphQL / HTML scraper
                if not post_data:
                    with st.spinner(f"Scraping {url_key} via GraphQL/HTML..."):
                        try:
                            if page_type == "blog":
                                scraper = BlogScraper(source_url, blog_path)
                                post_data = scraper.fetch_post_by_url_key(url_key)
                            else:
                                scraper = StaticPageScraper(source_url)
                                post_data = scraper.fetch_page_by_url(primary_url, url_key)
                        except Exception as e:
                            st.error(f"Exception: {e}")
                            post_data = None

                if not post_data:
                    pass  # error already reported
                elif post_data.get("error"):
                    st.error(f"Scraping failed: {post_data['error']}")
                elif not post_data.get("html_content"):
                    st.warning("No content found for this page.")
                else:
                    st.session_state.scraped_posts[url_key] = post_data
                    if not captured_live:
                        st.success(f"Scraped (legacy): {post_data.get('title', url_key)}")

        # ------------------------------------------------------------------
        # BATCH scrape: pick several pages at once
        # ------------------------------------------------------------------
        if pages:
            st.divider()
            with st.expander("Batch scrape multiple pages", expanded=False):
                st.caption(
                    "Select any number of pages, click **Scrape Selected**, and each "
                    "one will be captured and added to the preview list below. "
                    "Use this to queue up a set of troubled pages and review them "
                    "together before publishing."
                )

                batch_options = list(range(len(pages)))
                st.session_state.batch_scrape_selection = [
                    idx for idx in st.session_state.get("batch_scrape_selection", [])
                    if idx in batch_options
                ]
                picked = st.multiselect(
                    "Pages to scrape",
                    options=batch_options,
                    default=st.session_state.batch_scrape_selection,
                    format_func=lambda i: page_labels[i],
                    key="batch_scrape_multiselect",
                )
                st.session_state.batch_scrape_selection = picked

                col_bs1, col_bs2 = st.columns([1, 3])
                with col_bs1:
                    run_batch_scrape = st.button(
                        "Scrape Selected",
                        type="primary",
                        disabled=not picked,
                        width="stretch",
                    )
                with col_bs2:
                    st.caption(f"{len(picked)} page(s) selected")

                if run_batch_scrape and picked:
                    login = None
                    if magento_username and magento_password:
                        login = {
                            "admin_url": magento_admin_url,
                            "username": magento_username,
                            "password": magento_password,
                            "otp": magento_otp or "",
                        }

                    batch_progress = st.progress(0.0)
                    batch_status = st.empty()
                    scraped_ok = 0
                    scraped_fail = 0
                    for count, idx in enumerate(picked, start=1):
                        p = pages[idx]
                        url_key_b = p.get("url_key", "")
                        page_type_b = p.get("page_type", "blog")
                        primary_url_b = p.get("primary_url", "")
                        if not primary_url_b:
                            if page_type_b == "blog":
                                primary_url_b = f"{source_url.rstrip('/')}{blog_path}{url_key_b}"
                            else:
                                primary_url_b = f"{source_url.rstrip('/')}/{url_key_b}"

                        batch_status.markdown(
                            f"**[{count}/{len(picked)}]** Scraping `{url_key_b}`..."
                        )

                        post_data_b = None
                        if use_live_capture and primary_url_b:
                            try:
                                cap = capture_live_fragment(primary_url_b, login=login)
                                if cap.ok:
                                    post_data_b = {
                                        "title": cap.title,
                                        "html_content": cap.html_fragment,
                                        "thumbnail": cap.og_image,
                                        "og_image": cap.og_image,
                                        "meta_title": cap.meta_title,
                                        "meta_description": cap.meta_description,
                                        "url_key": url_key_b,
                                        "primary_url": primary_url_b,
                                        "images": cap.images,
                                        "source": "live_capture",
                                        "page_type": page_type_b,
                                        "_html_already_processed": True,
                                    }
                            except Exception as e:
                                st.warning(f"Live capture failed for {url_key_b}: {e}")

                        if not post_data_b:
                            try:
                                if page_type_b == "blog":
                                    scraper = BlogScraper(source_url, blog_path)
                                    post_data_b = scraper.fetch_post_by_url_key(url_key_b)
                                else:
                                    scraper = StaticPageScraper(source_url)
                                    post_data_b = scraper.fetch_page_by_url(primary_url_b, url_key_b)
                                if post_data_b:
                                    post_data_b.setdefault("primary_url", primary_url_b)
                            except Exception as e:
                                st.error(f"{url_key_b} failed: {e}")

                        if post_data_b and post_data_b.get("html_content"):
                            st.session_state.scraped_posts[url_key_b] = post_data_b
                            scraped_ok += 1
                        else:
                            scraped_fail += 1

                        batch_progress.progress(count / len(picked))

                    batch_status.markdown(
                        f"Batch scrape done — **{scraped_ok}** succeeded, "
                        f"**{scraped_fail}** failed."
                    )

        # Show preview of scraped content
        if st.session_state.scraped_posts:
            st.divider()
            st.markdown("### Scraped Content Preview")

            post_keys = list(st.session_state.scraped_posts.keys())
            post_labels = [
                f"{st.session_state.scraped_posts[k].get('title', k)} ({k})"
                for k in post_keys
            ]

            preview_idx = st.selectbox(
                "Select scraped page to preview",
                range(len(post_keys)),
                format_func=lambda i: post_labels[i],
                key="preview_select",
            )
            preview_key = post_keys[preview_idx]
            post_data = st.session_state.scraped_posts[preview_key]

            with st.expander("Page Metadata", expanded=False):
                meta_cols = st.columns(3)
                with meta_cols[0]:
                    st.markdown(f"**URL Key:** `{post_data.get('url_key', '')}`")
                    st.markdown(f"**Source:** `{post_data.get('source', '')}`")
                    st.markdown(f"**Type:** `{post_data.get('page_type', '')}`")
                with meta_cols[1]:
                    st.markdown(f"**Meta Title:** {post_data.get('meta_title', 'N/A')}")
                    st.markdown(f"**Meta Desc:** {post_data.get('meta_description', 'N/A')[:100]}")
                with meta_cols[2]:
                    st.markdown(f"**Images:** {len(post_data.get('images', []))}")
                    st.markdown(f"**Content Length:** {len(post_data.get('html_content', ''))} chars")

            preview_html = generate_blog_preview_html(post_data, base_url=source_url)
            components.html(preview_html, height=800, scrolling=True)

            col_dl_html, col_dl_json = st.columns(2)
            with col_dl_html:
                st.download_button(
                    "Download HTML",
                    data=preview_html,
                    file_name=f"{preview_key}.html",
                    mime="text/html",
                )
            with col_dl_json:
                st.download_button(
                    "Download JSON",
                    data=json.dumps(post_data, indent=2, ensure_ascii=False),
                    file_name=f"{preview_key}.json",
                    mime="application/json",
                )

            # --------------------------------------------------------------
            # Migrate & Publish from the preview
            # --------------------------------------------------------------
            # One-click "looks good — ship it" flow.  If an entry with this
            # slug already exists in Builder.io it is deleted first so the
            # fresh capture replaces it cleanly.
            st.divider()
            st.markdown("### Migrate & Publish")
            st.caption(
                "Upload the previewed page to Builder.io and publish it. "
                "If an entry with the same URL key already exists, it will be "
                "**deleted** first and replaced with this fresh capture."
            )

            if not builder_private_key or builder_private_key == "your_builder_private_api_key_here":
                st.info(
                    "Enter your Builder.io **Private API Key** in the sidebar "
                    "to enable publishing from this preview."
                )
            else:
                if st.button(
                    "Migrate & Publish This Page",
                    type="primary",
                    key=f"publish_preview_{preview_key}",
                ):
                    with st.spinner("Migrating and publishing..."):
                        try:
                            page_type = post_data.get("page_type", "blog")

                            pub_client = BuilderClient(
                                api_key=builder_private_key,
                                model_name=(
                                    builder_blog_model
                                    if page_type == "blog"
                                    else builder_page_model
                                ),
                                blog_model=builder_blog_model,
                                page_model=builder_page_model,
                                public_key=builder_public_key,
                            )
                            model = (
                                pub_client.blog_model
                                if page_type == "blog"
                                else pub_client.page_model
                            )

                            # 1. Delete existing entry if any
                            existing = pub_client.check_entry_exists(
                                post_data.get("url_key", ""), model_override=model
                            )
                            if existing and existing.get("id"):
                                st.write(f"Found existing entry `{existing['id']}` — deleting...")
                                del_res = pub_client.delete_entry(
                                    existing["id"], model_override=model
                                )
                                if not del_res.get("success"):
                                    st.error(
                                        "Could not delete existing entry: "
                                        f"{del_res.get('error', 'unknown error')}"
                                    )
                                    st.stop()

                            # 2. Run the full image pipeline and link rewrite
                            upload_data = dict(post_data)
                            img_handler = ImageHandler(builder_api_key=builder_private_key)
                            try:
                                st.write("Uploading images to Builder.io CDN...")
                                new_html, mappings = img_handler.process_images_in_html(
                                    upload_data["html_content"], base_url=source_url
                                )
                                upload_data["html_content"] = new_html
                                st.write(f"Uploaded {len(mappings)} image(s).")
                            except Exception as e:
                                st.warning(f"Image upload partially failed: {e}")
                            try:
                                upload_data["html_content"] = img_handler.rewrite_internal_links(
                                    upload_data["html_content"], source_base_url=source_url
                                )
                            except Exception as e:
                                st.warning(f"Link rewrite skipped: {e}")
                            if upload_data.get("thumbnail"):
                                try:
                                    thumb = img_handler.process_thumbnail(upload_data["thumbnail"])
                                    if thumb:
                                        upload_data["thumbnail"] = thumb
                                except Exception as e:
                                    st.warning(f"Thumbnail upload skipped: {e}")

                            # 3. Create fresh entry, published
                            api_res = pub_client.create_entry(
                                upload_data, page_type=page_type, publish=True,
                            )
                            if api_res.get("success"):
                                new_id = api_res.get("data", {}).get("id", "")
                                st.success(
                                    f"Published to Builder.io (id: `{new_id}`, model: `{model}`)."
                                )
                                preview_url = pub_client.get_preview_url(
                                    new_id, model_override=model
                                )
                                if preview_url:
                                    st.markdown(f"[Open in Builder.io]({preview_url})")
                            else:
                                st.error(
                                    "Publish failed: "
                                    f"{api_res.get('error', 'unknown error')}"
                                )
                                details = api_res.get("details")
                                if details:
                                    st.code(details, language="json")
                        except Exception as e:
                            st.error(f"Exception during publish: {e}")

            # --------------------------------------------------------------
            # Refine layout with AI (screenshot-driven)
            # --------------------------------------------------------------
            # When the preview still doesn't match the original live page
            # (sliders collapse, columns stack, etc.), send a full-page
            # screenshot of the original and the current preview to the
            # configured vision model and let it rewrite the HTML. The
            # result is stored back in st.session_state.scraped_posts so
            # the preview above updates on the next rerun.
            st.divider()
            st.markdown("### Refine Layout with AI")
            st.caption(
                "If the preview above still doesn't match the original live "
                "page, click below to screenshot both and let the vision "
                "model rewrite the HTML to match. The refined HTML replaces "
                "the current preview so you can re-check before publishing."
            )

            if not llm_fix_enabled():
                st.info(
                    "Set `OPENAI_API_KEY` (and optionally `OPENAI_MODEL`) in "
                    "your environment to enable AI-driven layout refinement."
                )
            else:
                refine_col_a, refine_col_b = st.columns([1, 3])
                with refine_col_a:
                    refine_clicked = st.button(
                        "Refine with AI",
                        type="primary",
                        key=f"refine_ai_{preview_key}",
                        width="stretch",
                    )
                with refine_col_b:
                    st.caption(
                        "Uses full-page screenshots of the source and the "
                        f"current preview. Model: `{os.getenv('OPENAI_MODEL', 'gpt-5')}`."
                    )

                if refine_clicked:
                    primary_url_r = (
                        post_data.get("primary_url")
                        or (f"{source_url.rstrip('/')}{blog_path}{preview_key}"
                            if post_data.get("page_type") == "blog"
                            else f"{source_url.rstrip('/')}/{preview_key}")
                    )
                    with st.spinner("Screenshotting and asking the model..."):
                        refine_result = refine_layout_with_screenshots(
                            original_url=primary_url_r,
                            current_html=post_data.get("html_content", ""),
                            url_key=preview_key,
                        )

                    if refine_result.get("changed"):
                        post_data["html_content"] = refine_result["html"]
                        # Mark as final so the upload path doesn't re-run the
                        # CSS processor on top of the model's output.
                        post_data["_html_already_processed"] = True
                        st.session_state.scraped_posts[preview_key] = post_data
                        st.success(
                            f"AI refinement applied (model: `{refine_result.get('model', '?')}`). "
                            "The preview above will refresh on the next rerun."
                        )
                        st.rerun()
                    else:
                        err = refine_result.get("error") or "No changes returned."
                        st.warning(f"AI refinement did not apply: {err}")

        # ------------------------------------------------------------------
        # BATCH migrate & publish: pick from the already-scraped previews
        # ------------------------------------------------------------------
        if st.session_state.scraped_posts:
            st.divider()
            with st.expander("Batch migrate & publish scraped pages", expanded=False):
                st.caption(
                    "Pick the pages you've already previewed above and publish "
                    "them as a single batch. For each page, any existing "
                    "Builder.io entry with the same URL key is deleted and "
                    "replaced with the fresh capture."
                )

                if not builder_private_key or builder_private_key == "your_builder_private_api_key_here":
                    st.info(
                        "Enter your Builder.io **Private API Key** in the "
                        "sidebar to enable batch publishing."
                    )
                else:
                    scraped_keys = list(st.session_state.scraped_posts.keys())
                    st.session_state.batch_publish_selection = [
                        k for k in st.session_state.batch_publish_selection
                        if k in scraped_keys
                    ]

                    sel = st.multiselect(
                        "Pages to publish",
                        options=scraped_keys,
                        default=st.session_state.batch_publish_selection or scraped_keys,
                        format_func=lambda k: (
                            f"{st.session_state.scraped_posts[k].get('title', k)}"
                            f" ({k})"
                        ),
                        key="batch_publish_multiselect",
                    )
                    st.session_state.batch_publish_selection = sel

                    col_bp1, col_bp2 = st.columns([1, 3])
                    with col_bp1:
                        run_batch_publish = st.button(
                            "Publish Selected",
                            type="primary",
                            disabled=not sel,
                            key="run_batch_publish_btn",
                            width="stretch",
                        )
                    with col_bp2:
                        st.caption(f"{len(sel)} page(s) selected")

                    if run_batch_publish and sel:
                        pub_progress = st.progress(0.0)
                        pub_status = st.empty()
                        pub_ok = 0
                        pub_fail = 0

                        img_handler = ImageHandler(builder_api_key=builder_private_key)
                        pub_client = BuilderClient(
                            api_key=builder_private_key,
                            model_name=builder_blog_model,
                            blog_model=builder_blog_model,
                            page_model=builder_page_model,
                            public_key=builder_public_key,
                        )

                        for n, key in enumerate(sel, start=1):
                            page_data_b = dict(st.session_state.scraped_posts[key])
                            page_type_b = page_data_b.get("page_type", "blog")
                            model = (
                                pub_client.blog_model
                                if page_type_b == "blog"
                                else pub_client.page_model
                            )
                            pub_status.markdown(
                                f"**[{n}/{len(sel)}]** Publishing `{key}` "
                                f"({page_type_b})..."
                            )

                            try:
                                existing = pub_client.check_entry_exists(
                                    page_data_b.get("url_key", ""),
                                    model_override=model,
                                )
                                if existing and existing.get("id"):
                                    pub_client.delete_entry(
                                        existing["id"], model_override=model
                                    )

                                try:
                                    new_html, _ = img_handler.process_images_in_html(
                                        page_data_b["html_content"],
                                        base_url=source_url,
                                    )
                                    page_data_b["html_content"] = new_html
                                except Exception as e:
                                    st.warning(
                                        f"Image upload partial failure for {key}: {e}"
                                    )
                                try:
                                    page_data_b["html_content"] = (
                                        img_handler.rewrite_internal_links(
                                            page_data_b["html_content"],
                                            source_base_url=source_url,
                                        )
                                    )
                                except Exception:
                                    pass
                                if page_data_b.get("thumbnail"):
                                    try:
                                        thumb = img_handler.process_thumbnail(
                                            page_data_b["thumbnail"]
                                        )
                                        if thumb:
                                            page_data_b["thumbnail"] = thumb
                                    except Exception:
                                        pass

                                api_res = pub_client.create_entry(
                                    page_data_b, page_type=page_type_b, publish=True,
                                )
                                if api_res.get("success"):
                                    pub_ok += 1
                                else:
                                    pub_fail += 1
                                    st.error(
                                        f"{key}: {api_res.get('error', 'unknown')}"
                                    )
                            except Exception as e:
                                pub_fail += 1
                                st.error(f"{key}: {e}")

                            pub_progress.progress(n / len(sel))

                        pub_status.markdown(
                            f"Batch publish done — **{pub_ok}** succeeded, "
                            f"**{pub_fail}** failed."
                        )


# ========================== TAB 3: RUN MIGRATION ==========================
with tab_pipeline:
    st.subheader("Run Migration Pipeline")

    if not st.session_state.loaded_pages:
        st.info("No pages loaded. Go to **Upload Excel** tab first to load your page lists.")
    elif not builder_private_key or builder_private_key == "your_builder_private_api_key_here":
        st.warning(
            "**Private API key not configured.** "
            "Enter your Builder.io Private API Key in the sidebar to enable the migration pipeline."
        )
    else:
        # Pipeline configuration
        st.markdown("### Pipeline Settings")

        col1, col2, col3 = st.columns(3)
        with col1:
            publish_mode = st.checkbox("Publish immediately (otherwise save as draft)")
            skip_existing = st.checkbox("Skip existing entries", value=True)
        with col2:
            dry_run = st.checkbox("Dry run (scrape only, don't upload)")
            limit = st.number_input("Limit pages (0 = all)", min_value=0, value=0, step=5)
        with col3:
            blog_count = sum(1 for p in st.session_state.loaded_pages if p.get("page_type") == "blog")
            static_count = sum(1 for p in st.session_state.loaded_pages if p.get("page_type") == "static")
            st.metric("Blog Posts", blog_count)
            st.metric("Static Pages", static_count)

        st.markdown(f"""
        **What will happen:**
        1. Each page will be scraped from the source website (GraphQL API first, then HTML fallback)
        2. Images will be downloaded and re-uploaded to Builder.io
        3. Content will be uploaded to Builder.io:
           - Blog posts saved under **Blog Post** model (`/blog/<url_key>`)
           - Static pages saved under **Page** model (`/<url_key>`)
        4. Pages with low confidence will be saved as **draft** for human review
        5. Results will be exported to an Excel file
        """)

        st.divider()

        # THE BUTTON
        run_pipeline = st.button(
            "Start Migration Pipeline",
            type="primary",
            width="stretch",
            disabled=st.session_state.pipeline_running,
        )

        if run_pipeline:
            st.session_state.pipeline_running = True
            st.session_state.pipeline_log = []

            pages = st.session_state.loaded_pages
            if limit > 0:
                pages = pages[:limit]

            progress_bar = st.progress(0)
            status_text = st.empty()
            log_container = st.container()

            # Initialize agent
            # Build Magento login dict only if credentials were entered.
            _login = None
            if magento_username and magento_password:
                _login = {
                    "admin_url": magento_admin_url,
                    "username": magento_username,
                    "password": magento_password,
                    "otp": magento_otp or "",
                }

            agent = MigrationAgent(
                source_base_url=source_url,
                builder_api_key=builder_private_key,
                builder_model=builder_blog_model,
                blog_model=builder_blog_model,
                page_model=builder_page_model,
                blog_path=blog_path,
                builder_public_key=builder_public_key,
                use_live_capture=use_live_capture,
                magento_login=_login,
            )

            # Build the migration list
            pages_to_migrate = []
            for p in pages:
                page_type = p.get("page_type", "blog")
                url_key = p.get("url_key", "")

                if page_type == "blog":
                    primary_url = f"{source_url}{blog_path}{url_key}"
                else:
                    primary_url = p.get("primary_url", "")

                pages_to_migrate.append({
                    "url_key": url_key,
                    "title": p.get("title", ""),
                    "page_type": page_type,
                    "primary_url": primary_url,
                    "original_data": p,
                })

            # Run migration
            total = len(pages_to_migrate)
            agent.results["started_at"] = datetime.now().isoformat()
            agent.results["total"] = total

            for i, page_info in enumerate(pages_to_migrate, 1):
                url_key = page_info["url_key"]
                page_type = page_info["page_type"]
                title = page_info.get("title", url_key)

                progress_bar.progress(i / total)
                status_text.markdown(
                    f"**[{i}/{total}]** Processing `{url_key}` ({page_type}) - {title[:40]}"
                )

                result = agent._migrate_single_page(
                    url_key=url_key,
                    page_type=page_type,
                    primary_url=page_info["primary_url"],
                    publish=publish_mode,
                    skip_existing=skip_existing,
                    dry_run=dry_run,
                )

                result["original_data"] = page_info.get("original_data", {})
                result["page_type"] = page_type
                agent.results["details"].append(result)

                if result["status"] == "published_by_agent":
                    agent.results["success"] += 1
                    with log_container:
                        st.success(f"[{i}/{total}] {title[:50]} - Published")
                elif result["status"] == "skipped":
                    agent.results["skipped"] += 1
                    with log_container:
                        st.info(f"[{i}/{total}] {title[:50]} - Skipped (exists)")
                elif result["status"] == "needs_human_review":
                    agent.results["needs_review"] += 1
                    with log_container:
                        st.warning(f"[{i}/{total}] {title[:50]} - Needs review")
                else:
                    agent.results["failed"] += 1
                    with log_container:
                        st.error(f"[{i}/{total}] {title[:50]} - Failed: {result.get('error', '')[:50]}")

            agent.results["completed_at"] = datetime.now().isoformat()
            progress_bar.progress(1.0)
            status_text.markdown("**Migration complete!**")

            # Store results
            st.session_state.migration_results = agent.results
            st.session_state.pipeline_log = agent.get_log_entries()
            st.session_state.pipeline_running = False

            # Export results to Excel
            excel_path = ""
            try:
                excel_path = export_blog_results(agent.results)
                if excel_path:
                    st.session_state["results_excel_path"] = excel_path
                    agent.mlog.info("__pipeline__", "", "export", f"Results exported to {excel_path}")
            except Exception as e:
                st.error(f"Failed to export Excel: {e}")

            # Export log
            log_path = ""
            try:
                log_path = agent.export_log()
                st.session_state["log_file_path"] = log_path
            except Exception:
                pass

            # Persist to disk so data survives page refresh
            run_id = agent.mlog.run_id
            try:
                save_run(
                    run_id=run_id,
                    migration_results=agent.results,
                    pipeline_log=agent.get_log_entries(),
                    results_excel_path=excel_path or "",
                    log_file_path=log_path or "",
                )
                st.session_state.current_run_id = run_id
            except Exception as e:
                st.error(f"Failed to persist run state: {e}")

            st.rerun()

        # Show previous results summary if available
        if st.session_state.migration_results and not st.session_state.pipeline_running:
            st.divider()
            r = st.session_state.migration_results
            st.markdown("### Last Migration Run")
            cols = st.columns(5)
            cols[0].metric("Total", r.get("total", 0))
            cols[1].metric("Published", r.get("success", 0))
            cols[2].metric("Failed", r.get("failed", 0))
            cols[3].metric("Skipped", r.get("skipped", 0))
            cols[4].metric("Needs Review", r.get("needs_review", 0))


# ========================== TAB 4: RERUN SPECIFIC URLS =========================
with tab_rerun:
    st.subheader("Rerun Migration for Specific URLs")
    st.caption(
        "Paste one source URL (or URL key) per line and rerun the full "
        "migration pipeline on just that set. Useful for retrying pages that "
        "came out broken — sliders, YouTube embeds, collapsed layouts — "
        "without re-migrating everything. Existing Builder.io entries for "
        "these URL keys are replaced with the fresh capture."
    )

    url_input_default = "\n".join([
        "https://www.pricerite.com.hk/hk/zh/corporate-order",
        "https://www.pricerite.com.hk/hk/zh/warranty",
        "https://www.pricerite.com.hk/hk/zh/service-charge",
        "https://www.pricerite.com.hk/hk/zh/corp-news",
        "https://www.pricerite.com.hk/hk/zh/corporate_culture",
        "https://www.pricerite.com.hk/hk/zh/awards",
        "https://www.pricerite.com.hk/mattress-eform",
        "https://www.pricerite.com.hk/hk/zh/customer-service-center",
        "https://www.pricerite.com.hk/hk/zh/mattress",
        "https://www.pricerite.com.hk/hk/zh/bed",
        "https://www.pricerite.com.hk/hk/zh/violino_collection",
        "https://www.pricerite.com.hk/hk/zh/staple_series",
        "https://www.pricerite.com.hk/hk/zh/mesh2.0",
        "https://www.pricerite.com.hk/hk/zh/mesh",
        "https://www.pricerite.com.hk/hk/zh/cmf",
        "https://www.pricerite.com.hk/hk/zh/econ_choices",
        "https://www.pricerite.com.hk/hk/zh/eshop_exclusive",
        "https://www.pricerite.com.hk/mo/delivery-arrangement",
        "https://www.pricerite.com.hk/hk/zh/home-food",
        "https://www.pricerite.com.hk/hk/zh/housingoffer2026",
        "https://www.pricerite.com.hk/hk/zh/futip_estate",
        "https://www.pricerite.com.hk/hk/zh/partitionfur",
        "https://www.pricerite.com.hk/hk/zh/tools",
        "https://www.pricerite.com.hk/hk/zh/intro-refrigerator-tips",
        "https://www.pricerite.com.hk/hk/zh/intro-television-tips",
        "https://www.pricerite.com.hk/hk/zh/intro-washingmachine-tips",
        "https://www.pricerite.com.hk/hk/zh/intro-fur-tips",
        "https://www.pricerite.com.hk/hk/zh/intro-water-heater-tips",
        "https://www.pricerite.com.hk/hk/zh/intro-aircon-tips",
        "https://www.pricerite.com.hk/hk/zh/intro-guide-double-bed-size",
        "https://www.pricerite.com.hk/hk/zh/intro-guide-single-bed-size",
        "https://www.pricerite.com.hk/hk/zh/intro-mattress-tips",
    ])

    rerun_urls_raw = st.text_area(
        "URLs or URL keys (one per line)",
        value=st.session_state.get("rerun_urls_raw", url_input_default),
        height=260,
        key="rerun_urls_raw",
    )

    col_rr1, col_rr2, col_rr3 = st.columns(3)
    with col_rr1:
        rerun_page_type = st.radio(
            "Page model",
            ["static", "blog"],
            horizontal=True,
            help="Most trouble pages are static CMS pages (`/foo`), not blog posts (`/blog/foo`).",
        )
    with col_rr2:
        rerun_publish = st.checkbox("Publish immediately", value=True)
    with col_rr3:
        rerun_skip_existing = st.checkbox(
            "Skip if already present",
            value=False,
            help="Leave unchecked to REPLACE existing entries with the fresh capture.",
        )

    def _parse_rerun_lines(raw: str) -> list[dict]:
        """Turn each non-empty line into {url_key, primary_url}.

        Accepts full URLs (https://.../foo) or bare url keys (foo / hk/zh/foo).
        """
        out = []
        base = source_url.rstrip("/")
        for line in raw.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("http://") or line.startswith("https://"):
                primary = line
                # Derive url_key from the last path segment for extension-free
                # Builder.io paths, matching _normalize_url_key semantics.
                from urllib.parse import urlparse
                parsed = urlparse(line)
                path = (parsed.path or "/").rstrip("/")
                if not path or path == "/":
                    continue
                url_key = path.rsplit("/", 1)[-1]
                if url_key.lower().endswith(".html"):
                    url_key = url_key[: -len(".html")]
            else:
                url_key = line.strip("/")
                if url_key.lower().endswith(".html"):
                    url_key = url_key[: -len(".html")]
                # Most rerun URLs are static pages at /hk/zh/<key>.
                primary = f"{base}/hk/zh/{url_key}"
            out.append({"url_key": url_key, "primary_url": primary})
        return out

    parsed_targets = _parse_rerun_lines(rerun_urls_raw)
    st.markdown(f"**{len(parsed_targets)}** target page(s) will be processed.")

    if (not builder_private_key) or builder_private_key == "your_builder_private_api_key_here":
        st.warning(
            "Enter your Builder.io **Private API Key** in the sidebar to "
            "enable the rerun pipeline."
        )
        rerun_disabled = True
    else:
        rerun_disabled = False

    run_clicked = st.button(
        "Rerun migration on these URLs",
        type="primary",
        disabled=rerun_disabled or not parsed_targets,
        key="rerun_targets_btn",
    )

    if run_clicked and parsed_targets:
        _login = None
        if magento_username and magento_password:
            _login = {
                "admin_url": magento_admin_url,
                "username": magento_username,
                "password": magento_password,
                "otp": magento_otp or "",
            }

        rr_agent = MigrationAgent(
            source_base_url=source_url,
            builder_api_key=builder_private_key,
            builder_model=(
                builder_blog_model if rerun_page_type == "blog" else builder_page_model
            ),
            blog_model=builder_blog_model,
            page_model=builder_page_model,
            blog_path=blog_path,
            builder_public_key=builder_public_key,
            use_live_capture=use_live_capture,
            magento_login=_login,
        )

        rr_progress = st.progress(0.0)
        rr_status = st.empty()
        rr_log = st.container()
        rr_agent.results["started_at"] = datetime.now().isoformat()
        rr_agent.results["total"] = len(parsed_targets)

        for idx, target in enumerate(parsed_targets, 1):
            url_key = target["url_key"]
            primary = target["primary_url"]
            rr_status.markdown(f"**[{idx}/{len(parsed_targets)}]** `{url_key}` — {primary}")

            result = rr_agent._migrate_single_page(
                url_key=url_key,
                page_type=rerun_page_type,
                primary_url=primary,
                publish=rerun_publish,
                skip_existing=rerun_skip_existing,
                dry_run=False,
            )
            result["original_data"] = {"url_key": url_key, "primary_url": primary}
            result["page_type"] = rerun_page_type
            rr_agent.results["details"].append(result)

            if result["status"] == "published_by_agent":
                rr_agent.results["success"] += 1
                with rr_log:
                    st.success(f"[{idx}/{len(parsed_targets)}] {url_key} — Published")
            elif result["status"] == "skipped":
                rr_agent.results["skipped"] += 1
                with rr_log:
                    st.info(f"[{idx}/{len(parsed_targets)}] {url_key} — Skipped (exists)")
            elif result["status"] == "needs_human_review":
                rr_agent.results["needs_review"] += 1
                with rr_log:
                    st.warning(f"[{idx}/{len(parsed_targets)}] {url_key} — Needs review")
            else:
                rr_agent.results["failed"] += 1
                with rr_log:
                    st.error(
                        f"[{idx}/{len(parsed_targets)}] {url_key} — "
                        f"Failed: {str(result.get('error', ''))[:120]}"
                    )

            rr_progress.progress(idx / len(parsed_targets))

        rr_agent.results["completed_at"] = datetime.now().isoformat()
        rr_status.markdown("**Rerun complete!**")

        # Make the rerun feel like any other migration — persist results and
        # show them under "Results & Export" / "Logs" too.
        st.session_state.migration_results = rr_agent.results
        st.session_state.pipeline_log = rr_agent.get_log_entries()

        try:
            excel_path = export_blog_results(rr_agent.results)
            if excel_path:
                st.session_state["results_excel_path"] = excel_path
        except Exception as e:
            st.warning(f"Failed to export Excel: {e}")
        try:
            log_path = rr_agent.export_log()
            st.session_state["log_file_path"] = log_path
        except Exception:
            pass
        try:
            save_run(
                run_id=rr_agent.mlog.run_id,
                migration_results=rr_agent.results,
                pipeline_log=rr_agent.get_log_entries(),
                results_excel_path=st.session_state.get("results_excel_path", ""),
                log_file_path=st.session_state.get("log_file_path", ""),
            )
            st.session_state.current_run_id = rr_agent.mlog.run_id
        except Exception as e:
            st.warning(f"Could not persist run state: {e}")

        cols = st.columns(4)
        cols[0].metric("Published", rr_agent.results["success"])
        cols[1].metric("Failed", rr_agent.results["failed"])
        cols[2].metric("Skipped", rr_agent.results["skipped"])
        cols[3].metric("Needs Review", rr_agent.results["needs_review"])


# ========================== TAB 5: BUILDER.IO BROWSER ==========================
with tab_builder:
    st.subheader("Builder.io Content Browser")

    if not builder_public_key:
        st.warning("Enter a Builder.io **Public API Key** in the sidebar to browse content.")
    else:
        model_to_browse = st.radio(
            "Select model to browse",
            ["blog-post", "page"],
            horizontal=True,
        )

        col_fetch, col_info = st.columns([1, 3])
        with col_fetch:
            fetch_clicked = st.button(
                "Fetch Content", type="primary", width="stretch"
            )

        if fetch_clicked:
            with st.spinner("Fetching entries from Builder.io..."):
                client = BuilderClient(builder_public_key, model_to_browse)
                entries = client.fetch_all_entries(limit=50, include_unpublished=True, model_override=model_to_browse)
                st.session_state.builder_entries = entries
                if entries:
                    st.success(f"Fetched {len(entries)} entries from Builder.io ({model_to_browse})")
                else:
                    st.warning("No entries found.")

        if st.session_state.builder_entries:
            entries = st.session_state.builder_entries

            table_data = []
            for entry in entries:
                data = entry.get("data", {})
                table_data.append({
                    "Name": entry.get("name", ""),
                    "Title": data.get("title", ""),
                    "Slug": data.get("slug", ""),
                    "Status": entry.get("published", ""),
                    "Has Blocks": "Yes" if data.get("blocks") else "No",
                })
            st.dataframe(table_data, width="stretch")

            st.divider()
            entry_labels = [
                f"{e.get('name', 'Untitled')} ({e.get('data', {}).get('slug', 'no-slug')})"
                for e in entries
            ]

            selected_entry_idx = st.selectbox(
                "Select an entry to preview",
                range(len(entries)),
                format_func=lambda i: entry_labels[i],
                key="builder_entry_select",
            )

            selected_entry = entries[selected_entry_idx]
            entry_data = selected_entry.get("data", {})

            with st.expander("Entry Metadata", expanded=False):
                meta_cols = st.columns(3)
                with meta_cols[0]:
                    st.markdown(f"**ID:** `{selected_entry.get('id', '')}`")
                    st.markdown(f"**Slug:** `{entry_data.get('slug', '')}`")
                    st.markdown(f"**Status:** `{selected_entry.get('published', '')}`")
                with meta_cols[1]:
                    st.markdown(f"**Meta Title:** {entry_data.get('metaTitle', 'N/A')}")
                    st.markdown(f"**Meta Desc:** {entry_data.get('metaDescription', 'N/A')}")
                with meta_cols[2]:
                    st.markdown(f"**URL:** {entry_data.get('url', 'N/A')}")
                    cover = entry_data.get("coverImage", "")
                    st.markdown(f"**Cover Image:** {'Yes' if cover else 'No'}")

            blocks = entry_data.get("blocks", [])
            if blocks:
                st.markdown("### Page Preview")
                preview_post = builder_entry_to_preview_data(selected_entry)
                preview_html = generate_blog_preview_html(preview_post)
                components.html(preview_html, height=800, scrolling=True)

            with st.expander("Raw Entry JSON"):
                st.json(selected_entry)

        # -------------------------------------------------------------------
        # Publish All Drafts section
        # -------------------------------------------------------------------
        st.divider()
        st.markdown("### Publish Drafts")

        if not builder_private_key or builder_private_key == "your_builder_private_api_key_here":
            st.warning("Enter your Builder.io **Private API Key** in the sidebar to enable publishing.")
        else:
            pub_model = st.radio(
                "Publish drafts in model",
                ["blog-post", "page", "both"],
                horizontal=True,
                key="publish_model_select",
            )

            # Fetch drafts count first
            if st.button("Check Draft Count", key="check_drafts_btn"):
                client = BuilderClient(builder_private_key)
                models_to_check = ["blog-post", "page"] if pub_model == "both" else [pub_model]
                total_drafts = 0
                for m in models_to_check:
                    drafts = client.fetch_draft_entries(limit=200, model_override=m)
                    count = len(drafts)
                    total_drafts += count
                    st.info(f"**{m}**: {count} draft entries")
                st.session_state["draft_count"] = total_drafts

            draft_count = st.session_state.get("draft_count", None)

            if draft_count is not None and draft_count > 0:
                st.warning(
                    f"This will publish **{draft_count}** draft entries. "
                    "Make sure you have reviewed them in the Builder.io editor first."
                )

                confirm = st.checkbox(
                    "I have reviewed the drafts and want to publish them all",
                    key="confirm_publish_drafts",
                )

                if confirm:
                    if st.button(
                        "Publish All Drafts",
                        type="primary",
                        width="stretch",
                        key="publish_all_drafts_btn",
                    ):
                        client = BuilderClient(builder_private_key)
                        models_to_publish = ["blog-post", "page"] if pub_model == "both" else [pub_model]

                        all_results = {"total": 0, "published": 0, "failed": 0, "details": []}

                        for m in models_to_publish:
                            st.markdown(f"**Publishing drafts in `{m}`...**")
                            progress = st.progress(0)
                            status = st.empty()

                            def on_progress(current, total, name, _m=m, _progress=progress, _status=status):
                                _progress.progress(current / total if total > 0 else 1.0)
                                _status.markdown(f"[{current}/{total}] Publishing: {name[:50]}")

                            result = client.publish_all_drafts(
                                model_override=m,
                                progress_callback=on_progress,
                            )

                            progress.progress(1.0)
                            all_results["total"] += result["total"]
                            all_results["published"] += result["published"]
                            all_results["failed"] += result["failed"]
                            all_results["details"].extend(result["details"])

                            if result["published"] > 0:
                                st.success(f"Published {result['published']}/{result['total']} entries in `{m}`")
                            if result["failed"] > 0:
                                st.error(f"Failed to publish {result['failed']} entries in `{m}`")

                        st.markdown(
                            f"**Done!** Published {all_results['published']}/{all_results['total']} total drafts."
                        )
                        st.session_state["draft_count"] = None  # Reset count

            elif draft_count == 0:
                st.success("No draft entries found - everything is already published!")


# ========================== TAB 5: RESULTS & EXPORT ==========================
with tab_results:
    st.subheader("Migration Results & Export")

    # -------------------------------------------------------------------
    # Past Runs browser
    # -------------------------------------------------------------------
    past_runs = list_runs()
    if past_runs:
        with st.expander(f"Past Runs ({len(past_runs)} saved)", expanded=False):
            run_table = []
            for run in past_runs:
                run_table.append({
                    "Run ID": run.get("run_id", ""),
                    "Started": (run.get("started_at", "") or "")[:19],
                    "Total": run.get("total", 0),
                    "Published": run.get("success", 0),
                    "Failed": run.get("failed", 0),
                    "Review": run.get("needs_review", 0),
                })
            st.dataframe(run_table, width="stretch")

            run_ids = [r["run_id"] for r in past_runs]
            current_idx = 0
            if st.session_state.current_run_id in run_ids:
                current_idx = run_ids.index(st.session_state.current_run_id)

            selected_run_id = st.selectbox(
                "Select a run to load",
                run_ids,
                index=current_idx,
                format_func=lambda rid: f"{rid} ({next((r.get('total',0) for r in past_runs if r['run_id']==rid), '?')} pages)",
                key="past_run_select",
            )

            if st.button("Load Selected Run", width="stretch"):
                loaded = load_run(selected_run_id)
                if loaded and loaded.get("migration_results"):
                    st.session_state.migration_results = loaded["migration_results"]
                    st.session_state.pipeline_log = loaded.get("pipeline_log", [])
                    st.session_state.current_run_id = selected_run_id
                    st.session_state["results_excel_path"] = loaded.get("results_excel_path", "")
                    st.session_state["log_file_path"] = loaded.get("log_file_path", "")
                    st.success(f"Loaded run: {selected_run_id}")
                    st.rerun()
                else:
                    st.error("Failed to load run data.")

    # -------------------------------------------------------------------
    # Current results display
    # -------------------------------------------------------------------
    if not st.session_state.migration_results:
        st.info("No migration results yet. Run the pipeline first, or load a past run above.")
    else:
        r = st.session_state.migration_results

        if st.session_state.current_run_id:
            st.caption(f"Run ID: `{st.session_state.current_run_id}`")

        # Summary metrics
        cols = st.columns(5)
        cols[0].metric("Total", r.get("total", 0))
        cols[1].metric("Published", r.get("success", 0))
        cols[2].metric("Failed", r.get("failed", 0))
        cols[3].metric("Skipped", r.get("skipped", 0))
        cols[4].metric("Needs Review", r.get("needs_review", 0))

        st.markdown(f"**Started:** {r.get('started_at', 'N/A')}")
        st.markdown(f"**Completed:** {r.get('completed_at', 'N/A')}")

        # Results table
        details = r.get("details", [])
        if details:
            st.divider()
            table_data = []
            for d in details:
                table_data.append({
                    "URL Key": d.get("url_key", ""),
                    "Title": (d.get("title", "") or "")[:50],
                    "Type": d.get("page_type", ""),
                    "Agent Status": d.get("status", ""),
                    "Confidence": d.get("confidence", ""),
                    "Source": d.get("source", ""),
                    "Images": d.get("images_processed", 0),
                    "Error": (d.get("error", "") or "")[:40],
                })
            st.dataframe(table_data, width="stretch")

        # Export buttons
        st.divider()
        st.markdown("### Export")

        col_excel, col_json, col_log = st.columns(3)

        with col_excel:
            excel_path = st.session_state.get("results_excel_path", "")
            if excel_path and Path(excel_path).exists():
                with open(excel_path, "rb") as f:
                    st.download_button(
                        "Download Results Excel",
                        data=f.read(),
                        file_name=f"migration_results_{datetime.now().strftime('%Y%m%d')}.xlsx",
                        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        width="stretch",
                    )
            else:
                if st.button("Generate Excel Report", width="stretch"):
                    try:
                        path = export_blog_results(r)
                        st.session_state["results_excel_path"] = path
                        # Also update saved run if we have one
                        if st.session_state.current_run_id:
                            save_run(
                                run_id=st.session_state.current_run_id,
                                migration_results=r,
                                pipeline_log=st.session_state.pipeline_log,
                                results_excel_path=path,
                                log_file_path=st.session_state.get("log_file_path", ""),
                            )
                        st.rerun()
                    except Exception as e:
                        st.error(f"Failed to generate Excel: {e}")

        with col_json:
            st.download_button(
                "Download Results JSON",
                data=json.dumps(r, indent=2, ensure_ascii=False),
                file_name="migration_results.json",
                mime="application/json",
                width="stretch",
            )

        with col_log:
            log_path = st.session_state.get("log_file_path", "")
            if log_path and Path(log_path).exists():
                with open(log_path, "r") as f:
                    st.download_button(
                        "Download Full Log",
                        data=f.read(),
                        file_name="migration_log.json",
                        mime="application/json",
                        width="stretch",
                    )


# ========================== TAB 6: LOGS ==========================
with tab_logs:
    st.subheader("Migration Logs")

    logs = st.session_state.pipeline_log

    # If no logs in session but we have a log file on disk, offer to load it
    if not logs:
        log_file = st.session_state.get("log_file_path", "")
        if log_file and Path(log_file).exists():
            st.info("Logs available on disk from a previous run.")
            if st.button("Load logs from disk"):
                try:
                    with open(log_file, "r", encoding="utf-8") as f:
                        st.session_state.pipeline_log = json.load(f)
                    st.rerun()
                except Exception as e:
                    st.error(f"Failed to load logs: {e}")

        # Also check for any JSONL log files in the logs/ directory
        logs_dir = Path("logs")
        if logs_dir.exists():
            jsonl_files = sorted(logs_dir.glob("migration_*.jsonl"), reverse=True)
            if jsonl_files:
                st.markdown("**Or load from a log file:**")
                selected_log = st.selectbox(
                    "Available log files",
                    jsonl_files,
                    format_func=lambda p: p.name,
                    key="log_file_select",
                )
                if st.button("Load selected log file"):
                    try:
                        entries = []
                        with open(selected_log, "r", encoding="utf-8") as f:
                            for line in f:
                                line = line.strip()
                                if line:
                                    entries.append(json.loads(line))
                        st.session_state.pipeline_log = entries
                        st.rerun()
                    except Exception as e:
                        st.error(f"Failed to parse log file: {e}")

        if not logs:
            st.info("No logs loaded. Run the migration pipeline or load a past run from the Results tab.")

    if logs:
        if st.session_state.current_run_id:
            st.caption(f"Viewing logs for run: `{st.session_state.current_run_id}`")

        # Filter controls
        col_filter_key, col_filter_step, col_filter_level = st.columns(3)

        with col_filter_key:
            all_keys = list(dict.fromkeys(e.get("page_key", "") for e in logs))
            filter_key = st.selectbox("Filter by page key", ["All"] + all_keys, key="log_filter_key")

        with col_filter_step:
            all_steps = list(dict.fromkeys(e.get("step", "") for e in logs))
            filter_step = st.selectbox("Filter by step", ["All"] + all_steps, key="log_filter_step")

        with col_filter_level:
            filter_level = st.selectbox("Filter by level", ["All", "ERROR", "WARNING", "INFO", "DEBUG"],
                                        key="log_filter_level")

        # Apply filters
        filtered = logs
        if filter_key != "All":
            filtered = [e for e in filtered if e.get("page_key") == filter_key]
        if filter_step != "All":
            filtered = [e for e in filtered if e.get("step") == filter_step]
        if filter_level != "All":
            filtered = [e for e in filtered if e.get("level") == filter_level]

        st.caption(f"Showing {len(filtered)} of {len(logs)} log entries")

        # Display logs
        for entry in filtered[-200:]:
            level = entry.get("level", "INFO")
            timestamp = entry.get("timestamp", "")[:19]
            page_key = entry.get("page_key", "")
            step = entry.get("step", "")
            message = entry.get("message", "")

            if level == "ERROR":
                st.error(f"`{timestamp}` **[{page_key}]** [{step}] {message}")
            elif level == "WARNING":
                st.warning(f"`{timestamp}` **[{page_key}]** [{step}] {message}")
            else:
                st.text(f"{timestamp} [{page_key}] [{step}] {message}")

    # Clear state
    st.divider()
    if st.button("Clear All Data", type="secondary"):
        for key in DEFAULTS:
            st.session_state[key] = DEFAULTS[key]
        st.session_state.pop("results_excel_path", None)
        st.session_state.pop("log_file_path", None)
        st.session_state.pop("_state_loaded", None)
        st.session_state.pop("current_run_id", None)
        st.rerun()
