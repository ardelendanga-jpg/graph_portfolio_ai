"""
HIV & Hypertension Literature Digest
Fetches recent PubMed articles, summarizes them with Claude AI,
and sends a monthly email digest to clinicians.
"""

import os
import smtplib
import urllib.request
import urllib.parse
import json
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText


# ─────────────────────────────────────────────
# CONFIGURATION — Edit these values
# ─────────────────────────────────────────────

# PubMed search terms (customize as needed)
SEARCH_QUERIES = [
    "HIV hypertension management Africa",
    "antiretroviral therapy cardiovascular Africa",
    "hypertension treatment sub-Saharan Africa"
]

# Number of articles per digest
MAX_ARTICLES = 10

# How many days back to search (e.g. 35 covers last month)
DAYS_BACK = 35

# Claude API key — set as environment variable ANTHROPIC_API_KEY
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

# Email settings — set as environment variables
SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587
EMAIL_SENDER = os.environ.get("EMAIL_SENDER", "")       # your Gmail address
EMAIL_PASSWORD = os.environ.get("EMAIL_PASSWORD", "")   # Gmail App Password
EMAIL_RECIPIENTS = os.environ.get("EMAIL_RECIPIENTS", "").split(",")  # comma-separated list

# Clinic/organisation name (appears in email)
CLINIC_NAME = "HIV & Hypertension Care Programme"


# ─────────────────────────────────────────────
# STEP 1: FETCH ARTICLES FROM PUBMED
# ─────────────────────────────────────────────

def build_date_range():
    end = datetime.today()
    start = end - timedelta(days=DAYS_BACK)
    return start.strftime("%Y/%m/%d"), end.strftime("%Y/%m/%d")


def search_pubmed(query, max_results=5):
    """Search PubMed and return list of PMIDs."""
    start_date, end_date = build_date_range()
    full_query = f'{query} AND ("{start_date}"[Date - Publication] : "{end_date}"[Date - Publication])'

    params = urllib.parse.urlencode({
        "db": "pubmed",
        "term": full_query,
        "retmax": max_results,
        "retmode": "json",
        "sort": "relevance"
    })
    url = f"https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi?{params}"

    with urllib.request.urlopen(url) as response:
        data = json.loads(response.read())
    return data.get("esearchresult", {}).get("idlist", [])


def fetch_article_details(pmids):
    """Fetch title, abstract, authors, journal for a list of PMIDs."""
    if not pmids:
        return []

    ids = ",".join(pmids)
    params = urllib.parse.urlencode({
        "db": "pubmed",
        "id": ids,
        "retmode": "xml"
    })
    url = f"https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi?{params}"

    with urllib.request.urlopen(url) as response:
        xml_data = response.read()

    root = ET.fromstring(xml_data)
    articles = []

    for article in root.findall(".//PubmedArticle"):
        try:
            pmid = article.findtext(".//PMID", "")
            title = article.findtext(".//ArticleTitle", "No title available")
            abstract = article.findtext(".//AbstractText", "")
            journal = article.findtext(".//Journal/Title", "Unknown Journal")
            year = article.findtext(".//PubDate/Year", "")

            # Get author list
            authors = []
            for author in article.findall(".//Author")[:3]:
                last = author.findtext("LastName", "")
                fore = author.findtext("ForeName", "")
                if last:
                    authors.append(f"{last} {fore}".strip())
            author_str = ", ".join(authors)
            if len(article.findall(".//Author")) > 3:
                author_str += " et al."

            if abstract:  # Only include articles with abstracts
                articles.append({
                    "pmid": pmid,
                    "title": title,
                    "abstract": abstract,
                    "journal": journal,
                    "year": year,
                    "authors": author_str,
                    "url": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/"
                })
        except Exception as e:
            print(f"Error parsing article: {e}")
            continue

    return articles


def get_articles():
    """Collect unique articles across all search queries."""
    seen_pmids = set()
    all_articles = []

    for query in SEARCH_QUERIES:
        print(f"Searching PubMed: {query}")
        pmids = search_pubmed(query, max_results=6)
        new_pmids = [p for p in pmids if p not in seen_pmids]
        seen_pmids.update(new_pmids)

        if new_pmids:
            articles = fetch_article_details(new_pmids)
            all_articles.extend(articles)

        if len(all_articles) >= MAX_ARTICLES:
            break

    return all_articles[:MAX_ARTICLES]


# ─────────────────────────────────────────────
# STEP 2: SUMMARIZE WITH CLAUDE API
# ─────────────────────────────────────────────

def summarize_with_claude(article):
    """Send abstract to Claude and return a plain-language clinician summary."""
    if not ANTHROPIC_API_KEY:
        return "AI summary not available — please configure ANTHROPIC_API_KEY."

    prompt = f"""You are a medical communication specialist helping clinicians in Southern Africa stay updated on HIV and hypertension management.

Write a plain-language summary of the following research article abstract. The summary should:
- Be 3-5 sentences long
- Highlight the key clinical finding or recommendation
- Mention why it is relevant to clinicians managing HIV or hypertension in a Southern African context
- Avoid heavy jargon — write for a busy clinician, not a researcher
- End with one practical takeaway sentence starting with "Clinical takeaway:"

Article title: {article['title']}

Abstract: {article['abstract']}

Write only the summary, no preamble."""

    request_data = json.dumps({
        "model": "claude-sonnet-4-20250514",
        "max_tokens": 300,
        "messages": [{"role": "user", "content": prompt}]
    }).encode("utf-8")

    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=request_data,
        headers={
            "Content-Type": "application/json",
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01"
        }
    )

    try:
        with urllib.request.urlopen(req) as response:
            result = json.loads(response.read())
        return result["content"][0]["text"]
    except Exception as e:
        print(f"Claude API error: {e}")
        return "Summary could not be generated for this article."


