import os
import asyncio
import random
import string
from dataclasses import dataclass, field
from typing import Dict, List, Set, Optional

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message, BotCommand
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton

from aiohttp import web


# ===================== SETTINGS =====================
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
if not BOT_TOKEN:
    raise RuntimeError("Не задан BOT_TOKEN (переменная окружения)")

PORT = int(os.getenv("PORT", "8000"))

dp = Dispatcher(storage=MemoryStorage())


# ===================== UI BUTTONS =====================
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
            [KeyboardButton(text="❤️ Жизни"), KeyboardButton(text="🪓 Новое слово")],
            [KeyboardButton(text="🚀 Старт игры"), KeyboardButton(text="🔄 Новая игра")],
            [KeyboardButton(text="🧹 Закрыть комнату")],
            [KeyboardButton(text="⬅️ Назад")],
        ],
        resize_keyboard=True,
    )


# ===================== FSM STATES =====================
class JoinFlow(StatesGroup):
    waiting_code = State()


class HostSetup(StatesGroup):
    waiting_lives = State()
    waiting_word = State()


class SetWordFlow(StatesGroup):
    waiting_word = State()


class LivesFlow(StatesGroup):
    waiting_lives = State()


# ===================== HANGMAN =====================
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

ALLOWED = set("абвгдеёжзийклмнопрстуфхцчшщъыьэюя-")  # русские + дефис


def gen_code(n: int = 5) -> str:
    return "".join(random.choice(string.ascii_uppercase + string.digits) for _ in range(n))


def tg_name(m: Message) -> str:
    return (m.from_user.first_name or m.from_user.full_name or "Игрок").strip()


def normalize_word(w: str) -> str:
    w = (w or "").strip().lower()
    w2 = "".join(ch for ch in w if ch in ALLOWED)
    return w2


@dataclass
class Room:
    code: str
    host_id: int
    players: Set[int] = field(default_factory=set)
    order: List[int] = field(default_factory=list)      # отгадывающие (без хоста)
    names: Dict[int, str] = field(default_factory=dict) # user_id -> имя

    started: bool = False
    max_fails: int = 6
    secret: str = ""
    guessed: Set[str] = field(default_factory=set)
    fails: int = 0
    turn_idx: int = 0


rooms_by_code: Dict[str, Room] = {}
user_room: Dict[int, str] = {}  # user_id -> code


# ===================== HELPERS =====================
async def safe_send(bot: Bot, user_id: int, text: str):
    try:
        await bot.send_message(user_id, text)
    except Exception:
        pass


async def broadcast(bot: Bot, room: Room, text: str):
    for uid in list(room.players):
        await safe_send(bot, uid, text)


def display_name(room: Room, uid: int) -> str:
    return room.names.get(uid) or "Игрок"


def shown_word(secret: str, guessed: Set[str]) -> str:
    return " ".join([ch if ch in guessed else "•" for ch in secret])


def hang_pic(fails: int) -> str:
    return HANGMAN_PICS[min(fails, len(HANGMAN_PICS) - 1)]


def render(room: Room) -> str:
    lives_left = max(0, room.max_fails - room.fails)
    shown = shown_word(room.secret, room.guessed) if room.secret else "(слово ещё не задано)"
    guessed = ", ".join(sorted(room.guessed)) if room.guessed else "-"
    return (
        f"🎮 Комната: {room.code}\n"
        f"👥 Игроков: {len(room.players)}\n"
        f"❤️ Жизни: {lives_left}/{room.max_fails}\n"
        f"{hang_pic(room.fails)}\n\n"
        f"🪓 Слово: {shown}\n"
        f"🔤 Буквы: {guessed}\n"
    )


def current_turn_user(room: Room) -> int:
    if not room.order:
        return -1
    return room.order[room.turn_idx % len(room.order)]


def is_host(uid: int) -> bool:
    code = user_room.get(uid)
    room = rooms_by_code.get(code) if code else None
    return bool(room and room.host_id == uid)


def ui_for(uid: int) -> ReplyKeyboardMarkup:
    return kb_host() if is_host(uid) else kb_main()


