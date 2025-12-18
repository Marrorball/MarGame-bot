import os
import asyncio
import random
import string
from dataclasses import dataclass, field
from typing import Dict, List, Set

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message

from aiohttp import web


# --- настройки из окружения ---
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
if not BOT_TOKEN:
    raise RuntimeError("Не задан BOT_TOKEN (в переменных окружения)")

PORT = int(os.getenv("PORT", "8000"))
# На Koyeb сервисы обычно слушают тот порт, который ты укажешь при деплое (часто 8000).

# --- виселица (ASCII-картинка по ошибкам) ---
HANGMAN_PICS = [
    r"""
 +---+
 |   |
     |
     |
     |
     |
=========""",
    r"""
 +---+
 |   |
 O   |
     |
     |
     |
=========""",
    r"""
 +---+
 |   |
 O   |
 |   |
     |
     |
=========""",
    r"""
 +---+
 |   |
 O   |
/|   |
     |
     |
=========""",
    r"""
 +---+
 |   |
 O   |
/|\  |
     |
     |
=========""",
    r"""
 +---+
 |   |
 O   |
/|\  |
/    |
     |
=========""",
    r"""
 +---+
 |   |
 O   |
/|\  |
/ \  |
     |
=========""",
]

ALLOWED = set("абвгдеёжзийклмнопрстуфхцчшщъыьэюя-")

def gen_code(n: int = 5) -> str:
    return "".join(random.choice(string.ascii_uppercase + string.digits) for _ in range(n))

def normalize_word(w: str) -> str:
    w = w.strip().lower()
    w = "".join(ch for ch in w if ch in ALLOWED)
    return w

@dataclass
class Room:
    code: str
    host_id: int
    players: Set[int] = field(default_factory=set)
    order: List[int] = field(default_factory=list)   # порядок отгадывающих (без хоста)
    started: bool = False

    max_fails: int = 6
    secret: str = ""              # задаёт хост
    guessed: Set[str] = field(default_factory=set)
    fails: int = 0
    turn_idx: int = 0             # чей ход (индекс в order)

rooms_by_code: Dict[str, Room] = {}
user_room: Dict[int, str] = {}   # user_id -> code

dp = Dispatcher()

async def safe_send(bot: Bot, user_id: int, text: str):
    try:
        await bot.send_message(user_id, text)
    except Exception:
        pass

async def broadcast(bot: Bot, room: Room, text: str):
    for uid in list(room.players):
        await safe_send(bot, uid, text)

def shown_word(secret: str, guessed: Set[str]) -> str:
    return " ".join([ch if ch in guessed else "•" for ch in secret])

def render(room: Room) -> str:
    pic = HANGMAN_PICS[min(room.fails, len(HANGMAN_PICS) - 1)]
    shown = shown_word(room.secret, room.guessed) if room.secret else "(хост ещё не загадал слово)"
    lives_left = room.max_fails - room.fails
    guessed = ", ".join(sorted(room.guessed)) if room.guessed else "-"
    return (
        f"🎮 Комната: {room.code}\n"
        f"👥 Игроков: {len(room.players)}\n"
        f"❤️ Жизни: {lives_left}/{room.max_fails}\n"
        f"{pic}\n\n"
        f"🪓 Слово: {shown}\n"
        f"🔤 Буквы: {guessed}\n"
    )

def current_turn_user(room: Room) -> int:
    if not room.order:
        return -1
    return room.order[room.turn_idx % len(room.order)]

@dp.message(Command("start"))
async def cmd_start(message: Message):
    await message.answer(
        "Это бот-виселица с комнатами (в личке у бота).\n\n"
        "Основное:\n"
        "• /create — создать комнату\n"
        "• /join CODE — войти по коду\n"
        "• /leave — выйти\n"
        "• /room — состояние комнаты\n\n"
        "Команды хоста:\n"
        "• /setword СЛОВО — загадать слово\n"
        "• /lives N — установить жизни (например 6)\n"
        "• /startgame — начать игру\n\n"
        "Ходы: отправляй букву или слово целиком (когда твоя очередь)."
    )

