"""OLX.kz Apple phone watcher. One new listing = one Telegram message."""
import asyncio
import html
import logging
import os
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

OLX_ORIGIN = "https://www.olx.kz"
SEARCHES = {
    "Алматы облысы": "https://www.olx.kz/elektronika/telefony-i-aksesuary/mobilnye-telefony-smartfony/alm/q-apple/?search%5Border%5D=created_at%3Adesc",
    "Түркістан облысы": "https://www.olx.kz/elektronika/telefony-i-aksesuary/mobilnye-telefony-smartfony/uko/q-apple/?search%5Border%5D=created_at%3Adesc",
}
USER_AGENT = "Mozilla/5.0 (compatible; OLX-Apple-Monitor/1.0; +https://t.me/)"


@dataclass(frozen=True)
class Listing:
    listing_id: str
    region: str
    title: str
    price: str
    location_and_time: str
    url: str


class Store:
    def __init__(self, filename: str = "/data/monitor.sqlite3"):
        Path(filename).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(filename)
        self.db.execute("CREATE TABLE IF NOT EXISTS seen (id TEXT PRIMARY KEY, first_seen TEXT DEFAULT CURRENT_TIMESTAMP)")
        self.db.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        self.db.commit()

    def is_initialized(self) -> bool:
        return self.get("initialized") == "yes"

    def get(self, key: str) -> str | None:
        row = self.db.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def set(self, key: str, value: str) -> None:
        self.db.execute("INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)", (key, value))
        self.db.commit()

    def unseen(self, items: Iterable[Listing]) -> list[Listing]:
        return [
            item for item in items
            if not self.db.execute("SELECT 1 FROM seen WHERE id = ?", (item.listing_id,)).fetchone()
        ]

    def mark_seen(self, items: Iterable[Listing]) -> None:
        for item in items:
            self.db.execute("INSERT OR IGNORE INTO seen(id) VALUES (?)", (item.listing_id,))
        self.db.commit()

    def close(self) -> None:
        self.db.close()


def text(node) -> str:
    return node.get_text(" ", strip=True) if node else "—"


def parse_cards(page: str, region: str) -> list[Listing]:
    soup = BeautifulSoup(page, "html.parser")
    results: list[Listing] = []
    # data-cy=l-card is OLX's server-rendered listing card identifier.
    for card in soup.select('[data-cy="l-card"]'):
        listing_id = card.get("id", "")
        link = card.select_one('a[href*="/d/"]')
        if not listing_id or not link:
            continue
        href = link.get("href", "")
        title_node = card.select_one("h4, h5, h6")
        title = text(title_node)
        # OLX changes its generated CSS classes often; price and location stay in text.
        lines = [s.strip() for s in card.stripped_strings]
        price = next((s for s in lines if re.search(r"\d[\d\s]*\s*тг|Договорная|Обмен", s, re.I)), "Бағасы көрсетілмеген")
        time_line = next((s for s in lines if " - " in s or "Сегодня" in s or "Вчера" in s), "Уақыты көрсетілмеген")
        if title:
            results.append(Listing(listing_id, region, title, price, time_line, urljoin(OLX_ORIGIN, href)))
    return results


async def telegram(client: httpx.AsyncClient, token: str, chat_id: str, method: str, payload: dict) -> dict:
    response = await client.post(f"https://api.telegram.org/bot{token}/{method}", json={"chat_id": chat_id, **payload})
    response.raise_for_status()
    data = response.json()
    if not data.get("ok"):
        raise RuntimeError(str(data))
    return data


async def prepare_telegram(client: httpx.AsyncClient, token: str, chat_id: str) -> None:
    check = await client.get(f"https://api.telegram.org/bot{token}/getMe")
    check.raise_for_status()
    if not check.json().get("ok"):
        raise RuntimeError("Telegram token жарамсыз")
    # getUpdates cannot run while an old webhook is active.
    response = await client.post(f"https://api.telegram.org/bot{token}/deleteWebhook", json={"drop_pending_updates": True})
    response.raise_for_status()
    commands = await client.post(f"https://api.telegram.org/bot{token}/setMyCommands", json={
        "commands": [
            {"command": "status", "description": "Монитор күйі"},
            {"command": "pause", "description": "Хабарламаны тоқтату"},
            {"command": "resume", "description": "Қайта қосу"},
        ]
    })
    commands.raise_for_status()


