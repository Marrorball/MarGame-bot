import os
import asyncio
import random
import string
from dataclasses import dataclass, field
from typing import Dict, List, Set

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message, BotCommand

from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage

from aiogram.types import ReplyKeyboardMarkup, KeyboardButton

from aiohttp import web


# ====== SETTINGS ======
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
if not BOT_TOKEN:
    raise RuntimeError("Не задан BOT_TOKEN (в переменных окружения)")

PORT = int(os.getenv("PORT", "8000"))

# ====== DISPATCHER ======
dp = Dispatcher(storage=MemoryStorage())


# ====== UI KEYBOARDS (кнопки снизу) ======
def kb_main() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="➕ Создать комнату"), KeyboardButton(text="🔑 Войти по коду")],
            [KeyboardButton(text="📋 Комната"), KeyboardButton(text="🚪 Выйти")],
        ],
        resize_keyboard=True,
    )


def kb_host() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🪓 Загадать слово"), KeyboardButton(text="❤️ Жизни")],
            [KeyboardButton(text="🚀 Старт игры")],
            [KeyboardButton(text="⬅️ Назад")],
        ],
        resize_keyboard=True,
    )


# ====== FSM STATES (чтобы /join /setword /lives были без аргументов) ======
class JoinFlow(StatesGroup):
    waiting_code = State()


class SetWordFlow(StatesGroup):
    waiting_word = State()


class LivesFlow(StatesGroup):
    waiting_lives = State()


# ====== HANGMAN ======
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
    order: List[int] = field(default_factory=list)  # порядок отгадывающих (без хоста)
    started: bool = False

    max_fails: int = 6
    secret: str = ""
    guessed: Set[str] = field(default_factory=set)
    fails: int = 0
    turn_idx: int = 0


rooms_by_code: Dict[str, Room] = {}
user_room: Dict[int, str] = {}  # user_id -> code


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


def is_host(uid: int) -> bool:
    code = user_room.get(uid)
    if not code:
        return False
    room = rooms_by_code.get(code)
    return bool(room and room.host_id == uid)


def ui_for(uid: int) -> ReplyKeyboardMarkup:
    return kb_host() if is_host(uid) else kb_main()


# ====== COMMANDS ======
@dp.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    name = message.from_user.first_name or message.from_user.full_name or "друг"
    await message.answer(
        f"Привет, {name}! 🎮\n\n"
        "Я игровой бот с комнатами.\n"
        "Управление — кнопками снизу.\n\n"
        "Если хочешь — команды тоже работают: /create /join /room /leave",
        reply_markup=kb_main(),
    )


@dp.message(Command("create"))
async def create_room(message: Message):
    uid = message.from_user.id
    if uid in user_room:
        await message.answer("Ты уже в комнате. Нажми 🚪 Выйти, если хочешь выйти.", reply_markup=ui_for(uid))
        return

    code = gen_code()
    room = Room(code=code, host_id=uid)
    room.players.add(uid)
    rooms_by_code[code] = room
    user_room[uid] = code

    await message.answer(
        f"✅ Комната создана: {code}\n\n"
        "Дай друзьям код и пусть нажмут 🔑 Войти по коду.\n"
        "Ты хост — нажми 🪓 Загадать слово, потом 🚀 Старт игры.",
        reply_markup=kb_host(),
    )


@dp.message(Command("room"))
async def room_info(message: Message):
    uid = message.from_user.id
    code = user_room.get(uid)
    if not code:
        await message.answer("Ты не в комнате. Нажми ➕ Создать комнату или 🔑 Войти по коду.", reply_markup=kb_main())
        return

    room = rooms_by_code.get(code)
    if not room:
        user_room.pop(uid, None)
        await message.answer("Комната не найдена. Нажми ➕ Создать комнату.", reply_markup=kb_main())
        return

    text = render(room)
    if room.started and room.order:
        text += f"\n➡️ Сейчас ходит игрок: {current_turn_user(room)} (user_id)"
    await message.answer(text, reply_markup=ui_for(uid))


@dp.message(Command("leave"))
async def leave_room(message: Message):
    uid = message.from_user.id
    code = user_room.pop(uid, None)
    if not code:
        await message.answer("Ты не в комнате.", reply_markup=kb_main())
        return

    room = rooms_by_code.get(code)
    if not room:
        await message.answer("Ок.", reply_markup=kb_main())
        return

    room.players.discard(uid)
    if uid in room.order:
        room.order.remove(uid)
        room.turn_idx = room.turn_idx % max(1, len(room.order))

    name = message.from_user.full_name

    # Если хост вышел — закрываем комнату
    if uid == room.host_id:
        await broadcast(dp.bot, room, "🧹 Хост вышел — комната закрыта.")
        for p in list(room.players):
            user_room.pop(p, None)
        rooms_by_code.pop(code, None)
        await message.answer(f"🧹 Ты вышел(ла). Комната {code} закрыта.", reply_markup=kb_main())
        return

    await broadcast(dp.bot, room, f"👋 {name} вышел(ла) из комнаты. Игроков: {len(room.players)}")
    await message.answer(f"👋 Ты вышел(ла) из комнаты {code}.", reply_markup=kb_main())


