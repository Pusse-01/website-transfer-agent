"""
Blog Migration Dashboard - Streamlit App

Scrape blog posts from source websites, preview them in-browser,
and optionally upload to Builder.io when ready.

Usage:
    streamlit run streamlit_app.py
"""

import json
import os
import tempfile
from datetime import datetime
from pathlib import Path

import streamlit as st
import streamlit.components.v1 as components
from dotenv import load_dotenv

from src.scraper import BlogScraper
from src.html_preview import generate_blog_preview_html
from src.builder_client import BuilderClient
from src.image_handler import ImageHandler
from src.excel_reader import read_blog_list

load_dotenv()

# ---------------------------------------------------------------------------
# Page config
# ---------------------------------------------------------------------------
st.set_page_config(
    page_title="Blog Migration Dashboard",
    page_icon="📝",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ---------------------------------------------------------------------------
# Session state defaults
# ---------------------------------------------------------------------------
if "scraped_posts" not in st.session_state:
    st.session_state.scraped_posts = {}  # url_key -> post_data dict
if "selected_post" not in st.session_state:
    st.session_state.selected_post = None
if "scrape_log" not in st.session_state:
    st.session_state.scrape_log = []
if "upload_results" not in st.session_state:
    st.session_state.upload_results = {}
if "builder_entries" not in st.session_state:
    st.session_state.builder_entries = []

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
        "Private API Key (optional)",
        value=os.getenv("BUILDER_API_KEY", ""),
        type="password",
        help="Required only for uploading to Builder.io",
    )
    builder_model = st.text_input(
        "Model Name",
        value=os.getenv("BUILDER_MODEL_NAME", "blog-post"),
    )

    st.divider()
    st.subheader("Status")
    st.metric("Posts Scraped", len(st.session_state.scraped_posts))
    uploaded_count = sum(
        1 for v in st.session_state.upload_results.values() if v.get("success")
    )
    st.metric("Posts Uploaded", uploaded_count)

# ---------------------------------------------------------------------------
# Helper: extract HTML from Builder.io blocks
# ---------------------------------------------------------------------------
def extract_html_from_blocks(blocks: list) -> str:
    """Recursively extract HTML content from Builder.io blocks."""
    html_parts = []
    for block in blocks:
        comp = block.get("component", {})
        name = comp.get("name", "")

        # Custom Code block - contains raw HTML
        if name == "Custom Code":
            code = comp.get("options", {}).get("code", "")
            if code:
                html_parts.append(code)

        # Text block
        elif name == "Text":
            text = comp.get("options", {}).get("text", "")
            if text:
                html_parts.append(text)

        # Image block
        elif name == "Image":
            opts = comp.get("options", {})
            img_url = opts.get("image", "")
            alt = opts.get("altText", "")
            if img_url:
                html_parts.append(f'<img src="{img_url}" alt="{alt}" style="max-width:100%" />')

        # Recurse into children
        children = block.get("children", [])
        if children:
            html_parts.append(extract_html_from_blocks(children))

        # Handle Columns
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

    # Extract HTML from blocks
    html_content = extract_html_from_blocks(blocks)

    # Extract tags
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
st.title("Blog Migration Dashboard")

tab_input, tab_preview, tab_builder, tab_upload, tab_results = st.tabs(
    ["1. Input & Scrape", "2. Preview", "3. Builder.io Content", "4. Upload to Builder.io", "5. Results"]
)

