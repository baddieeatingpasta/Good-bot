import asyncio, os, signal, uuid
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
RANKS = [(25, "excellent", "🏆 Excellent"), (10, "good", "⭐ Good"), (0, "neutral", "🙂 Neutral"),
         (-9, "questionable", "😐 Questionable"), (-24, "bad", "⚠️ Bad"), (None, "very_bad", "🔴 Very Bad")]

# Roles the bot manages (must already exist in the server)
ALL_RANK_ROLES = {"devta", "devi", "aacha bacha", "achi bachi", "acha none of the above",
                  "aam aadmi party", "nalayak", "gaddar"}


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
    return {"neutral": "Aam aadmi party", "bad": "Nalayak", "very_bad": "Gaddar"}.get(key)  # questionable: no role


def fmt(n: int) -> str:
    return f"+{n}" if n > 0 else str(n)


async def q(builder):
    # Time out instead of hanging forever (e.g. paused/unreachable Supabase)
    return (await asyncio.wait_for(asyncio.to_thread(builder.execute), timeout=15)).data


# ---------------------------------------------------------------- RAM cache
# Votes are applied to memory instantly and written to Supabase in the background.
scores = {}        # (guild_id, user_id) -> {"score", "good", "bad"}
last_vote = {}     # (guild_id, voter_id, target_id) -> datetime of last vote (for cooldown)
pending = []       # vote rows not yet saved to Supabase
flush_lock = asyncio.Lock()
cache_ready = False


def parse_ts(v):
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return datetime.now(timezone.utc)  # be strict if we can't parse


async def fetch_all(make):
    out, start = [], 0
    while True:
        chunk = await q(make().range(start, start + 999))
        out += chunk
        if len(chunk) < 1000:
            return out
        start += 1000


async def load_cache():
    """Preload scores + recent votes so commands never wait on the database."""
    global cache_ready
    delay = 5
    while True:
        try:
            since = (datetime.now(timezone.utc) - timedelta(hours=COOLDOWN_HOURS)).isoformat()
            votes = await fetch_all(lambda: db.table("votes").select("guild_id,voter_id,target_id,created_at")
                                    .gte("created_at", since))
            for r in votes:
                k = (r["guild_id"], r["voter_id"], r["target_id"])
                t = parse_ts(r["created_at"])
                if k not in last_vote or t > last_vote[k]:
                    last_vote[k] = t
            rows = await fetch_all(lambda: db.table("guild_scores").select("*"))
            for r in rows:  # never overwrite newer in-memory values
                scores.setdefault((r["guild_id"], r["target_id"]),
                                  {"score": r["score"], "good": r["good"], "bad": r["bad"]})
            cache_ready = True
            print(f"Cache loaded: {len(rows)} scores, {len(votes)} recent votes", flush=True)
            return
        except Exception as e:
            print(f"Cache load failed ({e!r}), retrying in {delay}s", flush=True)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60)


async def drain():
    async with flush_lock:
        while pending:
            batch = pending[:50]
            # upsert by id = safe to retry (no duplicates, never overwrites a later undo)
            await q(db.table("votes").upsert(batch, on_conflict="id", ignore_duplicates=True))
            del pending[:len(batch)]


async def flush_now(timeout=10):
    """Push pending votes right now (used before reads that hit the database)."""
    try:
        await asyncio.wait_for(drain(), timeout)
    except Exception as e:
        print(f"Flush failed: {e!r}", flush=True)


async def flusher():
    delay = 2
    while True:
        await asyncio.sleep(delay)
        if not pending:
            delay = 2
            continue
        try:
            await drain()
            delay = 2
        except Exception as e:
            print(f"Background save failed ({e!r}), {len(pending)} vote(s) queued, retrying", flush=True)
            delay = min(delay * 2, 60)


async def stats(gid, uid):
    cached = scores.get((gid, uid))
    if cached is not None:
        return cached
    rows = await q(db.table("guild_scores").select("*").eq("guild_id", gid).eq("target_id", uid))
    if rows:
        return scores.setdefault((gid, uid), {"score": rows[0]["score"], "good": rows[0]["good"],
                                              "bad": rows[0]["bad"]})
    return {"score": 0, "good": 0, "bad": 0}  # not cached until they actually get a vote


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
        pass  # bot needs Manage Roles and its role must sit above the rank roles


