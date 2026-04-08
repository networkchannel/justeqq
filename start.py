#!/usr/bin/env python3
"""
Bot Telegram - Passerelle Canal Privé
Trust score 0-1 + Captcha adaptatif + Rate limiting
"""

import logging
import random
import asyncio
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ChatMember
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from telegram.error import TelegramError
import os
from flask import Flask
import threading

app = Flask(__name__)

@app.route("/")
def home():
    return "200 le bot tourne"

def run_web():
    app.run(host="0.0.0.0", port=8080)

# ─── CONFIG ────────────────────────────────────────────────────────────────────
BOT_TOKEN = os.getenv("BOT_TOKEN")
PRIVATE_CHANNEL_ID = int(os.getenv("PRIVATE_CHANNEL_ID"))
MAX_ATTEMPTS = 3
RATE_LIMIT_MINUTES = 30
INVITE_LINK_EXPIRE_HOURS = 1
# ───────────────────────────────────────────────────────────────────────────────

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

user_state: dict[int, dict] = {}

# ─── DEVINETTES PIÈGES ────────────────────────────────────────────────────────
DEVINETTES = [
    ("Quelle est la couleur du cheval blanc d'Henri IV ?", ["Blanc", "Noir", "Brun", "Gris"], "Blanc"),
    ("Combien de mois de l'année ont 28 jours ?", ["12", "1", "6", "4"], "12"),
    ("Si un coq pond un œuf au sommet d'un toit, de quel côté tombe-t-il ?", ["Les coqs ne pondent pas", "À gauche", "À droite", "Il ne tombe pas"], "Les coqs ne pondent pas"),
    ("Un avion s'écrase à la frontière France-Espagne. Où enterre-t-on les survivants ?", ["On n'enterre pas les survivants", "En France", "En Espagne", "Aux deux endroits"], "On n'enterre pas les survivants"),
    ("Certains mois ont 31 jours, d'autres 30. Combien en ont 28 ?", ["Tous", "1", "6", "4"], "Tous"),
    ("Un fermier a 17 moutons. Tous meurent sauf 9. Combien en reste-t-il ?", ["9", "8", "0", "17"], "9"),
    ("Tu participes à une course et tu dépasses le 2ème. Quelle est ta position ?", ["2ème", "1er", "3ème", "Dernier"], "2ème"),
    ("Si tu as 3 pommes et tu en prends 2, combien en as-tu ?", ["2", "1", "3", "0"], "2"),
    ("Quelle est la nationalité du président de la République française ?", ["Française", "Européenne", "Inconnue", "Ça dépend"], "Française"),
    ("Un électricien et un plombier ont chacun un fils. Ces deux fils sont frères. Comment est-ce possible ?", ["Ils ont la même mère", "C'est impossible", "Ils sont jumeaux", "L'un est adopté"], "Ils ont la même mère"),
]


# ─── TRUST SCORE ───────────────────────────────────────────────────────────────
def compute_trust_score(user_id: int, username: str | None, first_name: str) -> float:
    score = 0.0

    if user_id < 100_000_000:
        score += 0.50
    elif user_id < 500_000_000:
        score += 0.40
    elif user_id < 1_000_000_000:
        score += 0.30
    elif user_id < 5_000_000_000:
        score += 0.15
    else:
        score += 0.05

    if username:
        score += 0.25

    name_len = len(first_name)
    if 3 <= name_len <= 20:
        score += 0.15
    elif name_len > 20:
        score += 0.05

    if not any(c.isdigit() for c in first_name):
        score += 0.10

    return round(min(score, 1.0), 2)


def trust_label(score: float) -> str:
    if score >= 0.8:
        return "🟢 Élevée"
    elif score >= 0.5:
        return "🟡 Moyenne"
    elif score >= 0.3:
        return "🟠 Faible"
    else:
        return "🔴 Très faible"


