# Duke systemd **user** timers

The autonomy jobs (duke-v2 P1.1/P1.7/P1.8) run as **systemd user timers**, not
cron — `CronCreate`'s 7-day expiry silently kills wakers, so the schedule lives
here instead. All units are `%h`-relative (`%h` = `$HOME`) and call scripts via
the stable symlink `~/blimp/repo` → the Windows-drive checkout, so the paths
don't change if the repo moves on `/mnt/c`.

All schedules are **Asia/Kuala_Lumpur** (the WSL system timezone), so bare
`OnCalendar=` clock times already mean KL local time — same convention as
`backup.timer`.

## Timers

| Timer | Service runs | Schedule (KL) | Catch-up | Notes |
|---|---|---|---|---|
| `backup.timer` | `backup.sh` | daily 03:00 | `Persistent=true` | pre-existing |
| `dreams.timer` | `dreams/dreams.sh` | daily 03:30 | `Persistent=true` | reflective + anticipatory pass, demand-gated |
| `overnight.timer` | `heartbeat/overnight.sh` | daily 00:30 | `Persistent=true` | draft-only headless shift, demand-gated |
| `heartbeat.timer` | `heartbeat/heartbeat.sh` | every 20 min, 08:00–23:00 | `Persistent=false` | daytime pulse, zero tokens; missed slot is itself an alert |
| `evals.timer` | `evals/run-evals.sh` | weekly Sun 04:00 | `Persistent=true` | incident-regression suite, read-only harness |

Each `*.service` is `Type=oneshot` with a `WorkingDirectory` next to its script
and appends stdout+stderr to `~/blimp/logs/<job>.log`. `dreams.sh` also keeps its
own richer `~/blimp/logs/dreams.log`; the rest only log via the unit.

## Install / enable (run at the terminal — session 3)

Unit files install to `~/.config/systemd/user/`. Logger dir + install + enable:

```sh
mkdir -p ~/blimp/logs ~/.config/systemd/user
cp ~/blimp/repo/blimp-infra/systemd/user/*.service ~/blimp/repo/blimp-infra/systemd/user/*.timer ~/.config/systemd/user/
systemctl --user daemon-reload

# enable + start each timer (the .timer pulls in its .service on schedule)
systemctl --user enable --now dreams.timer
systemctl --user enable --now heartbeat.timer
systemctl --user enable --now overnight.timer
systemctl --user enable --now evals.timer
```

Linger must be on so the timers fire with no shell open (one-time, needs sudo):

```sh
sudo loginctl enable-linger "$USER"
```

## Verify after enabling

```sh
systemctl --user list-timers --all          # next/last fire per timer
systemctl --user status dreams.timer
journalctl --user -u dreams.service -n 50    # systemd-side log
tail -n 50 ~/blimp/logs/dreams.log           # app-side log
```

Dry-run a job by hand without waiting for the timer (no LLM spend / writes):

```sh
DRY_RUN=1 bash ~/blimp/repo/blimp-infra/dreams/dreams.sh
bash ~/blimp/repo/blimp-infra/heartbeat/heartbeat.sh --dry-run
DRY_RUN=1 bash ~/blimp/repo/blimp-infra/heartbeat/overnight.sh
bash ~/blimp/repo/evals/run-evals.sh --dry-run
```

## Disable

```sh
systemctl --user disable --now dreams.timer    # (and heartbeat/overnight/evals)
```
