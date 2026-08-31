import os
import re
import random
import asyncio
import aiohttp
from datetime import datetime
from openai import OpenAI
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.tl.functions.users import GetFullUserRequest
from dotenv import load_dotenv

load_dotenv()

API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
SESSION = os.getenv("SESSION")  # на Railway обязательно
GROQ_API_KEY = os.getenv("GROQ_API_KEY")

client_ai = OpenAI(
    api_key=GROQ_API_KEY,
    base_url="https://api.groq.com/openai/v1"
)

if SESSION:
    bot = TelegramClient(StringSession(SESSION), API_ID, API_HASH)
else:
    bot = TelegramClient("my_userbot", API_ID, API_HASH)

conversations = {}
MAX_HISTORY = 12
ai_reply_ids = set()

DEFAULT_SYSTEM = (
    "Ты умный помощник в Telegram. Отвечай на русском коротко и по делу.\n"
    "Правила:\n"
    "1) Если запрос понятен — сразу отвечай, БЕЗ уточняющих вопросов.\n"
    "2) Если просят выбрать (чёт/нечет, A/B) — просто выбери одной фразой.\n"
    "3) Уточняющий вопрос — только если без него нельзя ответить, максимум ОДИН.\n"
    "4) Без воды и лекций.\n"
    "5) Курсы валют, цены, новости, погоду НЕ ВЫДУМЫВАЙ. Нет данных — скажи об этом "
    "или предложи команду .курс.\n"
    "6) В диалоге учитывай контекст, не переспрашивай одно и то же."
)


# ===================== ИИ =====================

async def ask_ai(prompt: str, chat_id: int = None, system: str = None) -> str:
    system_text = system or DEFAULT_SYSTEM
    messages = [{"role": "system", "content": system_text}]
    if chat_id is not None and chat_id in conversations:
        messages.extend(conversations[chat_id])
    messages.append({"role": "user", "content": prompt})

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
        return f"Ошибка ИИ: {e}"


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
            await asyncio.sleep(0.22)
            await msg.edit(frame)
        except Exception:
            break
    return msg


async def type_into_message(msg, full_text: str, chunk_size: int = 36, delay: float = 0.05):
    full_text = full_text or ""
    if not full_text.strip():
        try:
            await msg.edit("Пустой ответ")
        except Exception:
            pass
        track_ai_msg(msg)
        return msg

    if len(full_text) <= 50:
        try:
            await msg.edit(full_text)
        except Exception:
            pass
        track_ai_msg(msg)
        return msg

    if "```" in full_text or "def " in full_text:
        chunk_size = max(chunk_size, 52)
        delay = min(delay, 0.04)

    pos = 0
    while pos < len(full_text):
        pos = min(pos + chunk_size, len(full_text))
        current = full_text[:pos]
        suffix = " ▌" if pos < len(full_text) else ""
        try:
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


# ===================== КУРС =====================

async def get_rate(amount: float, fr: str, to: str) -> str:
    fr, to = fr.upper(), to.upper()
    url = f"https://open.er-api.com/v6/latest/{fr}"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=15) as resp:
                data = await resp.json()
        if data.get("result") != "success":
            return "Не удалось получить курс."
        rates = data.get("rates") or {}
        if to not in rates:
            return f"Валюта `{to}` не найдена. Пример: USD UAH KZT EUR RUB"
        rate = rates[to]
        result = amount * rate
        return (
            f"💱 **{amount:g} {fr}** ≈ **{result:,.2f} {to}**\n"
            f"Курс: 1 {fr} = {rate:.6g} {to}\n"
            f"_Источник: open.er-api.com_"
        )
    except Exception as e:
        return f"Ошибка курса: {e}"


# ===================== ПОИСК / СВОДКА / ПРОФИЛЬ =====================

