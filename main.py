import logging
import sqlite3
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
    ConversationHandler
)
import uuid # لتوليد معرفات فريدة لطلبات الشحن

# قم بتعيين توكن البوت الخاص بك هنا
BOT_TOKEN = "7965400501:AAEeu2jYnytPkj4_h8tJMy4BWAUwrsyCwo8"

# معرف الشات للمجموعة المسؤولة (ضع ID مجموعتك هنا)
# تأكد أن البوت هو مسؤول في هذه المجموعة ولديه صلاحية إرسال الرسائل
ADMIN_GROUP_CHAT_ID = -1002339354477 # مثال: -1001234567890 (معرفات المجموعات تبدأ بـ -100)

# IMPORTANT: Replace with your actual Telegram User ID or a list of admin IDs.
# You can get your user ID by forwarding any message to @userinfobot
ADMIN_USER_IDS = [7418035011] # Example: [123456789, 987654321]

# قم بتعيين اسم ملف قاعدة البيانات
DATABASE_NAME = 'bot_data.db'

# قم بتمكين التسجيل لتصحيح الأخطاء
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- تعريف حالات المحادثة لشحن الرصيد ---
ASKING_AMOUNT, ASKING_DETAILS, ASKING_FF_ID, ASKING_PUBG_ID, ASKING_BROADCAST_MESSAGE = range(5)

# --- تخزين مؤقت لطلبات الشحن بانتظار الموافقة ---
# المفتاح: request_id (سلسلة فريدة)
# القيمة: dict{'user_id': int, 'username': str, 'first_name': str, 'amount': int, 'details_message': str, 'photo_file_id': str, 'type': str, 'game_id': str, 'product_key': str}
pending_charge_requests = {}

# --- أسعار جواهر Free Fire (نقاط) ---
# يمكنك تعديل هذه الأسعار حسب حاجتك
FREEFIRE_PRICES = {
    '110': 9300,
    '341': 27800,
    '572': 46500,
    '1166': 88000,
    '2398': 190000,
    '5000': 336000,
    'weekly': 20000,  # سعر العضوية الأسبوعية
    'monthly': 59000, # سعر العضوية الشهرية
}

# --- أسعار شدات PUBG (نقاط) ---
# يمكنك تعديل هذه الأسعار حسب حاجتك
PUBG_PRICES = {
    '60UC': 9000,
    '325UC': 45000,
    '660UC': 89000,
    '1800UC': 218000,
}

# --- إعدادات المستويات ---
GOLD_LEVEL_THRESHOLD = 200000 # الرصيد المطلوب للوصول للمستوى الذهبي
GOLD_DISCOUNT_PERCENTAGE = 10 # نسبة الخصم للمستخدم الذهبي (مثال: 10%)

# --- وظائف قاعدة البيانات ---

