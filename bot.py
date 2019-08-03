from asyncio import create_task
from itertools import chain
from logging import getLogger
from sys import stderr
from traceback import print_exception, print_exc

import aiohttp
from discord import HTTPException, TextChannel, Member, Role, utils
from discord.ext.commands import \
    CheckFailure, Cog, CommandInvokeError, UserInputError, check, command, \
    guild_only
import discord.ext.tasks as tasks

from config import write_config


about_bot = """**FactorioUpbot**
Bot for monitoring changes to servers in the Factorio public games\
 list.  Made by Hornwitser#6431
"""
logger = getLogger(__name__)

# Can't use commands.is_owner because that doesn't let me easily reuse it
async def is_bot_owner(ctx):
    return await ctx.bot.is_owner(ctx.author)

async def is_guild_admin(ctx):
    if await is_bot_owner(ctx): return True
    if ctx.guild is None: return False
    if ctx.author.guild_permissions.administrator: return True

    cfg = ctx.bot.my_config
    role_id = cfg['guilds'][str(ctx.guild.id)].get('admin-role-id')
    if role_id is None: return False

    role = utils.get(ctx.author.roles, id=int(role_id))
    return role is not None

def no_ping(msg):
    msg = msg.replace('@everyone', '@\u200beveryone')
    return msg.replace('@here', '@\u200bhere')

def config_errors(ctx):
    guildcfg = ctx.bot.my_config['guilds'][str(ctx.guild.id)]
    errors = []

    if 'log-channel-id' in guildcfg:
        ch = ctx.guild.get_channel(int(guildcfg['log-channel-id']))
        if ch is not None and not ch.permissions_for(ctx.me).send_messages:
            errors.append("Bot does not have send permission to log channel")

    return ["\N{NO ENTRY} {}".format(e) for e in errors]

def config_warnings(ctx):
    guildcfg = ctx.bot.my_config['guilds'][str(ctx.guild.id)]
    warnings = []

    if not ctx.me.guild_permissions.add_reactions:
        warnings.append("Bot does not have guild wide add reaction permisson, "
                        "reactions will not work in channels without it")

    if not ctx.me.guild_permissions.read_message_history:
        warnings.append("Bot does not have guild wide read message history "
                        "permisson, this may be required for reactions")

    if 'log-channel-id' in guildcfg:
        ch = ctx.guild.get_channel(int(guildcfg['log-channel-id']))
        if ch is None:
            warnings.append("Configured log channel does not exist")
    else:
        warnings.append("Log channel is not configured")

    if 'admin-role-id' in guildcfg:
        role = utils.get(ctx.guild.roles, id=int(guildcfg['admin-role-id']))
        if role is None:
            warnings.append("Configured admin role does not exist")

    return ["\N{WARNING SIGN} {}".format(e) for e in warnings]

def config_problems(ctx):
    return config_errors(ctx) + config_warnings(ctx)

async def send_and_warn(ctx, msg):
    problems = config_problems(ctx)
    if problems:
        msg = "\n".join([msg, "\n**Warning**"] + problems)

    await ctx.send(msg)


class NoReplyPermission(CheckFailure):
    pass

def prefixes(bot, msg):
    def prefix_format(prefix):
        return prefix.format(bot_id=bot.user.id)

    cfg = bot.my_config
    if msg.guild is not None:
        defaults = cfg['global']['guild-command-prefixes']
        guild = cfg['guilds'][str(msg.guild.id)].get('command-prefixes', [])
        return chain(guild, map(prefix_format, defaults))

    defaults = cfg['global']['dm-command-prefixes']
    return map(prefix_format, defaults)

def find_game(server_cfg, games):
    matches = []
    for game in games:
        if game.get('name') == server_cfg['name']:
            matches.append(game)

    if matches:
        # XXX for now return the first match
        return matches[0]

    return None


async def check_guild(guild, guild_cfg, games):
    if not 'servers' in guild_cfg:
        return

    log_channel_id = guild_cfg.get('log-channel-id')
    if log_channel_id is None:
        return

    log_channel = guild.get_channel(int(log_channel_id))
    if log_channel is None:
        return

    logger.info(f"Checking for guild {guild.name}")
    messages = []
    for server_cfg in guild_cfg['servers']:
        game = find_game(server_cfg, games)

        messages.extend(await check_server(server_cfg, game, log_channel))

    if messages:
        await log_channel.send(no_ping("\n".join(messages)))

