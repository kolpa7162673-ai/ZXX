"""
background.py — фоновые циклы POMA: напоминания, ежедневный дайджест,
RSS-поллинг, автобэкап БД, health-check.
Каждый цикл — отдельная asyncio-задача, запускается из userbot.py через
asyncio.create_task(...).
"""

import asyncio
import time
import os
import aiohttp
import xml.etree.ElementTree as ET

import storage

DIGEST_HOUR_UTC = int(os.getenv("DIGEST_HOUR_UTC", "6"))  # во сколько (UTC) слать дайджест
HEALTHCHECK_INTERVAL = int(os.getenv("HEALTHCHECK_INTERVAL_SEC", str(3600)))
BACKUP_INTERVAL = int(os.getenv("BACKUP_INTERVAL_SEC", str(86400)))
RSS_POLL_INTERVAL = int(os.getenv("RSS_POLL_INTERVAL_SEC", str(900)))


async def reminders_loop(bot):
    """Проверяет просроченные напоминания каждые 30 секунд и шлёт их."""
    while True:
        try:
            now_ts = int(time.time())
            for r in storage.due_reminders(now_ts):
                try:
                    await bot.send_message(r["chat_id"], f"⏰ Напоминание: {r['text']}")
                except Exception:
                    pass
                storage.mark_reminder_done(r["id"])
        except Exception as e:
            print(f"[reminders_loop] ошибка: {e}")
        await asyncio.sleep(30)


async def daily_digest_loop(bot, build_digest_fn, log_chat_id):
    """Раз в сутки, в DIGEST_HOUR_UTC, шлёт дайджест в группу логов."""
    last_sent_day = None
    while True:
        try:
            now = time.gmtime()
            today = time.strftime("%Y-%m-%d", now)
            if now.tm_hour == DIGEST_HOUR_UTC and last_sent_day != today:
                text = await build_digest_fn()
                if text:
                    await bot.send_message(log_chat_id, text)
                storage.prune_digest_events(older_than_days=7)
                last_sent_day = today
        except Exception as e:
            print(f"[daily_digest_loop] ошибка: {e}")
        await asyncio.sleep(300)  # проверка раз в 5 минут достаточно


async def rss_loop(bot, log_chat_id):
    """Раз в RSS_POLL_INTERVAL секунд проверяет добавленные RSS-ленты."""
    while True:
        try:
            feeds = storage.list_feeds()
            for feed in feeds:
                try:
                    async with aiohttp.ClientSession() as session:
                        async with session.get(feed["url"], timeout=15) as resp:
                            raw = await resp.text()
                    root = ET.fromstring(raw)
                    items = root.findall(".//item")
                    if not items:
                        continue
                    newest = items[0]
                    guid_el = newest.find("guid")
                    title_el = newest.find("title")
                    link_el = newest.find("link")
                    guid = (guid_el.text if guid_el is not None else None) or (
                        link_el.text if link_el is not None else ""
                    )
                    if guid and guid != feed["last_guid"]:
                        title = title_el.text if title_el is not None else "Новая запись"
                        link = link_el.text if link_el is not None else ""
                        await bot.send_message(log_chat_id, f"📰 {title}\n{link}")
                        storage.update_feed_guid(feed["url"], guid)
                except Exception as e:
                    print(f"[rss_loop] ошибка ленты {feed['url']}: {e}")
        except Exception as e:
            print(f"[rss_loop] ошибка: {e}")
        await asyncio.sleep(RSS_POLL_INTERVAL)


async def backup_loop(bot, log_chat_id):
    """Раз в сутки шлёт файл БД в группу логов как бэкап."""
    while True:
        try:
            await asyncio.sleep(BACKUP_INTERVAL)
            if os.path.exists(storage.DB_PATH):
                await bot.send_file(
                    log_chat_id,
                    storage.DB_PATH,
                    caption=f"🗄 Автобэкап БД POMA — {time.strftime('%Y-%m-%d %H:%M UTC')}",
                )
        except Exception as e:
            print(f"[backup_loop] ошибка: {e}")


async def healthcheck_loop(bot, log_chat_id):
    """Раз в HEALTHCHECK_INTERVAL секунд шлёт короткий статус в группу логов."""
    while True:
        try:
            await asyncio.sleep(HEALTHCHECK_INTERVAL)
            await bot.send_message(
                log_chat_id,
                f"✅ POMA жива — {time.strftime('%Y-%m-%d %H:%M UTC')}",
            )
        except Exception as e:
            print(f"[healthcheck_loop] ошибка: {e}")
