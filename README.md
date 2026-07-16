# Daily cybersecurity digest

An agent that reads security RSS feeds every morning, has Claude write a brief, and emails it to you.

## Setup

**1. Install**

```bash
pip install -r requirements.txt
```

**2. Get your keys**

- `ANTHROPIC_API_KEY` — from https://console.anthropic.com
- `RESEND_API_KEY` — from https://resend.com (free tier: 100 emails/day, plenty for one recipient)

Resend requires a verified sender domain. If you don't have one, use their `onboarding@resend.dev` sender for testing, or swap `send_email()` for SMTP (see below).

**3. Set environment variables**

```bash
export ANTHROPIC_API_KEY="sk-ant-..."
export RESEND_API_KEY="re_..."
export EMAIL_FROM="Cyber Digest <digest@yourdomain.com>"
export EMAIL_TO="you@example.com"
```

**4. Test without sending**

```bash
python digest.py --dry-run   # writes preview.html, open it in a browser
```

**5. Schedule it**

Push this repo to GitHub, then add the four variables above as repository secrets under
`Settings → Secrets and variables → Actions`. The workflow in `.github/workflows/daily-digest.yml`
runs at 05:30 UTC daily. Use the "Run workflow" button in the Actions tab to test it live.

## Tuning

| What | Where |
|---|---|
| News sources | `FEEDS` list in `digest.py` |
| Digest structure, tone, ranking | `SYSTEM_PROMPT` |
| Time window | `LOOKBACK_HOURS` |
| Cost ceiling | `MAX_ARTICLES` (~60 items ≈ a few cents per run) |
| Dedup aggressiveness | `SIMILARITY_THRESHOLD` (lower = more merging) |
| Send time | `cron` in the workflow (UTC) |

The prompt is where most of the quality lives. Run `--dry-run` a few days in a row, note what
annoys you in the output, and encode the fix as a rule in `SYSTEM_PROMPT`.

## Gmail instead of Resend

Replace `send_email()` with:

```python
import smtplib
from email.message import EmailMessage

def send_email(subject: str, html_body: str) -> None:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = os.environ["EMAIL_FROM"]
    msg["To"] = os.environ["EMAIL_TO"]
    msg.set_content("HTML email — view in a client that supports it.")
    msg.add_alternative(html_body, subtype="html")

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(os.environ["EMAIL_FROM"], os.environ["GMAIL_APP_PASSWORD"])
        server.send_message(msg)
```

Use a Google app password, not your account password.

## Notes

- Only headlines and feed abstracts are sent to the model, never scraped article bodies.
- A dead feed logs a warning and the run continues.
- A day with no news skips the send rather than mailing an empty brief.
