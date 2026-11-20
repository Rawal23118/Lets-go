import asyncio
import base64
import hashlib
import hmac
import json
import time
from urllib.parse import urlencode

import aiohttp

BASE = "https://api.bitget.com"


def eligible_usdt_crypto_symbols(instruments):
    """Return only verified online, non-Reality USDT Spot crypto symbols."""
    if not isinstance(instruments, list) or not instruments:
        raise ValueError("Bitget instrument metadata is empty or invalid")
    symbols = {
        item.get("symbol")
        for item in instruments
        if isinstance(item, dict)
        and item.get("category") == "SPOT"
        and item.get("quoteCoin") == "USDT"
        and item.get("status") == "online"
        and str(item.get("isReality", "")).lower() == "no"
        and item.get("symbolType") == "crypto"
        and isinstance(item.get("symbol"), str)
        and item.get("symbol")
    }
    if not symbols:
        raise ValueError("No eligible ordinary USDT Spot crypto instruments")
    return symbols


def select_volume_tier_symbols(tickers, eligible_symbols, min_quote_volume, max_symbols):
    """Select across low, middle, and high volume tiers instead of only the largest pairs."""
    candidates = []
    for ticker in tickers or []:
        symbol = ticker.get("symbol", "")
        if symbol not in eligible_symbols:
            continue
        try:
            quote_volume = float(ticker.get("usdtVolume", ticker.get("quoteVolume", 0)) or 0)
        except (TypeError, ValueError):
            continue
        if quote_volume >= min_quote_volume and quote_volume > 0:
            candidates.append((quote_volume, symbol))

    candidates.sort(key=lambda item: item[0])
    if len(candidates) <= max_symbols:
        return [symbol for _, symbol in candidates]

    # Split the full liquidity range into three bands. Sample evenly within each
    # band so small/mid-volume markets are represented without selecting only
    # the very weakest markets in that band.
    bands = [candidates[:len(candidates)//3],
             candidates[len(candidates)//3:2*len(candidates)//3],
             candidates[2*len(candidates)//3:]]
    quotas = [max_symbols // 3] * 3
    for index in range(max_symbols % 3):
        quotas[2-index] += 1

    selected = []
    for band, quota in zip(bands, quotas):
        if not band or quota == 0:
            continue
        count = min(quota, len(band))
        for i in range(count):
            position = min(len(band)-1, int((i + 0.5) * len(band) / count))
            selected.append(band[position][1])
    return selected


class BitgetError(RuntimeError):
    pass


class Bitget:
    def __init__(self, key="", secret="", passphrase="", session=None):
        self.key, self.secret, self.passphrase = key, secret, passphrase
        self.session = session
        self.owned_session = session is None

    async def __aenter__(self):
        if self.session is None:
            timeout = aiohttp.ClientTimeout(total=20, connect=8, sock_read=15)
            self.session = aiohttp.ClientSession(timeout=timeout)
        return self

    async def __aexit__(self, *_):
        if self.owned_session and self.session:
            await self.session.close()

    def _headers(self, timestamp, method, path, query="", body=""):
        prehash = timestamp + method.upper() + path
        if query:
            prehash += "?" + query
        prehash += body
        digest = hmac.new(self.secret.encode(), prehash.encode(), hashlib.sha256).digest()
        return {
            "ACCESS-KEY": self.key,
            "ACCESS-SIGN": base64.b64encode(digest).decode(),
            "ACCESS-PASSPHRASE": self.passphrase,
            "ACCESS-TIMESTAMP": timestamp,
            "Content-Type": "application/json",
            "locale": "en-US",
        }

    async def request(self, method, path, params=None, payload=None, private=False):
        params = params or {}
        query = urlencode(sorted((k, str(v)) for k, v in params.items()))
        body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False) if payload is not None else ""
        url = BASE + path + (("?" + query) if query else "")
        if private and not all((self.key, self.secret, self.passphrase)):
            raise BitgetError("Missing API credentials")
        attempts = 3 if method.upper() == "GET" else 1

        for attempt in range(attempts):
            # Re-sign every private retry with a fresh timestamp.
            headers = (
                self._headers(str(int(time.time() * 1000)), method, path, query, body)
                if private else {}
            )
            try:
                async with self.session.request(
                    method, url,
                    data=body if body else None,
                    headers=headers,
                ) as resp:
                    raw = await resp.text()
                    try:
                        data = json.loads(raw)
                    except ValueError as exc:
                        raise BitgetError(
                            f"Invalid JSON from Bitget: {raw[:300]}"
                        ) from exc

                    if resp.status >= 400 or data.get("code") != "00000":
                        raise BitgetError(
                            f"Bitget API error HTTP {resp.status}: {data}"
                        )
                    return data.get("data")

            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                if method.upper() != "GET" or attempt >= attempts - 1:
                    raise BitgetError(
                        f"Network/timeout error after {attempt + 1} "
                        f"attempt(s): {exc}"
                    ) from exc

                delay = 0.5 * (2 ** attempt)
                await asyncio.sleep(delay)

    async def tickers(self):
        return await self.request("GET", "/api/v2/spot/market/tickers")

    async def instruments(self):
        return await self.request("GET", "/api/v3/market/instruments",
                                  {"category": "SPOT"})

    async def orderbook(self, symbol, limit=15):
        return await self.request("GET", "/api/v2/spot/market/orderbook",
                                  {"symbol": symbol, "type": "step0", "limit": limit})

    async def symbols(self, symbol=None):
        params = {"symbol": symbol} if symbol else {}
        return await self.request("GET", "/api/v2/spot/public/symbols", params)

    async def place_market_buy(self, symbol, quote_amount, client_oid):
        return await self.request("POST", "/api/v2/spot/trade/place-order", payload={
            "symbol": symbol, "side": "buy", "orderType": "market",
            "size": str(quote_amount), "clientOid": client_oid
        }, private=True)

    async def place_market_sell(self, symbol, base_amount, client_oid):
        return await self.request("POST", "/api/v2/spot/trade/place-order", payload={
            "symbol": symbol, "side": "sell", "orderType": "market",
            "size": str(base_amount), "clientOid": client_oid
        }, private=True)

    async def order_detail(self, order_id=None, client_oid=None):
        if not order_id and not client_oid:
            raise ValueError("order_id or client_oid is required")
        params = {"orderId": order_id} if order_id else {"clientOid": client_oid}
        data = await self.request("GET", "/api/v2/spot/trade/orderInfo", params, private=True)
        if isinstance(data, list):
            return data[0] if data else None
        return data

    async def cancel_order(self, symbol, order_id):
        return await self.request("POST", "/api/v2/spot/trade/cancel-order",
                                  payload={"symbol": symbol, "orderId": order_id}, private=True)

    async def assets(self):
        return await self.request("GET", "/api/v2/spot/account/assets",
                                  {"assetType": "all"}, private=True)

    async def open_orders(self):
        """Fetch every page of unfilled Classic Spot orders."""
        orders = []
        cursor = None
        while True:
            params = {"limit": "100"}
            if cursor:
                params["idLessThan"] = cursor
            page = await self.request(
                "GET", "/api/v2/spot/trade/unfilled-orders",
                params, private=True
            )
            if not isinstance(page, list):
                raise BitgetError("Invalid open-orders response; expected a list")
            orders.extend(page)
            if len(page) < 100:
                return orders
            next_cursor = str(page[-1].get("orderId", ""))
            if not next_cursor or next_cursor == cursor:
                raise BitgetError("Open-orders pagination did not advance")
            cursor = next_cursor