# ─── CAPTCHA ───────────────────────────────────────────────────────────────────
def get_captcha(trust_score: float) -> dict:
    """
    Score >= 0.4 → addition simple
    Score <  0.4 → devinette piège
    """
    if trust_score >= 0.4:
        a = random.randint(1, 20)
        b = random.randint(1, 20)
        answer = a + b
        fakes = set()
        while len(fakes) < 3:
            d = random.choice([-2, -1, 1, 2, 3])
            c = answer + d
            if c != answer and c > 0:
                fakes.add(c)
        choices = list(fakes) + [answer]
        random.shuffle(choices)
        return {
            "question": f"Combien font *{a} + {b}* ?",
            "choices": [str(c) for c in choices],
            "answer": str(answer),
        }
    else:
        q = random.choice(DEVINETTES)
        choices = q[1].copy()
        random.shuffle(choices)
        return {
            "question": f"🧩 *Question :*\n_{q[0]}_",
            "choices": choices,
            "answer": q[2],
        }


# ─── HELPERS ───────────────────────────────────────────────────────────────────
def get_state(user_id: int) -> dict:
    if user_id not in user_state:
        user_state[user_id] = {
            "attempts": 0,
            "banned_until": None,
            "captcha_answer": None,
            "trust_score": None,
        }
    return user_state[user_id]


