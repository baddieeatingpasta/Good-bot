import asyncio, os, uuid
from datetime import datetime, timedelta, timezone

import discord
from aiohttp import web
from discord import app_commands
from supabase import create_client

TOKEN = os.environ["DISCORD_TOKEN"]
db = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_KEY"])
COOLDOWN_HOURS = int(os.getenv("COOLDOWN_HOURS", "24"))
GUILD_ID = os.getenv("GUILD_ID")

# (minimum score, role name) checked top to bottom
RANKS = [(25, "🏆 Excellent"), (10, "⭐ Good"), (0, "🙂 Neutral"),
         (-9, "😐 Questionable"), (-24, "⚠️ Bad"), (None, "🔴 Very Bad")]
RANK_NAMES = {n for _, n in RANKS}


def rank_for(score: int) -> str:
    for floor, name in RANKS:
        if floor is None or score >= floor:
            return name


def fmt(n: int) -> str:
    return f"+{n}" if n > 0 else str(n)


async def q(builder):
    return (await asyncio.to_thread(builder.execute)).data


async def stats(gid, uid):
    rows = await q(db.table("guild_scores").select("*").eq("guild_id", gid).eq("target_id", uid))
    return rows[0] if rows else {"score": 0, "good": 0, "bad": 0}


async def sync_role(guild: discord.Guild, user_id: int, score: int):
    member = guild.get_member(user_id)
    if member is None:
        return
    want = rank_for(score)
    try:
        role = discord.utils.get(guild.roles, name=want) or await guild.create_role(
            name=want, reason="Rating bot rank")
        stale = [r for r in member.roles if r.name in RANK_NAMES and r != role]
        if stale:
            await member.remove_roles(*stale, reason="Rank changed")
        if role not in member.roles:
            await member.add_roles(role, reason="Rank changed")
    except discord.Forbidden:
        pass  # bot needs Manage Roles and a role above the rank roles


async def cast_vote(i: discord.Interaction, target: discord.Member, value: int, reason):
    await i.response.defer()
    gid, vid, tid = str(i.guild_id), str(i.user.id), str(target.id)
    since = (datetime.now(timezone.utc) - timedelta(hours=COOLDOWN_HOURS)).isoformat()
    recent = await q(db.table("votes").select("id").eq("guild_id", gid).eq("voter_id", vid)
                     .eq("target_id", tid).gte("created_at", since).limit(1))
    if recent:
        return await i.followup.send(
            f"You already rated {target.display_name} in the last {COOLDOWN_HOURS}h.", ephemeral=True)
    await q(db.table("votes").insert({
        "id": str(uuid.uuid4()), "guild_id": gid,
        "voter_id": vid, "voter_tag": str(i.user),
        "target_id": tid, "target_tag": str(target),
        "value": value, "reason": reason, "undone": False,
        "created_at": datetime.now(timezone.utc).isoformat()}))
    s = await stats(gid, tid)
    await sync_role(i.guild, target.id, s["score"])
    why = f" — {reason}" if reason else ""
    await i.followup.send(
        f"{i.user.mention} gave {target.mention} **{fmt(value)}**{why}\n"
        f"Score: {fmt(s['score'])} · {rank_for(s['score'])}",
        allowed_mentions=discord.AllowedMentions.none())


class ReasonModal(discord.ui.Modal, title="Why? (optional)"):
    reason = discord.ui.TextInput(label="Reason", required=False, max_length=200)

    def __init__(self, target, value):
        super().__init__()
        self.target, self.value = target, value

    async def on_submit(self, i: discord.Interaction):
        await cast_vote(i, self.target, self.value, self.reason.value.strip() or None)


class RateView(discord.ui.View):
    def __init__(self, target):
        super().__init__(timeout=120)
        self.target = target

    @discord.ui.button(label="+1", style=discord.ButtonStyle.success)
    async def plus(self, i, _):
        await i.response.send_modal(ReasonModal(self.target, 1))

    @discord.ui.button(label="-1", style=discord.ButtonStyle.danger)
    async def minus(self, i, _):
        await i.response.send_modal(ReasonModal(self.target, -1))