def get_room_by_user(uid: int) -> Optional[Room]:
    code = user_room.get(uid)
    if not code:
        return None
    return rooms_by_code.get(code)


def close_room(room: Room):
    for uid in list(room.players):
        user_room.pop(uid, None)
    rooms_by_code.pop(room.code, None)


def reset_game(room: Room):
    room.started = True
    room.guessed = set()
    room.fails = 0
    room.turn_idx = 0


async def start_game(room: Room):
    if not room.secret:
        await safe_send(dp.bot, room.host_id, "Сначала задай слово: 🪓 Новое слово")
        return
    if len(room.order) < 1:
        await safe_send(dp.bot, room.host_id, "Нужен хотя бы 1 отгадывающий (кроме хоста). Пусть друг войдёт по коду.")
        return

    reset_game(room)
    first_uid = current_turn_user(room)
    await broadcast(dp.bot, room, "🚀 Игра началась!\n\n" + render(room))
    await broadcast(dp.bot, room, f"➡️ Сейчас ходит: {display_name(room, first_uid)}\nПиши одну букву или слово целиком.")


# ===================== COMMANDS =====================
@dp.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
        f"Привет, {tg_name(message)}! 🎮\n\n"
        "Управление — кнопками снизу.",
        reply_markup=kb_main(),
    )


@dp.message(Command("room"))
async def cmd_room(message: Message):
    uid = message.from_user.id
    room = get_room_by_user(uid)
    if not room:
        await message.answer("Ты не в комнате. Нажми ➕ Создать комнату или 🔑 Войти по коду.", reply_markup=kb_main())
        return

    room.names[uid] = tg_name(message)

    txt = render(room)
    if room.started and room.order:
        tu = current_turn_user(room)
        txt += f"\n➡️ Сейчас ходит: {display_name(room, tu)}"
    elif room.started and not room.order:
        txt += "\n⚠️ Нет отгадывающих (кроме хоста)."
    await message.answer(txt, reply_markup=ui_for(uid))


@dp.message(Command("leave"))
async def cmd_leave(message: Message, state: FSMContext):
    await state.clear()
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

    name = tg_name(message)

    if uid == room.host_id:
        await broadcast(dp.bot, room, "🧹 Хост вышел — комната закрыта.")
        close_room(room)
        await message.answer("🧹 Комната закрыта.", reply_markup=kb_main())
        return

    await broadcast(dp.bot, room, f"👋 {name} вышел(ла). Игроков: {len(room.players)}")
    await message.answer("👋 Ты вышел(ла).", reply_markup=kb_main())


# ===================== CREATE (wizard: lives -> word -> autostart) =====================
@dp.message(Command("create"))
async def cmd_create(message: Message, state: FSMContext):
    await state.clear()
    uid = message.from_user.id
    if uid in user_room:
        await message.answer("Ты уже в комнате. Нажми 🚪 Выйти, если хочешь выйти.", reply_markup=ui_for(uid))
        return

    code = gen_code()
    room = Room(code=code, host_id=uid)
    room.players.add(uid)
    room.names[uid] = tg_name(message)

    rooms_by_code[code] = room
    user_room[uid] = code

    await state.set_state(HostSetup.waiting_lives)
    await message.answer(
        f"✅ Комната создана: {code}\n\n"
        "Шаг 1/2: введи количество жизней (например 6):",
        reply_markup=kb_host(),
    )


@dp.message(HostSetup.waiting_lives, F.text)
async def host_setup_lives(message: Message, state: FSMContext):
    uid = message.from_user.id
    room = get_room_by_user(uid)
    if not room or room.host_id != uid:
        await state.clear()
        await message.answer("Не могу настроить: ты не хост или комнаты нет.", reply_markup=kb_main())
        return

    txt = (message.text or "").strip()
    if not txt.isdigit():
        await message.answer("Нужно число. Например 6.")
        return
    n = int(txt)
    if n < 1:
        await message.answer("Жизни должны быть >= 1. Например 6.")
        return

    room.max_fails = n
    await state.set_state(HostSetup.waiting_word)
    await message.answer("✅ Жизни установлены.\n\nШаг 2/2: введи слово (русские буквы):", reply_markup=kb_host())