def init_db():
    """تهيئة قاعدة البيانات وإنشاء الجداول إذا لم تكن موجودة."""
    conn = sqlite3.connect(DATABASE_NAME)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            balance INTEGER DEFAULT 0
        )
    ''')
    conn.commit()
    conn.close()
    logger.info("تم تهيئة قاعدة البيانات بنجاح.")

def get_user_balance_and_level(user_id: int) -> tuple[int, str]:
    """الحصول على رصيد المستخدم ومستواه من قاعدة البيانات."""
    conn = sqlite3.connect(DATABASE_NAME)
    cursor = conn.cursor()
    cursor.execute('SELECT balance FROM users WHERE user_id = ?', (user_id,))
    result = cursor.fetchone()
    conn.close()
    balance = result[0] if result else 0
    level = "برونزي 🥉"
    if balance >= GOLD_LEVEL_THRESHOLD:
        level = "ذهبي 🥇"
    return balance, level

def update_user_balance(user_id: int, amount: int, username: str, first_name: str) -> None:
    """تحديث رصيد المستخدم في قاعدة البيانات."""
    conn = sqlite3.connect(DATABASE_NAME)
    cursor = conn.cursor()
    # التحقق مما إذا كان المستخدم موجودًا، إذا لم يكن، قم بإضافته أولاً
    cursor.execute('INSERT OR IGNORE INTO users (user_id, username, first_name, balance) VALUES (?, ?, ?, ?)',
                    (user_id, username, first_name, 0))
    
    cursor.execute('UPDATE users SET balance = balance + ? WHERE user_id = ?', (amount, user_id))
    conn.commit()
    conn.close()
    logger.info(f"تم تحديث رصيد المستخدم {user_id} بمقدار {amount}.")

def deduct_user_balance(user_id: int, amount: int) -> bool:
    """خصم مبلغ من رصيد المستخدم. يعود True إذا كان الخصم ناجحًا، False بخلاف ذلك."""
    conn = sqlite3.connect(DATABASE_NAME)
    cursor = conn.cursor()
    cursor.execute('SELECT balance FROM users WHERE user_id = ?', (user_id,))
    current_balance = cursor.fetchone()
    
    if current_balance and current_balance[0] >= amount:
        cursor.execute('UPDATE users SET balance = balance - ? WHERE user_id = ?', (amount, user_id))
        conn.commit()
        conn.close()
        logger.info(f"تم خصم {amount} من رصيد المستخدم {user_id}.")
        return True
    conn.close()
    logger.warning(f"فشل خصم {amount} من رصيد المستخدم {user_id}. الرصيد غير كافٍ.")
    return False

# --- وظائف معالجة الأوامر ---

async def start(update: Update, context) -> None:
    """يرسل رسالة ترحيب مع الأزرار الرئيسية."""
    user_id = update.message.from_user.id
    username = update.message.from_user.username
    first_name = update.message.from_user.first_name
    
    # تأكد من وجود المستخدم في قاعدة البيانات عند بدء تشغيل البوت
    conn = sqlite3.connect(DATABASE_NAME)
    cursor = conn.cursor()
    cursor.execute('INSERT OR IGNORE INTO users (user_id, username, first_name) VALUES (?, ?, ?)',
                    (user_id, username, first_name))
    conn.commit()
    conn.close()

    keyboard = [
        [InlineKeyboardButton("👾 شحن لعبة", callback_data='charge_game'),
         InlineKeyboardButton("💰 شحن الرصيد", callback_data='charge_balance')],
        [InlineKeyboardButton("👤 حسابي", callback_data='my_account'),
         InlineKeyboardButton("❓ الدعم", callback_data='support')]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text('♕           اخــتــر مــن أحــد الأوامــر الــتاليــــه           ♕', reply_markup=reply_markup)

# --- وظائف معالجة ضغط الأزرار (Callback Queries) ---

async def button(update: Update, context) -> int:
    """يعالج ضغط الأزرار الداخلية."""
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    current_balance, user_level = get_user_balance_and_level(user_id)

    if query.data == 'charge_game':
        game_keyboard = [
            [InlineKeyboardButton("Free Fire 💎", callback_data='freefire')],
            [InlineKeyboardButton("PUBG ⚜", callback_data='pubg')]
        ]
        game_reply_markup = InlineKeyboardMarkup(game_keyboard)
        await query.edit_message_text(text="اختر اللعبة التي ترغب في شحنها:🕹", reply_markup=game_reply_markup)
        return ConversationHandler.END # End current conversation if any, wait for new selection

    elif query.data == 'freefire':
        # Add Free Fire diamond and membership options with prices
        freefire_keyboard = [
            [InlineKeyboardButton(f"110 💎", callback_data='ff_110')],
            [InlineKeyboardButton(f"341 💎", callback_data='ff_341')],
            [InlineKeyboardButton(f"572 💎", callback_data='ff_572')],
            [InlineKeyboardButton(f"1166 💎", callback_data='ff_1166')],
            [InlineKeyboardButton(f"2398 💎", callback_data='ff_2398')],
            [InlineKeyboardButton(f"5000 💎", callback_data='ff_5000')],
            [InlineKeyboardButton(f"عضوية أسبوعية", callback_data='ff_weekly')],
            [InlineKeyboardButton(f"عضوية شهرية", callback_data='ff_monthly')],
            [InlineKeyboardButton("🔙 الرجوع", callback_data='charge_game')] # Back button
        ]
        freefire_reply_markup = InlineKeyboardMarkup(freefire_keyboard)
        await query.edit_message_text(text='''▪️ اللعبة: Free Fire

اختر حزمة:''', reply_markup=freefire_reply_markup)
        return ConversationHandler.END

    elif query.data.startswith('ff_'):
        product_key = query.data.split('_')[1]
        
        # Check if the product key exists in our prices
        if product_key not in FREEFIRE_PRICES:
            await query.edit_message_text("عفواً، المنتج المطلوب غير متوفر حالياً.")
            return ConversationHandler.END

        base_price = FREEFIRE_PRICES[product_key]
        
        # Apply discount for Gold level users
        if user_level == "ذهبي 🥇":
            price = int(base_price * (1 - GOLD_DISCOUNT_PERCENTAGE / 100))
        else:
            price = base_price

        # Check user's balance
        if current_balance < price:
            await query.edit_message_text(
                f"رصيدك الحالي *{current_balance} ل.س* وهو غير كافٍ لشراء {product_key} (الذي يتطلب {price} ل.س).\n"
                f"يرجى شحن رصيدك أولاً من قسم *شحن الرصيد*."
                , parse_mode='Markdown')
            return ConversationHandler.END # End the conversation here
        else:
            # User has enough balance, proceed to ask for FF ID
            context.user_data['selected_game'] = 'freefire'
            context.user_data['selected_product_key'] = product_key
            context.user_data['selected_price'] = price
            
            await query.edit_message_text(
                f''' *اللعبة*: Free Fire
 *الفئة*: {product_key}
