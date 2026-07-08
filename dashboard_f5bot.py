from pathlib import Path
import os
import re
import time
from urllib.parse import parse_qs, urljoin, urlparse

from dotenv import load_dotenv
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

import login_session
from f5bot_supabase import SupabaseRestClient, sync_dashboard_rows


load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
DASHBOARD_URL = os.getenv("F5BOT_DASHBOARD_URL", "https://f5bot.com/dash")
MAX_ATTEMPTS = 3


def is_login_page(page) -> bool:
    if page is None or page.is_closed():
        return False

    if "/login" in page.url.lower():
        return True

    title = page.title().lower()
    if "f5bot login" in title:
        return True

    return page.locator("input#email").count() > 0


def close_context(context) -> None:
    if context is not None:
        try:
            context.close()
        except PlaywrightError:
            pass


def goto_dashboard(page) -> None:
    page.goto(DASHBOARD_URL, wait_until="domcontentloaded", timeout=60000)
    try:
        page.wait_for_load_state("networkidle", timeout=15000)
    except PlaywrightTimeoutError:
        pass


def click_hits_header_if_available(page) -> None:
    header = page.locator(
        "th",
        has_text=re.compile(r"Hits\s+last\s+7\s+days", re.IGNORECASE),
    )

    if not header.count():
        print("Hits header not found; continuing without sorting.")
        return

    try:
        header.first.click(timeout=5000)
    except PlaywrightTimeoutError:
        print("Hits header was found but not clickable; continuing without sorting.")


def wait_for_alert_rows(page) -> None:
    try:
        page.wait_for_selector("#alerts tbody tr", timeout=15000)
    except PlaywrightTimeoutError as exc:
        screenshot_path = BASE_DIR / "dashboard_unexpected.png"
        page.screenshot(path=str(screenshot_path), full_page=True)
        raise RuntimeError(
            "Could not find the F5Bot alerts table. "
            f"Current URL: {page.url}. "
            f"Title: {page.title()}. "
            f"Screenshot: {screenshot_path}"
        ) from exc


def collect_dashboard_rows(page) -> list[dict[str, str]]:
    rows = []
    alert_rows = page.locator("#alerts tbody tr")

    for index in range(alert_rows.count()):
        row = alert_rows.nth(index)
        cells = row.locator("td")

        if cells.count() < 4:
            continue

        keyword_links = cells.nth(0).locator("a")
        if not keyword_links.count():
            continue

        keyword_link = keyword_links.first
        keyword = keyword_link.inner_text().strip()

        edit_href = keyword_link.get_attribute("href") or ""
        edit_url = urljoin(DASHBOARD_URL, edit_href)
        alert_id = parse_qs(urlparse(edit_url).query).get("alert_id", [""])[0]

        flags = cells.nth(1).inner_text().strip()
        history_cell = cells.nth(3)
        hits = history_cell.inner_text().strip()

        history_links = history_cell.locator("a")
        history_url = ""
        if history_links.count():
            history_href = history_links.first.get_attribute("href") or ""
            history_url = urljoin(DASHBOARD_URL, history_href)

        rows.append({
            "alert_id": alert_id,
            "keyword": keyword,
            "flags": flags,
            "hits_last_7_days": hits,
            "history_url": history_url,
        })

    return rows


def should_retry(error: Exception, page) -> bool:
    error_text = str(error)
    return (
        isinstance(error, PlaywrightError)
        or "Session is not authenticated" in error_text
        or "Could not find the F5Bot alerts table" in error_text
        or is_login_page(page)
    )


def scrape_dashboard_rows() -> list[dict[str, str]]:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            storage_state = login_session.load_storage_state()
            if not storage_state:
                print("No Supabase session found; logging in fresh.")
                storage_state = login_session.refresh_session(p)

            for attempt in range(1, MAX_ATTEMPTS + 1):
                context = None
                page = None

                try:
                    context = browser.new_context(storage_state=storage_state)
                    page = context.new_page()
                    goto_dashboard(page)

                    if is_login_page(page):
                        raise RuntimeError("Session is not authenticated; login page detected")

                    click_hits_header_if_available(page)
                    wait_for_alert_rows(page)
                    return collect_dashboard_rows(page)

                except Exception as exc:
                    close_context(context)
                    if attempt < MAX_ATTEMPTS and should_retry(exc, page):
                        print(f"Retry {attempt}/{MAX_ATTEMPTS} after error: {exc}")
                        storage_state = login_session.refresh_session(p)
                        time.sleep(5)
                        continue
                    raise

                finally:
                    close_context(context)

        finally:
            browser.close()


def main() -> None:
    rows = scrape_dashboard_rows()

    supabase = SupabaseRestClient()
    try:
        inserted, updated = sync_dashboard_rows(supabase, rows)
    finally:
        supabase.close()

    print(f"Synced {len(rows)} dashboard rows to Supabase")
    print(f"Inserted: {inserted}")
    print(f"Updated : {updated}")


if __name__ == "__main__":
    main()
