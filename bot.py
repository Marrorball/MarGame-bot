import os
import asyncio
import random
import string
from dataclasses import dataclass, field
from typing import Dict, List, Set, Optional, Tuple

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import (
    Message,
    BotCommand,
    ReplyKeyboardMarkup,
    KeyboardButton,
    BufferedInputFile,
    InputMediaPhoto,
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage

from aiohttp import web
from PIL import Image, ImageDraw


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

BTN_KICK = "👢 Удалить игрока"
BTN_TRANSFER = "👑 Выбрать другого хоста"

BTN_COMMENT = "💬 Комментарий"
BTN_CANCEL = "❌ Отмена"


# ===================== UI KEYBOARDS =====================
def kb_main() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=BTN_CREATE), KeyboardButton(text=BTN_JOIN)]],
        resize_keyboard=True,
    )


def kb_player_room() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_COMMENT)],
            [KeyboardButton(text=BTN_LEAVE)],
        ],
        resize_keyboard=True,
    )


def kb_host_room() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_LIVES), KeyboardButton(text=BTN_SETWORD)],
            [KeyboardButton(text=BTN_START), KeyboardButton(text=BTN_RESTART)],
            [KeyboardButton(text=BTN_KICK), KeyboardButton(text=BTN_TRANSFER)],
            [KeyboardButton(text=BTN_CLOSE)],
            [KeyboardButton(text=BTN_COMMENT)],
            [KeyboardButton(text=BTN_LEAVE)],
        ],
        resize_keyboard=True,
    )


def kb_cancel_only() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=BTN_CANCEL)]],
        resize_keyboard=True,
    )


# ===================== FSM =====================
class JoinFlow(StatesGroup):
    waiting_code = State()


class HostSetup(StatesGroup):
    waiting_lives = State()
    waiting_word = State()


class KickFlow(StatesGroup):
    waiting_index = State()


class TransferFlow(StatesGroup):
    waiting_index = State()


class CommentFlow(StatesGroup):
    waiting_text = State()


# ===================== GAME DATA =====================
ALLOWED = set("абвгдеёжзийклмнопрстуфхцчшщъыьэюя-")


def gen_code(n: int = 5) -> str:
    return "".join(random.choice(string.ascii_uppercase + string.digits) for _ in range(n))


def tg_name(m: Message) -> str:
    return (m.from_user.first_name or m.from_user.full_name or "Игрок").strip()


def tg_tag(m: Message) -> str:
    u = m.from_user
    if u and u.username:
        return f"@{u.username}"
    return tg_name(m)


def normalize_word(w: str) -> str:
    w = (w or "").strip().lower()
    return "".join(ch for ch in w if ch in ALLOWED)


@dataclass
class Room:
    code: str
    host_id: int
    players: Set[int] = field(default_factory=set)
    order: List[int] = field(default_factory=list)       # отгадывающие (без хоста)
    names: Dict[int, str] = field(default_factory=dict)
    tags: Dict[int, str] = field(default_factory=dict)

    started: bool = False
    max_fails: int = 6
    secret: str = ""
    guessed: Set[str] = field(default_factory=set)
    fails: int = 0
    turn_idx: int = 0

    status_msg_ids: Dict[int, List[int]] = field(default_factory=dict)  # uid -> list of message_ids
    last_move: str = ""

    img_cache: Dict[Tuple[int, int], bytes] = field(default_factory=dict)


rooms_by_code: Dict[str, Room] = {}
user_room: Dict[int, str] = {}


# ===================== HELPERS =====================
def get_room_by_user(uid: int) -> Optional[Room]:
    code = user_room.get(uid)
    if not code:
        return None
    return rooms_by_code.get(code)


def ui_for(uid: int) -> ReplyKeyboardMarkup:
    room = get_room_by_user(uid)
    if not room:
        return kb_main()
    return kb_host_room() if room.host_id == uid else kb_player_room()


def current_turn_user(room: Room) -> int:
    if not room.order:
        return -1
    return room.order[room.turn_idx % len(room.order)]


def shown_word(secret: str, guessed: Set[str]) -> str:
    return " ".join([ch if ch in guessed else "•" for ch in secret])


