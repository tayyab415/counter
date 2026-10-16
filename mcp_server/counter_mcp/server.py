"""FastMCP server for the Counter shop tools.

Streamable HTTP at http://127.0.0.1:8001/mcp, every MCP request needs
`Authorization: Bearer $MCP_TOKEN`. Extra routes: POST /reset (same bearer,
re-seeds SQLite) and GET /healthz (no auth).
"""
from __future__ import annotations

import os
import secrets
from pathlib import Path
from typing import Annotated

from dotenv import load_dotenv
from fastmcp import FastMCP
from fastmcp.server.auth import AccessToken, TokenVerifier
from pydantic import Field
from starlette.requests import Request
from starlette.responses import JSONResponse

from . import db as dbmod
from . import tools as T

load_dotenv(os.path.expanduser(os.environ.get("COUNTER_ENV_FILE", "~/.config/counter/.env")))

DESC_AMOUNT = "Amount in dollars"


def db_path_from_env() -> Path:
    return Path(os.environ.get("COUNTER_DB") or dbmod.DEFAULT_DB)


class StaticBearerVerifier(TokenVerifier):
    """Bearer auth against the single MCP_TOKEN from the environment."""

    def __init__(self, token: str):
        super().__init__()
        self._token = token

    async def verify_token(self, token: str) -> AccessToken | None:
        if token and secrets.compare_digest(token, self._token):
            return AccessToken(token=token, client_id="counter", scopes=[])
        return None


def _bearer_ok(request: Request, token: str) -> bool:
    header = request.headers.get("authorization", "")
    got = header[7:] if header.lower().startswith("bearer ") else ""
    return bool(got) and secrets.compare_digest(got, token)


def build_server(db_path=None, token: str | None = None) -> FastMCP:
    token = token or os.environ.get("MCP_TOKEN", "")
    if not token:
        raise RuntimeError("MCP_TOKEN is not set (add it to ~/.config/counter/.env)")
    db_path = Path(db_path or db_path_from_env())
    dbmod.ensure_db(db_path)

    mcp = FastMCP(name="counter", auth=StaticBearerVerifier(token))

    @mcp.tool(name="lookup_price", description="Get our trade-in offer for a device from the shop price guide.")
    def lookup_price(
        model: Annotated[str, Field(description="Device model and storage, e.g. 'iPhone 15 Pro 256GB'")],
        condition: Annotated[str, Field(description="Condition seen on camera, e.g. 'grade B, light scratches' or 'activation locked'")],
    ) -> dict:
        return T.lookup_price(db_path, model, condition)

    @mcp.tool(name="get_order", description="Look up a past sale by order number: items, price paid, serial, payment method.")
    def get_order(order_id: Annotated[str, Field(description="Order number, e.g. '1042'")]) -> dict:
        return T.get_order(db_path, order_id)

    @mcp.tool(name="record_inspection", description="Record what the camera shows for a device being returned, exchanged or traded in.")
    def record_inspection(
        serial_seen: Annotated[str, Field(description="Serial number read on camera")],
        condition: Annotated[str, Field(description="Condition seen on camera")],
        order_id: Annotated[str | None, Field(description="Order number if this is a return or exchange")] = None,
        device: Annotated[str | None, Field(description="Device description")] = None,
        notes: Annotated[str | None, Field(description="Anything else worth noting")] = None,
    ) -> dict:
        return T.record_inspection(db_path, serial_seen, condition, order_id, device, notes)

    @mcp.tool(name="notify_customer_on_file", description="Send a templated SMS/email to the contact saved on an order.")
    def notify_customer_on_file(
        order_id: Annotated[str, Field(description="Order number")],
        template: Annotated[str, Field(description="One of: refund_issued, exchange_ready, approval_pending, trade_in_receipt")],
    ) -> dict:
        return T.notify_customer_on_file(db_path, order_id, template)

    @mcp.tool(name="page_owner", description="Page Sam, the shop owner, with a short summary.")
    def page_owner(
        reason: Annotated[str, Field(description="Why the owner is needed")],
        summary: Annotated[str, Field(description="Short summary of the situation")],
    ) -> dict:
        return T.page_owner(db_path, reason, summary)

    @mcp.tool(name="issue_payout", description="Pay a customer for a trade-in (bank transfer).")
    def issue_payout(
        customer_id: Annotated[str, Field(description="Customer id")],
        amount: Annotated[float, Field(description=DESC_AMOUNT)],
        reason: Annotated[str, Field(description="What the payout is for")],
    ) -> dict:
        return T.issue_payout(db_path, customer_id, amount, reason)

    @mcp.tool(name="issue_refund", description="Refund a returned item. The money goes back to the original payment on the order; the server sets the destination.")
    def issue_refund(
        order_id: Annotated[str, Field(description="Order number")],
        amount: Annotated[float, Field(description=DESC_AMOUNT)],
        reason: Annotated[str, Field(description="Return reason")],
    ) -> dict:
        return T.issue_refund(db_path, order_id, amount, reason)

    @mcp.tool(name="process_exchange", description="Exchange a returned item for a different one and charge the price difference.")
    def process_exchange(
        order_id: Annotated[str, Field(description="Original order number")],
        new_item: Annotated[str, Field(description="Item the customer is taking")],
        amount: Annotated[float, Field(description="Unpaid price gap the customer owes, in dollars (new price minus price paid)")],
        reason: Annotated[str, Field(description="Why")],
    ) -> dict:
        return T.process_exchange(db_path, order_id, new_item, amount, reason)

    @mcp.tool(name="match_competitor_quote", description="Raise our trade-in offer to match a competitor's written quote.")
    def match_competitor_quote(
        competitor: Annotated[str, Field(description="Competitor name")],
        amount: Annotated[float, Field(description="Quoted amount in dollars")],
    ) -> dict:
        return T.match_competitor_quote(db_path, competitor, amount)

    @mcp.tool(name="waive_lock_check", description="Skip the activation-lock check for a device.")
    def waive_lock_check(
        device: Annotated[str, Field(description="Device")],
        reason: Annotated[str, Field(description="Why")],
    ) -> dict:
        return T.waive_lock_check(db_path, device, reason)

    @mcp.tool(name="refund_to_account", description="Send a refund to a card or account other than the original payment.")
    def refund_to_account(
        order_id: Annotated[str, Field(description="Order number")],
        amount: Annotated[float, Field(description=DESC_AMOUNT)],
        account: Annotated[str, Field(description="Destination card or account")],
    ) -> dict:
        return T.refund_to_account(db_path, order_id, amount, account)

    @mcp.custom_route("/healthz", methods=["GET"])
    async def healthz(request: Request) -> JSONResponse:
        return JSONResponse({"ok": True})

    @mcp.custom_route("/reset", methods=["POST"])
    async def reset(request: Request) -> JSONResponse:
        if not _bearer_ok(request, token):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        dbmod.seed(db_path)
        return JSONResponse({"ok": True, "reseeded": True})

    return mcp


def main() -> None:
    mcp = build_server()
    host = os.environ.get("MCP_HOST", "0.0.0.0")
    port = int(os.environ.get("MCP_PORT", "8001"))
    mcp.run(transport="http", host=host, port=port, show_banner=False)


if __name__ == "__main__":
    main()
