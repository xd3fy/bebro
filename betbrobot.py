import os

TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN env var not found!")



import asyncio
import random
import string
import zipfile
import os
from datetime import datetime, timedelta

import discord
from discord.ext import commands, tasks
from discord import app_commands
import asyncpg
from aiohttp import web

# ---------- Configuration ----------
GUILD_ID = 1359204211224744106
LOG_CHANNEL_ID = 1359211270099829038
MOD_RESULTS_CHANNEL_ID = 1359211291612545215
CONFIRM_CHANNEL_ID = 1360200193282412625
LEADERBOARD_CHANNEL_ID = 1363552844799676617

# Commission: 5% fee on total pot
COMMISSION_RATE = 0.05

DB_CONFIG = {
  "user": os.environ["DB_USER"],
  "password": os.environ["DB_PASSWORD"],
  "database": os.environ["DB_NAME"],
  "host": os.environ["DB_HOST"],
  "port": int(os.environ["DB_PORT"])
}


# ---------- Bot & DB Pool ----------
intents = discord.Intents.all()
bot = commands.Bot(command_prefix="!", intents=intents)
db_pool: asyncpg.Pool = None

# ---------- Helpers ----------
def generate_wager_id():
    return "WGR-" + "".join(random.choices(string.ascii_uppercase + string.digits, k=6))

def get_stats_rank(wins: int, coins: float) -> str:
    if wins >= 25 and coins >= 10000:
        return "Legend"
    if coins >= 5000:
        return "High Roller"
    if wins >= 10:
        return "Hustler"
    return "Rookie"

# ---------- Events ----------
@bot.event
async def on_ready():
    global db_pool
    db_pool = await asyncpg.create_pool(**DB_CONFIG)
    periodic_reminders.start()
    await bot.tree.sync(guild=discord.Object(id=GUILD_ID))
    print(f"Logged in as {bot.user}")

# ---------- Role-Rank Parser (/rank-logs) ----------
@bot.event
async def on_message(message: discord.Message):
    await bot.process_commands(message)
    if message.channel.name == "rank-logs" and not message.author.bot:
        import re
        pattern = (r"<@!?(\d+)>\s+(N/A|R[1-9]|R10)(?:\s+(low|mid|high))?\s+to\s+(N/A|R[1-9]|R10)(?:\s+(low|mid|high))?")
        m = re.match(pattern, message.content, re.IGNORECASE)
        if m:
            uid, pr, pt, nr, nt = m.groups()
            pr, nr = pr.upper(), nr.upper()
            pt, nt = (pt or "none").lower(), (nt or "none").lower()
            member = message.guild.get_member(int(uid))
            async with db_pool.acquire() as conn:
                rec = await conn.fetchrow('SELECT rank,tier FROM user_ranks WHERE user_id=$1', int(uid))
                cur_rank, cur_tier = (rec['rank'], rec['tier']) if rec else ('N/A','none')
                if cur_rank!=pr or cur_tier!=pt:
                    return await message.channel.send(f"❌ <@{uid}> has {cur_rank} {cur_tier}, not {pr} {pt}.")
                # update Discord roles
                if pr!='N/A':
                    old_r = discord.utils.get(message.guild.roles,name=pr)
                    old_t = discord.utils.get(message.guild.roles,name=pt)
                    await member.remove_roles(*(r for r in (old_r,old_t) if r))
                if nr!='N/A':
                    new_r = discord.utils.get(message.guild.roles,name=nr)
                    new_t = discord.utils.get(message.guild.roles,name=nt)
                    await member.add_roles(*(r for r in (new_r,new_t) if r))
                # persist to DB
                await conn.execute(
                    'INSERT INTO user_ranks(user_id,rank,tier) VALUES($1,$2,$3) '
                    'ON CONFLICT(user_id) DO UPDATE SET rank=$2,tier=$3', int(uid), nr, nt
                )
                await message.channel.send(f"✅ Updated <@{uid}> to {nr} {nt}.")

# ---------- Periodic Reminders ----------
@tasks.loop(minutes=30)
async def periodic_reminders():
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT w.wager_id,w.p1_id,w.p2_id,w.amount_usd FROM wagers w "
            "JOIN payments p ON w.wager_id=p.wager_id "
            "WHERE w.status='pending' GROUP BY w.wager_id,w.p1_id,w.p2_id,w.amount_usd "
            "HAVING bool_and(p.paid)=FALSE"
        )
        for r in rows:
            for uid in (r['p1_id'],r['p2_id']):
                user = bot.get_user(uid)
                if user:
                    await user.send(f"Reminder: complete payment of ${r['amount_usd']:.2f} for wager {r['wager_id']}")

