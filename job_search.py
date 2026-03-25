"""
Daily UK Digital Marketing Job Search
Runs at 9am via GitHub Actions. Does three things:
  1. Calls Claude API to search for jobs across 6 UK portals
  2. Sends the job list to your WhatsApp via Twilio
  3. POSTs the structured job list to the Flask server (so "tailor #N" works)
"""

import os
import re
import json
import requests
from datetime import date
import anthropic
from twilio.rest import Client

# ── Config ─────────────────────────────────────────────────────────────────────
ANTHROPIC_API_KEY  = os.environ["ANTHROPIC_API_KEY"]
TWILIO_ACCOUNT_SID = os.environ["TWILIO_ACCOUNT_SID"]
TWILIO_AUTH_TOKEN  = os.environ["TWILIO_AUTH_TOKEN"]
TWILIO_FROM        = os.environ["TWILIO_FROM"]
WHATSAPP_TO        = os.environ["WHATSAPP_TO"]
SERVER_URL         = os.environ["SERVER_URL"].rstrip("/")
UPDATE_SECRET      = os.environ["UPDATE_SECRET"]

SEARCH_PROMPT = """
Search the following 6 UK job portals for entry-level, junior, graduate, or assistant
digital marketing roles posted within the last 3 days.

Target job titles:
1. Digital Marketing Executive (junior / entry-level / graduate)
2. PPC / Paid Media Executive (junior / entry-level / graduate)
3. Digital Marketing Assistant

Use these date-filtered URLs:
- Indeed UK: https://uk.indeed.com/jobs?q=QUERY&fromage=3
- Reed: https://www.reed.co.uk/jobs/QUERY-jobs?datecreated=3
- Totaljobs: https://www.totaljobs.com/jobs/QUERY?postedWithin=3
- CV-Library: https://www.cv-library.co.uk/search-jobs?q=QUERY&posted=3
- Guardian Jobs: https://jobs.theguardian.com/jobs/marketing/?t=3
- LinkedIn: search site:linkedin.com/jobs with "1 day ago" OR "2 days ago" OR "3 days ago"

Return ONLY a JSON array of job objects. No extra text, no markdown, no explanation.
Each object must have exactly these fields:
  title, company, location, portal, url

Example:
[
  {"title": "Digital Marketing Assistant", "company": "Acme Ltd", "location": "London", "portal": "Reed", "url": "https://www.reed.co.uk/jobs/..."},
  {"title": "Junior PPC Executive", "company": "MediaCom", "location": "Manchester", "portal": "Indeed", "url": "https://uk.indeed.com/..."}
]

Only include real jobs posted in the last 3 days. Do not fabricate listings.
If no jobs found, return an empty array: []
"""


def run_job_search() -> list[dict]:
    """Call Claude API and return structured list of jobs."""
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    print("🔍 Calling Claude API for job search...")
    message = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=4096,
        messages=[{"role": "user", "content": SEARCH_PROMPT}]
    )
    raw = message.content[0].text.strip()

    # Strip any accidental markdown fences
    raw = re.sub(r"```(?:json)?", "", raw).strip()

    try:
        jobs = json.loads(raw)
        print(f"✅ Found {len(jobs)} jobs")
        return jobs
    except json.JSONDecodeError:
        print(f"⚠️  Claude returned non-JSON: {raw[:200]}")
        return []


def post_jobs_to_server(jobs_dict: dict):
    """Send the structured job list to the Flask server for "tailor #N" to work."""
    url = f"{SERVER_URL}/update-jobs"
    print(f"📡 Posting {len(jobs_dict)} jobs to server...")
    try:
        r = requests.post(
            url,
            json={"jobs": jobs_dict},
            headers={"X-Update-Secret": UPDATE_SECRET},
            timeout=15
        )
        r.raise_for_status()
        print(f"✅ Server updated: {r.json()}")
    except Exception as e:
        print(f"⚠️  Could not update server: {e}")


def format_whatsapp_messages(jobs: list[dict], today: str) -> list[str]:
    """Format jobs into WhatsApp message chunks with job numbers."""
    if not jobs:
        return [
            f"🎯 *Daily Digital Marketing Jobs — {today}*\n\n"
            "No new roles found today. Check back tomorrow!\n\n"
            "_Reply *help* for options._"
        ]

    messages = []

    # Header
    messages.append(
        f"🎯 *Daily Digital Marketing Jobs*\n"
        f"📅 {today}\n"
        f"{'─' * 28}\n"
        f"Entry-level · Junior · Graduate roles\n"
        f"Posted in the last 3 days\n"
        f"_{len(jobs)} roles found today_"
    )

    # Jobs (3 per message to stay under 1600 char limit)
    chunk_jobs = []
    chunk_len = 0

    for num, job in enumerate(jobs, 1):
        block = (
            f"*#{num} — {job['title']}*\n"
            f"🏢 {job['company']}\n"
            f"📍 {job['location']} · {job['portal']}\n"
            f"🔗 {job['url']}\n"
        )
        if chunk_len + len(block) > 1400 and chunk_jobs:
            messages.append("\n".join(chunk_jobs))
            chunk_jobs = []
            chunk_len = 0
        chunk_jobs.append(block)
        chunk_len += len(block)

    if chunk_jobs:
        messages.append("\n".join(chunk_jobs))

    # Footer
    messages.append(
        f"{'─' * 28}\n"
        f"💡 *Reply* _tailor #3_ to get an ATS-optimised CV\n"
        f"for any role above — sent back to you in ~30 seconds!"
    )

    return messages


def send_whatsapp_messages(messages: list[str]):
    """Send all message chunks via Twilio."""
    client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
    print(f"💬 Sending {len(messages)} WhatsApp message(s)...")
    for i, body in enumerate(messages, 1):
        client.messages.create(
            from_=TWILIO_FROM,
            to=WHATSAPP_TO,
            body=body
        )
        print(f"  ✅ Sent {i}/{len(messages)}")


def main():
    today = date.today().strftime("%d %B %Y")
    print(f"🚀 Daily job search starting — {today}")

    # 1. Search for jobs
    jobs_list = run_job_search()

    # 2. Number them (1-indexed dict for the server)
    jobs_dict = {str(i + 1): job for i, job in enumerate(jobs_list)}

    # 3. Post to Flask server (enables "tailor #N")
    post_jobs_to_server(jobs_dict)

    # 4. Format and send WhatsApp messages
    messages = format_whatsapp_messages(jobs_list, today)
    send_whatsapp_messages(messages)

    print("🎉 Done!")


if __name__ == "__main__":
    main()