# ========================== TAB 1: INPUT & SCRAPE ==========================
with tab_input:
    st.subheader("Add Blog Posts to Scrape")

    input_method = st.radio(
        "Input method",
        ["URL Keys (comma-separated)", "Excel File Upload"],
        horizontal=True,
    )

    url_keys_to_scrape = []

    if input_method == "URL Keys (comma-separated)":
        url_keys_text = st.text_area(
            "Enter URL keys",
            placeholder="airconditioner_hp, dehumidifiers, washing_machine_kg",
            help="Comma-separated blog URL keys (the slug part of the URL)",
        )
        if url_keys_text:
            url_keys_to_scrape = [
                k.strip() for k in url_keys_text.split(",") if k.strip()
            ]
    else:
        uploaded_file = st.file_uploader(
            "Upload Excel file with blog post list",
            type=["xlsx", "xls"],
        )
        if uploaded_file:
            with tempfile.NamedTemporaryFile(
                suffix=".xlsx", delete=False
            ) as tmp:
                tmp.write(uploaded_file.read())
                tmp_path = tmp.name

            try:
                blog_posts = read_blog_list(tmp_path)
                if blog_posts:
                    st.success(f"Found {len(blog_posts)} blog posts in Excel file")
                    preview_data = [
                        {
                            "Priority": p.get("priority", ""),
                            "Title": p.get("title", ""),
                            "URL Key": p.get("url_key", ""),
                            "Status": p.get("status", ""),
                        }
                        for p in blog_posts
                    ]
                    st.dataframe(preview_data, use_container_width=True)
                    url_keys_to_scrape = [p["url_key"] for p in blog_posts]
                else:
                    st.error("No blog posts found in the Excel file.")
            finally:
                os.unlink(tmp_path)

    if url_keys_to_scrape:
        st.info(f"Ready to scrape **{len(url_keys_to_scrape)}** blog post(s)")

        col1, col2 = st.columns([1, 3])
        with col1:
            scrape_clicked = st.button(
                "Scrape Blog Posts", type="primary", use_container_width=True
            )

        if scrape_clicked:
            scraper = BlogScraper(source_url, blog_path)
            progress_bar = st.progress(0)
            status_text = st.empty()
            log_area = st.container()

            for i, url_key in enumerate(url_keys_to_scrape):
                progress = (i + 1) / len(url_keys_to_scrape)
                status_text.markdown(
                    f"**Scraping** `{url_key}` ({i+1}/{len(url_keys_to_scrape)})"
                )
                progress_bar.progress(progress)

                try:
                    post_data = scraper.fetch_post_by_url_key(url_key)

                    if post_data.get("error"):
                        msg = f"Failed: {url_key} - {post_data['error']}"
                        st.session_state.scrape_log.append(
                            {"time": datetime.now().isoformat(), "level": "error", "msg": msg}
                        )
                        with log_area:
                            st.error(msg)
                    elif not post_data.get("html_content"):
                        msg = f"No content found for: {url_key}"
                        st.session_state.scrape_log.append(
                            {"time": datetime.now().isoformat(), "level": "warning", "msg": msg}
                        )
                        with log_area:
                            st.warning(msg)
                    else:
                        st.session_state.scraped_posts[url_key] = post_data
                        title = post_data.get("title", url_key)
                        msg = f"Scraped: {title}"
                        st.session_state.scrape_log.append(
                            {"time": datetime.now().isoformat(), "level": "success", "msg": msg}
                        )
                        with log_area:
                            st.success(msg)

                except Exception as e:
                    msg = f"Exception scraping {url_key}: {e}"
                    st.session_state.scrape_log.append(
                        {"time": datetime.now().isoformat(), "level": "error", "msg": msg}
                    )
                    with log_area:
                        st.error(msg)

            progress_bar.progress(1.0)
            status_text.markdown("**Scraping complete!**")
            st.rerun()

    # Show currently scraped posts
    if st.session_state.scraped_posts:
        st.divider()
        st.subheader("Scraped Posts")
        for key, post in st.session_state.scraped_posts.items():
            title = post.get("title", key)
            source = post.get("source", "unknown")
            n_images = len(post.get("images", []))
            st.markdown(
                f"- **{title}** (`{key}`) — source: `{source}`, images: {n_images}"
            )


