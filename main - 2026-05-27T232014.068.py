import os
import logging
import asyncio
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes
)
from telegram.error import TelegramError
from apscheduler.schedulers.asyncio import AsyncIOScheduler
import db
from video_fetcher import get_unseen_video, send_video_to_user
from telegram import ChatMemberUpdated
from telegram.ext import ChatMemberHandler
import qrcode
from io import BytesIO
import aiohttp
from aiohttp import web

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ── Secrets — set these two in Render environment variables ───────
BOT_TOKEN    = os.environ.get("BOT_TOKEN")      # Required
# MONGODB_URI is read directly in db.py          # Required

# ── Bot config (hard-coded — edit here to change settings) ────────
ADMIN_ID            = 6812561508
SOURCE_CHANNEL_ID   = -1005208194854
GROUP_INVITE_LINK   = "https://t.me/+rG0Py10eivRhYjJl"
USDT_ADDRESS        = "TXKKW8NCrC1JCmVC3dCaVnDSMXkHGt5x87"
FREE_VIDEOS_PER_DAY = 3
REFERRAL_VIDEOS     = 3
SUB_VIDEO_LIMIT     = 20
SUB_DAYS            = 7
SUB_PRICE           = "1"

# ── Render deployment (auto-detected — do not change) ─────────────
PORT                = int(os.environ.get("PORT", 10000))
RENDER_EXTERNAL_URL = os.environ.get("RENDER_EXTERNAL_URL", "")


# ─────────────────────────── KEYBOARDS ───────────────────────────
def main_menu_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎬 New Video 📸", callback_data="new_video")],
        [InlineKeyboardButton("👥 Referral", callback_data="referral"),
         InlineKeyboardButton("💳 Pay", callback_data="pay")],
        [InlineKeyboardButton("📊 My Stats", callback_data="my_stats"),
         InlineKeyboardButton("🆘 Support", callback_data="support")]
    ])


def admin_menu_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Bot Statistics",       callback_data="admin_stats")],
        [InlineKeyboardButton("✅ Pending Payments",     callback_data="admin_pending_payments")],
        [InlineKeyboardButton("👥 Recent Users",         callback_data="admin_users")],
        [InlineKeyboardButton("📹 Add Videos",           callback_data="admin_add_videos")],
        [InlineKeyboardButton("📢 Broadcast",            callback_data="admin_broadcast")],
    ])


# ─── Universal "go back to main menu" helper ─────────────────────
# Works whether the current message is a text message OR a photo/caption message.
async def go_to_main_menu(query, text="🏠 <b>Main Menu</b>\n\nChoose an option below:"):
    try:
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=main_menu_keyboard())
    except TelegramError:
        try:
            await query.edit_message_caption(caption=text, parse_mode="HTML", reply_markup=main_menu_keyboard())
        except TelegramError:
            await query.message.reply_text(text, parse_mode="HTML", reply_markup=main_menu_keyboard())


# ─── Universal safe edit (text or caption) ───────────────────────
async def safe_edit(query, text, keyboard=None, parse_mode="HTML"):
    markup = keyboard if keyboard else main_menu_keyboard()
    try:
        await query.edit_message_text(text, parse_mode=parse_mode, reply_markup=markup)
    except TelegramError:
        try:
            await query.edit_message_caption(caption=text, parse_mode=parse_mode, reply_markup=markup)
        except TelegramError:
            await query.message.reply_text(text, parse_mode=parse_mode, reply_markup=markup)


# ─────────────────────────── /start ───────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    args = context.args

    referrer_id = None
    if args and args[0].startswith("ref_"):
        try:
            referrer_id = int(args[0].replace("ref_", ""))
        except Exception:
            pass

    existing = db.get_user(user.id)
    if not existing:
        db.create_user(user.id, user.username or user.first_name, referrer_id)
        if referrer_id and referrer_id != user.id:
            db.add_referral_credits(referrer_id, REFERRAL_VIDEOS)
            try:
                await context.bot.send_message(
                    chat_id=referrer_id,
                    text=(
                        f"🎉 <b>New Referral!</b>\n\n"
                        f"@{user.username or user.first_name} joined using your link!\n"
                        f"You earned <b>{REFERRAL_VIDEOS} video credits</b> 🎬"
                    ),
                    parse_mode="HTML"
                )
            except Exception:
                pass

    await update.message.reply_text(
        f"👋 <b>Welcome, {user.first_name}!</b>\n\n"
        f"🎬 <b>Video Bot</b> - Premium Video Experience\n\n"
        f"📌 Free Users: <b>{FREE_VIDEOS_PER_DAY} videos/day</b>\n"
        f"👑 Subscribers: <b>Up to {SUB_VIDEO_LIMIT} videos/day</b>\n\n"
        f"Tap <b>New Video 📸</b> to get started!",
        parse_mode="HTML",
        reply_markup=main_menu_keyboard()
    )


# ─────────────────────────── /id ───────────────────────────
async def get_chat_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user
    await update.message.reply_text(
        f"💬 <b>Chat Info</b>\n\n"
        f"Chat ID: <code>{chat.id}</code>\n"
        f"Chat Type: {chat.type}\n"
        f"Your User ID: <code>{user.id}</code>",
        parse_mode="HTML"
    )


