import os
import re
import json
import requests
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
TABLE_NAME = "reddit_post_summary"

ZOHO_CLIENT_ID = os.getenv("ZOHO_CLIENT_ID")
ZOHO_CLIENT_SECRET = os.getenv("ZOHO_CLIENT_SECRET")
ZOHO_REFRESH_TOKEN = os.getenv("ZOHO_REFRESH_TOKEN")
ZOHO_RESOURCE_ID = os.getenv("ZOHO_RESOURCE_ID")
ZOHO_WORKSHEET_NAME = os.getenv("ZOHO_WORKSHEET_NAME")

REQUIRED_VARS = {
    "SUPABASE_URL": SUPABASE_URL,
    "SUPABASE_KEY": SUPABASE_KEY,
    "ZOHO_CLIENT_ID": ZOHO_CLIENT_ID,
    "ZOHO_CLIENT_SECRET": ZOHO_CLIENT_SECRET,
    "ZOHO_REFRESH_TOKEN": ZOHO_REFRESH_TOKEN,
    "ZOHO_RESOURCE_ID": ZOHO_RESOURCE_ID,
    "ZOHO_WORKSHEET_NAME": ZOHO_WORKSHEET_NAME,
}

missing = [k for k, v in REQUIRED_VARS.items() if not v]
if missing:
    raise SystemExit(f"Missing required environment variables: {', '.join(missing)}")

SUBREDDIT_RE = re.compile(r"/r/([^/]+)/")


def extract_subreddit(url):
    if not url:
        return ""
    m = SUBREDDIT_RE.search(url)
    return m.group(1) if m else ""


def format_date(iso_str):
    if not iso_str:
        return ""
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d %H:%M")
    except (ValueError, TypeError):
        return iso_str


def get_zoho_access_token():
    resp = requests.post(
        "https://accounts.zoho.in/oauth/v2/token",
        data={
            "refresh_token": ZOHO_REFRESH_TOKEN,
            "client_id": ZOHO_CLIENT_ID,
            "client_secret": ZOHO_CLIENT_SECRET,
            "grant_type": "refresh_token",
        },
    )
    if not resp.ok:
        print("Zoho token refresh failed:", resp.text)
    resp.raise_for_status()
    return resp.json()["access_token"]


def get_unsynced_rows():
    resp = requests.get(
        f"{SUPABASE_URL}/rest/v1/{TABLE_NAME}",
        headers={
            "apikey": SUPABASE_KEY,
            "Authorization": f"Bearer {SUPABASE_KEY}",
        },
        params={
            "synced_to_sheet": "eq.false",
            # Embed the related reddit_history row via the FK, to pull
            # in title + the post's original timestamp.
            "select": "*,reddit_history(title,timestamp_text)",
        },
    )
    if not resp.ok:
        print("Supabase fetch failed:", resp.text)
    resp.raise_for_status()
    return resp.json()


def map_row_to_sheet_fields(row):
    history = row.get("reddit_history") or {}
    post_date = format_date(history.get("timestamp_text"))

    return {
        "Date of post": post_date,
        "Date of comment": post_date,  # same value in both, per your call
        "Subreddit name": extract_subreddit(row.get("reddit_url")),
        "Post title": history.get("title"),
        "post URL": row.get("reddit_url"),
        "Category classification": row.get("category"),
        "confidence score": row.get("confidence"),
        "suggested response": row.get("reason"),  # see note below
    }


def push_to_zoho(access_token, rows):
    mapped_rows = [map_row_to_sheet_fields(row) for row in rows]

    payload = {
        "method": "worksheet.jsondata.append",
        "resource_id": ZOHO_RESOURCE_ID,
        "worksheet_name": ZOHO_WORKSHEET_NAME,
        "json_data": json.dumps(mapped_rows, default=str),
    }
    resp = requests.post(
        f"https://sheet.zoho.in/api/v2/{ZOHO_RESOURCE_ID}",
        headers={"Authorization": f"Zoho-oauthtoken {access_token}"},
        data=payload,
    )
    if not resp.ok:
        print("Zoho push failed:", resp.text)
    resp.raise_for_status()
    return resp.json()


def mark_as_synced(row_ids):
    resp = requests.patch(
        f"{SUPABASE_URL}/rest/v1/{TABLE_NAME}",
        headers={
            "apikey": SUPABASE_KEY,
            "Authorization": f"Bearer {SUPABASE_KEY}",
            "Content-Type": "application/json",
        },
        params={"id": f"in.({','.join(map(str, row_ids))})"},
        json={"synced_to_sheet": True},
    )
    if not resp.ok:
        print("Supabase mark-as-synced failed:", resp.text)
    resp.raise_for_status()


def main():
    rows = get_unsynced_rows()
    if not rows:
        print("Nothing new to sync.")
        return

    print(f"Found {len(rows)} unsynced row(s).")

    token = get_zoho_access_token()
    result = push_to_zoho(token, rows)
    print("Zoho response:", result)

    if result.get("status") == "success":
        mark_as_synced([r["id"] for r in rows])
        print(f"Synced {len(rows)} row(s).")
    else:
        print("Zoho did not report success — rows were NOT marked as synced. Will retry next run.")


if __name__ == "__main__":
    main()