@dp.message(HostSetup.waiting_word, F.text)
async def host_setup_word(message: Message, state: FSMContext):
    uid = message.from_user.id
    room = get_room_by_user(uid)
    if not room or room.host_id != uid:
        await state.clear()
        await message.answer("Не могу настроить: ты не хост или комнаты нет.", reply_markup=kb_main())
        return

    raw = (message.text or "").strip()
    w = normalize_word(raw)
    if len(w) < 2:
        await message.answer("Слово не подходит (русские буквы, минимум 2). Введи другое слово:")
        return

    room.secret = w
    room.started = False
    room.guessed = set()
    room.fails = 0
    room.turn_idx = 0

    await state.clear()

    if len(room.order) >= 1:
        await message.answer("✅ Слово задано. Запускаю игру! 🚀", reply_markup=kb_host())
        await start_game(room)
    else:
        await message.answer(
            "✅ Слово задано.\n"
            "Теперь пусть хотя бы 1 друг войдёт по коду.\n"
            "Когда будет игрок — нажми 🚀 Старт игры.",
            reply_markup=kb_host(),
        )


# ===================== JOIN =====================
@dp.message(Command("join"))
async def cmd_join(message: Message, state: FSMContext):
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) >= 2:
        await state.clear()
        await join_by_code(message, parts[1].strip().upper())
        return

    await state.set_state(JoinFlow.waiting_code)
    await message.answer("Введи код комнаты:", reply_markup=kb_main())


@dp.message(JoinFlow.waiting_code, F.text)
async def join_wait_code(message: Message, state: FSMContext):
    code = (message.text or "").strip().upper()
    await state.clear()
    await join_by_code(message, code)


async def join_by_code(message: Message, code: str):
    uid = message.from_user.id

    if uid in user_room:
        await message.answer("Ты уже в комнате. Нажми 🚪 Выйти, если хочешь выйти.", reply_markup=ui_for(uid))
        return

    room = rooms_by_code.get(code)
    if not room:
        await message.answer("Комната не найдена. Проверь код.", reply_markup=kb_main())
        return

    room.players.add(uid)
    room.names[uid] = tg_name(message)
    user_room[uid] = code

    if uid != room.host_id and uid not in room.order:
        room.order.append(uid)

    await message.answer(f"✅ Ты вошёл(ла) в комнату {code}.", reply_markup=kb_main())
    await broadcast(dp.bot, room, f"👤 {tg_name(message)} вошёл(ла). Игроков: {len(room.players)}")

    # если слово уже задано — напомним хосту, что можно стартовать
    if room.secret and not room.started:
        await safe_send(dp.bot, room.host_id, "✅ В комнате появился игрок. Можно нажимать 🚀 Старт игры.")


# ===================== HOST COMMANDS (also used by buttons) =====================
@dp.message(Command("startgame"))
async def cmd_startgame(message: Message):
    uid = message.from_user.id
    room = get_room_by_user(uid)
    if not room:
        await message.answer("Ты не в комнате.", reply_markup=kb_main())
        return
    if room.host_id != uid:
        await message.answer("Только хост может начать игру.", reply_markup=kb_main())
        return
    await start_game(room)


@dp.message(Command("restart"))
async def cmd_restart(message: Message):
    uid = message.from_user.id
    room = get_room_by_user(uid)
    if not room:
        await message.answer("Ты не в комнате.", reply_markup=kb_main())
        return
    if room.host_id != uid:
        await message.answer("Только хост может перезапустить игру.", reply_markup=kb_main())
        return
    await start_game(room)


@dp.message(Command("close"))
async def cmd_close(message: Message):
    uid = message.from_user.id
    room = get_room_by_user(uid)
    if not room:
        await message.answer("Ты не в комнате.", reply_markup=kb_main())
        return
    if room.host_id != uid:
        await message.answer("Только хост может закрыть комнату.", reply_markup=kb_main())
        return

    await broadcast(dp.bot, room, "🧹 Хост закрыл комнату. Игра завершена.")
    close_room(room)
    await message.answer("🧹 Комната закрыта.", reply_markup=kb_main())


