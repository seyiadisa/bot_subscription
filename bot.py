import logging
import os
import uuid
import pytz
import datetime
import requests
from dotenv import load_dotenv
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Bot,
    ReplyKeyboardMarkup,
)
from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
)
from db import (
    create_tables,
    get_expired_subscriptions,
    get_user_subscription,
    remove_subscription,
)

load_dotenv()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)

PAYSTACK_SECRET_KEY = os.getenv("PAYSTACK_SECRET_KEY")
DATABASE_URL = os.getenv("DATABASE_URL")
BOT_TOKEN = os.getenv("BOT_TOKEN")
TELEGRAM_GROUP_ID = os.getenv("TELEGRAM_GROUP_ID")
PORT = int(os.environ.get("PORT", "8443"))

subscription_plans = {
    "15 Minutes": {"duration": datetime.timedelta(minutes=15), "price": 15000},
    "30 Minutes": {"duration": datetime.timedelta(minutes=30), "price": 25000},
    "1 Hour": {"duration": datetime.timedelta(minutes=60), "price": 95000},
}

bot_instance = Bot(token=BOT_TOKEN)


def generate_unique_reference():
    reference = str(uuid.uuid4())
    if len(reference) > 100:
        reference = reference[:100]
    return reference


def initiate_payment(
    amount, email, reference, telegram_chat_id, subscription_type, username
):
    url = "https://api.paystack.co/transaction/initialize"
    headers = {
        "Authorization": f"Bearer {PAYSTACK_SECRET_KEY}",
        "Content-Type": "application/json",
    }
    data = {
        "amount": amount * 100,  # Paystack expects the amount in kobo (NGN)
        "email": email,
        "reference": reference,
        "metadata": {
            "telegram_chat_id": telegram_chat_id,
            "payment_reference": reference,
            "subscription_type": subscription_type,
            "username": username,
        },
    }
    response = requests.post(url, json=data, headers=headers)
    return response.json()


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        return
    try:
        keyboard = [["Join Private Group"], ["Subscription Status"]]

        reply_markup = ReplyKeyboardMarkup(
            keyboard,
            resize_keyboard=True,
            input_field_placeholder="Select an option below to interact with the bot",
        )

        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="Welcome, please click on the 'Join private group' button below",
            reply_markup=reply_markup,
        )
    except Exception as e:
        logging.error(f"Error in start handler: {e}")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        return
    user_message = update.message.text

    if user_message == "Join Private Group":
        await plans(update, context)
    elif user_message == "Subscription Status":
        await check_subscription_status(update, context)
    else:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="You selected an option. Please use /plans to see subscription plans or other options.",
        )


async def plans(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        return
    keyboard = [
        [InlineKeyboardButton("15 minutes: 15,000 NGN", callback_data="15 Minutes")],
        [InlineKeyboardButton("30 minutes: 25,000 NGN", callback_data="30 Minutes")],
        [InlineKeyboardButton("1 Hour: 95,000 NGN", callback_data="1 Hour")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        "Choose a subscription plan:", reply_markup=reply_markup
    )


async def check_subscription_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    subscription = get_user_subscription(update.effective_chat.id)

    if not subscription:
        await update.message.reply_text("You do not have an active subscription")
    else:
        logging.info(subscription["end_date"])
        expiry_date = datetime.datetime.fromisoformat(str(subscription["end_date"]))
        expiry_date = expiry_date.astimezone(pytz.timezone("Africa/Lagos"))

        await update.message.reply_text(
            f"Your subscription expires on: {expiry_date.date()} by {expiry_date.hour}:{expiry_date.minute}"
        )


async def check_subscription_expiry(context: ContextTypes.DEFAULT_TYPE):
    expired_subscriptions = get_expired_subscriptions()
    for subscription in expired_subscriptions:
        telegram_chat_id = subscription["telegram_chat_id"]
        keyboard = [
            [InlineKeyboardButton("Renew", callback_data=f"renew|{telegram_chat_id}")],
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await bot_instance.send_message(
            chat_id=telegram_chat_id,
            text="Your subscription has expired and you have been removed from the group. Renew your subscription to join again.",
            reply_markup=reply_markup,
        )
        # Remove user from the group
        await bot_instance.ban_chat_member(
            chat_id=TELEGRAM_GROUP_ID, user_id=telegram_chat_id
        )
        remove_subscription(telegram_chat_id)
        logging.info(
            f"User {telegram_chat_id} removed from group due to expired subscription"
        )


if __name__ == "__main__":
    from callbacks import (
        cancel_payment,
        select_plan,
        handle_renew,
    )

    create_tables()

    application = ApplicationBuilder().token(BOT_TOKEN).build()

    start_handler = CommandHandler("start", start)
    plans_handler = CommandHandler("plans", plans)
    select_plan_handler = CallbackQueryHandler(
        select_plan, pattern="^15 Minutes$|^30 Minutes$|^1 Hour$"
    )
    cancel_handler = CallbackQueryHandler(cancel_payment, pattern="^cancel\\|")
    message_handler = MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message)
    renew_handler = CallbackQueryHandler(handle_renew, pattern="^renew\\|")

    application.add_handler(start_handler)
    application.add_handler(plans_handler)
    application.add_handler(select_plan_handler)
    application.add_handler(cancel_handler)
    application.add_handler(message_handler)
    application.add_handler(renew_handler)

    job_queue = application.job_queue
    job_queue.run_repeating(
        check_subscription_expiry, interval=datetime.timedelta(seconds=200), first=0
    )

    application.run_polling()
