import asyncio
import os
import uuid
from datetime import datetime, timedelta, timezone

import discord
from aiohttp import web
from discord import app_commands
from supabase import create_client

TOKEN = os.environ["DISCORD_TOKEN"]
db = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_KEY"])
COOLDOWN_HOURS = int(os.getenv("COOLDOWN_HOURS", "24"))
GUILD_ID = os.getenv("GUILD_ID")

# Names of your existing male / female roles (comma-separated, case-insensitive).
MALE_ROLES = {s.strip().lower() for s in os.getenv("MALE_ROLES", "male,boy,man").split(",") if s.strip()}
FEMALE_ROLES = {s.strip().lower() for s in os.getenv("FEMALE_ROLES", "female,girl,woman").split(",") if s.strip()}

# (minimum score, key, label shown in messages) checked top to bottom
RANKS = [
    (25, "excellent", "🏆 Excellent"),
    (10, "good", "⭐ Good"),
    (0, "neutral", "🙂 Neutral"),
    (-9, "questionable", "😐 Questionable"),
    (-24, "bad", "⚠️ Bad"),
    (None, "very_bad", "🔴 Very Bad"),
]

# Roles the bot manages (must already exist in the server)
ALL_RANK_ROLES = {
    "devta", "devi", "aacha bacha", "achi bachi", "acha none of the above",
    "aam aadmi party", "nalayak", "gaddar",
}


def rank_for(score: int):
    for floor, key, label in RANKS:
        if floor is None or score >= floor:
            return key, label


def gender_of(member: discord.Member):
    names = {r.name.lower() for r in member.roles}
    female, male = bool(names & FEMALE_ROLES), bool(names & MALE_ROLES)
    if female and not male:
        return "f"
    if male and not female:
        return "m"
    return None


def role_name_for(key: str, member: discord.Member):
    g = gender_of(member)
    if key == "excellent":
        return "Devi" if g == "f" else "Devta"
    if key == "good":
        return {"f": "Achi bachi", "m": "Aacha bacha"}.get(g, "Acha none of the above")
    return {"neutral": "Aam aadmi party", "bad": "Nalayak", "very_bad": "Gaddar"}.get(key)


def fmt(n: int) -> str:
    return f"+{n}" if n > 0 else str(n)


async def q(builder):
    """Run synchronous Supabase calls off the event loop."""
    return (await asyncio.to_thread(builder.execute)).data


async def stats(gid, uid):
    rows = await q(
        db.table("guild_scores").select("*")
        .eq("guild_id", gid).eq("target_id", uid)
    )
    return rows[0] if rows else {"score": 0, "good": 0, "bad": 0}


async def sync_role(guild: discord.Guild, user_id: int, score: int):
    member = guild.get_member(user_id)
    if member is None:
        return
    key, _ = rank_for(score)
    want = role_name_for(key, member)
    role = discord.utils.find(lambda r: r.name.lower() == want.lower(), guild.roles) if want else None
    try:
        stale = [r for r in member.roles if r.name.lower() in ALL_RANK_ROLES and r != role]
        if stale:
            await member.remove_roles(*stale, reason="Rank changed")
        if role and role not in member.roles:
            await member.add_roles(role, reason="Rank changed")
    except discord.Forbidden:
        pass  # Bot needs Manage Roles and its role must sit above the rank roles.


class Bot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        intents.members = True  # Enable Server Members Intent in the Discord developer portal.
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        if GUILD_ID:
            guild = discord.Object(int(int(GUILD_ID)))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()


bot = Bot()
tree = bot.tree


