import os
import re
import time
import random
import asyncio
import aiohttp
import dateparser
from openai import OpenAI
from telethon import TelegramClient, events, utils
from telethon.sessions import StringSession
from telethon.tl.types import MessageService
from telethon.tl.functions.channels import CreateChannelRequest
from telethon.tl.functions.messages import SendReactionRequest
from telethon.tl.types import ReactionEmoji
from dotenv import load_dotenv

import storage
import background

load_dotenv()

API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
SESSION = os.getenv("SESSION")  # на Railway обязательно; локально можно не указывать
GROQ_API_KEY = os.getenv("GROQ_API_KEY")

client_ai = OpenAI(
    api_key=GROQ_API_KEY,
    base_url="https://api.groq.com/openai/v1"
)

if SESSION:
    bot = TelegramClient(StringSession(SESSION), API_ID, API_HASH)
else:
    bot = TelegramClient("my_userbot", API_ID, API_HASH)

storage.init_db()

conversations = {}
MAX_HISTORY = 12
ai_reply_ids = set()
menu_reply_ids = {}  # msg_id -> "main" | номер категории (для навигации по .меню)

MENU_CATEGORIES = {
    1: ("Основное (ИИ)", "📌", [".расскажи", ".объясни", ".кратко / .tldr", ".факт", ".шутка", ".идея", ".перевод"]),
    2: ("Код", "💻", [".код", ".bug", ".review / .ревью", ".regex", ".sql", ".тест", ".оптимизация"]),
    3: ("Стиль и контент", "🎨", [".роль", ".стиль", ".мем", ".цитата", ".хук", ".тред", ".cta", ".био", ".спор", ".разбор / .ору", ".сравни", ".имя", ".план", ".промпт", ".элия"]),
    4: ("NFT / Web3", "🖼", [".nft", ".коллекция", ".trait", ".roadmap", ".утилита", ".нейминг", ".mint"]),
    5: ("Учёба и работа", "💼", [".todo (ИИ-чеклист)", ".письмо", ".собес", ".термин", ".конспект"]),
    6: ("Заметки и задачи", "📝", [".заметка текст", ".заметки", ".удали_заметку id", ".задача текст", ".задачи", ".готово id"]),
    7: ("Контакты (CRM)", "👤", [".контакт @user заметка", ".контакт_инфо @user", ".контакты"]),
    8: ("Напоминания и отложка", "⏰", [".напомни через 2 часа текст", ".напоминания", ".отмени_напоминание id", ".отложи через 1 час @user текст"]),
    9: ("Мониторинг и приватность", "🔔", [".следи слово", ".не_следи слово", ".слежка", ".сводка", ".игнор [chat_id]", ".не_игнор [chat_id]", ".не_логируй"]),
    10: ("RSS и бэкап", "📡", [".rss_добавь url", ".rss_список", ".rss_удали url", ".бэкап"]),
    11: ("Активность чата", "📊", [".актив", ".молчуны"]),
    12: ("Голос и АФК", "🎙", [".распознай (ответом на войс)", ".афк вкл [текст]", ".афк выкл"]),
    13: ("Утилиты", "🎲", [".рандом 1-100", ".выбери a | b | c", ".cat", ".dog", ".info", ".a_troll", ".сброс", ".реакции (рандомные реакции в чате)"]),
    14: ("Сохранение и ссылки", "💾", [".сохрани (ответом на медиа)", ".суммаризируй ссылка"]),
}

# ---------------- runtime state (не персистентное, живёт в памяти процесса) ----------------
AFK_MODE = storage.get_setting("afk_enabled", "0") == "1"
AFK_TEXT = storage.get_setting("afk_text", "Сейчас недоступен, отвечу позже.")
_afk_recent_replies = {}  # user_id -> timestamp последнего автоответа (антиспам одному человеку)
_edit_throttle_lock = asyncio.Semaphore(3)  # ограничение параллельных msg.edit() (антифлуд-бан)
SAVES_DIR = "saved_media"
os.makedirs(SAVES_DIR, exist_ok=True)

RANDOM_REACTION_EMOJIS = ["👍", "🔥", "❤️", "😁", "🎉", "🤔", "👏", "😱", "🤯", "💯", "🥰", "😍"]
RANDOM_REACTION_CHANCE = 1 / 30  # в среднем 1 реакция на 30 сообщений


async def send_reaction(chat, msg_id: int, emoji: str):
    """Ставит реакцию-эмодзи на сообщение. Работает и для своих сообщений
    (подтверждение команды), и для чужих (рандомные реакции в чате)."""
    try:
        await bot(SendReactionRequest(
            peer=chat,
            msg_id=msg_id,
            reaction=[ReactionEmoji(emoticon=emoji)],
        ))
    except Exception as e:
        print(f"[reaction] не удалось поставить реакцию: {e}")


async def react_ok(event):
    await send_reaction(event.chat_id, event.id, "✅")


async def react_fail(event):
    await send_reaction(event.chat_id, event.id, "❌")


def progress_bar(done: int, total: int, width: int = 10) -> str:
    if total == 0:
        return "▱" * width + " 0/0"
    filled = round(width * done / total)
    return "▰" * filled + "▱" * (width - filled) + f" {done}/{total}"

# ---------------- лог-группа (создаётся автоматически) ----------------
LOG_LEVEL = int(os.getenv("LOG_LEVEL", "1"))  # 0 = не логировать вообще
LOG_CHAT_ID = None  # заполняется в ensure_log_chat() при старте


# ===================== ИИ =====================

async def ask_ai(prompt: str, chat_id: int = None, system: str = None) -> str:
    system_text = system or (
        "Ты весёлый и полезный ассистент. Отвечай на русском коротко и по делу, с лёгким юмором. "
        "Если нужно уточнение — один короткий вопрос. В диалоге опирайся на контекст."
    )
    messages = [{"role": "system", "content": system_text}]
    if chat_id is not None and chat_id in conversations:
        messages.extend(conversations[chat_id])
    messages.append({"role": "user", "content": prompt})

    last_err = None
    for attempt in range(3):
        try:
            response = client_ai.chat.completions.create(
                model="openai/gpt-oss-120b",
                messages=messages,
                max_tokens=1200
            )
            answer = response.choices[0].message.content
            if chat_id is not None:
                history = conversations.setdefault(chat_id, [])
                history.append({"role": "user", "content": prompt})
                history.append({"role": "assistant", "content": answer})
                if len(history) > MAX_HISTORY:
                    conversations[chat_id] = history[-MAX_HISTORY:]
            return answer
        except Exception as e:
            last_err = e
            print(f"[ask_ai] попытка {attempt + 1} не удалась: {e}")
            await asyncio.sleep(1.5 * (2 ** attempt))
    return f"Ошибка ИИ после нескольких попыток: {last_err}"


def track_menu_msg(msg_id, value):
    menu_reply_ids[msg_id] = value
    if len(menu_reply_ids) > 300:
        for k in list(menu_reply_ids.keys())[:-200]:
            menu_reply_ids.pop(k, None)


def track_ai_msg(msg):
    if not msg:
        return
    ai_reply_ids.add(msg.id)
    if len(ai_reply_ids) > 500:
        trimmed = list(ai_reply_ids)[-300:]
        ai_reply_ids.clear()
        ai_reply_ids.update(trimmed)


async def thinking_animation(event):
    frames = [
        "▰▱▱▱▱▱▱▱  `12%`  💭",
        "▰▰▱▱▱▱▱▱  `25%`  🔍",
        "▰▰▰▰▱▱▱▱  `50%`  ⚙️",
        "▰▰▰▰▰▰▱▱  `75%`  ⚡",
        "▰▰▰▰▰▰▰▰  `100%` ✨",
    ]
    msg = await event.reply(frames[0])
    for frame in frames[1:]:
        try:
            await asyncio.sleep(0.25)
            await msg.edit(frame)
        except Exception:
            break
    return msg


