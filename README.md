# nest

**A housing watch for students who move for every internship.**

Every six months, a new city, a new internship, the same hunt: refreshing Leboncoin, guessing
commute times from a map, writing the same message to fifty landlords, dodging fake agencies.
nest does the watching for you. It reads new rental listings every 10 minutes, computes the real
door-to-door commute to your internship, has Claude judge each listing against *your* criteria,
and sends a push notification to your phone only for the ones worth your time, with a first
message to the landlord already drafted. You choose, you send.

nest is in French (it targets the French rental market) and runs on your own computer.

## What it does

```
Leboncoin (+ Gens de Confiance, optional)
   → free filters: budget, availability date, short-term rentals, distance as the crow flies
   → real commute with Google Routes (transit, walking, optional motorbike)
   → duplicate detection across sites and reposts
   → Claude judges each remaining listing, criterion by criterion, and flags scams
   → score out of 100 → push notification (ntfy) above your threshold
```

Each notification shows the score, the commute ("22 min · Parmentier 4 min on foot · M3 › M11 ·
1 change"), what matches, what doesn't, scam warnings, and a ready-to-send first message.

- **Your criteria, not a template.** A guided interview in Claude Code asks how you live (your
  days, your past flats, forced trade-offs like "15 m² at 15 min or 22 m² at 30 min?") and turns
  it into 15 to 30 weighted criteria. Only the commute is a hard limit; everything else ranks,
  nothing else eliminates.
- **Scam filter.** Fake agencies, payment before viewing, "owner abroad": risky listings never
  reach you. A "Scam" button on the notification blocks the account for good.
- **Messages that don't read as AI.** Short, generic on purpose (no copied listing details), built
  from your own situation and your own model message.
- **Optional extras.** Read-only Gmail triage of agency replies (with scam signals and a tracking
  table), a Notion mirror of that table, and a small "copy the message and open the listing"
  page you can host for free.

## Screenshots

<!-- Captures à ajouter dans docs/captures/ puis à référencer ici. -->
| Notification | Message bridge page | Replay report |
|---|---|---|
| ![Notification](docs/captures/notification.png) | ![Bridge page](docs/captures/ecrire.png) | ![Replay report](docs/captures/rejeu.png) |

## Install in 3 steps (with Claude Code)

You need macOS or Linux (Windows is not supported), Python 3.10 or later, and
[Claude Code](https://claude.com/claude-code).

1. **Clone the repo** somewhere outside Documents, Desktop and Downloads (macOS blocks scheduled
   tasks from running scripts there):
   ```bash
   git clone https://github.com/theodvd/nest-logement.git ~/nest && cd ~/nest
   ```
2. **Open Claude Code** in that folder:
   ```bash
   claude
   ```
3. **Start the interview**: type `/entretien-recherche`.

The interview does the rest: it installs the Python environment, asks about your search and
writes `profil.yaml`, walks you through getting your keys and stores them in `.env`, schedules
nest (launchd on macOS, cron on Linux), and runs a first dry run where nothing is sent.

Keys you will be asked for:

| Key | What for | Where |
|---|---|---|
| `ANTHROPIC_API_KEY` | Claude judges the listings | [console.anthropic.com](https://console.anthropic.com) (prepaid credit) |
| `GOOGLE_MAPS_API_KEY` | Real commute times | Google Cloud, enable "Routes API" (billing account required, usage stays in the free tier) |
| `NTFY_TOPIC` | Push notifications | Any random secret name, then subscribe in the [ntfy](https://ntfy.sh) app |

## Monthly cost

Estimated from a real instance running since October 2026, on a big-city search for studios and
flatshares:

| | Volume | Cost |
|---|---|---|
| Claude (Sonnet 5) | 43 to 60 listings judged per day, about 1,300 to 1,800 per month, at about $0.018 each (measured over 359 judgments) | **about $23 to $32 / month** |
| Google Routes | about 150 route calls per day, about 4,500 per month (transit and walking are "Essentials", free up to 10,000 per month; the optional motorbike mode uses "Pro", free up to 5,000) | **$0** |
| ntfy, OpenStreetMap | | **$0** |

A big city means many listings; a smaller city or a tighter budget means fewer listings, so fewer
judgments and a smaller bill. Two settings cap the spend: listings are
filtered for free before Claude sees them, and `notif.max_jugements_par_passage` (15 by default)
limits judgments per run. Prices checked on 7 November 2026: Claude Sonnet 5 at $2 / $10 per
million input / output tokens; [Google Maps Platform pricing](https://developers.google.com/maps/billing-and-pricing/pricing).

## Everyday commands

```bash
.venv/bin/python -m nest --verifier          # check your profile and every key, sends nothing
.venv/bin/python -m nest --sec --max 3       # dry run: prints notifications, sends and saves nothing
.venv/bin/python -m nest --test-notif        # test notification on your phone
.venv/bin/python -m nest --signaler <url>    # block the account behind a listing (scam)
.venv/bin/python -m nest.planifier --retirer # stop the scheduled task
tail -f data/nest.log                        # log of scheduled runs
```

To change your criteria, run `/entretien-recherche` again. To calibrate your notification
threshold on last month's listings, see `nest/rejeu.py` (it reads up to 40 pages at once: run it
rarely).

## Optional sources and features

- **Gens de Confiance** (members-only listings): set `GDC_COOKIE` in `.env` with the cookie of
  your logged-in session. Without it, the source is skipped silently. The site centers its
  searches on Paris, so this source only helps for a search in the Paris region.
- **Message bridge page** (`ecrire/index.html`): a static page that copies the drafted message and
  opens the listing in one tap, handy on iPhone. Host it anywhere (GitHub Pages works) and put its
  URL in `notif.page_ecrire`. Without it, the message is in the notification itself. The message
  travels in the URL fragment and is never sent to any server.
- **Gmail triage** (read-only `gmail.readonly` scope): classifies agency replies, flags scams,
  tracks each application. Setup in `nest/mails/gmail.py`.
- **Notion mirror** of the application tracker: see `nest/notion.py`.

## How scoring works

Each criterion in `profil.yaml` gets a satisfaction between 0 and 1, either measured by code
(surface, rent, commute, number of changes, distance to a point, nearby park, landlord account
age) or judged by Claude from the listing (yes = 1, partial = 0.5, unknown = 0.5, no = 0). The
score is `100 × Σ(weight × satisfaction) / Σ(weight)`. Only the commute eliminates a listing;
a missed criterion just ranks it lower. See `profil.example.yaml` for a commented example.

## ⚠️ Warning: read before using

- **Leboncoin access goes through [`lbc`](https://github.com/etienne-hd/lbc), an unofficial
  library.** It is not an API offered by Leboncoin.
- **The Gens de Confiance source uses your own logged-in session and imitates a browser to get
  past Cloudflare.**
- **This may breach the terms of service of these sites. Your account is exposed:** it could be
  limited or banned. Keep the frequency low (10 minutes by default, never below 5) and do not
  run the replay tool often.
- **Use at your own risk.** nest is a personal project, not affiliated with, endorsed by or
  connected to Leboncoin, Gens de Confiance or any listing site.
- nest never contacts anyone on your behalf: it drafts, you send. Your profile, keys and data
  stay on your computer (`profil.yaml`, `.env` and `data/` are git-ignored); listings and your
  profile are sent to the Anthropic API for judging.

## License

[MIT](LICENSE)
