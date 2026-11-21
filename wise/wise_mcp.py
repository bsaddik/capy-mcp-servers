# /// script
# requires-python = ">=3.11"
# dependencies = ["mcp>=1.9,<2", "httpx>=0.27"]
# ///
"""Read-only Wise MCP server. Calls only https://api.wise.com with WISE_API_TOKEN."""

import os

import httpx
from mcp.server.fastmcp import FastMCP

BASE_URL = "https://api.wise.com"

mcp = FastMCP("wise")


def get(path: str, params: dict | None = None):
    token = os.environ.get("WISE_API_TOKEN", "").strip()
    if not token:
        raise RuntimeError("WISE_API_TOKEN is not set")
    query = {k: v for k, v in (params or {}).items() if v is not None}
    response = httpx.get(
        BASE_URL + path,
        params=query,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        timeout=30,
    )
    if response.status_code >= 400:
        raise RuntimeError(f"Wise API {response.status_code} on GET {path}: {response.text[:1000]}")
    return response.json()


@mcp.tool()
def list_profiles() -> list:
    """List the Wise profiles (personal and business) this token can see. Use the id as profile_id elsewhere."""
    return get("/v2/profiles")


@mcp.tool()
def get_balances(profile_id: int) -> list:
    """List a profile's balances per currency, with amounts, reserved amounts and balance ids."""
    return get(f"/v4/profiles/{profile_id}/balances", {"types": "STANDARD,SAVINGS"})


@mcp.tool()
def list_transfers(
    profile_id: int,
    created_date_start: str | None = None,
    created_date_end: str | None = None,
    status: str | None = None,
    source_currency: str | None = None,
    target_currency: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list:
    """List transfers sent from a profile, oldest first; set created_date_start to reach recent ones.

    Dates are ISO 8601 (2026-01-01 or 2026-01-01T00:00:00.000Z). status is a comma-separated list such as
    outgoing_payment_sent,processing,cancelled,funds_refunded. Page with limit (max 100) and offset;
    a page shorter than limit is the last one.
    """
    return get(
        "/v1/transfers",
        {
            "profile": profile_id,
            "createdDateStart": created_date_start,
            "createdDateEnd": created_date_end,
            "status": status,
            "sourceCurrency": source_currency,
            "targetCurrency": target_currency,
            "limit": limit,
            "offset": offset,
        },
    )


@mcp.tool()
def get_transfer(transfer_id: int) -> dict:
    """Get one transfer's full details and current status."""
    return get(f"/v1/transfers/{transfer_id}")


@mcp.tool()
def list_activities(
    profile_id: int,
    since: str | None = None,
    until: str | None = None,
    status: str | None = None,
    monetary_resource_type: str | None = None,
    size: int = 50,
    next_cursor: str | None = None,
) -> dict:
    """List a profile's activity feed: transfers in and out, card spend, conversions, direct debits and more.

    since and until are ISO 8601 timestamps. status is one of REQUIRES_ATTENTION, IN_PROGRESS, UPCOMING,
    COMPLETED, CANCELLED. monetary_resource_type narrows to one kind, such as TRANSFER or CARD_TRANSACTION.
    size is 1 to 100. Pass the response's cursor as next_cursor, with the same filters, until it is null.
    """
    return get(
        f"/v1/profiles/{profile_id}/activities",
        {
            "since": since,
            "until": until,
            "status": status,
            "monetaryResourceType": monetary_resource_type,
            "size": size,
            "nextCursor": next_cursor,
        },
    )


@mcp.tool()
def get_balance_statement(
    profile_id: int,
    balance_id: int,
    currency: str,
    interval_start: str,
    interval_end: str,
    statement_type: str = "COMPACT",
) -> dict:
    """Get a balance's statement: every credit and debit with running balance, like a bank statement.

    balance_id and currency come from get_balances. interval_start and interval_end are ISO 8601
    timestamps at most 469 days apart. statement_type is COMPACT (one line per transaction) or FLAT.
    """
    return get(
        f"/v1/profiles/{profile_id}/balance-statements/{balance_id}/statement.json",
        {
            "currency": currency,
            "intervalStart": interval_start,
            "intervalEnd": interval_end,
            "type": statement_type,
        },
    )


@mcp.tool()
def list_recipients(profile_id: int, currency: str | None = None, seek_position: int | None = None) -> dict:
    """List a profile's saved recipients, 20 per page. Pass the response's seekPositionForNext as seek_position for the next page."""
    return get(
        "/v2/accounts",
        {"profileId": profile_id, "currency": currency, "size": 20, "seekPosition": seek_position},
    )


@mcp.tool()
def get_exchange_rate(source_currency: str, target_currency: str, time: str | None = None) -> list:
    """Get Wise's live mid-market rate between two currencies, or the rate at an ISO 8601 time in the past."""
    return get("/v1/rates", {"source": source_currency, "target": target_currency, "time": time})


if __name__ == "__main__":
    mcp.run()