async def type_into_message(msg, full_text: str, chunk_size: int = 32, delay: float = 0.06):
    """Дописывает текст в уже существующее сообщение (эффект печати)."""
    full_text = full_text or ""
    if not full_text.strip():
        try:
            await msg.edit("Пустой ответ")
        except Exception:
            pass
        track_ai_msg(msg)
        return msg

    # короткое — сразу
    if len(full_text) <= 60:
        try:
            await msg.edit(full_text)
        except Exception:
            pass
        track_ai_msg(msg)
        return msg

    # для кода — чуть крупнее куски (быстрее и меньше flood)
    if "```" in full_text or "def " in full_text or "function " in full_text:
        chunk_size = max(chunk_size, 48)
        delay = min(delay, 0.05)

    pos = 0
    while pos < len(full_text):
        pos = min(pos + chunk_size, len(full_text))
        current = full_text[:pos]
        suffix = " ▌" if pos < len(full_text) else ""
        try:
            async with _edit_throttle_lock:
                await msg.edit(current + suffix)
            await asyncio.sleep(delay)
        except Exception:
            break

    try:
        await msg.edit(full_text)
    except Exception:
        pass

    track_ai_msg(msg)
    return msg


async def ask_ai_animated(event, prompt: str, chat_id: int = None, system: str = None, typewriter: bool = True):
    """
    1) анимация прогресса
    2) запрос к ИИ
    3) печать ответа в то же сообщение
    """
    msg = await thinking_animation(event)
    answer = await ask_ai(prompt, chat_id=chat_id, system=system)

    if typewriter:
        await type_into_message(msg, answer)
    else:
        try:
            await msg.edit(answer)
            track_ai_msg(msg)
        except Exception:
            m = await event.reply(answer)
            track_ai_msg(m)
    return answer


async def get_prompt(event, prefix: str) -> str:
    text = (event.raw_text or "").strip()
    after = text[len(prefix):].strip()
    if event.is_reply:
        replied_msg = await event.get_reply_message()
        if replied_msg and replied_msg.raw_text:
            replied = replied_msg.raw_text.strip()
            if after:
                return f"{after}\n\nТекст сообщения:\n{replied}"
            return replied
    return after


# ---------------- красивое оформление списков ----------------
_SEP = "┄" * 18


def fmt_panel(title: str, emoji: str, lines: list, footer: str = None, empty_text: str = "Пока пусто") -> str:
    """Единый стиль для всех списков: заголовок, разделитель, пункты, подвал.
    Если lines пуст — аккуратное сообщение вместо голого текста."""
    if not lines:
        return f"{emoji} **{title}**\n{_SEP}\n_{empty_text}_"
    body = "\n".join(lines)
    out = f"{emoji} **{title}**\n{_SEP}\n{body}"
    if footer:
        out += f"\n{_SEP}\n· _{footer}_"
    return out


def fmt_id(n) -> str:
    """Номер записи в моноширинном виде — визуально отделяет id от текста."""
    return f"`#{n}`"


async def build_digest() -> str:
    """Собирает дайджест: события слежки за ключевыми словами за последние 24 часа
    + список активных напоминаний + количество открытых задач."""
    since = int(time.time()) - 86400
    events_ = storage.pull_digest_events(since)
    lines = []

    for ev in events_[:15]:
        lines.append(f"🔔 [{ev['tag']}] {ev['text'][:120]}")

    tasks = storage.list_tasks()
    if tasks:
        lines.append(f"✅ Открытых задач: `{len(tasks)}`")

    rems = storage.list_pending_reminders()
    if rems:
        lines.append(f"⏰ Активных напоминаний: `{len(rems)}`")

    if not lines:
        return ""
    return fmt_panel("ДАЙДЖЕСТ ДНЯ", "📊", lines)


async def ensure_log_chat():
    """Проверяет сохранённый в БД чат для логов; если его нет (первый запуск
    или чат стал недоступен) — создаёт новую супергруппу «POMA Logs» только с
    самим собой и запоминает её id, чтобы использовать при следующих запусках."""
    global LOG_CHAT_ID
    saved_id = storage.get_setting("log_chat_id")
    if saved_id:
        try:
            entity = await bot.get_entity(int(saved_id))
            LOG_CHAT_ID = int(saved_id)
            return entity
        except Exception:
            pass  # чат удалён/недоступен — создадим новый ниже

    result = await bot(CreateChannelRequest(
        title="POMA Logs",
        about="Автосозданный чат для логов и уведомлений POMA (антиудаление, RSS, дайджест, бэкапы)",
        megagroup=True,
    ))
    new_chat = result.chats[0]
    marked_id = utils.get_peer_id(new_chat)
    storage.set_setting("log_chat_id", str(marked_id))
    LOG_CHAT_ID = marked_id
    return new_chat


async def _send_log(text: str = None, file: str = None, caption: str = None):
    try:
        if LOG_CHAT_ID is None:
            await ensure_log_chat()
        if file:
            await bot.send_file(LOG_CHAT_ID, file, caption=caption or text or "")
        else:
            await bot.send_message(LOG_CHAT_ID, text)
    except Exception as e:
        print(f"[log] не удалось отправить: {e}")


async def _log(text: str = None, level: int = 1, file: str = None, caption: str = None):
    """Логирование в группу POMA Logs. level: чем выше, тем менее важное
    сообщение — если LOG_LEVEL меньше level, сообщение пропускается."""
    if LOG_LEVEL < level:
        return
    try:
        asyncio.create_task(_send_log(text=text, file=file, caption=caption))
    except Exception:
        pass


async def send_animal(event, api_url: str, ok_caption: str, fail_text: str):
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(api_url) as resp:
                data = await resp.json()
                if isinstance(data, list):
                    url = data[0]["url"]
                elif isinstance(data, dict) and "message" in data:
                    url = data["message"]
                else:
                    url = data.get("url")
        await event.reply(ok_caption, file=url)
    except Exception:
        await event.reply(fail_text)


# ===================== АНТИУДАЛЕНИЕ / АНТИРЕДАКТИРОВАНИЕ =====================

@bot.on(events.NewMessage())
async def _track_for_antidelete(event):
    """Запоминаем сообщения только из личных чатов (не групп/каналов) —
    антиудаление/антиредактирование работает только в личке."""
    try:
        if not event.is_private:
            return
        if storage.is_no_log(event.chat_id):
            return
        text = event.raw_text or ""
        has_media = bool(event.message.media)
        storage.track_message(event.id, event.chat_id, event.sender_id, text, has_media)
    except Exception:
        pass


@bot.on(events.MessageEdited())
async def _on_edit(event):
    try:
        if not event.is_private:
            return
        if storage.is_no_log(event.chat_id):
            return
        old = storage.get_tracked_message(event.id, event.chat_id)
        new_text = event.raw_text or ""
        if old and old["text"] and old["text"] != new_text:
            chat = await event.get_chat()
            chat_name = getattr(chat, "title", None) or getattr(chat, "first_name", "чат")
            await _log(
                f"✏️ **Сообщение отредактировано** в «{chat_name}»\n\n"
                f"Было:\n{old['text']}\n\nСтало:\n{new_text}",
                level=1,
            )
        storage.track_message(event.id, event.chat_id, event.sender_id, new_text, bool(event.message.media))
    except Exception as e:
        print(f"[antiedit] ошибка: {e}")


