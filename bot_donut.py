"""
Donut Browser variant of the review bot.

Replaces the seleniumbase sb_cdp approach with:
  1. Donut Browser REST API  — creates/reuses a persistent anti-detect
     "wayfern" profile per email address, attaches the PROXY if configured.
  2. Playwright connect_over_cdp — attaches to the running browser via the
     CDP port returned by /v1/profiles/{id}/run.

Fingerprinting, browser isolation, and proxy routing are all handled by
Donut.  No manual UA / resolution overrides are needed.

Required env vars (add to .env):
    DONUT_API_KEY   Bearer token — Donut app → Settings → Integrations → Local API
    DONUT_BASE_URL  Default: http://127.0.0.1:10108
    PROXY           Optional: scheme://[user:pass@]host:port
                    e.g. socks5://user:pass@1.2.3.4:1080

NOTE: /v1/profiles/{id}/run and /v1/profiles/{id}/kill require an active
Donut Pro subscription (HTTP 402 otherwise).

To switch the worker to use Donut, change worker.py:
    import bot_donut as bot
    bot.run_reviews_donut(...)   # instead of bot.run_reviews(...)
"""

import logging
import os
import random
import time
import traceback
from urllib.parse import urlparse

import pyotp
import requests
from playwright.sync_api import TimeoutError as PlaywrightTimeout
from playwright.sync_api import sync_playwright

import db
from bot import generate_review

log = logging.getLogger(__name__)

DONUT_API_KEY = os.environ.get("DONUT_API_KEY", "")
DONUT_BASE_URL = os.environ.get("DONUT_BASE_URL", "http://127.0.0.1:10108")
PROXY = os.environ.get("PROXY", "")


# ── Donut REST API wrapper ─────────────────────────────────────────────────────

class DonutAPI:
    """Thin synchronous wrapper around the Donut Browser local REST API."""

    def __init__(self):
        self._base = DONUT_BASE_URL.rstrip("/")
        self._h = {
            "Authorization": f"Bearer {DONUT_API_KEY}",
            "Content-Type": "application/json",
        }

    def _get(self, path):
        r = requests.get(f"{self._base}{path}", headers=self._h, timeout=15)
        r.raise_for_status()
        return r.json()

    def _post(self, path, data=None):
        r = requests.post(f"{self._base}{path}", json=data or {}, headers=self._h, timeout=15)
        r.raise_for_status()
        return r.json() if r.content else {}

    def _delete(self, path):
        r = requests.delete(f"{self._base}{path}", headers=self._h, timeout=15)
        r.raise_for_status()

    # ── profiles ──────────────────────────────────────────────────────────────

    def list_profiles(self):
        return self._get("/v1/profiles")["profiles"]

    def create_profile(self, name, proxy_id=None):
        payload = {
            "name": name,
            "browser": "wayfern",
            "version": "latest",
            "wayfern_config": {},
        }
        if proxy_id:
            payload["proxy_id"] = proxy_id
        return self._post("/v1/profiles", payload)["profile"]

    def delete_profile(self, profile_id):
        self._delete(f"/v1/profiles/{profile_id}")

    def run_profile(self, profile_id, headless=False):
        """Launch the profile and return the CDP port."""
        result = self._post(
            f"/v1/profiles/{profile_id}/run",
            {"headless": headless},
        )
        return result["cdp_port"]

    def kill_profile(self, profile_id):
        try:
            self._post(f"/v1/profiles/{profile_id}/kill")
        except Exception:
            pass

    def find_profile_by_name(self, name):
        for p in self.list_profiles():
            if p["name"] == name:
                return p
        return None

    def get_or_create_profile(self, name, proxy_id=None):
        """Return the existing profile with *name*, or create a fresh one."""
        profile = self.find_profile_by_name(name)
        if not profile:
            profile = self.create_profile(name, proxy_id=proxy_id)
            log.info("Created Donut profile '%s' (%s)", name, profile["id"])
        else:
            log.info("Reusing Donut profile '%s' (%s)", name, profile["id"])
        return profile

    # ── proxies ───────────────────────────────────────────────────────────────

    def list_proxies(self):
        return self._get("/v1/proxies")

    def create_proxy(self, name, proxy_type, host, port, username=None, password=None):
        settings = {"proxy_type": proxy_type, "host": host, "port": port}
        if username:
            settings["username"] = username
        if password:
            settings["password"] = password
        return self._post("/v1/proxies", {"name": name, "proxy_settings": settings})


# ── Proxy helpers ──────────────────────────────────────────────────────────────

