from asyncio import run
from logging import basicConfig, INFO

from discord import Intents
from discord.ext.commands import Bot
from aioinflux import InfluxDBClient
import asyncpg

from bot import FactorioUpbot, prefixes
from config import load_config


basicConfig(level=INFO)

async def main():
    config = load_config()
    bot = Bot(
        intents=Intents(guilds=True, messages=True, message_content=True),
        command_prefix=prefixes, help_attrs={'name':config['help-command']},
        fetch_offline_members=False
    )
    cog = FactorioUpbot(config, bot)
    await bot.add_cog(cog)

    cog.pgpool = await asyncpg.create_pool(
        config["pg-url"], command_timeout=45
    )

    if 'ifxdb' in config:
        ifxdbc = InfluxDBClient(
            db=config['ifxdb'],
            username=config['ifxuser'],
            password=config['ifxpassword']
        )
        cog.ifxdbc = ifxdbc

    async with bot:
        await bot.start(config['bot-token'])

if __name__ == '__main__':
    run(main())
