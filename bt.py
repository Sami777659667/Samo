import os
import re
import sys
import zipfile
import asyncio
import subprocess
import signal
import shutil
import warnings
import uuid

# --- إضافات Webhook/Flask ---
from flask import Flask, request, jsonify # إضافة Flask

# --- إسكات التحذيرات ---
from telegram.warnings import PTBUserWarning
warnings.filterwarnings("ignore", category=PTBUserWarning)

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import Application, ApplicationBuilder, ContextTypes, CommandHandler, MessageHandler, filters, CallbackQueryHandler, ConversationHandler

import db

# --- إعدادات التكوين ---
HOST_TOKEN = "8596718039:AAHs3e1WU_2kVriqFSk9rjIxl26Zm_wBDP8"   # 🔴 ضع توكن البوت المضيف
ARCHIVE_CHANNEL_ID = -1001234567890     # 🔴 معرف قناة الأرشيف
ADMIN_ID = 123456789                    # 🔴 معرف المشرف

# المسارات الأساسية
BASE_DIR = os.path.abspath(os.getcwd())
HOSTING_DIR = os.path.join(BASE_DIR, "hosted_bots")
if not os.path.exists(HOSTING_DIR): os.makedirs(HOSTING_DIR)

# حالات المحادثة
WAITING_UPLOAD = 1
WAITING_TOKEN = 2

# تهيئة قاعدة البيانات
db.init_db()

# --- تعريف تطبيق Flask لاستضافة الويب هوك ---
flask_app = Flask(__name__) # تم تعريف التطبيق هنا

# --- 1. نظام الطابور (Message Queue) ---
deployment_queue = asyncio.Queue()

async def worker_processor(app: Application):
    """عامل يعمل في الخلفية لمعالجة الطابور"""
    print("👷 Worker started, waiting for tasks...")
    while True:
        # الانتظار حتى تصل مهمة جديدة
        task_data = await deployment_queue.get()
        user_id, chat_id, file_info, token, context = task_data
        
        try:
            await process_deployment(user_id, chat_id, file_info, token, context)
        except Exception as e:
            print(f"Queue Error: {e}")
            try:
                await context.bot.send_message(chat_id, f"❌ خطأ داخلي: {e}")
            except: pass
        
        deployment_queue.task_done()

# --- 2. Sandbox & Security ---
class SecurityScanner:
    DANGEROUS_PATTERNS = [
        r'os\.system\(', r'subprocess\.call\(', r'shutil\.rmtree\(',
        r'import\s+os', r'open\(.*w.*\)'
    ]
    @staticmethod
    def scan_directory(folder_path):
        warnings_found = []
        for root, _, files in os.walk(folder_path):
            for file in files:
                if file.endswith(".py"):
                    path = os.path.join(root, file)
                    try:
                        with open(path, 'r', encoding='utf-8', errors='ignore') as f:
                            content = f.read()
                            for pattern in SecurityScanner.DANGEROUS_PATTERNS:
                                if re.search(pattern, content):
                                    warnings_found.append(f"⚠️ `{file}`: `{pattern}`")
                    except: pass
        return warnings_found

# --- دوال المساعدة ---
def smart_inject_token(folder_path, token):
    token_patterns = [
        r'(TOKEN\s*=\s*)["\'].*?["\']',
        r'(API_KEY\s*=\s*)["\'].*?["\']',
        r'(bot_token\s*=\s*)["\'].*?["\']'
    ]
    for root, dirs, files in os.walk(folder_path):
        for file in files:
            if file.endswith(".py"):
                path = os.path.join(root, file)
                try:
                    with open(path, 'r', encoding='utf-8') as f: content = f.read()
                    new_c = content
                    for p in token_patterns:
                        if re.search(p, content, re.IGNORECASE):
                            new_c = re.sub(p, f'\\1"{token}"', new_c, flags=re.IGNORECASE)
                    if content != new_c:
                        with open(path, 'w', encoding='utf-8') as f: f.write(new_c)
                except: pass
                
def find_main_file(folder_path):
    candidates = ["main.py", "bot.py", "run.py"]
    for f in os.listdir(folder_path):
        if f in candidates: return os.path.join(folder_path, f)
    for root, _, files in os.walk(folder_path):
        for f in files:
            if f.endswith(".py"):
                path = os.path.join(root, f)
                try:
                    with open(path, 'r', errors='ignore') as fr:
                        if "ApplicationBuilder" in fr.read() or "Updater" in fr.read(): return path
                except: continue
    return None

# --- Handlers ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = [[KeyboardButton("🚀 رفع بوت"), KeyboardButton("🤖 بوتاتي")],
          [KeyboardButton("📚 تعليمات")]]
    await update.message.reply_text("🖥 **نظام الاستضافة المتقدم**", reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True))

async def upload_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = [[InlineKeyboardButton("📦 ZIP (شامل)", callback_data='up_zip'), InlineKeyboardButton("📄 Py (فردي)", callback_data='up_single')],
          [InlineKeyboardButton("❌ إلغاء", callback_data='cancel')]]
    await update.message.reply_text("نوع الملف؟", reply_markup=InlineKeyboardMarkup(kb))
    return WAITING_UPLOAD