def game_status_text(room: Room) -> str:
    lives_left = max(0, room.max_fails - room.fails)

    header = f"🎮 Комната: {room.code}\n👥 Игроков: {len(room.players)}\n❤️ Жизни: {lives_left}/{room.max_fails}\n"
    word_line = f"🪓 Слово: {shown_word(room.secret, room.guessed)}\n" if room.secret else "🪓 Слово: (хост ещё не загадал)\n"
    guessed_line = "🔤 Буквы: " + (", ".join(sorted(room.guessed)) if room.guessed else "-") + "\n"
    move_line = f"\n✍️ Последний ход: {room.last_move}\n" if room.last_move else ""

    if room.started and room.order:
        tu = current_turn_user(room)
        turn_line = f"\n➡️ Сейчас ходит: {room.tags.get(tu, room.names.get(tu, 'Игрок'))}\n(пиши букву или слово целиком)"
    elif room.started and not room.order:
        turn_line = "\n⚠️ Некому ходить (кроме хоста). Пусть друг войдёт по коду."
    else:
        if room.secret and room.order:
            turn_line = "\n⏸ Игра не запущена. Хост нажми 🚀 Старт игры."
        elif room.secret and not room.order:
            turn_line = "\n⏸ Ждём игроков… Пусть друг войдёт по коду."
        else:
            turn_line = "\n⏸ Хост: задай жизни и слово."

    return header + word_line + guessed_line + move_line + turn_line


# ===================== IMAGE (PIL) =====================
def _draw_hangman_png(stage: int) -> bytes:
    W, H = 700, 420
    img = Image.new("RGB", (W, H), "white")
    d = ImageDraw.Draw(img)

    d.line((80, 380, 320, 380), fill="black", width=6)
    d.line((140, 380, 140, 70), fill="black", width=8)
    d.line((140, 70, 360, 70), fill="black", width=8)
    d.line((360, 70, 360, 110), fill="black", width=6)

    if stage >= 1:
        d.ellipse((330, 110, 390, 170), outline="black", width=6)
    if stage >= 2:
        d.line((360, 170, 360, 260), fill="black", width=6)
    if stage >= 3:
        d.line((360, 200, 310, 235), fill="black", width=6)
    if stage >= 4:
        d.line((360, 200, 410, 235), fill="black", width=6)
    if stage >= 5:
        d.line((360, 260, 320, 330), fill="black", width=6)
    if stage >= 6:
        d.line((360, 260, 400, 330), fill="black", width=6)

    import io
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def hangman_image(room: Room) -> bytes:
    key = (room.max_fails, room.fails)
    if key in room.img_cache:
        return room.img_cache[key]

    if room.max_fails <= 0:
        stage = 6
    else:
        stage = int(round((room.fails / room.max_fails) * 6))
        stage = max(0, min(6, stage))

    data = _draw_hangman_png(stage)
    room.img_cache[key] = data
    return data


# ===================== STATUS MESSAGE (ONE, BUT CAN "BUMP") =====================
async def upsert_status(room: Room, uid: int, bump: bool = False):
    global BOT
    if not BOT:
        return

    caption = game_status_text(room)
    kb = ui_for(uid)

    ids = room.status_msg_ids.get(uid, [])

    png = hangman_image(room)
    file = BufferedInputFile(png, filename="hangman.png")

    # ВАЖНО:
    # - при ходах bump=False: редактируем ПОСЛЕДНЕЕ сообщение статуса
    # - после комментария bump=True: хотим “поднять вниз”, но гарантировать 1 статус:
    #   удаляем все старые статусы и отправляем новый

    if bump:
        # удалить все старые статусы
        for mid in ids:
            try:
                await BOT.delete_message(chat_id=uid, message_id=mid)
            except Exception:
                pass
        room.status_msg_ids[uid] = []

        # отправить новый (единственный)
        try:
            msg = await BOT.send_photo(chat_id=uid, photo=file, caption=caption, reply_markup=kb)
            room.status_msg_ids[uid] = [msg.message_id]
        except Exception:
            pass
        return

    # bump=False: пытаемся редактировать последний
    if ids:
        mid = ids[-1]
        try:
            media = InputMediaPhoto(media=file, caption=caption)
            await BOT.edit_message_media(chat_id=uid, message_id=mid, media=media, reply_markup=kb)
            return
        except Exception:
            # если не получилось — пробуем создать новый и заменить список на [new]
            room.status_msg_ids[uid] = []

    try:
        msg = await BOT.send_photo(chat_id=uid, photo=file, caption=caption, reply_markup=kb)
        room.status_msg_ids[uid] = [msg.message_id]
    except Exception:
        pass


async def refresh_room(room: Room, bump: bool = False):
    for uid in list(room.players):
        await upsert_status(room, uid, bump=bump)


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
    room.img_cache.clear()