# ---------- PayPal IPN Webhook ----------
async def handle_ipn(request):
    data = await request.post()
    if data.get('payment_status') == 'Completed':
        wid = data.get('invoice')
        pid = int(data.get('custom', 0))
        async with db_pool.acquire() as conn:
            await conn.execute('UPDATE payments SET paid=TRUE WHERE wager_id=$1 AND user_id=$2', wid, pid)
            cnt = await conn.fetchval('SELECT COUNT(*) FROM payments WHERE wager_id=$1 AND paid=TRUE', wid)
            if cnt == 2:
                await conn.execute("UPDATE wagers SET status='paid' WHERE wager_id=$1", wid)
                ch = bot.get_channel(CONFIRM_CHANNEL_ID)
                if ch:
                    await ch.send(f"💵 Wager {wid} funded. Match on!")
    return web.Response(status=200)

app = web.Application()
app.router.add_post('/paypal/ipn', handle_ipn)

# ---------- Commands ----------

# 1. /wager (unsupervised)
@bot.tree.command(name="wager", description="Start a risk wager.")
@app_commands.describe(opponent="Opponent", amount="USD amount", link="Game link")
async def wager_cmd(interaction: discord.Interaction, opponent: discord.Member, amount: float, link: str):
    wid = generate_wager_id()
    async with db_pool.acquire() as conn:
        await conn.execute(
            'INSERT INTO wagers(wager_id,host_id,p1_id,p2_id,amount_usd,is_supervised,status) VALUES($1,$2,$3,$4,$5,$6,$7)',
            wid, interaction.user.id, interaction.user.id, opponent.id, amount, False, 'pending'
        )
        await conn.executemany('INSERT INTO payments(wager_id,user_id) VALUES($1,$2)', [(wid, interaction.user.id), (wid, opponent.id)])
    embed = discord.Embed(title="Risk Wager Invite", color=discord.Color.blurple())
    embed.add_field(name="Wager ID", value=wid)
    embed.add_field(name="Challenger", value=interaction.user.mention)
    embed.add_field(name="Opponent", value=opponent.mention)
    embed.add_field(name="Amount (USD)", value=f"${amount:.2f}")
    embed.add_field(name="Game Link", value=link)
    await interaction.response.send_message("Invite sent! Awaiting confirmation.", ephemeral=True)
    view = RiskConfirm(wid, opponent.id)
    await opponent.send(embed=embed, view=view)

class RiskConfirm(discord.ui.View):
    def __init__(self, wid, pid):
        super().__init__(timeout=300)
        self.wid = wid
        self.pid = pid

    @discord.ui.button(label="Accept", style=discord.ButtonStyle.success)
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.pid:
            return await interaction.response.send_message("Not for you.", ephemeral=True)
        async with db_pool.acquire() as conn:
            await conn.execute('UPDATE payments SET paid=TRUE WHERE wager_id=$1 AND user_id=$2', self.wid, self.pid)
        await interaction.response.send_message("Accepted. Use /confirmwager.", ephemeral=True)

# 2. /confirmwager
@bot.tree.command(name="confirmwager", description="Confirm risk wager funding.")
async def confirmwager(interaction: discord.Interaction, wager_id: str):
    async with db_pool.acquire() as conn:
        cnt = await conn.fetchval('SELECT COUNT(*) FROM payments WHERE wager_id=$1 AND paid=TRUE', wager_id)
        if cnt == 2:
            await conn.execute('UPDATE wagers SET status=$1 WHERE wager_id=$2', 'paid', wager_id)
            c = bot.get_channel(CONFIRM_CHANNEL_ID)
            if c:
                await c.send(f"💵 Risk wager {wager_id} funded!")
            await interaction.response.send_message("Confirmed!", ephemeral=True)
        else:
            await interaction.response.send_message("Waiting on payments.", ephemeral=True)

# 3. /wagermod (single PayPal link)
@bot.tree.command(name="wagermod", description="Create a supervised wager (mod only) with a single PayPal link.")
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.describe(
    player1="First player",
    player2="Second player",
    amount="USD amount",
    vod="VOD required? (yes/no)",
    paypal_link="PayPal link for both players"
)
async def wagermod(
    interaction: discord.Interaction,
    player1: discord.Member,
    player2: discord.Member,
    amount: float,
    vod: str,
    paypal_link: str
):
    wid = generate_wager_id()
    vod_req = vod.lower() == "yes"
    total_pot = amount * 2
    commission = round(total_pot * COMMISSION_RATE, 2)

    async with db_pool.acquire() as conn:
        await conn.execute(
            'INSERT INTO wagers(wager_id,host_id,p1_id,p2_id,amount_usd,is_supervised,vod_required,mod_id,status,paypal_link_p1,commission) '
            'VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)',
            wid, interaction.user.id, player1.id, player2.id,
            amount, True, vod_req, interaction.user.id, 'pending', paypal_link, commission
        )
        await conn.executemany('INSERT INTO payments(wager_id,user_id) VALUES($1,$2)', [(wid,player1.id),(wid,player2.id)])

    embed = discord.Embed(title="🕵️ Supervised Wager Created", color=discord.Color.orange())
    embed.add_field(name="Wager ID", value=wid, inline=False)
    embed.add_field(name="Amount (each)", value=f"${amount:.2f}", inline=True)
    embed.add_field(name="Total Pot", value=f"${total_pot:.2f}", inline=True)
    embed.add_field(name="Commission (5%)", value=f"${commission:.2f}", inline=True)
    embed.add_field(name="VOD Required", value="Yes" if vod_req else "No", inline=False)
    embed.add_field(name="PayPal Link", value=paypal_link, inline=False)
    embed.set_footer(text="Moderator must confirm payments to start match.")

    await interaction.response.send_message("Wager created and posted.", ephemeral=True)
    ch = bot.get_channel(CONFIRM_CHANNEL_ID)
    if ch:
        await ch.send(embed=embed)

