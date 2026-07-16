#!/usr/bin/env python3
"""Daily cybersecurity news digest.

Collects security news from RSS feeds, deduplicates it, asks Claude to write
a brief, and emails the result. Designed to run once a day from cron or CI.
"""

import calendar
import html
import logging
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher

import feedparser
import requests
from anthropic import Anthropic

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

FEEDS = [
    ("BleepingComputer", "https://www.bleepingcomputer.com/feed/"),
    ("The Hacker News", "https://feeds.feedburner.com/TheHackersNews"),
    ("Krebs on Security", "https://krebsonsecurity.com/feed/"),
    ("Dark Reading", "https://www.darkreading.com/rss.xml"),
    ("SANS ISC", "https://isc.sans.edu/rssfeed_full.xml"),
    ("The Record", "https://therecord.media/feed/"),
    ("CISA Advisories", "https://www.cisa.gov/cybersecurity-advisories/all.xml"),
    ("Google Project Zero", "https://projectzero.google/feed.xml"),
]

LOOKBACK_HOURS = 24
MAX_ARTICLES = 60          # cap sent to the model, keeps cost predictable
MODEL = "claude-sonnet-5"
SIMILARITY_THRESHOLD = 0.72  # title similarity above which two items are "the same story"

SYSTEM_PROMPT = """You are a cybersecurity analyst writing a daily intelligence brief \
for a technical reader. You will receive raw headlines and short abstracts collected \
from security news feeds over the last 24 hours.

Write a concise brief with this structure:

1. A "Top stories" section: the 3-5 most consequential items, each with a bolded \
one-line headline followed by 2-3 sentences of context and why it matters.
2. A "Also notable" section: 4-8 single-line bullets for everything else worth knowing.
3. A "Watch list" section: 1-3 bullets on what may develop over the coming days.

Rules:
- Rank by real-world impact: actively exploited vulnerabilities, large breaches, and \
critical infrastructure incidents outrank product announcements and opinion pieces.
- Group items that cover the same underlying story into one entry.
- Cite the source name in parentheses and link the headline to its URL.
- Write in your own words. Do not quote the source text.
- If a claim is unconfirmed or vendor-supplied, say so.
- Drop marketing content, listicles, and sponsored posts entirely.
- Be direct. No preamble, no sign-off.

Output an HTML fragment only: use <h2>, <p>, <ul>, <li>, <strong>, and <a href="...">. \
No <html>, <head>, <body>, or CSS. No markdown, no code fences."""

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)s  %(message)s")
log = logging.getLogger("digest")


@dataclass
class Article:
    source: str
    title: str
    link: str
    published: datetime
    summary: str


# --------------------------------------------------------------------------
# 1. Collect
# --------------------------------------------------------------------------

def strip_html(raw: str, limit: int = 400) -> str:
    """Feed summaries arrive as HTML. Flatten to plain text."""
    text = re.sub(r"<[^>]+>", " ", raw or "")
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def entry_date(entry) -> datetime | None:
    for key in ("published_parsed", "updated_parsed"):
        parsed = getattr(entry, key, None)
        if parsed:
            return datetime.fromtimestamp(calendar.timegm(parsed), tz=timezone.utc)
    return None


def fetch_articles(cutoff: datetime) -> list[Article]:
    articles: list[Article] = []
    for source, url in FEEDS:
        try:
            parsed = feedparser.parse(url, agent="cyber-digest/1.0")
            if parsed.bozo and not parsed.entries:
                log.warning("%s: feed unreadable (%s)", source, parsed.bozo_exception)
                continue

            kept = 0
            for entry in parsed.entries:
                published = entry_date(entry)
                if published is None or published < cutoff:
                    continue
                title = strip_html(entry.get("title", ""), 200)
                link = entry.get("link", "")
                if not title or not link:
                    continue
                body = entry.get("summary", "") or entry.get("description", "")
                articles.append(
                    Article(source, title, link, published, strip_html(body))
                )
                kept += 1
            log.info("%s: %d recent item(s)", source, kept)
        except Exception as exc:  # one bad feed must not kill the run
            log.warning("%s: fetch failed (%s)", source, exc)

    articles.sort(key=lambda a: a.published, reverse=True)
    return articles


# --------------------------------------------------------------------------
# 2. Deduplicate
# --------------------------------------------------------------------------

