import asyncio
import logging
import os
import sqlite3

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

# ====== НАСТРОЙКИ ======
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
ADMIN_GROUP_ID = -5222436967          # ID группы админов
CHANNEL_ID = -1004452482109              # ID канала для публикации
SUPER_ADMIN_IDS = [8667806683]            # ID тех, кто может делать рассылку
# =======================

logging.basicConfig(level=logging.INFO)

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
dp = Dispatcher()

# ---------- База данных пользователей (для рассылки) ----------
conn = sqlite3.connect("/data/users.db")
cursor = conn.cursor()
cursor.execute("CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY)")
conn.commit()


def add_user(user_id: int):
    cursor.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (user_id,))
    conn.commit()


def get_all_users() -> list[int]:
    cursor.execute("SELECT user_id FROM users")
    return [row[0] for row in cursor.fetchall()]


# ---------- Хранилище предложенных постов ----------
pending_posts: dict[int, dict] = {}


class Broadcast(StatesGroup):
    waiting_message = State()


# ---------- Старт в личке ----------
@dp.message(CommandStart())
async def start_cmd(message: Message):
    if message.chat.type != "private":
        return
    add_user(message.from_user.id)
    await message.answer(
        "👋 Привет!\n\n"
        "Отправь мне <b>текст</b> или <b>фото с подписью</b>, и я передам "
        "твой пост на модерацию администрации канала."
    )


# ---------- Приём постов от пользователей ----------
@dp.message(F.chat.type == "private", F.text | F.photo)
async def handle_user_post(message: Message, state: FSMContext):
    # не обрабатываем, если пользователь в FSM рассылки (не актуально для private, но на всякий)
    user = message.from_user
    username = f"@{user.username}" if user.username else "нет"

    text = message.text or message.caption
    photo_id = message.photo[-1].file_id if message.photo else None

    if not text and not photo_id:
        await message.answer("Пожалуйста, отправь текст или фото.")
        return

    admin_text = (
        f"📩 <b>Новое предложение поста</b>\n\n"
        f"👤 Автор: {user.full_name}\n"
        f"🔗 Юзер: {username}\n"
        f"🆔 ID: <code>{user.id}</code>\n\n"
    )
    if text:
        admin_text += f"📝 <b>Текст:</b>\n{text}"

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Принять",
                    callback_data="accept",
                    style="success",
                ),
                InlineKeyboardButton(
                    text="❌ Отклонить",
                    callback_data="reject",
                    style="danger",
                ),
            ]
        ]
    )

    if photo_id:
        sent = await bot.send_photo(
            chat_id=ADMIN_GROUP_ID,
            photo=photo_id,
            caption=admin_text,
            reply_markup=kb,
        )
    else:
        sent = await bot.send_message(
            chat_id=ADMIN_GROUP_ID,
            text=admin_text,
            reply_markup=kb,
        )

    pending_posts[sent.message_id] = {
        "user_id": user.id,
        "text": text,
        "photo_id": photo_id,
    }

    await message.answer("✅ Твой пост отправлен на модерацию. Ожидай ответа!")


@dp.message(F.chat.type == "private")
async def handle_unsupported(message: Message):
    await message.answer("Пожалуйста, отправь текст или фото с подписью.")