async def check_server(server_cfg, game, log_channel):
    msg = None

    warnings = []
    returned = []
    locked = []
    unlocked = []
    infos = []

    state = server_cfg.setdefault('state', {})
    old_listed = state.get('listed')
    old_password = state.get('password')
    new_listed = bool(game)
    new_password = game.get('has_password') if game else None

    name = server_cfg['name']

    if old_listed is True:
        if not new_listed:
            warnings.append(f"{name} is no longer listed")

        elif new_password and not old_password:
            locked.append(f"{name} is now password protected")

        elif old_password and not new_password:
            unlocked.append(f"{name} is no longer password protected")

    elif old_listed is False:
        if new_listed:
            if new_password:
                locked.append(f"{name} is back on the list password protected")
            else:
                returned.append(f"{name} is back on the list")

    elif old_listed is None:
        if new_listed:
            if new_password:
                locked.append(f"{name} is listed as password protected")
            else:
                infos.append(f"{name} is listed")

        else:
            warnings.append(f"{name} is not listed")


    state['listed'] = new_listed
    state['password'] = new_password

    return (
        [f"\N{WARNING SIGN} {msg}" for msg in warnings]
        + [f"\N{WHITE HEAVY CHECK MARK} {msg}" for msg in returned]
        + [f"\N{LOCK} {msg}" for msg in locked]
        + [f"\N{OPEN LOCK} {msg}" for msg in unlocked]
        + [f"\N{BALLOT BOX WITH CHECK} {msg}" for msg in infos]
    )


