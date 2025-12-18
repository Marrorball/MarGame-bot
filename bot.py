import os
import asyncio
import random
import string
from dataclasses import dataclass, field
from typing import Dict, List, Set, Optional

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import (
    Message,
    BotCommand,
    ReplyKeyboardMarkup,
    KeyboardButton,
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage

from aiohttp import web


# ===================== SETTINGS =====================
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
if not BOT_TOKEN:
    raise RuntimeError("Не задан BOT_TOKEN (переменная окружения)")

PORT = int(os.getenv("PORT", "8000"))

dp = Dispatcher(storage=MemoryStorage())
BOT: Optional[Bot] = None


# ===================== BUTTON TEXTS =====================
BTN_CREATE = "➕ Создать комнату"
BTN_JOIN = "🔑 Войти по коду"

BTN_LEAVE = "🚪 Выйти из комнаты"

BTN_LIVES = "❤️ Жизни"
BTN_SETWORD = "🪓 Новое слово"
BTN_START = "🚀 Старт игры"
BTN_RESTART = "🔄 Новая игра"
BTN_CLOSE = "🧹 Закрыть комнату"


# ===================== UI KEYBOARDS =====================
def kb_main() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=BTN_CREATE), KeyboardButton(text=BTN_JOIN)]],
        resize_keyboard=True,
    )


def kb_player_room() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=BTN_LEAVE)]],
        resize_keyboard=True,
    )


def kb_host_room() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_LIVES), KeyboardButton(text=BTN_SETWORD)],
            [KeyboardButton(text=BTN_START), KeyboardButton(text=BTN_RESTART)],
            [KeyboardButton(text=BTN_CLOSE)],
            [KeyboardButton(text=BTN_LEAVE)],
        ],
        resize_keyboard=True,
    )


# ===================== FSM =====================
class JoinFlow(StatesGroup):
    waiting_code = State()


class HostSetup(StatesGroup):
    waiting_lives = State()
    waiting_word = State()


class SetWordFlow(StatesGroup):
    waiting_word = State()


class LivesFlow(StatesGroup):
    waiting_lives = State()


# ===================== GAME DATA =====================
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


def tg_name(m: Message) -> str:
    return (m.from_user.first_name or m.from_user.full_name or "Игрок").strip()


def normalize_word(w: str) -> str:
    w = (w or "").strip().lower()
    return "".join(ch for ch in w if ch in ALLOWED)


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

    status_msg_id: Dict[int, int] = field(default_factory=dict)
    last_move: str = ""


rooms_by_code: Dict[str, Room] = {}
user_room: Dict[int, str] = {}


# ===================== HELPERS =====================
def get_room_by_user(uid: int) -> Optional[Room]:
    code = user_room.get(uid)
    if not code:
        return None
    return rooms_by_code.get(code)


def is_host(uid: int) -> bool:
    room = get_room_by_user(uid)
    return bool(room and room.host_id == uid)


def ui_for(uid: int) -> ReplyKeyboardMarkup:
    room = get_room_by_user(uid)
    if not room:
        return kb_main()
    return kb_host_room() if room.host_id == uid else kb_player_room()


def display_name(room: Room, uid: int) -> str:
    return room.names.get(uid) or "Игрок"


def hang_pic(fails: int) -> str:
    return HANGMAN_PICS[min(fails, len(HANGMAN_PICS) - 1)]


def shown_word(secret: str, guessed: Set[str]) -> str:
    return " ".join([ch if ch in guessed else "•" for ch in secret])


def current_turn_user(room: Room) -> int:
    if not room.order:
        return -1
    return room.order[room.turn_idx % len(room.order)]


def game_status_text(room: Room) -> str:
    lives_left = max(0, room.max_fails - room.fails)

    header = f"🎮 Комната: {room.code}\n👥 Игроков: {len(room.players)}\n❤️ Жизни: {lives_left}/{room.max_fails}\n"
    pic = hang_pic(room.fails)

    if room.secret:
        word_line = f"🪓 Слово: {shown_word(room.secret, room.guessed)}\n"
    else:
        word_line = "🪓 Слово: (хост ещё не загадал)\n"

    guessed_line = "🔤 Буквы: " + (", ".join(sorted(room.guessed)) if room.guessed else "-") + "\n"

    move_line = f"\n✍️ Последний ход: {room.last_move}\n" if room.last_move else ""

    if room.started and room.order:
        tu = current_turn_user(room)
        turn_line = f"\n➡️ Сейчас ходит: {display_name(room, tu)}\n(пиши букву или слово целиком)"
    elif room.started and not room.order:
        turn_line = "\n⚠️ Некому ходить (кроме хоста). Пусть друг войдёт по коду."
    else:
        if room.secret and room.order:
            turn_line = "\n⏸ Игра не запущена. Хост нажми 🚀 Старт игры."
        elif room.secret and not room.order:
            turn_line = "\n⏸ Ждём игроков… Пусть друг войдёт по коду."
        else:
            turn_line = "\n⏸ Хост: задай жизни и слово."

    return header + pic + "\n\n" + word_line + guessed_line + move_line + turn_line