@dp.message(Command("setword"))
async def cmd_setword(message: Message, state: FSMContext):
    uid = message.from_user.id
    room = get_room_by_user(uid)
    if not room:
        await message.answer("Ты не в комнате.", reply_markup=kb_main())
        return
    if room.host_id != uid:
        await message.answer("Только хост может задавать слово.", reply_markup=kb_main())
        return

    parts = (message.text or "").split(maxsplit=1)
    if len(parts) >= 2:
        w = normalize_word(parts[1].strip())
        if len(w) < 2:
            await message.answer("Слово не подходит (русские буквы, минимум 2).", reply_markup=kb_host())
            return
        room.secret = w
        room.started = False
        room.guessed = set()
        room.fails = 0
        room.turn_idx = 0
        await message.answer("✅ Новое слово задано. Жми 🚀 Старт игры.", reply_markup=kb_host())
        return

    await state.set_state(SetWordFlow.waiting_word)
    await message.answer("Введи новое слово (русские буквы):", reply_markup=kb_host())


@dp.message(SetWordFlow.waiting_word, F.text)
async def setword_wait(message: Message, state: FSMContext):
    uid = message.from_user.id
    room = get_room_by_user(uid)
    if not room or room.host_id != uid:
        await state.clear()
        await message.answer("Не могу: ты не хост или комнаты нет.", reply_markup=kb_main())
        return

    w = normalize_word(message.text or "")
    if len(w) < 2:
        await message.answer("Слово не подходит. Попробуй ещё раз:")
        return

    room.secret = w
    room.started = False
    room.guessed = set()
    room.fails = 0
    room.turn_idx = 0

    await state.clear()
    await message.answer("✅ Новое слово задано. Жми 🚀 Старт игры.", reply_markup=kb_host())


@dp.message(Command("lives"))
async def cmd_lives(message: Message, state: FSMContext):
    uid = message.from_user.id
    room = get_room_by_user(uid)
    if not room:
        await message.answer("Ты не в комнате.", reply_markup=kb_main())
        return
    if room.host_id != uid:
        await message.answer("Только хост может менять жизни.", reply_markup=kb_main())
        return

    parts = (message.text or "").split(maxsplit=1)
    if len(parts) >= 2 and parts[1].isdigit():
        n = int(parts[1])
        if n < 1:
            await message.answer("Жизни должны быть >= 1", reply_markup=kb_host())
            return
        room.max_fails = n
        await message.answer(f"✅ Жизни установлены: {n}", reply_markup=kb_host())
        return

    await state.set_state(LivesFlow.waiting_lives)
    await message.answer("Введи число жизней (например 6):", reply_markup=kb_host())


@dp.message(LivesFlow.waiting_lives, F.text)
async def lives_wait(message: Message, state: FSMContext):
    uid = message.from_user.id
    room = get_room_by_user(uid)
    if not room or room.host_id != uid:
        await state.clear()
        await message.answer("Не могу: ты не хост или комнаты нет.", reply_markup=kb_main())
        return

    txt = (message.text or "").strip()
    if not txt.isdigit():
        await message.answer("Нужно число. Например 6.")
        return
    n = int(txt)
    if n < 1:
        await message.answer("Жизни должны быть >= 1. Например 6.")
        return

    room.max_fails = n
    await state.clear()
    await message.answer(f"✅ Жизни установлены: {n}", reply_markup=kb_host())


# ===================== BUTTON HANDLERS =====================
@dp.message(F.text == "➕ Создать комнату")
async def ui_create(message: Message, state: FSMContext):
    await cmd_create(message, state)


@dp.message(F.text == "🔑 Войти по коду")
async def ui_join(message: Message, state: FSMContext):
    await state.set_state(JoinFlow.waiting_code)
    await message.answer("Введи код комнаты:", reply_markup=kb_main())


@dp.message(F.text == "📋 Комната")
async def ui_room(message: Message):
    await cmd_room(message)


@dp.message(F.text == "🚪 Выйти")
async def ui_leave(message: Message, state: FSMContext):
    await cmd_leave(message, state)


@dp.message(F.text == "❤️ Жизни")
async def ui_lives(message: Message, state: FSMContext):
    await cmd_lives(message, state)


@dp.message(F.text == "🪓 Новое слово")
async def ui_setword(message: Message, state: FSMContext):
    await cmd_setword(message, state)


