"""
Job Bot — Flask Webhook Server
Receives WhatsApp replies via Twilio, handles "tailor #N" commands,
stores user CVs, calls Claude to tailor CVs, generates PDFs, sends them back.

Deploy this on Railway (free tier). Set these environment variables:
  TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_FROM,
  ANTHROPIC_API_KEY, SERVER_URL, UPDATE_SECRET
"""

import os
import json
import io
import re
import requests
import tempfile
from datetime import date
from pathlib import Path
from flask import Flask, request, Response, send_file
from twilio.rest import Client
from twilio.twiml.messaging_response import MessagingResponse
import anthropic

# ── PDF generation ─────────────────────────────────────────────────────────────
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import cm
from reportlab.lib import colors
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, HRFlowable, ListFlowable, ListItem
)
from reportlab.lib.enums import TA_LEFT, TA_CENTER

app = Flask(__name__)

# ── Config ─────────────────────────────────────────────────────────────────────
ANTHROPIC_API_KEY  = os.environ.get("ANTHROPIC_API_KEY", "")
TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN  = os.environ.get("TWILIO_AUTH_TOKEN", "")
TWILIO_FROM        = os.environ.get("TWILIO_FROM", "")
SERVER_URL         = os.environ.get("SERVER_URL", "").rstrip("/")
UPDATE_SECRET      = os.environ.get("UPDATE_SECRET", "changeme")

# ── Persistent state (JSON files on disk) ──────────────────────────────────────
DATA_DIR = Path("/data")
DATA_DIR.mkdir(exist_ok=True)

JOBS_FILE   = DATA_DIR / "jobs.json"
CVS_FILE    = DATA_DIR / "cvs.json"
STATE_FILE  = DATA_DIR / "states.json"
FILES_DIR   = DATA_DIR / "generated"
FILES_DIR.mkdir(exist_ok=True)


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text()) if path.exists() else default
    except Exception:
        return default


def save_json(path: Path, data):
    path.write_text(json.dumps(data, indent=2))


# ── Twilio helpers ─────────────────────────────────────────────────────────────

def send_whatsapp(to: str, body: str, media_url: str = None):
    """Send a WhatsApp message (with optional document attachment)."""
    client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
    kwargs = dict(from_=TWILIO_FROM, to=to, body=body)
    if media_url:
        kwargs["media_url"] = [media_url]
    client.messages.create(**kwargs)


def twiml_reply(body: str) -> Response:
    """Return a TwiML response (instant reply within the webhook)."""
    resp = MessagingResponse()
    resp.message(body)
    return Response(str(resp), mimetype="application/xml")


# ── CV text extraction ──────────────────────────────────────────────────────────

def extract_cv_text(media_url: str, content_type: str) -> str:
    """Download a CV from Twilio media URL and extract its text."""
    # Twilio requires auth to download media
    r = requests.get(
        media_url,
        auth=(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN),
        timeout=30
    )
    r.raise_for_status()

    content_type = content_type.lower()

    if "pdf" in content_type:
        import pdfplumber
        with pdfplumber.open(io.BytesIO(r.content)) as pdf:
            return "\n".join(
                page.extract_text() or "" for page in pdf.pages
            ).strip()

    elif "word" in content_type or "docx" in content_type or "openxmlformats" in content_type:
        from docx import Document
        doc = Document(io.BytesIO(r.content))
        return "\n".join(p.text for p in doc.paragraphs if p.text.strip())

    else:
        # Try plain text fallback
        return r.text.strip()


# ── Claude CV tailoring ─────────────────────────────────────────────────────────

def fetch_job_description(url: str) -> str:
    """Try to fetch the full job description from the listing URL."""
    try:
        headers = {"User-Agent": "Mozilla/5.0"}
        r = requests.get(url, headers=headers, timeout=15)
        # Basic text extraction — strip HTML tags
        text = re.sub(r"<[^>]+>", " ", r.text)
        text = re.sub(r"\s+", " ", text)
        # Return first 3000 chars (enough for JD context)
        return text[:3000].strip()
    except Exception:
        return ""


