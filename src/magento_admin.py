"""
Magento 2 admin client.

Logs into the Magento admin panel (with optional Google-Authenticator-style
TOTP second factor) and exposes helpers to read raw CMS page / block content
that may not be publicly accessible on the storefront.

The authenticated ``requests.Session`` can also be handed to
:class:`src.page_extractor.MagentoPageExtractor` so unpublished or
customer-only pages can be rendered and captured.

Only use this with credentials the operator owns — never store passwords in
source control. Values are read from environment variables or passed in
explicitly at the call site.
"""

from __future__ import annotations

import logging
import re
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)


class MagentoAdminError(RuntimeError):
    """Raised when the admin client cannot complete an action."""


class MagentoAdminClient:
    """Authenticated client for Magento 2 admin panel.

    The Magento admin routes and form fields are version-dependent. Where
    possible we read hidden form fields (including the ``form_key``) from the
    login page rather than hardcoding values, so the client works across
    releases without modification.
    """

    def __init__(self, base_url: str, admin_path: str = "/adminControl/"):
        self.base_url = base_url.rstrip("/")
        self.admin_path = "/" + admin_path.strip("/") + "/"
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0 Safari/537.36"
            ),
            "Accept-Language": "en-US,en;q=0.9",
        })
        self._logged_in = False

    # ------------------------------------------------------------ public API
    @property
    def admin_url(self) -> str:
        return urljoin(self.base_url, self.admin_path)

    def login(self, username: str, password: str, totp_code: str | None = None) -> None:
        """Log into the Magento admin, honoring 2FA if required."""
        if not username or not password:
            raise MagentoAdminError("Username and password are required")

        login_page = self._get(self.admin_url)
        soup = BeautifulSoup(login_page.text, "html.parser")
        form = soup.select_one("form[id*='login']") or soup.find("form")
        if form is None:
            raise MagentoAdminError("Could not locate the admin login form")

        action = urljoin(self.admin_url, form.get("action") or self.admin_url)
        payload = _collect_hidden_fields(form)
        payload.update({
            "login[username]": username,
            "login[password]": password,
        })

        response = self._post(action, data=payload)
        redirect_url = response.url

        if "login" in redirect_url and totp_code:
            # Magento 2 Two-Factor Auth module may show a TOTP form next.
            self._submit_totp(response, totp_code)
        elif totp_code and self._looks_like_otp_page(response.text):
            self._submit_totp(response, totp_code)

        if not self._confirm_logged_in():
            raise MagentoAdminError(
                "Admin login did not succeed. Verify credentials, the admin "
                "path, and the TOTP code if 2FA is enabled."
            )
        self._logged_in = True
        logger.info("Logged into Magento admin as %s", username)

    def fetch_cms_page_source(self, page_id: int | str) -> dict:
        """Return the raw CMS page as stored in Magento.

        Pulls the edit screen for the given page and reads the form fields so
        callers can recover the original Page Builder content / layout /
        stores without access to the database.
        """
        self._require_login()
        edit_url = urljoin(self.admin_url, f"cms/page/edit/page_id/{page_id}/")
        html = self._get(edit_url).text
        soup = BeautifulSoup(html, "html.parser")

        def _field(name: str) -> str:
            el = soup.find("input", {"name": name}) or soup.find("textarea", {"name": name})
            if el is None:
                return ""
            return el.get("value") or el.text or ""

        return {
            "page_id": page_id,
            "title": _field("title"),
            "identifier": _field("identifier"),
            "content_heading": _field("content_heading"),
            "content": _field("content"),
            "layout_update_xml": _field("layout_update_xml"),
            "meta_title": _field("meta_title"),
            "meta_keywords": _field("meta_keywords"),
            "meta_description": _field("meta_description"),
        }

    def close(self) -> None:
        self.session.close()

    # --------------------------------------------------------------- helpers
    def _submit_totp(self, prev_response: requests.Response, code: str) -> None:
        soup = BeautifulSoup(prev_response.text, "html.parser")
        form = soup.find("form")
        if not form:
            raise MagentoAdminError("2FA was required but no TOTP form was found")
        action = urljoin(prev_response.url, form.get("action") or prev_response.url)
        payload = _collect_hidden_fields(form)
        # Magento TFA module typically uses name="tfa_code" for the TOTP.
        for field_name in ("tfa_code", "code", "otp", "google[code]"):
            if soup.find("input", {"name": field_name}):
                payload[field_name] = code
                break
        else:
            payload["tfa_code"] = code  # sensible default
        self._post(action, data=payload)

    def _confirm_logged_in(self) -> bool:
        """Hit the admin dashboard; if we get redirected to login we failed."""
        resp = self._get(urljoin(self.admin_url, "admin/dashboard/"))
        return "login" not in resp.url and "Invalid" not in resp.text

    def _require_login(self) -> None:
        if not self._logged_in:
            raise MagentoAdminError("Call login() before using admin APIs")

    @staticmethod
    def _looks_like_otp_page(html: str) -> bool:
        needles = ("Two-Factor", "Authenticator", "tfa_code", "Google Authenticator")
        return any(n.lower() in html.lower() for n in needles)

    def _get(self, url: str) -> requests.Response:
        resp = self.session.get(url, timeout=30, allow_redirects=True)
        resp.raise_for_status()
        return resp

    def _post(self, url: str, data: dict) -> requests.Response:
        resp = self.session.post(url, data=data, timeout=30, allow_redirects=True)
        resp.raise_for_status()
        return resp


def _collect_hidden_fields(form) -> dict:
    """Gather every hidden input field from a form (including form_key)."""
    payload = {}
    for inp in form.find_all("input"):
        name = inp.get("name")
        if not name:
            continue
        payload[name] = inp.get("value", "")
    return payload


# Convenience helper -------------------------------------------------------

_FORM_KEY_RE = re.compile(r'var\s+FORM_KEY\s*=\s*["\']([^"\']+)')


def extract_form_key(html: str) -> str | None:
    """Pull Magento's FORM_KEY out of an admin page for ad-hoc requests."""
    match = _FORM_KEY_RE.search(html)
    return match.group(1) if match else None
