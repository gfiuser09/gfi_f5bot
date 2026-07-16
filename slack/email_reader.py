import csv
import os
import re
import json
import imaplib
import email
import requests
from io import StringIO
from email.header import decode_header
from email.utils import parseaddr
from dotenv import load_dotenv

load_dotenv()

EMAIL = os.getenv("Email_id")
APP_PASSWORD = os.getenv("APP_PASSWORD")
SENDER = os.getenv("SENDER_EMAIL")

ZOHO_CLIENT_ID = os.getenv("ZOHO_CLIENT_ID")
ZOHO_CLIENT_SECRET = os.getenv("ZOHO_CLIENT_SECRET")
ZOHO_REFRESH_TOKEN = os.getenv("ZOHO_REFRESH_TOKEN")
ZOHO_RESOURCE_ID = os.getenv("ZOHO_RESOURCE_ID")
ZOHO_WORKSHEET_NAME = os.getenv("ZOHO_WORKSHEET_NAME1")

SHEET_DATA_COLUMNS = [
    "subject",
    "date",
    "workspace",
    "workspace_url",
    "channel",
    "sender",
    "message_time",
    "message_number",
    "body",
]
SHEET_COLUMNS = SHEET_DATA_COLUMNS + ["email_uid"]

REQUIRED_VARS = {
    "Email_id": EMAIL,
    "APP_PASSWORD": APP_PASSWORD,
    "SENDER_EMAIL": SENDER,
    "ZOHO_CLIENT_ID": ZOHO_CLIENT_ID,
    "ZOHO_CLIENT_SECRET": ZOHO_CLIENT_SECRET,
    "ZOHO_REFRESH_TOKEN": ZOHO_REFRESH_TOKEN,
    "ZOHO_RESOURCE_ID": ZOHO_RESOURCE_ID,
    "ZOHO_WORKSHEET_NAME": ZOHO_WORKSHEET_NAME,
}

missing = [k for k, v in REQUIRED_VARS.items() if not v]
if missing:
    raise SystemExit(f"Missing required environment variables: {', '.join(missing)}")


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


def fetch_zoho_records(access_token):
    payload = {
        "method": "worksheet.records.fetch",
        "resource_id": ZOHO_RESOURCE_ID,
        "worksheet_name": ZOHO_WORKSHEET_NAME,
        "header_row": 1,
    }
    resp = requests.post(
        f"https://sheet.zoho.in/api/v2/{ZOHO_RESOURCE_ID}",
        headers={"Authorization": f"Zoho-oauthtoken {access_token}"},
        data=payload,
    )
    if not resp.ok:
        print("Zoho fetch failed:", resp.text)
    resp.raise_for_status()

    data = resp.json()
    records = data.get("records") or data.get("data") or []
    if isinstance(records, dict):
        records = records.get("records") or records.get("data") or []
    return records if isinstance(records, list) else []


def _sheet_value(record, column):
    value = record.get(column, "")
    return "" if value is None else str(value).strip()


def load_existing_sheet_state(access_token):
    keys = set()
    processed_uids = set()

    for record in fetch_zoho_records(access_token):
        if not isinstance(record, dict):
            continue

        key = tuple(_sheet_value(record, column) for column in SHEET_DATA_COLUMNS)
        if any(key):
            keys.add(key)

        email_uid = _sheet_value(record, "email_uid")
        if email_uid.isdigit():
            processed_uids.add(int(email_uid))

    return keys, processed_uids


def append_rows_to_zoho(access_token, rows):
    if not rows:
        return None

    payload = {
        "method": "worksheet.jsondata.append",
        "resource_id": ZOHO_RESOURCE_ID,
        "worksheet_name": ZOHO_WORKSHEET_NAME,
        "header_row": 1,
        "json_data": json.dumps(rows, default=str),
    }
    resp = requests.post(
        f"https://sheet.zoho.in/api/v2/{ZOHO_RESOURCE_ID}",
        headers={"Authorization": f"Zoho-oauthtoken {access_token}"},
        data=payload,
    )
    if not resp.ok:
        error = _zoho_error(resp)
        if error.get("error_code") == 2893:
            return append_csv_rows_to_zoho(access_token, rows)
        print("Zoho append failed:", resp.text)
    resp.raise_for_status()
    return resp.json()


def _zoho_error(resp):
    try:
        return resp.json()
    except ValueError:
        return {}


def _rows_to_csv(rows, include_headers=False):
    output = StringIO()
    writer = csv.writer(output, lineterminator="\n")
    if include_headers:
        writer.writerow(SHEET_COLUMNS)
    for row in rows:
        writer.writerow([row.get(column, "") for column in SHEET_COLUMNS])
    return output.getvalue()


