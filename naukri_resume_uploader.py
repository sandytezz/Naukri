#!/usr/bin/env python3
r"""
naukri_resume_uploader.py
==========================

Automates daily login to Naukri.com and re-upload of the latest resume
so the profile stays "recently updated" (which improves recruiter visibility).

Workflow
--------
1. Launch a browser with Playwright (headless by default).
2. Log in with credentials supplied via environment variables (never hard-coded).
3. Navigate to the profile / resume section.
4. Upload the resume file.
5. Verify the upload succeeded (checks for a success toast and/or updated
   "last modified" timestamp on the resume).
6. Log everything to a rotating log file and take a screenshot on failure
   for easy debugging.
7. Exit with a non-zero status code on failure so schedulers (Task Scheduler,
   cron, Jenkins, etc.) can detect and alert on failures.

Setup
-----
    pip install playwright
    playwright install chromium

Configuration (environment variables)
--------------------------------------
    NAUKRI_EMAIL          Your Naukri login email/username        (required)
    NAUKRI_PASSWORD       Your Naukri login password               (required)
    NAUKRI_RESUME_PATH    Absolute path to the resume file to upload (required)
    NAUKRI_HEADLESS       "true"/"false" - run browser headless    (default: true)
    NAUKRI_TIMEOUT_MS     Default Playwright timeout in ms         (default: 30000)
    NAUKRI_LOG_DIR        Directory to write logs/screenshots to   (default: ./logs)

Store credentials securely, e.g. in a `.env` file loaded by your scheduler,
Windows Credential Manager, or a secrets manager -- never commit them to
source control.

Scheduling
----------
Windows Task Scheduler:
    Program/script:  C:\path\to\python.exe
    Arguments:       C:\path\to\naukri_resume_uploader.py
    Trigger:         Daily, e.g. 9:00 AM
    (Set "Run whether user is logged on or not" and configure environment
    variables in a wrapper .bat file or via `setx` beforehand.)

cron (Linux/macOS):
    0 9 * * * /usr/bin/env NAUKRI_EMAIL=... NAUKRI_PASSWORD=... \
        NAUKRI_RESUME_PATH=/home/user/resume.pdf \
        /usr/bin/python3 /path/to/naukri_resume_uploader.py >> /var/log/naukri_uploader.log 2>&1

Notes
-----
- Naukri's DOM/selectors can change over time. Selectors below are grouped
  in the NaukriSelectors class and use resilient strategies (role-based
  locators, text matching, and fallbacks) but may still need periodic
  updates if Naukri redesigns their site.
- Respect Naukri's Terms of Service regarding automated access. This script
  is intended for personal account maintenance only (i.e., you automating
  your own account), run at a reasonable frequency (e.g., once per day).
"""

from __future__ import annotations

import logging
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

from playwright.sync_api import (
    Page,
    Browser,
    BrowserContext,
    Playwright,
    TimeoutError as PlaywrightTimeoutError,
    Error as PlaywrightError,
    sync_playwright,
)


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

# --------------------------------------------------------------------------- #
# EASY-EDIT DEFAULTS
# --------------------------------------------------------------------------- #
# If you don't want to deal with environment variables, just fill these in
# directly. They are only used as a FALLBACK when the corresponding
# environment variable (NAUKRI_EMAIL / NAUKRI_PASSWORD / NAUKRI_RESUME_PATH)
# is not set, so this stays safe to use with Jenkins/env-vars later too.
#
# IMPORTANT: this file will contain your password in plain text if you fill
# DEFAULT_PASSWORD in below. That's fine for a personal script on your own
# machine, but don't commit this file to git or share it with anyone.
DEFAULT_EMAIL = "santhosh09qa@gmail.com"          # e.g. "sandy@example.com"
DEFAULT_PASSWORD = "Qaengineer@0902"       # e.g. "MyPassword123"
DEFAULT_RESUME_PATH = r"D:\Naukri\Santhosh_QA_11_11.pdf"  # use r"..." for Windows paths