@tree.command(description="Give someone +1 or -1")
@app_commands.guild_only()
@app_commands.describe(user="Who you're rating", vote="+1 or -1", reason="Why (optional)")
@app_commands.choices(vote=[
    app_commands.Choice(name="+1", value=1),
    app_commands.Choice(name="-1", value=-1),
])
async def rate(
    i: discord.Interaction,
    user: discord.Member,
    vote: app_commands.Choice[int],
    reason: app_commands.Range[str, 1, 200] = None,
):
    if user.id == i.user.id:
        return await i.response.send_message("You can't rate yourself.")
    if user.bot:
        return await i.response.send_message("Bots can't be rated.")

    await i.response.defer()
    gid, vid, tid = str(i.guild_id), str(i.user.id), str(user.id)
    since = (datetime.now(timezone.utc) - timedelta(hours=COOLDOWN_HOURS)).isoformat()
    recent = await q(
        db.table("votes").select("id")
        .eq("guild_id", gid).eq("voter_id", vid).eq("target_id", tid)
        .gte("created_at", since).eq("undone", False).limit(1)
    )
    if recent:
        return await i.edit_original_response(
            content=f"{i.user.mention}, you already rated {user.display_name} in the last {COOLDOWN_HOURS}h.",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    await q(db.table("votes").insert({
        "id": str(uuid.uuid4()),
        "guild_id": gid,
        "voter_id": vid,
        "voter_tag": str(i.user),
        "target_id": tid,
        "target_tag": str(user),
        "value": vote.value,
        "reason": reason,
        "undone": False,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }))
    s = await stats(gid, tid)
    await sync_role(i.guild, user.id, s["score"])
    why = f" — {reason}" if reason else ""
    await i.edit_original_response(
        content=f"{i.user.mention} gave {user.mention} **{fmt(vote.value)}**{why}\n"
                f"Score: {fmt(s['score'])} · {rank_for(s['score'])[1]}",
        allowed_mentions=discord.AllowedMentions.none(),
    )


@tree.command(description="Show someone's score")
@app_commands.guild_only()
async def score(i: discord.Interaction, user: discord.Member):
    s = await stats(str(i.guild_id), str(user.id))
    await i.response.send_message(
        f"**{user.display_name}**\nScore: {fmt(s['score'])}\n"
        f"Good: {s['good']} · Bad: {s['bad']}\nRank: {rank_for(s['score'])[1]}"
    )


@tree.command(description="Top (and bottom) scores in this server")
@app_commands.guild_only()
async def leaderboard(i: discord.Interaction):
    rows = await q(
        db.table("guild_scores").select("*").eq("guild_id", str(i.guild_id))
        .order("score", desc=True)
    )
    if not rows:
        return await i.response.send_message("No votes yet.")
    shown = rows[:10]
    tail = [r for r in rows[10:] if r["score"] < 0][-3:]

    def line(n, row):
        return f"{n}. <@{row['target_id']}> — {fmt(row['score'])} ({rank_for(row['score'])[1]})"

    message = "\n".join(line(n, row) for n, row in enumerate(shown, 1))
    if tail:
        start = len(rows) - len(tail) + 1
        message += "\n…\n" + "\n".join(line(start + k, row) for k, row in enumerate(tail))
    await i.response.send_message(
        "**Leaderboard**\n" + message,
        allowed_mentions=discord.AllowedMentions.none(),
    )


@tree.command(description="Recent votes for someone")
@app_commands.guild_only()
async def history(i: discord.Interaction, user: discord.Member):
    rows = await q(
        db.table("votes").select("id,value,reason,created_at")
        .eq("guild_id", str(i.guild_id)).eq("target_id", str(user.id))
        .neq("undone", True).order("created_at", desc=True).limit(10)
    )
    if not rows:
        return await i.response.send_message(f"No votes for {user.display_name} yet.")
    message = "\n".join(
        f"{fmt(row['value'])} — {row['reason'] or 'no reason'} (`{row['id'][:8]}`)"
        for row in rows
    )
    await i.response.send_message(f"**Recent votes: {user.display_name}**\n{message}")


@tree.command(description="Moderators: undo a vote by its ID (shown in /history)")
@app_commands.guild_only()
@app_commands.default_permissions(manage_messages=True)
async def undo(i: discord.Interaction, vote_id: str):
    if not i.permissions.manage_messages:
        return await i.response.send_message("Moderators only.")
    await i.response.defer()
    rows = await q(
        db.table("votes").select("id,target_id")
        .eq("guild_id", str(i.guild_id)).neq("undone", True)
        .like("id", vote_id.strip().lower() + "%").limit(2)
    )
    if len(rows) != 1:
        return await i.followup.send("No unique matching vote found.")
    vote_row = rows[0]
    await q(
        db.table("votes").update({
            "undone": True,
            "undone_at": datetime.now(timezone.utc).isoformat(),
            "undone_by": str(i.user.id),
        }).eq("id", vote_row["id"])
    )
    s = await stats(str(i.guild_id), vote_row["target_id"])
    await sync_role(i.guild, int(vote_row["target_id"]), s["score"])
    await i.followup.send(f"Vote undone. New score: {fmt(s['score'])}.")


async def main():
    app = web.Application()
    app.router.add_get("/", lambda request: web.Response(text="ok"))
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(app, "0.0.0.0", int(os.getenv("PORT", "10000"))).start()

    async with bot:
        try:
            await bot.start(TOKEN)
        except discord.HTTPException as exc:
            if exc.status == 429:
                print("Discord 429, waiting 15 min", flush=True)
                await asyncio.sleep(900)
            raise


if __name__ == "__main__":
    asyncio.run(main())
