from __future__ import annotations

import json
import os
import time
from collections.abc import Iterable
from typing import Any

import httpx
from dotenv import load_dotenv


load_dotenv()


class SupabaseConfigError(RuntimeError):
    pass


class SupabaseRestClient:
    def __init__(self) -> None:
        supabase_url = os.getenv("SUPABASE_URL", "").rstrip("/")
        supabase_key = os.getenv("SUPABASE_KEY", "")

        if not supabase_url or not supabase_key:
            raise SupabaseConfigError("Set SUPABASE_URL and SUPABASE_KEY in .env.")

        self.base_url = f"{supabase_url}/rest/v1"
        self.client = httpx.Client(
            timeout=30,
            headers={
                "apikey": supabase_key,
                "Authorization": f"Bearer {supabase_key}",
                "Content-Type": "application/json",
            },
        )

    def close(self) -> None:
        self.client.close()

    def request(self, method: str, table: str, **kwargs: Any) -> httpx.Response:
        last_error: Exception | None = None

        for attempt in range(1, 4):
            try:
                response = self.client.request(
                    method,
                    f"{self.base_url}/{table}",
                    **kwargs,
                )
                self._raise_for_status(response)
                return response
            except httpx.TransportError as exc:
                last_error = exc
                if attempt == 3:
                    break
                time.sleep(2 * attempt)

        raise RuntimeError(f"Supabase request failed after retries: {last_error}") from last_error

    def fetch_one(
        self,
        table: str,
        *,
        select: str = "*",
        extra_params: dict[str, str] | None = None,
    ) -> dict[str, Any] | None:
        params = {
            "select": select,
            "limit": "1",
        }
        if extra_params:
            params.update(extra_params)

        response = self.request("GET", table, params=params)
        rows = response.json()
        if not isinstance(rows, list):
            raise RuntimeError(f"Unexpected Supabase response for {table}: {rows!r}")
        return rows[0] if rows else None

    def fetch_all(
        self,
        table: str,
        *,
        select: str = "*",
        page_size: int = 1000,
        extra_params: dict[str, str] | None = None,
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        offset = 0

        while True:
            params = {
                "select": select,
                "limit": str(page_size),
                "offset": str(offset),
            }
            if extra_params:
                params.update(extra_params)

            response = self.request("GET", table, params=params)
            batch = response.json()

            if not isinstance(batch, list):
                raise RuntimeError(f"Unexpected Supabase response for {table}: {batch!r}")

            rows.extend(batch)
            if len(batch) < page_size:
                return rows

            offset += page_size

    def insert_rows(
        self,
        table: str,
        rows: list[dict[str, Any]],
        *,
        on_conflict: str | None = None,
        ignore_duplicates: bool = False,
    ) -> list[dict[str, Any]]:
        if not rows:
            return []

        params = {}
        if on_conflict:
            params["on_conflict"] = on_conflict

        prefer_parts = ["return=representation"]
        if ignore_duplicates:
            prefer_parts.insert(0, "resolution=ignore-duplicates")

        response = self.request(
            "POST",
            table,
            params=params,
            json=rows,
            headers={"Prefer": ",".join(prefer_parts)},
        )
        if not response.content:
            return []
        data = response.json()
        return data if isinstance(data, list) else [data]

    def update_by_id(self, table: str, row_id: int, values: dict[str, Any]) -> None:
        self.request(
            "PATCH",
            table,
            params={"id": f"eq.{row_id}"},
            json=values,
            headers={"Prefer": "return=minimal"},
        )

    def _raise_for_status(self, response: httpx.Response) -> None:
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            detail = response.text.strip()
            raise RuntimeError(f"Supabase request failed: {exc}. {detail}") from exc


def chunked(rows: list[dict[str, Any]], size: int = 500) -> Iterable[list[dict[str, Any]]]:
    for index in range(0, len(rows), size):
        yield rows[index : index + size]


def parse_alert_id(value: Any) -> int | None:
    text = str(value or "").strip()
    if not text:
        return None

    try:
        return int(text)
    except ValueError:
        return None


def sync_dashboard_rows(client: SupabaseRestClient, rows: list[dict[str, Any]]) -> tuple[int, int]:
    existing_rows = client.fetch_all(
        "f5bot_dashboard",
        select="id,alert_id,keyword",
    )

    by_alert_id = {
        int(row["alert_id"]): int(row["id"])
        for row in existing_rows
        if row.get("alert_id") is not None and row.get("id") is not None
    }
    by_keyword = {
        str(row["keyword"]): int(row["id"])
        for row in existing_rows
        if row.get("keyword") is not None and row.get("id") is not None
    }

    inserted = 0
    updated = 0
    inserts: list[dict[str, Any]] = []

    for row in rows:
        alert_id = parse_alert_id(row.get("alert_id"))
        keyword = str(row.get("keyword") or "").strip()
        if alert_id is None or not keyword:
            print(f"Skipping dashboard row with missing alert_id/keyword: {row!r}")
            continue

        payload = {
            "alert_id": alert_id,
            "keyword": keyword,
            "flags": row.get("flags"),
            "hits_last_7_days": row.get("hits_last_7_days"),
            "history_url": row.get("history_url"),
        }

        row_id = by_alert_id.get(alert_id) or by_keyword.get(keyword)
        if row_id:
            client.update_by_id("f5bot_dashboard", row_id, payload)
            updated += 1
        else:
            inserts.append(payload)

    for batch in chunked(inserts):
        created = client.insert_rows("f5bot_dashboard", batch)
        inserted += len(created)

    return inserted, updated


def load_f5bot_storage_state(client: SupabaseRestClient) -> dict[str, Any] | None:
    row = client.fetch_one(
        "f5bot_dashboard",
        select="session",
        extra_params={
            "session": "not.is.null",
            "order": "id.asc",
        },
    )
    if not row:
        return None

    raw_session = str(row.get("session") or "").strip()
    if not raw_session:
        return None

    try:
        storage_state = json.loads(raw_session)
    except json.JSONDecodeError as exc:
        raise RuntimeError("Stored F5Bot session is not valid JSON.") from exc

    if not isinstance(storage_state, dict):
        raise RuntimeError("Stored F5Bot session JSON must be an object.")

    return storage_state


def save_f5bot_storage_state(
    client: SupabaseRestClient,
    storage_state: dict[str, Any],
) -> int:
    row = client.fetch_one(
        "f5bot_dashboard",
        select="id",
        extra_params={
            "session": "not.is.null",
            "order": "id.asc",
        },
    )

    if not row:
        row = client.fetch_one(
            "f5bot_dashboard",
            select="id",
            extra_params={"order": "id.asc"},
        )

    if not row or row.get("id") is None:
        raise RuntimeError(
            "Cannot save F5Bot session because f5bot_dashboard has no existing row "
            "to update. Sync dashboard rows once before moving session storage to Supabase."
        )

    row_id = int(row["id"])
    session_json = json.dumps(storage_state, separators=(",", ":"))
    client.update_by_id("f5bot_dashboard", row_id, {"session": session_json})
    return row_id


def load_dashboard_rows_with_hits(client: SupabaseRestClient) -> list[dict[str, Any]]:
    rows = client.fetch_all(
        "f5bot_dashboard",
        select="id,alert_id,keyword,flags,hits_last_7_days,history_url",
        extra_params={"order": "keyword.asc"},
    )

    return [row for row in rows if row_has_hits(row)]


def row_has_hits(row: dict[str, Any]) -> bool:
    hits_text = str(row.get("hits_last_7_days") or "").strip()
    if not hits_text or hits_text == "-":
        return False

    try:
        return int(hits_text) > 0
    except ValueError:
        return False


def load_existing_reddit_urls(client: SupabaseRestClient) -> set[str]:
    rows = client.fetch_all("reddit_history", select="reddit_url")
    return {
        str(row.get("reddit_url") or "").strip()
        for row in rows
        if str(row.get("reddit_url") or "").strip()
    }


def insert_reddit_history(client: SupabaseRestClient, rows: list[dict[str, Any]]) -> int:
    inserted = 0
    for batch in chunked(rows):
        created = client.insert_rows(
            "reddit_history",
            batch,
            on_conflict="reddit_url",
            ignore_duplicates=True,
        )
        inserted += len(created)
    return inserted
