# LinkHarbor uptime

A single-file Python agent that checks one URL on an interval and pushes
the result to your [LinkHarbor](https://linkharbor.com) bio page: a status
card with Up/Down and the response time ("184 ms"), an "Open" button, plus
events for push notifications to your followers when the site goes down or
comes back.

It runs anywhere with Python and outbound HTTPS, for example on a small
VPS or a Raspberry Pi. One agent checks one URL; run it twice for two
sites. Standard library only, Python 3.10 or newer.

Part of the [LinkHarbor integrations](https://github.com/linkharbor/integrations)
family.

## Setup

```bash
git clone https://github.com/linkharbor/integration-uptime
cd integration-uptime

export LH_INGEST_URL="https://dock.at-harbor.com/v1/ingest/123"   # dashboard: Integrations, Custom (API)
export LH_INGEST_SECRET="…"

python3 agent.py --url-check https://example.com --title "My website" \
  --expect-text "Welcome" --interval 60
```

Start with `--once -v`. Run it as a service with `integration-uptime.service`
(systemd; secrets go into `/etc/integration-uptime.env`, `chmod 600`).

A check passes when the HTTP status is within `--expect-status` (default
`200-399`; redirects are followed, so the final status counts) and, if
given, the body contains `--expect-text`. A timeout, DNS or TLS error
fails the check. The site counts as down after `--fails-before-down`
consecutive failures (default 2), so one lost packet does not notify your
followers.

## Events sent

| Event | When | Template variables |
|---|---|---|
| `site.up` | first passing check, or back up after being down | `{downtime_minutes}`, `{ms}`, `{title}`, `{value}` |
| `site.down` | `--fails-before-down` checks failed in a row | `{error}`, `{status}`, `{title}` |
| `response.slow` | a passing check took longer than `--slow-ms` | `{ms}`, `{status}`, `{title}` |

The state is sent with every event and every `--heartbeat` minutes (default
10). While down, the card shows the last error as its text.

Suggested push rule (dashboard, Integrations, Push rules, Custom (API), event `site.down`): title "🎮 {title} is down", body "{error}", minimum pause
30 min. And for `site.up`: title "{title} is back", body "down for
{downtime_minutes} min".

## Options

```
--url-check URL        URL to check (required)
--method GET|HEAD      HTTP method (default GET)
--expect-status SPEC   accepted status codes, e.g. 200 or 200-299,304 (default 200-399)
--expect-text TEXT     the body must contain this text (GET only)
--timeout SECONDS      seconds to wait for a response (default 10)
--fails-before-down N  consecutive failures before "down" (default 2)
--slow-ms MS           send response.slow above this response time (default 0 = off)
--title TEXT           card title (default: the host)
--name TEXT            same as --title
--address TEXT         text shown on the card while up (default: the HTTP status)
--url URL              link behind the card's button (default: the checked URL)
--cta TEXT             button label (default "Open")
--image-url URL        banner image (large layout)
--interval SECONDS     check interval (default 60, minimum 5)
--heartbeat MINUTES    state-only update interval (default 10, 0 = only on change)
--once                 check once, send, exit (exit code 1 when the check failed)
-v                     verbose logging
```

## License

[MIT](LICENSE). © 2026 EMI Solutions OÜ.
