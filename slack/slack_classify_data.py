import csv
import json
import os
from io import StringIO
from typing import Any

import requests
from dotenv import load_dotenv
from groq import Groq

load_dotenv()


MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")
ROW_LIMIT = int(os.getenv("SLACK_CLASSIFY_ROW_LIMIT", "0") or "0")
INSERT_BATCH_SIZE = max(1, int(os.getenv("SLACK_CLASSIFY_INSERT_BATCH_SIZE", "10") or "10"))

ZOHO_CLIENT_ID = os.getenv("ZOHO_CLIENT_ID")
ZOHO_CLIENT_SECRET = os.getenv("ZOHO_CLIENT_SECRET")
ZOHO_REFRESH_TOKEN = os.getenv("ZOHO_REFRESH_TOKEN")
ZOHO_RESOURCE_ID = os.getenv("ZOHO_RESOURCE_ID")
ZOHO_INPUT_WORKSHEET_NAME = os.getenv("ZOHO_WORKSHEET_NAME1") or os.getenv("ZOHO_WORKSHEET_NAME") or "Sheet2"
ZOHO_OUTPUT_WORKSHEET_NAME = (
    os.getenv("ZOHO_SLACK_OUTPUT_WORKSHEET_NAME")
    or os.getenv("ZOHO_WORKSHEET_NAME2")
    or "Sheet3"
)

INPUT_COLUMNS = [
    "subject",
    "date",
    "workspace",
    "workspace_url",
    "channel",
    "sender",
    "message_time",
    "message_number",
    "body",
    "email_uid",
]

OUTPUT_COLUMNS = INPUT_COLUMNS + [
    "category",
    "relevant",
    "confidence",
]

REQUIRED_VARS = {
    "ZOHO_CLIENT_ID": ZOHO_CLIENT_ID,
    "ZOHO_CLIENT_SECRET": ZOHO_CLIENT_SECRET,
    "ZOHO_REFRESH_TOKEN": ZOHO_REFRESH_TOKEN,
    "ZOHO_RESOURCE_ID": ZOHO_RESOURCE_ID,
    "ZOHO_INPUT_WORKSHEET_NAME": ZOHO_INPUT_WORKSHEET_NAME,
    "ZOHO_OUTPUT_WORKSHEET_NAME": ZOHO_OUTPUT_WORKSHEET_NAME,
}

missing = [k for k, v in REQUIRED_VARS.items() if not v]
if missing:
    raise SystemExit(f"Missing required environment variables: {', '.join(missing)}")


def load_groq_api_keys() -> list[str]:
    keys: list[str] = []

    primary_key = os.getenv("GROQ_API_KEY", "").strip()
    if primary_key:
        keys.append(primary_key)

    for index in range(1, 51):
        numbered_key = os.getenv(f"GROQ_API_KEY_{index}", "").strip()
        if numbered_key:
            keys.append(numbered_key)

    combined_keys = os.getenv("GROQ_API_KEYS", "")
    for raw_key in combined_keys.replace("\n", ",").split(","):
        key = raw_key.strip()
        if key:
            keys.append(key)

    deduped_keys = list(dict.fromkeys(keys))
    if not deduped_keys:
        raise RuntimeError("Set GROQ_API_KEY, GROQ_API_KEY_2, or GROQ_API_KEYS in .env.")
    return deduped_keys


def is_rate_limit_error(exc: Exception) -> bool:
    status_code = getattr(exc, "status_code", None)
    if status_code == 429:
        return True

    body = str(getattr(exc, "body", "") or "")
    message = str(exc)
    return "rate_limit" in body.lower() or "rate limit" in message.lower()


class GroqKeyRotator:
    def __init__(self, api_keys: list[str]) -> None:
        self.clients = [Groq(api_key=api_key) for api_key in api_keys]
        self.index = 0

    @property
    def count(self) -> int:
        return len(self.clients)

    def create_chat_completion(self, **kwargs: Any) -> Any:
        last_rate_limit_error: Exception | None = None

        for attempt in range(self.count):
            key_number = self.index + 1
            client = self.clients[self.index]

            try:
                if attempt:
                    print(f"  Retrying with Groq API key #{key_number}")
                return client.chat.completions.create(**kwargs)
            except Exception as exc:
                if not is_rate_limit_error(exc):
                    raise

                last_rate_limit_error = exc
                print(f"  Groq API key #{key_number} hit rate limit.")
                self.index = (self.index + 1) % self.count

        raise RuntimeError(f"All {self.count} Groq API key(s) hit rate limits.") from last_rate_limit_error


CATEGORIES = [
    "Job seeker in sustainability",
    "Job search advice",
    "Job board recommendations",
    "Struggling with job descriptions",
    "Career transition to sustainability",
    "Career path guidance in sustainability",
    "Getting started in sustainability (beginner)",
    "Seeking ESG/sustainability/LCA consultant",
    "Consultant seeking help/advice",
    "Software or database question (ESG, LCA, sustainability)",
    "Grants, funding, or accelerators",
    "Self-promotion",
    "Software selling",
    "Consultancy promotion",
    "Long-form content dump",
    "News article",
    "Political discussion",
    "Other",
]