@dataclass(frozen=True)
class Config:
    email: str
    password: str
    resume_path: Path
    headless: bool
    timeout_ms: int
    log_dir: Path

    @staticmethod
    def from_env() -> "Config":
        # Environment variables (e.g. injected by Jenkins credentials) take
        # priority. The DEFAULT_* constants above are only used as a local
        # fallback when the corresponding env var isn't set.
        email = os.environ.get("NAUKRI_EMAIL") #DEFAULT_EMAIL)
        password = os.environ.get("NAUKRI_PASSWORD")# DEFAULT_PASSWORD)
        resume_path_raw = os.environ.get("NAUKRI_RESUME_PATH") #DEFAULT_RESUME_PATH)

        missing = [
            name
            for name, val in (
                ("NAUKRI_EMAIL", email),
                ("NAUKRI_PASSWORD", password),
                ("NAUKRI_RESUME_PATH", resume_path_raw),
            )
            if not val
        ]
        if missing:
            raise EnvironmentError(
                f"Missing required environment variable(s): {', '.join(missing)}. "
                "Set them before running this script (see module docstring)."
            )

        resume_path = Path(resume_path_raw).expanduser().resolve()
        if not resume_path.is_file():
            raise FileNotFoundError(f"Resume file not found at: {resume_path}")

        allowed_ext = {".pdf", ".doc", ".docx"}
        if resume_path.suffix.lower() not in allowed_ext:
            raise ValueError(
                f"Resume file must be one of {allowed_ext}, got: {resume_path.suffix}"
            )

        headless = os.environ.get("NAUKRI_HEADLESS", "true").strip().lower() != "false"
        timeout_ms = int(os.environ.get("NAUKRI_TIMEOUT_MS", "30000"))
        log_dir = Path(os.environ.get("NAUKRI_LOG_DIR", "./logs")).expanduser().resolve()
        log_dir.mkdir(parents=True, exist_ok=True)

        return Config(
            email=email,
            password=password,
            resume_path=resume_path,
            headless=headless,
            timeout_ms=timeout_ms,
            log_dir=log_dir,
        )


# --------------------------------------------------------------------------- #
# Selectors (centralised so they're easy to update if Naukri changes its DOM)
# --------------------------------------------------------------------------- #

class NaukriSelectors:
    LOGIN_URL = "https://www.naukri.com/nlogin/login"
    PROFILE_URL = "https://www.naukri.com/mnjuser/profile"

    # Login page
    EMAIL_INPUT = "#usernameField"
    PASSWORD_INPUT = "#passwordField"
    LOGIN_BUTTON = "button[type='submit']"

    # Post-login sanity check (any element that only appears when logged in)
    LOGGED_IN_MARKER = "a[title='View Profile'], .nI-gNb-drawer__icon, header .nI-gNb-h-b1"

    # Candidate selectors for the clickable "View Profile" link/button, tried
    # in order. Kept as a list (not one combined string) because Playwright
    # doesn't allow mixing CSS and text= engines in a single comma selector.
    VIEW_PROFILE_CANDIDATES = [
        "a[title='View Profile']",
        "a:has-text('View Profile')",
        "text=/view profile/i",
    ]

    # Profile / resume section
    RESUME_UPLOAD_INPUT = "input[type='file']"
    RESUME_HEADING = "text=/resume/i"
    UPLOAD_SUCCESS_TOAST = "text=/Resume has been successfully uploaded./i"
    RESUME_LAST_UPDATED_TEXT = "text=/updated:?\\s*\\d/i"


# --------------------------------------------------------------------------- #
# Logging setup
# --------------------------------------------------------------------------- #

def setup_logging(log_dir: Path) -> logging.Logger:
    logger = logging.getLogger("naukri_uploader")
    logger.setLevel(logging.INFO)
    logger.propagate = False

    if logger.handlers:
        return logger  # avoid duplicate handlers if called twice

    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(fmt)
    logger.addHandler(console_handler)

    file_handler = RotatingFileHandler(
        log_dir / "naukri_uploader.log", maxBytes=2_000_000, backupCount=5
    )
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)

    return logger


# --------------------------------------------------------------------------- #
# Core automation class
# --------------------------------------------------------------------------- #

