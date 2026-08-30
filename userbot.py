import os
import random
import asyncio
import aiohttp
from openai import OpenAI
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from dotenv import load_dotenv

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

conversations = {}
MAX_HISTORY = 12
ai_reply_ids = set()


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

    if lower in (".помощь", ".help", ".команды"):
        await event.reply(
            "✨ **POMA — команды**\n"
            "_(все с точкой)_\n\n"
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
            "`.сброс` — очистить память диалога\n\n"
            "**💬 Диалог**\n"
            "Ответь на сообщение бота текстом — продолжит тему.\n"
            "Длинные ответы (код) печатаются в одном сообщении ▌"
        )
        return

    # продолжение только если ответ на сообщение ИИ
    if event.is_reply and not lower.startswith("."):
        replied = await event.get_reply_message()
        if replied and replied.id in ai_reply_ids:
            await ask_ai_animated(event, text, chat_id=chat_id)
        return


async def main():
    await bot.start()
    me = await bot.get_me()
    print(f"rio запущена: {me.first_name} (@{me.username})")
    print("Команды: .помощь")
    await bot.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