# ─────────────────────────────────────────────
# STEP 3: BUILD HTML EMAIL
# ─────────────────────────────────────────────

def build_email_html(articles_with_summaries):
    month_year = datetime.today().strftime("%B %Y")

    article_blocks = ""
    for i, item in enumerate(articles_with_summaries, 1):
        a = item["article"]
        summary = item["summary"]
        article_blocks += f"""
        <div style="background:#ffffff; border-left:4px solid #2563eb; margin-bottom:28px;
                    padding:20px 24px; border-radius:4px; box-shadow:0 1px 3px rgba(0,0,0,0.07);">
            <p style="margin:0 0 4px 0; font-size:13px; color:#6b7280; font-weight:500;">
                Article {i} &nbsp;|&nbsp; {a['journal']} {a['year']}
            </p>
            <h3 style="margin:0 0 6px 0; font-size:17px; color:#111827; line-height:1.4;">
                {a['title']}
            </h3>
            <p style="margin:0 0 12px 0; font-size:13px; color:#9ca3af;">{a['authors']}</p>
            <p style="margin:0 0 16px 0; font-size:15px; color:#374151; line-height:1.7;">
                {summary}
            </p>
            <a href="{a['url']}" style="display:inline-block; background:#2563eb; color:#ffffff;
               text-decoration:none; padding:8px 18px; border-radius:5px; font-size:13px;
               font-weight:600;">
                Read Full Article on PubMed →
            </a>
        </div>
        """

    html = f"""<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"></head>
<body style="margin:0; padding:0; background:#f3f4f6; font-family: -apple-system, BlinkMacSystemFont,
             'Segoe UI', Roboto, sans-serif;">

  <div style="max-width:660px; margin:32px auto; background:#f3f4f6; padding:0 16px 40px;">

    <!-- Header -->
    <div style="background:linear-gradient(135deg,#1e40af,#2563eb); border-radius:8px 8px 0 0;
                padding:32px 32px 24px; text-align:center;">
      <p style="margin:0 0 4px; color:#93c5fd; font-size:13px; font-weight:600; letter-spacing:1px;
                text-transform:uppercase;">{CLINIC_NAME}</p>
      <h1 style="margin:0 0 8px; color:#ffffff; font-size:26px; font-weight:700;">
        Monthly Literature Digest
      </h1>
      <p style="margin:0; color:#bfdbfe; font-size:15px;">{month_year} &nbsp;·&nbsp; HIV & Hypertension</p>
    </div>

    <!-- Intro -->
    <div style="background:#eff6ff; border-bottom:1px solid #dbeafe; padding:18px 32px;">
      <p style="margin:0; font-size:14px; color:#1e40af; line-height:1.6;">
        <strong>👋 Hello,</strong> here are this month's key articles on HIV and hypertension management,
        with plain-language summaries to help you stay current. All articles are sourced from PubMed
        and selected for relevance to Southern African clinical practice.
      </p>
    </div>

    <!-- Articles -->
    <div style="background:#f3f4f6; padding:24px 16px 8px;">
      {article_blocks}
    </div>

    <!-- Footer -->
    <div style="text-align:center; padding:20px 16px 0;">
      <p style="margin:0 0 6px; font-size:13px; color:#9ca3af;">
        Summaries generated by AI. Always verify clinical decisions with full articles and guidelines.
      </p>
      <p style="margin:0; font-size:12px; color:#d1d5db;">
        {CLINIC_NAME} &nbsp;·&nbsp; Automated Literature Digest &nbsp;·&nbsp; {month_year}
      </p>
    </div>

  </div>
</body>
</html>"""
    return html


# ─────────────────────────────────────────────
# STEP 4: SEND EMAIL
# ─────────────────────────────────────────────

def send_email(html_content):
    month_year = datetime.today().strftime("%B %Y")
    subject = f"📋 HIV & Hypertension Literature Digest — {month_year}"

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = EMAIL_SENDER
    msg["To"] = ", ".join(EMAIL_RECIPIENTS)
    msg.attach(MIMEText(html_content, "html"))

    print(f"Sending email to: {EMAIL_RECIPIENTS}")
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
        server.ehlo()
        server.starttls()
        server.login(EMAIL_SENDER, EMAIL_PASSWORD)
        server.sendmail(EMAIL_SENDER, EMAIL_RECIPIENTS, msg.as_string())
    print("✅ Email sent successfully.")


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────

def main():
    print("=" * 50)
    print(f"HIV & Hypertension Digest — {datetime.today().strftime('%Y-%m-%d')}")
    print("=" * 50)

    # 1. Fetch articles
    print("\n📥 Fetching articles from PubMed...")
    articles = get_articles()
    print(f"   Found {len(articles)} articles with abstracts.")

    if not articles:
        print("No articles found. Exiting.")
        return

    # 2. Summarize each article
    print("\n🤖 Generating AI summaries...")
    articles_with_summaries = []
    for i, article in enumerate(articles, 1):
        print(f"   Summarizing {i}/{len(articles)}: {article['title'][:60]}...")
        summary = summarize_with_claude(article)
        articles_with_summaries.append({"article": article, "summary": summary})

    # 3. Build email
    print("\n📧 Building email digest...")
    html = build_email_html(articles_with_summaries)

    # Save a local preview
    with open("digest_preview.html", "w", encoding="utf-8") as f:
        f.write(html)
    print("   Preview saved to digest_preview.html")

    # 4. Send email
    if EMAIL_SENDER and EMAIL_PASSWORD and EMAIL_RECIPIENTS[0]:
        send_email(html)
    else:
        print("⚠️  Email credentials not configured. Preview saved locally only.")

    print("\n✅ Done!")


if __name__ == "__main__":
    main()
