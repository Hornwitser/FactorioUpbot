from collections import defaultdict
import json

def write_config(config):
    with open('config.json', 'w') as config_file:
        # Note: uses dumps instead of dump as the latter can leave the
        #       the config file in an incomplete state on exceptions
        config_file.write(json.dumps(config, sort_keys=True, indent=4))

def load_config():
    try:
        with open('config.json') as config_file:
            config = json.load(config_file)
    except OSError:
        decision = input("Unable to load config.json, write a new one (y/N)? ")
        if decision.lower() != "y":
            print("Aborting")
            exit(1)

        config = {
            'bot-token': '<your-bot-token>',
            'factorio-username': '<your-factorio-username>',
            'factorio-token': '<your-factorio-token>',
            'help-command': 'help',
            'global': {
                'guild-command-prefixes': ['<@{bot_id}> ', '<@!{bot_id}> '],
                'dm-command-prefixes': ['<@{bot_id}> ', ''],
            },
            'guilds': {},
        }

        write_config(config)
        print("new config.json written, please configure it and restart")
        exit(1)

    if 'global' not in config:
        print("Adding missing 'global' entry to config")
        config['global'] = {
            'guild-command-prefixes': ['<@{bot_id}> ', '<@!{bot_id}> '],
            'dm-command-prefixes': ['<@{bot_id}> ', ''],
        }

    # Auto create entries for guilds on first usage
    config['guilds'] = defaultdict(lambda: {}, config['guilds'])

    return config