async def search_in_chat(event, query: str = None, username: str = None, limit: int = 12):
    from_user = None
    if username:
        username = username.lstrip("@").strip()
        try:
            from_user = await bot.get_entity(username)
        except Exception:
            await event.reply(f"Не нашёл пользователя `@{username}`")
            return

    if not query and not from_user:
        await event.reply(
            "Примеры:\n"
            "`.поиск дедлайн`\n"
            "`.поиск @nick оплата`\n"
            "`.поиск @nick`"
        )
        return

    status = await event.reply("🔍 Ищу...")
    lines = []
    try:
        kwargs = {"limit": limit}
        if query:
            kwargs["search"] = query
        if from_user:
            kwargs["from_user"] = from_user

        async for msg in bot.iter_messages(event.chat_id, **kwargs):
            if not msg or not msg.message:
                continue
            sender = await msg.get_sender()
            name = getattr(sender, "username", None) or getattr(sender, "first_name", None) or "?"
            date = msg.date.strftime("%d.%m %H:%M") if msg.date else "?"
            snippet = msg.message.replace("\n", " ")
            if len(snippet) > 120:
                snippet = snippet[:120] + "…"
            lines.append(f"• `{date}` **{name}**: {snippet}")
            if len(lines) >= limit:
                break
    except Exception as e:
        await status.edit(f"Ошибка поиска: {e}")
        return

    if not lines:
        await status.edit("Ничего не нашёл в этом чате.")
        return

    header = "🔍 **Результаты поиска**\n"
    if username:
        header += f"От: `@{username.lstrip('@')}`\n"
    if query:
        header += f"Запрос: `{query}`\n"
    header += "\n"
    text_out = header + "\n".join(lines)
    if len(text_out) > 4000:
        text_out = text_out[:4000] + "\n…"
    await status.edit(text_out)


async def chat_summary(event, limit: int = 40):
    status = await event.reply(f"📋 Читаю последние {limit} сообщений...")
    chunks = []
    try:
        async for msg in bot.iter_messages(event.chat_id, limit=limit):
            if not msg or not msg.message:
                continue
            sender = await msg.get_sender()
            name = getattr(sender, "username", None) or getattr(sender, "first_name", None) or "?"
            chunks.append(f"{name}: {msg.message[:300]}")
    except Exception as e:
        await status.edit(f"Ошибка: {e}")
        return

    if not chunks:
        await status.edit("Нечего суммировать.")
        return

    chunks.reverse()
    text_block = "\n".join(chunks)
    if len(text_block) > 6000:
        text_block = text_block[-6000:]

    prompt = (
        f"Кратко суммируй переписку (последние сообщения чата). "
        f"3–6 пунктов: о чём говорили, какие решения/вопросы. Без воды.\n\n{text_block}"
    )
    answer = await ask_ai(prompt, chat_id=None)
    try:
        await status.edit(f"📋 **Сводка чата** (≈{limit} сообщ.)\n\n{answer}")
        track_ai_msg(status)
    except Exception:
        m = await event.reply(f"📋 **Сводка**\n\n{answer}")
        track_ai_msg(m)


