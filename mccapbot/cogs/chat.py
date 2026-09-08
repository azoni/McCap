"""Chat with McCap: @mention it or DM it. Plus /memory to see what it keeps."""

import time

import discord
from discord import app_commands
from discord.ext import commands

from .. import chat
from ..config import CHAT_ENABLE, CHAT_MODEL
from ..logging_setup import log


OFF_REPLY_COOLDOWN = 600  # seconds between "chat is off" replies per channel


class ChatCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.guard = chat.SpendGuard()
        self._off_replied_at: dict = {}
        if CHAT_ENABLE:
            log.info("Chat enabled (%s); mention or DM the bot to talk to it.", CHAT_MODEL)
        else:
            log.warning("Chat is OFF: set ANTHROPIC_API_KEY (and CHAT_ENABLE=1) to turn it on.")

    # ---------------- mentions and DMs ----------------

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not self.bot.user:
            return
        if not chat.is_addressed(message, self.bot.user.id):
            return

        text = chat.strip_mention(message.content, self.bot.user.id)
        if not text:
            await message.reply("Yeah? Ask me something, or tell me what to remember.", mention_author=False)
            return
        if not CHAT_ENABLE:
            # Say so, but not on every mention: in a shared server that is spam.
            now = time.time()
            if now - self._off_replied_at.get(message.channel.id, 0.0) > OFF_REPLY_COOLDOWN:
                self._off_replied_at[message.channel.id] = now
                await message.reply("Chat is off: ANTHROPIC_API_KEY isn't set on my host.", mention_author=False)
            return

        now = time.time()
        blocked = self.guard.check(message.author.id, now)
        if blocked:
            await message.reply(blocked, mention_author=False)
            return
        # Count before the call: a burst of mentions must not all slip in
        # while the first one is still in flight.
        self.guard.note_call(message.author.id, now)

        ctx = chat.ChatContext(
            channel_id=message.channel.id,
            user_id=message.author.id,
            user_name=message.author.display_name,
            guild_id=message.guild.id if message.guild else 0,
            guild_name=message.guild.name if message.guild else "",
        )
        try:
            async with message.channel.typing():
                reply = await chat.respond(ctx, text)
        except Exception:
            log.exception("Chat failed for %s", ctx.user_name)
            reply = "Something broke on my end; it's in the logs."

        first = True
        for piece in chat.chunk_message(reply):
            if first:
                await message.reply(piece, mention_author=False)
                first = False
            else:
                await message.channel.send(piece)

    # ---------------- /memory ----------------

    # Scope is declared once on the group: decorators on subcommands are dropped at sync.
    memory = app_commands.Group(
        name="memory", description="What McCap remembers here",
        allowed_installs=app_commands.AppInstallationType(guild=True, user=True),
        allowed_contexts=app_commands.AppCommandContext(guild=True, dm_channel=True, private_channel=True),
    )

    @memory.command(name="view", description="List the notes McCap keeps for this server (or your DM)")
    async def memory_view(self, inter: discord.Interaction):
        scope = chat.scope_key(inter.guild_id, inter.user.id)
        notes = chat.notes_for(scope)
        if not notes:
            await inter.response.send_message("Nothing saved yet. @mention me and tell me what to remember.")
            return
        pieces = chat.chunk_message(f"**{len(notes)} note(s)**\n{chat.describe_notes(scope)}")
        await inter.response.send_message(pieces[0])
        for piece in pieces[1:]:
            await inter.followup.send(piece)

    @memory.command(name="forget", description="Delete one note by id")
    @app_commands.describe(note_id="The id shown in brackets by /memory view")
    async def memory_forget(self, inter: discord.Interaction, note_id: str):
        scope = chat.scope_key(inter.guild_id, inter.user.id)
        note = await chat.forget(scope, note_id.strip())
        if note:
            await inter.response.send_message(f"Forgot [{note.id}]: {note.text}")
        else:
            await inter.response.send_message("No note with that id here.", ephemeral=True)

    @memory.command(name="status", description="Chat model and today's usage")
    async def memory_status(self, inter: discord.Interaction):
        state = f"on ({CHAT_MODEL})" if CHAT_ENABLE else "off (no ANTHROPIC_API_KEY)"
        await inter.response.send_message(
            f"Chat: {state} / {self.guard.calls_today} call(s) today / "
            f"{len(chat.memory_notes)} note(s) total / {len(chat.chat_turns)} history turn(s)",
            ephemeral=True,
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(ChatCog(bot))
