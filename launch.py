from logging import basicConfig, INFO

from discord.ext.commands import Bot

from bot import FactorioUpbot, prefixes
from config import load_config


basicConfig(level=INFO)

if __name__ == '__main__':
    config = load_config()
    bot = Bot(
        command_prefix=prefixes, help_attrs={'name':config['help-command']},
        fetch_offline_members=False
    )
    bot.add_cog(FactorioUpbot(config, bot))
    bot.run(config['bot-token'])