@dp.message(Command("create"))
async def create_room(message: Message):
    uid = message.from_user.id
    if uid in user_room:
        await message.answer("Ты уже в комнате. /leave чтобы выйти.")
        return

    code = gen_code()
    room = Room(code=code, host_id=uid)
    room.players.add(uid)
    rooms_by_code[code] = room
    user_room[uid] = code

    await message.answer(
        f"✅ Комната создана: {code}\n"
        f"Друзья: /join {code}\n"
        f"Ты хост: загадай слово /setword ... потом /startgame"
    )

@dp.message(Command("join"))
async def join_room(message: Message):
    uid = message.from_user.id
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Использование: /join CODE")
        return
    if uid in user_room:
        await message.answer("Ты уже в комнате. /leave чтобы выйти.")
        return

    code = parts[1].strip().upper()
    room = rooms_by_code.get(code)
    if not room:
        await message.answer("Комната не найдена. Проверь код.")
        return

    room.players.add(uid)
    user_room[uid] = code

    # порядок ходов: хост НЕ угадывает, угадывают остальные в порядке входа
    if uid != room.host_id and uid not in room.order:
        room.order.append(uid)

    name = message.from_user.full_name
    await message.answer(f"✅ Ты вошёл(ла) в комнату {code}. Жди старта от хоста.")
    await broadcast(dp.bot, room, f"👤 {name} вошёл(ла) в комнату. Игроков: {len(room.players)}")

@dp.message(Command("leave"))
async def leave_room(message: Message):
    uid = message.from_user.id
    code = user_room.pop(uid, None)
    if not code:
        await message.answer("Ты не в комнате.")
        return
    room = rooms_by_code.get(code)
    if not room:
        await message.answer("Ок.")
        return

    room.players.discard(uid)
    if uid in room.order:
        room.order.remove(uid)
        room.turn_idx = room.turn_idx % max(1, len(room.order))  # аккуратно с индексом

    name = message.from_user.full_name

    # Если хост вышел — закрываем комнату (как ты попросил)
    if uid == room.host_id:
        await broadcast(dp.bot, room, "🧹 Хост вышел — комната закрыта.")
        for p in list(room.players):
            user_room.pop(p, None)
        rooms_by_code.pop(code, None)
        await message.answer(f"🧹 Ты вышел(ла). Комната {code} закрыта.")
        return

    await broadcast(dp.bot, room, f"👋 {name} вышел(ла) из комнаты. Игроков: {len(room.players)}")
    await message.answer(f"👋 Ты вышел(ла) из комнаты {code}.")

@dp.message(Command("room"))
async def room_info(message: Message):
    uid = message.from_user.id
    code = user_room.get(uid)
    if not code:
        await message.answer("Ты не в комнате. /create или /join CODE")
        return
    room = rooms_by_code.get(code)
    if not room:
        user_room.pop(uid, None)
        await message.answer("Комната не найдена. /create")
        return

    text = render(room)
    if room.started and room.order:
        turn_uid = current_turn_user(room)
        text += f"\n➡️ Сейчас ход игрока: {turn_uid} (user_id)\n"
    elif room.started and not room.order:
        text += "\n⚠️ В комнате нет отгадывающих (кроме хоста).\n"

    await message.answer(text)

@dp.message(Command("lives"))
async def set_lives(message: Message):
    uid = message.from_user.id
    code = user_room.get(uid)
    if not code:
        await message.answer("Ты не в комнате.")
        return
    room = rooms_by_code.get(code)
    if not room:
        await message.answer("Комната не найдена.")
        return
    if uid != room.host_id:
        await message.answer("Только хост может менять жизни.")
        return
    parts = (message.text or "").split()
    if len(parts) < 2 or not parts[1].isdigit():
        await message.answer("Использование: /lives 6")
        return
    n = int(parts[1])
    if n < 1:
        await message.answer("Жизни должны быть >= 1")
        return

    room.max_fails = n
    # подстроим картинки: если хочешь больше жизней — позже добавим больше стадий
    await message.answer(f"✅ Жизни установлены: {n}. Сейчас стадий картинки: {len(HANGMAN_PICS)-1} ошибок.")

