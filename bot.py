import logging
import os
import httpx
from dotenv import load_dotenv
from typing import Final
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

# httpx logs full request URLs at INFO level, and Telegram's Bot API embeds
# the bot token directly in the URL path (not a header) -- this is the same
# leak fixed for the fastapi service's logging (see db.py); this process
# runs independently and never imports db.py, so it needs the same explicit
# suppression rather than inheriting it.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
BOT_USERNAME: Final = '@polymarket_live_trades_bot'
FASTAPI_URL = os.getenv("FASTAPI_URL", "http://127.0.0.1:8000")

# Commands
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text('Welcome to the Polymarket live trades bot! I will notify you whenever there is high trade volume activity! Use /track <slug> to start receiving alerts.')

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text('Commands:\n/track <slug> [limit] - Start tracking\n/untrack <slug> - Stop tracking')

async def track_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /track <slug> [min_usd_size]")
        return

    slug = context.args[0]
    limit = 0.0
    if len(context.args) > 1:
        try:
            limit = float(context.args[1])
        except ValueError:
            await update.message.reply_text("Invalid limit value. Please provide a number.")
            return

    chat_id = update.effective_chat.id
    await update.message.reply_text(f"Connecting to market: {slug} with limit > ${limit}")

    try:
        url = f"{FASTAPI_URL}/get-live-trades/{slug}"

        params = {
            'chat_id': chat_id,
            'limit': limit
        }
        
        async with httpx.AsyncClient() as client:
            response = await client.get(url, params=params, timeout=5.0)
            
        if response.status_code == 200:
            await update.message.reply_text(f"Tracking started, alerts will be sent here.")
        else:
            await update.message.reply_text(f"Server error: {response.text}")
    except Exception as e:
        await update.message.reply_text(f"Failed to reach API: {e}")

async def untrack_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /untrack <slug>")
        return

    slug = context.args[0]
    chat_id = update.effective_chat.id

    url = f"{FASTAPI_URL}/untrack/{slug}"
    params = {'chat_id': chat_id}

    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(url, params=params, timeout=5.0)
            
        if response.status_code == 200:
             await update.message.reply_text(f"Stopped tracking: {slug}")
        elif response.status_code == 404:
             await update.message.reply_text(f"You are not currently tracking: {slug}")
        else:
             await update.message.reply_text(f"Error: {response.text}")
    except Exception as e:
        await update.message.reply_text(f"Failed to reach API: {e}")

# Responses
def handle_response(text: str) -> str:
    processed: str = text.lower()
    if 'help' in processed:
        return 'In order to know the commands, please type: /help'
    else:
        return 'If you are lost, please type: /help'

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message_type: str = update.message.chat.type
    text: str = update.message.text

    print(f'User ({update.message.chat.id}) in {message_type}: "{text}"')

    if message_type == 'group':
        if BOT_USERNAME in text:
            new_text: str = text.replace(BOT_USERNAME, '').strip()
            response: str = handle_response(new_text)
        else:
            return
    else:
        response: str = handle_response(text)

    print('Bot:', response)
    await update.message.reply_text(response)

async def error(update: Update, context: ContextTypes.DEFAULT_TYPE):
    print(f'Update {update} caused error {context.error}')

def main():
    print('Starting bot')
    if not BOT_TOKEN:
        print("bot: BOT_TOKEN not set")
        return
    app = Application.builder().token(BOT_TOKEN).build()

    # Commands
    app.add_handler(CommandHandler('start', start_command))
    app.add_handler(CommandHandler('track', track_command))
    app.add_handler(CommandHandler('untrack', untrack_command))
    app.add_handler(CommandHandler('help', help_command))

    # Messages
    app.add_handler(MessageHandler(filters.TEXT, handle_message))

    # Errors
    app.add_error_handler(error)

    # Polls the bot
    print('Polling')
    app.run_polling()

if __name__ == '__main__':
    main()