# 4. /confirmpayment (mod confirms)
@bot.tree.command(name="confirmpayment", description="Confirm a player's PayPal payment.")
@app_commands.checks.has_permissions(manage_guild=True)
async def confirmpayment(interaction: discord.Interaction, wid: str, player: discord.Member):
    async with db_pool.acquire() as conn:
        await conn.execute('UPDATE payments SET paid=TRUE WHERE wager_id=$1 AND user_id=$2', wid, player.id)
        cnt = await conn.fetchval('SELECT COUNT(*) FROM payments WHERE wager_id=$1 AND paid=TRUE', wid)
        if cnt == 2:
            await conn.execute('UPDATE wagers SET status=$1 WHERE wager_id=$2','paid',wid)
            c=bot.get_channel(CONFIRM_CHANNEL_ID)
            if c: await c.send(f"💵 Wager {wid} funded!")
    await interaction.response.send_message(f"{player.mention} confirmed.", ephemeral=True)

# 5. /resolve (risk)
@bot.tree.command(name="resolve", description="Resolve an unsupervised wager.")
@app_commands.describe(wid="Wager ID", winner="Winner", score="Score")
async def resolve(interaction: discord.Interaction, wid: str, winner: discord.Member, score: str):
    async with db_pool.acquire() as conn:
        d = await conn.fetchrow('SELECT * FROM wagers WHERE wager_id=$1', wid)
        if not d or d['is_supervised']:
            return await interaction.response.send_message('Invalid risk wager ID.', ephemeral=True)
        await conn.execute('UPDATE wagers SET status=$1 WHERE wager_id=$2','resolved',wid)
        pot = d['amount_usd']*2
        commission = d.get('commission',round(pot*COMMISSION_RATE,2))
        payout=round(pot-commission,2)
        await conn.execute('UPDATE users SET wins=wins+1,coins=coins+$1 WHERE user_id=$2',d['amount_usd'],winner.id)
        loser=d['p1_id'] if d['p2_id']==winner.id else d['p2_id']
        await conn.execute('UPDATE users SET losses=losses+1 WHERE user_id=$1',loser)
    embed=discord.Embed(title="📜 Wager Resolved", color=discord.Color.gold())
    embed.add_field(name="Wager ID",value=wid,inline=False)
    embed.add_field(name="Winner",value=winner.mention,inline=True)
    embed.add_field(name="Score",value=score,inline=True)
    embed.add_field(name="Total Pot",value=f"${pot:.2f}",inline=False)
    embed.add_field(name="Commission (5%)",value=f"${commission:.2f}",inline=False)
    embed.add_field(name="Payout",value=f"${payout:.2f}",inline=False)
    lc=bot.get_channel(LOG_CHANNEL_ID)
    if lc: await lc.send(embed=embed)
    await interaction.response.send_message("Resolved and payout shown.", ephemeral=True)

# 6. /resolvemod (supervised)
@bot.tree.command(name="resolvemod", description="Resolve a supervised wager.")
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.describe(wid="Wager ID", winner="Winner", score="Score")
async def resolvemod(interaction: discord.Interaction, wid: str, winner: discord.Member, score: str):
    async with db_pool.acquire() as conn:
        d = await conn.fetchrow('SELECT * FROM wagers WHERE wager_id=$1', wid)
        if not d or not d['is_supervised']:
            return await interaction.response.send_message('Invalid supervised wager ID.', ephemeral=True)
        await conn.execute('UPDATE wagers SET status=$1 WHERE wager_id=$2','resolved',wid)
        pot = d['amount_usd']*2
        commission = d.get('commission',round(pot*COMMISSION_RATE,2))
        payout=round(pot-commission,2)
        await conn.execute('UPDATE users SET wins=wins+1,coins=coins+$1 WHERE user_id=$2',d['amount_usd'],winner.id)
        loser=d['p1_id'] if d['p2_id']==winner.id else d['p2_id']
        await conn.execute('UPDATE users SET losses=losses+1 WHERE user_id=$1',loser)
    embed=discord.Embed(title="🎯 Supervised Wager Resolved", color=discord.Color.green())
    embed.add_field(name="Wager ID",value=wid,inline=False)
    embed.add_field(name="Winner",value=winner.mention,inline=True)
    embed.add_field(name="Score",value=score,inline=True)
    embed.add_field(name="Total Pot",value=f"${pot:.2f}",inline=False)
    embed.add_field(name="Commission (5%)",value=f"${commission:.2f}",inline=False)
    embed.add_field(name="Payout",value=f"${payout:.2f}",inline=False)
    mch=bot.get_channel(MOD_RESULTS_CHANNEL_ID)
    if mch: await mch.send(embed=embed)
    await interaction.response.send_message("Mod resolved and payout shown.", ephemeral=True)

