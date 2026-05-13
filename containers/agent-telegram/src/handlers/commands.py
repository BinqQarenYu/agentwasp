import structlog
from telegram import Update
from telegram.ext import ContextTypes

logger = structlog.get_logger()


def _welcome_text(lang: str, user_name: str) -> str:
    """Single, static welcome message localized to the user's Telegram language.

    Static (not LLM-generated) on purpose: an earlier LLM-generated welcome
    was being treated as real instructions by the planner, spawning ghost
    goals that ate into the replan budget. Static also guarantees one
    reply — no duplicates.
    """
    lang = (lang or "en").split("-")[0].lower()
    name_part = f" {user_name}" if user_name else ""

    if lang == "es":
        return (
            f"👋 ¡Hola{name_part}! Soy WASP, tu agente autónomo.\n"
            f"\n"
            f"No solo converso. Planifico y ejecuto tareas reales:\n"
            f"\n"
            f"   ⏰  Recordatorios y tareas programadas\n"
            f"   🔍  Búsqueda web, scraping, monitoreo de páginas\n"
            f"   📸  Capturas de pantalla de cualquier sitio\n"
            f"   📧  Correo (Gmail) — leer, enviar, buscar\n"
            f"   📈  Precios de cripto y alertas\n"
            f"   🛠️   Y 25+ habilidades más — incluso puedo crear nuevas\n"
            f"\n"
            f"🧠 Tengo memoria persistente: recuerdo lo que hablamos y aprendo "
            f"de tu feedback.\n"
            f"\n"
            f"Escribime en cualquier idioma — o /help para ver todos los comandos.\n"
            f"\n"
            f"¿En qué empezamos?"
        )

    if lang == "pt":
        return (
            f"👋 Olá{name_part}! Sou o WASP, seu agente autônomo.\n"
            f"\n"
            f"Não só converso. Planejo e executo tarefas reais:\n"
            f"\n"
            f"   ⏰  Lembretes e tarefas agendadas\n"
            f"   🔍  Busca web, scraping, monitoramento de sites\n"
            f"   📸  Capturas de tela de qualquer página\n"
            f"   📧  Email (Gmail) — ler, enviar, buscar\n"
            f"   📈  Preços de cripto e alertas\n"
            f"   🛠️   E 25+ habilidades — posso até criar novas\n"
            f"\n"
            f"🧠 Tenho memória persistente: lembro do que conversamos e "
            f"aprendo com seu feedback.\n"
            f"\n"
            f"Escreva em qualquer idioma — ou /help para ver todos os comandos.\n"
            f"\n"
            f"Por onde começamos?"
        )

    if lang == "fr":
        return (
            f"👋 Salut{name_part} ! Je suis WASP, ton agent autonome.\n"
            f"\n"
            f"Je ne fais pas que discuter. Je planifie et exécute de vraies tâches :\n"
            f"\n"
            f"   ⏰  Rappels et tâches planifiées\n"
            f"   🔍  Recherche web, scraping, surveillance de sites\n"
            f"   📸  Captures d'écran de n'importe quelle page\n"
            f"   📧  Email (Gmail) — lire, envoyer, chercher\n"
            f"   📈  Prix crypto et alertes\n"
            f"   🛠️   Et 25+ compétences — je peux même en créer de nouvelles\n"
            f"\n"
            f"🧠 J'ai une mémoire persistante : je me souviens de nos échanges "
            f"et j'apprends de tes retours.\n"
            f"\n"
            f"Écris-moi dans n'importe quelle langue — ou /help pour la liste des commandes.\n"
            f"\n"
            f"On commence par quoi ?"
        )

    # Default: English
    return (
        f"👋 Hi{name_part}! I'm WASP, your autonomous agent.\n"
        f"\n"
        f"I don't just chat. I plan and execute real tasks:\n"
        f"\n"
        f"   ⏰  Reminders and scheduled tasks\n"
        f"   🔍  Web search, scraping, website monitoring\n"
        f"   📸  Screenshots of any page\n"
        f"   📧  Email (Gmail) — read, send, search\n"
        f"   📈  Crypto prices and alerts\n"
        f"   🛠️   And 25+ more skills — I can even create new ones\n"
        f"\n"
        f"🧠 I have persistent memory: I remember our conversations and "
        f"learn from your feedback.\n"
        f"\n"
        f"Message me in any language — or /help for the full command list.\n"
        f"\n"
        f"What would you like to start with?"
    )