async def upsert_status(room: Room, uid: int):
    global BOT
    if not BOT:
        return

    text = game_status_text(room)
    kb = ui_for(uid)
    mid = room.status_msg_id.get(uid)

    if mid:
        try:
            await BOT.edit_message_text(chat_id=uid, message_id=mid, text=text, reply_markup=kb)
            return
        except Exception:
            room.status_msg_id.pop(uid, None)

    try:
        msg = await BOT.send_message(uid, text, reply_markup=kb)
        room.status_msg_id[uid] = msg.message_id
    except Exception:
        pass


async def refresh_room(room: Room):
    for uid in list(room.players):
        await upsert_status(room, uid)


async def close_room(room: Room):
    global BOT
    if not BOT:
        return

    for uid in list(room.players):
        user_room.pop(uid, None)
        room.status_msg_id.pop(uid, None)
        try:
            await BOT.send_message(uid, "🧹 Комната закрыта.", reply_markup=kb_main())
        except Exception:
            pass

    rooms_by_code.pop(room.code, None)


def reset_game(room: Room):
    room.started = True
    room.guessed = set()
    room.fails = 0
    room.turn_idx = 0
    room.last_move = ""


async def start_game(room: Room):
    global BOT
    if not BOT:
        return

    if not room.secret:
        await BOT.send_message(room.host_id, "Сначала задай слово: 🪓 Новое слово", reply_markup=kb_host_room())
        return

    if len(room.order) < 1:
        await BOT.send_message(room.host_id, "Нужен хотя бы 1 отгадывающий (кроме хоста). Пусть друг войдёт по коду.", reply_markup=kb_host_room())
        await refresh_room(room)
        return

    reset_game(room)
    room.last_move = "Игра запущена 🚀"
    await refresh_room(room)


async def broadcast_finish(room: Room, text: str):
    global BOT
    if not BOT:
        return
    for uid in list(room.players):
        try:
            await BOT.send_message(uid, text, reply_markup=ui_for(uid))
        except Exception:
            pass


# ===================== CORE COMMANDS =====================
@dp.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    uid = message.from_user.id
    await message.answer(f"Привет, {tg_name(message)}! 🎮\nУправление — кнопками снизу.", reply_markup=ui_for(uid))


@dp.message(Command("create"))
async def cmd_create(message: Message, state: FSMContext):
    await state.clear()
    uid = message.from_user.id

    if get_room_by_user(uid):
        await message.answer("Ты уже в комнате. Нажми 🚪 Выйти из комнаты.", reply_markup=ui_for(uid))
        return

    code = gen_code()
    room = Room(code=code, host_id=uid)
    room.players.add(uid)
    room.names[uid] = tg_name(message)

    rooms_by_code[code] = room
    user_room[uid] = code

    await state.set_state(HostSetup.waiting_lives)
    await message.answer(
        f"✅ Комната создана: {code}\n\nШаг 1/2: введи количество жизней (например 6):",
        reply_markup=kb_host_room(),
    )


@dp.message(Command("join"))
async def cmd_join(message: Message, state: FSMContext):
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) >= 2:
        await state.clear()
        await join_by_code(message, parts[1].strip().upper())
        return

    await state.set_state(JoinFlow.waiting_code)
    await message.answer("Введи код комнаты:", reply_markup=kb_main())


