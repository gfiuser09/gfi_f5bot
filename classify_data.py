import os
import json
from typing import Any

from dotenv import load_dotenv
from groq import Groq

from f5bot_supabase import SupabaseRestClient, chunked

load_dotenv()


MODEL = os.getenv("GROQ_MODEL") or "openai/gpt-oss-120b"
ROW_LIMIT = int(os.getenv("CLASSIFY_ROW_LIMIT", "0") or "0")
INSERT_BATCH_SIZE = max(1, int(os.getenv("CLASSIFY_INSERT_BATCH_SIZE", "10") or "10"))


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
        raise RuntimeError(
            "Set GROQ_API_KEY, GROQ_API_KEY_2, or GROQ_API_KEYS in .env."
        )
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

        raise RuntimeError(
            f"All {self.count} Groq API key(s) hit rate limits."
        ) from last_rate_limit_error


groq_rotator = GroqKeyRotator(load_groq_api_keys())
print(f"Loaded {groq_rotator.count} Groq API key(s).")

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

SYSTEM_PROMPT = f"""You are an expert at reading Reddit posts from sustainability/ESG-related subreddits and identifying the PRIMARY intent behind the post.

Pick exactly ONE category from this list (use the exact string):
{chr(10).join(f"- {c}" for c in CATEGORIES)}

Guidelines:
- Focus on what the person actually WANTS or is trying to DO, not just the topic they mention.
- If a post touches multiple things, pick the one that best matches the main ask or goal.
- "Self-promotion", "Software selling", and "Consultancy promotion" apply when the poster is marketing themselves, a product, or a company - not when they are genuinely asking for help or advice.
- "Long-form content dump" = an essay, rant, or story with no clear question or ask.
- Mark "relevant" as true only if a sustainability/ESG consulting company would plausibly want to see or respond to this post (e.g. a potential client, job seeker, or genuine industry discussion). Mark it false for unrelated news, politics, or spam.

Respond with ONLY valid JSON in this exact shape, nothing else:
{{
  "category": "<one of the categories above, exact string>",
  "relevant": true/false,
  "confidence": <integer 0-100>,
}}
"""


def classify(title: str, context: str) -> dict:
    user_prompt = f"Title: {title}\n\nContext: {context}"

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


def load_existing_summary_urls(supabase: SupabaseRestClient) -> set[str]:
    rows = supabase.fetch_all("reddit_post_summary", select="reddit_url")
    return {
        str(row.get("reddit_url") or "").strip()
        for row in rows
        if str(row.get("reddit_url") or "").strip()
    }


def load_unclassified_history_rows(
    supabase: SupabaseRestClient,
    existing_summary_urls: set[str],
) -> list[dict[str, Any]]:
    rows = supabase.fetch_all(
        "reddit_history",
        select="id,dashboard_id,title,reddit_url,context",
        extra_params={"order": "created_at.asc"},
    )

    pending_rows = []
    for row in rows:
        reddit_url = str(row.get("reddit_url") or "").strip()
        if not reddit_url or reddit_url in existing_summary_urls:
            continue
        pending_rows.append(row)

    if ROW_LIMIT > 0:
        return pending_rows[:ROW_LIMIT]

    return pending_rows


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


def build_summary_row(
    history_row: dict[str, Any],
    classification: dict[str, Any],
) -> dict[str, Any]:
    dashboard_id = history_row.get("dashboard_id")
    if dashboard_id is not None:
        dashboard_id = int(dashboard_id)

    return {
        "reddit_history_id": int(history_row["id"]),
        "dashboard_id": dashboard_id,
        "reddit_url": str(history_row["reddit_url"]).strip(),
        "category": classification["category"],
        "relevant": classification["relevant"],
        "confidence": classification["confidence"],
    }


def insert_reddit_post_summaries(
    supabase: SupabaseRestClient,
    rows: list[dict[str, Any]],
) -> int:
    inserted = 0
    for batch in chunked(rows, size=INSERT_BATCH_SIZE):
        created = supabase.insert_rows(
            "reddit_post_summary",
            batch,
            on_conflict="reddit_url",
            ignore_duplicates=True,
        )
        inserted += len(created)
    return inserted


def main() -> None:
    supabase = SupabaseRestClient()
    insert_buffer: list[dict[str, Any]] = []
    classified_count = 0
    inserted_count = 0
    failed = 0

    try:
        existing_summary_urls = load_existing_summary_urls(supabase)
        pending_rows = load_unclassified_history_rows(supabase, existing_summary_urls)

        print(f"Already summarized : {len(existing_summary_urls)}")
        print(f"Pending to classify: {len(pending_rows)}")

        for index, history_row in enumerate(pending_rows, start=1):
            title = str(history_row.get("title") or "").strip()
            context = str(history_row.get("context") or "").strip()
            reddit_url = str(history_row.get("reddit_url") or "").strip()

            try:
                result = classify(title, context)
                classification = normalize_classification(result)
                insert_buffer.append(build_summary_row(history_row, classification))
                classified_count += 1
                print(
                    f"[{index}/{len(pending_rows)}] "
                    f"{classification['category']} "
                    f"({classification['confidence']}%) - {reddit_url}"
                )

                if len(insert_buffer) >= INSERT_BATCH_SIZE:
                    batch_count = insert_reddit_post_summaries(supabase, insert_buffer)
                    inserted_count += batch_count
                    print(f"  Inserted batch: {batch_count} rows")
                    insert_buffer.clear()
            except Exception as exc:
                failed += 1
                print(f"[{index}/{len(pending_rows)}] Failed - {reddit_url}: {exc}")

        if insert_buffer:
            batch_count = insert_reddit_post_summaries(supabase, insert_buffer)
            inserted_count += batch_count
            print(f"  Inserted final batch: {batch_count} rows")
            insert_buffer.clear()
    finally:
        supabase.close()

    print()
    print("=" * 50)
    print(f"Rows classified : {classified_count}")
    print(f"Rows inserted   : {inserted_count}")
    print(f"Rows failed     : {failed}")
    print("Supabase table  : reddit_post_summary")
    print("=" * 50)


if __name__ == "__main__":
    main()
