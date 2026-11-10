<img width="408" height="360" alt="CASTSYNC" src="https://github.com/user-attachments/assets/35fa7f69-aba2-411b-9ec5-dc5744ed3a11" />

# CastSync
castsync runs hourly on a Linux box. It finds live streams that have finished (from Restream, or straight from YouTube, Twitch or Facebook) and checks whether each one is already an episode on your podcast host. Any that are missing get turned into a loudness-normalized, ID3-tagged MP3 and posted to the host you choose: **Libsyn** or **Transistor.fm**. You get an email whenever it does something or something goes wrong. Hours with nothing to do stay silent.

## How it works

```
hourly systemd timer
  └─ castsync run
       1. Read existing episodes            → Libsyn RSS feed, or Transistor API (incl. drafts)
       2. Restream API: finished events     → title, times, destination URLs
          + (optional) yt-dlp channel scans → YouTube /streams, Twitch archives, Facebook
       3. Decide: skip tests/short streams, skip anything just ended (<20 min),
          skip anything already on the host, keep one copy per stream across platforms,
          then order newest → oldest and take 2 per run
       3b. Libsyn only: check the monthly upload limit; anything that won't fit
          waits for the reset on the 1st
       4. Get audio: Restream's stored audio track first; if it's missing or
          expired, download the replay with yt-dlp in your chosen order
          (default YouTube → Facebook → Twitch)
       5. ffmpeg → MP3 (default 64 kbps, 44.1 kHz, mono; -16 LUFS; ID3 tags)
       6. Post it: Transistor → upload + create + publish (fully automatic)
                   Libsyn     → FTP dropbox (or full API mode on Libsyn's top plans)
       7. Email + save a report in ~/.local/share/castsync/reports/
```

Everything it handles is recorded in a small SQLite database, so a stream is never uploaded twice. If the host's episode list can't be read, castsync uploads nothing that hour, since it can't confirm what's already there.

## Command summary

Run any of these from any folder. Commands that change something on your podcast host show what they would do first, and only make changes with `--apply`.

**Everyday**

| Command | What it does |
|---|---|
| `castsync run` | One pass: find finished streams, make MP3s, post them, email a report. This is what the hourly timer runs. |
| `castsync run --dry-run` | Show what would be posted and where each episode's audio would come from. Changes nothing. |
| `castsync run --no-upload` | Download and make the MP3s but don't post them, so you can listen first. The MP3 is kept. |
| `castsync run --always-email` | Send the report email even when there was nothing to do. |
| `castsync run --no-email` | Don't send the report email (it's still saved under `~/.local/share/castsync/reports/`). |
| `castsync run --force` | Ignore castsync's history and duplicate checks. **Careful:** this can post duplicates. |
| `castsync status` | Which host, upload usage this month, and recent history with each stream's key. `--limit 50` shows more. |

**Setup and checks**

| Command | What it does |
|---|---|
| `castsync check-config` | Check `config.yaml` for mistakes and missing settings; shows the host and audio download order. |
| `castsync test-email` | Send a test report and show which mail server and login it used. |
| `castsync auth restream` | One-time Restream sign-in in your browser. |
| `castsync auth libsyn` | One-time Libsyn API sign-in (only for `libsyn.mode: api`). |
| `castsync shows` | List your Transistor shows and their IDs, for `transistor.show_id`. |

**Fixing things on Transistor**

