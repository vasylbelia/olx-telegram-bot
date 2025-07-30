import asyncio
import logging
from aiogram import Bot, Dispatcher, types
from aiogram.types import ParseMode
from aiogram.utils import executor
import aiohttp
from bs4 import BeautifulSoup

API_TOKEN = 'AAFuAU6PXvKx0X56GzynTZ7C27V5HvrcIP4'

logging.basicConfig(level=logging.INFO)
bot = Bot(token=API_TOKEN)
dp = Dispatcher(bot)

URL = "https://www.olx.pl/oferty/q-iphone/?search%5Border%5D=created_at:desc"

async def fetch_ads():
    async with aiohttp.ClientSession() as session:
        async with session.get(URL) as response:
            html = await response.text()
            soup = BeautifulSoup(html, "html.parser")
            items = soup.select(".css-1sw7q4x")
            ads = []
            for item in items[:5]:
                title = item.select_one("h6")
                link = item.get("href")
                if title and link:
                    ads.append(f"{title.text.strip()}\nhttps://www.olx.pl{link}")
            return ads

@dp.message_handler(commands=['start'])
async def start_handler(message: types.Message):
    await message.reply("Cześć! Będę monitorował nowe oferty iPhone na OLX.")

async def periodic_check():
    await bot.wait_until_ready()
    while True:
        ads = await fetch_ads()
        for ad in ads:
            await bot.send_message(chat_id=YOUR_CHAT_ID, text=ad)
        await asyncio.sleep(600)

if __name__ == '__main__':
    loop = asyncio.get_event_loop()
    loop.create_task(periodic_check())
    executor.start_polling(dp, skip_updates=True)