async def start_game(room: Room):
    global BOT
    if not BOT:
        return

    if not room.secret:
        await BOT.send_message(room.host_id, "Сначала задай слово: 🪓 Новое слово", reply_markup=kb_host_room())
        return

    if len(room.order) < 1:
        await BOT.send_message(room.host_id, "Нужен хотя бы 1 отгадывающий (кроме хоста).", reply_markup=kb_host_room())
        await refresh_room(room)
        return

    reset_game(room)
    room.last_move = "Игра запущена 🚀"
    await refresh_room(room)


async def broadcast_chat(room: Room, text: str):
    global BOT
    if not BOT:
        return
    for uid in list(room.players):
        try:
            await BOT.send_message(uid, text, reply_markup=ui_for(uid))
        except Exception:
            pass
    # после чата "поднимаем" статус вниз, чтобы он был последним
    await refresh_room(room, bump=True)


async def finish(room: Room, text: str):
    room.started = False
    room.last_move = text
    await refresh_room(room)


# ===================== COMMANDS / BUTTONS =====================
@dp.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    uid = message.from_user.id
    await message.answer(
        f"Привет, {tg_name(message)}! 🎮\nУправление — кнопками снизу.",
        reply_markup=ui_for(uid),
    )


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
    room.tags[uid] = tg_tag(message)

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


@dp.message(JoinFlow.waiting_code, F.text)
async def join_wait_code(message: Message, state: FSMContext):
    code = (message.text or "").strip().upper()
    await state.clear()
    await join_by_code(message, code)


async def join_by_code(message: Message, code: str):
    uid = message.from_user.id

    if get_room_by_user(uid):
        await message.answer("Ты уже в комнате.", reply_markup=ui_for(uid))
        return

    room = rooms_by_code.get(code)
    if not room:
        await message.answer("Комната не найдена. Проверь код.", reply_markup=kb_main())
        return

    room.players.add(uid)
    room.names[uid] = tg_name(message)
    room.tags[uid] = tg_tag(message)
    user_room[uid] = code

    if uid != room.host_id and uid not in room.order:
        room.order.append(uid)

    room.last_move = f"{room.tags[uid]} вошёл(ла)"
    await refresh_room(room)
    await message.answer(f"✅ Ты вошёл(ла) в комнату {code}.", reply_markup=ui_for(uid))


@dp.message(Command("leave"))
async def cmd_leave(message: Message, state: FSMContext):
    await state.clear()
    uid = message.from_user.id
    room = get_room_by_user(uid)

    if not room:
        await message.answer("Ты не в комнате.", reply_markup=kb_main())
        return

    # remove
    user_room.pop(uid, None)
    room.players.discard(uid)
    room.status_msg_id.pop(uid, None)

    if uid in room.order:
        was_turn = (room.order and current_turn_user(room) == uid)
        room.order.remove(uid)
        if room.order:
            room.turn_idx = room.turn_idx % len(room.order)
            if was_turn:
                room.last_move = f"{room.tags.get(uid,'Игрок')} вышел(ла), ход передан дальше"
        else:
            room.turn_idx = 0

    if uid == room.host_id:
        await close_room(room)
        return

    room.last_move = f"{room.tags.get(uid,'Игрок')} вышел(ла)"
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


# ===================== SETUP (lives -> word) =====================
@dp.message(HostSetup.waiting_lives, F.text)
async def host_setup_lives(message: Message, state: FSMContext):
    txt = (message.text or "").strip()

    if txt in (BTN_CLOSE, BTN_LEAVE, BTN_START):
        if txt == BTN_CLOSE:
            await state.clear()
            await cmd_close(message)
        elif txt == BTN_LEAVE:
            await cmd_leave(message, state)
        else:
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
    room.img_cache.clear()
    room.last_move = f"Хост установил жизни: {n} ❤️"
    await refresh_room(room)

    await state.set_state(HostSetup.waiting_word)
    await message.answer("✅ Жизни установлены.\n\nШаг 2/2: введи слово (русские буквы):", reply_markup=kb_host_room())


@dp.message(HostSetup.waiting_word, F.text)
async def host_setup_word(message: Message, state: FSMContext):
    txt = (message.text or "").strip()

    if txt in (BTN_CLOSE, BTN_LEAVE, BTN_START):
        if txt == BTN_CLOSE:
            await state.clear()
            await cmd_close(message)
        elif txt == BTN_LEAVE:
            await cmd_leave(message, state)
        else:
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
        await message.answer("Слово не подходит. Введи другое слово:")
        return

    room.secret = w
    room.started = False
    room.guessed = set()
    room.fails = 0
    room.turn_idx = 0
    room.img_cache.clear()
    room.last_move = "Хост задал слово 🪓"

    await state.clear()
    await refresh_room(room)

    if len(room.order) >= 1:
        await start_game(room)


