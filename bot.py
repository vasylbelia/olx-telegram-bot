"""
Telegram bot: monitor OLX offers for iPhones and notify subscribers about NEW offers

This file was updated to *handle environments where the Python `ssl` module is missing*.

Behavior summary:
- If your Python environment **has SSL support** (normal case), the bot will import `aiohttp` and `aiogram` and run as before.
- If **SSL is missing**, the script will *not* attempt to import `aiogram`/`aiohttp` (which would raise ModuleNotFoundError: ssl). Instead it prints a clear, actionable error explaining how to fix the environment and allows you to run a built-in self-test with `--test`.

New features in this version:
- Deferred (conditional) imports of `aiohttp` / `aiogram` to avoid the immediate crash when `ssl` is missing.
- A `--test` mode that runs lightweight unit tests for parsing and price normalization (so you can validate parser logic even without network).
- Clear troubleshooting instructions for installing OpenSSL / rebuilding Python (common cause of missing `ssl`).

Usage:
- Run self-tests (no network, works without ssl):
    python3 bot_olx_iphone.py --test

- Start the bot (requires ssl + aiohttp + aiogram installed and working):
    python3 bot_olx_iphone.py

If SSL is missing the script will exit with an explanation and suggestions on how to get a working Python build.

NOTE: This file still contains the API_TOKEN you previously provided. Keep it private.
"""

import argparse
import asyncio
import json
import logging
import re
import sys
from pathlib import Path
from typing import Dict, List, Optional

# BeautifulSoup is pure-python and safe to import even in restricted environments
from bs4 import BeautifulSoup

# Try to detect SSL support first. If ssl is missing, defer importing aiohttp/aiogram.
try:
    import ssl  # type: ignore
    SSL_AVAILABLE = True
except Exception:
    SSL_AVAILABLE = False

# We will only import aiohttp / aiogram if SSL is available
AIOLIB_AVAILABLE = False
if SSL_AVAILABLE:
    try:
        import aiohttp  # type: ignore
        from aiogram import Bot, Dispatcher, executor, types  # type: ignore
        AIOLIB_AVAILABLE = True
    except Exception:
        # Could not import aiohttp/aiogram despite ssl present (bad env). We'll log and continue in non-network mode.
        AIOLIB_AVAILABLE = False

# ---------------------- CONFIG ----------------------
# Inserted API token (you provided this)
API_TOKEN = "8240356801:AAFuAU6PXvKx0X56GzynTZ7C27V5HvrcIP4"

# Where to store persistent data
DATA_DIR = Path("./data")
DATA_DIR.mkdir(exist_ok=True)
SEEN_FILE = DATA_DIR / "seen_offers.json"
SUBSCRIBERS_FILE = DATA_DIR / "subscribers.json"

# Polling interval (seconds)
POLL_INTERVAL = 60  # change as you wish (e.g., 30, 60, 120)

# Search queries / URLs to check. You can put full OLX search result URLs here.
SEARCH_QUERIES = [
    "https://www.olx.pl/d/oferty/q/iphone/",
]

# Price thresholds (PLN) - example table. Edit to match your price list.
PRICE_THRESHOLDS: Dict[str, int] = {
    "iphone 11": 350,
    "iphone 11 pro": 400,
    "iphone 12": 400,
    "iphone 12 pro": 750,
    "iphone 12 pro max": 750,
    "iphone 13": 700,
    "iphone 13 pro": 1200,
    "iphone 13 pro max": 1300,
    "iphone 14": 1100,
    "iphone 14 pro": 1500,
    "iphone 14 plus": 1300,
    "iphone 14 pro max": 1600,
    "iphone 15": 1550,
    "iphone 15 pro": 2400,
    "iphone 15 pro max": 2700,
    "iphone 16": 2900,
}

# Optional: only notify if offer contains one of these words (e.g., 'stan dobry', 'bez blokad')
MUST_CONTAIN: List[str] = []