# 7. /dispute
@bot.tree.command(name="dispute", description="Flag a wager for review.")
async def dispute(interaction: discord.Interaction, wid: str):
    mch = bot.get_channel(MOD_RESULTS_CHANNEL_ID)
    if mch: await mch.send(f"⚠️ Dispute opened for {wid} by {interaction.user.mention}")
    await interaction.response.send_message('Dispute flagged.', ephemeral=True)

# 8. /profile
@bot.tree.command(name="profile", description="View a user's profile.")
@app_commands.describe(user="Optional user")
async def profile(interaction: discord.Interaction, user: discord.User=None):
    u = user or interaction.user
    async with db_pool.acquire() as conn:
        st = await conn.fetchrow('SELECT wins,losses,coins,stats_rank FROM users WHERE user_id=$1',u.id)
        rt = await conn.fetchrow('SELECT rank,tier FROM user_ranks WHERE user_id=$1',u.id)
    embed=discord.Embed(title=f"{u.display_name}'s Profile", color=discord.Color.blue())
    embed.set_thumbnail(url=u.display_avatar.url)
    embed.add_field(name="Role Rank", value=f"{rt['rank']} {rt['tier'].capitalize()}")
    embed.add_field(name="Stats Rank", value=st['stats_rank'], inline=True)
    embed.add_field(name="Wins", value=str(st['wins']), inline=True)
    embed.add_field(name="Losses", value=str(st['losses']), inline=True)
    embed.add_field(name="Coins", value=f"${st['coins']:.2f}", inline=True)
    await interaction.response.send_message(embed=embed)

# 9. /leaderboard
@bot.tree.command(name="leaderboard", description="Show role or stats leaderboard.")
@app_commands.describe(type="'role' or 'stats'")
async def leaderboard(interaction: discord.Interaction, type: str):
    type = type.lower()
    embed = discord.Embed(color=discord.Color.gold())
    async with db_pool.acquire() as conn:
        if type == 'role':
            embed.title = "🏅 Role Leaderboard"
            rows = await conn.fetch(
                "SELECT user_id,rank,tier FROM user_ranks "
                "ORDER BY (CASE rank WHEN 'N/A' THEN 0 ELSE CAST(SUBSTRING(rank,2) AS INT) END) DESC, "
                "(CASE tier WHEN 'none' THEN 0 WHEN 'low' THEN 1 WHEN 'mid' THEN 2 ELSE 3 END) DESC LIMIT 5"
            )
            for i, r in enumerate(rows,1):
                u = await bot.fetch_user(r['user_id'])
                embed.add_field(name=f"{i}. {u.display_name}", value=f"{r['rank']} {r['tier'].capitalize()}", inline=False)
        elif type == 'stats':
            embed.title = "🏆 Stats Leaderboard"
            rows = await conn.fetch(
                "SELECT user_id,coins,wins,stats_rank FROM users "
                "ORDER BY coins DESC LIMIT 10"
            )
            for i, r in enumerate(rows,1):
                u = await bot.fetch_user(r['user_id'])
                embed.add_field(name=f"{i}. {u.display_name}", value=f"${r['coins']:.2f}, {r['wins']} wins ({r['stats_rank']})", inline=False)
        else:
            return await interaction.response.send_message("❌ Invalid type.", ephemeral=True)
    await bot.get_channel(LEADERBOARD_CHANNEL_ID).send(embed=embed)
    await interaction.response.send_message(f"✅ {type.capitalize()} leaderboard posted.", ephemeral=True)

# ---------- Run ----------
def main():
    loop = asyncio.get_event_loop()
    runner = web.AppRunner(app)
    loop.run_until_complete(runner.setup())
    site = web.TCPSite(runner, '0.0.0.0', 8080)
    loop.run_until_complete(site.start())
    loop.create_task(bot.start(TOKEN))
    loop.run_forever()

if __name__ == '__main__':
    main()
    
bot.run(TOKEN)