# ====== JOIN FLOW (без CODE) ======
@dp.message(Command("join"))
async def join_cmd(message: Message, state: FSMContext):
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) >= 2:
        await state.clear()
        await _join_by_code(message, parts[1].strip().upper())
        return

    await state.set_state(JoinFlow.waiting_code)
    await message.answer("Введи код комнаты (например A7K2Q):", reply_markup=kb_main())


@dp.message(JoinFlow.waiting_code, F.text)
async def join_wait_code(message: Message, state: FSMContext):
    code = (message.text or "").strip().upper()
    await state.clear()
    await _join_by_code(message, code)


async def _join_by_code(message: Message, code: str):
    uid = message.from_user.id
    if uid in user_room:
        await message.answer("Ты уже в комнате. Нажми 🚪 Выйти, если хочешь выйти.", reply_markup=ui_for(uid))
        return

    room = rooms_by_code.get(code)
    if not room:
        await message.answer("Комната не найдена. Проверь код и попробуй ещё раз: 🔑 Войти по коду", reply_markup=kb_main())
        return

    room.players.add(uid)
    user_room[uid] = code

    # хост не угадывает — угадывают остальные по очереди (в порядке входа)
    if uid != room.host_id and uid not in room.order:
        room.order.append(uid)

    name = message.from_user.full_name
    await message.answer(f"✅ Ты вошёл(ла) в комнату {code}.", reply_markup=ui_for(uid))
    await broadcast(dp.bot, room, f"👤 {name} вошёл(ла) в комнату. Игроков: {len(room.players)}")


# ====== HOST FLOW: SETWORD (без слова) ======
@dp.message(Command("setword"))
async def setword_cmd(message: Message, state: FSMContext):
    uid = message.from_user.id
    code = user_room.get(uid)
    room = rooms_by_code.get(code) if code else None
    if not room:
        await message.answer("Ты не в комнате.", reply_markup=kb_main())
        return
    if uid != room.host_id:
        await message.answer("Только хост может загадывать слово.", reply_markup=kb_main())
        return
    if room.started:
        await message.answer("Игра уже началась — нельзя менять слово.", reply_markup=kb_host())
        return

    parts = (message.text or "").split(maxsplit=1)
    if len(parts) >= 2:
        w = normalize_word(parts[1])
        await state.clear()
        await _apply_word(message, room, w)
        return

    await state.set_state(SetWordFlow.waiting_word)
    await message.answer("Введи слово (русские буквы):", reply_markup=kb_host())


@dp.message(SetWordFlow.waiting_word, F.text)
async def setword_wait(message: Message, state: FSMContext):
    uid = message.from_user.id
    code = user_room.get(uid)
    room = rooms_by_code.get(code) if code else None
    if not room:
        await state.clear()
        await message.answer("Комната не найдена.", reply_markup=kb_main())
        return

    w = normalize_word(message.text or "")
    await state.clear()
    await _apply_word(message, room, w)


async def _apply_word(message: Message, room: Room, w: str):
    if len(w) < 2:
        await message.answer("Слово слишком короткое. Нажми 🪓 Загадать слово и попробуй ещё раз.", reply_markup=kb_host())
        return
    room.secret = w
    room.guessed = set()
    room.fails = 0
    room.turn_idx = 0
    room.started = False
    await message.answer("✅ Слово загадано. Теперь нажми 🚀 Старт игры.", reply_markup=kb_host())


# ====== HOST FLOW: LIVES (без N) ======
@dp.message(Command("lives"))
async def lives_cmd(message: Message, state: FSMContext):
    uid = message.from_user.id
    code = user_room.get(uid)
    room = rooms_by_code.get(code) if code else None
    if not room:
        await message.answer("Ты не в комнате.", reply_markup=kb_main())
        return
    if uid != room.host_id:
        await message.answer("Только хост может менять жизни.", reply_markup=kb_main())
        return

    parts = (message.text or "").split()
    if len(parts) >= 2 and parts[1].isdigit():
        n = int(parts[1])
        await state.clear()
        await _apply_lives(message, room, n)
        return

    await state.set_state(LivesFlow.waiting_lives)
    await message.answer("Введи число жизней (например 6):", reply_markup=kb_host())


@dp.message(LivesFlow.waiting_lives, F.text)
async def lives_wait(message: Message, state: FSMContext):
    txt = (message.text or "").strip()
    if not txt.isdigit():
        await message.answer("Нужно число. Например 6.", reply_markup=kb_host())
        return

    uid = message.from_user.id
    code = user_room.get(uid)
    room = rooms_by_code.get(code) if code else None
    if not room:
        await state.clear()
        await message.answer("Комната не найдена.", reply_markup=kb_main())
        return

    await state.clear()
    await _apply_lives(message, room, int(txt))