# ─────────────────────────── /setsource ───────────────────────────
async def set_source(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin types /setsource inside the source group to register it."""
    global SOURCE_CHANNEL_ID
    user = update.effective_user
    chat = update.effective_chat

    if user.id != ADMIN_ID:
        return

    if chat.type not in ("group", "supergroup", "channel"):
        await update.message.reply_text(
            "❌ This command must be used inside the source group/channel, not in a private chat."
        )
        return

    SOURCE_CHANNEL_ID = chat.id
    db.save_config("source_channel_id", str(chat.id))

    import video_fetcher as vf
    vf.SOURCE_CHANNEL_ID = chat.id

    await update.message.reply_text(
        f"✅ Source group set!\nChat ID: <code>{chat.id}</code>\n\n"
        f"Now go to this group, select videos, and forward them to me (bot DM) to add them to the library.",
        parse_mode="HTML"
    )

    # Notify admin in DM with instructions
    try:
        await context.bot.send_message(
            chat_id=ADMIN_ID,
            text=(
                f"✅ <b>Source group configured!</b>\n\n"
                f"Group: <b>{chat.title}</b>\n"
                f"Chat ID: <code>{chat.id}</code>\n\n"
                f"📹 <b>To add videos to the library:</b>\n"
                f"Go to the source group → select a video → forward it here to me.\n"
                f"I'll add it automatically. No scanning needed!"
            ),
            parse_mode="HTML"
        )
    except Exception:
        pass


async def send_add_videos_instructions(bot):
    """Send admin instructions on how to add videos to the library."""
    total = db.get_cached_video_count()
    try:
        await bot.send_message(
            chat_id=ADMIN_ID,
            text=(
                f"📹 <b>How to Add Videos to Library</b>\n\n"
                f"Currently cached: <b>{total} video(s)</b>\n\n"
                f"<b>Steps:</b>\n"
                f"1. Go to your source group\n"
                f"2. Select any video\n"
                f"3. Forward it to this bot (me) in private chat\n"
                f"4. Bot will automatically add it to the library ✅\n\n"
                f"You can forward multiple videos one by one!"
            ),
            parse_mode="HTML"
        )
    except Exception:
        pass


# ─────────────────────────── /admin ───────────────────────────
async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    await update.message.reply_text(
        "🔧 <b>Admin Panel</b>",
        parse_mode="HTML",
        reply_markup=admin_menu_keyboard()
    )


# ─────────────────────────── CALLBACK ROUTER ───────────────────────────
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = query.from_user
    data = query.data

    if data == "new_video":
        await handle_new_video(query, context, user)

    elif data == "referral":
        await handle_referral(query, context, user)

    elif data == "pay":
        await handle_pay(query, context, user)

    elif data == "pay_usdt":
        await handle_pay_usdt(query, context, user)

    elif data.startswith("confirm_payment_"):
        amount = data.replace("confirm_payment_", "")
        await handle_confirm_payment(query, context, user, amount)

    elif data == "my_stats":
        await handle_my_stats(query, context, user)

    elif data == "support":
        await handle_support_start(query, context, user)

    elif data == "support_confirm":
        await handle_support_confirm(query, context, user)

    elif data == "support_cancel":
        context.user_data.pop("support_message", None)
        context.user_data.pop("support_step", None)
        await go_to_main_menu(query, "❌ Support request cancelled.")

    elif data == "back_main":
        await go_to_main_menu(query)

    elif data == "admin_panel" and user.id == ADMIN_ID:
        await safe_edit(query, "🔧 <b>Admin Panel</b>", admin_menu_keyboard())

    elif data == "admin_stats" and user.id == ADMIN_ID:
        await handle_admin_stats(query, context)

    elif data == "admin_pending_payments" and user.id == ADMIN_ID:
        await handle_admin_pending_payments(query, context)

    elif data == "admin_users" and user.id == ADMIN_ID:
        await handle_admin_users(query, context)

    elif data == "admin_add_videos" and user.id == ADMIN_ID:
        await handle_admin_add_videos(query, context)

    elif data == "admin_broadcast" and user.id == ADMIN_ID:
        await handle_admin_broadcast_start(query, context)

    elif data == "broadcast_confirm" and user.id == ADMIN_ID:
        await handle_broadcast_confirm(query, context)

    elif data == "broadcast_cancel" and user.id == ADMIN_ID:
        context.user_data.pop("broadcast_step", None)
        context.user_data.pop("broadcast_text", None)
        context.user_data.pop("broadcast_photo_id", None)
        await safe_edit(query, "❌ Broadcast cancelled.", admin_menu_keyboard())

    elif data.startswith("approve_") and user.id == ADMIN_ID:
        await handle_approve_payment(query, context, data.replace("approve_", ""))

    elif data.startswith("reject_") and user.id == ADMIN_ID:
        await handle_reject_payment(query, context, data.replace("reject_", ""))


# ─────────────────────────── AUTO DELETE ───────────────────────────
DELETE_AFTER_SECONDS = 15 * 60  # 15 minutes

async def delete_video_job(context: ContextTypes.DEFAULT_TYPE):
    """
    Job queue callback — runs exactly 15 minutes after video is sent.
    Deletes the video message and the reminder message from user DM,
    then sends a notification with the main menu.
    """
    data        = context.job.data
    user_id     = data["user_id"]
    video_msg_id   = data["video_msg_id"]
    reminder_msg_id = data["reminder_msg_id"]

    try:
        await context.bot.delete_message(chat_id=user_id, message_id=video_msg_id)
    except Exception as e:
        logger.warning(f"Auto-delete video msg failed for user {user_id}: {e}")

    try:
        await context.bot.delete_message(chat_id=user_id, message_id=reminder_msg_id)
    except Exception as e:
        logger.warning(f"Auto-delete reminder msg failed for user {user_id}: {e}")

    try:
        await context.bot.send_message(
            chat_id=user_id,
            text=(
                "🗑 <b>Video Deleted</b>\n\n"
                "The video has been auto-deleted to protect copyright. 🔐\n\n"
                "💾 If you saved it to <b>Saved Messages</b>, you can still watch it anytime!\n\n"
                "Tap <b>New Video 📸</b> for another one 🎬"
            ),
            parse_mode="HTML",
            reply_markup=main_menu_keyboard()
        )
    except Exception as e:
        logger.warning(f"Auto-delete notification failed for user {user_id}: {e}")


# ─────────────────────────── NEW VIDEO ───────────────────────────
async def handle_new_video(query, context, user):
    user_data = db.get_user(user.id)
    if not user_data:
        db.create_user(user.id, user.username or user.first_name)
        user_data = db.get_user(user.id)

    now = datetime.utcnow()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

    has_sub = bool(
        user_data.get("subscription_expiry") and
        user_data["subscription_expiry"] > now
    )
    daily_limit     = SUB_VIDEO_LIMIT if has_sub else FREE_VIDEOS_PER_DAY
    videos_today    = db.count_videos_today(user.id, today_start)
    referral_credits = user_data.get("referral_credits", 0)

    # Limit reached and no referral credits left
    if videos_today >= daily_limit and referral_credits <= 0:
        limit_text = (
            f"⚠️ <b>Daily Limit Reached!</b>\n\n"
            f"You've used <b>{videos_today}/{daily_limit}</b> videos today.\n\n"
        )
        if has_sub:
            limit_text += "Come back tomorrow! 🌅"
            keyboard = [[InlineKeyboardButton("🏠 Back", callback_data="back_main")]]
        else:
            limit_text += "Get more videos by:"
            keyboard = [
                [InlineKeyboardButton("👥 Refer Friends (+3 videos)", callback_data="referral")],
                [InlineKeyboardButton("💳 Subscribe ($1 / 7 days)",  callback_data="pay")],
                [InlineKeyboardButton("🏠 Back",                     callback_data="back_main")]
            ]
        await safe_edit(query, limit_text, InlineKeyboardMarkup(keyboard))
        return

    use_credit = (videos_today >= daily_limit and referral_credits > 0)

    sent_file_ids = db.get_sent_video_file_ids(user.id)

    await safe_edit(query, "⏳ <b>Fetching your video...</b>")

    video_info = get_unseen_video(sent_file_ids)
    if not video_info:
        await safe_edit(
            query,
            "⏳ <b>No video ready right now.</b>\n\n"
            "All available videos have been watched or the library is empty.\n\n"
            "New videos are added automatically when posted in the source channel.\n"
            "Please try again later!",
            main_menu_keyboard()
        )
        return

    video_msg = await send_video_to_user(context.bot, user.id, video_info)
    if not video_msg:
        await safe_edit(
            query,
            "❌ <b>Could not send video.</b>\n\nPlease try again.",
            main_menu_keyboard()
        )
        return

    db.record_video_sent(user.id, video_info["file_id"])

    if use_credit:
        db.use_referral_credit(user.id)
        status_line = f"🎟 Referral credits left: <b>{referral_credits - 1}</b>"
    else:
        remaining   = daily_limit - videos_today - 1
        status_line = f"Videos left today: <b>{max(0, remaining)}</b>"

    sub_badge = "👑 Subscriber" if has_sub else "🆓 Free"

    # Send save reminder + countdown notice
    reminder_msg = await context.bot.send_message(
        chat_id=user.id,
        text=(
            f"🎬 <b>Enjoy your video!</b>\n\n"
            f"{sub_badge} | {status_line}\n\n"
            f"⚠️ <b>This video will be auto-deleted in 15 minutes</b> to protect copyright.\n\n"
            f"💾 <b>Save it now:</b> Tap & hold the video → <i>Forward</i> → <i>Saved Messages</i>\n"
            f"So you can watch it anytime later! 🔐"
        ),
        parse_mode="HTML",
        reply_markup=main_menu_keyboard()
    )

    # Schedule auto-delete of the video + reminder after 15 minutes via job_queue
    context.job_queue.run_once(
        delete_video_job,
        when=DELETE_AFTER_SECONDS,
        data={
            "user_id":        user.id,
            "video_msg_id":   video_msg.message_id,
            "reminder_msg_id": reminder_msg.message_id,
        },
        name=f"del_{user.id}_{video_msg.message_id}"
    )


# ─────────────────────────── REFERRAL ───────────────────────────
async def handle_referral(query, context, user):
    user_data = db.get_user(user.id)
    if not user_data:
        db.create_user(user.id, user.username or user.first_name)
        user_data = db.get_user(user.id)

    bot_username  = (await context.bot.get_me()).username
    referral_link = f"https://t.me/{bot_username}?start=ref_{user.id}"

    await safe_edit(
        query,
        f"👥 <b>Referral Program</b>\n\n"
        f"🔗 Your Link:\n<code>{referral_link}</code>\n\n"
        f"📊 <b>Your Stats:</b>\n"
        f"• Total Referrals: <b>{user_data.get('referral_count', 0)}</b>\n"
        f"• Video Credits Available: <b>{user_data.get('referral_credits', 0)}</b>\n\n"
        f"💡 <b>How it works:</b>\n"
        f"• Share your link with friends\n"
        f"• Each friend who joins = <b>{REFERRAL_VIDEOS} video credits</b>\n"
        f"• Credits let you watch extra videos beyond your daily limit!",
        InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Back", callback_data="back_main")]])
    )


# ─────────────────────────── PAY ───────────────────────────
async def handle_pay(query, context, user):
    await safe_edit(
        query,
        f"💳 <b>Get More Videos</b>\n\nChoose your payment option:",
        InlineKeyboardMarkup([
            [InlineKeyboardButton(f"👑 ${SUB_PRICE} for {SUB_DAYS} Days Subscription", callback_data="pay_usdt")],
            [InlineKeyboardButton("🏠 Back", callback_data="back_main")]
        ])
    )


async def handle_pay_usdt(query, context, user):
    qr = qrcode.QRCode(version=1, box_size=10, border=4)
    qr.add_data(USDT_ADDRESS)
    qr.make(fit=True)
    buf = BytesIO()
    qr.make_image(fill_color="black", back_color="white").save(buf, format="PNG")
    buf.seek(0)

    caption = (
        f"💰 <b>USDT Payment — {SUB_DAYS}-Day Subscription</b>\n\n"
        f"💵 Amount: <b>${SUB_PRICE} USDT (TRC20)</b>\n\n"
        f"📋 Send to this address:\n<code>{USDT_ADDRESS}</code>\n\n"
        f"⚠️ <b>Important:</b>\n"
        f"• Use <b>TRC20 (TRON)</b> network only\n"
        f"• After sending, tap <b>Confirm Payment</b> below\n"
        f"• Admin will verify & activate your subscription\n\n"
        f"👑 <b>Benefits:</b>\n"
        f"• Up to {SUB_VIDEO_LIMIT} videos/day\n"
        f"• Valid for {SUB_DAYS} days"
    )
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"✅ Confirm Payment (${SUB_PRICE})", callback_data=f"confirm_payment_{SUB_PRICE}")],
        [InlineKeyboardButton("◀️ Back", callback_data="pay")]
    ])

    # Delete old message, send new photo message
    try:
        await query.message.delete()
    except Exception:
        pass
    await context.bot.send_photo(
        chat_id=user.id,
        photo=buf,
        caption=caption,
        parse_mode="HTML",
        reply_markup=keyboard
    )


async def handle_confirm_payment(query, context, user, amount):
    payment_id = db.create_payment_request(user.id, user.username or user.first_name, amount)

    await safe_edit(
        query,
        f"⏳ <b>Payment Confirmation Submitted!</b>\n\n"
        f"Amount: <b>${amount} USDT</b>\n"
        f"Status: <b>Pending Admin Review</b>\n\n"
        f"You'll be notified once approved ✅",
        InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Main Menu", callback_data="back_main")]])
    )

    await context.bot.send_message(
        chat_id=ADMIN_ID,
        text=(
            f"💰 <b>New Payment Request</b>\n\n"
            f"👤 @{user.username or 'N/A'} (ID: <code>{user.id}</code>)\n"
            f"💵 Amount: <b>${amount} USDT</b>\n"
            f"🕐 {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}\n"
            f"🆔 Payment ID: <code>{payment_id}</code>\n\n"
            f"Please verify and approve/reject:"
        ),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Approve", callback_data=f"approve_{payment_id}"),
             InlineKeyboardButton("❌ Reject",  callback_data=f"reject_{payment_id}")]
        ])
    )


# ─────────────────────────── MY STATS ───────────────────────────
async def handle_my_stats(query, context, user):
    user_data = db.get_user(user.id)
    if not user_data:
        db.create_user(user.id, user.username or user.first_name)
        user_data = db.get_user(user.id)

    now         = datetime.utcnow()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    videos_today = db.count_videos_today(user.id, today_start)
    has_sub      = bool(user_data.get("subscription_expiry") and user_data["subscription_expiry"] > now)
    daily_limit  = SUB_VIDEO_LIMIT if has_sub else FREE_VIDEOS_PER_DAY
    sub_expiry_str = (
        user_data["subscription_expiry"].strftime("%Y-%m-%d")
        if has_sub else "No active subscription"
    )

    await safe_edit(
        query,
        f"📊 <b>Your Stats</b>\n\n"
        f"👤 @{user.username or user.first_name}\n"
        f"🆔 ID: <code>{user.id}</code>\n\n"
        f"📅 <b>Today:</b>\n"
        f"• Videos watched: <b>{videos_today}/{daily_limit}</b>\n"
        f"• Referral credits: <b>{user_data.get('referral_credits', 0)}</b>\n\n"
        f"👑 <b>Subscription:</b>\n"
        f"• Status: {'Active ✅' if has_sub else 'Free 🆓'}\n"
        f"• {'Expires: ' + sub_expiry_str if has_sub else sub_expiry_str}\n\n"
        f"👥 <b>Total Referrals:</b> {user_data.get('referral_count', 0)}",
        InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Back", callback_data="back_main")]])
    )


# ─────────────────────────── SUPPORT ───────────────────────────
async def handle_support_start(query, context, user):
    context.user_data["support_step"] = "typing"
    await safe_edit(
        query,
        "🆘 <b>Support</b>\n\n"
        "Please type your query or issue below.\n"
        "I'll forward it directly to the admin.",
        InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="support_cancel")]])
    )


async def handle_support_confirm(query, context, user):
    msg = context.user_data.get("support_message", "")
    if not msg:
        await query.answer("No message found. Please type your query first.", show_alert=True)
        return

    await context.bot.send_message(
        chat_id=ADMIN_ID,
        text=(
            f"🆘 <b>Support Request</b>\n\n"
            f"👤 @{user.username or 'N/A'} (ID: <code>{user.id}</code>)\n"
            f"🕐 {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}\n\n"
            f"📝 <b>Message:</b>\n{msg}"
        ),
        parse_mode="HTML"
    )
    context.user_data.pop("support_message", None)
    context.user_data.pop("support_step", None)

    await safe_edit(
        query,
        "✅ <b>Support request sent!</b>\n\nAdmin will get back to you soon. Thank you!",
        InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Main Menu", callback_data="back_main")]])
    )


# ─────────────────────────── TEXT MESSAGES ───────────────────────────
async def handle_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    text = update.message.text

    # Admin broadcast — waiting for text input
    if user.id == ADMIN_ID and context.user_data.get("broadcast_step") == "waiting_input":
        context.user_data["broadcast_text"]    = text
        context.user_data["broadcast_photo_id"] = None
        context.user_data["broadcast_step"]    = "preview"
        await update.message.reply_text(
            f"📢 <b>Broadcast Preview</b>\n\n{text}\n\n"
            f"<i>Send this text message to all users?</i>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Send Now", callback_data="broadcast_confirm")],
                [InlineKeyboardButton("❌ Cancel",   callback_data="broadcast_cancel")]
            ])
        )
        return

    if context.user_data.get("support_step") == "typing":
        context.user_data["support_message"] = text
        context.user_data["support_step"]    = "confirm"
        await update.message.reply_text(
            f"📝 <b>Your message:</b>\n\n{text}\n\n<i>Confirm to send to admin?</i>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Send Message", callback_data="support_confirm")],
                [InlineKeyboardButton("❌ Cancel",       callback_data="support_cancel")]
            ])
        )
        return

    await update.message.reply_text(
        "Use the menu buttons below to interact with the bot 👇",
        reply_markup=main_menu_keyboard()
    )


async def handle_photo_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles photos sent by admin — used for broadcast with image."""
    user = update.effective_user
    msg  = update.message

    if user.id != ADMIN_ID:
        return

    if context.user_data.get("broadcast_step") != "waiting_input":
        return

    photo_id = msg.photo[-1].file_id  # highest quality
    caption  = msg.caption or ""

    context.user_data["broadcast_photo_id"] = photo_id
    context.user_data["broadcast_text"]     = caption
    context.user_data["broadcast_step"]     = "preview"

    preview_caption = caption if caption else "<i>(No caption)</i>"
    await msg.reply_text(
        f"📢 <b>Broadcast Preview</b>\n\n"
        f"📷 Image will be sent with caption:\n{preview_caption}\n\n"
        f"<i>Send this to all users?</i>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Send Now", callback_data="broadcast_confirm")],
            [InlineKeyboardButton("❌ Cancel",   callback_data="broadcast_cancel")]
        ])
    )