# ========================== TAB 2: PREVIEW ==========================
with tab_preview:
    st.subheader("Blog Post Preview")

    if not st.session_state.scraped_posts:
        st.info("No scraped posts yet. Go to **Input & Scrape** tab first.")
    else:
        post_keys = list(st.session_state.scraped_posts.keys())
        post_labels = [
            f"{st.session_state.scraped_posts[k].get('title', k)} ({k})"
            for k in post_keys
        ]

        selected_idx = st.selectbox(
            "Select a post to preview",
            range(len(post_keys)),
            format_func=lambda i: post_labels[i],
        )
        selected_key = post_keys[selected_idx]
        post_data = st.session_state.scraped_posts[selected_key]

        # Show metadata in expander
        with st.expander("Post Metadata", expanded=False):
            meta_cols = st.columns(3)
            with meta_cols[0]:
                st.markdown(f"**URL Key:** `{post_data.get('url_key', '')}`")
                st.markdown(f"**Source:** `{post_data.get('source', '')}`")
            with meta_cols[1]:
                st.markdown(f"**Published:** {post_data.get('published_at', 'N/A')}")
                st.markdown(f"**Images:** {len(post_data.get('images', []))}")
            with meta_cols[2]:
                st.markdown(
                    f"**Tags:** {', '.join(str(t) for t in post_data.get('tags', [])) or 'None'}"
                )
                cats = post_data.get("categories", [])
                if isinstance(cats, list):
                    cats_str = ", ".join(str(c) for c in cats) or "None"
                else:
                    cats_str = str(cats) or "None"
                st.markdown(f"**Categories:** {cats_str}")

        preview_html = generate_blog_preview_html(post_data, base_url=source_url)

        st.markdown("---")
        components.html(preview_html, height=800, scrolling=True)

        # Buttons row
        col_dl_html, col_dl_json, col_raw = st.columns(3)

        with col_dl_html:
            st.download_button(
                "Download HTML",
                data=preview_html,
                file_name=f"{selected_key}.html",
                mime="text/html",
            )

        with col_dl_json:
            json_data = json.dumps(post_data, indent=2, ensure_ascii=False)
            st.download_button(
                "Download JSON",
                data=json_data,
                file_name=f"{selected_key}.json",
                mime="application/json",
            )

        with col_raw:
            with st.expander("View Raw HTML"):
                st.code(post_data.get("html_content", ""), language="html")


