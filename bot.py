from asyncio import create_task
from collections import defaultdict
from itertools import chain
import json
from logging import getLogger
from sys import stderr
from time import time
from traceback import print_exception, print_exc
from typing import Union

import aiohttp
from discord import HTTPException, TextChannel, Member, Role, utils
from discord.ext.commands import \
    CheckFailure, Cog, CommandInvokeError, UserInputError, check, command, \
    guild_only

from config import write_config
from schedule import repeat


about_bot = """**FactorioUpbot**
Bot for monitoring changes to servers in the Factorio public games\
 list.  Made by Hornwitser#6431
"""
logger = getLogger(__name__)

def fbool(factorio_boolean):
    """Convert a Factorio boolean to python boolean"""
    if type(factorio_boolean) is bool:
        return factorio_boolean

    if type(factorio_boolean) is str:
        return factorio_boolean == 'true'

    if factorio_boolean is None:
        return False

    raise TypeError(f"Expected str or bool, not {factorio_boolean!r}")

def format_minutes(minutes):
    """Returns a y/d/h/m string from a minute count"""
    years = minutes // (365 * 60 * 24)
    minutes = minutes % (365 * 60 * 24)
    days = minutes // (60 * 24)
    minutes = minutes % (60 * 24)
    hours = minutes // 60
    minutes = minutes % 60

    if years:
        return f"{years}y {days}d {hours}h {minutes}m"

    if days:
        return f"{days}d {hours}h {minutes}m"

    if hours:
        return f"{hours}h {minutes}m"

    return f"{minutes}m"

def version_stats(games):
    versions = defaultdict(lambda: {'servers': 0, 'players': 0})

    for game in games:
        app_ver = game.get('application_version', {})
        ver = app_ver.get('game_version', 'unknown')
        versions[ver]['servers'] += 1
        versions[ver]['players'] += len(game.get('players', []))

    return versions

def platform_stats(games):
    platforms = defaultdict(lambda: {'servers': 0, 'players': 0})

    for game in games:
        app_ver = game.get('application_version', {})
        plat = (
            app_ver.get('platform', 'unknown'),
            app_ver.get('build_mode', 'unknown'),
        )

        platforms[plat]['servers'] += 1
        platforms[plat]['players'] += len(game.get('players', []))

    return platforms

def password_stats(games):
    passwords = defaultdict(lambda: {'servers': 0, 'players': 0})

    for game in games:
        password = fbool(game.get('has_password'))

        passwords[password]['servers'] += 1
        passwords[password]['players'] += len(game.get('players', []))

    return passwords

def mod_stats(games):
    mods = defaultdict(lambda: {'servers': 0, 'players': 0})

    for game in games:
        modded = fbool(game.get('has_mods'))

        mods[modded]['servers'] += 1
        mods[modded]['players'] += len(game.get('players', []))

    return mods

def popular_stats(games, popular_games):
    popular = defaultdict(lambda: {'players': 0})

    for game in games:
        name = game.get('name', 'unknown')
        player_count = len(game.get('players', []))
        if name in popular_games or player_count >= 10:
            popular[name]['players'] = player_count

    return popular

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

    if 'role-pings' in guildcfg:
        missing_count = 0
        for role_id in guildcfg['role-pings']:
            role = ctx.guild.get_role(role_id)
            if role is None:
                missing_count += 1
                continue

            if not role.mentionable:
                warnings.append(f"{role.name} cannot be mentioned by the bot")

        if missing_count:
            warnings.append(f"{missing_count} role(s) to ping no longe exists")

    if 'member-pings' in guildcfg:
        missing_count = 0
        for member_id in guildcfg['member-pings']:
            member = ctx.guild.get_member(member_id)
            if member is None:
                missing_count += 1
                continue

        if missing_count:
            warnings.append(
                f"{missing_count} member(s) to ping are no longer present"
            )

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

    messages = []
    should_ping = False
    for server_cfg in guild_cfg['servers']:
        game = find_game(server_cfg, games)

        ping, msgs = await check_server(server_cfg, game, log_channel)
        should_ping = should_ping or ping
        messages.extend(msgs)

    if messages:
        pings = []
        if should_ping:
            for role_id in guild_cfg.get('role-pings', []):
                role = guild.get_role(role_id)
                if role is not None:
                    pings.append(role.mention)

            for member_id in guild_cfg.get('member-pings', []):
                member = guild.get_member(member_id)
                if member is not None:
                    pings.append(member.mention)

        await log_channel.send("\n".join([
            no_ping("\n".join(messages)),
            " ".join(pings),
        ]))

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
    new_password = fbool(game.get('has_password')) if game else None

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

    messages = (
        [f"\N{WARNING SIGN} {msg}" for msg in warnings]
        + [f"\N{WHITE HEAVY CHECK MARK} {msg}" for msg in returned]
        + [f"\N{LOCK} {msg}" for msg in locked]
        + [f"\N{OPEN LOCK} {msg}" for msg in unlocked]
        + [f"\N{BALLOT BOX WITH CHECK} {msg}" for msg in infos]
    )

    return (bool(warnings), messages)

