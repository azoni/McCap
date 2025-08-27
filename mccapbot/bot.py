import asyncio, time
from typing import Optional, Dict
import discord
from discord import app_commands
from discord.ext import commands
from typing import Optional, Dict, Any, List  # NEW: Any, List
import datetime as dt                        # NEW: for window math

from .config import DONATION_WALLET, BALANCE_POLL_SECONDS
from .logging_setup import log
from .helpers import parse_mc_input, humanize
from .helpers import username_from_id
from .storage import load_reminders, load_invoices, load_alerts, reminders, invoices, alert_events, save_reminders, save_invoices
from .solana import get_solana_balance
from .alerts import watcher as alerts_watcher
from .payments import payments_watcher, solana_pay_link, qr_url, parse_asset_choice, new_invoice_id
from .tables import fixed_table, payments_table_with_users, alerts_table
from .cache import token_cache, TOKEN_CACHE_LOCK, update_cache
from .dex import fetch_dex_token, choose_consensus_pair, resolve_mc_value, build_token_url, get_image_url, table_lp, summarize_lp_venues
from .models import Reminder, Invoice
from .graduated import GraduatedCog


class Bot(commands.Bot):  # ✅ inherit from commands.Bot so add_cog/tree work
    def __init__(self):
        intents = discord.Intents.default()
        # enable if you need message content for any text commands (not required for slash commands)
        intents.message_content = False
        super().__init__(command_prefix="!", intents=intents)  # command_prefix unused for slash cmds, but required by Bot

    async def _wallet_presence_loop(self):
        await self.wait_until_ready()
        if not DONATION_WALLET:
            import logging; logging.info("No valid DONATION_WALLET set; presence loop disabled.")
            return
        while not self.is_closed():
            bal = await get_solana_balance(DONATION_WALLET)
            activity = discord.Activity(
                type=discord.ActivityType.watching,
                name=(f"💰 {bal:,.2f} SOL" if bal is not None else "💰 fetching SOL…")
            )
            try:
                await self.change_presence(status=discord.Status.online, activity=activity)
            except Exception:
                import logging; logging.exception("Failed to update presence")
            await asyncio.sleep(BALANCE_POLL_SECONDS)

    async def setup_hook(self):
        # load storage
        await load_reminders(); await load_invoices(); await load_alerts()

        # background tasks
        asyncio.create_task(self._wallet_presence_loop())
        asyncio.create_task(alerts_watcher(self))
        asyncio.create_task(payments_watcher(self))

        # 🔹 load the Graduated Cog (contains its own slash command + hourly task)
        await self.add_cog(GraduatedCog(self))

        # ------- Graduated tokens report (moved here; Cog keeps only the hourly task) -------
        @self.tree.command(name="graduated_report", description="Show graduated tokens in the past X hours")
        async def graduated_report(interaction: discord.Interaction, hours: int = 1):
            cog: GraduatedCog = interaction.client.get_cog("GraduatedCog")
            if cog is None:
                await interaction.response.send_message("⚠️ GraduatedCog not loaded.", ephemeral=True)
                return

            # floor to full hours, like the hourly task
            end = dt.datetime.now(dt.timezone.utc).replace(minute=0, second=0, microsecond=0)
            start = end - dt.timedelta(hours=hours)

            msg = await cog.fetch_report(start, end)
            await interaction.response.send_message(msg)

        # ------- LP suggestion (keeps your name change "mc_lp") -------
        @self.tree.command(name="mc_lp", description="Suggest the best LP venue (Meteora, Raydium, Pumpswap) for a token")
        @app_commands.describe(ca="Contract address / mint")
        async def mc_lp(inter: discord.Interaction, ca: str):
            await inter.response.defer(thinking=True)
            data = await fetch_dex_token(ca)
            if not data or not data.get("pairs"):
                await inter.followup.send(f"Couldn’t find pairs for `{ca}`."); return
            agg, best = summarize_lp_venues(data["pairs"], ca)
            if not agg:
                await inter.followup.send("No eligible pools found on Meteora, Raydium, or Pumpswap."); return
            table = table_lp(agg)
            if best:
                best_name, a = best
                rec_title = f"✅ Best LP venue: **{best_name.capitalize()}**"
                rec_link  = a.get("best_url"); rec_line = f"[Open pool]({rec_link})" if rec_link else ""
                notes = "Scored by liquidity + 24h volume + 24h tx count (log-weighted)."
            else:
                rec_title, rec_line, notes = "✅ Best LP venue", "", "No single venue dominated; compare metrics below."
            embed = discord.Embed(title="LP Venue Suggestion", description=f"{rec_title}\n{rec_line}\n\n{notes}", color=0x00b894)
            embed.add_field(name="Meteora / Raydium / Pumpswap (aggregated)", value=table, inline=False)
            embed.set_footer(text="Heuristic suggestion — always double-check slippage/fees for your pool.")
            await inter.followup.send(embed=embed)

        # ------- Set MC alert -------
        @self.tree.command(name="mc", description="Set a Market Cap alert (auto up/down). Optional note is included when it fires.")
        @app_commands.describe(ca="Contract address / mint", target="Target MC (e.g. 250k, 2.5m, 1b, 1t)", note="Optional message to include when the alert fires")
        async def mc(inter: discord.Interaction, ca: str, target: str, note: Optional[str] = None):
            await inter.response.defer(thinking=True)
            try:
                target_val = parse_mc_input(target)
            except Exception:
                await inter.followup.send("❌ Invalid target. Use `2500000` or shorthand like `250k`, `2.5m`, `1b`, `1t`."); return
            data = await fetch_dex_token(ca)
            if not data or not data.get("pairs"):
                await inter.followup.send(f"Couldn’t find pairs for `{ca}`."); return
            best, consensus, _ = choose_consensus_pair(data["pairs"], ca)
            if not best:
                await inter.followup.send(f"No valid pairs found for `{ca}` on allowed DEXes."); return

            base = best.get("baseToken") or {}
            name = base.get("name") or base.get("symbol") or "Token"
            symbol = base.get("symbol") or ""
            mc_now_val, src = resolve_mc_value(best, ca)
            url = build_token_url(ca, best)
            img = get_image_url(best, ca)

            await update_cache(
                ca, mc=mc_now_val, url=url, source=src, dex=best.get("dexId",""),
                chain=best.get("chainId",""), quote=((best.get("quoteToken") or {}).get("symbol") or "").upper(),
                consensus=consensus, image_url=img
            )

            if mc_now_val is None:
                direction, msg = "above", (f"⚠️ `{name}` has no reported MC/FDV yet. I’ll watch and alert when it reaches **${humanize(target_val)} MC**.")
            else:
                direction = "below" if target_val < mc_now_val else "above"
                msg = (f"⏰ Alert set for **{name} ({symbol})** — Market Cap "
                       f"{'≤' if direction=='below' else '≥'} ${humanize(target_val)} (current: ${humanize(mc_now_val)}).")
            if note:
                msg += f"\n📝 Note saved."

            reminders.append(Reminder(
                ca=ca, target_mc=float(target_val), direction=direction,
                channel_id=inter.channel_id, creator_id=inter.user.id,
                guild_id=inter.guild_id or 0, name=name, symbol=symbol, note=(note or "").strip()
            ))
            await save_reminders()
            await inter.followup.send(msg)

        # ------- List MC alerts -------
        @self.tree.command(
            name="mc_list",
            description="List this server’s Market Cap alerts (cached current MC)"
        )
        async def mc_list(
            inter: discord.Interaction,
            user: Optional[discord.User] = None,
            public: bool = True,
        ):
            await inter.response.defer(thinking=True, ephemeral=not public)

            gid = inter.guild_id or 0
            sr = [r for r in reminders if r.guild_id == gid]

            # Filter by user if provided
            if user:
                sr = [r for r in sr if r.creator_id == user.id]

            if not sr:
                await inter.followup.send(
                    "No active alerts in this server." + (f" (filtered by {user.display_name})" if user else ""),
                    ephemeral=not public
                )
                return

            # Snapshot current token cache
            async with TOKEN_CACHE_LOCK:
                snap_by_ca = {r.ca: token_cache.get(r.ca) for r in sr}

            headers = ["#", "Token", "Target", "Current", "By"]
            rows_ge, rows_le = [], []

            for i, r in enumerate(sr, 1):
                s = snap_by_ca.get(r.ca)
                curr = f"${humanize(s.mc)}" if s and s.mc is not None else "—"
                dir_sym = "≥" if r.direction == "above" else "≤"
                label = r.symbol or r.name
                row = [
                    str(i),
                    label,
                    f"{dir_sym} ${humanize(r.target_mc)}",
                    curr,
                    await username_from_id(self, r.creator_id)
                ]
                (rows_ge if r.direction == "above" else rows_le).append(row)

            from .tables import fixed_table
            filt = f" (filtered by {user.display_name})" if user else ""
            embed = discord.Embed(
                title="Market Cap Alerts",
                description=f"Cached values shown; grouped by alert direction{filt}.",
                color=0x2b90d9
            )

            if rows_ge:
                embed.add_field(
                    name="📈 Breakouts (MC ≥ target)",
                    value=fixed_table(headers, rows_ge),
                    inline=False
                )
            if rows_le:
                embed.add_field(
                    name="📉 Pullbacks (MC ≤ target)",
                    value=fixed_table(headers, rows_le),
                    inline=False
                )

            embed.set_footer(text=f"{len(sr)} alert(s){filt}")
            await inter.followup.send(embed=embed, ephemeral=not public)

        # ------- Remove MC alert -------
        @self.tree.command(name="mc_remove", description="Remove an alert by index from /mc_list")
        @app_commands.describe(index="Index shown in /mc_list")
        async def mc_remove(inter: discord.Interaction, index: int):
            await inter.response.defer(thinking=False)
            gid = inter.guild_id or 0
            sr = [r for r in reminders if r.guild_id == gid]
            if index < 1 or index > len(sr):
                await inter.followup.send("❌ Invalid index. Use `/mc_list` to see valid indices."); return
            rem = sr[index-1]
            can_manage = isinstance(inter.user, discord.Member) and inter.user.guild_permissions.manage_guild
            if inter.user.id != rem.creator_id and not can_manage:
                await inter.followup.send("❌ Only the alert creator or a user with **Manage Server** can remove this alert."); return
            reminders.remove(rem); await save_reminders()
            await inter.followup.send(f"🗑️ Removed alert #{index} for **{rem.name} ({rem.symbol})** (MC {'≥' if rem.direction=='above' else '≤'} ${humanize(rem.target_mc)}).")

        # ------- Recent fired alerts -------
        @self.tree.command(name="mc_recent", description="Show recent fired MC alerts (default last 5).")
        @app_commands.describe(count="How many to show (default 5, max 50)", user="Only alerts created by this user (optional)", public="Show to everyone (True) or only you (False)")
        async def mc_recent(inter: discord.Interaction, count: Optional[int] = 5, user: Optional[discord.User] = None, public: Optional[bool] = True):
            await inter.response.defer(thinking=True, ephemeral=not public)
            gid = inter.guild_id or 0
            evs = [e for e in alert_events if e.guild_id == gid]
            if user:
                evs = [e for e in evs if e.creator_id == user.id]
            evs.sort(key=lambda e: e.ts, reverse=True)
            n = max(1, min(int(count or 5), 50)); evs = evs[:n]
            if not evs:
                await inter.followup.send("No matching alerts found.", ephemeral=not public); return

            uids = {e.creator_id for e in evs}
            name_by_id = {uid: await username_from_id(self, uid) for uid in uids}

            async with TOKEN_CACHE_LOCK:
                current_by_ca = {e.ca: (token_cache.get(e.ca).mc if token_cache.get(e.ca) else None) for e in evs}

            table = alerts_table(evs, name_by_id, current_by_ca)
            filt = f" — by: {user.name}" if user else ""
            embed = discord.Embed(title="Recent Alerts", description=f"Most recent {len(evs)} alert(s){filt}", color=0xf39c12)
            embed.add_field(name="History", value=table, inline=False)
            await inter.followup.send(embed=embed, ephemeral=not public)

        # ------- Pay (SOL/USDC) ------- 
        @self.tree.command(name="pay", description="Pay the bot via Solana Pay (defaults to SOL).")
        @app_commands.describe(amount="Amount (e.g., 0.25)", asset="(Optional) Choose USDC if not paying in SOL", note="Optional note to include")
        @app_commands.choices(asset=[app_commands.Choice(name="SOL (default)", value="SOL"), app_commands.Choice(name="USDC", value="USDC")])
        async def pay(inter: discord.Interaction, amount: float, asset: Optional[app_commands.Choice[str]] = None, note: Optional[str] = None):
            await inter.response.defer(thinking=True, ephemeral=True)
            asset_value = (asset.value if asset else "SOL").upper()
            if not DONATION_WALLET: await inter.followup.send("⚠️ No DONATION_WALLET configured.", ephemeral=True); return
            if amount <= 0: await inter.followup.send("Enter a positive amount.", ephemeral=True); return

            asset_u, dec, mint = parse_asset_choice(asset_value)
            ref_full = __import__("mccapbot.solana", fromlist=['_random_pubkey'])._random_pubkey()
            if asset_u == "SOL":
                amount_base = __import__("mccapbot.helpers", fromlist=['to_lamports']).to_lamports(amount)
                sp_link = solana_pay_link(DONATION_WALLET, amount, label="McCap Bot", message=(note or ""), reference=ref_full)
            else:
                amount_base = int(round(amount * (10**dec)))
                sp_link = solana_pay_link(DONATION_WALLET, amount, label="McCap Bot", message=(note or ""), reference=ref_full, spl_token=mint)

            inv = Invoice(id=ref_full[:6], reference=ref_full, asset=asset_u, mint=(mint or ""), amount_base=amount_base, decimals=dec,
                          user_id=inter.user.id, channel_id=inter.channel_id, guild_id=inter.guild_id or 0,
                          note=(note or ""), created_ts=time.time())
            invoices.append(inv); await save_invoices()

            from urllib.parse import quote
            encoded = quote(sp_link, safe="")
            phantom_ul  = f"https://phantom.app/ul/v1/pay?link={encoded}"
            solflare_ul = f"https://solflare.com/ul/v1/solanaPay?link={encoded}"
            qr = __import__("mccapbot.payments", fromlist=['qr_url']).qr_url(sp_link, 260)

            desc=(f"**Phantom (browser extension):** click the button below — the extension will open.\n"
                  f"Or scan the **QR**, or use Solflare.\n\n**Amount:** {amount:,.4f} {asset_u}\n"
                  f"**Invoice ID:** `{inv.id}` (expires in {__import__('mccapbot.config', fromlist=['PAY_EXPIRY_SEC']).PAY_EXPIRY_SEC//60}m)\n\n"
                  f"**Mobile users:** you can also tap this Solana Pay link:\n`{sp_link}`")
            embed=discord.Embed(title="Solana Pay", description=desc, color=0x00b894); embed.set_image(url=qr)
            view=discord.ui.View()
            view.add_item(discord.ui.Button(label="Open in Phantom (Extension)", url=phantom_ul, style=discord.ButtonStyle.link))
            view.add_item(discord.ui.Button(label="Open in Solflare", url=solflare_ul, style=discord.ButtonStyle.link))
            await inter.followup.send(embed=embed, view=view, ephemeral=True)

        # ------- Pay status -------
        @self.tree.command(name="pay_status", description="Check status of your last payment (or by ID)")
        @app_commands.describe(invoice_id="Optional invoice id (first 6 chars shown when created)")
        async def pay_status(inter: discord.Interaction, invoice_id: Optional[str] = None):
            await inter.response.defer(thinking=True, ephemeral=True)
            gid = inter.guild_id or 0
            mine = [i for i in invoices if i.guild_id == gid and i.user_id == inter.user.id]
            if not mine: await inter.followup.send("No payments found.", ephemeral=True); return
            inv=None
            if invoice_id:
                for i in mine:
                    if i.id == invoice_id: inv=i; break
            inv = inv or max(mine, key=lambda x: x.created_ts)
            human_amt = inv.amount_base / (10**inv.decimals)
            lines=[f"**Invoice:** `{inv.id}`", f"**Asset:** {inv.asset}", f"**Amount:** {human_amt:,.4f} {inv.asset}", f"**Status:** {inv.status.upper()}"]
            if inv.tx_sig: lines.append(f"**Tx:** `{inv.tx_sig}`")
            await inter.followup.send("\n".join(lines), ephemeral=True)

        # ------- Pay list -------
        @self.tree.command(name="pay_list", description="List payments (yours by default, or server-wide with permission).")
        @app_commands.describe(scope="mine (default) or server", status="Filter by status: pending, paid, expired (optional)", limit="Max rows to display (default 15)", public="Show in channel (True) or only to you (False)")
        @app_commands.choices(
            scope=[app_commands.Choice(name="mine (default)", value="mine"), app_commands.Choice(name="server (admin only)", value="server")],
            status=[app_commands.Choice(name="pending", value="pending"), app_commands.Choice(name="paid", value="paid"), app_commands.Choice(name="expired", value="expired")]
        )
        async def pay_list(inter: discord.Interaction, scope: Optional[app_commands.Choice[str]] = None, status: Optional[app_commands.Choice[str]] = None, limit: Optional[int] = 15, public: Optional[bool] = True):
            await inter.response.defer(thinking=True, ephemeral=not public)
            gid = inter.guild_id or 0
            scope_val = (scope.value if scope else "mine")
            if scope_val == "server":
                member_ok = isinstance(inter.user, discord.Member) and (inter.user.guild_permissions.manage_guild or inter.user.guild_permissions.administrator)
                if not member_ok:
                    await inter.followup.send("❌ You need **Manage Server** to view server-wide payments.", ephemeral=not public); return
            records = [i for i in invoices if i.guild_id == gid]
            if scope_val == "mine": records = [i for i in records if i.user_id == inter.user.id]
            if status: records = [i for i in records if i.status == status.value]
            records.sort(key=lambda x: x.created_ts, reverse=True)
            records = records[: (limit or 15)]
            if not records: await inter.followup.send("No matching payments found.", ephemeral=not public); return
            unique_uids = {i.user_id for i in records}
            name_by_id: Dict[int,str] = {uid: await username_from_id(self, uid) for uid in unique_uids}
            table = payments_table_with_users(records, name_by_id)
            scope_label = "your payments" if scope_val == "mine" else "server payments"
            filt = f" — status:{status.value}" if status else ""
            embed = discord.Embed(title="Payments", description=f"{scope_label}{filt}", color=0x00b894)
            embed.add_field(name="History", value=table, inline=False)
            await inter.followup.send(embed=embed, ephemeral=not public)

        # Finally, make sure slash commands are registered
        await self.tree.sync()

    async def _sync_all_guilds_after_ready(self):
        await self.wait_until_ready()
        for g in self.guilds:
            log.info(f"Commands available in guild: {g.name} ({g.id})")

    async def on_ready(self):
        print(f"✅ Logged in as {self.user} (ID: {self.user.id})")
        try:
            synced = await self.tree.sync()
            print(f"✅ Synced {len(synced)} slash commands")
        except Exception as e:
            print(f"⚠️ Slash sync failed: {e}")
        guilds = ", ".join([f"{g.name}({g.id})" for g in self.guilds]) or "none"
        from .config import LOG_LEVEL, DEX_BLACKLIST
        log.info(f"Logged in as {self.user} | Guilds: [{guilds}] | LOG_LEVEL={LOG_LEVEL} | DEX_BLACKLIST={sorted(DEX_BLACKLIST)}")

    async def on_guild_join(self, guild: discord.Guild):
        log.info(f"Joined guild {guild.name} ({guild.id}); using global application commands only.")

    # ------- NEW: manual sync helpers -------
    @commands.command(name="sync")
    async def sync_global(self, ctx: commands.Context):
        synced = await self.tree.sync()
        await ctx.send(f"🔄 Synced {len(synced)} global slash commands.")

    @commands.command(name="sync_here")
    async def sync_guild(self, ctx: commands.Context):
        synced = await self.tree.sync(guild=ctx.guild)
        await ctx.send(f"🔄 Synced {len(synced)} commands to this guild.")