class NaukriResumeUploader:
    """Encapsulates the full login -> navigate -> upload -> verify workflow."""

    def __init__(self, config: Config, logger: logging.Logger):
        self.config = config
        self.logger = logger

    # ---- Public entry point ------------------------------------------------

    def run(self) -> bool:
        """Runs the full workflow. Returns True on success, False on failure."""
        with sync_playwright() as playwright:
            browser: Browser | None = None
            try:
                browser, context, page = self._launch_browser(playwright)
                self._login(page)
                self._navigate_to_profile(page)
                self._upload_resume(page)
                success = self._verify_upload(page)
                if success:
                    self.logger.info("Resume upload verified successfully.")
                else:
                    self.logger.error("Could not verify resume upload.")
                    self._save_screenshot(page, "verification_failed")
                return success

            except PlaywrightTimeoutError as exc:
                self.logger.error("Timed out waiting for an element: %s", exc)
                self._safe_screenshot(playwright, browser, "timeout_error")
                return False

            except PlaywrightError as exc:
                self.logger.error("Playwright error: %s", exc)
                self._safe_screenshot(playwright, browser, "playwright_error")
                return False

            except Exception as exc:  # noqa: BLE001 - top-level safety net
                self.logger.exception("Unexpected error during automation: %s", exc)
                self._safe_screenshot(playwright, browser, "unexpected_error")
                return False

            finally:
                if browser is not None:
                    browser.close()

    # ---- Steps ---------------------------------------------------------------

    def _launch_browser(
        self, playwright: Playwright
    ) -> tuple[Browser, BrowserContext, Page]:
        self.logger.info(
            "Launching browser (headless=%s)...", self.config.headless
        )
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": 1366, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
            ),
        )
        context.set_default_timeout(self.config.timeout_ms)
        page = context.new_page()

        # Auto-dismiss any native browser dialogs (confirm/alert/beforeunload
        # popups). Naukri occasionally shows these, and if left unhandled
        # they can stall the page or, in some Playwright/Chromium versions,
        # lead to the page/context being torn down unexpectedly.
        page.on("dialog", lambda dialog: dialog.accept())

        return browser, context, page

    def _login(self, page: Page) -> None:
        self.logger.info("Navigating to login page...")
        page.goto(NaukriSelectors.LOGIN_URL, wait_until="domcontentloaded")

        self.logger.info("Filling in credentials...")
        page.fill(NaukriSelectors.EMAIL_INPUT, self.config.email)
        page.fill(NaukriSelectors.PASSWORD_INPUT, self.config.password)

        self.logger.info("Submitting login form...")
        page.click(NaukriSelectors.LOGIN_BUTTON)

        # Wait for a marker that only appears once logged in.
        try:
            page.wait_for_selector(
                NaukriSelectors.LOGGED_IN_MARKER, timeout=self.config.timeout_ms
            )
            self.logger.info("Login successful.")
        except PlaywrightTimeoutError:
            # Check for a visible error message to give a clearer failure reason.
            error_locator = page.locator(
                "text=/invalid|incorrect|failed/i"
            )
            if error_locator.count() > 0:
                raise RuntimeError(
                    f"Login failed - site reported: {error_locator.first.inner_text()}"
                )
            raise RuntimeError(
                "Login did not complete within the timeout window; "
                "the page layout may have changed or credentials may be invalid."
            )

    def _navigate_to_profile(self, page: Page) -> None:
        self.logger.info("Navigating to profile page...")

        # Prefer clicking "View Profile" the same way a real user would --
        # some sites behave differently (session checks, lazy-loaded state,
        # analytics-gated redirects) when you jump straight to a URL versus
        # clicking through the UI. Try each candidate selector in turn.
        clicked = False
        for selector in NaukriSelectors.VIEW_PROFILE_CANDIDATES:
            try:
                view_profile = page.locator(selector).first
                view_profile.wait_for(state="visible", timeout=5_000)
                view_profile.click()
                clicked = True
                self.logger.info("Clicked 'View Profile' link (selector: %s).", selector)
                break
            except PlaywrightTimeoutError:
                continue

        if not clicked:
            self.logger.warning(
                "'View Profile' link not found/visible with any known "
                "selector; falling back to direct URL navigation."
            )
            page.goto(NaukriSelectors.PROFILE_URL, wait_until="domcontentloaded")

        try:
            page.wait_for_selector(
                NaukriSelectors.RESUME_HEADING, timeout=self.config.timeout_ms
            )
            self.logger.info("Profile page loaded.")
        except PlaywrightTimeoutError:
            self._dump_debug_artifacts(page, "profile_navigation_failed")
            raise RuntimeError(
                "Profile page did not load after clicking 'View Profile' "
                "(or the direct URL fallback). Check "
                "profile_navigation_failed_*.png/.html in the log folder "
                "to see what the page actually showed."
            )

    def _upload_resume(self, page: Page) -> None:
        self.logger.info("Uploading resume: %s", self.config.resume_path)

        # Naukri (like many sites) sometimes keeps the real <input type=file>
        # hidden until you click a visible "Update resume" link/button. Try
        # clicking one if it exists; if not, we just fall through and look
        # for the input directly. Wrapped in try/except so it never blocks
        # the flow if the trigger isn't present or already visible.
        trigger = page.locator(
            "text=/update resume|upload resume|attach resume/i"
        ).first
        try:
            if trigger.count() > 0 and trigger.is_visible():
                self.logger.info("Clicking resume upload trigger element...")
                trigger.click()
                page.wait_for_timeout(1000)
        except PlaywrightError:
            self.logger.info("No separate upload trigger found; continuing.")

        file_input = page.locator(NaukriSelectors.RESUME_UPLOAD_INPUT).first
        file_input.wait_for(state="attached", timeout=self.config.timeout_ms)
        file_input.set_input_files(str(self.config.resume_path))

        # Give the site a moment to process/upload before checking for
        # a success indicator (some sites debounce or run async validation).
        page.wait_for_timeout(3000)
        self.logger.info("Resume file submitted.")

        # Always dump artifacts right after upload -- this is the single most
        # useful moment to inspect if verification later fails, since it shows
        # exactly what the page looked like right after the file was set.
        self._dump_debug_artifacts(page, "post_upload")

    def _verify_upload(self, page: Page) -> bool:
        self.logger.info("Verifying upload...")

        # Strategy 1: look for an explicit success toast/message.
        try:
            page.wait_for_selector(
                NaukriSelectors.UPLOAD_SUCCESS_TOAST, timeout=10_000
            )
            return True
        except PlaywrightTimeoutError:
            self.logger.info("Success toast not found, trying fallback check...")

        # Strategy 2: check the "last updated" timestamp on the resume section
        # reflects today's date.
        try:
            page.wait_for_selector(
                NaukriSelectors.RESUME_LAST_UPDATED_TEXT, timeout=10_000
            )
            updated_text = page.locator(
                NaukriSelectors.RESUME_LAST_UPDATED_TEXT
            ).first.inner_text()
            self.logger.info("Resume section shows: %s", updated_text)
            today = datetime.now().strftime("%d")  # loose day-of-month check
            return today in updated_text
        except PlaywrightTimeoutError:
            self._dump_debug_artifacts(page, "verification_failed")
            return False

    # ---- Utilities -------------------------------------------------------

    def _save_screenshot(self, page: Page, label: str) -> None:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = self.config.log_dir / f"{label}_{timestamp}.png"
        try:
            page.screenshot(path=str(path), full_page=True)
            self.logger.info("Saved screenshot to %s", path)
        except PlaywrightError as exc:
            self.logger.warning("Could not save screenshot: %s", exc)

    def _dump_debug_artifacts(self, page: Page, label: str) -> None:
        """Saves a screenshot AND the raw HTML so selectors can be corrected
        by inspecting exactly what the page contained at this point in time.
        """
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self._save_screenshot(page, label)
        html_path = self.config.log_dir / f"{label}_{timestamp}.html"
        try:
            html_path.write_text(page.content(), encoding="utf-8")
            self.logger.info("Saved page HTML to %s", html_path)
        except PlaywrightError as exc:
            self.logger.warning("Could not save page HTML: %s", exc)

    def _safe_screenshot(
        self, playwright: Playwright, browser: Browser | None, label: str
    ) -> None:
        """Best-effort screenshot when we may not have a valid page reference."""
        if browser is None:
            return
        try:
            for context in browser.contexts:
                for page in context.pages:
                    self._save_screenshot(page, label)
                    return
        except Exception:  # noqa: BLE001 - screenshotting must never mask the real error
            pass