# ========================== TAB 3: BUILDER.IO CONTENT ==========================
with tab_builder:
    st.subheader("Builder.io Content Browser")

    if not builder_public_key:
        st.warning("Enter a Builder.io **Public API Key** in the sidebar to browse content.")
    else:
        col_fetch, col_info = st.columns([1, 3])
        with col_fetch:
            fetch_clicked = st.button(
                "Fetch Content from Builder.io", type="primary", use_container_width=True
            )

        if fetch_clicked:
            with st.spinner("Fetching entries from Builder.io..."):
                client = BuilderClient(builder_public_key, builder_model)
                entries = client.fetch_all_entries(limit=50, include_unpublished=True)
                st.session_state.builder_entries = entries
                if entries:
                    st.success(f"Fetched {len(entries)} entries from Builder.io")
                else:
                    st.warning("No entries found. Check your API key and model name.")

        if st.session_state.builder_entries:
            entries = st.session_state.builder_entries

            # Summary table
            table_data = []
            for entry in entries:
                data = entry.get("data", {})
                table_data.append({
                    "Name": entry.get("name", ""),
                    "Title": data.get("title", ""),
                    "Slug": data.get("slug", ""),
                    "Status": entry.get("published", ""),
                    "Publish Date": data.get("publishDate", "N/A"),
                    "Has Blocks": "Yes" if data.get("blocks") else "No",
                    "Has Cover": "Yes" if data.get("coverImage") else "No",
                })
            st.dataframe(table_data, use_container_width=True)

            # Select entry to preview
            st.divider()
            entry_labels = [
                f"{e.get('name', 'Untitled')} ({e.get('data', {}).get('slug', 'no-slug')}) — {e.get('published', '')}"
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

            # Metadata
            with st.expander("Entry Metadata", expanded=False):
                meta_cols = st.columns(3)
                with meta_cols[0]:
                    st.markdown(f"**ID:** `{selected_entry.get('id', '')}`")
                    st.markdown(f"**Slug:** `{entry_data.get('slug', '')}`")
                    st.markdown(f"**Status:** `{selected_entry.get('published', '')}`")
                with meta_cols[1]:
                    st.markdown(f"**Description:** {entry_data.get('description', 'N/A')}")
                    st.markdown(f"**Excerpt:** {entry_data.get('excerpt', 'N/A')}")
                    st.markdown(f"**Author:** {entry_data.get('authorName', 'N/A')}")
                with meta_cols[2]:
                    st.markdown(f"**Publish Date:** {entry_data.get('publishDate', 'N/A')}")
                    cover = entry_data.get("coverImage", "")
                    st.markdown(f"**Cover Image:** {'Yes' if cover else 'No'}")
                    tags = entry_data.get("tags", [])
                    tag_strs = [t.get("tag", str(t)) if isinstance(t, dict) else str(t) for t in tags]
                    st.markdown(f"**Tags:** {', '.join(tag_strs) or 'None'}")

            # Cover image preview
            cover_image = entry_data.get("coverImage", "")
            if cover_image:
                st.image(cover_image, caption="Cover Image", width=400)

            # Render blocks as HTML preview
            blocks = entry_data.get("blocks", [])
            if blocks:
                st.markdown("### Page Preview")
                preview_post = builder_entry_to_preview_data(selected_entry)
                preview_html = generate_blog_preview_html(preview_post)
                components.html(preview_html, height=800, scrolling=True)
            else:
                st.warning("This entry has no content blocks. The page will appear empty in the editor.")

            # Raw JSON viewer
            with st.expander("Raw Entry JSON"):
                st.json(selected_entry)

            # Download
            st.download_button(
                "Download Entry JSON",
                data=json.dumps(selected_entry, indent=2, ensure_ascii=False),
                file_name=f"builder_{entry_data.get('slug', 'entry')}.json",
                mime="application/json",
                key="builder_dl_json",
            )


# ========================== TAB 4: UPLOAD ==========================
with tab_upload:
    st.subheader("Upload to Builder.io")

    if not builder_private_key or builder_private_key == "your_builder_private_api_key_here":
        st.warning(
            "**Private API key not configured.** "
            "Enter your Builder.io Private API Key in the sidebar to enable uploads. "
            "In the meantime, you can preview and download blog posts from the Preview tab."
        )
    elif not st.session_state.scraped_posts:
        st.info("No scraped posts yet. Go to **Input & Scrape** tab first.")
    else:
        publish_mode = st.checkbox("Publish immediately (otherwise save as draft)")

        # Select posts to upload
        all_keys = list(st.session_state.scraped_posts.keys())
        selected_keys = st.multiselect(
            "Select posts to upload",
            all_keys,
            default=all_keys,
            format_func=lambda k: f"{st.session_state.scraped_posts[k].get('title', k)} ({k})",
        )

        if selected_keys:
            st.info(f"Will upload **{len(selected_keys)}** post(s) to Builder.io model `{builder_model}`")

            if st.button("Upload to Builder.io", type="primary"):
                builder = BuilderClient(builder_private_key, builder_model)
                image_handler = ImageHandler(builder_private_key)
                progress = st.progress(0)
                status = st.empty()

                for i, key in enumerate(selected_keys):
                    post = st.session_state.scraped_posts[key]
                    status.markdown(f"**Uploading** `{key}` ({i+1}/{len(selected_keys)})")
                    progress.progress((i + 1) / len(selected_keys))

                    try:
                        # Process images
                        updated_html, mappings = image_handler.process_images_in_html(
                            post["html_content"], base_url=source_url
                        )
                        upload_post = {**post, "html_content": updated_html}

                        # Upload thumbnail
                        if post.get("thumbnail"):
                            new_thumb = image_handler.process_thumbnail(post["thumbnail"])
                            if new_thumb:
                                upload_post["thumbnail"] = new_thumb

                        # Create entry
                        result = builder.create_blog_entry(upload_post, publish=publish_mode)
                        st.session_state.upload_results[key] = result

                        if result.get("success"):
                            st.success(f"Uploaded: {post.get('title', key)}")
                        else:
                            st.error(f"Failed: {key} - {result.get('error', 'Unknown')}")

                    except Exception as e:
                        st.session_state.upload_results[key] = {
                            "success": False, "error": str(e)
                        }
                        st.error(f"Exception: {key} - {e}")

                progress.progress(1.0)
                status.markdown("**Upload complete!**")


# ========================== TAB 5: RESULTS ==========================
with tab_results:
    st.subheader("Migration Results")

    if not st.session_state.scraped_posts and not st.session_state.upload_results:
        st.info("No results yet. Start by scraping some blog posts.")
    else:
        # Summary metrics
        total = len(st.session_state.scraped_posts)
        uploaded_ok = sum(
            1 for v in st.session_state.upload_results.values() if v.get("success")
        )
        uploaded_fail = sum(
            1 for v in st.session_state.upload_results.values() if not v.get("success")
        )
        not_uploaded = total - uploaded_ok - uploaded_fail

        cols = st.columns(4)
        cols[0].metric("Total Scraped", total)
        cols[1].metric("Uploaded OK", uploaded_ok)
        cols[2].metric("Upload Failed", uploaded_fail)
        cols[3].metric("Pending Upload", not_uploaded)

        # Results table
        if st.session_state.scraped_posts:
            table_data = []
            for key, post in st.session_state.scraped_posts.items():
                upload = st.session_state.upload_results.get(key, {})
                table_data.append({
                    "URL Key": key,
                    "Title": post.get("title", "")[:50],
                    "Source": post.get("source", ""),
                    "Images": len(post.get("images", [])),
                    "Upload Status": (
                        "OK" if upload.get("success")
                        else upload.get("error", "Not uploaded")[:40]
                        if upload
                        else "Pending"
                    ),
                })
            st.dataframe(table_data, use_container_width=True)

        # Export all results
        if st.session_state.scraped_posts:
            st.divider()
            export_data = {
                "exported_at": datetime.now().isoformat(),
                "total": total,
                "posts": {},
            }
            for key, post in st.session_state.scraped_posts.items():
                export_data["posts"][key] = {
                    "title": post.get("title", ""),
                    "url_key": key,
                    "source": post.get("source", ""),
                    "images": post.get("images", []),
                    "meta_description": post.get("meta_description", ""),
                    "tags": post.get("tags", []),
                    "categories": post.get("categories", []),
                    "upload_result": st.session_state.upload_results.get(key),
                }

            st.download_button(
                "Export All Results (JSON)",
                data=json.dumps(export_data, indent=2, ensure_ascii=False),
                file_name="migration_results.json",
                mime="application/json",
            )

        # Scrape log
        if st.session_state.scrape_log:
            with st.expander("Scrape Log"):
                for entry in reversed(st.session_state.scrape_log[-50:]):
                    st.text(f"[{entry['time']}] {entry['level'].upper()}: {entry['msg']}")

    # Clear state button
    st.divider()
    if st.button("Clear All Data", type="secondary"):
        st.session_state.scraped_posts = {}
        st.session_state.selected_post = None
        st.session_state.scrape_log = []
        st.session_state.upload_results = {}
        st.session_state.builder_entries = []
        st.rerun()
