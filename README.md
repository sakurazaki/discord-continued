discord-continued
====================

**A simple API wrapper for Discord interactions.**


[![PyPI Monthly Downloads](https://img.shields.io/pypi/dm/discord-continued.svg)](https://pypi.org/project/discord-continued/)
[![PyPI version](https://img.shields.io/pypi/v/discord-continued.svg)](https://pypi.org/project/discord-continued/)
[![License](https://img.shields.io/github/license/sakurazaki/discord-continued.svg)](https://github.com/sakurazaki/discord-continued/blob/master/LICENSE)
[![Discord](https://discord.com/api/guilds/905226844851286048/embed.png)](https://discord.gg/R2FsdNn29A)


Stable version for a Bot.
Fully supports Interactions and Chat commands.
Should manage auto sharding.

Needs a lot of documentation.

Beta - WIP

Installation
============

Use this line to install our library:

	pip install -U discord-continued

Examples
===========

```python
import discord

class MyClient(discord.Client):
    async def on_ready(self):
        print('Logged on as', self.user)

    async def on_message(self, message):
        # don't respond to ourselves
        if message.author == self.user:
            return

        if message.content == 'ping':
            await message.channel.send('pong')

client = MyClient()
client.run('token')
```

```python
import discord
from discord.ext import commands

bot = commands.Bot(command_prefix='>')

@bot.command()
async def ping(ctx):
    await ctx.send('pong')

bot.run('token')
```


Links
=============
[Official Discord Server](https://discord.gg/R2FsdNn29A)