def filter_duplicate_top_games(games):
    """Remove games listed twice in a 12 hour period"""
    listed = {}
    indices = []
    for index, game in enumerate(games):
        name = game.get('name', 'unknown')
        if name in listed:
            for time in listed[name]:
                if abs(time - game['_time']) < 60*60*12:
                    indices.append(index)
                    break
            else:
                listed[name].append(game['_time'])
        else:
            listed[name] = [game['_time']]

    for index in reversed(indices):
        del games[index]


class FactorioUpbot(Cog):
    async def bot_check(self, ctx):
        if not ctx.channel.permissions_for(ctx.me).send_messages:
            raise NoReplyPermission("Bot can't reply")
        return True

    def __init__(self, config, bot):
        self.bot = bot
        self.bot.my_config = config
        self.app_info = None

        self.checker_session = None
        self.checker_loop = None

        self.games_cache = []
        self.last_check = 0
        self.load_top_lists()
        self.load_popular()

        self.ifxdbc = None
        self.pgpool = None

    @Cog.listener()
    async def on_ready(self):
        print("Ready")
        if self.checker_session is None:
            self.checker_session = aiohttp.ClientSession()
            self.checker_loop = create_task(repeat(self.check_games, 60))

    async def check_games(self):
        cfg = self.bot.my_config
        url = 'https://multiplayer.factorio.com/get-games'
        params = {
            'username': cfg['factorio-username'],
            'token': cfg['factorio-token'],
        }

        async with self.checker_session.get(url, params=params) as resp:
            games = await resp.json()
            check_time = int(time())

        if type(games) is not list:
            logger.error(
                "Unexpected response from get-games endpoint: "
                f"{games}"
            )
            return

        if self.ifxdbc is not None:
            await self.post_factorio_stats(games, check_time)

        for guild_id, guild_cfg in cfg['guilds'].items():
            guild = self.bot.get_guild(int(guild_id))
            if guild is not None:
                await check_guild(guild, guild_cfg, games)

        await self.update_players(games, check_time)
        write_config(self.bot.my_config)

        self.last_check = check_time
        self.games_cache = games

        self.update_top_lists(games, check_time)
        self.update_popular(games, check_time)

    def cog_unload(self):
        self.checker_loop.cancel()
        create_task(self.checker_session.close())

    async def post_factorio_stats(self, games, timestamp):
        versions = version_stats(games)
        for version, fields in versions.items():
            await self.ifxdbc.write({
                'measurement': 'version',
                'time': timestamp * 10**9,
                'tags': { 'version': version },
                'fields': fields,
            })

        platforms = platform_stats(games)
        for (platform, build_mode), fields in platforms.items():
            await self.ifxdbc.write({
                'measurement': 'platform',
                'time': timestamp * 10**9,
                'tags': { 'platform': platform, 'build_mode': build_mode },
                'fields': fields,
            })

        passwords = password_stats(games)
        for password, fields in passwords.items():
            await self.ifxdbc.write({
                'measurement': 'has_password',
                'time': timestamp * 10**9,
                'tags': { 'has_password': 'yes' if password else 'no' },
                'fields': fields,
            })

        mods = mod_stats(games)
        for modded, fields in mods.items():
            await self.ifxdbc.write({
                'measurement': 'has_mods',
                'time': timestamp * 10**9,
                'tags': { 'has_mods': 'yes' if modded else 'no' },
                'fields': fields,
            })

        popular = popular_stats(games, self.popular_cache)
        for name, fields in popular.items():
            await self.ifxdbc.write({
                'measurement': 'popular',
                'time': timestamp * 10**9,
                'tags': { 'game_name': name },
                'fields': fields,
            })

    async def update_players(self, games, check_time):
        async with self.pgpool.acquire() as con:
            async with con.transaction():
                for game in games:
                    game_players = game.get('players', [])
                    if game_players:
                        await con.executemany('''
                            INSERT INTO players (name, last_seen, last_server, minutes, first_seen)
                            VALUES ($1, $2, $3, 1, $4)
                            ON CONFLICT (name) DO UPDATE SET
                            (last_seen, last_server, minutes)
                                = ($2, $3, players.minutes + 1);
                        ''', [(p, check_time, game.get('name'), check_time) for p in game_players])

    def update_top_lists(self, games, check_time):
        by_players = lambda g: len(g.get('players', []))
        top_games = sorted(games, key=by_players, reverse=True)[:20]

        for game in top_games:
            game['_time'] = check_time

        cutoff = {
            'day': check_time - 60*60*24,
            'week': check_time - 60*60*24*7,
            'month': check_time - 60*60*24*30,
            'year': check_time - 60*60*24*365,
            'all': 0,
        }

        for key, top_list in self.top_lists_cache['servers'].items():
            fresh = lambda g: g['_time'] >= cutoff[key]
            top_list = list(filter(fresh, top_list)) + top_games
            top_list.sort(key=by_players, reverse=True)
            filter_duplicate_top_games(top_list)
            self.top_lists_cache['servers'][key] = top_list[:20]

        with open('top-lists.json', 'w') as top_lists_file:
            top_lists_file.write(
                json.dumps(self.top_lists_cache, sort_keys=True, indent=4)
            )

    def load_top_lists(self):
        try:
            with open('top-lists.json') as top_lists_file:
                top_lists = json.load(top_lists_file)
        except OSError:
            top_lists = {
                'servers': {
                    'day': [],
                    'week': [],
                    'month': [],
                    'year': [],
                    'all': [],
                }
            }

        self.top_lists_cache = top_lists

    def update_popular(self, games, check_time):
        for game in games:
            if len(game.get('players', [])) >= 10:
                self.popular_cache[game.get('name', 'unknown')] = check_time

        cutoff = check_time - 60*60*12
        for name, time in list(self.popular_cache.items()):
            if time < cutoff:
                del self.popular_cache[name]

        with open('popular.json', 'w') as popular_file:
            popular_file.write(
                json.dumps(self.popular_cache, sort_keys=True, indent=4)
            )

    def load_popular(self):
        try:
            with open('popular.json') as popular_file:
                popular = json.load(popular_file)
        except OSError:
            popular = {}

        self.popular_cache = popular

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
    async def player(self, ctx, player_name):
        player = await self.pgpool.fetchrow("""
            SELECT name, last_seen, last_server, minutes FROM players
            WHERE lower(players.name) = lower($1);
        """, player_name)
        if player is None:
            await ctx.send("Haven't seen that player online")
            return

        name = player['name']
        server_name = player['last_server']
        if server_name is None:
            server_name = "_an unknown server_"

        if player['last_seen'] == self.last_check:
            msg = f"{name} is on {server_name}"
        else:
            delta = format_minutes((int(time()) - player['last_seen']) // 60)
            msg = f"{name} was last seen on {server_name} {delta} ago"

        duration = format_minutes(player['minutes'])
        msg += f" and has been seen online for {duration}"
        await ctx.send(no_ping(msg))

    @command(name='top-servers')
    async def top_servers(self, ctx, period=None):
        """List the top 10 servers by current player count"""
        if (
            period is not None and
            period not in ['day', 'week', 'month', 'year', 'all']
        ):
            msg = "Expected period to be one of day, week, month, year and all"
            await ctx.send(msg)
            return

        def key(game):
            players = len(game.get('players', []))
            game_time = int(game.get('game_time_elapsed', '0'))
            return (players, game_time)

        if period:
            top = self.top_lists_cache['servers'][period][:10]
        else:
            top = sorted(self.games_cache, reverse=True, key=key)[:10]

        if not top:
            await ctx.send("No servers are listed")
            return

        servers = []
        for game in top:
            name = game.get('name', 'unknown')

            limit = game.get('max_players', 0)
            count = len(game.get('players', []))
            players = f"`{count}/{limit if limit else '∞'}`"

            app_ver = game.get('application_version', {})
            ver = f"`{app_ver.get('game_version', 'unknown')}`"

            servers.append(f"{ver} {players} {name}")

        await ctx.send(no_ping("\n".join(servers)))

    @command(name='top-players')
    async def top_players(self, ctx):
        """List the top 10 players by online play time"""
        top = await self.pgpool.fetch("""
            SELECT name, minutes FROM players ORDER BY minutes DESC LIMIT 10;
        """)

        top_list = []
        for player in top:
            time_seen = format_minutes(player['minutes'])
            top_list.append(f"{time_seen} {player['name']}")

        await ctx.send(no_ping("\n".join(top_list)))

    @command(name='top-versions')
    async def top_versions(self, ctx):
        """List the top 10 versions by number of servers using it"""
        versions = version_stats(self.games_cache)

        if not versions:
            await ctx.send("No servers online")
            return

        def key(item):
            return item[1]['players'] + item[1]['servers']

        top = sorted(versions.items(), reverse=True, key=key)[:10]

        top_list = ["version servers players"]
        for ver, counts in top:
            def pad(field):
                space = '\N{EN SPACE}'*max(4-len(str(field)), 0)
                return f"{space}`{field}`"

            top_list.append(
                f"`{ver}` {pad(counts['servers'])} {pad(counts['players'])}"
            )

        await ctx.send(no_ping("\n".join(top_list)))


    @command()
    async def stats(self, ctx):
        """Show statistics about the multiplayer servers"""
        row = await self.pgpool.fetchrow("""
            SELECT SUM(minutes) AS sum, COUNT(*) AS count FROM players;
        """)

        unique_players = set()
        unique_versions = set()
        has_password = 0
        has_mods = 0
        for game in self.games_cache:
            unique_players.update(game.get('players', []))

            app_ver = game.get('application_version', {})
            unique_versions.add(app_ver.get('game_version', 'unknown'))

            has_password += fbool(game.get('has_password'))
            has_mods += fbool(game.get('has_mods'))

        await ctx.send(
            f"{len(self.games_cache)} servers online accross"
            f" {len(unique_versions)} different versions of Factorio\n"
            f"{has_password} servers are password protected and"
            f" {has_mods} servers use mods\n"
            f"{len(unique_players)} players currently online out of"
            f" {row['count']} seen with a"
            f" combined playtime online of {format_minutes(row['sum'])}"
        )

    @command()
    @guild_only()
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
                limit = game.get('max_players', 0)
                count = len(game.get('players', []))
                players = f"`{count}/{limit if limit else '∞'}`"

                app_ver = game.get('application_version', {})
                ver = f"`{app_ver.get('game_version', 'unknown')}`"

                if fbool(game.get('has_password')):
                    statuses.append(
                        f"\N{LOCK} {ver} {players} {name}"
                        " is listed as password protected"
                    )
                else:
                    statuses.append(
                        f"\N{BALLOT BOX WITH CHECK} {ver} {players} {name}"
                        " is listed"
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

        game = find_game({'name': name}, self.games_cache)
        server_cfgs.append({
            'name': name,
            'state': {
                'listed': bool(game),
                'password': fbool(game.get('has_password')) if game else None,
            },
        })

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
            server_cfgs.append({
                'name': game['name'],
                'state': {
                    'listed': True,
                    'password': fbool(game.get('has_password')),
                },
            })

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

    @command(name='remove-all')
    @guild_only()
    @check(is_guild_admin)
    async def remove_all(self, ctx, pattern=""):
        """Remove all server being checked that matches pattern"""
        cfg = self.bot.my_config
        guild_cfg = cfg['guilds'][str(ctx.guild.id)]

        server_cfgs = guild_cfg.setdefault('servers', [])
        if not server_cfgs:
            await cxt.send("No servers are currently being checked for")
            return

        indices_to_remove = []
        for index, server_cfg in enumerate(server_cfgs):
            if pattern in server_cfg['name']:
                indices_to_remove.append(index)

        if not indices_to_remove:
            await ctx.send("No server being checked for matched the pattern")
            return

        if len(indices_to_remove) == len(server_cfgs):
            server_cfgs.clear()
            msg = "Removed all servers currently being checked for"

        else:
            for index in reversed(indices_to_remove):
                del server_cfgs[index]

            msg = f"Removed {len(indices_to_remove)} servers being checked for"

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

    @command(name='set-unlisted-pings')
    @guild_only()
    @check(is_guild_admin)
    async def set_unlisted_pings(self, ctx, *pings: Union[Role, Member]):
        """Set the pings to emmit when a server becomes unlisted"""
        cfg = self.bot.my_config
        guild_cfg = cfg['guilds'][str(ctx.guild.id)]

        if pings:
            guild_cfg['role-pings'] = []
            guild_cfg['member-pings'] = []
            for ping in pings:
                if isinstance(ping, Role):
                    guild_cfg['role-pings'].append(ping.id)
                else:
                    guild_cfg['member-pings'].append(ping.id)

            await send_and_warn(ctx, "Updated unlisted pings")

        else:
            if (
                'role-pings' not in guild_cfg
                and 'member-pings' not in guild_cfg
            ):
                await send_and_warn(ctx, "No unlisted pings")

            else:
                del guild_cfg['role-pings']
                del guild_cfg['member-pings']
                await send_and_warn(ctx, "Removed unlisted pings")

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
