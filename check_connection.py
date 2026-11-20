import asyncio
import os
from bitget import Bitget

async def main():
    async with Bitget(
        os.getenv("BITGET_API_KEY", ""),
        os.getenv("BITGET_API_SECRET", ""),
        os.getenv("BITGET_API_PASSPHRASE", "")
    ) as client:
        assets = await client.assets()
        print("BITGET PRIVATE API: SUCCESS")
        print("Asset records returned:", len(assets) if isinstance(assets, list) else "Unexpected response")
        print("No orders placed.")

asyncio.run(main())
