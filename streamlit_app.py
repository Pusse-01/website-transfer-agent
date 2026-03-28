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
}

for key, default in DEFAULTS.items():
    if key not in st.session_state:
        st.session_state[key] = default

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
    builder_public_key = st.text_input(
        "Public API Key",
        value=os.getenv("BUILDER_PUBLIC_KEY", "6be3ec8a86714634979b0d3ca2064d06"),
        help="Used for reading content from Builder.io",
    )
    builder_private_key = st.text_input(
        "Private API Key",
        value=os.getenv("BUILDER_API_KEY", ""),
        type="password",
        help="Required for uploading to Builder.io",
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
    }


# ---------------------------------------------------------------------------
# Main content
# ---------------------------------------------------------------------------
st.title("Website Transfer Agent")
st.caption("Migrate blog posts and static pages from Magento to Builder.io")

tab_input, tab_preview, tab_pipeline, tab_builder, tab_results, tab_logs = st.tabs(
    ["1. Upload Excel", "2. Preview Pages", "3. Run Migration",
     "4. Builder.io Browser", "5. Results & Export", "6. Logs"]
)

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
                    st.dataframe(preview, use_container_width=True)
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
                    st.dataframe(preview, use_container_width=True)
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
                scrape_preview = st.button("Scrape & Preview", type="primary", use_container_width=True)

            if scrape_preview:
                url_key = selected_page.get("url_key", "")
                page_type = selected_page.get("page_type", "blog")

                with st.spinner(f"Scraping {url_key}..."):
                    try:
                        if page_type == "blog":
                            scraper = BlogScraper(source_url, blog_path)
                            post_data = scraper.fetch_post_by_url_key(url_key)
                        else:
                            scraper = StaticPageScraper(source_url)
                            primary_url = selected_page.get("primary_url", "")
                            post_data = scraper.fetch_page_by_url(primary_url, url_key)

                        if post_data.get("error"):
                            st.error(f"Scraping failed: {post_data['error']}")
                        elif not post_data.get("html_content"):
                            st.warning("No content found for this page.")
                        else:
                            st.session_state.scraped_posts[url_key] = post_data
                            st.success(f"Scraped: {post_data.get('title', url_key)}")
                    except Exception as e:
                        st.error(f"Exception: {e}")

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
            use_container_width=True,
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
            agent = MigrationAgent(
                source_base_url=source_url,
                builder_api_key=builder_private_key,
                builder_model="blog-post",
                blog_path=blog_path,
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
            try:
                excel_path = export_blog_results(agent.results)
                if excel_path:
                    st.session_state["results_excel_path"] = excel_path
                    agent.mlog.info("__pipeline__", "", "export", f"Results exported to {excel_path}")
            except Exception as e:
                st.error(f"Failed to export Excel: {e}")

            # Export log
            try:
                log_path = agent.export_log()
                st.session_state["log_file_path"] = log_path
            except Exception:
                pass

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


# ========================== TAB 4: BUILDER.IO BROWSER ==========================
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
                "Fetch Content", type="primary", use_container_width=True
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
            st.dataframe(table_data, use_container_width=True)

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


# ========================== TAB 5: RESULTS & EXPORT ==========================
with tab_results:
    st.subheader("Migration Results & Export")

    if not st.session_state.migration_results:
        st.info("No migration results yet. Run the pipeline first.")
    else:
        r = st.session_state.migration_results

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
            st.dataframe(table_data, use_container_width=True)

        # Export buttons
        st.divider()
        st.markdown("### Export")

        col_excel, col_json, col_log = st.columns(3)

        with col_excel:
            # Excel export
            excel_path = st.session_state.get("results_excel_path", "")
            if excel_path and Path(excel_path).exists():
                with open(excel_path, "rb") as f:
                    st.download_button(
                        "Download Results Excel",
                        data=f.read(),
                        file_name=f"migration_results_{datetime.now().strftime('%Y%m%d')}.xlsx",
                        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        use_container_width=True,
                    )
            else:
                # Generate on-the-fly
                if st.button("Generate Excel Report", use_container_width=True):
                    try:
                        path = export_blog_results(r)
                        st.session_state["results_excel_path"] = path
                        st.rerun()
                    except Exception as e:
                        st.error(f"Failed to generate Excel: {e}")

        with col_json:
            st.download_button(
                "Download Results JSON",
                data=json.dumps(r, indent=2, ensure_ascii=False),
                file_name="migration_results.json",
                mime="application/json",
                use_container_width=True,
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
                        use_container_width=True,
                    )


# ========================== TAB 6: LOGS ==========================
with tab_logs:
    st.subheader("Migration Logs")

    if not st.session_state.pipeline_log:
        st.info("No logs yet. Run the migration pipeline to generate logs.")
    else:
        logs = st.session_state.pipeline_log

        # Filter controls
        col_filter_key, col_filter_step, col_filter_level = st.columns(3)

        with col_filter_key:
            all_keys = list(dict.fromkeys(e["page_key"] for e in logs))
            filter_key = st.selectbox("Filter by page key", ["All"] + all_keys, key="log_filter_key")

        with col_filter_step:
            all_steps = list(dict.fromkeys(e["step"] for e in logs))
            filter_step = st.selectbox("Filter by step", ["All"] + all_steps, key="log_filter_step")

        with col_filter_level:
            filter_level = st.selectbox("Filter by level", ["All", "ERROR", "WARNING", "INFO", "DEBUG"],
                                        key="log_filter_level")

        # Apply filters
        filtered = logs
        if filter_key != "All":
            filtered = [e for e in filtered if e["page_key"] == filter_key]
        if filter_step != "All":
            filtered = [e for e in filtered if e["step"] == filter_step]
        if filter_level != "All":
            filtered = [e for e in filtered if e["level"] == filter_level]

        st.caption(f"Showing {len(filtered)} of {len(logs)} log entries")

        # Display logs
        for entry in filtered[-100:]:  # Show last 100
            level = entry.get("level", "INFO")
            icon = {"ERROR": "!!!", "WARNING": "!", "INFO": "", "DEBUG": ""}.get(level, "")
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
        st.rerun()
