import os
import sys
import time
from typing import Any

from dotenv import load_dotenv
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

from f5bot_supabase import (
    SupabaseRestClient,
    load_f5bot_storage_state,
    save_f5bot_storage_state,
)


load_dotenv()

LOGIN_URL = "https://f5bot.com/login"
DASHBOARD_URL = os.getenv("F5BOT_DASHBOARD_URL", "https://f5bot.com/dash")

MAX_RETRIES = 3
RETRY_DELAY_SECONDS = 10


def get_credentials() -> tuple[str, str]:
    email = os.getenv("F5BOT_EMAIL", "")
    password = os.getenv("F5BOT_PASSWORD", "")

    if not email or not password:
        raise RuntimeError("Set F5BOT_EMAIL and F5BOT_PASSWORD in .env.")

    return email, password


def goto_with_retry(page, url, **kwargs) -> None:
    last_error = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            page.goto(url, **kwargs)
            return
        except PlaywrightError as e:
            last_error = e
            print(f"Navigation attempt {attempt}/{MAX_RETRIES} to {url} failed: {e}")
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY_SECONDS)
    raise last_error


def load_storage_state() -> dict[str, Any] | None:
    supabase = SupabaseRestClient()
    try:
        return load_f5bot_storage_state(supabase)
    finally:
        supabase.close()


def save_storage_state(storage_state: dict[str, Any]) -> int:
    supabase = SupabaseRestClient()
    try:
        return save_f5bot_storage_state(supabase, storage_state)
    finally:
        supabase.close()


def is_login_page(page) -> bool:
    if "/login" in page.url.lower():
        return True

    title = page.title().lower()
    if "f5bot login" in title:
        return True

    return page.locator("input#email").count() > 0


def fresh_login(playwright) -> dict[str, Any]:
    email, password = get_credentials()

    browser = playwright.chromium.launch(headless=True)
    context = browser.new_context()
    page = context.new_page()

    try:
        goto_with_retry(page, LOGIN_URL, wait_until="domcontentloaded", timeout=60000)
        page.locator("#email").fill(email)
        page.locator("#password").fill(password)
        page.locator("//button[@type='submit']").click()
        page.wait_for_load_state("networkidle", timeout=30000)

        if is_login_page(page):
            raise RuntimeError(f"Login failed, still on {page.url}")

        storage_state = context.storage_state()
        row_id = save_storage_state(storage_state)
        print(f"Logged in fresh at {page.url}; session saved to f5bot_dashboard.id={row_id}")
        return storage_state
    finally:
        browser.close()


def try_saved_session(playwright, storage_state: dict[str, Any]) -> bool:
    browser = playwright.chromium.launch(headless=True)
    context = browser.new_context(storage_state=storage_state)
    page = context.new_page()

    try:
        goto_with_retry(page, DASHBOARD_URL, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_load_state("networkidle", timeout=30000)

        ok = not is_login_page(page)
        if ok:
            print(f"Visited {page.url} using Supabase session.")
        else:
            print("Supabase session expired; falling back to fresh login.")
        return ok
    finally:
        browser.close()


def ensure_session(playwright) -> dict[str, Any]:
    storage_state = load_storage_state()
    if storage_state and try_saved_session(playwright, storage_state):
        return storage_state

    return fresh_login(playwright)


def refresh_session(playwright) -> dict[str, Any]:
    return fresh_login(playwright)


def main() -> None:
    try:
        with sync_playwright() as p:
            ensure_session(p)
    except Exception as exc:
        print(f"ERROR: {exc}")
        sys.exit(1)


if __name__ == "__main__":
    main()
