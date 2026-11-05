# Blimp

Your own team of AI agents that share one memory and one task board. You talk to them on Discord.

| Agent | Role |
|---|---|
| **Duke** | Builds things: code, delivery, coordination |
| **Hermes** (optional) | Technical lead: architecture and design |
| **Frank** (optional) | Operator: monitoring, data, upkeep, reports |

You are "the Boss". The agents answer only you, and they ask before spending money, contacting anyone, or changing their own rules.

## Set it up (about 20 minutes)

### 1. What you need
- A Windows 10/11 PC (or an Ubuntu 22.04+ machine)
- A Claude Pro or Max plan ([claude.ai](https://claude.ai))
- A free Discord account and a Discord server of your own
- An OpenRouter account with a few dollars of credit, for the memory ([openrouter.ai](https://openrouter.ai))

### 2. Get your three answers ready
The setup asks for these once.

1. **OpenRouter API key**: [openrouter.ai/keys](https://openrouter.ai/keys), then **Create Key**. It starts with `sk-or-`.
2. **Discord bot token for Duke**:
   1. Go to [discord.com/developers/applications](https://discord.com/developers/applications) and click **New Application**. Name it "Duke".
   2. Open **Bot**, click **Reset Token**, and copy it.
   3. On the same page, turn on **Message Content Intent**.
   4. Open **OAuth2**, then **URL Generator**. Tick **bot**, then tick **View Channels**, **Send Messages**, **Read Message History**, **Attach Files** and **Add Reactions**. Open the link at the bottom and add Duke to your server.
3. **Your Discord user ID**: in Discord, open **Settings**, then **Advanced**, and turn on **Developer Mode**. Right-click your own name and choose **Copy User ID**.

### 3. Run it
- **Windows:** download this repo (green **Code** button, then **Download ZIP**), unzip it, and double-click **`START-BLIMP.bat`**.
  - The first time, it installs Ubuntu for Windows. Restart your PC when it says so, open **Ubuntu** from the Start menu once to choose a username and password, then double-click `START-BLIMP.bat` again.
- **Ubuntu:** `git clone` this repo, then run `bash setup.sh`.

It asks your three answers, installs everything, opens a browser so you can log in to Claude, and starts Duke.

### 4. Say hi
DM your Duke bot on Discord. He answers only you.

To start Blimp after a restart, double-click `START-BLIMP.bat` again. It skips everything already done.

## Make it yours
Edit these files in plain English. Duke reads them at the start of every session.
- `blimp-infra/duke-system-context.md`: who Duke is and how he works
- `AGENTS.md`: the rules every agent follows

Optional extras, once Duke is running:
- Hermes: `bash blimp-infra/bootstrap-hermes.sh` (needs a second Discord bot)
- Scheduled jobs (heartbeat, nightly memory tidy): see `blimp-infra/systemd/user/README.md`. They call AI models, so they cost money while you sleep. Leave them off until you want them.

## If something goes wrong
- **Watch Duke work:** open Ubuntu and run `tmux attach -t duke` (leave with Ctrl+B, then D).
- **Duke doesn't reply:** check you DM'd the right bot, and that Message Content Intent is on (step 2.2).
- **Wrong answer typed in setup:** edit `~/blimp/.env` in Ubuntu (`nano ~/blimp/.env`) and run setup again.
- **Your Ubuntu has another name** (for example `Ubuntu-24.04`): open `START-BLIMP.bat` in Notepad and change both `Ubuntu` to that name.

## What's inside
- `blimp/mesh/`: the task board, approvals, and checks that stop an agent saying "done" without proof
- `blimp-infra/`: shared-memory server, hooks, watchdog, scheduled jobs, voice
- `.claude/agents/`, `.claude/skills/`: helper agents and skills
- `evals/`: behaviour tests
- `docs/architecture.md`: how it all fits together

## Status: early
The double-click launcher is tested on Windows, and the setup's install steps on a clean Ubuntu 24.04. The memory, Claude login and Discord steps have not yet been run end to end on a fresh machine, so expect a rough edge or two. Known gaps:
- The owner role is called `logan` inside the database and code (it shows as "the Boss" in text).
- Some prompts mention the original Discord channel names. Rename them to yours.
- No licence yet.