@dp.message(Command("leave"))
async def cmd_leave(message: Message, state: FSMContext):
    await state.clear()
    uid = message.from_user.id
    room = get_room_by_user(uid)

    if not room:
        await message.answer("Ты не в комнате.", reply_markup=kb_main())
        return

    # убрать
    user_room.pop(uid, None)
    room.players.discard(uid)
    room.status_msg_id.pop(uid, None)

    if uid in room.order:
        room.order.remove(uid)
        room.turn_idx = room.turn_idx % max(1, len(room.order)) if room.order else 0

    if uid == room.host_id:
        await close_room(room)
        return

    room.last_move = f"{tg_name(message)} вышел(ла)"
    await refresh_room(room)
    await message.answer("👋 Ты вышел(ла) из комнаты.", reply_markup=kb_main())


@dp.message(Command("close"))
async def cmd_close(message: Message):
    uid = message.from_user.id
    room = get_room_by_user(uid)
    if not room:
        await message.answer("Ты не в комнате.", reply_markup=kb_main())
        return
    if room.host_id != uid:
        await message.answer("Только хост может закрыть комнату.", reply_markup=kb_player_room())
        return
    await close_room(room)


@dp.message(Command("startgame"))
async def cmd_startgame(message: Message):
    uid = message.from_user.id
    room = get_room_by_user(uid)
    if not room:
        await message.answer("Ты не в комнате.", reply_markup=kb_main())
        return
    if room.host_id != uid:
        await message.answer("Только хост может начать игру.", reply_markup=kb_player_room())
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
        await message.answer("Только хост может начать новую игру.", reply_markup=kb_player_room())
        return
    if not room.secret:
        await message.answer("Сначала задай слово.", reply_markup=kb_host_room())
        return
    room.last_move = "Хост: новая игра 🔄"
    await start_game(room)


# ===================== JOIN FLOW =====================
@dp.message(JoinFlow.waiting_code, F.text)
async def join_wait_code(message: Message, state: FSMContext):
    code = (message.text or "").strip().upper()
    await state.clear()
    await join_by_code(message, code)


async def join_by_code(message: Message, code: str):
    uid = message.from_user.id

    if get_room_by_user(uid):
        await message.answer("Ты уже в комнате. Нажми 🚪 Выйти из комнаты.", reply_markup=ui_for(uid))
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

    room.last_move = f"{tg_name(message)} вошёл(ла)"
    await refresh_room(room)
    await message.answer(f"✅ Ты вошёл(ла) в комнату {code}.", reply_markup=ui_for(uid))


# ===================== SETUP LIVES/WORD (важно: кнопки работают и тут!) =====================
@dp.message(HostSetup.waiting_lives, F.text)
async def host_setup_lives(message: Message, state: FSMContext):
    txt = (message.text or "").strip()

    # ВАЖНО: если нажали кнопку во время шага — обработаем
    if txt == BTN_CLOSE:
        await state.clear()
        await cmd_close(message)
        return
    if txt == BTN_LEAVE:
        await cmd_leave(message, state)
        return
    if txt == BTN_START:
        await state.clear()
        await cmd_startgame(message)
        return

    uid = message.from_user.id
    room = get_room_by_user(uid)
    if not room or room.host_id != uid:
        await state.clear()
        await message.answer("Не могу настроить: ты не хост или комнаты нет.", reply_markup=kb_main())
        return

    if not txt.isdigit():
        await message.answer("Нужно число. Например 6.")
        return

    n = int(txt)
    if n < 1:
        await message.answer("Жизни должны быть >= 1.")
        return

    room.max_fails = n
    room.last_move = f"Хост установил жизни: {n} ❤️"
    await refresh_room(room)

    await state.set_state(HostSetup.waiting_word)
    await message.answer("✅ Жизни установлены.\n\nШаг 2/2: введи слово (русские буквы):", reply_markup=kb_host_room())


@dp.message(HostSetup.waiting_word, F.text)
async def host_setup_word(message: Message, state: FSMContext):
    txt = (message.text or "").strip()

    if txt == BTN_CLOSE:
        await state.clear()
        await cmd_close(message)
        return
    if txt == BTN_LEAVE:
        await cmd_leave(message, state)
        return
    if txt == BTN_START:
        await state.clear()
        await cmd_startgame(message)
        return

    uid = message.from_user.id
    room = get_room_by_user(uid)
    if not room or room.host_id != uid:
        await state.clear()
        await message.answer("Не могу настроить: ты не хост или комнаты нет.", reply_markup=kb_main())
        return

    w = normalize_word(txt)
    if len(w) < 2:
        await message.answer("Слово не подходит (русские буквы, минимум 2). Введи другое слово:")
        return

    room.secret = w
    room.started = False
    room.guessed = set()
    room.fails = 0
    room.turn_idx = 0
    room.last_move = "Хост задал слово 🪓"
    await state.clear()

    await refresh_room(room)

    # автостарт если уже есть хотя бы 1 отгадывающий
    if len(room.order) >= 1:
        await start_game(room)


