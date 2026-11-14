# Blimp

Your own AI agent, running on your own computer, that you talk to on Discord. It keeps a long-term memory, comes with a full set of skills and helper agents, and answers only you.

| Agent | Role |
|---|---|
| **Duke** | Builds things: code, delivery, coordination (installed by setup) |
| **Hermes** (optional) | Technical lead: architecture and design |
| **Frank** (optional) | Operator: monitoring, data, upkeep, reports |

You are "the Boss". The agents ask before spending money, contacting anyone, or changing their own rules.

## What you get
- **The agent**: Claude Code (or Codex) running in the background, linked to your Discord.
- **Long-term memory**: [Hindsight](https://github.com/vectorize-io/hindsight) on a local Postgres database with [pgvector](https://github.com/pgvector/pgvector). It starts empty and fills as you work. It never leaves your computer, except the short passages a small AI model (through OpenRouter) reads to file each memory.
- **Structured data**: tasks, prospects and quotes in the same Postgres, through the `blimp-brain` memory tools.
- **Skills and helper agents** in `.claude/`: verification, critic, planner, debugging, pre-mortem and more.
- **The doctor**: checks every part of the install, fixes what it can, and can talk a problem through with you.

## Set it up (about 30 minutes, mostly waiting)

### 1. What you need
- A Mac (macOS 13 or newer, Apple silicon or Intel), a Windows 10/11 PC, or Ubuntu 22.04 or newer. About 8 GB of free disk.
- On a Mac, an administrator account (setup installs [Homebrew](https://brew.sh) if it is missing and asks for your password once). No Docker needed.
- A Claude Pro or Max plan ([claude.ai](https://claude.ai)), or a ChatGPT plan that includes Codex.
- A free Discord account and a Discord server of your own.
- For the memory: an OpenRouter account with a few dollars of credit ([openrouter.ai](https://openrouter.ai)). You can type skip and add it later: run setup again and it asks for the key again.

### 2. Get your answers ready
1. **OpenRouter key**: [openrouter.ai/keys](https://openrouter.ai/keys), **Create Key**. It starts with `sk-or-`.
2. **A Discord bot token**:
   1. [discord.com/developers/applications](https://discord.com/developers/applications), **New Application**, name it (for example "Duke").
   2. **Bot**, **Reset Token**, copy it. On the same page turn on **Message Content Intent** and save.
   3. **OAuth2**, **URL Generator**: tick **bot**, then **View Channels**, **Send Messages**, **Read Message History**, **Attach Files**, **Add Reactions**. Open the link at the bottom and add the bot to your server.
3. **Your Discord user ID**: Discord **Settings**, **Advanced**, turn on **Developer Mode**. Right-click your own name, **Copy User ID**.

### 3. Run it
- **Mac:** download this repository (green **Code** button, **Download ZIP**) and unzip it. Open **Terminal** (press Cmd+Space, type Terminal, press Enter), type `bash ` with a space after it, drag `setup.sh` from the unzipped folder into the Terminal window, and press Enter. If the folder is in Downloads, Desktop or Documents, setup moves Blimp to `~/blimp/app` first, because macOS does not let programs that start at login read those folders. From then on, that is your copy.
- **Windows:** download and unzip the same way, then double-click **`START-BLIMP.bat`**. The first time it installs Ubuntu for Windows: restart when it says so, open **Ubuntu** from the Start menu once to choose a username and password, then double-click `START-BLIMP.bat` again. Keep its window open (minimised is fine): Windows stops Ubuntu, and Blimp with it, when that window closes.
- **Ubuntu:** `git clone` this repository and run `bash setup.sh`.

Setup works out your computer by itself, asks which AI to use, a name for your system, and your answers from step 2, then installs and checks everything in nine steps. It checks your bot token with Discord before going on. At the end you sign in to Claude in your browser, and the doctor checks the whole install. Running setup again is always safe: it skips what is done and repairs what is not.

On a Mac, the first start of Claude may ask whether it may work in the folder you installed. Setup shows that question to you. Say yes.

### 4. Say hi
DM your bot on Discord. It answers only you.

## Every day: the `blimp` command

| Command | What it does |
|---|---|
| `blimp start` | Start the agent and its memory |
| `blimp stop` | Stop the agent |
| `blimp restart` | Stop, then start |
| `blimp status` | Check every part, change nothing |
| `blimp doctor` | Find problems and fix them with you (`blimp doctor --chat` to talk it through) |
| `blimp logs` | Watch the agent work (leave with Ctrl+B, then D) |
| `blimp update` | Get the newest version (git copies only) and set it up |

On a Mac, setup offers to start Blimp by itself each time you log in. On Windows, double-click `START-BLIMP.bat` after a restart: once setup has finished, it only starts Blimp.

## The doctor
`blimp doctor` checks 22 things in five groups (system, tools, memory, Discord, agent) and prints what is wrong in plain words, each with the fix. It offers to run the fixes it knows; it asks first unless you told setup it may run them by itself. `blimp doctor --chat` opens a conversation with an AI that reads the doctor's report and the logs and works the problem through with you. It is told to stay inside the install folder and `~/blimp`, and your keys are removed from what it sees, but it is an AI running commands: keep "ask each time" unless you trust it.

## Make it yours
Edit these in plain English. The agent reads them at the start of every session.
- `blimp-infra/duke-system-context.md`: who the agent is and how it works
- `AGENTS.md`: the rules every agent follows

(On a Mac installed from Downloads, Desktop or Documents, these are in `~/blimp/app`.)

Optional extras once it runs: Hermes (`bash blimp-infra/bootstrap-hermes.sh`, needs a second Discord bot), and scheduled jobs (`blimp-infra/systemd/user/README.md`, Linux only; they call AI models, so they cost money while you sleep).

## If something goes wrong
Run `blimp doctor` first. Then:
- **The bot does not reply:** check you DM'd the right bot and that Message Content Intent is on. `blimp logs` shows what the agent sees.
- **Setup stopped:** it prints the step and the log file (`~/blimp/logs/`). Run it again; it carries on where it stopped.
- **"blimp: command not found":** open a new Terminal window, or run `export PATH="$HOME/.local/bin:$PATH"`.
- **Wrong key or token typed:** `nano ~/blimp/.env`, delete that line, run setup again (it asks again).
- **Windows, your Ubuntu has another name** (for example `Ubuntu-24.04`): open `START-BLIMP.bat` in Notepad and change both `Ubuntu` to that name.

## What is tested, and what is not
Tested automatically on every change (GitHub Actions, `.github/workflows/setup.yml`), on a clean Mac (macOS, Apple silicon, the Mac's own bash 3.2) and a clean Ubuntu:
- the full setup, unattended, twice in a row: tools, Postgres with pgvector, the long-term memory answering, the memory tools registered with Claude Code, the Discord files, the doctor's checks;
- a memory round trip (write and read back).

Not tested by that, because it needs a person or real accounts:
- the Claude sign-in, a real Discord bot, and the first conversation;
- the Mac's first-run prompts (the folder question, macOS security prompts) and the login item after a real restart;
- Homebrew's own install (CI Macs come with Homebrew), and Macs older than macOS 14.

Known gaps:
- Codex is less tested than Claude. With Codex the agent is a small Discord bridge (`blimp-infra/codex-bridge/`) that answers your DMs through `codex exec`, with the memory attached. Production guards (no push to main, no force-push) are instructions only on Codex, not hard blocks. Memory on Codex can also run on your ChatGPT login instead of OpenRouter (press Enter at the key question); Hindsight's docs call that route personal use only.
- The owner role is called `logan` inside the database and code (it shows as "the Boss" in text). Some prompts mention the original Discord channel names; rename them to yours.
- No licence yet.

## What's inside
- `setup.sh`: the installer and starter (`--start` starts without questions)
- `blimp-infra/doctor/`: the doctor
- `blimp-infra/postgres/`: the memory setup (Mac: Homebrew Postgres + pgvector + Hindsight from pip)
- `blimp-infra/mcp/`: the `blimp-brain` memory tools
- `blimp/mesh/`: the task board, approvals, and checks that stop an agent saying "done" without proof
- `.claude/agents/`, `.claude/skills/`: helper agents and skills
- `evals/`: behaviour tests
- `docs/architecture.md`: how it all fits together