@bot.on(events.MessageDeleted())
async def _on_delete(event):
    """Важно: Telegram для приватных чатов и обычных (не супергрупп) групп
    НЕ передаёт chat_id в событии удаления — это ограничение самого API,
    а не наше. Поэтому если chat_id есть — это супергруппа/канал, и мы её
    сознательно игнорируем (антиудаление только для личных чатов).
    Если chat_id отсутствует — ищем совпадение по msg_id среди сообщений,
    которые мы уже трекали (а трекаем теперь только личку)."""
    try:
        for msg_id in event.deleted_ids:
            if event.chat_id is not None:
                continue  # это супергруппа/канал — не личка, пропускаем

            old = storage.find_tracked_message_by_id(msg_id)
            if not old:
                continue
            if storage.is_no_log(old["chat_id"]):
                continue
            if not (old["text"] or old["has_media"]):
                continue

            try:
                chat = await bot.get_entity(old["chat_id"])
                chat_name = getattr(chat, "first_name", None) or str(old["chat_id"])
            except Exception:
                chat_name = str(old["chat_id"])

            body = old["text"] or "(медиа без текста)"
            await _log(f"🗑 **Сообщение удалено** в личке с «{chat_name}»\n\n{body}", level=1)
    except Exception as e:
        print(f"[antidelete] ошибка: {e}")


# ===================== ВХОДЯЩИЕ: АФК / МОНИТОРИНГ КЛЮЧЕВЫХ СЛОВ / АКТИВНОСТЬ =====================

@bot.on(events.NewMessage(incoming=True))
async def _incoming_handler(event):
    global AFK_MODE, AFK_TEXT
    try:
        chat_id = event.chat_id
        sender_id = event.sender_id
        text = event.raw_text or ""

        # игнор-лист: полностью пропускаем чат, если не найдено исключение по важности
        ignored = storage.is_ignored(chat_id)
        important = False
        if ignored:
            if ignored["ai_exception"] and text:
                check = await ask_ai(
                    f"Ответь ТОЛЬКО 'да' или 'нет'. Это сообщение реально важное/срочное "
                    f"(упоминание имени, просьба о помощи, что-то критичное)?\n\n{text}",
                    chat_id=None,
                )
                important = "да" in (check or "").lower()[:10]
            if not important:
                return  # чат в игноре и ничего важного — молча выходим

        # трекинг активности для .актив/.молчуны (только группы/каналы, не личка)
        if event.is_group or event.is_channel:
            day = time.strftime("%Y-%m-%d", time.gmtime())
            storage.bump_activity(chat_id, sender_id, day)

        # мониторинг ключевых слов
        if text:
            watches = storage.list_watches()
            lower_text = text.lower()
            for kw in watches:
                if kw in lower_text:
                    storage.add_digest_event(chat_id, sender_id, text, tag="keyword")
                    try:
                        chat = await event.get_chat()
                        chat_name = getattr(chat, "title", None) or getattr(chat, "first_name", "чат")
                    except Exception:
                        chat_name = str(chat_id)
                    await _log(f"🔔 Слово «{kw}» упомянуто в «{chat_name}»:\n{text}", level=1)
                    break

        # рандомные реакции (если включены командой .реакции для этого чата)
        if storage.is_random_reactions(chat_id) and random.random() < RANDOM_REACTION_CHANCE:
            emoji = random.choice(RANDOM_REACTION_EMOJIS)
            await send_reaction(chat_id, event.id, emoji)

        # АФК-автоответчик
        if AFK_MODE and event.is_private and not event.out:
            last = _afk_recent_replies.get(sender_id, 0)
            if time.time() - last < 600:  # не спамим одному человеку чаще раза в 10 минут
                return
            urgency = "не срочно"
            if text:
                verdict = await ask_ai(
                    f"Ответь ОДНИМ словом: 'срочно' или 'обычно'. Оцени срочность сообщения "
                    f"для человека, который сейчас недоступен:\n\n{text}",
                    chat_id=None,
                )
                if verdict and "срочно" in verdict.lower():
                    urgency = "срочно"
            reply_text = AFK_TEXT
            if urgency == "срочно":
                reply_text += "\n\n(это сообщение помечено как срочное — я отдельно уведомлён)"
                try:
                    sender = await event.get_sender()
                    name = getattr(sender, "first_name", "Кто-то")
                except Exception:
                    name = "Кто-то"
                await _log(f"⚡ Похоже на срочное сообщение от {name}:\n{text}", level=1)
            await event.reply(reply_text)
            _afk_recent_replies[sender_id] = time.time()
    except Exception as e:
        print(f"[incoming_handler] ошибка: {e}")


# ===================== HANDLER =====================