# ===================== KICK PLAYER =====================
@dp.message(F.text == BTN_KICK)
async def ui_kick(message: Message, state: FSMContext):
    uid = message.from_user.id
    room = get_room_by_user(uid)
    if not room or room.host_id != uid:
        await message.answer("Только хост может удалять игроков.", reply_markup=ui_for(uid))
        return

    if not room.order:
        await message.answer("Некого удалять (нет отгадывающих).", reply_markup=kb_host_room())
        return

    lines = ["Кого удалить? Ответь цифрой (или ❌ Отмена):\n"]
    for i, pid in enumerate(room.order, start=1):
        lines.append(f"{i}) {room.tags.get(pid, 'Игрок')}")
    await state.set_state(KickFlow.waiting_index)
    await message.answer("\n".join(lines), reply_markup=kb_cancel_only())


@dp.message(KickFlow.waiting_index, F.text)
async def kick_wait(message: Message, state: FSMContext):
    uid = message.from_user.id
    room = get_room_by_user(uid)
    txt = (message.text or "").strip()

    if txt == BTN_CANCEL:
        await state.clear()
        await message.answer("Ок, отменено.", reply_markup=ui_for(uid))
        return

    if not room or room.host_id != uid:
        await state.clear()
        await message.answer("Не могу: ты не хост или комнаты нет.", reply_markup=kb_main())
        return

    if not txt.isdigit():
        await message.answer("Нужно число из списка (или ❌ Отмена).", reply_markup=kb_cancel_only())
        return

    idx = int(txt)
    if idx < 1 or idx > len(room.order):
        await message.answer("Нет такого номера. Попробуй ещё раз.", reply_markup=kb_cancel_only())
        return

    kicked_id = room.order[idx - 1]
    was_turn = (room.started and current_turn_user(room) == kicked_id)

    room.players.discard(kicked_id)
    user_room.pop(kicked_id, None)
    room.status_msg_id.pop(kicked_id, None)

    room.order.remove(kicked_id)
    if room.order:
        room.turn_idx = room.turn_idx % len(room.order)
    else:
        room.turn_idx = 0

    await state.clear()

    try:
        await BOT.send_message(kicked_id, "👢 Тебя удалили из комнаты.", reply_markup=kb_main())
    except Exception:
        pass

    room.last_move = f"Хост удалил {room.tags.get(kicked_id,'Игрок')} 👢"
    if was_turn and room.order:
        room.last_move += " (ход перешёл дальше)"
    if room.started and not room.order:
        room.started = False
        room.last_move += " — игра остановлена (нет отгадывающих)."

    await refresh_room(room)
    await message.answer("Готово ✅", reply_markup=ui_for(uid))


# ===================== TRANSFER HOST =====================
@dp.message(F.text == BTN_TRANSFER)
async def ui_transfer(message: Message, state: FSMContext):
    uid = message.from_user.id
    room = get_room_by_user(uid)
    if not room or room.host_id != uid:
        await message.answer("Только хост может передавать хоста.", reply_markup=ui_for(uid))
        return

    candidates = [p for p in room.players if p != room.host_id]
    if not candidates:
        await message.answer("Некому передать хоста (ты один/одна в комнате).", reply_markup=kb_host_room())
        return

    # показываем только не-хоста (включая игроков, которые угадывают)
    lines = ["Кому передать хоста? Ответь цифрой (или ❌ Отмена):\n"]
    for i, pid in enumerate(candidates, start=1):
        lines.append(f"{i}) {room.tags.get(pid, 'Игрок')}")
    await state.set_state(TransferFlow.waiting_index)
    await state.update_data(candidates=candidates)
    await message.answer("\n".join(lines), reply_markup=kb_cancel_only())