def append_csv_rows_to_zoho(access_token, rows, include_headers=False):
    payload = {
        "method": "worksheet.csvdata.append",
        "resource_id": ZOHO_RESOURCE_ID,
        "worksheet_name": ZOHO_WORKSHEET_NAME,
        "csv_data": _rows_to_csv(rows, include_headers=include_headers),
    }
    resp = requests.post(
        f"https://sheet.zoho.in/api/v2/{ZOHO_RESOURCE_ID}",
        headers={"Authorization": f"Zoho-oauthtoken {access_token}"},
        data=payload,
    )
    if not resp.ok:
        print("Zoho CSV append failed:", resp.text)
    resp.raise_for_status()
    return resp.json()

def decode_payload(payload, declared_charset):
    candidates = []
    if declared_charset:
        candidates.append(declared_charset)
    candidates += ["utf-8", "windows-1252", "latin-1"]

    for charset in candidates:
        try:
            return payload.decode(charset)
        except (UnicodeDecodeError, LookupError):
            continue
    return payload.decode("utf-8", errors="replace")


def fix_mojibake(text):
    if not text or ("â€" not in text and "Ã" not in text):
        return text
    try:
        return text.encode("cp1252").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return text

_WORKSPACE_RE = re.compile(
    r"You have\b.*?\bfrom the\s+(?P<workspace>.+?)\s+workspace\s*"
    r"\(\s*(?P<url>[^)]+?)\s*\)",
    re.IGNORECASE | re.DOTALL,
)

_FOOTER_START_RE = re.compile(r"\*\s*\*\s*\*")

_CHANNEL_BLOCK_SPLIT_RE = re.compile(r"\n\s*-{2,}\s*\n")
_CHANNEL_BLOCK_SPLIT_FALLBACK_RE = re.compile(r"-{2,}")

_CHANNEL_HEADER_RE = re.compile(r"^#([\w-]+)\s*$", re.MULTILINE)
_ARCHIVE_LINK_LINE_RE = re.compile(
    r"^View in the archives:\s*(?P<url>https?://\S+)\s*$",
    re.IGNORECASE | re.MULTILINE,
)

_SENDER_HEADER_RE = re.compile(
    r"^(?P<name>[A-Za-z][\w .'\u2019-]*?)\s*"
    r"\(\s*(?P<time>\d{1,2}:\d{2}\s*[APap][Mm])\s*,\s*"
    r"(?P<date>[A-Za-z]+\s+\d{1,2}(?:st|nd|rd|th)?)\s*\)\s*$",
    re.MULTILINE,
)

_MESSAGE_NOISE_LINE_RE = re.compile(
    r"^(open in slack|reply|view thread|reply in thread"
    r"|\d+\s+repl(y|ies)|react(ed|ion)?s?)$",
    re.IGNORECASE,
)

_ATTACHMENT_LINE_RE = re.compile(
    r"^[\w.\-]+\.(png|jpe?g|gif|webp|bmp|svg|pdf|mp4|mov)\s*:\s*https?://\S+$",
    re.IGNORECASE,
)
_EMOJI_SHORTCODE_RE = re.compile(r":[a-zA-Z0-9_+\-]+:")
_URL_TOKEN_RE = re.compile(
    r"(?:https?://\S+)|(?:\b(?:www\.)?[\w-]+(?:\.[\w-]+)+/\S*)", re.IGNORECASE
)

_MIN_MESSAGE_CHARS = 5
_MIN_MESSAGE_WORDS = 1


def _looks_like_message(text):
    return len(text) >= _MIN_MESSAGE_CHARS and len(text.split()) >= _MIN_MESSAGE_WORDS


def _clean_message_text(raw_block):
    lines = [l.strip() for l in raw_block.splitlines()]
    lines = [l for l in lines if l]
    lines = [l for l in lines if not _MESSAGE_NOISE_LINE_RE.match(l)]
    lines = [l for l in lines if not _ATTACHMENT_LINE_RE.match(l)]

    text = " ".join(lines)
    text = _EMOJI_SHORTCODE_RE.sub("", text)
    text = _URL_TOKEN_RE.sub("", text)
    text = re.sub(r"\s+", " ", text).strip()
    text = fix_mojibake(text)
    return text


def _extract_channel_and_messages(block):
    channel_match = _CHANNEL_HEADER_RE.search(block)
    channel = channel_match.group(1).strip() if channel_match else ""

    archive_match = _ARCHIVE_LINK_LINE_RE.search(block)
    message_url = archive_match.group("url").strip() if archive_match else ""

    remainder = _CHANNEL_HEADER_RE.sub("", block, count=1)
    remainder = _ARCHIVE_LINK_LINE_RE.sub("", remainder)

    headers = list(_SENDER_HEADER_RE.finditer(remainder))
    messages = []
    for i, header in enumerate(headers):
        start = header.end()
        end = headers[i + 1].start() if i + 1 < len(headers) else len(remainder)
        text = _clean_message_text(remainder[start:end])
        if _looks_like_message(text):
            messages.append({
                "sender": fix_mojibake(header.group("name").strip()),
                "time": f'{header.group("time").strip()}, {header.group("date").strip()}',
                "body": text,
                "url": message_url,
            })

    return channel, messages


