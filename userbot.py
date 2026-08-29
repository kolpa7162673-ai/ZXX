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
SESSION = os.getenv("SESSION")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")

client_ai = OpenAI(
    api_key=GROQ_API_KEY,
    base_url="https://api.groq.com/openai/v1"
)

if SESSION:
    bot = TelegramClient(StringSession(SESSION), API_ID, API_HASH)
else:
    bot = TelegramClient("my_userbot", API_ID, API_HASH)

# История диалога по чатам
conversations = {}
MAX_HISTORY = 12

# ID сообщений, на которые можно «продолжать» диалог (ответы юзербота)
ai_reply_ids = set()


async def ask_ai(prompt: str, chat_id: int = None, system: str = None) -> str:
    system_text = system or (
        "Ты весёлый и полезный ассистент. Отвечай на русском коротко и с лёгким юмором. "
        "Если нужно уточнение — задай 1 короткий вопрос. "
        "Если пользователь продолжает диалог — отвечай по контексту."
    )

    messages = [{"role": "system", "content": system_text}]
    if chat_id is not None and chat_id in conversations:
        messages.extend(conversations[chat_id])
    messages.append({"role": "user", "content": prompt})

    try:
        response = client_ai.chat.completions.create(
            model="openai/gpt-oss-120b",
            messages=messages,
            max_tokens=900
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


async def reply_ai(event, text: str):
    """Ответить и запомнить ID, чтобы можно было продолжить диалог."""
    msg = await event.reply(text)
    if msg:
        ai_reply_ids.add(msg.id)
        # не раздуваем set
        if len(ai_reply_ids) > 500:
            # оставляем «хвост»
            trimmed = list(ai_reply_ids)[-300:]
            ai_reply_ids.clear()
            ai_reply_ids.update(trimmed)
    return msg


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


@bot.on(events.NewMessage(outgoing=True))
async def handler(event):
    text = (event.raw_text or "").strip()
    if not text:
        return

    lower = text.lower()
    chat_id = event.chat_id

    # ----- команды только с точкой -----

    if lower.startswith(".расскажи"):
        prompt = await get_prompt(event, ".расскажи") or "Расскажи что-нибудь интересное и короткое"
        answer = await ask_ai(prompt, chat_id=chat_id)
        await reply_ai(event, answer)
        return

    if lower.startswith(".код"):
        prompt = await get_prompt(event, ".код") or "Напиши простой пример"
        answer = await ask_ai(
            f"Напиши рабочий код по задаче: {prompt}. Только код + очень короткое объяснение.",
            chat_id=chat_id,
            system="Ты опытный программист. Отвечай на русском. Давай рабочий код и короткое объяснение."
        )
        await reply_ai(event, answer)
        return

    if lower == ".cat":
        await send_animal(
            event,
            "https://api.thecatapi.com/v1/images/search",
            "🐱 Вот тебе кот",
            "Не удалось получить кота 😿"
        )
        return

    if lower == ".dog":
        await send_animal(
            event,
            "https://dog.ceo/api/breeds/image/random",
            "🐶 Вот тебе пёс",
            "Не удалось получить собаку 😢"
        )
        return

    if lower == ".ghoul":
        msg = await event.reply("1000")
        for i in range(999, 0, -7):
            try:
                await msg.edit(str(i))
                await asyncio.sleep(0.12)
            except Exception:
                break
        try:
            await msg.edit("Я гуль...")
        except Exception:
            pass
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
            await event.reply("Напиши: .spam текст")
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
            "Классика жанра"
        ]
        await event.reply(random.choice(trolls))
        return

    if lower.startswith(".факт"):
        topic = await get_prompt(event, ".факт") or "случайный интересный факт"
        answer = await ask_ai(f"Расскажи один короткий интересный факт на тему: {topic}", chat_id=chat_id)
        await reply_ai(event, answer)
        return

    if lower.startswith(".шутка") or lower.startswith(".joke"):
        topic = text.split(maxsplit=1)[1] if " " in text.strip() else "общая"
        answer = await ask_ai(f"Придумай одну короткую смешную шутку на тему: {topic}", chat_id=chat_id)
        await reply_ai(event, answer)
        return

    if lower.startswith(".кратко"):
        prompt = await get_prompt(event, ".кратко")
        if not prompt:
            await event.reply("Ответь на сообщение: .кратко  или напиши текст")
            return
        answer = await ask_ai(f"Кратко перескажи суть (2–4 предложения):\n{prompt}", chat_id=chat_id)
        await reply_ai(event, answer)
        return

    if lower.startswith(".объясни"):
        prompt = await get_prompt(event, ".объясни")
        if not prompt:
            await event.reply("Напиши: .объясни тема")
            return
        answer = await ask_ai(f"Объясни простыми словами:\n{prompt}", chat_id=chat_id)
        await reply_ai(event, answer)
        return

    if lower.startswith(".идея"):
        topic = await get_prompt(event, ".идея") or "что угодно"
        answer = await ask_ai(f"Предложи 3 креативные идеи на тему: {topic}. Коротко, списком.", chat_id=chat_id)
        await reply_ai(event, answer)
        return

    if lower.startswith(".перевод"):
        prompt = await get_prompt(event, ".перевод")
        if not prompt:
            await event.reply("Напиши: .перевод текст")
            return
        answer = await ask_ai(
            f"Переведи на русский, если текст не на русском. Если уже на русском — на английский. Только перевод:\n{prompt}",
            chat_id=chat_id
        )
        await reply_ai(event, answer)
        return

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
                await event.reply("Укажи тему: `.роль детектив Почему Wi‑Fi так называется`")
                return
        answer = await ask_ai(
            f"Ответь полностью в роли: {role}. Тема: {topic}. Не выходи из роли. Коротко и ярко.",
            chat_id=chat_id
        )
        await reply_ai(event, answer)
        return

    if lower.startswith(".спор"):
        topic = await get_prompt(event, ".спор")
        if not topic:
            await event.reply("Напиши: `.спор тема`")
            return
        answer = await ask_ai(
            f"Тема: {topic}\n\n✅ 3 аргумента ЗА\n❌ 3 аргумента ПРОТИВ\nВ конце — короткий вывод.",
            chat_id=chat_id
        )
        await reply_ai(event, answer)
        return

    if lower.startswith(".разбор"):
        prompt = await get_prompt(event, ".разбор")
        if not prompt:
            await event.reply("Ответь на сообщение: `.разбор`")
            return
        answer = await ask_ai(
            f"Разбор:\n{prompt}\n\n1) Суть\n2) Сильное\n3) Слабое\n4) Вердикт",
            chat_id=chat_id
        )
        await reply_ai(event, answer)
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
                await event.reply("Пример: `.стиль мемно Завтра сдаём отчёт`")
                return
            style, body = parts[0], parts[1]
        if not body:
            await event.reply("Нет текста")
            return
        answer = await ask_ai(
            f"Перепиши в стиле «{style}». Только результат:\n{body}",
            chat_id=chat_id
        )
        await reply_ai(event, answer)
        return

    if lower.startswith(".имя"):
        topic = await get_prompt(event, ".имя") or "игровой ник"
        answer = await ask_ai(
            f"Придумай 8 крутых ников/названий для: {topic}. Списком, без воды.",
            chat_id=chat_id
        )
        await reply_ai(event, answer)
        return

    if lower.startswith(".план"):
        topic = await get_prompt(event, ".план")
        if not topic:
            await event.reply("Пример: `.план выучить python за месяц`")
            return
        answer = await ask_ai(
            f"Короткий план (5–8 шагов) для цели:\n{topic}",
            chat_id=chat_id
        )
        await reply_ai(event, answer)
        return

    if lower.startswith(".сравни"):
        topic = await get_prompt(event, ".сравни")
        if not topic:
            await event.reply("Пример: `.сравни Python и JavaScript`")
            return
        answer = await ask_ai(
            f"Сравни по делу:\n{topic}\nПлюсы/минусы/когда что выбрать.",
            chat_id=chat_id
        )
        await reply_ai(event, answer)
        return

    if lower.startswith(".промпт"):
        topic = await get_prompt(event, ".промпт")
        if not topic:
            await event.reply("Пример: `.промпт логотип кофейни`")
            return
        answer = await ask_ai(
            f"Готовый сильный промпт для:\n{topic}\nТолько промпт.",
            chat_id=chat_id
        )
        await reply_ai(event, answer)
        return

    if lower.startswith(".элия") or lower.startswith(".eli5"):
        prompt = text.split(maxsplit=1)[1] if " " in text else ""
        if not prompt and event.is_reply:
            replied = await event.get_reply_message()
            prompt = (replied.raw_text or "").strip() if replied else ""
        if not prompt:
            await event.reply("Пример: `.элия чёрные дыры`")
            return
        answer = await ask_ai(f"Объясни как ребёнку 5 лет:\n{prompt}", chat_id=chat_id)
        await reply_ai(event, answer)
        return

    if lower in (".сброс", ".reset", ".clear"):
        conversations.pop(chat_id, None)
        await event.reply("Память диалога очищена 🧹")
        return

    if lower in (".помощь", ".help", ".команды"):
        await event.reply(
            "**Команды (только с точкой)**\n\n"
            "`.расскажи` `.код` `.объясни` `.кратко`\n"
            "`.факт` `.шутка` `.идея` `.перевод`\n"
            "`.роль` `.спор` `.разбор` `.стиль`\n"
            "`.имя` `.план` `.сравни` `.промпт` `.элия`\n"
            "`.cat` `.dog` `.ghoul` `.info` `.a_troll`\n"
            "`.сброс` `.помощь`\n\n"
            "Продолжить диалог: **ответь** на сообщение бота обычным текстом."
        )
        return

    # ----- продолжение ТОЛЬКО если ответ на сообщение юзербота -----
    if event.is_reply and not lower.startswith("."):
        replied = await event.get_reply_message()
        if replied and replied.id in ai_reply_ids:
            answer = await ask_ai(text, chat_id=chat_id)
            await reply_ai(event, answer)
        # иначе молчим
        return

    # всё остальное (просто "dog", "привет" и т.д.) — игнорируем


async def main():
    await bot.start()
    me = await bot.get_me()
    print(f"POMA: {me.first_name} (@{me.username})")
    await bot.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())