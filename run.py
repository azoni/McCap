from mccapbot.config import TOKEN
from mccapbot.logging_setup import setup_logging, log
from mccapbot.bot import Bot
import os
from dotenv import load_dotenv
load_dotenv()

if __name__ == "__main__":
    setup_logging()
    TOKEN = os.getenv("MCCAP_TOKEN")
    bot = Bot()
    bot.run(TOKEN)