async def is_member(bot, user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(chat_id=PRIVATE_CHANNEL_ID, user_id=user_id)
        return member.status in [ChatMember.MEMBER, ChatMember.ADMINISTRATOR, ChatMember.OWNER]
    except TelegramError:
        return False


async def send_captcha(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int):
    state = get_state(user_id)
    trust = state.get("trust_score", 0.5)
    captcha = get_captcha(trust)
    state["captcha_answer"] = captcha["answer"]

    keyboard = [[InlineKeyboardButton(c, callback_data=f"cap_{c}") for c in captcha["choices"]]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    attempt_num = state["attempts"] + 1
    text = (
        f"🔐 *Vérification* — Tentative {attempt_num}/{MAX_ATTEMPTS}\n\n"
        f"{captcha['question']}\n\n"
        f"Clique sur la bonne réponse :"
    )

    if update.callback_query:
        await update.callback_query.message.reply_text(text, parse_mode="Markdown", reply_markup=reply_markup)
    else:
        await update.message.reply_text(text, parse_mode="Markdown", reply_markup=reply_markup)


# ─── HANDLERS ──────────────────────────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = user.id
    state = get_state(user_id)

    logger.info(f"[/start] {user.first_name} ({user_id})")

    if await is_member(context.bot, user_id):
        await update.message.reply_text(
            "✅ *Tu es déjà membre du canal !*\n\nTu as déjà accès, rien à faire 😎",
            parse_mode="Markdown",
        )
        return

    if state["banned_until"] and datetime.now() < state["banned_until"]:
        remaining = state["banned_until"] - datetime.now()
        mins = int(remaining.total_seconds() // 60)
        secs = int(remaining.total_seconds() % 60)
        await update.message.reply_text(
            f"⛔ *Trop de tentatives échouées.*\n\nRéessaie dans *{mins}m {secs}s*.",
            parse_mode="Markdown",
        )
        return

    if state["banned_until"] and datetime.now() >= state["banned_until"]:
        state["attempts"] = 0
        state["banned_until"] = None

    trust = compute_trust_score(user_id, user.username, user.first_name)
    state["trust_score"] = trust
    label = trust_label(trust)
    logger.info(f"[TRUST] {user.first_name} ({user_id}) → {trust} ({label})")

    keyboard = [[InlineKeyboardButton("🚀 Rejoindre le canal", callback_data="join")]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(
        f"👋 Bonjour *{user.first_name}* !\n\n"
        f"Ce bot te permet d'accéder à notre *canal privé exclusif*.\n\n"
        f"🛡️ Indice de confiance : {label} `({trust})`\n\n"
        f"Pour rejoindre, résous une petite vérification 🤖\n\n"
        f"Clique ci-dessous pour commencer :",
        parse_mode="Markdown",
        reply_markup=reply_markup,
    )


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = query.from_user
    user_id = user.id
    state = get_state(user_id)
    data = query.data

    if data == "join":
        if await is_member(context.bot, user_id):
            await query.message.reply_text("✅ Tu es déjà membre du canal !")
            return
        if state["banned_until"] and datetime.now() < state["banned_until"]:
            remaining = state["banned_until"] - datetime.now()
            mins = int(remaining.total_seconds() // 60)
            await query.message.reply_text(f"⛔ Encore bloqué. Réessaie dans *{mins} minutes*.", parse_mode="Markdown")
            return
        await send_captcha(update, context, user_id)
        return

    if data.startswith("cap_"):
        chosen = data[4:]
        correct = state["captcha_answer"]

        if correct is None:
            await query.message.reply_text("⚠️ Session expirée. Tape /start pour recommencer.")
            return

        if chosen == correct:
            state["attempts"] = 0
            state["captcha_answer"] = None
            logger.info(f"[✅ OK] {user.first_name} ({user_id})")

            if await is_member(context.bot, user_id):
                await query.message.reply_text("✅ Bravo ! Mais tu es déjà membre du canal 😄")
                return

            try:
                expire_date = datetime.now() + timedelta(hours=INVITE_LINK_EXPIRE_HOURS)
                invite = await context.bot.create_chat_invite_link(
                    chat_id=PRIVATE_CHANNEL_ID,
                    expire_date=expire_date,
                    member_limit=1,
                )
                logger.info(f"[🔗 LIEN] {user.first_name} ({user_id}) → {invite.invite_link}")
                await query.message.reply_text(
                    f"✅ *Bravo ! Vérification réussie.*\n\n"
                    f"Voici ton lien d'invitation personnel :\n"
                    f"👉 {invite.invite_link}\n\n"
                    f"⚠️ Valable *1 heure* • *1 seule utilisation*\n"
                    f"Ne le partage pas !",
                    parse_mode="Markdown",
                )
            except TelegramError as e:
                logger.error(f"[❌ ERREUR LIEN] {user_id} : {e}")
                await query.message.reply_text(
                    f"❌ *Erreur lors de la génération du lien.*\n\n`{e}`\n\nContacte un administrateur.",
                    parse_mode="Markdown",
                )

        else:
            state["attempts"] += 1
            remaining_attempts = MAX_ATTEMPTS - state["attempts"]
            logger.info(f"[❌ FAIL] {user.first_name} ({user_id}) — {state['attempts']}/{MAX_ATTEMPTS}")

            if state["attempts"] >= MAX_ATTEMPTS:
                state["banned_until"] = datetime.now() + timedelta(minutes=RATE_LIMIT_MINUTES)
                state["captcha_answer"] = None
                logger.info(f"[🚫 BAN] {user.first_name} ({user_id}) banni {RATE_LIMIT_MINUTES}min")
                await query.message.reply_text(
                    f"🚫 *{MAX_ATTEMPTS} tentatives échouées.*\n\n"
                    f"Tu es bloqué pendant *{RATE_LIMIT_MINUTES} minutes*.\n"
                    f"Retape /start quand tu seras prêt.",
                    parse_mode="Markdown",
                )
            else:
                await query.message.reply_text(
                    f"❌ *Mauvaise réponse !*\n\nIl te reste *{remaining_attempts} tentative(s)*.",
                    parse_mode="Markdown",
                )
                await asyncio.sleep(1)
                await send_captcha(update, context, user_id)


async def fallback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Tape /start pour commencer la vérification 👋")


def main():
    # Lancer le serveur web en parallèle
    t = threading.Thread(target=run_web)
    t.start()

    app_bot = Application.builder().token(BOT_TOKEN).build()
    app_bot.add_handler(CommandHandler("start", start))
    app_bot.add_handler(CallbackQueryHandler(button_handler))
    app_bot.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, fallback))

    logger.info("Bot + Web server démarrés ✅")
    app_bot.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