@dp.message(F.text == "🚀 Старт игры")
async def ui_startgame(message: Message):
    await cmd_startgame(message)


@dp.message(F.text == "🔄 Новая игра")
async def ui_restart(message: Message):
    await cmd_restart(message)


@dp.message(F.text == "🧹 Закрыть комнату")
async def ui_close(message: Message):
    await cmd_close(message)


@dp.message(F.text == "⬅️ Назад")
async def ui_back(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Ок 🙂", reply_markup=kb_main())


# ===================== GAME INPUT (буква/слово) =====================
@dp.message(F.text)
async def on_text(message: Message, state: FSMContext):
    # если пользователь сейчас в режиме ввода (код/жизни/слово), не мешаем FSM
    if await state.get_state() is not None:
        return

    uid = message.from_user.id
    room = get_room_by_user(uid)

    if not room:
        await message.answer("Ты не в комнате. Нажми 🔑 Войти по коду или ➕ Создать комнату.", reply_markup=kb_main())
        return

    room.names[uid] = tg_name(message)

    if not room.started:
        await message.answer("Игра ещё не началась. Жди, пока хост нажмёт 🚀 Старт игры.", reply_markup=ui_for(uid))
        return

    if not room.order:
        await message.answer("Нет отгадывающих (кроме хоста). Пусть кто-то войдёт по коду.", reply_markup=kb_host())
        return

    # ходит только текущий игрок
    turn_uid = current_turn_user(room)
    if uid != turn_uid:
        await message.answer(f"Сейчас ходит: {display_name(room, turn_uid)} 🙂", reply_markup=ui_for(uid))
        return

    # хост не угадывает
    if uid == room.host_id:
        await message.answer("Хост не угадывает 🙂", reply_markup=kb_host())
        return

    txt = (message.text or "").strip().lower()
    if not txt:
        return

    # 1 буква
    if len(txt) == 1:
        ch = txt
        if ch not in ALLOWED:
            await message.answer("Пиши одну русскую букву.", reply_markup=ui_for(uid))
            return
        if ch in room.guessed:
            await message.answer("Эта буква уже была.", reply_markup=ui_for(uid))
            return

        room.guessed.add(ch)
        if ch not in room.secret:
            room.fails += 1
    else:
        guess = normalize_word(txt)
        if len(guess) < 2:
            await message.answer("Если слово — пиши слово целиком русскими буквами.", reply_markup=ui_for(uid))
            return
        if guess == room.secret:
            room.guessed.update(set(room.secret))
        else:
            room.fails += 1

    await broadcast(dp.bot, room, f"✍️ Ход: {display_name(room, uid)}\n\n{render(room)}")

    # победа
    if all(ch in room.guessed for ch in room.secret):
        await broadcast(dp.bot, room, f"🎉 Победа! Слово: {room.secret}\nХост может нажать 🔄 Новая игра или 🪓 Новое слово.")
        room.started = False
        return

    # поражение
    if room.fails >= room.max_fails:
        await broadcast(dp.bot, room, f"💀 Поражение. Слово было: {room.secret}\nХост может нажать 🔄 Новая игра или 🪓 Новое слово.")
        room.started = False
        return

    # следующий ход
    room.turn_idx += 1
    next_uid = current_turn_user(room)
    await broadcast(dp.bot, room, f"➡️ Следующий ход: {display_name(room, next_uid)}")


# ===================== HEALTHCHECK (Koyeb) =====================
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

    await bot.set_my_commands([
        BotCommand(command="start", description="Запуск"),
        BotCommand(command="create", description="Создать комнату"),
        BotCommand(command="join", description="Войти по коду"),
        BotCommand(command="room", description="Состояние комнаты"),
        BotCommand(command="leave", description="Выйти"),
        BotCommand(command="startgame", description="Хост: старт игры"),
        BotCommand(command="restart", description="Хост: новая игра"),
        BotCommand(command="setword", description="Хост: новое слово"),
        BotCommand(command="lives", description="Хост: жизни"),
        BotCommand(command="close", description="Хост: закрыть комнату"),
    ])

    await run_http_server()
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())