# ─────────────────────────── ADMIN HANDLERS ───────────────────────────
async def handle_admin_stats(query, context):
    stats = db.get_stats()
    await safe_edit(
        query,
        f"📊 <b>Bot Statistics</b>\n\n"
        f"👥 Total Users: <b>{stats['total_users']}</b>\n"
        f"👑 Active Subscribers: <b>{stats['active_subscribers']}</b>\n"
        f"🆓 Free Users: <b>{stats['total_users'] - stats['active_subscribers']}</b>\n"
        f"🎬 Total Videos Sent: <b>{stats['total_videos_sent']}</b>\n"
        f"💾 Cached Videos: <b>{stats['cached_videos']}</b>\n"
        f"💰 Pending Payments: <b>{stats['pending_payments']}</b>\n"
        f"📅 Today's New Users: <b>{stats['today_new_users']}</b>\n"
        f"📹 Today's Videos: <b>{stats['today_videos']}</b>",
        InlineKeyboardMarkup([
            [InlineKeyboardButton("🔄 Refresh", callback_data="admin_stats")],
            [InlineKeyboardButton("◀️ Back",    callback_data="admin_panel")]
        ])
    )


async def handle_admin_pending_payments(query, context):
    payments = db.get_pending_payments()
    if not payments:
        await safe_edit(
            query, "✅ No pending payments at the moment.",
            InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Back", callback_data="admin_panel")]])
        )
        return

    await safe_edit(
        query,
        f"💰 <b>{len(payments)} Pending Payment(s)</b>\nSending details below...",
        InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Back", callback_data="admin_panel")]])
    )
    for p in payments[:10]:
        await context.bot.send_message(
            chat_id=ADMIN_ID,
            text=(
                f"💰 <b>Pending Payment</b>\n\n"
                f"👤 @{p.get('username', 'N/A')} (ID: <code>{p['user_id']}</code>)\n"
                f"💵 Amount: <b>${p['amount']} USDT</b>\n"
                f"🕐 {p['created_at'].strftime('%Y-%m-%d %H:%M UTC')}"
            ),
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Approve", callback_data=f"approve_{p['_id']}"),
                 InlineKeyboardButton("❌ Reject",  callback_data=f"reject_{p['_id']}")]
            ])
        )