def _parse_proxy(proxy_str):
    """Parse PROXY env var into a dict. Returns None if empty."""
    if not proxy_str:
        return None
    if "://" not in proxy_str:
        proxy_str = "http://" + proxy_str
    p = urlparse(proxy_str)
    return {
        "scheme": p.scheme or "http",
        "host": p.hostname,
        "port": p.port or 8080,
        "username": p.username,
        "password": p.password,
    }


def _get_or_create_donut_proxy(donut: DonutAPI, proxy_str: str):
    """Find an existing Donut proxy matching host:port, or create one.
    Returns proxy_id, or None if no proxy is configured."""
    parsed = _parse_proxy(proxy_str)
    if not parsed:
        return None

    for px in donut.list_proxies():
        s = px.get("proxy_settings", {})
        if s.get("host") == parsed["host"] and s.get("port") == parsed["port"]:
            log.info("Reusing Donut proxy '%s' (%s)", px["name"], px["id"])
            return px["id"]

    proxy_name = f"{parsed['host']}:{parsed['port']}"
    px = donut.create_proxy(
        name=proxy_name,
        proxy_type=parsed["scheme"],
        host=parsed["host"],
        port=parsed["port"],
        username=parsed.get("username"),
        password=parsed.get("password"),
    )
    log.info("Created Donut proxy '%s' (%s)", proxy_name, px["id"])
    return px["id"]


# ── Playwright helpers ─────────────────────────────────────────────────────────

def _click_if_visible(page, selector, timeout=5_000):
    """Click *selector* if present and visible; silently skip otherwise."""
    try:
        el = page.wait_for_selector(selector, timeout=timeout, state="visible")
        if el:
            el.click()
            return True
    except Exception:
        pass
    return False


# ── Core review function ───────────────────────────────────────────────────────

