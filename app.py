"""
Lead Time signup handler.
Receives form submissions from leadtime.news, adds them to the Mailchimp
Lead Time audience AND to the beehiiv Lead Time publication, serves the
static site files, and emails Karen a notification for each signup.

Transition design (July 2026):
  - Newsletter sign-ups go to BOTH Mailchimp and beehiiv.
  - Guide-only downloaders (didn't tick the box) go to Mailchimp only.
  - Neither service can block the other, and neither can block the
    guide download. The visitor always gets the guide.
"""
import os
import hashlib
import smtplib
import threading
from datetime import datetime
from email.message import EmailMessage

import requests
from flask import Flask, request, jsonify, send_from_directory, Response

# Calgary timestamps for the notification emails. If the timezone database
# is ever unavailable on the server, we quietly fall back to UTC rather
# than breaking anything.
try:
    from zoneinfo import ZoneInfo
    CALGARY_TZ = ZoneInfo('America/Edmonton')
except Exception:
    CALGARY_TZ = None

app = Flask(__name__, static_folder='.', static_url_path='')

# Mailchimp configuration - all secrets pulled from Render environment variables
MAILCHIMP_API_KEY = os.environ.get('MAILCHIMP_API_KEY', '')
MAILCHIMP_AUDIENCE_ID = os.environ.get('MAILCHIMP_AUDIENCE_ID', 'de5f89484c')
# The data center is the suffix on the API key (e.g. 'us17' from a key ending in '-us17')
MAILCHIMP_DC = MAILCHIMP_API_KEY.split('-')[-1] if '-' in MAILCHIMP_API_KEY else 'us17'

# beehiiv configuration. The API key is a secret and lives only in Render.
# The publication ID is not a secret, so it has a default here.
BEEHIIV_API_KEY = os.environ.get('BEEHIIV_API_KEY', '').strip()
BEEHIIV_PUBLICATION_ID = os.environ.get(
    'BEEHIIV_PUBLICATION_ID',
    'pub_9d452f61-864e-4250-bbcb-d3d66bca1f7d'
).strip()

# These names must match the custom fields in beehiiv exactly, character for
# character. If they don't match, beehiiv accepts the signup but silently
# throws the value away.
BEEHIIV_FIELD_FIRST_NAME = 'First Name'
BEEHIIV_FIELD_OPTIN_TIME = 'Opt-in date/time'

# Notification email configuration - pulled from Render environment variables.
# The .replace(' ', '') on the app password removes any spaces, so the
# password works whether it was pasted with or without Google's display spaces.
NOTIFY_GMAIL_ADDRESS = os.environ.get('NOTIFY_GMAIL_ADDRESS', '').strip()
NOTIFY_GMAIL_APP_PASSWORD = os.environ.get('NOTIFY_GMAIL_APP_PASSWORD', '').replace(' ', '').strip()
NOTIFY_TO_ADDRESS = os.environ.get('NOTIFY_TO_ADDRESS', '').strip()


def calgary_now():
    """Current time, in Calgary if possible, otherwise UTC."""
    if CALGARY_TZ:
        return datetime.now(CALGARY_TZ)
    return datetime.utcnow()


def readable_timestamp(moment):
    """e.g. 'July 25, 2026 at 07:56 PM Calgary time'"""
    if CALGARY_TZ:
        return moment.strftime('%B %d, %Y at %I:%M %p Calgary time')
    return moment.strftime('%B %d, %Y at %H:%M UTC')


