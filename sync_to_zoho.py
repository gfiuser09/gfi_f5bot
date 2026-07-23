import os
import requests
from dotenv import load_dotenv

load_dotenv()

ZOHO_CLIENT_ID = os.getenv("ZOHO_CLIENT_ID")
ZOHO_CLIENT_SECRET = os.getenv("ZOHO_CLIENT_SECRET")
ZOHO_REFRESH_TOKEN = os.getenv("ZOHO_REFRESH_TOKEN")
ZOHO_RESOURCE_ID = os.getenv("ZOHO_RESOURCE_ID")
ZOHO_WORKSHEET_NAME = os.getenv("ZOHO_WORKSHEET_NAME")


def get_access_token():
    r = requests.post(
        "https://accounts.zoho.in/oauth/v2/token",
        data={
            "refresh_token": ZOHO_REFRESH_TOKEN,
            "client_id": ZOHO_CLIENT_ID,
            "client_secret": ZOHO_CLIENT_SECRET,
            "grant_type": "refresh_token",
        },
    )
    r.raise_for_status()
    return r.json()["access_token"]


token = get_access_token()

headers = {
    "Authorization": f"Zoho-oauthtoken {token}"
}

# Read all rows
params = {
    "method": "worksheet.records.fetch",
    "worksheet_name": ZOHO_WORKSHEET_NAME,
}

r = requests.get(
    f"https://sheet.zoho.in/api/v2/{ZOHO_RESOURCE_ID}",
    headers=headers,
    params=params,
)
r.raise_for_status()

rows = r.json()["rows"]

print("Rows:", len(rows))

for row in rows:

    value = row.get("Date of post")

    if not value:
        continue

    new_value = value[:10]

    if value == new_value:
        continue

    payload = {
        "method": "worksheet.records.update",
        "worksheet_name": ZOHO_WORKSHEET_NAME,
        "criteria": f'ROWID="{row["ROWID"]}"',
        "record_data": f'{{"Date of post":"{new_value}"}}',
    }

    res = requests.post(
        f"https://sheet.zoho.in/api/v2/{ZOHO_RESOURCE_ID}",
        headers=headers,
        data=payload,
    )

    print(row["ROWID"], res.json())

print("Done")