def normalize(title: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", title.lower()).strip()


def dedupe(articles: list[Article]) -> list[Article]:
    """Drop repeat URLs and near-identical headlines across sources."""
    kept: list[Article] = []
    seen_links: set[str] = set()

    for article in articles:
        link = article.link.split("?")[0].rstrip("/")
        if link in seen_links:
            continue

        norm = normalize(article.title)
        if any(
            SequenceMatcher(None, norm, normalize(k.title)).ratio() > SIMILARITY_THRESHOLD
            for k in kept
        ):
            continue

        seen_links.add(link)
        kept.append(article)

    log.info("Deduplicated %d -> %d article(s)", len(articles), len(kept))
    return kept


# --------------------------------------------------------------------------
# 3. Summarize
# --------------------------------------------------------------------------

def summarize(articles: list[Article]) -> str:
    client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    lines = []
    for i, a in enumerate(articles[:MAX_ARTICLES], 1):
        lines.append(
            f"[{i}] {a.title}\n"
            f"    Source: {a.source} | {a.published.strftime('%Y-%m-%d %H:%M UTC')}\n"
            f"    URL: {a.link}\n"
            f"    Abstract: {a.summary}"
        )
    payload = "\n\n".join(lines)

    response = client.messages.create(
        model=MODEL,
        max_tokens=4000,
        system=SYSTEM_PROMPT,
        messages=[
            {
                "role": "user",
                "content": (
                    f"Here are {min(len(articles), MAX_ARTICLES)} security items from the "
                    f"last {LOOKBACK_HOURS} hours. Write the brief.\n\n{payload}"
                ),
            }
        ],
    )

    text = "".join(block.text for block in response.content if block.type == "text")
    return re.sub(r"^```(?:html)?\s*|\s*```$", "", text.strip())


# --------------------------------------------------------------------------
# 4. Format
# --------------------------------------------------------------------------

def build_email(body_html: str, article_count: int, feed_count: int) -> str:
    today = datetime.now(timezone.utc).strftime("%A %d %B %Y")
    return f"""<!DOCTYPE html>
<html>
<body style="margin:0;padding:24px;background:#f5f5f4;font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;">
  <div style="max-width:640px;margin:0 auto;background:#ffffff;border:1px solid #e5e5e3;border-radius:8px;padding:32px;">
    <p style="margin:0 0 4px;font-size:12px;letter-spacing:.08em;text-transform:uppercase;color:#8a8a85;">Daily brief</p>
    <h1 style="margin:0 0 24px;font-size:22px;font-weight:600;color:#1a1a19;">Cybersecurity &middot; {today}</h1>
    <div style="font-size:15px;line-height:1.65;color:#2c2c2a;">
      {body_html}
    </div>
    <hr style="border:none;border-top:1px solid #e5e5e3;margin:28px 0 12px;">
    <p style="margin:0;font-size:12px;color:#8a8a85;">
      {article_count} stories from {feed_count} sources, last {LOOKBACK_HOURS}h.
      Summaries are AI-generated &mdash; verify before acting.
    </p>
  </div>
</body>
</html>"""


# --------------------------------------------------------------------------
# 5. Send
# --------------------------------------------------------------------------

def send_email(subject: str, html_body: str) -> None:
    response = requests.post(
        "https://api.resend.com/emails",
        headers={"Authorization": f"Bearer {os.environ['RESEND_API_KEY']}"},
        json={
            "from": os.environ["EMAIL_FROM"],       # e.g. "Digest <digest@yourdomain.com>"
            "to": [os.environ["EMAIL_TO"]],
            "subject": subject,
            "html": html_body,
        },
        timeout=30,
    )
    response.raise_for_status()
    log.info("Email sent: %s", response.json().get("id"))


# --------------------------------------------------------------------------

def main() -> int:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)

    articles = dedupe(fetch_articles(cutoff))
    if not articles:
        log.warning("Nothing collected. Skipping today's send.")
        return 0

    body = summarize(articles)
    email_html = build_email(body, len(articles), len(FEEDS))

    if "--dry-run" in sys.argv:
        with open("preview.html", "w", encoding="utf-8") as fh:
            fh.write(email_html)
        log.info("Dry run: wrote preview.html (no email sent)")
        return 0

    subject = f"Cyber brief — {datetime.now(timezone.utc).strftime('%d %b %Y')}"
    send_email(subject, email_html)
    return 0


if __name__ == "__main__":
    sys.exit(main())