def add_to_mailchimp(email, first_name, newsletter_optin):
    """
    Add or update this person in the Mailchimp audience.

    Returns a short plain-English status string for the notification email.
    Never raises.
    """
    if not MAILCHIMP_API_KEY:
        return 'skipped (no Mailchimp key configured)'

    # Mailchimp uses an MD5 hash of the lowercased email as the subscriber's ID
    subscriber_hash = hashlib.md5(email.encode('utf-8')).hexdigest()

    # The newsletter box controls whether this person actually joins the
    # newsletter. If they ticked it, they're subscribed. If they only
    # wanted the guide, their email is still captured in the audience but
    # marked 'unsubscribed', so they never receive the weekly newsletter.
    member_status = 'subscribed' if newsletter_optin else 'unsubscribed'

    url = (
        f'https://{MAILCHIMP_DC}.api.mailchimp.com/3.0/lists/'
        f'{MAILCHIMP_AUDIENCE_ID}/members/{subscriber_hash}'
    )
    payload = {
        'email_address': email,
        'status_if_new': member_status,
    }
    # Only ever push a status UPWARD. If they ticked the box, subscribe them.
    # If they only wanted the guide, we deliberately do NOT send a status at
    # all, so an existing subscriber who comes back for the guide and doesn't
    # tick the box keeps their subscription instead of being silently removed.
    if newsletter_optin:
        payload['status'] = 'subscribed'
    if first_name:
        payload['merge_fields'] = {'FNAME': first_name}

    try:
        response = requests.put(
            url,
            json=payload,
            auth=('anystring', MAILCHIMP_API_KEY),
            timeout=10
        )

        if response.status_code in (200, 201):
            if newsletter_optin:
                return 'added'

            # Guide only. Find out what Mailchimp says this person's status is
            # now, so we don't tag an existing subscriber as a non-subscriber.
            try:
                current_status = (response.json() or {}).get('status', '')
            except Exception:
                current_status = ''

            if current_status == 'subscribed':
                return 'already a subscriber, left subscribed'

            apply_nonsub_tag(subscriber_hash)
            return 'added (guide only, not subscribed)'

        print(f'Mailchimp error: {response.status_code} - {response.text}')
        return f'FAILED (Mailchimp said {response.status_code})'

    except Exception as e:
        print(f'Request to Mailchimp failed: {e}')
        return 'FAILED (could not reach Mailchimp)'


def apply_nonsub_tag(subscriber_hash):
    """
    Tag a guide-only downloader with 'NONSUB' so Karen can see and filter
    them in Mailchimp. Wrapped in a try/except so it can never affect the
    signup. The tag is a nice-to-have for Karen's visibility.
    """
    try:
        tag_url = (
            f'https://{MAILCHIMP_DC}.api.mailchimp.com/3.0/lists/'
            f'{MAILCHIMP_AUDIENCE_ID}/members/{subscriber_hash}/tags'
        )
        requests.post(
            tag_url,
            json={'tags': [{'name': 'NONSUB', 'status': 'active'}]},
            auth=('anystring', MAILCHIMP_API_KEY),
            timeout=10
        )
    except Exception as e:
        print(f'NONSUB tag failed: {e}')


def add_to_beehiiv(email, first_name, optin_moment):
    """
    Add this person to the beehiiv publication.

    Only called for people who ticked the newsletter box. Guide-only
    downloaders are deliberately kept out of beehiiv for now.

    Returns a short plain-English status string for the notification email.
    Never raises.
    """
    if not BEEHIIV_API_KEY:
        return 'skipped (no beehiiv key configured)'
    if not BEEHIIV_PUBLICATION_ID:
        return 'skipped (no beehiiv publication ID configured)'

    url = f'https://api.beehiiv.com/v2/publications/{BEEHIIV_PUBLICATION_ID}/subscriptions'

    custom_fields = []
    if first_name:
        custom_fields.append({'name': BEEHIIV_FIELD_FIRST_NAME, 'value': first_name})
    # The consent record: when this person ticked the box.
    custom_fields.append({
        'name': BEEHIIV_FIELD_OPTIN_TIME,
        'value': optin_moment.isoformat()
    })

    payload = {
        'email': email,
        # Never resurrect someone who deliberately unsubscribed.
        'reactivate_existing': False,
        # Karen's decision: no welcome email.
        'send_welcome_email': False,
        # Karen's decision: no double opt-in. Set explicitly rather than
        # letting beehiiv's publication default decide.
        'double_opt_override': 'off',
        # So Karen can tell website signups apart from recommendation
        # network and referral signups in beehiiv.
        'utm_source': 'leadtime.news',
        'utm_medium': 'walkthrough-signup',
        'referring_site': 'https://leadtime.news/',
        'custom_fields': custom_fields,
    }

    try:
        response = requests.post(
            url,
            json=payload,
            headers={
                'Authorization': f'Bearer {BEEHIIV_API_KEY}',
                'Content-Type': 'application/json',
            },
            timeout=10
        )

        if response.status_code in (200, 201):
            return 'added'

        print(f'beehiiv error: {response.status_code} - {response.text}')
        return f'FAILED (beehiiv said {response.status_code})'

    except Exception as e:
        print(f'Request to beehiiv failed: {e}')
        return 'FAILED (could not reach beehiiv)'