# --------------------------------------------------------------------------- #
# Retry wrapper (handles transient network/site hiccups)
# --------------------------------------------------------------------------- #

def run_with_retries(
    uploader: NaukriResumeUploader, logger: logging.Logger, max_attempts: int = 3
) -> bool:
    for attempt in range(1, max_attempts + 1):
        logger.info("=== Attempt %d of %d ===", attempt, max_attempts)
        if uploader.run():
            return True
        if attempt < max_attempts:
            backoff_seconds = 10 * attempt
            logger.warning(
                "Attempt %d failed. Retrying in %d seconds...",
                attempt,
                backoff_seconds,
            )
            time.sleep(backoff_seconds)
    logger.error("All %d attempts failed.", max_attempts)
    return False


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #

def main() -> int:
    try:
        config = Config.from_env()
    except (EnvironmentError, FileNotFoundError, ValueError) as exc:
        # Logging isn't configured yet if config loading itself failed,
        # so print directly to stderr as well.
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    logger = setup_logging(config.log_dir)
    logger.info("Starting Naukri resume auto-uploader.")
    logger.info("Resume file: %s", config.resume_path)

    uploader = NaukriResumeUploader(config, logger)
    success = run_with_retries(uploader, logger, max_attempts=3)

    if success:
        logger.info("Daily resume upload completed successfully.")
        return 0
    else:
        logger.error("Daily resume upload FAILED after retries.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
