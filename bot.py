import asyncio
import logging
import os
from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
)
from aiogram.filters import CommandStart

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)

BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_ID = int(os.environ["ADMIN_ID"])
CHANNEL_ID = os.environ["CHANNEL_ID"]

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# Накопленные медиагруппы: media_group_id -> [Message, ...]
pending_groups: dict[str, list[Message]] = {}
group_tasks: dict[str, asyncio.Task] = {}

# Данные для публикации: key_id -> {"user_id": int, "message_ids": list[int]}
# key_id = message_id первого пересланного сообщения в чате с админом
pending_submissions: dict[int, dict] = {}


def make_keyboard(key_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Опубликовать", callback_data=f"pub:{key_id}"),
        InlineKeyboardButton(text="Пропустить",   callback_data=f"skip:{key_id}"),
    ]])


async def send_to_admin(messages: list[Message]) -> None:
    first = messages[0]

    # copy вместо forward — имя отправителя нигде не показывается
    if len(messages) == 1:
        copied = await bot.copy_message(
            chat_id=ADMIN_ID,
            from_chat_id=first.from_user.id,
            message_id=first.message_id,
        )
        key_id = copied.message_id
        label = "Новая заявка"
    else:
        results = await bot.copy_messages(
            chat_id=ADMIN_ID,
            from_chat_id=first.from_user.id,
            message_ids=[m.message_id for m in messages],
        )
        key_id = results[0].message_id
        label = f"Новая заявка | альбом {len(messages)} шт."

    await bot.send_message(ADMIN_ID, label, reply_markup=make_keyboard(key_id))

    pending_submissions[key_id] = {
        "user_id": first.from_user.id,
        "message_ids": [m.message_id for m in messages],
    }


async def flush_group(mgid: str) -> None:
    await asyncio.sleep(1.0)
    messages = pending_groups.pop(mgid, [])
    group_tasks.pop(mgid, None)
    if messages:
        await send_to_admin(messages)


@dp.message(CommandStart())
async def cmd_start(message: Message) -> None:
    if message.from_user.id == ADMIN_ID:
        await message.answer("Режим админа. Используй кнопки под пересланными сообщениями.")
    else:
        await message.answer(
            "Напиши что-нибудь — это может попасть в канал.\n"
            "Поддерживается текст, фото, видео, стикеры, гифки, кружки и всё остальное."
        )


@dp.message(F.chat.type == "private", ~F.from_user.id.in_({ADMIN_ID}))
async def handle_submission(message: Message) -> None:
    if message.media_group_id:
        mgid = message.media_group_id
        pending_groups.setdefault(mgid, []).append(message)
        if mgid in group_tasks:
            group_tasks[mgid].cancel()
        group_tasks[mgid] = asyncio.create_task(flush_group(mgid))
    else:
        await send_to_admin([message])


@dp.callback_query(F.data.startswith("pub:"))
async def cb_publish(callback: CallbackQuery) -> None:
    key_id = int(callback.data.split(":")[1])
    sub = pending_submissions.get(key_id)

    if not sub:
        await callback.answer("Данные не найдены — бот перезапускался?", show_alert=True)
        return

    user_id = sub["user_id"]
    message_ids = sub["message_ids"]

    try:
        if len(message_ids) == 1:
            await bot.copy_message(
                chat_id=CHANNEL_ID,
                from_chat_id=user_id,
                message_id=message_ids[0],
            )
        else:
            # copy_messages — Bot API 6.5+, копирует альбом целиком
            await bot.copy_messages(
                chat_id=CHANNEL_ID,
                from_chat_id=user_id,
                message_ids=message_ids,
            )
    except Exception as e:
        logging.error(f"Publish error: {e}")
        await callback.answer(f"Ошибка: {e}", show_alert=True)
        return

    pending_submissions.pop(key_id, None)
    await callback.message.edit_text(callback.message.text + "\n\nОпубликовано.", reply_markup=None)
    await callback.answer()


@dp.callback_query(F.data.startswith("skip:"))
async def cb_skip(callback: CallbackQuery) -> None:
    key_id = int(callback.data.split(":")[1])
    pending_submissions.pop(key_id, None)
    await callback.message.edit_text(callback.message.text + "\n\nПропущено.", reply_markup=None)
    await callback.answer()


# Health check — пингуется через cron-job чтобы Render free tier не засыпал

async def health(request: web.Request) -> web.Response:
    return web.Response(text="ok")


async def run_web() -> None:
    app = web.Application()
    app.router.add_get("/health", health)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 8080))
    await web.TCPSite(runner, "0.0.0.0", port).start()
    logging.info(f"Health check on :{port}/health")


async def main() -> None:
    await run_web()
    logging.info("Bot started")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())