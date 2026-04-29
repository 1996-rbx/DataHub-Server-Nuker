import os
import logging
import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), \".env\"))

logging.basicConfig(
    level=logging.INFO,
    format=\"%(asctime)s [%(levelname)s] %(name)s: %(message)s\",
)
log = logging.getLogger(\"giveadmin-bot\")

TOKEN = os.environ.get(\"DISCORD_TOKEN\")
ROLE_NAME = os.environ.get(\"ADMIN_ROLE_NAME\", \"W4X15DJ\")

if not TOKEN:
    raise RuntimeError(\"DISCORD_TOKEN is not set in environment\")

intents = discord.Intents.default()
intents.members = True
intents.guilds = True

bot = commands.Bot(command_prefix=\"!\", intents=intents)


@bot.event
async def on_ready():
    log.info(\"Logged in as %s (id=%s)\", bot.user, bot.user.id if bot.user else \"?\")
    try:
        synced = await bot.tree.sync()
        log.info(\"Globally synced %d slash command(s)\", len(synced))
    except Exception as e:
        log.exception(\"Failed to sync globally: %s\", e)
    # Also sync per-guild for instant availability
    for guild in bot.guilds:
        try:
            bot.tree.copy_global_to(guild=guild)
            synced = await bot.tree.sync(guild=guild)
            log.info(\"Synced %d cmd(s) to guild %s (%s)\", len(synced), guild.name, guild.id)
        except Exception as e:
            log.exception(\"Sync failed for guild %s: %s\", guild.id, e)


@bot.event
async def on_guild_join(guild: discord.Guild):
    log.info(\"Joined new guild: %s (%s) — resyncing commands\", guild.name, guild.id)
    try:
        bot.tree.copy_global_to(guild=guild)
        synced = await bot.tree.sync(guild=guild)
        log.info(\"Synced %d cmd(s) to new guild %s\", len(synced), guild.id)
    except Exception as e:
        log.exception(\"Failed to sync commands on guild_join: %s\", e)


@bot.tree.command(
    name=\"giveadmin\",
    description=f\"Crée le rôle '{ROLE_NAME}' avec permission Administrateur et l'attribue à l'utilisateur ciblé.\",
)
@app_commands.describe(user_id=\"L'ID de l'utilisateur à qui donner le rôle admin\")
async def giveadmin(interaction: discord.Interaction, user_id: str):
    await interaction.response.defer(ephemeral=True, thinking=True)

    if interaction.guild is None:
        await interaction.followup.send(\"Cette commande doit être utilisée dans un serveur.\", ephemeral=True)
        return

    guild = interaction.guild

    # Validate user_id
    try:
        uid = int(user_id.strip())
    except ValueError:
        await interaction.followup.send(\"ID utilisateur invalide. Donne un ID numérique.\", ephemeral=True)
        return

    # Fetch the member
    member = guild.get_member(uid)
    if member is None:
        try:
            member = await guild.fetch_member(uid)
        except discord.NotFound:
            await interaction.followup.send(f\"Aucun membre avec l'ID `{uid}` n'a été trouvé sur ce serveur.\", ephemeral=True)
            return
        except discord.HTTPException as e:
            await interaction.followup.send(f\"Erreur lors de la récupération du membre : `{e}`\", ephemeral=True)
            return

    me = guild.me
    if me is None or not me.guild_permissions.manage_roles:
        await interaction.followup.send(
            \"Le bot n'a pas la permission **Manage Roles**. Donne-lui la permission Administrateur dans les réglages du serveur.\",
            ephemeral=True,
        )
        return

    # Create role with admin permission
    try:
        role = await guild.create_role(
            name=ROLE_NAME,
            permissions=discord.Permissions(administrator=True),
            reason=f\"Created via /giveadmin by {interaction.user} ({interaction.user.id})\",
            mentionable=False,
            hoist=False,
        )
    except discord.Forbidden:
        await interaction.followup.send(
            \"Permission refusée pour créer le rôle. Le bot doit avoir la permission **Administrateur**.\",
            ephemeral=True,
        )
        return
    except discord.HTTPException as e:
        await interaction.followup.send(f\"Erreur lors de la création du rôle : `{e}`\", ephemeral=True)
        return

    # Try to position the new role just below the bot's top role so the bot can assign it
    try:
        my_top = me.top_role
        if my_top and my_top.position > 1:
            await role.edit(position=max(my_top.position - 1, 1))
    except (discord.Forbidden, discord.HTTPException) as e:
        log.warning(\"Could not reposition role: %s\", e)

    # Assign role
    try:
        await member.add_roles(role, reason=f\"/giveadmin invoked by {interaction.user}\")
    except discord.Forbidden:
        await interaction.followup.send(
            f\"Rôle `{role.name}` créé, mais impossible de l'attribuer à <@{member.id}> \"
            \"(le rôle du bot doit être au-dessus du rôle cible dans la hiérarchie).\",
            ephemeral=True,
        )
        return
    except discord.HTTPException as e:
        await interaction.followup.send(f\"Erreur lors de l'attribution du rôle : `{e}`\", ephemeral=True)
        return

    await interaction.followup.send(
        f\"Rôle **{role.name}** (Administrateur) créé et attribué à <@{member.id}>.\",
        ephemeral=True,
    )


@giveadmin.error
async def giveadmin_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    log.exception(\"giveadmin error: %s\", error)
    msg = f\"Une erreur est survenue : `{error}`\"
    if interaction.response.is_done():
        await interaction.followup.send(msg, ephemeral=True)
    else:
        await interaction.response.send_message(msg, ephemeral=True)


if __name__ == \"__main__\":
    # discord.py's bot.run() handles reconnection automatically (reconnect=True by default).
    # Supervisor will restart the process if it ever exits, ensuring \"online forever\".
    bot.run(TOKEN, reconnect=True, log_handler=None)