async def handle_admin_users(query, context):
    stats  = db.get_stats()
    recent = db.get_recent_users(10)
    now    = datetime.utcnow()
    lines  = []
    for u in recent:
        sub   = "👑" if u.get("subscription_expiry") and u["subscription_expiry"] > now else "🆓"
        lines.append(f"{sub} @{u.get('username', 'N/A')} — <code>{u['user_id']}</code>")

    await safe_edit(
        query,
        f"👥 <b>Users ({stats['total_users']} total)</b>\n\n<b>Recent 10:</b>\n" + "\n".join(lines),
        InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Back", callback_data="admin_panel")]])
    )


async def handle_admin_add_videos(query, context):
    total = db.get_cached_video_count()
    await safe_edit(
        query,
        f"📹 <b>Video Library</b>\n\n"
        f"Currently in library: <b>{total} video(s)</b>\n\n"
        f"<b>How videos are added automatically:</b>\n"
        f"• Bot monitors the source channel 24/7\n"
        f"• Any new video posted there is instantly recorded\n"
        f"• Bot replies in the channel to confirm ✅\n\n"
        f"<b>To add manually (if needed):</b>\n"
        f"Send or forward any video to bot DM — it gets cached instantly.",
        InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Back", callback_data="admin_panel")]])
    )


