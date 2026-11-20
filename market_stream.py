import asyncio
import json
import ssl
import time

import aiohttp


WS_PUBLIC_URL = "wss://ws.bitget.com/v2/ws/public"


class SpotBookStream:
    """
    Classic Spot books15 snapshot stream.

    Uses fewer than 50 symbols per connection as recommended by Bitget.
    A shard's cache is cleared whenever its connection drops, so callers
    can never trade from a snapshot retained across a disconnect.
    """

    def __init__(self, session, logger, shard_size=45):
        if session is None:
            raise ValueError("An active aiohttp session is required")
        if not 1 <= int(shard_size) < 50:
            raise ValueError("shard_size must be between 1 and 49")
        self.session = session
        self.log = logger
        # Keep normal CA-chain and hostname verification enabled. Some Windows
        # OpenSSL builds reject otherwise-valid server chains solely because a
        # certificate omits the optional Authority Key Identifier extension.
        # Relax only OpenSSL's strict-extension checks; do not disable TLS
        # certificate verification.
        self.ssl_context = ssl.create_default_context()
        strict_flag = getattr(ssl, "VERIFY_X509_STRICT", 0)
        if strict_flag:
            self.ssl_context.verify_flags &= ~strict_flag
        self.shard_size = int(shard_size)
        self.symbols = set()
        self.cache = {}
        self.tasks = []
        self.active_shards = set()
        self.version = 0
        self.closed = False

    def get(self, symbol):
        """Return (book, local_receive_monotonic, version), or None."""
        return self.cache.get(symbol)

    @property
    def active_connections(self):
        return len(self.active_shards)

    async def set_symbols(self, symbols):
        wanted = set(symbols or [])
        if wanted == self.symbols and (not wanted or self.tasks):
            return

        await self._stop_tasks()
        self.symbols = wanted
        self.closed = False

        if not wanted:
            return

        ordered = sorted(wanted)
        shards = [
            ordered[i:i + self.shard_size]
            for i in range(0, len(ordered), self.shard_size)
        ]
        self.log.info(
            "Starting Bitget books15 stream: %s symbols across %s connections "
            "(maximum %s symbols per connection)",
            len(ordered), len(shards), self.shard_size,
        )

        for shard_id, shard in enumerate(shards):
            task = asyncio.create_task(
                self._run_shard(shard_id, tuple(shard)),
                name=f"bitget-books15-{shard_id}",
            )
            self.tasks.append(task)

    async def _stop_tasks(self):
        old_tasks, self.tasks = self.tasks, []
        self.closed = True
        for task in old_tasks:
            task.cancel()
        if old_tasks:
            await asyncio.gather(*old_tasks, return_exceptions=True)
        self.active_shards.clear()
        self.cache.clear()

    async def close(self):
        await self._stop_tasks()
        self.symbols.clear()

    async def _run_shard(self, shard_id, symbols):
        delay = 1.0
        symbol_set = set(symbols)

        while not self.closed:
            try:
                async with self.session.ws_connect(
                    WS_PUBLIC_URL,
                    ssl=self.ssl_context,
                    timeout=10,
                    receive_timeout=75,
                    autoping=False,
                    autoclose=True,
                    max_msg_size=4 * 1024 * 1024,
                ) as ws:
                    # Never carry snapshots over a reconnect.
                    for symbol in symbol_set:
                        self.cache.pop(symbol, None)

                    args = [
                        {
                            "instType": "SPOT",
                            "channel": "books15",
                            "instId": symbol,
                        }
                        for symbol in symbols
                    ]
                    await ws.send_json({"op": "subscribe", "args": args})
                    self.active_shards.add(shard_id)
                    self.log.info(
                        "Bitget books15 connection %s connected; subscribed to %s symbols",
                        shard_id + 1, len(symbols),
                    )
                    delay = 1.0
                    await self._serve_connection(ws, shard_id, symbol_set)

            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.log.warning(
                    "Bitget books15 connection %s disconnected: %s; reconnecting in %.1fs",
                    shard_id + 1, exc, delay,
                )
            finally:
                self.active_shards.discard(shard_id)
                for symbol in symbol_set:
                    self.cache.pop(symbol, None)

            if not self.closed:
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30.0)

    async def _serve_connection(self, ws, shard_id, symbols):
        pong_event = asyncio.Event()
        reader = asyncio.create_task(
            self._reader(ws, symbols, pong_event),
            name=f"bitget-books15-reader-{shard_id}",
        )
        heartbeat = asyncio.create_task(
            self._heartbeat(ws, pong_event),
            name=f"bitget-books15-heartbeat-{shard_id}",
        )
        tasks = (reader, heartbeat)
        try:
            done, pending = await asyncio.wait(
                tasks, return_when=asyncio.FIRST_COMPLETED
            )
            for task in done:
                exc = task.exception()
                if exc:
                    raise exc
            raise RuntimeError("WebSocket reader or heartbeat stopped")
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _heartbeat(self, ws, pong_event):
        while not ws.closed:
            await asyncio.sleep(25)
            pong_event.clear()
            await ws.send_str("ping")
            try:
                await asyncio.wait_for(pong_event.wait(), timeout=30)
            except asyncio.TimeoutError as exc:
                raise RuntimeError("Bitget WebSocket pong timed out") from exc

    async def _reader(self, ws, symbols, pong_event):
        while True:
            msg = await ws.receive()

            if msg.type == aiohttp.WSMsgType.TEXT:
                raw = msg.data
                if raw == "pong":
                    pong_event.set()
                    continue
                if raw == "ping":
                    await ws.send_str("pong")
                    continue

                try:
                    payload = json.loads(raw)
                except (TypeError, ValueError):
                    continue

                event = payload.get("event")
                if event == "error":
                    raise RuntimeError(
                        f"Bitget subscription error: "
                        f"{payload.get('code')} {payload.get('msg')}"
                    )

                # Ignore subscription acknowledgements and unrelated messages.
                arg = payload.get("arg") or {}
                if arg.get("channel") != "books15":
                    continue

                symbol = arg.get("instId")
                if symbol not in symbols:
                    continue

                rows = payload.get("data")
                if not isinstance(rows, list) or not rows:
                    continue

                row = rows[0]
                if not isinstance(row, dict):
                    continue

                bids = row.get("bids") or []
                asks = row.get("asks") or []
                ts = row.get("ts") or payload.get("ts")
                if not bids or not asks or ts in (None, ""):
                    continue

                try:
                    ts_num = float(ts)
                except (TypeError, ValueError):
                    continue
                if ts_num <= 0:
                    continue

                # books15 messages are full snapshots, not incremental deltas.
                book = {"bids": bids, "asks": asks, "ts": ts_num}
                self.version += 1
                self.cache[symbol] = (
                    book,
                    time.monotonic(),
                    self.version,
                )

            elif msg.type == aiohttp.WSMsgType.PING:
                await ws.pong(msg.data)
            elif msg.type in (
                aiohttp.WSMsgType.CLOSE,
                aiohttp.WSMsgType.CLOSED,
                aiohttp.WSMsgType.CLOSING,
            ):
                raise RuntimeError("Bitget closed the WebSocket connection")
            elif msg.type == aiohttp.WSMsgType.ERROR:
                raise RuntimeError(f"WebSocket transport error: {ws.exception()}")