class Bot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        intents.members = True  # "Server Members Intent" must be on in the dev portal
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        if GUILD_ID:
            g = discord.Object(int(GUILD_ID))
            self.tree.copy_global_to(guild=g)
            await self.tree.sync(guild=g)
        else:
            await self.tree.sync()


bot = Bot()
tree = bot.tree


@tree.error
async def on_app_command_error(i: discord.Interaction, error: app_commands.AppCommandError):
    # Any crash after defer() used to leave "Good bot is thinking..." forever.
    print(f"Command error in /{i.command.name if i.command else '?'}: {error!r}", flush=True)
    msg = "Something went wrong (database problem or timeout). Please try again in a moment."
    try:
        if i.response.is_done():
            await i.edit_original_response(content=msg)
        else:
            await i.response.send_message(msg)
    except discord.HTTPException:
        try:
            await i.followup.send(msg)
        except discord.HTTPException:
            pass


@tree.command(description="Give someone +1 or -1")
@app_commands.guild_only()
@app_commands.describe(user="Who you're rating", vote="+1 or -1", reason="Why (optional)")
@app_commands.choices(vote=[app_commands.Choice(name="+1", value=1), app_commands.Choice(name="-1", value=-1)])
async def rate(i: discord.Interaction, user: discord.Member, vote: app_commands.Choice[int],
               reason: app_commands.Range[str, 1, 200] = None):
    if user.id == i.user.id:
        return await i.response.send_message("You can't rate yourself.")
    if user.bot:
        return await i.response.send_message("Bots can't be rated.")
    await i.response.defer()  # public reply
    gid, vid, tid = str(i.guild_id), str(i.user.id), str(user.id)
    now = datetime.now(timezone.utc)
    ck = (gid, vid, tid)
    last = last_vote.get(ck)
    if last is None and not cache_ready:  # cache still loading: fall back to the database
        since = (now - timedelta(hours=COOLDOWN_HOURS)).isoformat()
        recent = await q(db.table("votes").select("created_at").eq("guild_id", gid).eq("voter_id", vid)
                         .eq("target_id", tid).gte("created_at", since).limit(1))
        if recent or any(p["guild_id"] == gid and p["voter_id"] == vid and p["target_id"] == tid
                         for p in pending):
            last = now
    if last and now - last < timedelta(hours=COOLDOWN_HOURS):
        return await i.edit_original_response(
            content=f"{i.user.mention}, you already rated {user.display_name} in the last {COOLDOWN_HOURS}h.",
            allowed_mentions=discord.AllowedMentions.none())
    last_vote[ck] = now  # block double-submits immediately
    s = await stats(gid, tid)
    scores[(gid, tid)] = s
    s["score"] += vote.value
    s["good" if vote.value > 0 else "bad"] += 1
    pending.append({
        "id": str(uuid.uuid4()), "guild_id": gid,
        "voter_id": vid, "voter_tag": str(i.user),
        "target_id": tid, "target_tag": str(user),
        "value": vote.value, "reason": reason, "undone": False,
        "created_at": now.isoformat()})
    why = f" — {reason}" if reason else ""
    await i.edit_original_response(
        content=f"{i.user.mention} gave {user.mention} **{fmt(vote.value)}**{why}\n"
                f"Score: {fmt(s['score'])} · {rank_for(s['score'])[1]}",
        allowed_mentions=discord.AllowedMentions.none())
    await sync_role(i.guild, user.id, s["score"])


@tree.command(description="Show someone's score")
@app_commands.guild_only()
async def score(i: discord.Interaction, user: discord.Member):
    s = await stats(str(i.guild_id), str(user.id))
    await i.response.send_message(
        f"**{user.display_name}**\nScore: {fmt(s['score'])}\n"
        f"Good: {s['good']} · Bad: {s['bad']}\nRank: {rank_for(s['score'])[1]}")