async def handle_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if q.data == 'cancel': 
        await q.edit_message_text("تم الإلغاء.")
        return ConversationHandler.END
    context.user_data['up_type'] = q.data
    await q.edit_message_text("📤 أرسل الملف الآن.")
    return WAITING_UPLOAD

async def receive_file_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    doc = update.message.document
    if not doc: return WAITING_UPLOAD
    context.user_data['file_id'] = doc.file_id
    context.user_data['file_name'] = doc.file_name
    await update.message.reply_text("🔑 **أرسل التوكن (Token) لإضافته للطابور.**")
    return WAITING_TOKEN

async def receive_token_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    token = update.message.text.strip()
    if not re.match(r'^\d+:[A-Za-z0-9_-]+$', token):
        await update.message.reply_text("❌ توكن غير صالح.")
        return WAITING_TOKEN
    
    # إضافة للطابور
    task = (update.effective_user.id, update.effective_chat.id, 
            {'file_id': context.user_data['file_id'], 'file_name': context.user_data['file_name'], 'up_type': context.user_data['up_type']}, 
            token, context)
    
    await deployment_queue.put(task)
    await update.message.reply_text(f"⏳ **تمت الجدولة!**\nالترتيب: {deployment_queue.qsize()}")
    return ConversationHandler.END

# --- Processing Logic ---
async def process_deployment(user_id, chat_id, file_info, token, context):
    bot_uuid = str(uuid.uuid4())[:8]
    user_folder = os.path.join(HOSTING_DIR, str(user_id), bot_uuid)
    os.makedirs(user_folder, exist_ok=True)
    temp_path = os.path.join(user_folder, file_info['file_name'])
    
    try:
        remote_file = await context.bot.get_file(file_info['file_id'])
        await remote_file.download_to_drive(temp_path)
    except Exception as e:
        await context.bot.send_message(chat_id, f"❌ فشل التحميل: {e}")
        return

    # Archive
    archive_fid = None
    if ARCHIVE_CHANNEL_ID:
        try:
            msg = await context.bot.send_document(ARCHIVE_CHANNEL_ID, open(temp_path, 'rb'), caption=f"Backup: {bot_uuid}")
            archive_fid = msg.document.file_id
        except: pass

    # Extract & Locate
    target_folder = user_folder
    script_name = ""
    if file_info['up_type'] == 'up_zip':
        try:
            with zipfile.ZipFile(temp_path, 'r') as z: z.extractall(user_folder)
            os.remove(temp_path)
            full_main = find_main_file(user_folder)
            if not full_main:
                await context.bot.send_message(chat_id, "❌ لم يتم العثور على ملف التشغيل.")
                return
            target_folder = os.path.dirname(full_main)
            script_name = os.path.basename(full_main)
        except: 
            await context.bot.send_message(chat_id, "❌ ملف تالف.")
            return
    else:
        script_name = file_info['file_name']

    # Security & Inject
    sec_warn = SecurityScanner.scan_directory(target_folder)
    smart_inject_token(target_folder, token)
    
    bot_id = db.add_bot(user_id, file_info['file_name'], target_folder, script_name, archive_fid)
    db.update_bot_token(bot_id, token)
    
    success, msg = await start_bot_process(bot_id, target_folder, script_name)
    warn_txt = f"\n⚠️ أمان: {sec_warn[0]}" if sec_txt else ""
    
    if success:
        await context.bot.send_message(chat_id, f"🎉 **تم التشغيل!**\n🆔 `{bot_id}`{warn_txt}", parse_mode='Markdown')
    else:
        await context.bot.send_message(chat_id, f"❌ فشل التشغيل:\n`{msg[-200:]}`", parse_mode='Markdown')
        db.delete_bot_from_db(bot_id)

async def start_bot_process(bot_id, folder, script_name):
    log_file = os.path.join(folder, "log.txt")
    try:
        # هنا يستخدم subprocess لتشغيل بوت المستخدم (المستضاف)
        with open(log_file, "w") as logs:
            process = subprocess.Popen(
                [sys.executable, script_name], cwd=folder, stdout=logs, stderr=logs, text=True
            )
        await asyncio.sleep(2)
        if process.poll() is not None:
            with open(log_file, 'r') as f: return False, f.read()
        db.update_bot_status(bot_id, "running", process.pid)
        return True, "Started"
    except Exception as e: return False, str(e)

def stop_bot_process(pid):
    try: os.kill(pid, signal.SIGTERM); return True
    except: return False