# ─────────────────────────── BROADCAST ───────────────────────────
async def handle_admin_broadcast_start(query, context):
    context.user_data["broadcast_step"] = "waiting_input"
    context.user_data.pop("broadcast_text", None)
    context.user_data.pop("broadcast_photo_id", None)
    await safe_edit(
        query,
        "📢 <b>Broadcast to All Users</b>\n\n"
        "Send me the message you want to broadcast.\n\n"
        "You can send:\n"
        "• Text only\n"
        "• Photo only\n"
        "• Photo with caption (text below image)\n\n"
        "<i>Send your message now 👇</i>",
        InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="broadcast_cancel")]])
    )


async def handle_broadcast_confirm(query, context):
    text     = context.user_data.get("broadcast_text", "")
    photo_id = context.user_data.get("broadcast_photo_id")
    users    = db.get_all_user_ids()

    await safe_edit(query, f"📤 <b>Sending broadcast to {len(users)} users...</b>")

    success = 0
    failed  = 0
    for uid in users:
        try:
            if photo_id:
                await context.bot.send_photo(
                    chat_id=uid,
                    photo=photo_id,
                    caption=text or None,
                    parse_mode="HTML"
                )
            else:
                await context.bot.send_message(
                    chat_id=uid,
                    text=text,
                    parse_mode="HTML"
                )
            success += 1
        except Exception as e:
            logger.warning(f"Broadcast failed for user {uid}: {e}")
            failed += 1
        await asyncio.sleep(0.3)

    context.user_data.pop("broadcast_step", None)
    context.user_data.pop("broadcast_text", None)
    context.user_data.pop("broadcast_photo_id", None)

    await context.bot.send_message(
        chat_id=ADMIN_ID,
        text=(
            f"✅ <b>Broadcast Complete!</b>\n\n"
            f"📤 Sent: <b>{success}</b>\n"
            f"❌ Failed: <b>{failed}</b>"
        ),
        parse_mode="HTML",
        reply_markup=admin_menu_keyboard()
    )