# ===================== BUTTON HANDLERS =====================
@dp.message(F.text == BTN_CREATE)
async def ui_create(message: Message, state: FSMContext):
    await cmd_create(message, state)


@dp.message(F.text == BTN_JOIN)
async def ui_join(message: Message, state: FSMContext):
    await state.set_state(JoinFlow.waiting_code)
    await message.answer("Введи код комнаты:", reply_markup=kb_main())


@dp.message(F.text == BTN_LEAVE)
async def ui_leave(message: Message, state: FSMContext):
    await cmd_leave(message, state)


@dp.message(F.text == BTN_CLOSE)
async def ui_close(message: Message):
    await cmd_close(message)


@dp.message(F.text == BTN_START)
async def ui_start(message: Message):
    await cmd_startgame(message)


@dp.message(F.text == BTN_RESTART)
async def ui_restart(message: Message):
    await cmd_restart(message)


# ===================== GAME INPUT (буква/слово) =====================
@dp.message(F.text)
async def on_text(message: Message, state: FSMContext):
    # если пользователь в FSM — не мешаем
    if await state.get_state() is not None:
        return

    uid = message.from_user.id
    room = get_room_by_user(uid)
    if not room:
        return

    room.names[uid] = tg_name(message)

    if not room.started:
        await refresh_room(room)
        return

    if not room.order:
        await refresh_room(room)
        return

    turn_uid = current_turn_user(room)
    if uid != turn_uid:
        await refresh_room(room)
        return

    if uid == room.host_id:
        await refresh_room(room)
        return

    txt = (message.text or "").strip().lower()
    if not txt:
        return

    # буква
    if len(txt) == 1:
        ch = txt
        if ch not in ALLOWED:
            room.last_move = f"{display_name(room, uid)} ввёл(ла) не-русскую букву ❌"
            await refresh_room(room)
            await message.answer("Пиши русскую букву (например: р, т, а).", reply_markup=ui_for(uid))
            return
        if ch in room.guessed:
            room.last_move = f"{display_name(room, uid)} повторил(а) букву: {ch}"
            await refresh_room(room)
            return

        room.guessed.add(ch)
        ok = ch in room.secret
        if not ok:
            room.fails += 1
        room.last_move = f"{display_name(room, uid)}: {ch} ({'✅ есть' if ok else '❌ нет'})"

    # слово
    else:
        guess = normalize_word(txt)
        if len(guess) < 2:
            room.last_move = f"{display_name(room, uid)} ввёл(ла) некорректное слово ❌"
            await refresh_room(room)
            return

        if guess == room.secret:
            room.guessed.update(set(room.secret))
            room.last_move = f"{display_name(room, uid)} угадал(а) слово целиком ✅"
        else:
            room.fails += 1
            room.last_move = f"{display_name(room, uid)} попытка словом ❌"

    await refresh_room(room)

    win = room.secret and all(ch in room.guessed for ch in room.secret)
    lose = room.fails >= room.max_fails

    if win:
        room.started = False
        await broadcast_finish(room, f"🎉 Победа! Слово: {room.secret}\nХост: 🔄 Новая игра или 🪓 Новое слово.")
        return

    if lose:
        room.started = False
        await broadcast_finish(room, f"💀 Поражение. Слово было: {room.secret}\nХост: 🔄 Новая игра или 🪓 Новое слово.")
        return

    room.turn_idx += 1
    await refresh_room(room)


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
    global BOT
    BOT = Bot(BOT_TOKEN)

    await BOT.set_my_commands([
        BotCommand(command="start", description="Запуск"),
        BotCommand(command="create", description="Создать комнату"),
        BotCommand(command="join", description="Войти по коду"),
        BotCommand(command="leave", description="Выйти из комнаты"),
        BotCommand(command="startgame", description="Хост: старт игры"),
        BotCommand(command="restart", description="Хост: новая игра"),
        BotCommand(command="close", description="Хост: закрыть комнату"),
    ])

    await run_http_server()
    await dp.start_polling(BOT)


if __name__ == "__main__":
    asyncio.run(main())