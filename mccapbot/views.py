"""Shared Discord UI pieces.

Lives outside ``mccapbot.cogs`` on purpose: discord.py purges an extension's
module from ``sys.modules`` when it is unloaded, so anything imported lazily
from a cog module can silently resolve to a different object than the one a
test patched. A plain module is never purged.
"""

from typing import Optional

import discord


class ConfirmOrder(discord.ui.View):
    """A single-use confirm/cancel prompt, bound to one user."""

    def __init__(self, owner_id: int, timeout: int):
        super().__init__(timeout=timeout)
        self.owner_id = owner_id
        self.value: Optional[bool] = None

    async def interaction_check(self, inter: discord.Interaction) -> bool:
        # Buttons are visible to whoever can see the message; bind them to the
        # owner so nobody else can press Confirm.
        if inter.user.id != self.owner_id:
            await inter.response.send_message("This isn't your order.", ephemeral=True)
            return False
        return True

    # stop() runs in a finally: if editing the message fails, the waiter must
    # still wake up now. Otherwise it wakes at the timeout with value already
    # set and the order executes a minute after the click.
    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.danger)
    async def confirm(self, inter: discord.Interaction, _b: discord.ui.Button):
        self.value = True
        try:
            for child in self.children:
                child.disabled = True
            await inter.response.edit_message(view=self)
        finally:
            self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, inter: discord.Interaction, _b: discord.ui.Button):
        self.value = False
        try:
            for child in self.children:
                child.disabled = True
            await inter.response.edit_message(view=self)
        finally:
            self.stop()
