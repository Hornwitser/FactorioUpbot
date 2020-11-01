import asyncio
import asyncpg
import json

from config import load_config


async def migrate():
    config = load_config()

    conn = await asyncpg.connect(config["pg-url"], command_timeout=45)

    with open("players.json") as players_file:
        players = json.load(players_file)

    async with conn.transaction():
        for n, p in players.items():
            await conn.execute('''
                INSERT INTO players (name, last_seen, last_server, minutes)
                VALUES ($1, $2, $3, $4);
            ''', n, p["last_seen"], p["last_server"], p["minutes"])

    await conn.close()

asyncio.run(migrate())