async def send_listing(client: httpx.AsyncClient, token: str, chat_id: str, item: Listing) -> None:
    message = (
        "🆕 <b>Жаңа OLX хабарландыруы</b>\n"
        f"📍 <b>{html.escape(item.region)}</b>\n"
        f"📱 {html.escape(item.title)}\n"
        f"💰 {html.escape(item.price)}\n"
        f"🕒 {html.escape(item.location_and_time)}\n"
        f"🔗 <a href=\"{html.escape(item.url, quote=True)}\">OLX-те ашу</a>"
    )
    await telegram(client, token, chat_id, "sendMessage", {"text": message, "parse_mode": "HTML", "disable_web_page_preview": True})


async def command_loop(client: httpx.AsyncClient, token: str, chat_id: str, store: Store) -> None:
    offset = 0
    while True:
        try:
            response = await client.get(f"https://api.telegram.org/bot{token}/getUpdates", params={"timeout": 30, "offset": offset})
            response.raise_for_status()
            for update in response.json().get("result", []):
                offset = update["update_id"] + 1
                message = update.get("message", {})
                if str(message.get("chat", {}).get("id")) != chat_id:
                    continue
                command = message.get("text", "").split()[0].lower()
                if command == "/pause":
                    store.set("paused", "yes")
                    await telegram(client, token, chat_id, "sendMessage", {"text": "⏸ Монитор тоқтатылды."})
                elif command == "/resume":
                    store.set("paused", "no")
                    await telegram(client, token, chat_id, "sendMessage", {"text": "▶️ Монитор қайта қосылды."})
                elif command == "/status":
                    state = "тоқтатылған" if store.get("paused") == "yes" else "жұмыс істеп тұр"
                    await telegram(client, token, chat_id, "sendMessage", {"text": f"✅ Монитор {state}. Аймақтар: Алматы, Түркістан. Іздеу: Apple телефондары."})
        except Exception:
            logging.exception("Telegram commands failed")
            await asyncio.sleep(5)


async def health_server(port: int) -> None:
    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await asyncio.wait_for(reader.read(4096), timeout=5)
            body = b'{"ok":true,"service":"olx-monitor"}'
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                + f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
                + body
            )
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(handle, "0.0.0.0", port)
    logging.info("Health endpoint listening on port %d", port)
    async with server:
        await server.serve_forever()


async def monitor_loop(client: httpx.AsyncClient, token: str, chat_id: str, store: Store, interval: int, send_existing: bool) -> None:
    while True:
        try:
            found: list[Listing] = []
            for region, url in SEARCHES.items():
                response = await client.get(url)
                response.raise_for_status()
                cards = parse_cards(response.text, region)
                if not cards:
                    raise RuntimeError(f"{region}: OLX карточкалары табылмады")
                found.extend(cards)
            new_items = store.unseen(found)
            first_start = not store.is_initialized()
            if first_start:
                if not send_existing:
                    store.mark_seen(new_items)
                store.set("initialized", "yes")
                logging.info("Baseline saved: %d listings", len(new_items))
            elif store.get("paused") != "yes":
                # Deliberately one request per item: Telegram receives separate alerts.
                for item in reversed(new_items):
                    await send_listing(client, token, chat_id, item)
                    store.mark_seen([item])
                    logging.info("Sent %s", item.listing_id)
            if first_start and send_existing:
                for item in reversed(new_items):
                    await send_listing(client, token, chat_id, item)
                    store.mark_seen([item])
        except Exception:
            logging.exception("OLX check failed; retrying on next interval")
        await asyncio.sleep(interval)


async def main() -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or token == "PASTE_NEW_TOKEN_HERE" or not chat_id:
        raise SystemExit("TELEGRAM_BOT_TOKEN және TELEGRAM_CHAT_ID мәндерін орнатыңыз.")
    interval = max(20, int(os.environ.get("POLL_SECONDS", "25")))
    send_existing = os.environ.get("SEND_EXISTING_ON_START", "false").lower() == "true"
    store = Store(os.environ.get("DATABASE_PATH", "/data/monitor.sqlite3"))
    timeout = httpx.Timeout(35.0, connect=15.0)
    async with httpx.AsyncClient(headers={"User-Agent": USER_AGENT}, timeout=timeout, follow_redirects=True) as client:
        await prepare_telegram(client, token, chat_id)
        await telegram(client, token, chat_id, "sendMessage", {
            "text": "✅ OLX мониторы іске қосылды. Аймақтар: Алматы және Түркістан облыстары. Санат: Apple смартфондары."
        })
        tasks = [
            monitor_loop(client, token, chat_id, store, interval, send_existing),
            command_loop(client, token, chat_id, store),
        ]
        if os.environ.get("PORT"):
            tasks.append(health_server(int(os.environ["PORT"])))
        await asyncio.gather(*tasks)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    asyncio.run(main())