SYSTEM_PROMPT = f"""You are an expert at reading Slack messages from sustainability/ESG-related Slack communities and identifying the PRIMARY intent behind the message.

Pick exactly ONE category from this list (use the exact string):
{chr(10).join(f"- {c}" for c in CATEGORIES)}

Guidelines:
- Focus on what the sender actually WANTS or is trying to DO, not just the topic they mention.
- If a message touches multiple things, pick the one that best matches the main ask or goal.
- Slack messages are often short, conversational, or mid-thread. Classify based on the text given - do not assume missing context. If a message is too short or vague to classify with confidence (e.g. "thanks!", "same here", a single emoji, "+1", "bump"), use "Other" and set relevant to false.
- "Self-promotion", "Software selling", and "Consultancy promotion" apply when the sender is marketing themselves, a product, or a company - not when they are genuinely asking for help or advice.
- "Long-form content dump" = an essay, rant, or story with no clear question or ask.
- Mark "relevant" as true only if a sustainability/ESG consulting company would plausibly want to see or respond to this message (e.g. a potential client, job seeker, or genuine industry discussion). Mark it false for unrelated news, politics, spam, bot/system notifications (channel joins, topic changes, integrations posting), plain acknowledgments, or thread replies that add no new ask.

Respond with ONLY valid JSON in this exact shape, nothing else:
{{
  "category": "<one of the categories above, exact string>",
  "relevant": true/false,
  "confidence": <integer 0-100>
}}
"""


def get_zoho_access_token() -> str:
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


def fetch_zoho_records(access_token: str, worksheet_name: str) -> list[dict[str, Any]]:
    payload = {
        "method": "worksheet.records.fetch",
        "resource_id": ZOHO_RESOURCE_ID,
        "worksheet_name": worksheet_name,
        "header_row": 1,
    }
    resp = requests.post(
        f"https://sheet.zoho.in/api/v2/{ZOHO_RESOURCE_ID}",
        headers={"Authorization": f"Zoho-oauthtoken {access_token}"},
        data=payload,
    )
    if not resp.ok:
        error = zoho_error(resp)
        if error.get("error_code") == 2893:
            return []
        print(f"Zoho fetch failed for {worksheet_name}:", resp.text)
    resp.raise_for_status()

    data = resp.json()
    records = data.get("records") or data.get("data") or []
    if isinstance(records, dict):
        records = records.get("records") or records.get("data") or []
    return records if isinstance(records, list) else []


def sheet_value(row: dict[str, Any], column: str) -> str:
    value = row.get(column, "")
    return "" if value is None else str(value).strip()


def row_key(row: dict[str, Any]) -> str:
    message_url = sheet_value(row, "workspace_url")
    if message_url:
        return message_url

    email_uid = sheet_value(row, "email_uid")
    message_number = sheet_value(row, "message_number")
    if email_uid and message_number:
        return f"{email_uid}:{message_number}"

    return "|".join(sheet_value(row, column) for column in INPUT_COLUMNS)


def load_pending_rows(
    input_rows: list[dict[str, Any]],
    output_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    existing_keys = {row_key(row) for row in output_rows if row_key(row)}

    pending_rows = []
    for row in input_rows:
        body = sheet_value(row, "body")
        if not body:
            continue

        key = row_key(row)
        if not key or key in existing_keys:
            continue

        pending_rows.append(row)

    if ROW_LIMIT > 0:
        return pending_rows[:ROW_LIMIT]

    return pending_rows


def classify(row: dict[str, Any], groq_rotator: GroqKeyRotator) -> dict[str, Any]:
    user_prompt = "\n".join([
        f"Workspace: {sheet_value(row, 'workspace')}",
        f"Channel: {sheet_value(row, 'channel')}",
        f"Sender: {sheet_value(row, 'sender')}",
        f"Message time: {sheet_value(row, 'message_time')}",
        f"Message: {sheet_value(row, 'body')}",
    ])

    response = groq_rotator.create_chat_completion(
        model=MODEL,
        temperature=0,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
    )

    raw = response.choices[0].message.content
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"error": "Failed to parse model output", "raw": raw}


def to_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1", "yes", "y"}
    return bool(value)


def normalize_classification(result: dict[str, Any]) -> dict[str, Any]:
    if "error" in result:
        raise ValueError(str(result["error"]))

    category = str(result.get("category") or "").strip()
    if category not in CATEGORIES:
        category = "Other"

    try:
        confidence = int(result.get("confidence", 0))
    except (TypeError, ValueError):
        confidence = 0

    return {
        "category": category,
        "relevant": to_bool(result.get("relevant")),
        "confidence": max(0, min(100, confidence)),
    }


def build_output_row(
    input_row: dict[str, Any],
    classification: dict[str, Any],
) -> dict[str, Any]:
    output = {column: sheet_value(input_row, column) for column in INPUT_COLUMNS}
    output.update({
        "category": classification["category"],
        "relevant": str(classification["relevant"]).lower(),
        "confidence": classification["confidence"],
    })
    return output