@bot.on(events.NewMessage(outgoing=True))
async def handler(event):
    text = (event.raw_text or "").strip()
    if not text:
        return

    lower = text.lower()
    chat_id = event.chat_id

    # ---------- ОСНОВНОЕ ----------
    if lower.startswith(".расскажи"):
        prompt = await get_prompt(event, ".расскажи") or "Расскажи что-нибудь интересное и короткое"
        await ask_ai_animated(event, prompt, chat_id=chat_id)
        return

    if lower.startswith(".код"):
        prompt = await get_prompt(event, ".код") or "Напиши простой пример"
        await ask_ai_animated(
            event,
            f"Напиши рабочий код по задаче: {prompt}. Только код + очень короткое объяснение.",
            chat_id=chat_id,
            system="Senior-разработчик. Отвечай на русском. Рабочий код + короткое объяснение.",
            typewriter=True,
        )
        return

    if lower.startswith(".объясни"):
        prompt = await get_prompt(event, ".объясни")
        if not prompt:
            await event.reply("Напиши: `.объясни тема` или ответь на сообщение")
            return
        await ask_ai_animated(event, f"Объясни простыми словами:\n{prompt}", chat_id=chat_id)
        return

    if lower.startswith(".кратко") or lower.startswith(".tldr") or lower.startswith(".tl;dr"):
        prompt = None
        for p in (".кратко", ".tldr", ".tl;dr"):
            if lower.startswith(p):
                prompt = await get_prompt(event, p)
                break
        if not prompt:
            await event.reply("Ответь на сообщение: `.кратко`")
            return
        await ask_ai_animated(event, f"Кратко перескажи (2–4 предложения):\n{prompt}", chat_id=chat_id)
        return

    if lower.startswith(".факт"):
        topic = await get_prompt(event, ".факт") or "случайный интересный факт"
        await ask_ai_animated(event, f"Один короткий интересный факт: {topic}", chat_id=chat_id)
        return

    if lower.startswith(".шутка") or lower.startswith(".joke"):
        topic = text.split(maxsplit=1)[1] if " " in text else "общая"
        await ask_ai_animated(event, f"Одна короткая шутка на тему: {topic}", chat_id=chat_id)
        return

    if lower.startswith(".идея"):
        topic = await get_prompt(event, ".идея") or "что угодно"
        await ask_ai_animated(event, f"3 креативные идеи на тему: {topic}. Списком.", chat_id=chat_id)
        return

    if lower.startswith(".перевод"):
        prompt = await get_prompt(event, ".перевод")
        if not prompt:
            await event.reply("`.перевод текст` или ответом на сообщение")
            return
        await ask_ai_animated(
            event,
            f"Переведи на русский, если не русский; если русский — на английский. Только перевод:\n{prompt}",
            chat_id=chat_id,
        )
        return

    # ---------- СТИЛЬ / КОНТЕНТ ----------
    if lower.startswith(".роль"):
        rest = text[5:].strip()
        if not rest:
            await event.reply("Пример: `.роль пират Почему небо голубое`")
            return
        if "|" in rest:
            role, topic = [x.strip() for x in rest.split("|", 1)]
        else:
            parts = rest.split(maxsplit=1)
            role = parts[0]
            topic = parts[1] if len(parts) > 1 else await get_prompt(event, ".роль")
            if not topic or topic == rest:
                await event.reply("Укажи тему после роли")
                return
        await ask_ai_animated(
            event,
            f"Ответь полностью в роли «{role}». Тема: {topic}. Не выходи из роли.",
            chat_id=chat_id,
        )
        return

    if lower.startswith(".спор"):
        topic = await get_prompt(event, ".спор")
        if not topic:
            await event.reply("`.спор тема`")
            return
        await ask_ai_animated(
            event,
            f"Тема: {topic}\n✅ 3 аргумента ЗА\n❌ 3 ПРОТИВ\nКороткий вывод.",
            chat_id=chat_id,
        )
        return

    if lower.startswith(".разбор") or lower.startswith(".ору"):
        pref = ".разбор" if lower.startswith(".разбор") else ".ору"
        prompt = await get_prompt(event, pref)
        if not prompt:
            await event.reply(f"Ответь на сообщение: `{pref}`")
            return
        tone = "эмоционально и ярко, но по делу" if pref == ".ору" else "спокойно и структурно"
        await ask_ai_animated(
            event,
            f"Разбор ({tone}):\n{prompt}\n\n1) Суть\n2) Сильное\n3) Слабое\n4) Вердикт",
            chat_id=chat_id,
        )
        return

    if lower.startswith(".стиль"):
        rest = text[6:].strip()
        body, style = "", rest
        if event.is_reply:
            replied = await event.get_reply_message()
            body = (replied.raw_text or "").strip() if replied else ""
            style = rest or "интереснее"
        else:
            parts = rest.split(maxsplit=1)
            if len(parts) < 2:
                await event.reply("`.стиль мемно текст` или ответом: `.стиль официально`")
                return
            style, body = parts[0], parts[1]
        if not body:
            await event.reply("Нет текста")
            return
        await ask_ai_animated(
            event,
            f"Перепиши в стиле «{style}». Только результат:\n{body}",
            chat_id=chat_id,
        )
        return

    if lower.startswith(".мем") or lower.startswith(".rofl"):
        pref = ".мем" if lower.startswith(".мем") else ".rofl"
        prompt = await get_prompt(event, pref) or "случайный мемный комментарий"
        await ask_ai_animated(
            event,
            f"Ответь максимально мемно и коротко по теме:\n{prompt}",
            chat_id=chat_id,
        )
        return

    if lower.startswith(".цитата"):
        prompt = await get_prompt(event, ".цитата")
        if not prompt:
            await event.reply("`.цитата текст` или ответом на сообщение")
            return
        await ask_ai_animated(
            event,
            f"Оформи как красивую цитату (1–2 варианта):\n{prompt}",
            chat_id=chat_id,
        )
        return

    if lower.startswith(".имя"):
        topic = await get_prompt(event, ".имя") or "игровой ник"
        await ask_ai_animated(
            event,
            f"8 крутых ников/названий для: {topic}. Списком.",
            chat_id=chat_id,
        )
        return

    if lower.startswith(".план"):
        topic = await get_prompt(event, ".план")
        if not topic:
            await event.reply("`.план цель`")
            return
        await ask_ai_animated(event, f"Практичный план 5–8 шагов:\n{topic}", chat_id=chat_id)
        return

    if lower.startswith(".сравни"):
        topic = await get_prompt(event, ".сравни")
        if not topic:
            await event.reply("`.сравни A и B`")
            return
        await ask_ai_animated(
            event,
            f"Сравни:\n{topic}\nПлюсы/минусы/когда что выбрать.",
            chat_id=chat_id,
        )
        return

    if lower.startswith(".промпт"):
        topic = await get_prompt(event, ".промпт")
        if not topic:
            await event.reply("`.промпт идея`")
            return
        await ask_ai_animated(
            event,
            f"Готовый сильный промпт для:\n{topic}\nТолько промпт.",
            chat_id=chat_id,
        )
        return

    if lower.startswith(".элия") or lower.startswith(".eli5"):
        prompt = text.split(maxsplit=1)[1] if " " in text else ""
        if not prompt and event.is_reply:
            replied = await event.get_reply_message()
            prompt = (replied.raw_text or "").strip() if replied else ""
        if not prompt:
            await event.reply("`.элия тема`")
            return
        await ask_ai_animated(event, f"Объясни как ребёнку 5 лет:\n{prompt}", chat_id=chat_id)
        return

    if lower.startswith(".хук"):
        prompt = await get_prompt(event, ".хук")
        if not prompt:
            await event.reply("`.хук тема или текст`")
            return
        await ask_ai_animated(event, f"3 цепляющих хука/заголовка для:\n{prompt}", chat_id=chat_id)
        return

    if lower.startswith(".тред"):
        prompt = await get_prompt(event, ".тред")
        if not prompt:
            await event.reply("`.тред тема`")
            return
        await ask_ai_animated(
            event,
            f"Разверни в тред из 5–7 коротких постов:\n{prompt}",
            chat_id=chat_id,
        )
        return

    if lower.startswith(".cta"):
        prompt = await get_prompt(event, ".cta")
        if not prompt:
            await event.reply("`.cta о чём пост`")
            return
        await ask_ai_animated(
            event,
            f"3 сильных CTA (призыва к действию) для:\n{prompt}",
            chat_id=chat_id,
        )
        return

    if lower.startswith(".био"):
        prompt = await get_prompt(event, ".био") or "креативный профиль"
        await ask_ai_animated(
            event,
            f"5 вариантов био (TG/Twitter), коротко, для: {prompt}",
            chat_id=chat_id,
        )
        return

    # ---------- УЧЁБА / РАБОТА ----------
    if lower.startswith(".todo"):
        prompt = await get_prompt(event, ".todo")
        if not prompt:
            await event.reply("`.todo задача`")
            return
        await ask_ai_animated(event, f"Разбей на конкретный чеклист:\n{prompt}", chat_id=chat_id)
        return

    if lower.startswith(".письмо"):
        prompt = await get_prompt(event, ".письмо")
        if not prompt:
            await event.reply("`.письмо что написать`")
            return
        await ask_ai_animated(
            event,
            f"Вежливое деловое сообщение/письмо:\n{prompt}",
            chat_id=chat_id,
        )
        return

    if lower.startswith(".собес"):
        topic = await get_prompt(event, ".собес") or "python junior"
        await ask_ai_animated(
            event,
            f"10 вопросов для собеседования по теме: {topic}. С краткими ответами.",
            chat_id=chat_id,
        )
        return

    if lower.startswith(".термин"):
        prompt = await get_prompt(event, ".термин")
        if not prompt:
            await event.reply("`.термин слово`")
            return
        await ask_ai_animated(
            event,
            f"Термин за 20 секунд + пример:\n{prompt}",
            chat_id=chat_id,
        )
        return

    if lower.startswith(".конспект"):
        prompt = await get_prompt(event, ".конспект")
        if not prompt:
            await event.reply("`.конспект тема/текст`")
            return
        await ask_ai_animated(
            event,
            f"Краткий конспект пунктами:\n{prompt}",
            chat_id=chat_id,
        )
        return

    # ---------- КОД ----------
    if lower.startswith(".bug") or lower.startswith(".ошибка"):
        pref = ".bug" if lower.startswith(".bug") else ".ошибка"
        prompt = await get_prompt(event, pref)
        if not prompt:
            await event.reply("Пришли код/ошибку: `.bug`")
            return
        await ask_ai_animated(
            event,
            f"Найди баг, объясни в 2–3 предложениях, дай исправленный код:\n{prompt}",
            chat_id=chat_id,
            system="Senior-разработчик. Диагноз + рабочий фикс.",
            typewriter=True,
        )
        return

    if lower.startswith(".review") or lower.startswith(".ревью"):
        pref = ".review" if lower.startswith(".review") else ".ревью"
        prompt = await get_prompt(event, pref)
        if not prompt:
            await event.reply("Ответь на код: `.review`")
            return
        await ask_ai_animated(
            event,
            f"Code review:\n{prompt}\n\n✅ Что ок\n⚠️ Проблемы\n💡 Как улучшить",
            chat_id=chat_id,
            system="Конструктивный code reviewer.",
            typewriter=True,
        )
        return

    if lower.startswith(".regex"):
        prompt = await get_prompt(event, ".regex")
        if not prompt:
            await event.reply("`.regex что найти/проверить`")
            return
        await ask_ai_animated(
            event,
            f"Regex + короткое объяснение для:\n{prompt}",
            chat_id=chat_id,
            typewriter=True,
        )
        return

    if lower.startswith(".sql"):
        prompt = await get_prompt(event, ".sql")
        if not prompt:
            await event.reply("`.sql задача`")
            return
        await ask_ai_animated(
            event,
            f"SQL по задаче + коротко поясни:\n{prompt}",
            chat_id=chat_id,
            typewriter=True,
        )
        return

    if lower.startswith(".тест"):
        prompt = await get_prompt(event, ".тест")
        if not prompt:
            await event.reply("`.тест код или функция`")
            return
        await ask_ai_animated(
            event,
            f"Тест-кейсы / пример pytest для:\n{prompt}",
            chat_id=chat_id,
            typewriter=True,
        )
        return

    if lower.startswith(".оптимизация"):
        prompt = await get_prompt(event, ".оптимизация")
        if not prompt:
            await event.reply("`.оптимизация код`")
            return
        await ask_ai_animated(
            event,
            f"Как упростить/ускорить + улучшенный вариант:\n{prompt}",
            chat_id=chat_id,
            typewriter=True,
        )
        return

    # ---------- NFT / WEB3 ----------
    if lower.startswith(".nft"):
        topic = await get_prompt(event, ".nft") or "идея NFT-коллекции"
        await ask_ai_animated(
            event,
            f"Тема NFT: {topic}\n1) Концепт 2) Уникальность 3) Аудитория 4) Utility 5) Риски\nКоротко.",
            chat_id=chat_id,
            system="Эксперт NFT/Web3. По делу, без хайпа.",
        )
        return

    if lower.startswith(".коллекция") or lower.startswith(".collection"):
        pref = ".коллекция" if lower.startswith(".коллекция") else ".collection"
        topic = await get_prompt(event, pref) or "пиксельные животные"
        await ask_ai_animated(
            event,
            f"NFT-коллекция «{topic}»: название, 5 trait-категорий, редкости, 3 примера, roadmap 4 пункта.",
            chat_id=chat_id,
        )
        return

    if lower.startswith(".trait"):
        topic = await get_prompt(event, ".trait") or "пиксельные персонажи"
        await ask_ai_animated(
            event,
            f"Traits и редкости для NFT «{topic}»: категории, значения, % редкостей.",
            chat_id=chat_id,
        )
        return

    if lower.startswith(".roadmap"):
        topic = await get_prompt(event, ".roadmap") or "NFT-проект"
        await ask_ai_animated(
            event,
            f"Реалистичный roadmap 6–8 пунктов для: {topic}",
            chat_id=chat_id,
        )
        return

    if lower.startswith(".утилита"):
        topic = await get_prompt(event, ".утилита") or "NFT-коллекция"
        await ask_ai_animated(event, f"7 идей utility для холдеров: {topic}", chat_id=chat_id)
        return

    if lower.startswith(".нейминг"):
        topic = await get_prompt(event, ".нейминг") or "NFT-коллекция"
        await ask_ai_animated(
            event,
            f"12 названий для: {topic}. Разные стили, списком.",
            chat_id=chat_id,
        )
        return

    if lower.startswith(".mint"):
        topic = await get_prompt(event, ".mint") or "первая коллекция"
        await ask_ai_animated(
            event,
            f"Чеклист mint для «{topic}»: арт, контракт, метаданные, маркетплейс, комьюнити, безопасность.",
            chat_id=chat_id,
        )
        return

    # ---------- УТИЛИТЫ ----------
    if lower.startswith(".рандом") or lower.startswith(".random"):
        rest = text.split(maxsplit=1)
        if len(rest) < 2 or "-" not in rest[1]:
            await event.reply("Пример: `.рандом 1-100`")
            return
        try:
            a, b = rest[1].replace(" ", "").split("-", 1)
            a, b = int(a), int(b)
            if a > b:
                a, b = b, a
            await event.reply(f"🎲 {random.randint(a, b)}")
        except Exception:
            await event.reply("Формат: `.рандом 1-100`")
        return

    if lower.startswith(".выбери"):
        rest = text[7:].strip()
        if "|" not in rest:
            await event.reply("Пример: `.выбери пицца | суши | бургер`")
            return
        options = [x.strip() for x in rest.split("|") if x.strip()]
        if len(options) < 2:
            await event.reply("Нужно минимум 2 варианта через |")
            return
        await event.reply(f"🎯 {random.choice(options)}")
        return

    if lower == ".cat":
        await send_animal(
            event,
            "https://api.thecatapi.com/v1/images/search",
            "🐱 Вот тебе кот",
            "Не удалось получить кота 😿",
        )
        return

    if lower == ".dog":
        await send_animal(
            event,
            "https://dog.ceo/api/breeds/image/random",
            "🐶 Вот тебе пёс",
            "Не удалось получить собаку 😢",
        )
        return

    if lower == ".info":
        sender = await event.get_sender()
        await event.reply(
            f"👤 **Информация**\n\n"
            f"Имя: {sender.first_name}\n"
            f"Username: @{sender.username or 'нет'}\n"
            f"ID: `{sender.id}`"
        )
        return

    if lower.startswith(".spam "):
        spam_text = text[6:].strip()
        if not spam_text:
            await event.reply("`.spam текст`")
            return
        for _ in range(5):
            await event.respond(spam_text)
            await asyncio.sleep(0.4)
        return

    if lower == ".a_troll":
        trolls = [
            "Ты серьёзно это написал?",
            "Ого, какой умный...",
            "Дальше будет ещё смешнее",
            "Я бы на твоём месте помолчал",
            "Классика жанра",
        ]
        await event.reply(random.choice(trolls))
        return

    if lower in (".сброс", ".reset", ".clear"):
        conversations.pop(chat_id, None)
        await event.reply("Память диалога очищена 🧹")
        return

    # ---------- ГОЛОС ----------
    if lower.startswith(".распознай"):
        if not event.is_reply:
            await event.reply("Ответь этой командой на голосовое/аудио сообщение")
            return
        replied = await event.get_reply_message()
        if not replied or not replied.media:
            await event.reply("В отвеченном сообщении нет аудио")
            return
        status = await event.reply("🎙 Распознаю...")
        try:
            path = await replied.download_media(file="voice_tmp")
            with open(path, "rb") as f:
                transcript = client_ai.audio.transcriptions.create(
                    model="whisper-large-v3",
                    file=f,
                )
            os.remove(path)
            await status.edit(f"📝 Расшифровка:\n\n{transcript.text}")
        except Exception as e:
            await status.edit(f"Не удалось распознать: {e}")
        return

    # ---------- АФК ----------
    if lower.startswith(".афк"):
        global AFK_MODE, AFK_TEXT
        rest = text[4:].strip()
        if rest.startswith("вкл"):
            AFK_MODE = True
            custom = rest[3:].strip()
            if custom:
                AFK_TEXT = custom
            storage.set_setting("afk_enabled", "1")
            storage.set_setting("afk_text", AFK_TEXT)
            await event.reply(f"🌙 АФК включён. Текст автоответа:\n{AFK_TEXT}")
        elif rest.startswith("выкл"):
            AFK_MODE = False
            storage.set_setting("afk_enabled", "0")
            await event.reply("☀️ АФК выключен")
        else:
            await event.reply("Использование: `.афк вкл [текст]` или `.афк выкл`")
        return

    # ---------- ЗАМЕТКИ ----------
    if lower.startswith(".заметка"):
        note_text = await get_prompt(event, ".заметка")
        if not note_text:
            await event.reply("`.заметка текст`")
            return
        nid = storage.add_note(note_text)
        await event.reply(f"✅ Заметка {fmt_id(nid)} сохранена")
        return

    if lower == ".заметки":
        notes = storage.list_notes()
        lines = [f"📌 {fmt_id(n['id'])}  {n['text']}" for n in notes]
        await event.reply(
            fmt_panel("ЗАМЕТКИ", "🗒", lines, footer=f"всего: {len(notes)}", empty_text="Заметок пока нет")
        )
        return

    if lower.startswith(".удали_заметку"):
        arg = text.split(maxsplit=1)
        if len(arg) < 2 or not arg[1].strip().isdigit():
            await event.reply("`.удали_заметку id`")
            return
        ok = storage.delete_note(int(arg[1].strip()))
        if ok:
            await react_ok(event)
        else:
            await react_fail(event)
            await event.reply("Заметка с таким id не найдена")
        return

    # ---------- ЗАДАЧИ (реальный трекер, отдельно от ИИ-.todo) ----------
    if lower.startswith(".задача"):
        task_text = await get_prompt(event, ".задача")
        if not task_text:
            await event.reply("`.задача текст`")
            return
        tid = storage.add_task(task_text)
        await event.reply(f"✅ Задача {fmt_id(tid)} добавлена")
        return

    if lower == ".задачи":
        tasks = storage.list_tasks()
        lines = [f"🔲 {fmt_id(t['id'])}  {t['text']}" for t in tasks]
        await event.reply(
            fmt_panel("ЗАДАЧИ", "📋", lines, footer=f"открыто: {len(tasks)}", empty_text="Активных задач нет 🎉")
        )
        return

    if lower.startswith(".готово"):
        arg = text.split(maxsplit=1)
        if len(arg) < 2 or not arg[1].strip().isdigit():
            await event.reply("`.готово id`")
            return
        ok = storage.complete_task(int(arg[1].strip()))
        if ok:
            await react_ok(event)
        else:
            await react_fail(event)
            await event.reply("Задача с таким id не найдена")
        return

    # ---------- КОНТАКТЫ (CRM) ----------
    if lower.startswith(".контакт "):
        parts = text.split(maxsplit=2)
        if len(parts) < 3 or not parts[1].startswith("@"):
            await event.reply("`.контакт @username заметка`")
            return
        username, note = parts[1].lstrip("@"), parts[2]
        storage.upsert_contact(username, note)
        await event.reply(f"👤 Заметка о @{username} сохранена")
        return

    if lower.startswith(".контакт_инфо"):
        parts = text.split(maxsplit=1)
        if len(parts) < 2 or not parts[1].startswith("@"):
            await event.reply("`.контакт_инфо @username`")
            return
        username = parts[1].lstrip("@")
        c = storage.get_contact(username)
        if not c:
            await event.reply("По этому контакту записей нет")
            return
        notes_lines = [f"· {line}" for line in c["note"].split("\n") if line.strip()]
        await event.reply(fmt_panel(f"@{username}", "👤", notes_lines))
        return

    if lower == ".контакты":
        contacts = storage.list_contacts()
        lines = [f"👤 @{c['username']}" for c in contacts]
        await event.reply(
            fmt_panel("КОНТАКТЫ", "📇", lines, footer=f"всего: {len(contacts)}", empty_text="Контактов пока нет")
        )
        return

    # ---------- НАПОМИНАНИЯ ----------
    if lower.startswith(".напомни"):
        rest = text[8:].strip()
        # ожидаем формат: "<когда> <текст>", когда парсим по первому разумному куску
        parsed_dt = None
        remind_text = rest
        for cut in range(len(rest), 0, -1):
            candidate = rest[:cut]
            dt = dateparser.parse(
                candidate,
                languages=["ru"],
                settings={"PREFER_DATES_FROM": "future", "RETURN_AS_TIMEZONE_AWARE": False},
            )
            if dt:
                parsed_dt = dt
                remind_text = rest[cut:].strip() or "напоминание"
                break
        if not parsed_dt:
            await event.reply(
                "Не понял время. Примеры:\n"
                "`.напомни через 2 часа купить хлеб`\n"
                "`.напомни завтра в 9 позвонить`"
            )
            return
        remind_ts = int(parsed_dt.timestamp())
        rid = storage.add_reminder(chat_id, remind_text, remind_ts)
        await event.reply(
            f"⏰ Напоминание #{rid} поставлено на {parsed_dt.strftime('%d.%m %H:%M')}"
        )
        return

    if lower == ".напоминания":
        rems = storage.list_pending_reminders()
        lines = [
            f"⏰ {fmt_id(r['id'])}  {r['text']}  ·  `{time.strftime('%d.%m %H:%M', time.localtime(r['remind_at']))}`"
            for r in rems
        ]
        await event.reply(
            fmt_panel("НАПОМИНАНИЯ", "⏰", lines, footer=f"активно: {len(rems)}", empty_text="Напоминаний нет")
        )
        return

    if lower.startswith(".отмени_напоминание"):
        arg = text.split(maxsplit=1)
        if len(arg) < 2 or not arg[1].strip().isdigit():
            await event.reply("`.отмени_напоминание id`")
            return
        ok = storage.cancel_reminder(int(arg[1].strip()))
        if ok:
            await react_ok(event)
        else:
            await react_fail(event)
            await event.reply("Не найдено")
        return

    # ---------- СОХРАНЕНИЕ МЕДИА ----------
    if lower == ".сохрани":
        if not event.is_reply:
            await event.reply("Ответь этой командой на фото/видео/документ")
            return
        replied = await event.get_reply_message()
        if not replied or not replied.media:
            await event.reply("В отвеченном сообщении нет медиа")
            return
        try:
            path = await replied.download_media(file=SAVES_DIR + "/")
            await event.reply(f"💾 Сохранено: `{path}`")
        except Exception as e:
            await event.reply(f"Не удалось сохранить: {e}")
        return

    # ---------- СУММАРИЗАЦИЯ ССЫЛКИ ----------
    if lower.startswith(".суммаризируй"):
        url = text.split(maxsplit=1)
        if len(url) < 2 or not url[1].strip().startswith("http"):
            await event.reply("`.суммаризируй ссылка`")
            return
        target_url = url[1].strip()
        status = await event.reply("🌐 Загружаю страницу...")
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(target_url, timeout=15) as resp:
                    html = await resp.text()
            text_only = re.sub("<[^<]+?>", " ", html)
            text_only = re.sub(r"\s+", " ", text_only).strip()[:6000]
            await status.edit("🤖 Делаю пересказ...")
            answer = await ask_ai(
                f"Кратко перескажи содержание страницы (5-7 предложений):\n\n{text_only}",
                chat_id=chat_id,
            )
            await status.edit(answer)
        except Exception as e:
            await status.edit(f"Не удалось обработать ссылку: {e}")
        return

    # ---------- СЛЕЖКА ЗА КЛЮЧЕВЫМИ СЛОВАМИ ----------
    if lower.startswith(".следи"):
        kw = text[6:].strip()
        if not kw:
            await event.reply("`.следи слово`")
            return
        storage.add_watch(kw)
        await event.reply(f"👁 Слежу за словом «{kw}» во всех чатах")
        return

    if lower.startswith(".не_следи"):
        kw = text[9:].strip()
        if not kw:
            await event.reply("`.не_следи слово`")
            return
        ok = storage.remove_watch(kw)
        await event.reply("Убрал из слежки ✅" if ok else "Такого слова не было в списке")
        return

    if lower == ".слежка":
        watches = storage.list_watches()
        lines = [f"👁 {w}" for w in watches]
        await event.reply(
            fmt_panel("СЛЕЖКА ЗА СЛОВАМИ", "🔔", lines, footer=f"слов: {len(watches)}", empty_text="Список слежки пуст")
        )
        return

    # ---------- ДАЙДЖЕСТ ПО ЗАПРОСУ ----------
    if lower == ".сводка":
        status = await event.reply("📊 Собираю сводку...")
        digest_text = await build_digest()
        await status.edit(digest_text or "За последнее время значимых событий не было")
        return

    # ---------- ОТЛОЖЕННАЯ ОТПРАВКА ----------
    if lower.startswith(".отложи"):
        rest = text[7:].strip()
        parts = rest.split(maxsplit=2)
        if len(parts) < 3:
            await event.reply("`.отложи <когда> @получатель текст`")
            return
        when_str, target, msg_text = parts[0], parts[1], parts[2]
        # время может состоять из пары слов ("через", "2", "часа") — пробуем по нарастающей
        combined = rest
        parsed_dt = None
        target_str = None
        body = None
        tokens = rest.split()
        for i in range(1, len(tokens)):
            maybe_time = " ".join(tokens[:i])
            dt = dateparser.parse(
                maybe_time, languages=["ru"],
                settings={"PREFER_DATES_FROM": "future", "RETURN_AS_TIMEZONE_AWARE": False},
            )
            if dt and i < len(tokens) and tokens[i].startswith("@"):
                parsed_dt = dt
                target_str = tokens[i]
                body = " ".join(tokens[i + 1:])
                break
        if not parsed_dt or not target_str or not body:
            await event.reply(
                "Не разобрал формат. Пример:\n`.отложи через 1 час @friend Привет!`"
            )
            return
        delay = max(0, parsed_dt.timestamp() - time.time())

        async def _send_later(delay_s, target_username, message):
            await asyncio.sleep(delay_s)
            try:
                await bot.send_message(target_username, message)
            except Exception as e:
                print(f"[отложи] не удалось отправить: {e}")

        asyncio.create_task(_send_later(delay, target_str, body))
        await event.reply(
            f"📤 Отправлю «{body}» пользователю {target_str} в {parsed_dt.strftime('%d.%m %H:%M')}"
        )
        return

    # ---------- АКТИВНОСТЬ ЧАТА ----------
    if lower.startswith(".актив"):
        rows = storage.top_active_users(chat_id, days=7)
        medals = ["🥇", "🥈", "🥉"]
        lines = []
        for i, r in enumerate(rows):
            try:
                u = await bot.get_entity(r["user_id"])
                name = getattr(u, "first_name", str(r["user_id"]))
            except Exception:
                name = str(r["user_id"])
            mark = medals[i] if i < 3 else "▫️"
            lines.append(f"{mark} {name}  ·  `{r['total']}` сообщ.")
        await event.reply(
            fmt_panel("АКТИВНОСТЬ ЗА 7 ДНЕЙ", "📊", lines, empty_text="Данных пока нет (собираются на входящих)")
        )
        return

    if lower.startswith(".молчуны"):
        rows = storage.last_seen_per_user(chat_id)
        cutoff = time.strftime("%Y-%m-%d", time.gmtime(time.time() - 7 * 86400))
        silent = [r for r in rows if r["last_day"] < cutoff]
        lines = []
        for r in silent:
            try:
                u = await bot.get_entity(r["user_id"])
                name = getattr(u, "first_name", str(r["user_id"]))
            except Exception:
                name = str(r["user_id"])
            lines.append(f"🔇 {name}  ·  посл. раз `{r['last_day']}`")
        await event.reply(
            fmt_panel("МОЛЧУНЫ", "🔕", lines, empty_text="Все активны за последнюю неделю 👍")
        )
        return

    # ---------- ИГНОР-ЛИСТ ----------
    if lower.startswith(".игнор"):
        arg = text.split(maxsplit=1)
        target = int(arg[1]) if len(arg) > 1 and arg[1].lstrip("-").isdigit() else chat_id
        storage.add_ignore(target, ai_exception=True)
        await event.reply(f"🔕 Чат {target} добавлен в игнор (с ИИ-исключением на важное)")
        return

    if lower.startswith(".не_игнор"):
        arg = text.split(maxsplit=1)
        target = int(arg[1]) if len(arg) > 1 and arg[1].lstrip("-").isdigit() else chat_id
        ok = storage.remove_ignore(target)
        await event.reply("🔔 Убрал из игнора" if ok else "Этого чата не было в игноре")
        return

    # ---------- ПРИВАТНОСТЬ / НЕ ЛОГИРОВАТЬ ----------
    if lower == ".не_логируй":
        currently = storage.is_no_log(chat_id)
        storage.set_no_log(chat_id, not currently)
        if currently:
            await event.reply("📝 Логирование этого чата включено обратно")
        else:
            await event.reply("🔒 Этот чат больше не логируется (антиудаление/индексация выключены)")
        return

    # ---------- RSS ----------
    if lower.startswith(".rss_добавь"):
        url = text.split(maxsplit=1)
        if len(url) < 2 or not url[1].strip().startswith("http"):
            await event.reply("`.rss_добавь ссылка`")
            return
        storage.add_feed(url[1].strip())
        await event.reply("📡 RSS-лента добавлена")
        return

    if lower.startswith(".rss_удали"):
        url = text.split(maxsplit=1)
        if len(url) < 2:
            await event.reply("`.rss_удали ссылка`")
            return
        ok = storage.remove_feed(url[1].strip())
        await event.reply("Удалено ✅" if ok else "Такой ленты не было")
        return

    if lower == ".rss_список":
        feeds = storage.list_feeds()
        lines = [f"📡 {f['url']}" for f in feeds]
        await event.reply(
            fmt_panel("RSS-ЛЕНТЫ", "📰", lines, footer=f"лент: {len(feeds)}", empty_text="Лент пока нет")
        )
        return

    # ---------- БЭКАП ----------
    if lower == ".бэкап":
        if os.path.exists(storage.DB_PATH):
            await event.reply(file=storage.DB_PATH, message="🗄 Ручной бэкап БД")
        else:
            await event.reply("Файл БД пока не создан")
        return

    # ---------- РАНДОМНЫЕ РЕАКЦИИ В ЧАТЕ ----------
    if lower == ".реакции":
        currently = storage.is_random_reactions(chat_id)
        if currently:
            storage.disable_random_reactions(chat_id)
            await event.reply("⭕ Рандомные реакции в этом чате выключены")
        else:
            storage.enable_random_reactions(chat_id)
            await event.reply("🎲 Рандомные реакции включены — буду иногда (примерно раз в 30 сообщений) реагировать эмодзи. Выключить: снова `.реакции`")
        return

    # ---------- КРАСИВОЕ МЕНЮ ----------
    if lower in (".меню", ".menu"):
        rows = []
        for num, (title, emoji, _) in MENU_CATEGORIES.items():
            rows.append(f"{emoji}  `{num:>2}`  {title}")
        menu_text = (
            "╭──────────────────────╮\n"
            "│   ✨ **P O M A** ✨   │\n"
            "╰──────────────────────╯\n\n"
            + "\n".join(rows) +
            f"\n{_SEP}\n· ответь номером на это сообщение, чтобы открыть раздел"
        )
        sent = await event.reply(menu_text)
        track_menu_msg(sent.id, "main")
        return

    if lower == ".дашборд":
        notes_n = len(storage.list_notes())
        all_tasks = storage.list_tasks(include_done=True)
        open_tasks = [t for t in all_tasks if not t["done"]]
        done_n = len(all_tasks) - len(open_tasks)
        rems_n = len(storage.list_pending_reminders())
        watches_n = len(storage.list_watches())
        feeds_n = len(storage.list_feeds())
        ignored_n = storage.count_ignored()
        afk_state = "🌙 включён" if AFK_MODE else "☀️ выключен"
        reactions_state = "🎲 включены" if storage.is_random_reactions(chat_id) else "⭕ выключены"
        lines = [
            f"📝 Заметок: `{notes_n}`",
            f"📈 Задачи: `{progress_bar(done_n, len(all_tasks))}`",
            f"⏰ Напоминаний активно: `{rems_n}`",
            f"👁 Слов в слежке: `{watches_n}`",
            f"📡 RSS-лент: `{feeds_n}`",
            f"🔕 Чатов в игноре: `{ignored_n}`",
            f"🎙 АФК: {afk_state}",
            f"🎲 Рандом-реакции здесь: {reactions_state}",
        ]
        await event.reply(fmt_panel("ДАШБОРД POMA", "📟", lines))
        return

    if lower in (".помощь", ".help", ".команды"):
        await event.reply(
            "✨ **POMA — команды**\n"
            "_(все с точкой)_\n\n"
            "🧭 `.меню` — красивое меню по разделам (отвечай номером)\n"
            "📟 `.дашборд` — статистика: заметки, задачи, напоминания\n\n"
            "**📌 Основное**\n"
            "`.расскажи` `.объясни` `.кратко` / `.tldr`\n"
            "`.факт` `.шутка` `.идея` `.перевод`\n\n"
            "**💻 Код**\n"
            "`.код` `.bug` `.review` / `.ревью`\n"
            "`.regex` `.sql` `.тест` `.оптимизация`\n\n"
            "**🎨 Стиль и контент**\n"
            "`.роль` `.стиль` `.мем` `.цитата`\n"
            "`.хук` `.тред` `.cta` `.био`\n"
            "`.спор` `.разбор` / `.ору` `.сравни`\n"
            "`.имя` `.план` `.промпт` `.элия`\n\n"
            "**🖼 NFT / Web3**\n"
            "`.nft` `.коллекция` `.trait` `.roadmap`\n"
            "`.утилита` `.нейминг` `.mint`\n\n"
            "**💼 Учёба и работа**\n"
            "`.todo` `.письмо` `.собес` `.термин` `.конспект`\n\n"
            "**🎲 Утилиты**\n"
            "`.рандом 1-100` `.выбери a | b | c`\n"
            "`.cat` `.dog` `.info` `.a_troll`\n"
            "`.сброс` — очистить память диалога\n"
            "`.реакции` — рандомные реакции в чате вкл/выкл\n\n"
            "**🎙 Голос / АФК**\n"
            "`.распознай` (ответом на войс) `.афк вкл/выкл [текст]`\n\n"
            "**📝 Заметки и задачи**\n"
            "`.заметка` `.заметки` `.удали_заметку id`\n"
            "`.задача` `.задачи` `.готово id`\n\n"
            "**👤 Контакты**\n"
            "`.контакт @user заметка` `.контакт_инфо @user` `.контакты`\n\n"
            "**⏰ Напоминания и отложенная отправка**\n"
            "`.напомни через 2 часа текст` `.напоминания` `.отмени_напоминание id`\n"
            "`.отложи через 1 час @user текст`\n\n"
            "**💾 Медиа и контент**\n"
            "`.сохрани` (ответом на медиа) `.суммаризируй ссылка`\n\n"
            "**🔔 Мониторинг**\n"
            "`.следи слово` `.не_следи слово` `.слежка`\n"
            "`.сводка` — дайджест по запросу (+ авто раз в день)\n"
            "`.игнор [chat_id]` `.не_игнор [chat_id]` `.не_логируй`\n\n"
            "**📡 RSS и бэкап**\n"
            "`.rss_добавь url` `.rss_список` `.rss_удали url` `.бэкап`\n\n"
            "**📊 Активность**\n"
            "`.актив` `.молчуны`\n\n"
            "**🛡 Автоматически (без команд)**\n"
            "Антиудаление и антиредактирование — лог в Избранное\n"
            "Уведомление о новом входе в аккаунт\n\n"
            "**💬 Диалог**\n"
            "Ответь на сообщение бота текстом — продолжит тему.\n"
            "Длинные ответы (код) печатаются в одном сообщении ▌"
        )
        return

    # продолжение только если ответ на сообщение ИИ или навигация по .меню
    if event.is_reply and not lower.startswith("."):
        replied = await event.get_reply_message()
        if replied and replied.id in menu_reply_ids and text.strip().isdigit():
            num = int(text.strip())
            category = MENU_CATEGORIES.get(num)
            if not category:
                await event.reply(f"Раздела {num} нет. Ответь числом от 1 до {len(MENU_CATEGORIES)}")
                return
            title, emoji, commands = category
            lines = [f"▫️ `{c}`" for c in commands]
            sent = await event.reply(fmt_panel(title.upper(), emoji, lines))
            track_menu_msg(sent.id, num)
            return
        if replied and replied.id in ai_reply_ids:
            await ask_ai_animated(event, text, chat_id=chat_id)
        return