class FactorioUpbot(Cog):
    async def bot_check(self, ctx):
        if not ctx.channel.permissions_for(ctx.me).send_messages:
            raise NoReplyPermission("Bot can't reply")
        return True

    def __init__(self, config, bot):
        self.bot = bot
        self.bot.my_config = config
        self.app_info = None

        self.checker_session = aiohttp.ClientSession()
        self.checker_loop.start()
        self.games_cache = []

    @tasks.loop(seconds=60)
    async def checker_loop(self):
        # Who thought it was a good idea to just swallow exceptions in tasks?
        try:
            await self.check_games()
        except Exception:
            print_exc()

    async def check_games(self):
        cfg = self.bot.my_config
        url = 'https://multiplayer.factorio.com/get-games'
        params = {
            'username': cfg['factorio-username'],
            'token': cfg['factorio-token'],
        }

        async with self.checker_session.get(url, params=params) as resp:
            games = await resp.json()
            if type(games) is not list:
                logger.error(
                    "Unexpected response from get-games endpoint: "
                    f"{games}"
                )
                return

            self.games_cache = games
            logger.info(f"Got response with {len(games)} entries")
            for guild_id, guild_cfg in cfg['guilds'].items():
                guild = self.bot.get_guild(int(guild_id))
                if guild is not None:
                    await check_guild(guild, guild_cfg, games)

        write_config(self.bot.my_config)

    @checker_loop.before_loop
    async def before_checker(self):
        await self.bot.wait_until_ready()

    def cog_unload(self):
        self.checker_loop.cancel()
        create_task(self.checker_session.close())

    @command()
    async def about(self, ctx):
        """About this bot"""
        await ctx.send(about_bot)

    @command()
    async def invite(self, ctx):
        """Gives an invite link for this bot"""
        if self.app_info is None:
            self.app_info = await self.bot.application_info()

        if self.app_info.bot_public:
            url = "https://discordapp.com/api/oauth2/authorize"
            params = f"client_id={self.app_info.id}&scope=bot"
            await ctx.send(f"<{url}?{params}>")

        else:
            await ctx.send(f"This bot is private")

    @command()
    @guild_only()
    @check(is_guild_admin)
    async def status(self, ctx):
        """Show the status of all currently tracked servers"""
        cfg = self.bot.my_config
        guild_cfg = cfg['guilds'][str(ctx.guild.id)]

        server_cfgs = guild_cfg.get('servers')
        if not server_cfgs:
            await ctx.send("No servers have been added")
            return

        statuses = []
        for server_cfg in server_cfgs:
            name = server_cfg['name']
            game = find_game(server_cfg, self.games_cache)
            if game:
                if game.get('has_password'):
                    statuses.append(
                        f"\N{LOCK} {name} is listed as password protected"
                    )
                else:
                    statuses.append(
                        f"\N{BALLOT BOX WITH CHECK} {name} is listed"
                    )

            else:
                statuses.append(f"\N{WARNING SIGN} {name} is not listed")

        await ctx.send("\n".join(statuses))

    @command(name='add-server')
    @guild_only()
    @check(is_guild_admin)
    async def add_server(self, ctx, name):
        """Add a server to check for online status"""
        cfg = self.bot.my_config
        guild_cfg = cfg['guilds'][str(ctx.guild.id)]

        server_cfgs = guild_cfg.setdefault('servers', [])
        for server_cfg in server_cfgs:
            if server_cfg['name'] == name:
                msg = "Error: Server with that name has already been added"
                await ctx.send(msg)
                return

        server_cfg = {'name': name}
        game = find_game(server_cfg, self.games_cache)
        server_cfg['listed'] = bool(game)
        server_cfg['password'] = game.get('has_password') if game else None
        server_cfgs.append(server_cfg)

        msg = no_ping(f"Added {name} to the list of servers to check for")
        await send_and_warn(ctx, msg)
        write_config(cfg)

    @command(name='add-all')
    @guild_only()
    @check(is_guild_admin)
    async def add_all(self, ctx, pattern):
        """Add all servers containing the pattern in name"""
        cfg = self.bot.my_config
        guild_cfg = cfg['guilds'][str(ctx.guild.id)]
        server_cfgs = guild_cfg.setdefault('servers', [])

        if not pattern:
            await ctx.send("Error: pattern cannot be empty")
            return

        to_add = []
        for game in self.games_cache:
            name = game.get('name', "")
            if pattern in name:
                for server_cfg in server_cfgs:
                    if server_cfg['name'] == name:
                        break # Already have this configured
                else:
                    to_add.append(game)

        if not to_add:
            await ctx.send("No additional servers matched the pattern")
            return

        if len(to_add) > 100:
            await ctx.send(f"Refusing to add {len(to_add)} entries")
            return

        for game in to_add:
            server_cfg = {'name': game.get('name', "")}
            server_cfg['listed'] = True
            server_cfg['password'] = game.get('has_password') if game else None
            server_cfgs.append(server_cfg)

        msg = no_ping(
            f"Added {len(to_add)} new entries to the list of servers"
            " to check for"
        )
        await send_and_warn(ctx, msg)
        write_config(cfg)

    @command(name='remove-server')
    @guild_only()
    @check(is_guild_admin)
    async def remove_server(self, ctx, name):
        """Remove server from being checked for online status"""
        cfg = self.bot.my_config
        guild_cfg = cfg['guilds'][str(ctx.guild.id)]

        server_cfgs = guild_cfg.setdefault('servers', [])
        index_to_remove = None
        for index, server_cfg in enumerate(server_cfgs):
            if server_cfg['name'] == name:
                index_to_remove = index
                break
        else:
            msg = "Error: no server with that name is being checked for"
            await ctx.send(msg)
            return

        del server_cfgs[index]

        msg = no_ping(f"Removed {name} from the list of servers to check for")
        await send_and_warn(ctx, msg)
        write_config(cfg)

    @command(name='set-admin-role')
    @guild_only()
    @check(is_guild_admin)
    async def set_admin_role(self, ctx, role: Role = None):
        """Role granting access to settings on the bot"""
        cfg = self.bot.my_config
        if role is not None:
            if not role.is_default():
                arid = str(role.id)
                cfg['guilds'][str(ctx.guild.id)]['admin-role-id'] = arid
                msg = no_ping("Set admin role to {}".format(role.name))
            else:
                msg = "Granting admin access to everyone is not allowed"
        else:
            try:
                del cfg['guilds'][str(ctx.guild.id)]['admin-role-id']
                msg = "Removed configured admin role"
            except KeyError:
                msg = "Admin role is not set"

        await send_and_warn(ctx, msg)
        write_config(cfg)

    @command(name='set-log-channel')
    @guild_only()
    @check(is_guild_admin)
    async def set_log_channel(self, ctx, ch: TextChannel = None):
        """Channel down messages are logged to"""
        cfg = self.bot.my_config
        if ch is not None:
            cfg['guilds'][str(ctx.guild.id)]['log-channel-id'] = str(ch.id)
            msg = "Set log channel to {}".format(ch.mention)

        else:
            try:
                del cfg['guilds'][str(ctx.guild.id)]['log-channel-id']
                msg = "Removed configured log channel"
            except KeyError:
                msg = "Log channel is not set"

        await send_and_warn(ctx, msg)
        write_config(cfg)

    @command(name='set-bot-nick')
    @guild_only()
    @check(is_guild_admin)
    async def set_bot_nick(self, ctx, *, nick=None):
        """Set the nickname of the bot for this guild"""
        if ctx.me.guild_permissions.change_nickname:
            await ctx.me.edit(nick=nick)
            if nick is not None:
                await ctx.send(no_ping("Changed nick to {}".format(nick)))
            else:
                await ctx.send("Reset nick")
        else:
            await ctx.send("\N{NO ENTRY} Bot does not have permission "
                           "to change nickname")

    @command(name='set-bot-prefixes')
    @guild_only()
    @check(is_guild_admin)
    async def set_bot_prefix(self, ctx, *prefixes):
        """Set the command prefixes of the bot for this guild"""
        cfg = self.bot.my_config
        if prefixes:
            cfg['guilds'][str(ctx.guild.id)]['command-prefixes'] = prefixes
            msg = no_ping("Set bot command prefixes to {}"
                          "".format(', '.join(prefixes)))

        else:
            try:
                del cfg['guilds'][str(ctx.guild.id)]['command-prefixes']
                msg = "Removed configured command prefixes"
            except KeyError:
                msg = "Command prefixes is not set"

        await send_and_warn(ctx, msg)
        write_config(cfg)

    @command(name='check-config')
    @guild_only()
    @check(is_guild_admin)
    async def check_config(self, ctx):
        """Check for possible problems with the config and permissions"""
        problems = config_problems(ctx)
        if problems:
            await ctx.send("\n".join(["Found the following issues"] + problems))
        else:
            await ctx.send("No issues with the configuration detected")

    @command(name='set-factorio-username')
    @check(is_bot_owner)
    async def set_factorio_username(self, ctx, username):
        """Set the username used for querying the multiplayer list"""
        cfg = self.bot.my_config
        cfg['factorio-username'] = username
        await ctx.send(
            no_ping("Changed factorio username to {}.".format(username))
        )

    @command(name='set-factorio-token')
    @check(is_bot_owner)
    async def set_factorio_token(self, ctx, token):
        """Set the token used for querying the multiplayer list"""
        cfg = self.bot.my_config
        cfg['factorio-token'] = token
        await ctx.send("Updated the token")

    @command()
    @check(is_bot_owner)
    async def name(self, ctx, *, new_name: str):
        """Set the bot's name"""
        await self.user.edit(username=new_name)
        await ctx.send(no_ping("Changed name to {}.".format(new_name)))

    @command()
    @check(is_bot_owner)
    async def avatar(self, ctx):
        """Set bot avatar to the image uploaded"""
        att = ctx.message.attachments
        if len(att) == 1:
            async with aiohttp.ClientSession() as session:
                async with session.get(att[0].proxy_url) as resp:
                    avatar = await resp.read()
                    await self.user.edit(avatar=avatar)
                    await ctx.send("Avatar changed.")
        else:
            await ctx.send("You need to upload the avatar with the command.")

    async def cog_command_error(self, ctx, error):
        itis = lambda cls: isinstance(error, cls)
        if itis(CommandInvokeError): reaction = "\N{COLLISION SYMBOL}"
        elif itis(NoReplyPermission): reaction = "\N{ZIPPER-MOUTH FACE}"
        elif itis(CheckFailure): reaction = "\N{NO ENTRY SIGN}"
        elif itis(UserInputError): reaction = "\N{BLACK QUESTION MARK ORNAMENT}"
        else: reaction = None

        if reaction is not None:
            try:
                await ctx.message.add_reaction(reaction)
            except HTTPException:
                if ctx.channel.permissions_for(ctx.me).send_messages:
                    try:
                        await ctx.send(reaction)
                    except HTTPException:
                        pass

        if itis(CommandInvokeError):
            print("Exception in command {}:".format(ctx.command), file=stderr)
            print_exception(
                type(error), error, error.__traceback__, file=stderr
            )