@tree.command(description="Top (and bottom) scores in this server")
@app_commands.guild_only()
async def leaderboard(i: discord.Interaction):
    gid = str(i.guild_id)
    if cache_ready:
        rows = sorted(({"target_id": u, **v} for (g, u), v in scores.items() if g == gid),
                      key=lambda r: r["score"], reverse=True)
    else:
        rows = await q(db.table("guild_scores").select("*").eq("guild_id", gid).order("score", desc=True))
    if not rows:
        return await i.response.send_message("No votes yet.")
    shown = rows[:10]
    tail = [r for r in rows[10:] if r["score"] < 0][-3:]
    line = lambda n, r: f"{n}. <@{r['target_id']}> — {fmt(r['score'])} ({rank_for(r['score'])[1]})"
    text = "\n".join(line(n, r) for n, r in enumerate(shown, 1))
    if tail:
        start = len(rows) - len(tail) + 1
        text += "\n…\n" + "\n".join(line(start + k, r) for k, r in enumerate(tail))
    await i.response.send_message("**Leaderboard**\n" + text,
                                  allowed_mentions=discord.AllowedMentions.none())


@tree.command(description="Recent votes for someone")
@app_commands.guild_only()
async def history(i: discord.Interaction, user: discord.Member):
    await i.response.defer()
    await flush_now()
    rows = await q(db.table("votes").select("id,value,reason,created_at")
                   .eq("guild_id", str(i.guild_id)).eq("target_id", str(user.id))
                   .neq("undone", True).order("created_at", desc=True).limit(10))
    if not rows:
        return await i.edit_original_response(content=f"No votes for {user.display_name} yet.")
    text = "\n".join(f"{fmt(r['value'])} — {r['reason'] or 'no reason'} (`{r['id'][:8]}`)" for r in rows)
    await i.edit_original_response(content=f"**Recent votes: {user.display_name}**\n{text}")


@tree.command(description="Moderators: undo a vote by its ID (shown in /history)")
@app_commands.guild_only()
@app_commands.default_permissions(manage_messages=True)
async def undo(i: discord.Interaction, vote_id: str):
    if not i.permissions.manage_messages:
        return await i.response.send_message("Moderators only.")
    await i.response.defer()
    await flush_now()
    rows = await q(db.table("votes").select("id,target_id").eq("guild_id", str(i.guild_id))
                   .neq("undone", True).like("id", vote_id.strip().lower() + "%").limit(2))
    if len(rows) != 1:
        return await i.followup.send("No unique matching vote found.")
    v = rows[0]
    await q(db.table("votes").update({"undone": True, "undone_at": datetime.now(timezone.utc).isoformat(),
                                      "undone_by": str(i.user.id)}).eq("id", v["id"]))
    scores.pop((str(i.guild_id), v["target_id"]), None)  # reload fresh numbers from the database
    s = await stats(str(i.guild_id), v["target_id"])
    await sync_role(i.guild, int(v["target_id"]), s["score"])
    await i.followup.send(f"Vote undone. New score: {fmt(s['score'])}.")


async def run_bot():
    try:
        await bot.start(TOKEN)
    except discord.HTTPException as e:
        if e.status == 429:
            print("Discord 429, waiting 15 min", flush=True)
            await asyncio.sleep(900)
        raise


async def main():
    app = web.Application()
    app.router.add_get("/", lambda r: web.Response(text="ok"))
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", int(os.getenv("PORT", "10000"))).start()

    stop = asyncio.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            asyncio.get_running_loop().add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError):
            pass

    async with bot:
        tasks = [asyncio.create_task(load_cache()), asyncio.create_task(flusher())]
        bot_task = asyncio.create_task(run_bot())
        stopper = asyncio.create_task(stop.wait())
        await asyncio.wait({bot_task, stopper}, return_when=asyncio.FIRST_COMPLETED)
        await flush_now(timeout=15)  # save queued votes before shutting down / restarting
        for t in tasks:
            t.cancel()
        if bot_task.done():
            bot_task.result()  # re-raise the original error so Render restarts us


asyncio.run(main())