class Bot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        intents.members = True  # enable "Server Members Intent" in the dev portal
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        app = web.Application()
        app.router.add_get("/", lambda r: web.Response(text="ok"))
        runner = web.AppRunner(app)
        await runner.setup()
        await web.TCPSite(runner, "0.0.0.0", int(os.getenv("PORT", "10000"))).start()
        if GUILD_ID:
            g = discord.Object(int(GUILD_ID))
            self.tree.copy_global_to(guild=g)
            await self.tree.sync(guild=g)
        else:
            await self.tree.sync()


bot = Bot()
tree = bot.tree


@tree.command(description="Give someone +1 or -1")
@app_commands.guild_only()
async def rate(i: discord.Interaction, user: discord.Member):
    if user.id == i.user.id:
        return await i.response.send_message("You can't rate yourself.", ephemeral=True)
    if user.bot:
        return await i.response.send_message("Bots can't be rated.", ephemeral=True)
    await i.response.send_message(f"Rate {user.mention}:", view=RateView(user), ephemeral=True,
                                  allowed_mentions=discord.AllowedMentions.none())


@tree.command(description="Show someone's score")
@app_commands.guild_only()
async def score(i: discord.Interaction, user: discord.Member):
    s = await stats(str(i.guild_id), str(user.id))
    await i.response.send_message(
        f"**{user.display_name}**\nScore: {fmt(s['score'])}\n"
        f"Good: {s['good']} · Bad: {s['bad']}\nRank: {rank_for(s['score'])}")


@tree.command(description="Top (and bottom) scores in this server")
@app_commands.guild_only()
async def leaderboard(i: discord.Interaction):
    rows = await q(db.table("guild_scores").select("*").eq("guild_id", str(i.guild_id))
                   .order("score", desc=True))
    if not rows:
        return await i.response.send_message("No votes yet.")
    shown = rows[:10]
    tail = [r for r in rows[10:] if r["score"] < 0][-3:]
    line = lambda n, r: f"{n}. <@{r['target_id']}> — {fmt(r['score'])} ({rank_for(r['score'])})"
    text = "\n".join(line(n, r) for n, r in enumerate(shown, 1))
    if tail:
        start = len(rows) - len(tail) + 1
        text += "\n…\n" + "\n".join(line(start + k, r) for k, r in enumerate(tail))
    await i.response.send_message("**Leaderboard**\n" + text,
                                  allowed_mentions=discord.AllowedMentions.none())


@tree.command(description="Recent votes for someone")
@app_commands.guild_only()
async def history(i: discord.Interaction, user: discord.Member):
    rows = await q(db.table("votes").select("id,value,reason,created_at")
                   .eq("guild_id", str(i.guild_id)).eq("target_id", str(user.id))
                   .neq("undone", True).order("created_at", desc=True).limit(10))
    if not rows:
        return await i.response.send_message(f"No votes for {user.display_name} yet.")
    text = "\n".join(f"{fmt(r['value'])} — {r['reason'] or 'no reason'} (`{r['id'][:8]}`)" for r in rows)
    await i.response.send_message(f"**Recent votes: {user.display_name}**\n{text}")


@tree.command(description="Moderators: undo a vote by its ID (shown in /history)")
@app_commands.guild_only()
@app_commands.default_permissions(manage_messages=True)
async def undo(i: discord.Interaction, vote_id: str):
    if not i.permissions.manage_messages:
        return await i.response.send_message("Moderators only.", ephemeral=True)
    await i.response.defer(ephemeral=True)
    rows = await q(db.table("votes").select("id,target_id").eq("guild_id", str(i.guild_id))
                   .neq("undone", True).like("id", vote_id.strip().lower() + "%").limit(2))
    if len(rows) != 1:
        return await i.followup.send("No unique matching vote found.")
    v = rows[0]
    await q(db.table("votes").update({"undone": True, "undone_at": datetime.now(timezone.utc).isoformat(),
                                      "undone_by": str(i.user.id)}).eq("id", v["id"]))
    s = await stats(str(i.guild_id), v["target_id"])
    await sync_role(i.guild, int(v["target_id"]), s["score"])
    await i.followup.send(f"Vote undone. New score: {fmt(s['score'])}.")


bot.run(TOKEN)
