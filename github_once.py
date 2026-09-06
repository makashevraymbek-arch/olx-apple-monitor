"""Run one OLX check from GitHub Actions."""
import asyncio
import logging
import os

import httpx

from app import SEARCHES, Store, parse_cards, send_listing, telegram


async def main() -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        raise SystemExit("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID secrets are required")

    store = Store(os.environ.get("DATABASE_PATH", "data/monitor.sqlite3"))
    timeout = httpx.Timeout(35.0, connect=15.0)
    headers = {"User-Agent": "Mozilla/5.0 (compatible; OLX-Apple-Monitor/1.0)"}
    try:
        async with httpx.AsyncClient(headers=headers, timeout=timeout, follow_redirects=True) as client:
            found = []
            for region, url in SEARCHES.items():
                response = await client.get(url)
                response.raise_for_status()
                cards = parse_cards(response.text, region)
                if not cards:
                    raise RuntimeError(f"{region}: OLX карточкалары табылмады")
                found.extend(cards)

            new_items = store.unseen(found)
            if not store.is_initialized():
                await telegram(client, token, chat_id, "sendMessage", {
                    "text": "✅ OLX мониторы іске қосылды. Әр 5 минут сайын Алматы және Түркістан облыстарындағы Apple смартфондары тексеріледі."
                })
                store.mark_seen(new_items)
                store.set("initialized", "yes")
                logging.info("Алғашқы база сақталды: %d хабарландыру", len(new_items))
                return

            for item in reversed(new_items):
                await send_listing(client, token, chat_id, item)
                store.mark_seen([item])
                logging.info("Telegram-ға жіберілді: %s", item.listing_id)
    finally:
        store.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    asyncio.run(main())