@dp.message(Command("setword"))
async def set_word(message: Message):
    uid = message.from_user.id
    code = user_room.get(uid)
    if not code:
        await message.answer("Ты не в комнате.")
        return
    room = rooms_by_code.get(code)
    if not room:
        await message.answer("Комната не найдена.")
        return
    if uid != room.host_id:
        await message.answer("Только хост может загадывать слово.")
        return
    if room.started:
        await message.answer("Игра уже началась — нельзя менять слово.")
        return

    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Использование: /setword слово")
        return

    w = normalize_word(parts[1])
    if len(w) < 2:
        await message.answer("Слово слишком короткое/неподходящее. Используй русские буквы.")
        return

    room.secret = w
    room.guessed = set()
    room.fails = 0
    room.turn_idx = 0

    await message.answer("✅ Слово загадано. Теперь /startgame")

@dp.message(Command("startgame"))
async def start_game(message: Message):
    uid = message.from_user.id
    code = user_room.get(uid)
    if not code:
        await message.answer("Ты не в комнате.")
        return
    room = rooms_by_code.get(code)
    if not room:
        await message.answer("Комната не найдена.")
        return
    if uid != room.host_id:
        await message.answer("Только хост может начать игру.")
        return
    if not room.secret:
        await message.answer("Сначала загадай слово: /setword ...")
        return
    if not room.order:
        await message.answer("Нужны отгадывающие (кроме хоста). Пусть друзья зайдут: /join CODE")
        return

    room.started = True
    room.guessed = set()
    room.fails = 0
    room.turn_idx = 0

    await broadcast(dp.bot, room, "🚀 Игра началась!\n\n" + render(room))
    await broadcast(dp.bot, room, f"➡️ Первый ход: {current_turn_user(room)} (user_id)\nПиши букву или слово.")

@dp.message(F.text)
async def on_text(message: Message):
    uid = message.from_user.id
    code = user_room.get(uid)
    if not code:
        return
    room = rooms_by_code.get(code)
    if not room or not room.started:
        return

    # проверка очереди: ходит только текущий игрок
    turn_uid = current_turn_user(room)
    if uid != turn_uid:
        await message.answer("Сейчас не твой ход 🙂")
        return

    txt = (message.text or "").strip().lower()
    if not txt:
        return

    # хост не угадывает (на всякий)
    if uid == room.host_id:
        await message.answer("Хост не угадывает 🙂")
        return

    # ход
    if len(txt) == 1:
        ch = txt
        if ch not in ALLOWED:
            await message.answer("Пиши русскую букву.")
            return
        if ch in room.guessed:
            await message.answer("Эта буква уже была.")
            return
        room.guessed.add(ch)
        if ch not in room.secret:
            room.fails += 1
    else:
        guess = normalize_word(txt)
        if guess == room.secret:
            room.guessed.update(set(room.secret))
        else:
            room.fails += 1

    name = message.from_user.full_name
    await broadcast(dp.bot, room, f"✍️ Ход: {name}\n\n{render(room)}")

    # конец игры
    if all(ch in room.guessed for ch in room.secret):
        await broadcast(dp.bot, room, f"🎉 Победа! Слово: {room.secret}\nХост может начать заново: /startgame (или загадать новое /setword)")
        room.started = False
        return

    if room.fails >= room.max_fails:
        await broadcast(dp.bot, room, f"💀 Поражение. Слово было: {room.secret}\nХост может начать заново: /startgame (или новое /setword)")
        room.started = False
        return

    # следующий ход
    room.turn_idx += 1
    await broadcast(dp.bot, room, f"➡️ Следующий ход: {current_turn_user(room)} (user_id)")

# --- tiny HTTP server для health check Koyeb ---
async def health(request: web.Request):
    return web.Response(text="ok")

async def run_http_server():
    app = web.Application()
    app.router.add_get("/health", health)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=PORT)
    await site.start()

async def main():
    bot = Bot(BOT_TOKEN)
    await run_http_server()             # важно для Koyeb health checks
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