@dp.message(TransferFlow.waiting_index, F.text)
async def transfer_wait(message: Message, state: FSMContext):
    uid = message.from_user.id
    room = get_room_by_user(uid)
    txt = (message.text or "").strip()

    if txt == BTN_CANCEL:
        await state.clear()
        await message.answer("Ок, отменено.", reply_markup=ui_for(uid))
        return

    if not room or room.host_id != uid:
        await state.clear()
        await message.answer("Не могу: ты не хост или комнаты нет.", reply_markup=kb_main())
        return

    data = await state.get_data()
    candidates = data.get("candidates", [])
    if not txt.isdigit():
        await message.answer("Нужно число из списка (или ❌ Отмена).", reply_markup=kb_cancel_only())
        return

    idx = int(txt)
    if idx < 1 or idx > len(candidates):
        await message.answer("Нет такого номера. Попробуй ещё раз.", reply_markup=kb_cancel_only())
        return

    new_host = candidates[idx - 1]
    old_host = room.host_id

    # сохраняем текущего ходящего
    old_turn = current_turn_user(room)

    # хост не должен угадывать: убираем new_host из очереди
    if new_host in room.order:
        room.order.remove(new_host)

    # бывший хост становится обычным игроком-угадывающим (по умолчанию)
    if old_host != new_host and old_host in room.players and old_host not in room.order:
        room.order.append(old_host)

    room.host_id = new_host

    # поправим turn_idx, чтобы игра не ломалась
    if room.order:
        if old_turn in room.order:
            room.turn_idx = room.order.index(old_turn)
        else:
            room.turn_idx = room.turn_idx % len(room.order)
    else:
        room.turn_idx = 0
        if room.started:
            room.started = False
            room.last_move = "Хост сменился — игра остановлена (нет отгадывающих)."

    await state.clear()

    room.last_move = f"👑 Хост теперь {room.tags.get(new_host,'Игрок')}"
    await refresh_room(room)
    await broadcast_chat(room, f"👑 Хост передан: {room.tags.get(new_host,'Игрок')}")

    await message.answer("Готово ✅", reply_markup=ui_for(new_host))


# ===================== COMMENTS =====================
@dp.message(F.text == BTN_COMMENT)
async def ui_comment(message: Message, state: FSMContext):
    uid = message.from_user.id
    room = get_room_by_user(uid)
    if not room:
        await message.answer("Ты не в комнате.", reply_markup=kb_main())
        return
    await state.set_state(CommentFlow.waiting_text)
    await message.answer("Напиши комментарий (или ❌ Отмена):", reply_markup=kb_cancel_only())


@dp.message(CommentFlow.waiting_text, F.text)
async def comment_wait(message: Message, state: FSMContext):
    uid = message.from_user.id
    room = get_room_by_user(uid)
    txt = (message.text or "").strip()

    if txt == BTN_CANCEL:
        await state.clear()
        await message.answer("Ок, отменено.", reply_markup=ui_for(uid))
        return

    if not room:
        await state.clear()
        await message.answer("Комнаты уже нет.", reply_markup=kb_main())
        return

    room.names[uid] = tg_name(message)
    room.tags[uid] = tg_tag(message)

    prefix = f"💬 {room.tags.get(uid,'Игрок')}: "
    if uid == room.host_id:
        prefix = f"💬 ХОСТ {room.tags.get(uid,'Игрок')}: "

    await state.clear()
    await broadcast_chat(room, prefix + txt)


# ===================== BUTTON ROUTES =====================
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
    if await state.get_state() is not None:
        return

    uid = message.from_user.id
    room = get_room_by_user(uid)
    if not room:
        return

    room.names[uid] = tg_name(message)
    room.tags[uid] = tg_tag(message)

    if not room.started or not room.order:
        await refresh_room(room)
        return

    if uid == room.host_id:
        await refresh_room(room)
        return

    turn_uid = current_turn_user(room)
    if uid != turn_uid:
        await refresh_room(room)
        return

    txt = (message.text or "").strip().lower()
    if not txt:
        return

    if len(txt) == 1:
        ch = txt
        if ch not in ALLOWED:
            room.last_move = f"{room.tags.get(uid,'Игрок')} не-русская буква ❌"
            await refresh_room(room)
            return
        if ch in room.guessed:
            room.last_move = f"{room.tags.get(uid,'Игрок')} повторил(а): {ch}"
            await refresh_room(room)
            return

        room.guessed.add(ch)
        ok = ch in room.secret
        if not ok:
            room.fails += 1
        room.last_move = f"{room.tags.get(uid,'Игрок')}: {ch} ({'✅ есть' if ok else '❌ нет'})"

    else:
        guess = normalize_word(txt)
        if len(guess) < 2:
            room.last_move = f"{room.tags.get(uid,'Игрок')} некорректное слово ❌"
            await refresh_room(room)
            return

        if guess == room.secret:
            room.guessed.update(set(room.secret))
            room.last_move = f"{room.tags.get(uid,'Игрок')} угадал(а) слово ✅"
        else:
            room.fails += 1
            room.last_move = f"{room.tags.get(uid,'Игрок')} попытка словом ❌"

    await refresh_room(room)

    win = room.secret and all(ch in room.guessed for ch in room.secret)
    lose = room.fails >= room.max_fails

    if win:
        await finish(room, "🎉 Победа!")
        await broadcast_chat(room, f"🎉 Победа! Слово: {room.secret}")
        return

    if lose:
        await finish(room, "💀 Поражение!")
        await broadcast_chat(room, f"💀 Поражение. Слово было: {room.secret}")
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