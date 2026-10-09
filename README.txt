RENDER + SUPABASE DISCORD KARMA BOT

Render build command: npm install
Render start command: npm start
Node version: 20 or newer

Required Render environment variables:
DISCORD_TOKEN = your Discord bot token (secret)
DISCORD_CLIENT_ID = your Discord application's Application ID
SUPABASE_URL = https://qyzdylzzogqutukpbzti.supabase.co (confirm this is your project URL)
SUPABASE_SERVICE_ROLE_KEY = your Supabase server-side secret/service_role key (secret; never expose publicly)
DISCORD_GUILD_ID = optional; your Discord server ID for faster test slash-command registration

The Supabase public.votes table should contain:
id text primary key, voter_id text not null, voter_tag text, target_id text not null, target_tag text,
value smallint not null, reason text not null, created_at timestamptz not null, undone boolean not null,
undone_at timestamptz, undone_by text, guild_id text.
The table was created via the connected Supabase MCP, with RLS enabled. Use the service-role/secret key only as a Render environment variable.

UptimeRobot monitor URL after deployment: https://YOUR-SERVICE.onrender.com/health
This can help wake a Render free web service but cannot guarantee uninterrupted uptime.

Discord setup:
- Enable Server Members Intent in the Developer Portal.
- Give the bot Manage Roles and move its highest role above every rank role.
- In index.js, change GENDER_ROLES to exact role names in your server.
- Required rank roles: Aam aadmi party, Devta, Devi, Acha bacha, Acha bachi, Acha none of the above, Nalayak, Gaddar.

This ZIP does not include your token or Supabase key. Do not add secrets to a public repository.
Existing votes in a previous data.json file are not automatically imported by this version.