def tailor_cv_with_claude(cv_text: str, job: dict) -> str:
    """Call Claude API to tailor the CV for the job. Returns plain text CV."""
    jd_text = fetch_job_description(job.get("url", ""))

    jd_section = (
        f"Full job description (fetched from listing):\n{jd_text}"
        if jd_text
        else f"Job title: {job['title']} at {job['company']} ({job['location']}) via {job['portal']}"
    )

    prompt = f"""You are an expert CV writer specialising in ATS-optimised CVs for digital marketing roles.

Here is the candidate's current CV:
---
{cv_text}
---

Here is the target role:
---
{jd_section}
---

Please produce a fully tailored, ATS-optimised CV for this specific role. Follow these rules:

1. PROFESSIONAL SUMMARY: Write a new 3-4 sentence summary targeting this exact role. Use keywords from the job description naturally.

2. WORK EXPERIENCE: Keep all original roles. For each role, reorder bullet points so the most relevant ones (matching JD keywords) come first. Rewrite bullets to use action verbs and mirror the JD language where appropriate. Include measurable outcomes (%, numbers, £) where present.

3. SKILLS: Ensure all tools/platforms mentioned in the JD that the candidate has experience with appear in the skills section. Move the most relevant ones to the top.

4. ATS RULES: Use standard section headings only. No tables. No columns. Plain text bullet points only.

5. DO NOT invent experience that isn't present in the original CV.

Return the CV in this exact plain text format — I will convert it to PDF:

FULL NAME
email@email.com | 07XXX XXXXXX | linkedin.com/in/... | City, UK

PROFESSIONAL SUMMARY
[3-4 sentences]

WORK EXPERIENCE

Job Title — Company Name | Month YYYY – Month YYYY
- Bullet point
- Bullet point

EDUCATION

Degree Name — Institution Name | YYYY
Grade: [if present]

SKILLS
Skill 1, Skill 2, Skill 3...

[Any other sections from original CV]
"""

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=4000,
        messages=[{"role": "user", "content": prompt}]
    )
    return message.content[0].text.strip()


# ── PDF generation ──────────────────────────────────────────────────────────────

def generate_cv_pdf(cv_text: str, filename: str) -> Path:
    """Convert plain text CV to a clean ATS-safe PDF using ReportLab."""
    output_path = FILES_DIR / filename

    doc = SimpleDocTemplate(
        str(output_path),
        pagesize=A4,
        topMargin=1.8 * cm,
        bottomMargin=1.8 * cm,
        leftMargin=2 * cm,
        rightMargin=2 * cm,
    )

    # ── Styles ──────────────────────────────────────────────────────────────────
    NAME_STYLE = ParagraphStyle(
        "name", fontName="Helvetica-Bold", fontSize=18,
        alignment=TA_CENTER, spaceAfter=4, textColor=colors.HexColor("#1a1a1a")
    )
    CONTACT_STYLE = ParagraphStyle(
        "contact", fontName="Helvetica", fontSize=9,
        alignment=TA_CENTER, spaceAfter=12, textColor=colors.HexColor("#555555")
    )
    SECTION_STYLE = ParagraphStyle(
        "section", fontName="Helvetica-Bold", fontSize=11,
        spaceBefore=14, spaceAfter=4, textColor=colors.HexColor("#1a1a1a"),
        borderPadding=(0, 0, 3, 0)
    )
    BODY_STYLE = ParagraphStyle(
        "body", fontName="Helvetica", fontSize=10,
        leading=14, spaceAfter=2, textColor=colors.HexColor("#1a1a1a")
    )
    ROLE_TITLE_STYLE = ParagraphStyle(
        "role", fontName="Helvetica-Bold", fontSize=10,
        spaceBefore=8, spaceAfter=2, textColor=colors.HexColor("#1a1a1a")
    )
    BULLET_STYLE = ParagraphStyle(
        "bullet", fontName="Helvetica", fontSize=10,
        leading=13, leftIndent=14, spaceAfter=1, textColor=colors.HexColor("#1a1a1a")
    )

    story = []
    lines = cv_text.split("\n")
    i = 0
    n = len(lines)

    SECTION_HEADERS = {
        "PROFESSIONAL SUMMARY", "WORK EXPERIENCE", "EDUCATION",
        "SKILLS", "CERTIFICATIONS", "ACHIEVEMENTS", "INTERESTS", "REFERENCES"
    }

    # First line = name, second line = contact (if starts with common patterns)
    if lines:
        story.append(Paragraph(lines[0].strip(), NAME_STYLE))
        i = 1

    if i < n and ("|" in lines[i] or "@" in lines[i] or lines[i].strip().startswith("+")):
        story.append(Paragraph(lines[i].strip(), CONTACT_STYLE))
        i += 1

    while i < n:
        line = lines[i].strip()

        if not line:
            i += 1
            continue

        # Section header detection
        if line.upper() in SECTION_HEADERS or (
            line.upper().rstrip(":") in SECTION_HEADERS
        ):
            story.append(Spacer(1, 4))
            story.append(Paragraph(line.upper().rstrip(":"), SECTION_STYLE))
            story.append(HRFlowable(
                width="100%", thickness=1,
                color=colors.HexColor("#2E74B5"), spaceAfter=6
            ))
            i += 1
            continue

        # Bullet points
        if line.startswith("- ") or line.startswith("• "):
            bullet_text = line[2:].strip()
            story.append(Paragraph(f"• {bullet_text}", BULLET_STYLE))
            i += 1
            continue

        # Role title lines (contain " — " or " - " with a company name)
        if " — " in line or (" - " in line and "|" in line):
            story.append(Paragraph(line, ROLE_TITLE_STYLE))
            i += 1
            continue

        # Default: body paragraph
        story.append(Paragraph(line, BODY_STYLE))
        i += 1

    doc.build(story)
    return output_path