# User-Agent header to mimic a real browser
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0 Safari/537.36"
}

# ---------------------- END CONFIG ----------------------

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# Persistence helpers

def load_json(path: Path, default):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning(f"Failed to load {path}: {e}")
            return default
    return default


def save_json(path: Path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


seen_offers = load_json(SEEN_FILE, [])  # list of offer ids we've already notified about
subscribers = load_json(SUBSCRIBERS_FILE, [])  # list of chat_ids


# ---------------------- Helpers ----------------------

def normalize_price(text: str) -> Optional[int]:
    """Extract integer PLN price from text like '1 200 zł' or 'PLN 1,200.50'.
    Returns int or None if not parseable. Note: this truncates fractional part (int).
    """
    if not text:
        return None
    cleaned = re.sub(r"[^0-9,\.]+", "", text)
    if not cleaned:
        return None
    cleaned = cleaned.replace(',', '.')
    parts = cleaned.split('.')
    if len(parts) > 2:
        # join all but last as thousands, keep last as fractional part
        cleaned = ''.join(parts[:-1]) + '.' + parts[-1]
    try:
        value = float(cleaned)
        return int(value)
    except Exception:
        digits = re.sub(r"\D", "", text)
        if digits:
            return int(digits)
    return None


def matches_price_threshold(title: str, price_pln: Optional[int]) -> bool:
    if price_pln is None:
        return False
    title_l = title.lower()
    for key, max_price in PRICE_THRESHOLDS.items():
        if key in title_l:
            return price_pln <= max_price
    return False


def contains_required_words(text: str) -> bool:
    if not MUST_CONTAIN:
        return True
    lower = text.lower()
    return all(word.lower() in lower for word in MUST_CONTAIN)


# ---------------------- OLX scraping (parser) ----------------------

def parse_offers_from_html(html: str) -> List[Dict]:
    """Attempt to parse OLX search results and extract offers.
    Returns list of dicts: {id, title, price, price_int, url, location, excerpt}

    This parser uses a couple of heuristics and a simple fallback (link hrefs) so it can be
    tested without network access.
    """
    soup = BeautifulSoup(html, "html.parser")
    offers = []

    # Try structured result first
    candidates = soup.select('[data-testid="listing-grid"] [data-cy="l-card"]')
    if not candidates:
        candidates = soup.find_all('a', href=re.compile(r'/d/oferta/|/oferta/'))

    for tag in candidates:
        try:
            a = tag.find('a', href=True) if tag.name != 'a' else tag
            href = a['href'] if a and a.has_attr('href') else None
            if href and href.startswith('/'):
                href = 'https://www.olx.pl' + href

            offer_id = None
            if hasattr(tag, 'attrs') and 'data-id' in tag.attrs:
                offer_id = tag.attrs.get('data-id')
            elif href:
                m = re.search(r'-([0-9]{6,})\b', href)
                if m:
                    offer_id = m.group(1)
                else:
                    offer_id = href.rstrip('/').split('/')[-1]

            title = ''
            t = tag.select_one('h6') or tag.select_one('h3') or tag.select_one('.css-1bbgabe') or tag.select_one('.css-16v5mdi')
            if t:
                title = t.get_text(strip=True)
            else:
                title = a.get_text(strip=True) if a else ''

            price_text = ''
            p = tag.select_one('[data-testid="ad-price"]') or tag.select_one('.price') or tag.select_one('.css-10b0gli')
            if p:
                price_text = p.get_text(strip=True)

            price_int = normalize_price(price_text)

            location = ''
            loc = tag.select_one('.css-19yf5ek') or tag.select_one('.css-nq3w9f')
            if loc:
                location = loc.get_text(strip=True)

            excerpt = ''
            desc = tag.select_one('.css-6safw6') or tag.select_one('.css-1c9m2a9')
            if desc:
                excerpt = desc.get_text(strip=True)

            if not offer_id:
                continue

            offers.append({
                'id': str(offer_id),
                'title': title,
                'price_text': price_text,
                'price': price_int,
                'url': href,
                'location': location,
                'excerpt': excerpt,
            })
        except Exception:
            logger.exception('Error parsing one offer element')

    return offers


# ---------------------- Network / Bot code (only if aiohttp + aiogram are available) ----------------------

if AIOLIB_AVAILABLE:
    bot = Bot(token=API_TOKEN)
    dp = Dispatcher(bot)

    async def fetch(session: aiohttp.ClientSession, url: str) -> Optional[str]:
        try:
            async with session.get(url, headers=HEADERS, timeout=20) as resp:
                if resp.status == 200:
                    return await resp.text()
                else:
                    logger.warning(f"Non-200 from {url}: {resp.status}")
        except Exception as e:
            logger.exception(f"Error fetching {url}: {e}")
        return None

    async def check_once(session: aiohttp.ClientSession):
        global seen_offers
        new_found = []
        for query in SEARCH_QUERIES:
            logger.info(f"Checking: {query}")
            html = await fetch(session, query)
            if not html:
                continue
            offers = parse_offers_from_html(html)
            logger.info(f"Parsed {len(offers)} offers from query")
            for off in offers:
                oid = off['id']
                if oid in seen_offers:
                    continue
                if not matches_price_threshold(off['title'], off['price']):
                    continue
                if not contains_required_words(off.get('excerpt') or off.get('title')):
                    continue
                text = f"📱 *{off['title']}*\n"
                if off['price']:
                    text += f"💰 {off['price']} zł\n"
                elif off['price_text']:
                    text += f"💰 {off['price_text']}\n"
                if off['location']:
                    text += f"📍 {off['location']}\n"
                if off['url']:
                    text += f"🔗 {off['url']}\n"
                if off.get('excerpt'):
                    excerpt = off['excerpt']
                    if len(excerpt) > 300:
                        excerpt = excerpt[:300] + '...'
                    text += f"\n{excerpt}\n"
                text += f"\nID: {oid}"

                new_found.append((oid, text))

        if new_found:
            for oid, _ in new_found:
                seen_offers.append(oid)
            save_json(SEEN_FILE, seen_offers)
            for oid, text in new_found:
                for chat_id in list(subscribers):
                    try:
                        await bot.send_message(chat_id, text, disable_web_page_preview=False)
                    except Exception as e:
                        logger.warning(f"Failed to send to {chat_id}: {e}")
        else:
            logger.info('No new matching offers found this cycle')

    async def polling_loop():
        await bot.wait_until_ready()
        async with aiohttp.ClientSession() as session:
            while True:
                try:
                    await check_once(session)
                except Exception:
                    logger.exception('Error during check_once')
                await asyncio.sleep(POLL_INTERVAL)

    @dp.message_handler(commands=["start"])
    async def cmd_start(message: types.Message):
        chat_id = message.chat.id
        if chat_id not in subscribers:
            subscribers.append(chat_id)
            save_json(SUBSCRIBERS_FILE, subscribers)
            await message.reply("Subskrypcja aktywowana. Będziesz otrzymywać powiadomienia o nowych ofertach.")
        else:
            await message.reply("Już jesteś subskrybentem.")

    @dp.message_handler(commands=["stop"])
    async def cmd_stop(message: types.Message):
        chat_id = message.chat.id
        if chat_id in subscribers:
            subscribers.remove(chat_id)
            save_json(SUBSCRIBERS_FILE, subscribers)
            await message.reply("Subskrypcja została wyłączona.")
        else:
            await message.reply("Nie byłeś subskrybentem.")

    @dp.message_handler(commands=["status"])
    async def cmd_status(message: types.Message):
        await message.reply(f"Zapisane oferty: {len(seen_offers)}\nSubskrybentów: {len(subscribers)}")

    @dp.message_handler(commands=["addquery"])
    async def cmd_addquery(message: types.Message):
        args = message.get_args()
        if not args:
            await message.reply("Użycie: /addquery <url lub fraza wyszukiwania>")
            return
        SEARCH_QUERIES.append(args)
        await message.reply(f"Dodano zapytanie: {args}. Restart bota nie jest wymagany.")


# ---------------------- Self-tests ----------------------

def run_self_tests():
    """Run quick unit tests for parser and price normalization."""
    print("Running self-tests...")

    # normalize_price tests
    assert normalize_price("1 200 zł") == 1200, "Failed parse '1 200 zł'"
    assert normalize_price("PLN 2,345.67") == 2345, "Failed parse 'PLN 2,345.67'"
    assert normalize_price("~ 999") == 999
    assert normalize_price("") is None

    # parse_offers_from_html tests
    sample_html = '''
    <html>
      <body>
        <a href="/d/oferta/iphone-11-dobry-stan-1200-123456/">
          <h3>iPhone 11 - dobry stan</h3>
          <span class="price">1 200 zł</span>
          <p class="css-6safw6">bez blokad</p>
        </a>
      </body>
    </html>
    '''
    offers = parse_offers_from_html(sample_html)
    assert len(offers) == 1, f"Expected 1 offer, got {len(offers)}"
    o = offers[0]
    # ID is taken from the URL's trailing segment
    assert o['id'] == '123456', f"Unexpected id parsed: {o['id']}"
    assert 'iphone' in o['title'].lower()
    assert o['price'] == 1200

    # matches_price_threshold tests (based on PRICE_THRESHOLDS above)
    assert matches_price_threshold('iPhone 11 super', 350) is True
    assert matches_price_threshold('iPhone 11 super', 351) is False

    print("All self-tests passed.")


# ---------------------- Entrypoint ----------------------

def print_ssl_help_and_exit():
    msg = """
ERROR: The Python 'ssl' module is not available in this environment.

This is required by networking libraries used in this script (aiohttp, aiogram).
Common causes:
  - Python was built from source without OpenSSL headers available during build.
  - Using a minimal/sandboxed Python image that omitted ssl support.

How to fix (examples):
  - Debian/Ubuntu (when building Python from source):
      sudo apt-get update
      sudo apt-get install -y libssl-dev openssl libbz2-dev libreadline-dev libsqlite3-dev libffi-dev zlib1g-dev build-essential
      # then rebuild/reinstall Python

  - Alpine Linux:
      apk add --no-cache openssl-dev libffi-dev bzip2-dev zlib-dev
      # rebuild Python

  - Windows: use the official Python installer from python.org (it includes SSL), or use Anaconda/Miniconda.

  - Easier alternative: use a prebuilt Python distribution (system packages, pyenv, conda) that includes ssl.

If you can't change the environment you can still run the built-in tests without network:
    python3 bot_olx_iphone.py --test

Once you have Python with ssl support, install dependencies and run the bot:
    pip install -U pip
    pip install aiogram aiohttp beautifulsoup4
    python3 bot_olx_iphone.py

Note: if your bot token was exposed while debugging, consider rotating it in BotFather.
"""
    print(msg)
    sys.exit(1)


def main():
    parser = argparse.ArgumentParser(description='OLX iPhone Telegram bot')
    parser.add_argument('--test', action='store_true', help='Run self-tests and exit')
    args = parser.parse_args()

    if args.test:
        run_self_tests()
        return

    if not AIOLIB_AVAILABLE:
        print_ssl_help_and_exit()

    # Network mode: start bot
    save_json(SEEN_FILE, seen_offers)
    save_json(SUBSCRIBERS_FILE, subscribers)

    loop = asyncio.get_event_loop()
    loop.create_task(polling_loop())
    executor.start_polling(dp, skip_updates=True)


if __name__ == '__main__':
    main()