# ---------- Модерация ----------
@dp.callback_query(F.data.in_({"accept", "reject"}))
async def handle_moderation(call: CallbackQuery):
    data = pending_posts.pop(call.message.message_id, None)

    if data is None:
        await call.answer("Этот пост уже обработан.", show_alert=True)
        return

    user_id = data["user_id"]
    text = data["text"]
    photo_id = data["photo_id"]

    if call.data == "accept":
        try:
            if photo_id:
                await bot.send_photo(
                    chat_id=CHANNEL_ID,
                    photo=photo_id,
                    caption=text,
                )
            else:
                await bot.send_message(CHANNEL_ID, text)
            await bot.send_message(user_id, "✅ Твой пост одобрен и опубликован!")
            await call.answer("Опубликовано")
        except Exception as e:
            logging.exception(e)
            await call.answer("Ошибка при публикации", show_alert=True)
            await bot.send_message(user_id, "⚠️ Произошла ошибка при публикации.")
    else:
        try:
            await bot.send_message(user_id, "❌ Твой пост отклонён администрацией.")
            await call.answer("Отклонено")
        except Exception as e:
            logging.exception(e)
            await call.answer("Отклонено (не удалось уведомить автора)", show_alert=True)

    try:
        status = "✅ Принято" if call.data == "accept" else "❌ Отклонено"
        if call.message.photo:
            new_caption = (call.message.caption or "") + f"\n\n<b>{status}</b>"
            await call.message.edit_caption(caption=new_caption, reply_markup=None)
        else:
            await call.message.edit_text(
                text=call.message.html_text + f"\n\n<b>{status}</b>",
                reply_markup=None,
            )
    except Exception:
        pass


# ---------- Массовая рассылка в группе ----------
@dp.message(F.chat.id == ADMIN_GROUP_ID, Command("broadcast"))
async def broadcast_start(message: Message, state: FSMContext):
    if message.from_user.id not in SUPER_ADMIN_IDS:
        await message.reply("⛔ У тебя нет прав для рассылки.")
        return

    # Вариант 1: /broadcast в ответ на сообщение — сразу берём его как контент
    if message.reply_to_message:
        await do_broadcast(message, message.reply_to_message)
        return

    # Вариант 2: ждём следующее сообщение от этого админа
    await state.set_state(Broadcast.waiting_message)
    await message.reply(
        "📢 Отправь <b>следующим сообщением</b> контент для рассылки "
        "(текст, фото или фото с подписью).\n\n"
        "Либо используй <code>/broadcast</code> в ответ на готовое сообщение.\n"
        "Отмена: /cancel"
    )


@dp.message(F.chat.id == ADMIN_GROUP_ID, Command("cancel"))
async def broadcast_cancel(message: Message, state: FSMContext):
    if await state.get_state() is None:
        return
    await state.clear()
    await message.reply("❌ Рассылка отменена.")


@dp.message(Broadcast.waiting_message, F.chat.id == ADMIN_GROUP_ID)
async def broadcast_from_group(message: Message, state: FSMContext):
    if message.from_user.id not in SUPER_ADMIN_IDS:
        await state.clear()
        return

    await state.clear()
    await do_broadcast(message, message)


async def do_broadcast(trigger_message: Message, content_message: Message):
    """trigger_message — куда отвечать, content_message — что рассылать."""
    text = content_message.text or content_message.caption
    photo_id = (
        content_message.photo[-1].file_id if content_message.photo else None
    )

    if not text and not photo_id:
        await trigger_message.reply("⚠️ Нечего рассылать — нет текста или фото.")
        return

    users = get_all_users()
    if not users:
        await trigger_message.reply("Пока нет пользователей для рассылки.")
        return

    status_msg = await trigger_message.reply(
        f"🚀 Начинаю рассылку на {len(users)} пользователей..."
    )

    sent = 0
    failed = 0
    for user_id in users:
        try:
            if photo_id:
                await bot.send_photo(user_id, photo=photo_id, caption=text)
            else:
                await bot.send_message(user_id, text)
            sent += 1
        except Exception:
            failed += 1
        await asyncio.sleep(0.05)  # защита от flood-limit

    try:
        await status_msg.edit_text(
            f"✅ Рассылка завершена.\n"
            f"Доставлено: <b>{sent}</b>\n"
            f"Не доставлено: <b>{failed}</b>"
        )
    except Exception:
        await trigger_message.reply(
            f"✅ Рассылка завершена. Доставлено: {sent}, не доставлено: {failed}"
        )


# ---------- Запуск ----------
async def main():
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())