# ── Main webhook handler ────────────────────────────────────────────────────────

@app.route("/webhook", methods=["POST"])
def webhook():
    """Twilio calls this for every incoming WhatsApp message."""
    sender      = request.form.get("From", "")
    body        = request.form.get("Body", "").strip()
    num_media   = int(request.form.get("NumMedia", 0))
    media_url   = request.form.get("MediaUrl0", "")
    media_type  = request.form.get("MediaContentType0", "")

    jobs   = load_json(JOBS_FILE, {})
    cvs    = load_json(CVS_FILE, {})
    states = load_json(STATE_FILE, {})

    state = states.get(sender, {})

    # ── Case 1: User sent a CV file ─────────────────────────────────────────────
    if num_media > 0 and media_url:
        if not any(t in media_type.lower() for t in ["pdf", "word", "docx", "openxml"]):
            return twiml_reply(
                "⚠️ Please send your CV as a *PDF* or *Word (.docx)* file."
            )

        try:
            cv_text = extract_cv_text(media_url, media_type)
        except Exception as e:
            return twiml_reply(f"Sorry, I couldn't read that file. Try sending it as a PDF. (Error: {e})")

        # Save the CV
        cvs[sender] = cv_text
        save_json(CVS_FILE, cvs)

        pending_job_num = state.get("pending_job")

        if pending_job_num and str(pending_job_num) in jobs:
            # Clear pending state
            states.pop(sender, None)
            save_json(STATE_FILE, states)

            job = jobs[str(pending_job_num)]
            # Kick off async tailoring (respond now, send CV after)
            send_whatsapp(
                sender,
                f"✅ CV saved! ⏳ Generating your tailored ATS CV for:\n\n"
                f"*{job['title']}* at {job['company']}\n\n"
                f"This takes about 30 seconds..."
            )
            _tailor_and_send(sender, job, cv_text, pending_job_num)
            return twiml_reply("")  # Empty TwiML (we already sent a message above)
        else:
            # CV saved but no pending job
            states.pop(sender, None)
            save_json(STATE_FILE, states)
            return twiml_reply(
                "✅ CV saved! Now reply *tailor #N* with any job number from this morning's list to generate your tailored CV."
            )

    # ── Case 2: "tailor #N" command ────────────────────────────────────────────
    tailor_match = re.search(r"tailor\s*#?(\d+)", body, re.IGNORECASE)
    if tailor_match:
        job_num = tailor_match.group(1)

        if not jobs:
            return twiml_reply("No jobs found yet — check back after 9am tomorrow!")

        if job_num not in jobs:
            return twiml_reply(
                f"Job #{job_num} wasn't in today's list. "
                f"Valid numbers are 1–{len(jobs)}. Try again!"
            )

        job = jobs[job_num]
        cv_text = cvs.get(sender)

        if not cv_text:
            # Ask for CV
            state["pending_job"] = job_num
            states[sender] = state
            save_json(STATE_FILE, states)
            return twiml_reply(
                f"Great choice! 📄 To tailor your CV for:\n\n"
                f"*{job['title']}* at {job['company']}\n\n"
                f"Please send me your CV as a *PDF* or *Word (.docx)* file."
            )
        else:
            # CV already on file — go straight to tailoring
            send_whatsapp(
                sender,
                f"⏳ Generating your tailored ATS CV for:\n\n"
                f"*{job['title']}* at {job['company']}\n\n"
                f"~30 seconds..."
            )
            _tailor_and_send(sender, job, cv_text, job_num)
            return twiml_reply("")

    # ── Case 3: "help" or unrecognised ────────────────────────────────────────
    if "help" in body.lower() or "?" in body:
        return twiml_reply(
            "🤖 *Job Bot commands:*\n\n"
            "• *tailor #3* — tailor your CV for job #3 from today's list\n"
            "• *Send a CV file* — upload your PDF or Word CV (saved for future)\n"
            "• *help* — show this message\n\n"
            "_Jobs are refreshed every morning at 9am._"
        )

    return twiml_reply(
        "Reply *tailor #N* (e.g. tailor #3) to tailor your CV for a job from today's list, "
        "or send *help* for options."
    )