def post_review_donut(
    email,
    password,
    maps_url,
    star_rating,
    business_name,
    review_prompt,
    email_id,
    recovery_email=None,
    review_type="medium",
    language="English",
    totp_secret=None,
):
    donut = DonutAPI()

    # Resolve proxy
    proxy_id = _get_or_create_donut_proxy(donut, PROXY) if PROXY else None

    # Get or create the persistent anti-detect profile for this email
    profile_name = f"review_{email}"
    profile = donut.get_or_create_profile(profile_name, proxy_id=proxy_id)
    profile_id = profile["id"]

    # Kill any stale running instance before launching fresh
    if profile.get("is_running"):
        log.info("Profile '%s' still running — killing before relaunch.", profile_name)
        donut.kill_profile(profile_id)
        time.sleep(3)

    log.info("Launching Donut profile '%s'...", profile_name)
    cdp_port = donut.run_profile(profile_id, headless=False)
    log.info("Profile '%s' on CDP port %d", profile_name, cdp_port)
    time.sleep(8)  # Give Wayfern time to initialize before connecting

    iframe_sel = "iframe[name='goog-reviews-write-widget']"

    try:
        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(f"http://127.0.0.1:{cdp_port}")
            context = browser.contexts[0]
            page = context.pages[0] if context.pages else context.new_page()
            page.set_default_timeout(30_000)

            # ── Login check ───────────────────────────────────────────────────
            page.goto(
                "https://accounts.google.com/signin/v2/identifier",
                wait_until="domcontentloaded",
            )
            page.wait_for_timeout(10_000)

            # If Google still shows a sign-in page, we need to log in
            if "accounts.google.com" in page.url:
                page.wait_for_timeout(2_000)

                page.locator('input[aria-label="Email or phone"]').click(timeout=15_000)
                page.wait_for_timeout(3_000)
                page.locator('input[aria-label="Email or phone"]').type(email, delay=80)
                page.wait_for_timeout(3_000)
                page.locator("#identifierNext").click(timeout=15_000)
                page.wait_for_timeout(5_000)

                page.locator('input[type="password"]').click(timeout=30_000)
                page.wait_for_timeout(3_000)
                page.locator('input[type="password"]').type(password, delay=80)
                page.wait_for_timeout(3_000)
                page.locator("#passwordNext").click(timeout=15_000)
                page.wait_for_timeout(5_000)

                # ── 2FA (TOTP) ────────────────────────────────────────────────
                try:
                    page.wait_for_selector(
                        'input[name="totpPin"]', timeout=20_000, state="visible"
                    )
                    totp_code = pyotp.TOTP(totp_secret).now() if totp_secret else ""
                    page.locator('input[name="totpPin"]').click(timeout=10_000)
                    page.wait_for_timeout(3_000)
                    page.locator('input[name="totpPin"]').fill(totp_code)
                    page.wait_for_timeout(3_000)
                    page.locator("#totpNext").click(timeout=15_000)
                    page.wait_for_timeout(5_000)
                except PlaywrightTimeout:
                    pass

                # Dismiss optional Cancel / Skip dialogs
                _click_if_visible(page, 'button[aria-label="Cancel"]', timeout=10_000)
                page.wait_for_timeout(3_000)
                _click_if_visible(page, 'button[aria-label="Skip"]', timeout=10_000)

            # ── Navigate to the Maps listing ──────────────────────────────────
            page.wait_for_timeout(random.randint(10_000, 15_000))
            page.goto(maps_url, wait_until="domcontentloaded")
            page.wait_for_timeout(random.randint(5_000, 10_000))

            page.locator('button[aria-label*="Reviews"]').click(timeout=30_000)
            page.wait_for_timeout(random.randint(10_000, 15_000))

            page.locator('button[aria-label="Write a review"]').click(timeout=30_000)
            page.wait_for_timeout(random.randint(5_000, 10_000))

            # ── Wait for the review iframe ────────────────────────────────────
            page.wait_for_selector(iframe_sel, timeout=30_000)
            page.wait_for_timeout(5_000)

            iframe = page.frame_locator(iframe_sel)
            review_area = iframe.locator('textarea[aria-label="Enter review"]')

            # Poll for the textarea (up to 60 s)
            for attempt in range(24):  # 24 × 2.5 s = 60 s max
                try:
                    review_area.wait_for(timeout=2_500, state="visible")
                    break
                except PlaywrightTimeout:
                    log.debug("Waiting for review textarea (attempt %d/24)...", attempt + 1)
            else:
                raise RuntimeError("Review textarea never appeared inside iframe after 60s")

            review_area.click()
            page.wait_for_timeout(3_000)

            # Select star rating first so the dialog stays active during Gemini call
            iframe.locator(f"div[data-rating='{star_rating}'][role='radio']").click()
            page.wait_for_timeout(random.randint(2_000, 5_000))

            # Generate review text now that the write box is confirmed open
            review_text = generate_review(
                business_name, review_prompt, star_rating, review_type, language
            )
            log.info("Review generated for %s (type=%s, lang=%s)", email, review_type, language)

            # Type review text into the textarea (skip for no_text reviews)
            if review_text:
                review_area.type(review_text, delay=50)
                page.wait_for_timeout(random.randint(2_000, 5_000))

            # Click the Post button inside the iframe by text content
            page.evaluate("""
                var iframe = document.querySelector("iframe[name='goog-reviews-write-widget']");
                var doc = iframe.contentDocument || iframe.contentWindow.document;
                var buttons = doc.querySelectorAll('button');
                for (var btn of buttons) {
                    if (btn.textContent.trim() === 'Post') { btn.click(); break; }
                }
            """)
            page.wait_for_timeout(random.randint(3_000, 5_000))

            # Click Done inside the iframe
            iframe.locator('button[aria-label="Done"]').click(timeout=15_000)
            page.wait_for_timeout(random.randint(3_000, 5_000))

            db.update_review_status(email_id, "reviewed", review_text, star_rating)
            log.info("Review posted successfully for %s", email)
            return True

    except Exception as exc:
        log.error("post_review_donut failed for %s: %s", email, exc)
        log.error(traceback.format_exc())
        db.update_review_status(email_id, "pending", None, star_rating)
        return False

    finally:
        log.info("Killing Donut profile '%s'...", profile_name)
        donut.kill_profile(profile_id)


# ── Run loop (drop-in replacement for bot.run_reviews) ────────────────────────

def run_reviews_donut(business_id, biz, config, delay=60):
    pending = db.get_pending_reviews(business_id)
    previous_email = None

    for review_row in pending:
        star = review_row["star_rating"] or 5

        db.update_review_status(review_row["id"], "reviewing", None, star)

        log.info("Opening Donut browser for %s", review_row["email"])
        success = post_review_donut(
            review_row["email"],
            review_row["password"],
            biz["url"],
            star,
            biz["name"],
            config.get("business_description", ""),
            review_row["id"],
            recovery_email=previous_email,
            review_type=review_row.get("review_type") or "medium",
            language=review_row.get("review_language") or "English",
            totp_secret=review_row.get("totp_secret"),
        )
        if not success:
            log.warning("post_review_donut returned False for %s", review_row["email"])
            flagged = db.increment_email_fail_count(review_row["email_id"])
            if flagged:
                log.warning(
                    "Email %s has failed 5 times — flagged and removed from all pending jobs.",
                    review_row["email"],
                )
        previous_email = review_row["email"]

        actual_delay = random.uniform(delay, delay + 300)
        log.info("Waiting %.0fs before next review...", actual_delay)
        time.sleep(actual_delay)