def zoho_error(resp: requests.Response) -> dict[str, Any]:
    try:
        return resp.json()
    except ValueError:
        return {}


def rows_to_csv(rows: list[dict[str, Any]], include_headers: bool = False) -> str:
    output = StringIO()
    writer = csv.writer(output, lineterminator="\n")
    if include_headers:
        writer.writerow(OUTPUT_COLUMNS)
    for row in rows:
        writer.writerow([row.get(column, "") for column in OUTPUT_COLUMNS])
    return output.getvalue()


def append_csv_rows_to_zoho(
    access_token: str,
    worksheet_name: str,
    rows: list[dict[str, Any]],
    include_headers: bool = False,
) -> dict[str, Any]:
    payload = {
        "method": "worksheet.csvdata.append",
        "resource_id": ZOHO_RESOURCE_ID,
        "worksheet_name": worksheet_name,
        "csv_data": rows_to_csv(rows, include_headers=include_headers),
    }
    resp = requests.post(
        f"https://sheet.zoho.in/api/v2/{ZOHO_RESOURCE_ID}",
        headers={"Authorization": f"Zoho-oauthtoken {access_token}"},
        data=payload,
    )
    if not resp.ok:
        print(f"Zoho CSV append failed for {worksheet_name}:", resp.text)
    resp.raise_for_status()
    return resp.json()


def append_rows_to_zoho(
    access_token: str,
    worksheet_name: str,
    rows: list[dict[str, Any]],
) -> dict[str, Any] | None:
    if not rows:
        return None

    payload = {
        "method": "worksheet.jsondata.append",
        "resource_id": ZOHO_RESOURCE_ID,
        "worksheet_name": worksheet_name,
        "header_row": 1,
        "json_data": json.dumps(rows, default=str),
    }
    resp = requests.post(
        f"https://sheet.zoho.in/api/v2/{ZOHO_RESOURCE_ID}",
        headers={"Authorization": f"Zoho-oauthtoken {access_token}"},
        data=payload,
    )
    if not resp.ok:
        error = zoho_error(resp)
        if error.get("error_code") == 2893:
            return append_csv_rows_to_zoho(access_token, worksheet_name, rows, include_headers=True)
        print(f"Zoho append failed for {worksheet_name}:", resp.text)
    resp.raise_for_status()
    return resp.json()


def main() -> None:
    groq_rotator = GroqKeyRotator(load_groq_api_keys())
    print(f"Loaded {groq_rotator.count} Groq API key(s).")

    access_token = get_zoho_access_token()
    input_rows = fetch_zoho_records(access_token, ZOHO_INPUT_WORKSHEET_NAME)
    output_rows = fetch_zoho_records(access_token, ZOHO_OUTPUT_WORKSHEET_NAME)
    pending_rows = load_pending_rows(input_rows, output_rows)

    print(f"Input worksheet : {ZOHO_INPUT_WORKSHEET_NAME}")
    print(f"Output worksheet: {ZOHO_OUTPUT_WORKSHEET_NAME}")
    print(f"Input rows      : {len(input_rows)}")
    print(f"Already written : {len(output_rows)}")
    print(f"Pending classify: {len(pending_rows)}")

    insert_buffer: list[dict[str, Any]] = []
    classified_count = 0
    inserted_count = 0
    failed = 0

    for index, row in enumerate(pending_rows, start=1):
        message_url = sheet_value(row, "workspace_url")
        try:
            result = classify(row, groq_rotator)
            classification = normalize_classification(result)
            insert_buffer.append(build_output_row(row, classification))
            classified_count += 1

            print(
                f"[{index}/{len(pending_rows)}] "
                f"{classification['category']} "
                f"({classification['confidence']}%) - {message_url}"
            )

            if len(insert_buffer) >= INSERT_BATCH_SIZE:
                append_rows_to_zoho(access_token, ZOHO_OUTPUT_WORKSHEET_NAME, insert_buffer)
                inserted_count += len(insert_buffer)
                print(f"  Appended batch: {len(insert_buffer)} rows")
                insert_buffer.clear()
        except Exception as exc:
            failed += 1
            print(f"[{index}/{len(pending_rows)}] Failed - {message_url}: {exc}")

    if insert_buffer:
        append_rows_to_zoho(access_token, ZOHO_OUTPUT_WORKSHEET_NAME, insert_buffer)
        inserted_count += len(insert_buffer)
        print(f"  Appended final batch: {len(insert_buffer)} rows")
        insert_buffer.clear()

    print()
    print("=" * 50)
    print(f"Rows classified : {classified_count}")
    print(f"Rows inserted   : {inserted_count}")
    print(f"Rows failed     : {failed}")
    print(f"Zoho worksheet  : {ZOHO_OUTPUT_WORKSHEET_NAME}")
    print("=" * 50)


if __name__ == "__main__":
    main()