*السيرفر* : ⚡
 *السعر بالليرة السورية*: {price} ل.س
أدخل الـID اللاعب في *Free Fire*: '''
                , parse_mode='Markdown')
            return ASKING_FF_ID # Transition to a new state to get FF ID
            
    elif query.data == 'pubg':
        # Add PUBG UC options with prices
        pubg_keyboard = [
            [InlineKeyboardButton(f"60 UC", callback_data='pubg_60UC')],
            [InlineKeyboardButton(f"325 UC", callback_data='pubg_325UC')],
            [InlineKeyboardButton(f"660 UC", callback_data='pubg_660UC')],
            [InlineKeyboardButton(f"1800 UC", callback_data='pubg_1800UC')],
            [InlineKeyboardButton("🔙 الرجوع", callback_data='charge_game')] # Back button
        ]
        pubg_reply_markup = InlineKeyboardMarkup(pubg_keyboard)
        await query.edit_message_text(text='''▪️ اللعبة: PUBG Mobile

اختر حزمة:''', reply_markup=pubg_reply_markup)
        return ConversationHandler.END

    elif query.data.startswith('pubg_'):
        product_key = query.data.split('_')[1]
        
        if product_key not in PUBG_PRICES:
            await query.edit_message_text("عفواً، المنتج المطلوب غير متوفر حالياً.")
            return ConversationHandler.END

        base_price = PUBG_PRICES[product_key]
        
        # Apply discount for Gold level users
        if user_level == "ذهبي 🥇":
            price = int(base_price * (1 - GOLD_DISCOUNT_PERCENTAGE / 100))
        else:
            price = base_price

        if current_balance < price:
            await query.edit_message_text(
                f"رصيدك الحالي *{current_balance} ل.س* وهو غير كافٍ لشراء {product_key} (الذي يتطلب {price} ل.س).\n"
                f"يرجى شحن رصيدك أولاً من قسم *شحن الرصيد*."
                , parse_mode='Markdown')
            return ConversationHandler.END
        else:
            context.user_data['selected_game'] = 'pubg'
            context.user_data['selected_product_key'] = product_key
            context.user_data['selected_price'] = price

            await query.edit_message_text(
                f''' *اللعبة*: PUBG Mobile
 *الفئة*: {product_key}
*السيرفر* : ⚡
 *السعر بالليرة السورية*: {price} ل.س
أدخل الـID اللاعب في *PUBG Mobile*: '''
                , parse_mode='Markdown')
            return ASKING_PUBG_ID

    elif query.data == 'charge_balance':
        # Show options for charging balance, including "syr cash auto"
        charge_balance_keyboard = [
            [InlineKeyboardButton("SYR cash ( auto)", callback_data='syr_cash_auto')], # Renamed for clarity
            [InlineKeyboardButton("🔙 الرجوع", callback_data='start_menu')] # Back button
        ]
        charge_balance_reply_markup = InlineKeyboardMarkup(charge_balance_keyboard)
        await query.edit_message_text(
            text="اختر طريقة الإيداع المطلوبة:", reply_markup=charge_balance_reply_markup
        )
        # This is where the crucial change for 'syr_cash_auto' lies.
        # We need to explicitly return a state that the ConversationHandler can transition to.
        # In this case, by letting the `ConversationHandler` handle the 'syr_cash_auto' callback
        # in its states, we avoid ending the conversation here and allow it to proceed.
        return ASKING_AMOUNT # The next step for syr_cash_auto is to ask for amount

    elif query.data == 'syr_cash_auto':
        await query.edit_message_text(
            text=''' يرجى استخدام 🛑( التحويل اليدوي )🛑حصرااا
في حال تم تحويل رصيد ( وحدات ) لن نعوض..
تم التحذير ❌:

                     26649300

علماً أنَّ:
1 credit = 15,000 ل.س


--------------------------

قم بأدخال المبلغ : '''

        )
        return ASKING_AMOUNT # Proceed to ask for amount
        
    elif query.data == 'start_menu':
        # Go back to the main menu
        await start(update, context) # Re-use the start function to display main menu
        return ConversationHandler.END

    elif query.data == 'support':
        await query.edit_message_text(text="يمكنك التواصل مع الدعم عبر [@Naeem13873].") # Updated placeholder
        return ConversationHandler.END # End the conversation after displaying support info
    elif query.data == 'my_account':
        username = query.from_user.username
        first_name = query.from_user.first_name
        
        balance, level = get_user_balance_and_level(user_id)
        
        await query.edit_message_text(f'''