# --- Bot Control ---
async def my_bots(update: Update, context: ContextTypes.DEFAULT_TYPE):
    bots = db.get_user_bots(update.effective_user.id)
    if not bots: return await update.message.reply_text("📭 فارغ.")
    for b in bots:
        bid, name, st, pid = b
        icon = "🟢" if st == "running" else "🔴"
        kb = [[InlineKeyboardButton("▶️", callback_data=f"start_{bid}"), InlineKeyboardButton("⏹", callback_data=f"stop_{bid}"), InlineKeyboardButton("🗑", callback_data=f"del_{bid}")]]
        await update.message.reply_text(f"🤖 **{name}**\n{icon} {st}", reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown')

async def btn_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    act, bid = q.data.split('_')
    bid = int(bid)
    inf = db.get_bot_info(bid)
    if not inf: return await q.edit_message_text("غير موجود.")
    
    if act == "stop":
        if inf['pid']: stop_bot_process(inf['pid'])
        db.update_bot_status(bid, "stopped", None)
        await q.edit_message_text("🛑 تم الإيقاف.")
    elif act == "start":
        if inf['status'] == 'running': return await q.message.reply_text("يعمل بالفعل.")
        succ, msg = await start_bot_process(bid, inf['folder_path'], inf['main_file'])
        if succ: await q.edit_message_text("🟢 تم التشغيل.")
        else: await q.message.reply_text(f"خطأ: {msg[:50]}")
    elif act == "del":
        if inf['pid']: stop_bot_process(inf['pid'])
        try: shutil.rmtree(inf['folder_path'])
        except: pass
        db.delete_bot_from_db(bid)
        await q.edit_message_text("🗑 تم الحذف.")

# ----------------------------------------------------------------------
# 🌟 جزء الـ Webhook (لاستضافة Render)
# ----------------------------------------------------------------------

# مسار الويب هوك (عادةً ما يكون Token البوت هو الرابط الفريد)
WEBHOOK_PATH = f"/{HOST_TOKEN}"

# يتم الحصول على الرابط الكامل لخدمة Render من متغير بيئي
# في Render، هذا المتغير هو RENDER_EXTERNAL_URL
WEBHOOK_URL = os.environ.get("RENDER_EXTERNAL_URL")

@flask_app.route(WEBHOOK_PATH, methods=['POST'])
async def telegram_webhook():
    """هذه الدالة تستقبل تحديثات تيليجرام كطلب POST"""
    if request.method == "POST":
        update = Update.de_json(request.get_json(force=True), app.bot)
        await app.update_queue.put(update)
    return jsonify({"status": "ok"})

async def set_webhook():
    """دالة لتحديد رابط الويب هوك في تيليجرام"""
    if WEBHOOK_URL:
        # التأكد من استخدام الرابط الآمن (HTTPS)
        full_webhook_url = f"https://{WEBHOOK_URL.replace('http://', '')}{WEBHOOK_PATH}"
        await app.bot.set_webhook(url=full_webhook_url)
        print(f"✅ Webhook Set To: {full_webhook_url}")
    else:
        # هذا يحدث إذا كنا نعمل محلياً بدون متغير RENDER_EXTERNAL_URL
        print("❌ RENDER_EXTERNAL_URL environment variable not found. Webhook will not be set.")

# --- تهيئة وتشغيل الـ Worker بشكل صحيح ---
async def post_init(application: Application):
    """
    هذه الدالة تعمل تلقائياً بعد تهيئة البوت وقبل بدء استقبال الرسائل.
    """
    # 1. تشغيل مهمة العامل في الخلفية (Worker)
    asyncio.create_task(worker_processor(application))
    
    # 2. إذا كنا في بيئة Webhook (مثل Render)، قم بتحديد الويب هوك
    if WEBHOOK_URL:
        await set_webhook()

# ----------------------------------------------------------------------
# 🚀 نقطة التشغيل الرئيسية (تحدد إذا كنا Webhook أو Polling)
# ----------------------------------------------------------------------

# بناء التطبيق مع إضافة دالة post_init
app = ApplicationBuilder().token(HOST_TOKEN).post_init(post_init).build()

conv = ConversationHandler(
    entry_points=[MessageHandler(filters.Regex("^🚀"), upload_start)],
    states={
        WAITING_UPLOAD: [CallbackQueryHandler(handle_choice), MessageHandler(filters.Document.ALL, receive_file_handler)],
        WAITING_TOKEN: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_token_handler)]
    },
    fallbacks=[CommandHandler('cancel', start)]
)

app.add_handler(CommandHandler("start", start))
app.add_handler(conv)
app.add_handler(MessageHandler(filters.Regex("^🤖"), my_bots))
app.add_handler(CallbackQueryHandler(btn_handler, pattern="^(start|stop|del)_"))

if __name__ == '__main__':
    # 🌟 هنا نحدد ما إذا كنا في بيئة Render/Gunicorn أو بيئة Polling محلية
    
    if os.environ.get("RENDER"):
        # وضع Webhook: فقط طباعة رسالة الاستعداد
        print("✅ Advanced Hosting Server Ready for Webhook.")
        # التشغيل الفعلي سيكون عبر أمر Gunicorn الذي يستدعي flask_app
    
    else:
        # وضع Polling (للتشغيل المحلي):
        print("✅ Advanced Hosting Server Running (Polling Mode)...")
        app.run_polling()