def _tailor_and_send(sender: str, job: dict, cv_text: str, job_num):
    """Tailor CV via Claude, generate PDF, send back via WhatsApp."""
    try:
        tailored_text = tailor_cv_with_claude(cv_text, job)

        # Generate filename
        safe_title   = re.sub(r"[^\w\s-]", "", job["title"])[:30].strip()
        safe_company = re.sub(r"[^\w\s-]", "", job["company"])[:20].strip()
        filename = f"CV_{safe_title}_{safe_company}_{date.today().isoformat()}.pdf"
        filename = filename.replace(" ", "_")

        pdf_path = generate_cv_pdf(tailored_text, filename)

        # Build public URL for the PDF
        pdf_url = f"{SERVER_URL}/files/{filename}"

        send_whatsapp(
            sender,
            f"✅ *Your tailored ATS CV is ready!*\n\n"
            f"Role: *{job['title']}* at {job['company']}\n"
            f"📎 Download your CV below\n\n"
            f"_Keywords matched, experience reordered, summary rewritten for this role._\n\n"
            f"Reply *tailor #N* for another role!",
            media_url=pdf_url
        )

    except Exception as e:
        send_whatsapp(
            sender,
            f"Sorry, something went wrong generating your CV. 😕\n"
            f"Error: {str(e)[:200]}\n\nPlease try again in a moment."
        )


# ── Job update endpoint (called by GitHub Actions each morning) ────────────────

@app.route("/update-jobs", methods=["POST"])
def update_jobs():
    """Receives the day's job list from the morning cron job."""
    secret = request.headers.get("X-Update-Secret", "")
    if secret != UPDATE_SECRET:
        return {"error": "Unauthorized"}, 403

    data = request.get_json(force=True)
    jobs = data.get("jobs", {})
    save_json(JOBS_FILE, jobs)
    return {"status": "ok", "count": len(jobs)}, 200


# ── Serve generated CV PDFs ────────────────────────────────────────────────────

@app.route("/files/<filename>")
def serve_file(filename: str):
    """Serve a generated CV PDF (Twilio needs a public URL)."""
    # Basic security: only serve files from our generated dir
    safe_name = Path(filename).name
    file_path = FILES_DIR / safe_name
    if not file_path.exists():
        return {"error": "Not found"}, 404
    return send_file(str(file_path), mimetype="application/pdf", as_attachment=True)


@app.route("/health")
def health():
    return {"status": "ok", "date": date.today().isoformat()}, 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