async def user_info_text(user) -> str:
    full = None
    try:
        full = await bot(GetFullUserRequest(user))
    except Exception:
        pass

    lines = ["👤 **Профиль (данные Telegram)**\n"]
    name = " ".join(x for x in [user.first_name, user.last_name] if x)
    lines.append(f"**Имя:** {name or '—'}")
    lines.append(f"**Username:** @{user.username}" if user.username else "**Username:** нет")
    lines.append(f"**ID:** `{user.id}`")

    lang = getattr(user, "lang_code", None)
    lines.append(f"**Язык (lang_code):** {lang or 'не указан / скрыт'}")

    flags = []
    if getattr(user, "bot", False):
        flags.append("бот")
    if getattr(user, "premium", False):
        flags.append("Premium")
    if getattr(user, "verified", False):
        flags.append("verified")
    if getattr(user, "scam", False):
        flags.append("scam")
    if getattr(user, "fake", False):
        flags.append("fake")
    if flags:
        lines.append(f"**Метки:** {', '.join(flags)}")

    status = getattr(user, "status", None)
    if status is not None:
        lines.append(f"**Статус:** `{status.__class__.__name__}`")

    if full and getattr(full, "full_user", None):
        about = getattr(full.full_user, "about", None)
        common = getattr(full.full_user, "common_chats_count", None)
        if about:
            lines.append(f"**Био:** {about}")
        if common is not None:
            lines.append(f"**Общих чатов:** {common}")

    lines.append("\n_Дата регистрации и регион чужим аккаунтам API не отдаёт._")
    return "\n".join(lines)


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
        prompt = await get_prompt(event, ".расскажи") or "Расскажи что-нибудь полезное и короткое"
        await ask_ai_animated(event, prompt, chat_id=chat_id)
        return

    if lower.startswith(".объясни"):
        prompt = await get_prompt(event, ".объясни")
        if not prompt:
            await event.reply("`.объясни тема` или ответом на сообщение")
            return
        await ask_ai_animated(event, f"Объясни просто и коротко:\n{prompt}", chat_id=chat_id)
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
        await ask_ai_animated(event, f"Кратко (2–4 предложения):\n{prompt}", chat_id=chat_id)
        return

    if lower.startswith(".идея"):
        topic = await get_prompt(event, ".идея") or "что угодно"
        await ask_ai_animated(event, f"3 конкретные идеи по теме: {topic}. Списком.", chat_id=chat_id)
        return

    if lower.startswith(".перевод"):
        prompt = await get_prompt(event, ".перевод")
        if not prompt:
            await event.reply("`.перевод текст` или ответом на сообщение")
            return
        await ask_ai_animated(
            event,
            f"Переведи: если не русский — на русский; если русский — на английский. Только перевод:\n{prompt}",
            chat_id=chat_id,
        )
        return

    if lower.startswith(".повтор"):
        prompt = await get_prompt(event, ".повтор")
        if not prompt:
            await event.reply("Ответь на сообщение: `.повтор` — перепишу текст нормально")
            return
        await ask_ai_animated(
            event,
            f"Перепиши яснее и аккуратнее, смысл тот же. Только результат:\n{prompt}",
            chat_id=chat_id,
        )
        return

    # ---------- КОД ----------
    if lower.startswith(".код"):
        prompt = await get_prompt(event, ".код") or "Напиши простой пример"
        await ask_ai_animated(
            event,
            f"Рабочий код по задаче: {prompt}. Код + очень короткое объяснение.",
            chat_id=chat_id,
            system="Senior-разработчик. Русский. Рабочий код, без лишней воды.",
            typewriter=True,
        )
        return

    if lower.startswith(".bug") or lower.startswith(".ошибка"):
        pref = ".bug" if lower.startswith(".bug") else ".ошибка"
        prompt = await get_prompt(event, pref)
        if not prompt:
            await event.reply("Пришли код/ошибку: `.bug`")
            return
        await ask_ai_animated(
            event,
            f"Найди баг, 2–3 предложения объяснения, исправленный код:\n{prompt}",
            chat_id=chat_id,
            system="Senior-разработчик. Диагноз + фикс.",
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
            f"Code review:\n{prompt}\n\n✅ Ок\n⚠️ Проблемы\n💡 Улучшения",
            chat_id=chat_id,
            typewriter=True,
        )
        return

    if lower.startswith(".regex"):
        prompt = await get_prompt(event, ".regex")
        if not prompt:
            await event.reply("`.regex что найти`")
            return
        await ask_ai_animated(event, f"Regex + коротко поясни:\n{prompt}", chat_id=chat_id, typewriter=True)
        return

    if lower.startswith(".sql"):
        prompt = await get_prompt(event, ".sql")
        if not prompt:
            await event.reply("`.sql задача`")
            return
        await ask_ai_animated(event, f"SQL + коротко поясни:\n{prompt}", chat_id=chat_id, typewriter=True)
        return

    if lower.startswith(".тест"):
        prompt = await get_prompt(event, ".тест")
        if not prompt:
            await event.reply("`.тест код/функция`")
            return
        await ask_ai_animated(event, f"Тесты / pytest для:\n{prompt}", chat_id=chat_id, typewriter=True)
        return

    if lower.startswith(".оптимизация"):
        prompt = await get_prompt(event, ".оптимизация")
        if not prompt:
            await event.reply("`.оптимизация код`")
            return
        await ask_ai_animated(
            event,
            f"Упростить/ускорить + улучшенный вариант:\n{prompt}",
            chat_id=chat_id,
            typewriter=True,
        )
        return

    # ---------- РАБОТА ----------
    if lower.startswith(".todo"):
        prompt = await get_prompt(event, ".todo")
        if not prompt:
            await event.reply("`.todo задача`")
            return
        await ask_ai_animated(event, f"Конкретный чеклист:\n{prompt}", chat_id=chat_id)
        return

    if lower.startswith(".письмо"):
        prompt = await get_prompt(event, ".письмо")
        if not prompt:
            await event.reply("`.письмо что написать`")
            return
        await ask_ai_animated(event, f"Деловое сообщение, готовый текст:\n{prompt}", chat_id=chat_id)
        return

    if lower.startswith(".собес"):
        topic = await get_prompt(event, ".собес") or "python junior"
        await ask_ai_animated(
            event,
            f"8–10 вопросов на собеседование: {topic}. С краткими ответами.",
            chat_id=chat_id,
        )
        return

    if lower.startswith(".термин"):
        prompt = await get_prompt(event, ".термин")
        if not prompt:
            await event.reply("`.термин слово`")
            return
        await ask_ai_animated(event, f"Термин за 20 секунд + пример:\n{prompt}", chat_id=chat_id)
        return

    if lower.startswith(".конспект"):
        prompt = await get_prompt(event, ".конспект")
        if not prompt:
            await event.reply("`.конспект тема/текст`")
            return
        await ask_ai_animated(event, f"Конспект пунктами:\n{prompt}", chat_id=chat_id)
        return

    # ---------- ЧАТ: ПОИСК / СВОДКА / ИНФО ----------
    if lower.startswith(".поиск") or lower.startswith(".find"):
        rest = text.split(maxsplit=1)
        rest = rest[1].strip() if len(rest) > 1 else ""
        username = None
        query = rest
        m = re.match(r"^@?([A-Za-z0-9_]{4,})\s*(.*)$", rest)
        # если первое слово похоже на username
        if rest.startswith("@") or (m and not rest.startswith("http")):
            parts = rest.split(maxsplit=1)
            first = parts[0].lstrip("@")
            # эвристика: username без пробелов
            if re.fullmatch(r"[A-Za-z0-9_]{4,}", first):
                username = first
                query = parts[1].strip() if len(parts) > 1 else None
        if not query and event.is_reply and not username:
            replied = await event.get_reply_message()
            query = (replied.raw_text or "").strip() if replied else None
        await search_in_chat(event, query=query or None, username=username)
        return

    if lower.startswith(".сводка"):
        parts = text.split()
        limit = 40
        if len(parts) > 1 and parts[1].isdigit():
            limit = max(10, min(int(parts[1]), 100))
        await chat_summary(event, limit=limit)
        return

    if lower in (".узнай", ".кто"):
        if not event.is_reply:
            await event.reply("Ответь на сообщение человека и напиши `.узнай`")
            return
        replied = await event.get_reply_message()
        if not replied:
            await event.reply("Не нашёл сообщение")
            return
        try:
            user = await replied.get_sender()
            if not user:
                await event.reply("Не удалось получить пользователя")
                return
            user = await bot.get_entity(user.id)
            await event.reply(await user_info_text(user))
        except Exception as e:
            await event.reply(f"Ошибка: {e}")
        return

    if lower == ".чат":
        chat = await event.get_chat()
        title = getattr(chat, "title", None) or "Личка"
        username = getattr(chat, "username", None)
        lines = [
            "💬 **Чат**",
            f"**Название:** {title}",
            f"**ID:** `{event.chat_id}`",
        ]
        if username:
            lines.append(f"**Username:** @{username}")
        await event.reply("\n".join(lines))
        return

    if lower == ".когда":
        if not event.is_reply:
            await event.reply("Ответь на сообщение: `.когда`")
            return
        replied = await event.get_reply_message()
        if not replied or not replied.date:
            await event.reply("Нет даты")
            return
        d = replied.date
        await event.reply(f"🕒 {d.strftime('%d.%m.%Y %H:%M:%S')} UTC")
        return

    # ---------- КУРС ----------
    if lower.startswith(".курс"):
        parts = text.split()
        if len(parts) < 4:
            await event.reply(
                "Формат:\n"
                "`.курс 1000000 KZT UAH`\n"
                "`.курс 1 USD UAH`\n"
                "`.курс 500 EUR KZT`"
            )
            return
        try:
            amount = float(parts[1].replace(",", ".").replace(" ", ""))
            fr, to = parts[2], parts[3]
        except Exception:
            await event.reply("Не понял число. Пример: `.курс 1000000 KZT UAH`")
            return
        msg = await event.reply("💱 Смотрю курс...")
        result = await get_rate(amount, fr, to)
        try:
            await msg.edit(result)
        except Exception:
            await event.reply(result)
        return

    # ---------- УТИЛИТЫ ----------
    if lower.startswith(".рандом") or lower.startswith(".random"):
        rest = text.split(maxsplit=1)
        if len(rest) < 2 or "-" not in rest[1]:
            await event.reply("`.рандом 1-100`")
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
            await event.reply("`.выбери пицца | суши | бургер`")
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
            "🐱 Кот",
            "Не удалось получить кота",
        )
        return

    if lower == ".dog":
        await send_animal(
            event,
            "https://dog.ceo/api/breeds/image/random",
            "🐶 Пёс",
            "Не удалось получить собаку",
        )
        return

    if lower == ".info":
        sender = await event.get_sender()
        await event.reply(
            f"👤 **Ты**\n"
            f"Имя: {sender.first_name}\n"
            f"Username: @{sender.username or 'нет'}\n"
            f"ID: `{sender.id}`"
        )
        return

    if lower in (".сброс", ".reset", ".clear"):
        conversations.pop(chat_id, None)
        await event.reply("Память диалога очищена 🧹")
        return

    if lower in (".помощь", ".help", ".команды"):
        await event.reply(
            "✨ **Команды** _(с точкой)_\n\n"
            "**📌 Основное**\n"
            "`.расскажи` `.объясни` `.кратко` `.идея`\n"
            "`.перевод` `.повтор`\n\n"
            "**💻 Код**\n"
            "`.код` `.bug` `.review`\n"
            "`.regex` `.sql` `.тест` `.оптимизация`\n\n"
            "**💼 Работа**\n"
            "`.todo` `.письмо` `.собес` `.термин` `.конспект`\n\n"
            "**💬 Чат**\n"
            "`.поиск текст` / `.поиск @user текст`\n"
            "`.сводка` / `.сводка 50`\n"
            "`.узнай` — ответом на человека\n"
            "`.чат` `.когда`\n\n"
            "**💱 Курс**\n"
            "`.курс 1000000 KZT UAH`\n\n"
            "**🎲 Утилиты**\n"
            "`.рандом 1-100` `.выбери a | b`\n"
            "`.cat` `.dog` `.info` `.сброс`\n\n"
            "**Диалог:** ответь на сообщение бота текстом — продолжит тему.\n"
            "Длинные ответы печатаются в одном сообщении."
        )
        return

    # продолжение диалога только на ответы ИИ
    if event.is_reply and not lower.startswith("."):
        replied = await event.get_reply_message()
        if replied and replied.id in ai_reply_ids:
            await ask_ai_animated(event, text, chat_id=chat_id)
        return


async def main():
    await bot.start()
    me = await bot.get_me()
    print(f"Юзербот запущен: {me.first_name} (@{me.username})")
    print("Команды: .помощь")
    await bot.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