def send_signup_notification(email, first_name, newsletter_optin,
                             moment, mailchimp_status, beehiiv_status):
    """
    Email Karen about a signup, including how each service responded.

    Wrapped in a try/except so no matter what goes wrong here (Gmail down,
    password revoked, network hiccup), nothing else is affected.
    """
    if not (NOTIFY_GMAIL_ADDRESS and NOTIFY_GMAIL_APP_PASSWORD and NOTIFY_TO_ADDRESS):
        return

    try:
        timestamp = readable_timestamp(moment)

        message = EmailMessage()
        if newsletter_optin:
            message['Subject'] = f'New Lead Time subscriber: {email}'
            intro = 'A new subscriber just joined Lead Time (ticked the newsletter box).'
        else:
            message['Subject'] = f'Guide download (no newsletter): {email}'
            intro = ('Someone downloaded the guide but did not tick the newsletter box, '
                     'so they were captured but not subscribed.')
        message['From'] = f'Lead Time Signups <{NOTIFY_GMAIL_ADDRESS}>'
        message['To'] = NOTIFY_TO_ADDRESS

        name_line = f'Name: {first_name}\n' if first_name else ''
        message.set_content(
            f'{intro}\n'
            '\n'
            f'Email: {email}\n'
            f'{name_line}'
            f'When: {timestamp}\n'
            '\n'
            f'Mailchimp: {mailchimp_status}\n'
            f'beehiiv: {beehiiv_status}\n'
            '\n'
            'If either line above says FAILED, that person still received the '
            'guide, but you may want to add them to that list by hand.\n'
            '\n'
            'Sent automatically by leadtime.news.'
        )

        with smtplib.SMTP_SSL('smtp.gmail.com', 465, timeout=15) as server:
            server.login(NOTIFY_GMAIL_ADDRESS, NOTIFY_GMAIL_APP_PASSWORD)
            server.send_message(message)

    except Exception as e:
        # Log it for the Render logs, then move on. Never raise.
        print(f'Signup notification email failed: {e}')


def process_signup(email, first_name, newsletter_optin):
    """
    Everything that happens after the visitor has been sent on their way
    to the guide. Runs in a background thread so nothing here can delay or
    block the download.
    """
    moment = calgary_now()

    mailchimp_status = add_to_mailchimp(email, first_name, newsletter_optin)

    if newsletter_optin:
        beehiiv_status = add_to_beehiiv(email, first_name, moment)
    else:
        beehiiv_status = 'not sent (guide only, no newsletter box ticked)'

    send_signup_notification(
        email, first_name, newsletter_optin,
        moment, mailchimp_status, beehiiv_status
    )


# Serve the landing page at the root URL
@app.route('/')
def home():
    return send_from_directory('.', 'lead-time-walkthrough.html')


# Serve the landing page at its named path too
@app.route('/lead-time-walkthrough.html')
def landing_page():
    return send_from_directory('.', 'lead-time-walkthrough.html')


# Serve the thank-you page
@app.route('/lead-time-walkthrough-ready')
@app.route('/lead-time-walkthrough-ready.html')
def thank_you_page():
    return send_from_directory('.', 'lead-time-walkthrough-ready.html')