def _help_text(lang: str) -> str:
    """Full command reference — separate from /start so the welcome stays brief."""
    lang = (lang or "en").split("-")[0].lower()

    if lang == "es":
        return (
            "📚 *Comandos disponibles*\n"
            "\n"
            "   /start       — mensaje de bienvenida\n"
            "   /help        — esta ayuda\n"
            "   /ping        — verificar conexión\n"
            "   /status      — estado del sistema\n"
            "   /memory      — información de memoria\n"
            "   /snapshot    — guardar estado actual\n"
            "   /model       — modelo activo\n"
            "   /skills      — habilidades disponibles\n"
            "   /skill       — invocar una habilidad\n"
            "   /schedule    — tareas programadas\n"
            "   /introspect  — autoinspección\n"
            "   /monitor     — vigilar una URL\n"
            "   /broker      — gestión de integraciones\n"
            "   /api         — información de la API\n"
            "   /openclaw    — habilidades dinámicas\n"
            "\n"
            "💡 Tip: para casi todo, escribime en lenguaje natural. "
            "Los comandos son atajos opcionales."
        )

    if lang == "pt":
        return (
            "📚 *Comandos disponíveis*\n"
            "\n"
            "   /start       — mensagem de boas-vindas\n"
            "   /help        — esta ajuda\n"
            "   /ping        — verificar conexão\n"
            "   /status      — status do sistema\n"
            "   /memory      — informações de memória\n"
            "   /snapshot    — salvar estado atual\n"
            "   /model       — modelo ativo\n"
            "   /skills      — habilidades disponíveis\n"
            "   /skill       — invocar uma habilidade\n"
            "   /schedule    — tarefas agendadas\n"
            "   /introspect  — autoinspeção\n"
            "   /monitor     — monitorar uma URL\n"
            "   /broker      — gerenciamento de integrações\n"
            "   /api         — info da API\n"
            "   /openclaw    — habilidades dinâmicas\n"
            "\n"
            "💡 Dica: para quase tudo, escreva em linguagem natural. "
            "Os comandos são atalhos opcionais."
        )

    if lang == "fr":
        return (
            "📚 *Commandes disponibles*\n"
            "\n"
            "   /start       — message de bienvenue\n"
            "   /help        — cette aide\n"
            "   /ping        — vérifier la connexion\n"
            "   /status      — état du système\n"
            "   /memory      — info mémoire\n"
            "   /snapshot    — sauvegarder l'état\n"
            "   /model       — modèle actif\n"
            "   /skills      — compétences disponibles\n"
            "   /skill       — invoquer une compétence\n"
            "   /schedule    — tâches planifiées\n"
            "   /introspect  — auto-inspection\n"
            "   /monitor     — surveiller une URL\n"
            "   /broker      — gestion des intégrations\n"
            "   /api         — info API\n"
            "   /openclaw    — compétences dynamiques\n"
            "\n"
            "💡 Astuce : pour presque tout, écris en langage naturel. "
            "Les commandes sont des raccourcis optionnels."
        )

    return (
        "📚 *Available commands*\n"
        "\n"
        "   /start       — welcome message\n"
        "   /help        — this help\n"
        "   /ping        — check connection\n"
        "   /status      — system status\n"
        "   /memory      — memory info\n"
        "   /snapshot    — save current state\n"
        "   /model       — active model\n"
        "   /skills      — available skills\n"
        "   /skill       — invoke a skill\n"
        "   /schedule    — scheduled tasks\n"
        "   /introspect  — agent self-inspection\n"
        "   /monitor     — monitor a URL\n"
        "   /broker      — integrations management\n"
        "   /api         — API info\n"
        "   /openclaw    — dynamic skill registry\n"
        "\n"
        "💡 Tip: for almost everything, just write in plain language. "
        "Commands are optional shortcuts."
    )


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """First-contact handler — sends ONE static welcome.

    We intentionally do NOT round-trip through agent-core/LLM here. Reasons:
      1. The LLM was sometimes producing 2-3 short replies in a row,
         spamming the user.
      2. A previous meta-prompt listing example skills caused the planner
         to treat those examples as real tasks, exhausting the replan budget.
      3. A static reply guarantees the user immediately sees what the agent
         can do — which is what they need most on first contact.
    """
    from ..main import is_authorized

    if not is_authorized(update.effective_user.id):
        await update.message.reply_text("Unauthorized.")
        return

    lang = getattr(update.effective_user, "language_code", None) or "en"
    user_name = (getattr(update.effective_user, "first_name", "") or "").strip()
    await update.message.reply_text(_welcome_text(lang, user_name))


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/help — the command reference (separate from the welcome banner)."""
    from ..main import is_authorized

    if not is_authorized(update.effective_user.id):
        await update.message.reply_text("Unauthorized.")
        return

    lang = getattr(update.effective_user, "language_code", None) or "en"
    await update.message.reply_text(_help_text(lang), parse_mode="Markdown")
