from pathlib import Path
import time

from dotenv import load_dotenv
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

import login_session
from f5bot_supabase import (
    SupabaseRestClient,
    insert_reddit_history,
    load_dashboard_rows_with_hits,
    load_existing_reddit_urls,
)


load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
HISTORY_ROW_LIMIT = 10


def is_login_page(page) -> bool:
    if page is None or page.is_closed():
        return False

    url = page.url.lower()
    if "/login" in url:
        return True

    title = page.title().lower()
    if "f5bot login" in title:
        return True

    return page.locator("input#email").count() > 0


def should_retry(error: Exception, page) -> bool:
    error_text = str(error)
    return (
        isinstance(error, PlaywrightError)
        or "ERR_NAME_NOT_RESOLVED" in error_text
        or "ERR_CONNECTION" in error_text
        or "Session is not authenticated" in error_text
        or is_login_page(page)
    )


def close_page(page) -> None:
    if page is not None and not page.is_closed():
        page.close()


def close_context(context) -> None:
    if context is not None:
        try:
            context.close()
        except PlaywrightError:
            pass


def scrape_history_rows(dashboard: list[dict], existing_urls: set[str]) -> list[dict]:
    new_records = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        storage_state = login_session.load_storage_state()
        if not storage_state:
            print("No Supabase session found; logging in fresh.")
            storage_state = login_session.refresh_session(p)

        for dashboard_row in dashboard:
            dashboard_id = int(dashboard_row["id"])
            keyword = str(dashboard_row.get("keyword") or "").strip()
            history_url = str(dashboard_row.get("history_url") or "").strip()

            if not history_url:
                print(f"\nSkipping {keyword}: missing history_url")
                continue

            print(f"\nProcessing: {keyword}")

            context = None
            page = None

            for attempt in range(1, 4):
                try:
                    close_context(context)

                    context = browser.new_context(storage_state=storage_state)
                    page = context.new_page()

                    page.goto(
                        history_url,
                        wait_until="domcontentloaded",
                        timeout=60000,
                    )
                    page.wait_for_load_state("networkidle", timeout=15000)

                    if is_login_page(page):
                        raise RuntimeError("Session is not authenticated; login page detected")

                    page.wait_for_selector("#history tbody tr", timeout=15000)

                    rows = page.locator("#history tbody tr")
                    checked_count = min(rows.count(), HISTORY_ROW_LIMIT)
                    new_count = 0
                    existing_count = 0

                    for i in range(checked_count):
                        row = rows.nth(i)
                        cells = row.locator("td")

                        title_links = cells.nth(3).locator("a")
                        if not title_links.count():
                            continue
                        title_link = title_links.first

                        reddit_url = (title_link.get_attribute("href") or "").strip()
                        if not reddit_url:
                            continue

                        if reddit_url in existing_urls:
                            existing_count += 1
                            continue

                        existing_urls.add(reddit_url)

                        new_records.append({
                            "dashboard_id": dashboard_id,
                            "keyword": cells.nth(0).inner_text().strip() or keyword,
                            "site": cells.nth(2).inner_text().strip(),
                            "title": title_link.inner_text().strip() or reddit_url,
                            "reddit_url": reddit_url,
                            "context": cells.nth(4).inner_text().strip(),
                            "timestamp_text": cells.nth(5).inner_text().strip(),
                            "history_url": history_url,
                        })

                        new_count += 1

                    print(
                        f"  Checked top {checked_count} rows; "
                        f"added {new_count} new rows; "
                        f"skipped {existing_count} existing rows"
                    )
                    break

                except Exception as e:
                    if attempt < 3 and should_retry(e, page):
                        print(f"  Retry {attempt}/3 after error: {e}")
                        close_page(page)
                        close_context(context)
                        context = None
                        page = None
                        storage_state = login_session.refresh_session(p)
                        time.sleep(5)
                        continue

                    print(f"Error: {e}")
                    close_context(context)
                    break

            close_context(context)

        browser.close()

    return new_records


def main() -> None:
    supabase = SupabaseRestClient()
    try:
        dashboard = load_dashboard_rows_with_hits(supabase)
        print(f"Dashboard rows with hits: {len(dashboard)}")

        existing_urls = load_existing_reddit_urls(supabase)
        print(f"Already stored: {len(existing_urls)} records")

        new_records = scrape_history_rows(dashboard, existing_urls)
        inserted_count = insert_reddit_history(supabase, new_records)
    finally:
        supabase.close()

    print()
    print("=" * 50)
    print(f"New records scraped  : {len(new_records)}")
    print(f"New records inserted : {inserted_count}")
    print(f"Total known URLs     : {len(existing_urls)}")
    print("Supabase table       : reddit_history")
    print("=" * 50)


if __name__ == "__main__":
    main()