async def _apply_lives(message: Message, room: Room, n: int):
    if n < 1:
        await message.answer("Жизни должны быть >= 1", reply_markup=kb_host())
        return
    room.max_fails = n
    await message.answer(f"✅ Жизни установлены: {n}", reply_markup=kb_host())


# ====== HOST: START GAME ======
@dp.message(Command("startgame"))
async def start_game(message: Message):
    uid = message.from_user.id
    code = user_room.get(uid)
    room = rooms_by_code.get(code) if code else None
    if not room:
        await message.answer("Ты не в комнате.", reply_markup=kb_main())
        return
    if uid != room.host_id:
        await message.answer("Только хост может начать игру.", reply_markup=kb_main())
        return
    if not room.secret:
        await message.answer("Сначала загадай слово: 🪓 Загадать слово", reply_markup=kb_host())
        return
    if not room.order:
        await message.answer("Нужны отгадывающие (кроме хоста). Пусть друзья войдут по коду.", reply_markup=kb_host())
        return

    room.started = True
    room.guessed = set()
    room.fails = 0
    room.turn_idx = 0

    await broadcast(dp.bot, room, "🚀 Игра началась!\n\n" + render(room))
    await broadcast(dp.bot, room, f"➡️ Первый ход: {current_turn_user(room)} (user_id)\nПиши букву или слово.")


# ====== BUTTON HANDLERS (кнопки снизу) ======
@dp.message(F.text == "➕ Создать комнату")
async def ui_create(message: Message):
    await create_room(message)


@dp.message(F.text == "🔑 Войти по коду")
async def ui_join(message: Message, state: FSMContext):
    await state.set_state(JoinFlow.waiting_code)
    await message.answer("Введи код комнаты (например A7K2Q):", reply_markup=kb_main())


@dp.message(F.text == "📋 Комната")
async def ui_room(message: Message):
    await room_info(message)


@dp.message(F.text == "🚪 Выйти")
async def ui_leave(message: Message):
    await leave_room(message)


@dp.message(F.text == "🪓 Загадать слово")
async def ui_setword(message: Message, state: FSMContext):
    await setword_cmd(message, state)


@dp.message(F.text == "❤️ Жизни")
async def ui_lives(message: Message, state: FSMContext):
    await lives_cmd(message, state)


@dp.message(F.text == "🚀 Старт игры")
async def ui_startgame(message: Message):
    await start_game(message)


@dp.message(F.text == "⬅️ Назад")
async def ui_back(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Ок 🙂", reply_markup=kb_main())


# ====== GAME INPUT (буква/слово) ======
@dp.message(F.text)
async def on_text(message: Message, state: FSMContext):
    # если человек сейчас в режиме ввода (код/слово/жизни) — не мешаем FSM
    if await state.get_state() is not None:
        return

    uid = message.from_user.id
    code = user_room.get(uid)
    if not code:
        return
    room = rooms_by_code.get(code)
    if not room or not room.started:
        return

    # ходит только текущий игрок
    turn_uid = current_turn_user(room)
    if uid != turn_uid:
        await message.answer("Сейчас не твой ход 🙂", reply_markup=ui_for(uid))
        return

    # хост не угадывает
    if uid == room.host_id:
        await message.answer("Хост не угадывает 🙂", reply_markup=kb_host())
        return

    txt = (message.text or "").strip().lower()
    if not txt:
        return

    # буква
    if len(txt) == 1:
        ch = txt
        if ch not in ALLOWED:
            await message.answer("Пиши русскую букву.", reply_markup=ui_for(uid))
            return
        if ch in room.guessed:
            await message.answer("Эта буква уже была.", reply_markup=ui_for(uid))
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

    # победа
    if all(ch in room.guessed for ch in room.secret):
        await broadcast(dp.bot, room, f"🎉 Победа! Слово: {room.secret}\nХост может загадать новое слово и начать заново.")
        room.started = False
        return

    # поражение
    if room.fails >= room.max_fails:
        await broadcast(dp.bot, room, f"💀 Поражение. Слово было: {room.secret}\nХост может загадать новое слово и начать заново.")
        room.started = False
        return

    # следующий ход
    room.turn_idx += 1
    await broadcast(dp.bot, room, f"➡️ Следующий ход: {current_turn_user(room)} (user_id)")


# ====== HTTP health endpoint for Koyeb ======
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

    # команды в меню (кнопка /)
    await bot.set_my_commands([
        BotCommand(command="start", description="Запуск"),
        BotCommand(command="create", description="Создать комнату"),
        BotCommand(command="join", description="Войти по коду"),
        BotCommand(command="room", description="Состояние комнаты"),
        BotCommand(command="leave", description="Выйти"),
        BotCommand(command="setword", description="Хост: загадать слово"),
        BotCommand(command="lives", description="Хост: жизни"),
        BotCommand(command="startgame", description="Хост: начать игру"),
    ])

    await run_http_server()
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())