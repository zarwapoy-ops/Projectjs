"""Entry point for the manga-starz.net Discord notification bot."""

import asyncio

from mangastarz.bot import run

if __name__ == "__main__":
    asyncio.run(run())
