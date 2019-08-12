from logging import basicConfig, INFO

from discord.ext.commands import Bot
from aioinflux import InfluxDBClient

from bot import FactorioUpbot, prefixes
from config import load_config


basicConfig(level=INFO)

if __name__ == '__main__':
    config = load_config()
    bot = Bot(
        command_prefix=prefixes, help_attrs={'name':config['help-command']},
        fetch_offline_members=False
    )
    cog = FactorioUpbot(config, bot)
    bot.add_cog(cog)

    if 'ifxdb' in config:
        ifxdbc = InfluxDBClient(db=config['ifxdb'])
        cog.ifxdbc = ifxdbc
        bot.run(config['bot-token'])
        # I hate you

    else:
        bot.run(config['bot-token'])