async def handle_approve_payment(query, context, payment_id):
    payment = db.get_payment(payment_id)
    if not payment:
        await query.answer("Payment not found!", show_alert=True)
        return

    sub_expiry = datetime.utcnow() + timedelta(days=SUB_DAYS)
    db.approve_payment(payment_id, payment["user_id"], sub_expiry)

    await query.edit_message_text(
        f"✅ Payment approved for user <code>{payment['user_id']}</code>",
        parse_mode="HTML"
    )
    try:
        await context.bot.send_message(
            chat_id=payment["user_id"],
            text=(
                f"🎉 <b>Payment Approved!</b>\n\n"
                f"Your ${payment['amount']} USDT payment has been verified.\n\n"
                f"👑 <b>Subscription Activated!</b>\n"
                f"• Duration: {SUB_DAYS} days\n"
                f"• Daily limit: {SUB_VIDEO_LIMIT} videos/day\n"
                f"• Expires: {sub_expiry.strftime('%Y-%m-%d')}\n\n"
                f"Enjoy watching! 🎬"
            ),
            parse_mode="HTML",
            reply_markup=main_menu_keyboard()
        )
    except Exception as e:
        logger.error(f"Error notifying user {payment['user_id']}: {e}")


async def handle_reject_payment(query, context, payment_id):
    payment = db.get_payment(payment_id)
    if not payment:
        await query.answer("Payment not found!", show_alert=True)
        return

    db.reject_payment(payment_id)
    await query.edit_message_text(
        f"❌ Payment rejected for user <code>{payment['user_id']}</code>",
        parse_mode="HTML"
    )
    try:
        await context.bot.send_message(
            chat_id=payment["user_id"],
            text=(
                f"❌ <b>Payment Not Verified</b>\n\n"
                f"Your ${payment['amount']} USDT payment could not be verified.\n\n"
                f"Please ensure you sent the correct amount on TRC20 network and try again.\n"
                f"Contact support if you need help."
            ),
            parse_mode="HTML",
            reply_markup=main_menu_keyboard()
        )
    except Exception as e:
        logger.error(f"Error notifying user {payment['user_id']}: {e}")


# ─────────────────────────── SCHEDULER ───────────────────────────
async def check_subscriptions(app):
    logger.info("Running daily subscription check...")
    now          = datetime.utcnow()
    tomorrow_end = now + timedelta(days=1)

    for u in db.get_expiring_subscriptions(tomorrow_end):
        try:
            await app.bot.send_message(
                chat_id=u["user_id"],
                text=(
                    "⚠️ <b>Subscription Expiring Tomorrow!</b>\n\n"
                    "Your 7-day subscription expires tomorrow.\n\n"
                    "Please buy a new subscription to continue enjoying the service.\n"
                    "Thank you for your support! 🙏"
                ),
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("💳 Renew Subscription", callback_data="pay")]
                ])
            )
            db.mark_expiry_warned(u["user_id"])
        except Exception as e:
            logger.error(f"Error warning user {u['user_id']}: {e}")

    for u in db.get_expired_subscriptions(now):
        try:
            await app.bot.send_message(
                chat_id=u["user_id"],
                text=(
                    "🔴 <b>Subscription Expired!</b>\n\n"
                    "Your subscription has expired.\n\n"
                    "Please renew immediately to continue enjoying the service.\n"
                    "You are now on the free plan (3 videos/day)."
                ),
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("👥 Referral", callback_data="referral"),
                     InlineKeyboardButton("💳 Pay",      callback_data="pay")]
                ])
            )
            db.mark_subscription_expired(u["user_id"])
        except Exception as e:
            logger.error(f"Error notifying expired user {u['user_id']}: {e}")


