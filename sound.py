import os, json, edge_tts
from aiogram import Router, Bot
from aiogram.filters import Command
from aiogram.types import (Message, CallbackQuery, FSInputFile,
    InlineKeyboardMarkup, InlineKeyboardButton)

router = Router()
VOICE = "am-ET-AmehaNeural"
PREFS = "sound.json"
os.makedirs("audio", exist_ok=True)
FILE_IDS = {}

ONES = ["", "አንድ", "ሁለት", "ሦስት", "አራት", "አምስት",
        "ስድስት", "ሰባት", "ስምንት", "ዘጠኝ"]
TENS = {2: "ሃያ", 3: "ሠላሳ", 4: "አርባ", 5: "ሃምሳ", 6: "ስድሳ", 7: "ሰባ"}
LETTERS = {"B": "ቢ", "I": "አይ", "N": "ኤን", "G": "ጂ", "O": "ኦ"}

def amharic(n):
    if n == 10: return "አስር"
    if n < 10: return ONES[n]
    if n < 20: return "አስራ " + ONES[n - 10]
    t, o = divmod(n, 10)
    return (TENS[t] + " " + ONES[o]).strip()

def letter(n):
    return "BINGO"[(n - 1) // 15]

def load():
    try:
        with open(PREFS) as f: return json.load(f)
    except Exception: return {}

def is_on(uid):
    return load().get(str(uid), False)

def set_sound(uid, val):
    d = load(); d[str(uid)] = val
    with open(PREFS, "w") as f: json.dump(d, f)

def kb(uid):
    text = "🔊 Sound ON" if is_on(uid) else "🔇 Sound OFF"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=text, callback_data="toggle_sound")]])

@router.message(Command("sound"))
async def sound_cmd(m: Message):
    await m.answer("Amharic number calls (sent to you privately):",
                   reply_markup=kb(m.from_user.id))

@router.callback_query(lambda c: c.data == "toggle_sound")
async def toggle(c: CallbackQuery):
    uid = c.from_user.id
    set_sound(uid, not is_on(uid))
    await c.message.edit_reply_markup(reply_markup=kb(uid))
    await c.answer("Sound ON 🔊" if is_on(uid) else "Sound OFF 🔇")

async def make_audio(n):
    path = f"audio/{n}.mp3"
    if not os.path.exists(path):
        text = f"{LETTERS[letter(n)]}፣ {amharic(n)}"
        await edge_tts.Communicate(text, VOICE).save(path)
    return path

async def send_call_audio(bot: Bot, number, player_ids):
    targets = [u for u in player_ids if is_on(u)]
    if not targets: return
    path = await make_audio(number)
    for uid in targets:
        try:
            src = FILE_IDS.get(number) or FSInputFile(path)
            msg = await bot.send_voice(uid, src,
                caption=f"{letter(number)}{number}")
            FILE_IDS[number] = msg.voice.file_id
        except Exception:
            pass  # player hasn't pressed Start in private chat
