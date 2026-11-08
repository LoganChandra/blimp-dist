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
- A Windows 10/11 PC, an Ubuntu 22.04+ machine, or a Mac (experimental)
- One AI account: a Claude Pro or Max plan ([claude.ai](https://claude.ai)) or a ChatGPT plan that includes Codex ([chatgpt.com](https://chatgpt.com)). Setup asks which.
- On a Mac: [Homebrew](https://brew.sh), installed; [Docker Desktop](https://www.docker.com/products/docker-desktop) too unless you pick the ChatGPT-login memory on Codex
- A free Discord account and a Discord server of your own
- An OpenRouter account with a few dollars of credit, for the memory, unless you use Codex with the ChatGPT-login memory ([openrouter.ai](https://openrouter.ai))

### 2. Get your three answers ready
The setup asks for these once.

1. **OpenRouter API key** (not needed on Codex with the ChatGPT-login memory): [openrouter.ai/keys](https://openrouter.ai/keys), then **Create Key**. It starts with `sk-or-`.
2. **Discord bot token for Duke**:
   1. Go to [discord.com/developers/applications](https://discord.com/developers/applications) and click **New Application**. Name it "Duke".
   2. Open **Bot**, click **Reset Token**, and copy it.
   3. On the same page, turn on **Message Content Intent**.
   4. Open **OAuth2**, then **URL Generator**. Tick **bot**, then tick **View Channels**, **Send Messages**, **Read Message History**, **Attach Files** and **Add Reactions**. Open the link at the bottom and add Duke to your server.
3. **Your Discord user ID**: in Discord, open **Settings**, then **Advanced**, and turn on **Developer Mode**. Right-click your own name and choose **Copy User ID**.

### 3. Run it
- **Windows:** download this repo (green **Code** button, then **Download ZIP**), unzip it, and double-click **`START-BLIMP.bat`**.
  - The first time, it installs Ubuntu for Windows. Restart your PC when it says so, open **Ubuntu** from the Start menu once to choose a username and password, then double-click `START-BLIMP.bat` again.
- **Ubuntu or Mac:** `git clone` this repo, then run `bash setup.sh`.

Setup first asks four things: your computer (Windows, Linux or macOS), which AI runs the system (Claude, Codex, DeepSeek, or any OpenRouter model you type), what to call your system (the name only changes what you see, default Blimp), and whether the doctor may run commands itself. Then it asks your three answers, installs everything, logs you in, and starts the agent. No login or key yet? Press Enter at a question and it prints a short checklist for getting one.

**The doctor.** If setup stops, it offers the doctor: a chat in the terminal that reads the logs, finds the problem and fixes it with you. Start it any time with `python3 blimp-infra/doctor/doctor.py`. It runs on the AI you picked. If you let it run commands itself it will not ask each time; otherwise it asks before every command. It is told to stay inside the install folder and `~/blimp`, and your keys are removed from what it sees, but it is an AI running commands: keep the "ask each time" answer unless you trust it.

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
- macOS is experimental and untested: it has its own Homebrew memory setup (`bootstrap-duke-memory-mac.sh`), never run on a real Mac yet.
- Memory on Codex has three options at setup: an OpenRouter key (cheap, needs Docker), your ChatGPT login (press Enter; no key and no Docker, installs Hindsight from pip, a few GB, needs Python 3.11-3.13 and a second browser login; Hindsight's docs say personal use only), or `skip` for no memory. The ChatGPT-login route is untested on a real account.
- Codex is untested end to end. With Codex the agent is a small Discord bridge (`blimp-infra/codex-bridge/`) that answers your DMs through `codex exec`, with the shared memory attached. Claude's hooks and helper agents are rebuilt for it as skills (`codex-guards`, `codex-agent-<name>`) plus bridge code: every turn is logged to memory, a verify-before-done `Check:` line is required on completion claims, and helper agents run as separate `codex exec` calls. Production guards (no push to main, no force-push) are instructions only on Codex, not hard blocks. Its Discord link was tested only against a stand-in server.
- DeepSeek or another OpenRouter model powers the doctor and memory only; the Discord agent still needs Claude.
- The owner role is called `logan` inside the database and code (it shows as "the Boss" in text).
- Some prompts mention the original Discord channel names. Rename them to yours.
- No licence yet.