👤 *حسابي*
اسم المستخدم: @{username or 'غير متاح'}
الأيدي: `{user_id}`
رصيدك الحالي: *{balance} ل.س*
المستوى: *{level}*
''', parse_mode='Markdown')
        return ConversationHandler.END # End the conversation after displaying account info
        
    return ConversationHandler.END # إنهاء المحادثة إذا لم تكن لزر الشحن

# --- وظائف معالجة مراحل شحن الرصيد ---

async def ask_amount(update: Update, context) -> int:
    """يستقبل المبلغ من المستخدم ويطلب التفاصيل الأخرى."""
    user_input = update.message.text
    try:
        amount = int(user_input)
        if amount <= 0:
            await update.message.reply_text("المبلغ يجب أن يكون رقماً موجباً. يرجى إدخال المبلغ الصحيح.")
            return ASKING_AMOUNT
        
        context.user_data['charge_amount'] = amount
        await update.message.reply_text(
            f"تم استلام المبلغ: {amount}.\n"
            "قم بأرسال رقم عملية التحويل:"
        )
        return ASKING_DETAILS
    except ValueError:
        await update.message.reply_text("هذا ليس رقماً صالحاً. يرجى إدخال المبلغ كـ *رقم فقط*.")
        return ASKING_AMOUNT

async def ask_details(update: Update, context) -> int:
    """يستقبل تفاصيل الشحن (نص أو صورة) ويرسلها للمسؤولين."""
    details_message = update.message.caption if update.message.photo else update.message.text
    photo_file_id = update.message.photo[-1].file_id if update.message.photo else None
    
    user_id = update.message.from_user.id
    username = update.message.from_user.username
    first_name = update.message.from_user.first_name
    amount = context.user_data.get('charge_amount', 0)

    if not details_message and not photo_file_id:
        await update.message.reply_text(
            "يرجى إرسال تفاصيل الشحن (نص أو نص مع صورة إيصال)."
        )
        return ASKING_DETAILS

    request_id = str(uuid.uuid4())
    
    pending_charge_requests[request_id] = {
        'user_id': user_id,
        'username': username,
        'first_name': first_name,
        'amount': amount,
        'details_message': details_message,
        'photo_file_id': photo_file_id,
        'type': 'balance_charge' # Indicate type of request
    }

    admin_keyboard = [
        [
            InlineKeyboardButton("✅ موافقة", callback_data=f'approve_balance_{request_id}'), # Changed callback_data
            InlineKeyboardButton("❌ رفض", callback_data=f'reject_balance_{request_id}')    # Changed callback_data
        ]
    ]
    admin_reply_markup = InlineKeyboardMarkup(admin_keyboard)

    admin_message_text = (
        f"⚡️ *طلب شحن رصيد جديد من المستخدم:*\n"
        f"ID: `{user_id}`\n"
        f"اسم المستخدم: @{username or 'غير متاح'}\n"
        f"الاسم: {first_name}\n"
        f"المبلغ المطلوب: *{amount}*\n\n"
        f"*تفاصيل الشحن:*\n`{details_message or 'لا توجد تفاصيل نصية'}`\n\n"
        f"يرجى مراجعة الطلب والموافقة أو الرفض."
    )

    if photo_file_id:
        await context.bot.send_photo(
            chat_id=ADMIN_GROUP_CHAT_ID,
            photo=photo_file_id,
            caption=admin_message_text,
            reply_markup=admin_reply_markup,
            parse_mode='Markdown'
        )
    else:
        await context.bot.send_message(
            chat_id=ADMIN_GROUP_CHAT_ID,
            text=admin_message_text,
            reply_markup=admin_reply_markup,
            parse_mode='Markdown'
        )
    
    await update.message.reply_text(
        "جاري التحقق من العملية، يرجى الانتظار..."
    )
    
    if 'charge_amount' in context.user_data:
        del context.user_data['charge_amount']
    
    return ConversationHandler.END

async def ask_ff_id(update: Update, context) -> int:
    """يستقبل الأيدي الخاص بـ Free Fire من المستخدم."""
    ff_id = update.message.text
    user_id = update.message.from_user.id
    username = update.message.from_user.username
    first_name = update.message.from_user.first_name
    
    product_key = context.user_data.get('selected_product_key')
    price = context.user_data.get('selected_price') # This is the price AFTER potential discount
    selected_game = context.user_data.get('selected_game') # 'freefire'

    if not product_key or not price:
        await update.message.reply_text("حدث خطأ ما. يرجى البدء من جديد من القائمة الرئيسية.")
        return ConversationHandler.END

    # Check balance again to prevent double spending or balance change
    current_balance, _ = get_user_balance_and_level(user_id) # Get balance to re-check
    if current_balance < price:
        await update.message.reply_text(
            f"عفواً، رصيدك تغير وأصبح *{current_balance} نقطة* وهو غير كافٍ. يرجاء شحن رصيدك أو اختيار منتج آخر."
        , parse_mode='Markdown')
        # Clean up user data immediately if balance is insufficient
        for key in ['selected_product_key', 'selected_price', 'selected_game']:
            if key in context.user_data:
                del context.user_data[key]
        return ConversationHandler.END

    # Deduct balance
    if deduct_user_balance(user_id, price):
        request_id = str(uuid.uuid4())
        pending_charge_requests[request_id] = {
            'user_id': user_id,
            'username': username,
            'first_name': first_name,
            'amount': price, # Store price as amount for game charges
            'product_key': product_key,
            'game_id': ff_id,
            'type': 'freefire_charge' # Indicate type of request
        }

        admin_keyboard = [
            [
                InlineKeyboardButton("✅ تم الشحن", callback_data=f'charge_complete_ff_{request_id}'),
                InlineKeyboardButton("❌ مشكلة بالشحن", callback_data=f'charge_issue_ff_{request_id}')
            ]
        ]
        admin_reply_markup = InlineKeyboardMarkup(admin_keyboard)

        admin_message_text = (
            f"💎 *طلب شحن Free Fire جديد:*\n"
            f"ID: `{user_id}`\n"
            f"اسم المستخدم: @{username or 'غير متاح'}\n"
            f"الاسم: {first_name}\n"
            f"المنتج: *{product_key}* (بتكلفة *{price} ل.س*)\n"
            f"رصيد المستخدم بعد الخصم: *{get_user_balance_and_level(user_id)[0]} ل.س*\n" # Display updated balance
            f"أيدي Free Fire: `{ff_id}`\n\n"
            f"يرجى معالجة الطلب يدوياً وتأكيد إتمام الشحن."
        )
        
        await context.bot.send_message(
            chat_id=ADMIN_GROUP_CHAT_ID,
            text=admin_message_text,
            reply_markup=admin_reply_markup, # Add buttons to admin message
            parse_mode='Markdown'
        )

        await update.message.reply_text(
            f"تم خصم *{price} ل.س* من رصيدك بنجاح.\n"
            f"تم إرسال طلب شحن *{product_key}* لأيدي Free Fire الخاص بك: `{ff_id}`.\n"
            f"سيتم معالجته قريباً!"
        , parse_mode='Markdown')
        logger.info(f"تم إنشاء طلب Free Fire لـ ID: {user_id}, المنتج: {product_key}, الأيدي: {ff_id}")
    else:
        await update.message.reply_text(
            "عفواً، لم نتمكن من خصم المبلغ. قد يكون رصيدك غير كافٍ. يرجى التحقق من حسابك."
        )

    # Clean up user data
    for key in ['selected_product_key', 'selected_price', 'selected_game']:
        if key in context.user_data:
            del context.user_data[key]

    return ConversationHandler.END

async def ask_pubg_id(update: Update, context) -> int:
    """يستقبل الأيدي الخاص بـ PUBG Mobile من المستخدم."""
    pubg_id = update.message.text
    user_id = update.message.from_user.id
    username = update.message.from_user.username
    first_name = update.message.from_user.first_name
    
    product_key = context.user_data.get('selected_product_key')
    price = context.user_data.get('selected_price') # This is the price AFTER potential discount
    selected_game = context.user_data.get('selected_game') # 'pubg'

    if not product_key or not price:
        await update.message.reply_text("حدث خطأ ما. يرجى البدء من جديد من القائمة الرئيسية.")
        return ConversationHandler.END

    current_balance, _ = get_user_balance_and_level(user_id) # Get balance to re-check
    if current_balance < price:
        await update.message.reply_text(
            f"عفواً، رصيدك تغير وأصبح *{current_balance} نقطة* وهو غير كافٍ. يرجى شحن رصيدك أو اختيار منتج آخر."
        , parse_mode='Markdown')
        # Clean up user data immediately if balance is insufficient
        for key in ['selected_product_key', 'selected_price', 'selected_game']:
            if key in context.user_data:
                del context.user_data[key]
        return ConversationHandler.END

    if deduct_user_balance(user_id, price):
        request_id = str(uuid.uuid4())
        pending_charge_requests[request_id] = {
            'user_id': user_id,
            'username': username,
            'first_name': first_name,
            'amount': price, # Store price as amount for game charges
            'product_key': product_key,
            'game_id': pubg_id,
            'type': 'pubg_charge' # Indicate type of request
        }

        admin_keyboard = [
            [
                InlineKeyboardButton("✅ تم الشحن", callback_data=f'charge_complete_pubg_{request_id}'),
                InlineKeyboardButton("❌ مشكلة بالشحن", callback_data=f'charge_issue_pubg_{request_id}')
            ]
        ]
        admin_reply_markup = InlineKeyboardMarkup(admin_keyboard)
        
        admin_message_text = (
            f"⚜️ *طلب شحن PUBG Mobile جديد:*\n"
            f"ID: `{user_id}`\n"
            f"اسم المستخدم: @{username or 'غير متاح'}\n"
            f"الاسم: {first_name}\n"
            f"المنتج: *{product_key}* (بتكلفة *{amount} ل.س*)\n"
            f"رصيد المستخدم بعد الخصم: *{get_user_balance_and_level(user_id)[0]} ل.س*\n" # Display updated balance
            f"أيدي PUBG Mobile: `{pubg_id}`\n\n"
            f"يرجى معالجة الطلب يدوياً وتأكيد إتمام الشحن."
        )
        
        await context.bot.send_message(
            chat_id=ADMIN_GROUP_CHAT_ID,
            text=admin_message_text,
            reply_markup=admin_reply_markup, # Add buttons to admin message
            parse_mode='Markdown'
        )

        await update.message.reply_text(
            f"تم خصم *{price} ل.س* من رصيدك بنجاح.\n"
            f"تم إرسال طلب شحن *{product_key}* لأيدي PUBG Mobile الخاص بك: `{pubg_id}`.\n"
            f"سيتم معالجته قريباً!"
        , parse_mode='Markdown')
        logger.info(f"تم إنشاء طلب PUBG Mobile لـ ID: {user_id}, المنتج: {product_key}, الأيدي: {pubg_id}")
    else:
        await update.message.reply_text(
            "عفواً، لم نتمكن من خصم المبلغ. قد يكون رصيدك غير كافٍ. يرجى التحقق من حسابك."
        )

    # Clean up user data
    for key in ['selected_product_key', 'selected_price', 'selected_game']:
        if key in context.user_data:
            del context.user_data[key]

    return ConversationHandler.END

async def cancel_charge(update: Update, context) -> int:
    """يلغي عملية شحن الرصيد أو شحن Free Fire أو PUBG."""
    await update.message.reply_text('تم إلغاء العملية.')
    # Clean up all relevant user data on cancel
    for key in ['charge_amount', 'selected_product_key', 'selected_price', 'selected_game']:
        if key in context.user_data:
            del context.user_data[key]
    return ConversationHandler.END

# --- وظائف معالجة أزرار الموافقة/الرفض للمسؤول ---
async def admin_approval(update: Update, context) -> None:
    """يعالج ضغط أزرار الموافقة/الرفض من قبل المسؤولين."""
    query = update.callback_query
    await query.answer()

    # تحقق مما إذا كان المستخدم الذي ضغط الزر هو مسؤول
    if query.message.chat.id != ADMIN_GROUP_CHAT_ID and query.from_user.id not in ADMIN_USER_IDS:
        await query.edit_message_text("ليس لديك صلاحية للقيام بهذا الإجراء.")
        return

    # Updated pattern: action_type_request_id (e.g., approve_balance_uuid, charge_complete_ff_uuid)
    # Corrected split logic to correctly extract action, type, and request_id
    parts = query.data.split('_')
    
    action = parts[0] # e.g., 'approve', 'reject', 'charge'
    
    request_id = None
    full_action = None
    request_type = None

    if len(parts) == 3 and (action == 'approve' or action == 'reject') and parts[1] == 'balance':
        request_type = 'balance_charge'
        request_id = parts[2]
        full_action = action
    elif len(parts) == 4 and action == 'charge' and (parts[1] == 'complete' or parts[1] == 'issue'):
        request_type = f"{parts[2]}_charge" # 'ff_charge' or 'pubg_charge'
        request_id = parts[3]
        full_action = f"{action}_{parts[1]}" # 'charge_complete' or 'charge_issue'
    else:
        await query.edit_message_text("عفواً، صيغة طلب غير معروفة.")
        logger.error(f"Failed to parse callback_data: {query.data}")
        return

    if request_id not in pending_charge_requests:
        await query.edit_message_text("عفواً، لم يتم العثور على هذا الطلب أو تمت معالجته بالفعل.")
        return

    request_data = pending_charge_requests.pop(request_id) # إزالة الطلب بعد معالجته

    user_id = request_data['user_id']
    username = request_data['username']
    first_name = request_data['first_name']
    amount = request_data['amount'] # This is the amount for balance charge, or price for game charge
    
    admin_name = query.from_user.first_name

    if request_data['type'] == 'balance_charge': # Handling balance charge requests
        if full_action == 'approve':
            # --- إضافة النقاط إلى رصيد المستخدم في قاعدة البيانات ---
            update_user_balance(user_id, amount, username, first_name)
            
            confirmation_message_to_admin = (
                f"✅ تم الموافقة على طلب الشحن من قبل {admin_name} للمستخدم:\n"
                f"ID: `{user_id}`\n"
                f"اسم المستخدم: @{username or 'غير متاح'}\n"
                f"الاسم: {first_name}\n"
                f"المبلغ: *{amount} ل.س*\n\n"
                f"*تم إضافة الرصيد إلى حساب المستخدم في قاعدة البيانات.*"
            )
            await query.edit_message_text(confirmation_message_to_admin, parse_mode='Markdown')

            await context.bot.send_message(
                chat_id=user_id,
                text=f" تمت الموافقة ✅على طلب الشحن الخاص بك بمبلغ *{amount} ل.س*. تم إضافة الرصيد إلى حسابك."
            , parse_mode='Markdown')
            logger.info(f"تم الموافقة على طلب شحن الرصيد بمبلغ {amount} لـ ID: {user_id} بواسطة {admin_name}")

        elif full_action == 'reject':
            confirmation_message_to_admin = (
                f"❌ تم رفض طلب الشحن من قبل {admin_name} للمستخدم:\n"
                f"ID: `{user_id}`\n"
                f"اسم المستخدم: @{username or 'غير متاح'}\n"
                f"الاسم: {first_name}\n"
                f"المبلغ: *{amount} ل.س*\n\n"
                f"*تم رفض الطلب.*"
            )
            await query.edit_message_text(confirmation_message_to_admin, parse_mode='Markdown')

            await context.bot.send_message(
                chat_id=user_id,
                text="😞 نأسف! لقد تم رفض طلب الشحن الخاص بك. يرجى التحقق من المعلومات أو التواصل مع الدعم."
            )
            logger.info(f"تم رفض طلب الشحن بمبلغ {amount} لـ ID: {user_id} بواسطة {admin_name}")

    elif request_data['type'] in ['freefire_charge', 'pubg_charge']: # Handling game charge requests
        game_name = "Free Fire" if request_data['type'] == 'freefire_charge' else "PUBG Mobile"
        product_key = request_data.get('product_key', 'N/A')
        game_id = request_data.get('game_id', 'N/A')
        
        if full_action == 'charge_complete':
            confirmation_message_to_admin = (
                f"✅ تم تأكيد إتمام شحن {game_name} من قبل {admin_name} للمستخدم:\n"
                f"ID: `{user_id}`\n"
                f"اسم المستخدم: @{username or 'غير متاح'}\n"
                f"الاسم: {first_name}\n"
                f"المنتج: *{product_key}* (بتكلفة *{amount} ل.س*)\n"
                f"أيدي {game_name}: `{game_id}`\n\n"
                f"*تم تأكيد تسليم الشحن بنجاح.*"
            )
            await query.edit_message_text(confirmation_message_to_admin, parse_mode='Markdown')

            await context.bot.send_message(
                chat_id=user_id,
                text=f" 🎉 رائع! تم شحن *{product_key}* بنجاح لأيدي {game_name} الخاص بك: `{game_id}`."
            , parse_mode='Markdown')
            logger.info(f"تم تأكيد شحن {game_name} لـ ID: {user_id}, المنتج: {product_key}، الأيدي: {game_id} بواسطة {admin_name}")

        elif full_action == 'charge_issue':
            # Option to refund balance if there was an issue with the game charge
            update_user_balance(user_id, amount, username, first_name) # Refund the amount
            
            confirmation_message_to_admin = (
                f"⚠️ مشكلة في شحن {game_name} تم تأكيدها من قبل {admin_name} للمستخدم:\n"
                f"ID: `{user_id}`\n"
                f"اسم المستخدم: @{username or 'غير متاح'}\n"
                f"الاسم: {first_name}\n"
                f"المنتج: *{product_key}* (بتكلفة *{amount} ل.س*)\n"
                f"أيدي {game_name}: `{game_id}`\n"
                f"*تم إعادة المبلغ ({amount} ل.س) إلى رصيد المستخدم.*"
            )
            await query.edit_message_text(confirmation_message_to_admin, parse_mode='Markdown')

            await context.bot.send_message(
                chat_id=user_id,
                text=f" 😞 نأسف! حدثت مشكلة في شحن *{product_key}* لأيدي {game_name} الخاص بك: `{game_id}`.\n"
                     f" تم إعادة مبلغ *{amount} ل.س* إلى رصيدك. يرجى التواصل مع الدعم للمساعدة."
            , parse_mode='Markdown')
            logger.warning(f"مشكلة في شحن {game_name} لـ ID: {user_id}, المنتج: {product_key}، الأيدي: {game_id}. تم استرجاع المبلغ {amount} بواسطة {admin_name}")
    else:
        await query.edit_message_text(f"خطأ: نوع طلب غير معروف ({request_data['type']}) للمعالجة.")
        logger.error(f"محاولة معالجة طلب بنوع غير معروف: {request_data['type']}, ID: {request_id}")

# --- Broadcast Functions ---
async def broadcast_command(update: Update, context) -> int:
    """Initiates the broadcast process for admins."""
    user_id = update.effective_user.id
    if user_id not in ADMIN_USER_IDS:
        await update.message.reply_text("عفواً، ليس لديك صلاحية لاستخدام هذا الأمر.")
        return ConversationHandler.END

    await update.message.reply_text("يرجى إرسال الرسالة التي ترغب في بثها لجميع المستخدمين:")
    return ASKING_BROADCAST_MESSAGE

async def send_broadcast_message(update: Update, context) -> int:
    """Sends the broadcast message to all users."""
    broadcast_text = update.message.text
    
    conn = sqlite3.connect(DATABASE_NAME)
    cursor = conn.cursor()
    cursor.execute('SELECT user_id FROM users')
    users = cursor.fetchall()
    conn.close()

    successful_sends = 0
    failed_sends = 0

    for user in users:
        try:
            await context.bot.send_message(chat_id=user[0], text=broadcast_text)
            successful_sends += 1
        except Exception as e:
            logger.error(f"فشل إرسال رسالة البث إلى المستخدم {user[0]}: {e}")
            failed_sends += 1
    
    await update.message.reply_text(
        f"تم إرسال رسالة البث بنجاح إلى {successful_sends} مستخدم.\n"
        f"فشل إرسال الرسالة إلى {failed_sends} مستخدم."
    )
    return ConversationHandler.END

# --- Main function to run the bot ---
def main() -> None:
    """يشغل البوت."""
    init_db() # تهيئة قاعدة البيانات عند بدء تشغيل البوت

    application = Application.builder().token(BOT_TOKEN).build()

    # Conversation handler for charging balance
    charge_balance_conv_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(button, pattern='^charge_balance$')], # Handle 'charge_balance' button press
        states={
            ASKING_AMOUNT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, ask_amount),
                CommandHandler("cancel", cancel_charge)
            ],
            ASKING_DETAILS: [
                MessageHandler(filters.TEXT | filters.PHOTO & ~filters.COMMAND, ask_details),
                CommandHandler("cancel", cancel_charge)
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel_charge), CallbackQueryHandler(button)],
        allow_reentry=True
    )
    application.add_handler(charge_balance_conv_handler)

    # Conversation handler for Free Fire game charge
    ff_charge_conv_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(button, pattern='^ff_')],
        states={
            ASKING_FF_ID: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, ask_ff_id),
                CommandHandler("cancel", cancel_charge)
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel_charge), CallbackQueryHandler(button)],
        allow_reentry=True
    )
    application.add_handler(ff_charge_conv_handler)

    # Conversation handler for PUBG game charge
    pubg_charge_conv_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(button, pattern='^pubg_')],
        states={
            ASKING_PUBG_ID: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, ask_pubg_id),
                CommandHandler("cancel", cancel_charge)
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel_charge), CallbackQueryHandler(button)],
        allow_reentry=True
    )
    application.add_handler(pubg_charge_conv_handler)
    
    # Broadcast conversation handler
    broadcast_conv_handler = ConversationHandler(
        entry_points=[CommandHandler("broadcast", broadcast_command)],
        states={
            ASKING_BROADCAST_MESSAGE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, send_broadcast_message),
                CommandHandler("cancel", cancel_charge)
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel_charge)],
        allow_reentry=True
    )
    application.add_handler(broadcast_conv_handler)

    # Handlers for general commands and button presses
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(button, pattern='^charge_game$'))
    application.add_handler(CallbackQueryHandler(button, pattern='^freefire$'))
    application.add_handler(CallbackQueryHandler(button, pattern='^pubg$'))
    application.add_handler(CallbackQueryHandler(button, pattern='^syr_cash_auto$')) # Handle auto-charge callback
    application.add_handler(CallbackQueryHandler(button, pattern='^start_menu$'))
    application.add_handler(CallbackQueryHandler(button, pattern='^support$'))
    application.add_handler(CallbackQueryHandler(button, pattern='^my_account$'))

    # Handler for admin approval actions (approve/reject balance, complete/issue game charge)
    application.add_handler(CallbackQueryHandler(admin_approval, pattern='^(approve|reject)_balance_.*|charge_(complete|issue)_(ff|pubg)_.*$'))


    # Run the bot until the user presses Ctrl-C
    logger.info("البوت بدأ العمل...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == '__main__':
    main()
