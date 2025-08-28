import os
import logging
import tempfile
import zipfile
from dotenv import load_dotenv
from pathlib import Path
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, ReplyKeyboardRemove, BotCommand
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, CallbackQueryHandler
from telegram.constants import ChatAction
from PIL import Image
import img2pdf
from pypdf import PdfReader, PdfWriter
import fitz  # PyMuPDF

# Load environment variables
load_dotenv(Path(__file__).parent / '.env')
print(f"Loaded .env: {os.getenv('BOT_TOKEN')}")
BOT_TOKEN = os.getenv('BOT_TOKEN')

# Quality presets
IMAGE_QUALITY = {'high': 95, 'medium': 85, 'low': 70}

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class TelegramConverter:
    def __init__(self, token):
        self.token = token
        self.app = Application.builder().token(token).build()
        self.setup_handlers()
        self.setup_bot_commands()  # Ensure bot commands are set up during initialization
        self.pending_operation = {}  # Store user operations

    def setup_handlers(self):
        self.app.add_handler(CommandHandler("start", self.start_command))
        self.app.add_handler(CommandHandler("help", self.help_command))
        self.app.add_handler(CommandHandler("img2pdf", self.prompt_img2pdf))
        self.app.add_handler(CommandHandler("pdf2img", self.prompt_pdf2img))
        self.app.add_handler(CommandHandler("compress", self.prompt_compress))
        self.app.add_handler(CommandHandler("done", self.done_command))
        self.app.add_handler(CallbackQueryHandler(self.handle_callback))
        self.app.add_handler(MessageHandler(filters.PHOTO | filters.ATTACHMENT, self.handle_file))
        self.app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_text))

    def setup_bot_commands(self):
        commands = [
            BotCommand("start", "Show the main menu"),
            BotCommand("help", "Show help information"),
            BotCommand("img2pdf", "Convert images to PDF"),
            BotCommand("pdf2img", "Convert PDF to images"),
            BotCommand("compress", "Compress images or PDFs"),
            BotCommand("done", "Finish multi-image conversion")
        ]
        self.app.bot.set_my_commands(commands)
        print("Bot commands registered:", [cmd.command for cmd in commands])  # Debug output

    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        keyboard = [
            [InlineKeyboardButton("🖼️ Images (img2pdf, pdf2img)", callback_data="images")],
            [InlineKeyboardButton("🗜️ Compress (images/PDFs)", callback_data="compress")],
            [InlineKeyboardButton("❓ Help", callback_data="help")]
        ]
        reply_keyboard = [["/img2pdf", "/pdf2img"], ["/compress", "/help"], ["Back"]]
        await update.message.reply_text("Welcome! Choose an option:", reply_markup=InlineKeyboardMarkup(keyboard))

    async def help_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        help_text = """
Simple Commands & Usage:

- /img2pdf: Send one or more images (JPG, PNG, etc.) to convert to a single PDF. Send images and type /done to finish.
- /pdf2img: Send a PDF to convert to images (PNG files or ZIP if multiple pages, up to 10).
- /compress [high/medium/low]: Send an image or PDF to compress. Specify quality (e.g., /compress medium) or bot will ask.
- /help: Show this help message.

Tips:
- Use /start for the menu.
- For multiple images, send all and type /done.
- Use 'Back' to return to the main menu.
        """
        if update.message:
            await update.message.reply_text(help_text, reply_markup=ReplyKeyboardRemove())
        elif update.callback_query:
            await update.callback_query.message.edit_text(help_text)
            await update.callback_query.answer()

    async def handle_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        data = query.data
        await query.answer()
        if data == "images":
            await query.edit_message_text("/img2pdf or /pdf2img - Send files after command.")
        elif data == "compress":
            await query.edit_message_text("/compress [high/medium/low] - Send image or PDF after.")
        elif data == "help":
            await self.help_command(update, context)

    async def handle_file(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.message.from_user.id if update.message else update.callback_query.from_user.id
        op = self.pending_operation.get(user_id, {})
        if filters.PHOTO.check_update(update):
            if 'img2pdf' in op:
                op['files'] = op.get('files', []) + [update.message.photo[-1].file_id]
                await update.message.reply_text(f"Added image. Total: {len(op['files'])}. Send more or /done to convert.", reply_markup=ReplyKeyboardMarkup([["/done", "Back"]], resize_keyboard=True))
            elif 'compress' in op:
                quality = op.get('quality', 'medium')
                await self.compress_file(update, context, quality, update.message.photo[-1].file_id)
            else:
                await update.message.reply_text("Use /img2pdf or /compress first.", reply_markup=ReplyKeyboardMarkup([["/img2pdf", "/compress", "/help"], ["Back"]], resize_keyboard=True))
        elif update.message and update.message.document:
            doc = update.message.document
            file_name = doc.file_name.lower()
            if 'pdf2img' in op and file_name.endswith('.pdf'):
                await self.convert_pdf2img(update, context, doc.file_id)
            elif 'compress' in op and file_name.endswith('.pdf'):
                quality = op.get('quality', 'medium')
                await self.compress_file(update, context, quality, doc.file_id)
            else:
                await update.message.reply_text("Use /pdf2img or /compress first.", reply_markup=ReplyKeyboardMarkup([["/pdf2img", "/compress", "/help"], ["Back"]], resize_keyboard=True))

    async def handle_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = update.message.text.lower() if update.message else ''
        user_id = update.message.from_user.id if update.message else update.callback_query.from_user.id
        op = self.pending_operation.get(user_id, {})
        if text == 'back':
            await self.start_command(update, context)
        elif 'compress' in op and text in ['high', 'medium', 'low']:
            op['quality'] = text
            await update.message.reply_text(f"Quality set to {text}. Send image or PDF to compress.", reply_markup=ReplyKeyboardMarkup([["Back"]], resize_keyboard=True))
        else:
            await update.message.reply_text("Use /done for img2pdf or set quality (high/medium/low) for compress. See /help.", reply_markup=ReplyKeyboardMarkup([["/img2pdf", "/pdf2img", "/compress", "/help"], ["Back"]], resize_keyboard=True))

    async def done_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.message.from_user.id
        op = self.pending_operation.get(user_id, {})
        if 'img2pdf' in op and 'files' in op and op['files']:
            await self.convert_img2pdf(update, context, op['files'])
            del self.pending_operation[user_id]
            await update.message.reply_text("Conversion complete.", reply_markup=ReplyKeyboardRemove())
        else:
            await update.message.reply_text("No images to convert or operation not started. Use /img2pdf first.", reply_markup=ReplyKeyboardRemove())

    async def prompt_img2pdf(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        self.pending_operation[update.message.from_user.id] = {'img2pdf': True, 'files': []}
        await update.message.reply_text("Send images (JPG, PNG, etc.). Send more or /done to convert.", reply_markup=ReplyKeyboardMarkup([["/done", "Back"]], resize_keyboard=True))

    async def prompt_pdf2img(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        self.pending_operation[update.message.from_user.id] = {'pdf2img': True}
        await update.message.reply_text("Send PDF to convert to images.", reply_markup=ReplyKeyboardMarkup([["Back"]], resize_keyboard=True))

    async def prompt_compress(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        args = context.args
        quality = args[0] if args and args[0] in ['high', 'medium', 'low'] else None
        self.pending_operation[update.message.from_user.id] = {'compress': True, 'quality': quality}
        if not quality:
            await update.message.reply_text("Choose quality: high, medium, low. Then send image or PDF.", reply_markup=ReplyKeyboardMarkup([["high", "medium", "low"], ["Back"]], resize_keyboard=True))
        else:
            await update.message.reply_text(f"Send image or PDF to compress ({quality}).", reply_markup=ReplyKeyboardMarkup([["Back"]], resize_keyboard=True))

    async def convert_img2pdf(self, update: Update, context: ContextTypes.DEFAULT_TYPE, file_ids):
        await update.message.reply_chat_action(ChatAction.UPLOAD_DOCUMENT)
        with tempfile.TemporaryDirectory() as temp_dir:
            paths = []
            for fid in file_ids:
                file = await context.bot.get_file(fid)
                path = os.path.join(temp_dir, f"{fid}.jpg")
                await file.download_to_drive(path)
                paths.append(path)
            output_path = os.path.join(temp_dir, "converted.pdf")
            with open(output_path, "wb") as f:
                f.write(img2pdf.convert(paths))
            with open(output_path, 'rb') as pdf:
                await update.message.reply_document(pdf, filename="converted.pdf")

    async def convert_pdf2img(self, update: Update, context: ContextTypes.DEFAULT_TYPE, file_id):
        await update.message.reply_chat_action(ChatAction.UPLOAD_PHOTO)
        file = await context.bot.get_file(file_id)
        with tempfile.TemporaryDirectory() as temp_dir:
            pdf_path = os.path.join(temp_dir, "input.pdf")
            await file.download_to_drive(pdf_path)
            pdf_doc = fitz.open(pdf_path)
            files = []
            for i in range(min(10, len(pdf_doc))):
                page = pdf_doc[i]
                pix = page.get_pixmap(dpi=150)
                img_path = os.path.join(temp_dir, f"page_{i+1}.png")
                pix.save(img_path)
                files.append(img_path)
            if len(files) > 1:
                zip_path = os.path.join(temp_dir, "images.zip")
                with zipfile.ZipFile(zip_path, 'w') as z:
                    for f in files:
                        z.write(f, os.path.basename(f))
                with open(zip_path, 'rb') as zf:
                    await update.message.reply_document(zf, filename="images.zip")
            else:
                with open(files[0], 'rb') as img:
                    await update.message.reply_photo(img)
            pdf_doc.close()
        await update.message.reply_text("Conversion complete.", reply_markup=ReplyKeyboardRemove())

    async def compress_file(self, update: Update, context: ContextTypes.DEFAULT_TYPE, quality, file_id):
        await update.message.reply_chat_action(ChatAction.UPLOAD_DOCUMENT)
        file = await context.bot.get_file(file_id)
        with tempfile.TemporaryDirectory() as temp_dir:
            input_path = os.path.join(temp_dir, f"input{file.file_path.split('.')[-1]}")
            await file.download_to_drive(input_path)
            output_path = os.path.join(temp_dir, "compressed" + os.path.splitext(file.file_path)[1])
            if file.file_path.lower().endswith(('.jpg', '.png', '.jpeg')):
                img = Image.open(input_path)
                img.save(output_path, quality=IMAGE_QUALITY[quality], optimize=True)
            elif file.file_path.lower().endswith('.pdf'):
                pdf_doc = fitz.open(input_path)
                pdf_doc.save(output_path, garbage=3, deflate=True, clean=True, use_objstms=1)
                pdf_doc.close()
            with open(output_path, 'rb') as out:
                await update.message.reply_document(out, filename="compressed" + os.path.splitext(file.file_path)[1])
        await update.message.reply_text("Compression complete.", reply_markup=ReplyKeyboardRemove())

    def run(self):
        self.app.run_polling()

if __name__ == "__main__":
    if not BOT_TOKEN:
        print("Set BOT_TOKEN in .env")
        exit(1)
    bot = TelegramConverter(BOT_TOKEN)
    bot.run()