# Serve the About page
@app.route('/about')
@app.route('/about.html')
def about_page():
    return send_from_directory('.', 'about.html')


# Serve the Privacy & Terms page
@app.route('/privacy-and-terms')
@app.route('/privacy-and-terms.html')
def privacy_and_terms_page():
    return send_from_directory('.', 'privacy-and-terms.html')


# Serve the Articles index page
@app.route('/articles')
@app.route('/articles.html')
def articles_index_page():
    return send_from_directory('.', 'articles-index.html')


# Serve individual articles. Each new article gets its own route below,
# pointing the clean URL at its HTML file. To add a future article, copy
# the block for "when-the-house-stops-fitting", change the URL path and
# the filename to match the new article, and upload the new HTML file.
@app.route('/articles/when-the-house-stops-fitting')
def article_when_the_house_stops_fitting():
    return send_from_directory('.', 'when-the-house-stops-fitting.html')


# Serve the sitemap so search engines can discover every page.
# Built inline as an explicit XML response (rather than served as a static
# file) because some automated crawlers reject the static-file response even
# when a browser accepts it. This is the most compatible approach.
SITEMAP_XML = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url>
    <loc>https://leadtime.news/</loc>
    <changefreq>monthly</changefreq>
    <priority>1.0</priority>
  </url>
  <url>
    <loc>https://leadtime.news/articles</loc>
    <changefreq>weekly</changefreq>
    <priority>0.8</priority>
  </url>
  <url>
    <loc>https://leadtime.news/articles/when-the-house-stops-fitting</loc>
    <lastmod>2026-06-09</lastmod>
    <changefreq>monthly</changefreq>
    <priority>0.7</priority>
  </url>
  <url>
    <loc>https://leadtime.news/about</loc>
    <changefreq>yearly</changefreq>
    <priority>0.5</priority>
  </url>
  <url>
    <loc>https://leadtime.news/privacy-and-terms</loc>
    <changefreq>yearly</changefreq>
    <priority>0.3</priority>
  </url>
</urlset>
"""


@app.route('/sitemap.xml')
def sitemap():
    return Response(SITEMAP_XML, mimetype='application/xml')


# Serve robots.txt inline, pointing crawlers to the sitemap
ROBOTS_TXT = """User-agent: *
Allow: /

Sitemap: https://leadtime.news/sitemap.xml
"""


@app.route('/robots.txt')
def robots():
    return Response(ROBOTS_TXT, mimetype='text/plain')


# Handle the signup form submission
@app.route('/subscribe', methods=['POST'])
def subscribe():
    # Get the form data
    data = request.get_json() if request.is_json else request.form
    email = (data.get('email') or '').strip().lower()
    first_name = (data.get('first_name') or '').strip()
    honeypot = (data.get('honeypot') or '').strip()

    # Did the person tick the newsletter box? An unticked checkbox sends
    # nothing at all, so its mere presence (any value) means they opted in.
    newsletter_optin = bool((data.get('newsletter_optin') or '').strip())

    # Honeypot check: if a bot filled this hidden field, silently fake success.
    # Real humans never see or fill this field. No notification is sent for bots.
    if honeypot:
        return jsonify({'status': 'success'}), 200

    # Basic email validation. This is the only thing that can stop a signup.
    if not email or '@' not in email or '.' not in email:
        return jsonify({'status': 'error', 'message': 'Please enter a valid email address.'}), 400

    # Everything else happens in the background: Mailchimp, beehiiv, and the
    # notification email. The visitor is sent straight to the guide and never
    # waits on, or is affected by, any of those three.
    threading.Thread(
        target=process_signup,
        args=(email, first_name, newsletter_optin),
        daemon=True
    ).start()

    return jsonify({'status': 'success'}), 200


if __name__ == '__main__':
    # Render sets PORT automatically; default to 5000 for local testing
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