| Command | What it does |
|---|---|
| `castsync retitle` | For episodes castsync posted: show each one's current and new name, episode # and season. Changes nothing. |
| `castsync retitle --apply` | Make those changes on Transistor. `--days 365` looks further back than the default 180 days. |
| `castsync renumber` | For **every** episode on the show (including ones castsync didn't post): show the episode # / season it would get. Changes nothing. |
| `castsync renumber --apply` | Make those changes on Transistor. |

Changes to Transistor go out in batches of 10 with a 30-second pause between batches; the preview shows how long it will take.

**One specific stream** (keys come from `castsync status`, e.g. `restream:abc123`)

| Command | What it does |
|---|---|
| `castsync retry KEY` | Try a failed stream again on the next run. |
| `castsync allow KEY` | Post it on the next run even though it looked like a duplicate (e.g. two shows on the same day). |
| `castsync ignore KEY` | Never post this stream. |

**Removing castsync**

| Command | What it does |
|---|---|
| `castsync-uninstall` (or `castsync uninstall`) | Remove castsync; asks before deleting your settings, history and kept audio. |
| `castsync-uninstall --keep-data` | Remove the program only; keep settings, history and audio. |
| `castsync-uninstall --purge` | Remove everything. Add `--yes` to skip the questions. |
| `./uninstall.sh` (from the download folder) | Same as above; also cleans up older castsync versions that don't have `castsync-uninstall`. |

**Options for any command:** `-c FILE` uses a different config file (default `~/.config/castsync/config.yaml`). `-v` gives more detailed logging. `castsync --help` or `castsync COMMAND --help` lists the options.

**The hourly schedule and logs**

| Command | What it does |
|---|---|
| `systemctl --user enable --now castsync.timer` | Turn on the hourly schedule. |
| `systemctl --user disable --now castsync.timer` | Pause it. |
| `systemctl --user list-timers castsync.timer` | Show when it runs next. |
| `journalctl --user -u castsync -f` | Watch the log live (Ctrl+C to stop). The same log is in `~/.local/share/castsync/castsync.log`. |

## Choosing where episodes go: Libsyn or Transistor

Set it at the top of `config.yaml`:

```yaml
destination: transistor     # or: libsyn
```

| | `transistor` | `libsyn` |
|---|---|---|
| Fully automatic (published with no clicks) | Yes, on every plan | Only with Libsyn's API, which is limited to its most expensive plans. Otherwise the MP3 lands in your FTP dropbox and you publish it in Libsyn. |
| What you need | API key + show ID | Libsyn login (FTP); API client ID/secret for API mode |
| Upload limit | None. Transistor plans are priced by downloads, not storage. | Monthly upload limit by plan; castsync tracks it (see below) |
| How castsync sees existing episodes | Transistor API, including drafts and scheduled episodes | Your public RSS feed |

**Transistor setup:**
1. In the Transistor dashboard open **Account → API Access** and copy your API key into `castsync.env` as `TRANSISTOR_API_KEY=...`.
2. Run `castsync shows` to list your shows with their IDs, and put the right one in `config.yaml` as `transistor.show_id`.
3. `transistor.publish: published` (default) makes each episode go live right away. Use `draft` if you'd rather look it over in Transistor first.
4. **Episode numbers and seasons** (the `#S2018:-` label in Transistor means season 2018, no episode number). By default castsync numbers each episode by its air date as YYYYMMDD (e.g. 20261102), so episodes sort in airing order. It leaves the season to your show's setting unless you choose one:

   ```yaml
   transistor:
     episode_number: date   # default; or none for no number, or a digits-only format like "{date:%y%m%d}"
     season: year           # e.g. 2026; or a fixed number; or leave (default: Transistor's show setting)
   ```
5. Optional: `transistor.extra_fields` passes extra episode fields (for example `type: full`).

**Pacing.** castsync never rushes Transistor:
- **About a second between requests**, staying under Transistor's limit of 10 requests per 10 seconds.
- **A 30-second pause after every 10 requests**, for every command: catching up on a backlog, `retitle --apply`, `renumber --apply` (10 episodes per batch), or reading a long episode list.
- **If Transistor still says to slow down**, castsync waits and retries.

You can change the batch size and pause:

```yaml
transistor:
  batch_size: 10              # requests per batch; 0 = no batch pauses
  batch_pause_seconds: 30
```

`retitle` and `renumber` tell you how long `--apply` will take before you run it.

Duplicate protection, newest-first order, 2 per run, MP3 quality, file cleanup and the report emails all work the same for both hosts.

## Zorin OS: step-by-step install and configure

Written for Zorin OS 18 / 18.1 (Ubuntu 24.04 base, Python 3.12) and Zorin OS 17 / 17.3 including Lite (Ubuntu 22.04 base, Python 3.10). Core, Pro and Education all work the same way. Everything below is typed in **Terminal**: press `Ctrl`+`Alt`+`T`, or open the Zorin menu and search "Terminal".

### 1. Check your version (optional)

```bash
grep -E 'PRETTY_NAME|UBUNTU_CODENAME' /etc/os-release
```

You should see Zorin OS with `noble` (Zorin 18) or `jammy` (Zorin 17).

### 2. Unpack castsync and run the Zorin installer

Save `castsync.tar.gz` to your **Downloads** folder, then:

```bash
cd ~/Downloads
tar xzf castsync.tar.gz
cd castsync
./install-zorin.sh
```

Type your password when asked. The script does the following:

- Installs the system packages: `python3`, `python3-venv`, `python3-pip`, `ffmpeg`, `ca-certificates` (via `apt`).
- Creates a private Python environment in `~/.local/share/castsync/venv` and installs castsync with `requests`, `PyYAML` and `yt-dlp`. Nothing is installed into the system Python.
- Copies config templates to `~/.config/castsync/`.
- Installs the systemd timers (hourly castsync, daily yt-dlp update) into `~/.config/systemd/user/`.
- Runs `sudo loginctl enable-linger $USER` so the timer keeps running after you log out.

When it finishes, **close Terminal and open a new one**, or run `source ~/.bashrc`, so the `castsync` command is found. The installer adds `~/.local/bin` to your command path in `~/.bashrc`. You can run castsync from any folder. Check:

```bash
castsync --help
```

To do the package step by hand instead: `sudo apt update && sudo apt install -y python3 python3-venv python3-pip ffmpeg ca-certificates`, then `./install.sh`, then `sudo loginctl enable-linger $USER`.

### 3. Get the accounts and keys ready

You'll paste these into a settings file in step 4.

| What | Where to get it |
|---|---|
| **Restream client ID + secret** | Sign in at https://developers.restream.io, create an application, and set the **Redirect URI** to exactly `http://localhost:8765/callback`. If it asks which permissions (scopes) to grant, include `stream.read` and `storage.read`. Then copy the Client ID and Client Secret. |
| **Restream recordings** (recommended) | Make sure recording is turned on for your Restream events. castsync then uses Restream's audio track. Without recordings it downloads the audio from YouTube/Twitch instead. |
| **Transistor API key + show ID** (if `destination: transistor`) | Transistor dashboard → **Account → API Access**. You'll get the show ID with `castsync shows` after step 5. |
| **Libsyn RSS feed URL** (if `destination: libsyn`) | In Libsyn, open your show's destinations/RSS settings. It usually looks like `https://yourshow.libsyn.com/rss`. Open it in Firefox to confirm it lists your episodes. |
| **Libsyn login** (if `destination: libsyn`) | The email and password you sign in to Libsyn with (used for the FTP upload). |
| **Libsyn monthly upload limit** (if `destination: libsyn`) | Your plan's "new storage per month" in MB, shown on your Libsyn account/plan page (e.g. 162 or 324). |
| **Email account for reports** | Any mailbox that allows SMTP sending: Zoho Mail, Gmail, Microsoft 365, your web host, etc. You need the SMTP server details (see [Email settings](#email-settings-zoho-gmail-or-any-smtp-server)), the full email address, and a password. **Zoho:** if two-factor sign-in is on, create an app-specific password in your Zoho account's security settings (look for "App Passwords") and use that. |
| **YouTube streams page** (optional) | `https://www.youtube.com/@YourChannel/streams`. Add your Twitch `https://www.twitch.tv/yourchannel/videos?filter=archives` too if you like. |

### 4. Enter your settings

The config folder is hidden. To browse to it in **Files**, press `Ctrl`+`H` to show hidden folders. The easiest way is to open the two files from Terminal:

```bash
xdg-open ~/.config/castsync/castsync.env       # opens in Zorin's Text Editor
xdg-open ~/.config/castsync/config.yaml
```

`nano ~/.config/castsync/config.yaml` works too. Save with `Ctrl`+`O`, Enter, then exit with `Ctrl`+`X`.

**`castsync.env`** holds the secrets, one per line, no spaces around `=`:

```
RESTREAM_CLIENT_ID=abc123
RESTREAM_CLIENT_SECRET=xyz789
LIBSYN_USER=you@example.com              # Libsyn only
LIBSYN_PASSWORD=your-libsyn-password     # Libsyn only
TRANSISTOR_API_KEY=your-transistor-key   # Transistor only
SMTP_USER=you@yourdomain.com
SMTP_PASSWORD=your-app-specific-password
```

**`config.yaml`**. Change at least these lines and leave everything else as is:

```yaml
destination: transistor   # or libsyn (see "Choosing where episodes go")
general:
  show_name: "Your Podcast Name"
  keep_files: nothing     # nothing | originals | originals_and_converted | converted
platforms:
  channels:
    - https://www.youtube.com/@YourChannel/streams
episode:
  artist: "Your Podcast Name"
transistor:               # if destination: transistor
  show_id: 12345          # from `castsync shows`
libsyn:                   # if destination: libsyn
  feed_url: https://yourshow.libsyn.com/rss
  monthly_limit_mb: 324   # your Libsyn plan's monthly upload limit
audio:                    # optional - default is 64k / 44.1 kHz / mono (see "MP3 quality")
  bitrate: 64k
email:
  provider: zoho          # zoho | zoho_pro | gmail | custom  (see "Email settings")
  to:
    - you@yourdomain.com
```

YAML is picky about indentation: use spaces, never tabs, and keep lines in the same section lined up exactly. Optional settings in the file are commented out like `  #smtp_host: ...`; to turn one on, delete just the `#`. Values containing `: ` need quotes, e.g. `show_name: "My Show: Live"`. Check the file with:

```bash
castsync check-config
```

If there's a mistake, it shows the exact line, what's wrong and how to fix it. `nano +161 ~/.config/castsync/config.yaml` opens the file at line 161.

### 5. Connect Restream (one time)

```bash
castsync auth restream
```

1. Copy the long URL it prints. Hold `Ctrl` and click it, or paste it into Firefox.
2. Sign in to Restream and approve. The browser shows "castsync authorized".
3. Go back to Terminal and press **Enter**.

**Transistor users:** now run `castsync shows`, copy your show's ID into `transistor.show_id` in config.yaml.

The Restream sign-in renews itself after that. You'd only repeat this step if you don't run castsync for about a year, or if you revoke access in Restream.

### 6. Test it

```bash
castsync test-email          # check your inbox (and spam) for a test report
castsync check-config        # catches config mistakes first
castsync run --dry-run       # shows what it WOULD upload; changes nothing
castsync run --no-upload     # downloads + builds the MP3 but doesn't upload it
```

The MP3 from `--no-upload` is under `~/.local/share/castsync/work/` (the report shows the exact path). It's kept even with `keep_files: nothing`. Open it in Files (`Ctrl`+`H` again) and play it to check the audio. Then run one real pass:

```bash
castsync run
```

### 7. Turn on the hourly schedule

```bash
systemctl --user enable --now castsync.timer
systemctl --user list-timers castsync.timer     # shows the next run time
```

Watch it work:

```bash
journalctl --user -u castsync -f                # live log (Ctrl+C to stop)
cat ~/.local/share/castsync/castsync.log        # same log as a file
ls ~/.local/share/castsync/reports/             # every emailed report is kept here
```

### 8. Keep the computer awake

A sleeping PC can't check for streams. If the machine missed a run while asleep, castsync catches up as soon as it wakes. Open **Settings → Power** and set **Automatic Suspend** to *Off* while plugged in, or at least don't let it sleep in the hours after your show. Screen blanking is fine.

### Updating, stopping, uninstalling

```bash
# update castsync to a newer castsync.tar.gz (your settings are kept)
cd ~/Downloads && tar xzf castsync.tar.gz && cd castsync && ./install.sh

systemctl --user disable --now castsync.timer      # pause the schedule
systemctl --user enable  --now castsync.timer      # resume

# uninstall (see "Uninstalling" below for options)
castsync-uninstall
```

### Zorin troubleshooting

| Symptom | Fix |
|---|---|
| `castsync: command not found` | You can run castsync from any folder; this means `~/.local/bin` isn't on your command path yet. Run `source ~/.bashrc` or open a new Terminal window. If you installed with an older castsync, re-run `./install.sh` once. In a pinch, call it by its full path: `~/.local/bin/castsync`. If `ls ~/.local/bin/castsync` says the file doesn't exist, the install didn't finish; run `./install-zorin.sh` again and look for errors. |
| "There's a formatting mistake in …config.yaml" (or a long `yaml.scanner.ScannerError` traceback from an older castsync) | A line in config.yaml isn't lined up or needs quotes. The message names the line. Most common: a setting was un-commented by deleting `#` but not the space after it, so it's indented 3 spaces instead of 2. Open it with `nano +LINE ~/.config/castsync/config.yaml`, fix the spacing, then run `castsync check-config`. |
| `ensurepip is not available` / venv error | `sudo apt install python3-venv`, then run `./install.sh` again. |
| `Address already in use` during `auth restream` | Something else is using port 8765. Change `redirect_uri` in config.yaml to e.g. `http://localhost:8766/callback`, and set the same URI in your Restream app. |
| Restream page says the redirect URI doesn't match | The Redirect URI in the Restream app must be exactly the `redirect_uri` from config.yaml, including `/callback`. |
| Test email fails (`535`, "Relaying Disallowed", timeout...) | `castsync test-email` prints the server it used and a hint for the error. See [Email settings](#email-settings-zoho-gmail-or-any-smtp-server). |
| Timer only runs while you're logged in | `sudo loginctl enable-linger $USER`. Check with `loginctl show-user $USER -p Linger` (should say `yes`). |
| YouTube downloads start failing | Usually fixed by a yt-dlp update. The daily updater does this automatically, or run `~/.local/share/castsync/venv/bin/pip install -U yt-dlp`. |
| FTP upload to Libsyn times out | Zorin's firewall is off by default and outgoing connections are allowed. If you turned on `ufw` or use a VPN, allow outgoing FTP or try `libsyn.ftp.tls: true`. |

## Install (any Linux)

On Zorin OS, follow the [Zorin OS step-by-step guide](#zorin-os-step-by-step-install-and-configure) above instead.

Needs Python 3.9+ and ffmpeg (`sudo apt install python3-venv ffmpeg`, or the dnf/pacman equivalent).

```bash
tar xzf castsync.tar.gz && cd castsync
./install.sh
```

This creates a virtualenv in `~/.local/share/castsync/venv`, puts a `castsync` command in `~/.local/bin`, copies config templates to `~/.config/castsync/`, and installs user-level systemd units. There is also a daily yt-dlp updater, because the platforms change often.

## Setup (any Linux)

1. **Restream API app.** Go to https://developers.restream.io and create an application. Set its redirect URI to `http://localhost:8765/callback` and give it the `stream.read` and `storage.read` scopes. Put the client ID and secret in `~/.config/castsync/castsync.env`.
   - To get the audio track, Restream recording has to be on for your events, and Restream's recordings expire. When there's no recording, castsync falls back to the YouTube/Twitch/Facebook VOD.
2. **Edit `~/.config/castsync/config.yaml`.** Set `destination` (`transistor` or `libsyn`), `show_name`, your YouTube `/streams` URL, the host's details (Transistor API key + `show_id`, or Libsyn `feed_url` + login), and the email settings (see [Email settings](#email-settings-zoho-gmail-or-any-smtp-server)).
3. **Sign in to Restream once:** `castsync auth restream`. Open the printed URL and approve. If you're on SSH, paste back the URL the browser was redirected to. Tokens are saved with 0600 permissions and refresh themselves.
4. **Test:**
   ```bash
   castsync test-email
   castsync run --dry-run            # lists what it would upload; changes nothing
   castsync run --no-upload          # downloads + builds the MP3 only; check the audio
   ```
5. **Turn it on:** `systemctl --user enable --now castsync.timer`

## Episode names

The episode's name is exactly what `episode.title_template` produces. You control the whole thing:

```yaml
general:
  show_name: "PTR Radio"
episode:
  title_template: "{show} ({short_date}) - {topic}"
```

With a stream titled "PTR Radio (11/2/2026) - we're sooo tired", that gives **PTR Radio (11/2/2026) - we're sooo tired**.

| Placeholder | Becomes |
|---|---|
| `{topic}` | Just the episode's own part of the stream title: `we're sooo tired`. The show name, dates and the separators around them are removed, so the result stays the same even if you write the stream title a little differently. |
| `{title}` | The whole stream title, after `strip_from_title`: `PTR Radio (11/2/2026) - we're sooo tired` |
| `{stream_title}` | The whole stream title, untouched |
| `{show}` | `general.show_name` |
| `{short_date}` | `11/2/2026` |
| `{pretty_date}` | `November 2, 2026` |
| `{date}` | `2026-11-02` |
| `{date:FORMAT}` | Any date format, e.g. `{date:%m/%d/%y}` gives `11/02/26`, `{date:%b %-d}` gives `Nov 2`, `{date:%A}` gives `Monday` |

Dates are the day the stream aired. A misspelled placeholder stops the run before anything is downloaded and lists the valid ones; `castsync check-config` catches it too.

Use `{topic}`, not `{title}`, when your template adds the show name or date itself. `{title}` already contains them, which is how "PTR Radio November 2, 2026 - PTR Radio (11/2/2026) - …" happens.

**Fixing episodes already on Transistor.** After changing the template, `episode_number` or `season`, run:

```bash
castsync retitle            # shows each episode's name, episode # and season, current and new; changes nothing
castsync retitle --apply    # updates them on Transistor
```

It only touches episodes castsync published itself. It looks up each episode's original stream from the last 180 days; use `--days 365` to go further back. The episode's web address on Transistor may keep the old wording, since Transistor sets that separately.

**Numbering every episode on the show**, including ones castsync didn't upload:

```bash
castsync renumber            # shows the episode # / season each episode would get; changes nothing
castsync renumber --apply    # updates them on Transistor
```

It uses your `transistor.episode_number` and `transistor.season` settings. Each episode is dated by, in order:
1. **The air date**, for episodes castsync uploaded.
2. **A date in the episode's title**, such as "(10/19/2026)" or "October 19, 2026".
3. **The date it was published** on Transistor.

Some episodes are left unchanged and listed, so you can set them by hand:
- **Two episodes from the same day**, since they'd get the same number.
- **Drafts with no date in the title.**

Changes go out in batches of 10 with a 30-second pause between batches (see **Pacing** above), so 100 episodes take about 6 minutes. The preview tells you the exact estimate.

## Where the audio comes from (and when Restream's recording is gone)

For each stream, castsync tries these in order:

1. **Restream's own recording.** The audio-only track, or the audio from the video recording.
2. **The replay on each platform, in the order you choose**, downloaded with yt-dlp. The default is YouTube, then Facebook, then Twitch:

   ```yaml
   platforms:
     download_order: [youtube, facebook, twitch]
   ```

   - **Change the order** by reordering the list.
   - **Never download from a platform** by leaving it out of the list.
   - **Where the links come from:** each Restream event already records its YouTube/Facebook/Twitch replay links, so the order works even if only YouTube is listed under `platforms.channels`. Channels add replay links of their own, which helps when a Restream event is missing one.
   - **Check it:** `castsync check-config` prints the order it will use.

   Keep YouTube near the front: its replays don't expire, while Twitch deletes replays after a while.

You **don't need to keep recordings in Restream longer**. castsync grabs a new stream within about an hour of it ending, well inside a 7-day Restream retention. Older streams, such as a backlog on the first run, come from the YouTube replay. That replay must still exist and be public or unlisted. For members-only or private videos, see `platforms.cookies_file`.

`castsync run --dry-run` shows the source for every episode, for example:

```
audio:  Restream no longer has a recording; will download the replay from YouTube -> Facebook -> Twitch with yt-dlp (...)
```

If the dry run says **NO AUDIO SOURCE**, Restream has no recording and no replay link for that stream. Add your YouTube `/streams` page to `platforms.channels` so castsync can find the replay itself.

## Order, pace and the monthly upload limit (Libsyn)

**Newest first, 2 per run.** Each hourly run sorts the missing episodes from most recent to oldest and handles at most 2, one after the other (`general.max_episodes_per_run`). Any more are listed in the report as queued and taken by the following runs, still newest first. Once castsync is running normally there's usually one new stream at a time, so this mostly matters when catching up.

**Monthly upload limit (Libsyn only; Transistor has none).** Libsyn plans allow a fixed amount of new uploads per calendar month, and the quota resets on the 1st. Set your plan's limit:

```yaml
libsyn:
  monthly_limit_mb: 324
```

Before each upload, castsync adds up this month's usage:
- **Its own uploads:** exact sizes, kept in its database.
- **Episodes you uploaded yourself:** episodes in your RSS feed published since the 1st that castsync didn't upload, using the file sizes the feed reports.
- **Anything else you tell it about:** `quota_already_used_mb`, for uploads it can't see, such as files sitting unpublished in Libsyn.

It keeps a 5 MB safety margin (`quota_safety_mb`). If an episode won't fit:
- **When it can estimate the size first** (constant bitrate and a known stream length), it doesn't even download the episode.
- **Otherwise** it converts the episode, measures the real file, and only then decides.

An episode that doesn't fit is marked *waiting* and never uploaded that month. You get one email listing what's waiting, and after the reset those episodes upload automatically, newest first. They're remembered even if they're older than `lookback_days` by then.

Every report and `castsync status` shows a line like:

```
Libsyn monthly upload: 310.2 of 324 MB used (96%), 8.8 MB available; resets Dec 1, 6 AM
```

Notes:
- **Counting is cautious.** MB means 1,000,000 bytes. The new month only starts at 6 AM local time on the 1st (`quota_reset_delay_hours`), in case Libsyn resets in a later time zone.
- **What castsync can't see.** Files uploaded through Libsyn's website but not yet published don't appear in the feed. If you do that, add their size to `quota_already_used_mb` until the month ends.
- **Fit more per month.** At the default 64 kbps mono, an hour is about 29 MB. A lower bitrate (see [MP3 quality](#mp3-quality)) fits more episodes into the limit.

## Keeping or discarding local files

By default castsync leaves **nothing** behind. After each episode is handled, its working folder is deleted: the downloaded audio and the MP3. That happens whether the episode was uploaded, failed, or is waiting for the Libsyn monthly limit. Choose what to keep in `config.yaml`:

```yaml
general:
  keep_files: nothing        # nothing | originals | originals_and_converted | converted
  # keep_dir: ~/Podcast/castsync
  keep_days: 14              # 0 = keep forever
```

| `keep_files` | Downloaded audio (originals) | MP3 sent to your host (converted) |
|---|---|---|
| `nothing` (default) | deleted | deleted |
| `originals` | kept | deleted |
| `originals_and_converted` | kept | kept |
| `converted` | deleted | kept |

- **Plain wording works:** `Originals + Converted`, `both`, `only converted` and `none` are understood too. A value it doesn't recognize stops the run with an error before anything is downloaded.
- **Where kept files go:** if you set `keep_dir`, kept files are moved there into `originals/` and `converted/` folders. Otherwise they stay under `~/.local/share/castsync/work/<episode>/`.
- **How long:** kept files are deleted after `keep_days`. Set it to 0 to keep them forever.
- **Always removed:** partial downloads and temporary files, whatever the setting.
- **One exception:** `castsync run --no-upload` always keeps the MP3, because the point of that command is to listen to it.
- **Every report** shows where a kept file is, or notes that it was deleted locally.

## MP3 quality

By default the MP3 sent to your host is **64 kbps, 44.1 kHz, mono, constant bitrate**. That's clear for spoken word and about 29 MB per hour. To change it, edit the `audio:` section of `config.yaml`:

```yaml
audio:
  bitrate: 64k          # 64, "64k" or "64kbps" all work
  sample_rate: 44.1khz  # 44100, "44.1k", "44khz" (= 44.1 kHz), "48k", 22050 ...
  channels: mono        # mono / stereo (or 1 / 2)
  bitrate_mode: cbr     # cbr (recommended for podcast hosts) or vbr
  # vbr_quality: 6      # vbr only: 0 = best/biggest ... 9 = smallest
```

| Setting | Size per hour | Good for |
|---|---|---|
| 64k, 44.1 kHz, mono (default) | ~29 MB | Talk shows |
| 96k, 44.1 kHz, mono | ~43 MB | Shows with music beds |
| 128k, 44.1 kHz, stereo | ~58 MB | Music-heavy shows |
| 32k, 22.05 kHz, mono | ~14 MB | Smallest files; noticeably lower quality |

MP3 only allows certain combinations:
- **32,000 / 44,100 / 48,000 Hz:** 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256 or 320 kbps
- **16,000 / 22,050 / 24,000 Hz:** 8 to 160 kbps

If you choose one that isn't possible (65k, or 320k at 22 kHz), castsync stops before downloading anything. The email lists the values that would work. Every report shows the quality each MP3 was made with, and `castsync run --no-upload` lets you make a test file and listen before anything is uploaded.

Volume levelling (`loudnorm`, target `-16` LUFS) is applied whatever the quality setting. Some producers use `target_lufs: -19` for mono shows.

## Email settings (Zoho, Gmail or any SMTP server)

castsync sends its reports through any SMTP server. In `config.yaml`, choose a `provider` preset, or use `custom` and enter the server yourself. Anything you set explicitly overrides the preset.

| `provider` | Server | Port / security | Use for |
|---|---|---|---|
| `zoho` | `smtp.zoho.com` | 465 / ssl | Zoho Mail personal accounts and free organization plans |
| `zoho_pro` | `smtppro.zoho.com` | 465 / ssl | Zoho Mail paid plans with your own domain (you@yourdomain.com) |
| `gmail` | `smtp.gmail.com` | 587 / starttls | Gmail / Google Workspace (needs an App Password) |
| `custom` | your `smtp_host` | your `smtp_port` / `security` | Anything else: web host mail, Fastmail, Microsoft 365, a local relay… |

**Zoho Mail example:**

```yaml
email:
  provider: zoho_pro              # or zoho for a personal/free account
  username: ${SMTP_USER}          # your full Zoho address
  password: ${SMTP_PASSWORD}      # app-specific password if two-factor sign-in is on
  to:
    - you@yourdomain.com
```

Things to know about Zoho:
- **Region.** If your Zoho account lives in a non-US data center, keep `provider: zoho` (or `zoho_pro`) and set `smtp_host` to that region's server, e.g. `smtp.zoho.eu`, `smtp.zoho.in` or `smtp.zoho.com.au`. Zoho shows the exact server for your account in its mail settings, under the server configuration details.
- **From address.** It must be your Zoho address or one of its aliases. Anything else fails with "Relaying Disallowed". castsync uses your login address unless you set `from`.
- **Port 587.** If 465 is blocked on your network, set `smtp_port: 587`. castsync switches to starttls automatically.

**Any other SMTP server:**

```yaml
email:
  provider: custom
  smtp_host: mail.yourhost.com
  smtp_port: 587                  # 465 -> ssl, 587 -> starttls (worked out from the port)
  security: starttls              # optional: ssl | starttls | none
  username: ${SMTP_USER}          # leave empty for a relay that needs no login
  password: ${SMTP_PASSWORD}
  from: podcast-bot@yourdomain.com
  from_name: "castsync"
  to: [you@yourdomain.com, cohost@yourdomain.com]
```

`verify_tls: false` is available for a self-hosted server with a self-signed certificate. Don't use it with a public provider.

**Check it:** `castsync test-email` shows the server, port, security, login and From address it will use, then sends a test report. If sending fails, it explains the likely cause:
- **`535` / authentication failed:** wrong password, or two-factor sign-in needs an app-specific password. With Zoho it can also mean the wrong server for your plan or region.
- **Relaying disallowed:** the From address doesn't match your login.
- **SSL wrong version:** port and security don't match. Use 465 with ssl, 587 with starttls.
- **Timeout:** the network is blocking that port. Try the other one.

## Libsyn modes (when `destination: libsyn`)

| `libsyn.mode` | What happens | What you need |
|---|---|---|
| `ftp` (default) | The MP3 is uploaded to your Libsyn FTP dropbox. The email says it's waiting there, and you pick it when you create the episode. | Your Libsyn login |
| `api` | The MP3 goes to FTP, then castsync creates the episode via `POST https://api.libsyn.com/post` (draft or published). | Libsyn API client ID/secret (ask Libsyn support), your `show_id` |

**Check before switching to `api` mode.** Libsyn's API documentation lists `show_id`, `item_title`, `item_slug`, `is_draft` and `release_date` as required. It says the audio is attached through a "primary content" method described on its Posts page. I couldn't read that page directly, so `primary_content_field` (default `primary_content_url`) and `primary_content_template` (default `libsyn-ftp-{content_id}`) are settings you can change. Compare them with the docs in your Libsyn account. Keep `publish_as_draft: true` for the first episode, look at the draft in Libsyn, then switch it to `false`.

## Duplicate protection

A stream is treated as already uploaded to your host (Libsyn or Transistor), and skipped, when **either** of these is true:

1. **Same title.** The titles match, ignoring case, punctuation and words like "LIVE".
2. **Same date.** A date in the stream's names matches a date in an existing episode's names.
   - Stream side: the stream title, the episode title castsync would use, and the day the stream aired (`match_air_date`, on by default).
   - Host side: each episode's title and its audio file name, plus everything castsync has uploaded before. For Libsyn these come from your RSS feed. For Transistor they come from its API, which also covers drafts and scheduled episodes.
   - Formats it reads: `2026-11-09`, `11/9/2026`, `11-09-26`, `Nov 9`, `November 9th, 2026`, `9 November`, `20261109`. Plain number pairs with no year, like "Part 1/2", are not read as dates. Month-name dates with no year take the year from the air date or publish date.

So "Big News Night" aired Nov 9 is not uploaded if your host already has "Episode 42 – November 9th, 2026" or a file called `2026-11-09-ep42.mp3`.

The same rule works within a single run. If your Twitch and YouTube copies of one stream have different titles, only one is uploaded.

**If a match is wrong.** Two different shows on the same day will look like duplicates. Whenever a stream is held back only because of a date, castsync emails you the match it found. Run `castsync allow <key>` and it uploads that stream on the next run. To match on dates written in names only, without the air date, set `matching.match_air_date: false`.

## Safety limits

`lookback_days` (7), `max_episodes_per_run` (2, newest first), `monthly_limit_mb` (Libsyn plan limit), `max_attempts` (3, after which you get an email saying it gave up). These keep the first run from uploading your whole back catalog and, on Libsyn, keep you inside your plan. All commands are listed in the [Command summary](#command-summary).

## Run without systemd

`castsync run` is a single pass. Cron works too: `5 * * * * ~/.local/bin/castsync run`

## Uninstalling

The uninstaller removes castsync's program files, the hourly timer and the yt-dlp updater. It also finds and removes copies left by **earlier versions**, including ones installed before the uninstaller existed:
- castsync systemd timers/services under any name
- cron lines that run castsync
- the old program folder and `castsync` command
- copies installed with `pip --user` or `pipx`
- with your OK, anything in `/usr/local/bin` or system-wide

```bash
castsync-uninstall                 # or: castsync uninstall
#   asks before deleting your settings, history and any audio you chose to keep

castsync-uninstall --keep-data     # remove the program only, keep settings/history/audio
castsync-uninstall --purge         # remove everything, including settings, history and kept audio
castsync-uninstall --purge --yes   # same, no questions (for scripts)
```

If you installed an older castsync that doesn't have `castsync-uninstall`, unpack this download and run `./uninstall.sh` from its folder. It cleans up any version.

It doesn't remove `ffmpeg` or Python (other programs may use them), and it doesn't turn off "linger". It tells you the commands for both. You may also want to revoke your Restream app, Transistor API key and email app password on those websites.