def parse_slack_email(body):
    workspace = ""
    workspace_url = ""

    wmatch = _WORKSPACE_RE.search(body)
    content_start = 0
    if wmatch:
        workspace = fix_mojibake(wmatch.group("workspace").strip())
        workspace_url = wmatch.group("url").strip()
        content_start = wmatch.end()

    footer_match = _FOOTER_START_RE.search(body, content_start)
    content_end = footer_match.start() if footer_match else len(body)

    content = body[content_start:content_end]

    blocks = _CHANNEL_BLOCK_SPLIT_RE.split(content)
    if len(blocks) == 1:
        blocks = _CHANNEL_BLOCK_SPLIT_FALLBACK_RE.split(content)

    all_messages = []
    for block in blocks:
        if not block.strip():
            continue
        channel, messages = _extract_channel_and_messages(block)
        for message in messages:
            message["channel"] = channel
            all_messages.append(message)

    return workspace, workspace_url, all_messages

access_token = get_zoho_access_token()
existing_row_keys, processed_uids = load_existing_sheet_state(access_token)
last_uid = max(processed_uids) if processed_uids else 0
rows_to_append = []

# Connect to Gmail
mail = imaplib.IMAP4_SSL("imap.gmail.com")
mail.login(EMAIL, APP_PASSWORD)
mail.select("INBOX")

search_filter = (
    f'(UID {last_uid + 1}:* FROM "{SENDER}")'
    if last_uid
    else f'(FROM "{SENDER}")'
)

if last_uid:
    print(f"Last processed UID from Zoho Sheet: {last_uid}")

status, data = mail.uid(
    "search",
    None,
    search_filter
)

if status != "OK":
    print("Failed to search emails.")
    mail.logout()
    exit()

uids = data[0].split()

if last_uid:
    print(f"Found {len(uids)} new email(s)")
else:
    print(f"Found {len(uids)} matching email(s)")

for uid in uids:
    uid_text = uid.decode()
    status, msg_data = mail.uid("fetch", uid, "(BODY.PEEK[])")

    if status != "OK":
        continue

    raw_email = None
    for part in msg_data:
        if isinstance(part, tuple):
            raw_email = part[1]
            break

    if raw_email is None:
        continue

    msg = email.message_from_bytes(raw_email)
    from_email = parseaddr(msg.get("From"))[1].lower()
    if from_email != SENDER.lower():
        continue
    subject, encoding = decode_header(msg["Subject"])[0]
    if isinstance(subject, bytes):
        subject = subject.decode(encoding or "utf-8", errors="replace")
    subject = fix_mojibake(subject)

    # Date
    date = msg["Date"]
    body = ""

    if msg.is_multipart():
        for part in msg.walk():
            if (
                part.get_content_type() == "text/plain"
                and "attachment" not in str(part.get("Content-Disposition"))
            ):
                payload = part.get_payload(decode=True)
                if payload:
                    body = decode_payload(payload, part.get_content_charset())
                    break
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            body = decode_payload(payload, msg.get_content_charset())
    workspace, workspace_url, messages_in_email = parse_slack_email(body)

    if not messages_in_email:
        print("=" * 80)
        print(f"UID          : {uid_text}")
        print(f"Subject      : {subject}")
        print("WARNING: could not parse any messages from this email")
        continue

    new_rows = []
    for i, message in enumerate(messages_in_email, start=1):
        row = {
            "subject": subject,
            "date": date,
            "workspace": workspace,
            "workspace_url": message.get("url") or workspace_url,
            "channel": message["channel"],
            "sender": message["sender"],
            "message_time": message["time"],
            "message_number": str(i),
            "body": message["body"],
            "email_uid": uid_text,
        }
        key = tuple(row[column] for column in SHEET_DATA_COLUMNS)
        if key in existing_row_keys:
            continue
        existing_row_keys.add(key)
        rows_to_append.append(row)
        new_rows.append(row)

    print("=" * 80)
    print(f"UID          : {uid_text}")
    print(f"Subject      : {subject}")
    print(f"Date         : {date}")
    print(f"Workspace    : {workspace}")
    print(f"Workspace URL: {workspace_url}")
    print(f"Messages     : {len(messages_in_email)} ({len(new_rows)} new)")
    print("Queued for Zoho Sheet" if new_rows else "Already in Zoho Sheet")

if rows_to_append:
    result = append_rows_to_zoho(access_token, rows_to_append)
    print(f"\nAppended {len(rows_to_append)} row(s) to Zoho Sheet.")
    print("Zoho response:", result)
else:
    print("\nNothing new to append to Zoho Sheet.")

mail.logout()

print("\nDone.")