# ─────────────────────────── STARTUP ───────────────────────────
async def resolve_source_channel(bot) -> int:
    """
    Try to discover the correct source channel ID.
    Priority:
      1. DB-stored ID (previously resolved)
      2. get_chat via invite link (works if bot is already a member)
      3. Fall back to SOURCE_CHANNEL_ID env var
    """
    # 1. Check DB for previously saved channel ID
    saved = db.get_config("source_channel_id")
    if saved:
        logger.info(f"Using saved source channel ID from DB: {saved}")
        return int(saved)

    # 2. Try get_chat with the invite link
    if GROUP_INVITE_LINK:
        logger.info(f"Trying get_chat with invite link: {GROUP_INVITE_LINK}")
        try:
            chat = await bot.get_chat(GROUP_INVITE_LINK)
            channel_id = chat.id
            db.save_config("source_channel_id", str(channel_id))
            logger.info(f"Resolved group chat ID: {channel_id} ({chat.title})")
            return channel_id
        except Exception as e:
            logger.warning(f"get_chat via invite link failed: {e}")
            logger.warning("Bot is probably not a member of the group yet.")
            logger.warning("Add the bot to the group and it will auto-configure.")

    # 3. Fall back to env var
    logger.info(f"Falling back to SOURCE_CHANNEL_ID env: {SOURCE_CHANNEL_ID}")
    return SOURCE_CHANNEL_ID


async def on_bot_added_to_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Fires when bot's membership status changes in any chat."""
    global SOURCE_CHANNEL_ID
    result = update.my_chat_member
    if not result:
        return

    chat      = result.chat
    new_status = result.new_chat_member.status

    # Bot was added (member or administrator)
    if new_status in ("member", "administrator") and chat.type in ("group", "supergroup", "channel"):
        logger.info(f"Bot added to chat: {chat.title} (ID: {chat.id})")

        # Save this as the source channel
        db.save_config("source_channel_id", str(chat.id))
        SOURCE_CHANNEL_ID = chat.id
        import video_fetcher as vf
        vf.SOURCE_CHANNEL_ID = chat.id

        await context.bot.send_message(
            chat_id=ADMIN_ID,
            text=(
                f"✅ <b>Bot added to source group!</b>\n\n"
                f"Group: <b>{chat.title}</b>\n"
                f"Chat ID: <code>{chat.id}</code>\n\n"
                f"📹 <b>To add videos to the library:</b>\n"
                f"Go to this group → select a video → forward it here to me.\n"
                f"I'll add it automatically. No scanning needed!"
            ),
            parse_mode="HTML"
        )


async def post_init(app: Application):
    global SOURCE_CHANNEL_ID

    # Start scheduler
    scheduler = AsyncIOScheduler()
    scheduler.add_job(check_subscriptions, 'cron', hour=9, minute=0, args=[app])
    scheduler.start()
    logger.info("Scheduler started.")

    # Resolve the correct source channel ID
    SOURCE_CHANNEL_ID = await resolve_source_channel(app.bot)

    # Verify that the resolved channel is actually accessible
    try:
        await app.bot.get_chat(SOURCE_CHANNEL_ID)
        logger.info(f"✅ Source channel {SOURCE_CHANNEL_ID} is accessible.")
        import video_fetcher as vf
        vf.SOURCE_CHANNEL_ID = SOURCE_CHANNEL_ID

        cached = db.get_cached_video_count()
        if cached == 0:
            logger.info("No cached videos. Admin must forward videos to bot DM to add them.")
            try:
                await app.bot.send_message(
                    chat_id=ADMIN_ID,
                    text=(
                        "📹 <b>Video library is empty!</b>\n\n"
                        "To add videos:\n"
                        "1. Go to your source group\n"
                        "2. Select a video\n"
                        "3. Forward it to me here\n\n"
                        "I'll add it instantly — no scanning needed!"
                    ),
                    parse_mode="HTML"
                )
            except Exception:
                pass
        else:
            logger.info(f"Video cache already has {cached} video(s). Ready!")

    except Exception as e:
        logger.warning(f"Source channel {SOURCE_CHANNEL_ID} not accessible: {e}")
        # Clear bad cached ID so we don't keep using it
        db.save_config("source_channel_id", "")
        SOURCE_CHANNEL_ID = 0
        logger.warning(
            "⚠️ Source group not configured properly.\n"
            "Admin: Go to the source group and type /setsource to register it."
        )
        try:
            await app.bot.send_message(
                chat_id=ADMIN_ID,
                text=(
                    "⚠️ <b>Source Group Not Configured!</b>\n\n"
                    "The bot cannot access the video source group.\n\n"
                    "<b>To fix this:</b>\n"
                    "1. Make sure the bot is added to the source group as Admin\n"
                    "2. Go to that group\n"
                    "3. Type <code>/setsource</code>\n\n"
                    "Then forward videos from that group to me to add them to the library."
                ),
                parse_mode="HTML"
            )
        except Exception:
            pass


# ─────────────────────────── CHANNEL POST LISTENER ───────────────────────────
def _extract_file_info(msg) -> tuple[str, str] | tuple[None, None]:
    """Extract (file_id, file_type) from a message. Returns (None, None) if not a video."""
    if msg.video:
        return msg.video.file_id, "video"
    if msg.animation:
        return msg.animation.file_id, "animation"
    if msg.video_note:
        return msg.video_note.file_id, "video_note"
    if msg.document and msg.document.mime_type and "video" in msg.document.mime_type:
        return msg.document.file_id, "document"
    return None, None