@bot.on(events.NewMessage(chats="Telegram"))
async def _login_notice(event):
    """Официальный аккаунт Telegram шлёт сюда уведомления о новых входах —
    дублируем себе с пометкой, чтобы не потерялось среди других чатов."""
    try:
        text = event.raw_text or ""
        if any(kw in text.lower() for kw in ("new sign", "новый вход", "log in", "вход в аккаунт")):
            await _log(f"🔐 **Внимание: возможен новый вход в аккаунт**\n\n{text}", level=0)
    except Exception as e:
        print(f"[login_notice] ошибка: {e}")


async def main():
    await bot.start()
    me = await bot.get_me()
    print(f"POMA запущена: {me.first_name} (@{me.username})")

    log_chat = await ensure_log_chat()
    log_name = getattr(log_chat, "title", "POMA Logs")
    print(f"Группа для логов: «{log_name}» (id: {LOG_CHAT_ID})")
    print("Команды: .помощь")

    # фоновые задачи (шлют в группу логов, а не в Избранное)
    asyncio.create_task(background.reminders_loop(bot))
    asyncio.create_task(background.daily_digest_loop(bot, build_digest, LOG_CHAT_ID))
    asyncio.create_task(background.rss_loop(bot, LOG_CHAT_ID))
    asyncio.create_task(background.backup_loop(bot, LOG_CHAT_ID))
    asyncio.create_task(background.healthcheck_loop(bot, LOG_CHAT_ID))

    await bot.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