async def handle_channel_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Monitors source channel in real-time.
    When a new video is posted:
      1. Records it in the database (by file_id).
      2. Sends a reply/notification in the same channel confirming it was noted.
    """
    msg = update.channel_post
    if not msg:
        return
    if SOURCE_CHANNEL_ID and msg.chat.id != SOURCE_CHANNEL_ID:
        return

    file_id, file_type = _extract_file_info(msg)
    if not file_id:
        return

    db.add_video_to_cache(file_id, file_type)
    total = db.get_cached_video_count()
    logger.info(f"Auto-cached channel video type={file_type} total={total}")

    # Notify in the source channel that the video has been recorded
    try:
        await context.bot.send_message(
            chat_id=msg.chat.id,
            text=(
                f"✅ <b>Video #{total} noted!</b>\n"
                f"📦 Total in library: <b>{total}</b> video(s)"
            ),
            parse_mode="HTML",
            reply_to_message_id=msg.message_id
        )
    except Exception as e:
        logger.warning(f"Could not send group notification: {e}")




# ─────────────────────────── /clearcache COMMAND ───────────────────────────
async def clear_cache_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin clears the entire video cache (removes all stored message IDs)."""
    user = update.effective_user
    if user.id != ADMIN_ID:
        return
    deleted = db.clear_video_cache()
    await update.message.reply_text(
        f"🗑 <b>Video cache cleared!</b>\n\n"
        f"Removed <b>{deleted}</b> cached video ID(s).\n\n"
        f"Now forward videos from your source group to me to re-add them to the library.",
        parse_mode="HTML"
    )




# ─────────────────────────── FORWARDED/SENT VIDEO HANDLER (ADMIN) ───────────────────────────
async def handle_admin_video_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Admin sends or forwards a video to the bot DM → extract file_id and cache it.
    Works even when the source channel has 'Protect Content' enabled,
    because we cache the file_id from the message object directly.
    """
    user = update.effective_user
    msg  = update.message

    if user.id != ADMIN_ID:
        return

    file_id, file_type = _extract_file_info(msg)
    if not file_id:
        return

    db.add_video_to_cache(file_id, file_type)
    total = db.get_cached_video_count()

    await msg.reply_text(
        f"✅ <b>Video added to library!</b>\n\n"
        f"Type: <b>{file_type}</b>\n"
        f"Total videos in library: <b>{total}</b>",
        parse_mode="HTML"
    )


# ─────────────────────────── KEEP-ALIVE (Render) ────────────────────────────

async def health_check(request):
    """Simple health-check endpoint so Render marks the service as healthy."""
    return web.Response(text="OK", content_type="text/plain")


async def keep_alive_ping(url: str):
    """
    Pings our own public URL every 15 seconds so Render's free tier
    never idles the instance down.
    """
    ping_url = url.rstrip("/") + "/"
    logger.info(f"Keep-alive pinger started → {ping_url}")
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                async with session.get(
                    ping_url,
                    timeout=aiohttp.ClientTimeout(total=10)
                ) as resp:
                    logger.debug(f"Keep-alive ping OK ({resp.status})")
            except Exception as e:
                logger.warning(f"Keep-alive ping failed: {e}")
            await asyncio.sleep(15)


# ─────────────────────────── MAIN ───────────────────────────

def _build_app() -> Application:
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("admin", admin_command))
    app.add_handler(CommandHandler("id", get_chat_id))
    app.add_handler(CommandHandler("setsource", set_source))
    app.add_handler(CommandHandler("clearcache", clear_cache_command))
    app.add_handler(ChatMemberHandler(on_bot_added_to_chat, ChatMemberHandler.MY_CHAT_MEMBER))
    # Channel post listener — auto-caches new videos posted in source channel
    app.add_handler(MessageHandler(filters.UpdateType.CHANNEL_POST, handle_channel_post))
    app.add_handler(CallbackQueryHandler(button_handler))
    # Admin photo handler (broadcast with image) — must come before video/text handlers
    app.add_handler(MessageHandler(
        filters.ChatType.PRIVATE & filters.User(ADMIN_ID) & filters.PHOTO,
        handle_photo_message
    ))
    # Admin video handler — admin sends/forwards a video to bot DM → auto-cached by file_id
    app.add_handler(MessageHandler(
        filters.ChatType.PRIVATE &
        (filters.VIDEO | filters.Document.ALL | filters.ANIMATION | filters.VIDEO_NOTE),
        handle_admin_video_message
    ))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_message))
    return app


async def run():
    # ── 1. Start the aiohttp health-check web server ──────────────
    web_app = web.Application()
    web_app.router.add_get("/",        health_check)
    web_app.router.add_get("/health",  health_check)

    runner = web.AppRunner(web_app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logger.info(f"Health-check server started on port {PORT}")

    # ── 2. Start keep-alive pinger if deployed on Render ──────────
    if RENDER_EXTERNAL_URL:
        asyncio.create_task(keep_alive_ping(RENDER_EXTERNAL_URL))
    else:
        logger.info("RENDER_EXTERNAL_URL not set — keep-alive pinger inactive (local dev mode)")

    # ── 3. Start the Telegram bot ──────────────────────────────────
    bot_app = _build_app()
    logger.info("Bot is starting...")

    async with bot_app:
        await bot_app.start()
        await bot_app.updater.start_polling(allowed_updates=Update.ALL_TYPES)
        logger.info("Bot is polling for updates.")
        # Block here forever (Ctrl-C / SIGTERM will unblock)
        await asyncio.Event().wait()


def main():
    asyncio.run(run())


if __name__ == "__main__":
    main()
