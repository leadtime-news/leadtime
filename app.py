"""
Lead Time signup handler.
Receives form submissions from leadtime.news, adds newsletter sign-ups to
the beehiiv Lead Time publication, serves the static site files, and emails
Karen a notification for each signup.

Design (August 2026 - Mailchimp removed):
  - Newsletter sign-ups go to beehiiv.
  - Guide-only downloaders (didn't tick the box) are not added to any list.
    Karen is still emailed about every one of them.
  - Nothing can block the guide download. The visitor always gets the guide.

Added August 2026 - the unsubscribe listener:
  beehiiv has no way to email Karen when someone unsubscribes. Instead it
  can send the details to a web address, which is what the route at the
  bottom of this file is. It receives the details, waits a quarter of an
  hour so the reader has time to answer the "Before you go" question, asks
  beehiiv what they said, and then emails Karen one summary: who left, how
  long they stayed, where they came from, and why. Nothing about the
  newsletter, the list or the signup form depends on it; if it breaks, only
  the email is missed.

Added September 2026 - signups from the Kintrak sales page:
  kintrak.ca carries its own Lead Time signup box. It sends its signups
  here, to the same /subscribe address the leadtime.news form uses, so they
  are handled identically: same beehiiv setup, same consent record, same
  notification email. The only difference is that they are stamped as
  coming from kintrak.ca, in beehiiv and in the notification. Only the
  kintrak.ca website is allowed to send signups here from another site.
"""
import os
import hmac
import smtplib
import threading
import time
from datetime import datetime
from email.message import EmailMessage
from urllib.parse import quote

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

# The field the "Before you go" survey writes the chosen reason into. It must
# match the name in beehiiv exactly, character for character.
BEEHIIV_FIELD_UNSUB_REASON = 'Unsubscribe reason'

# How long to wait before emailing Karen about an unsubscribe. The survey is
# shown to the reader AFTER they unsubscribe, so waiting a little while means
# the notification can include their answer instead of arriving before it
# exists. 900 seconds is 15 minutes. Set to 0 to send immediately.
UNSUBSCRIBE_NOTIFICATION_DELAY_SECONDS = int(
    os.environ.get('UNSUBSCRIBE_NOTIFICATION_DELAY_SECONDS', '900')
)

# The unsubscribe listener's secret. It sits inside the web address that
# beehiiv sends unsubscribe details to, so that only beehiiv's messages are
# acted on. It lives in Render, never in this file, exactly like the API
# keys above. If it is not set, the listener politely does nothing.
BEEHIIV_WEBHOOK_SECRET = os.environ.get('BEEHIIV_WEBHOOK_SECRET', '').strip()

# Notification email configuration - pulled from Render environment variables.
# The .replace(' ', '') on the app password removes any spaces, so the
# password works whether it was pasted with or without Google's display spaces.
NOTIFY_GMAIL_ADDRESS = os.environ.get('NOTIFY_GMAIL_ADDRESS', '').strip()
NOTIFY_GMAIL_APP_PASSWORD = os.environ.get('NOTIFY_GMAIL_APP_PASSWORD', '').replace(' ', '').strip()
NOTIFY_TO_ADDRESS = os.environ.get('NOTIFY_TO_ADDRESS', '').strip()

# Where a signup came from. The leadtime.news form sends nothing, so it gets
# the first entry. The kintrak.ca signup box sends signup_source "kintrak".
# Any other value is ignored and treated as leadtime.news.
SIGNUP_SOURCES = {
    'leadtime': {
        'utm_source': 'leadtime.news',
        'utm_medium': 'walkthrough-signup',
        'referring_site': 'https://leadtime.news/',
        'label': 'leadtime.news',
    },
    'kintrak': {
        'utm_source': 'kintrak.ca',
        'utm_medium': 'kintrak-sales-page',
        'referring_site': 'https://kintrak.ca/',
        'label': 'the Kintrak sales page (kintrak.ca)',
    },
    # Added September 2026: the pop-up guide offer on kintrak.ca (the box
    # that opens as a computer visitor heads for the top of the window, or
    # from the slim bar on phones and tablets). Kept separate from the box
    # further down the page so Karen can see whether the pop-up earns its
    # place.
    'kintrak-popup': {
        'utm_source': 'kintrak.ca',
        'utm_medium': 'kintrak-guide-popup',
        'referring_site': 'https://kintrak.ca/',
        'label': 'the pop-up guide offer on the Kintrak sales page (kintrak.ca)',
    },
}

# The other websites allowed to send signups to /subscribe. Browsers refuse
# to let one site send a form to another unless the receiving site names it.
SIGNUP_ALLOWED_ORIGINS = {
    'https://kintrak.ca',
    'https://www.kintrak.ca',
}

# Ad labels. Added September 2026 for the Pinterest ads.
# When a visitor arrives from an ad, the ad's link carries labels such as
# utm_source=pinterest and utm_campaign=guide-us-sept2026. Both signup boxes
# (leadtime.news and kintrak.ca) now pass those labels along with the form,
# as ad_source, ad_medium, ad_campaign and ad_content. When they are present,
# beehiiv records the ad instead of the plain website stamp, so Karen can see
# which subscribers came from which campaign and which pin. The page they
# signed up on is still recorded, as the referring site.
AD_LABEL_MAX_LENGTH = 60


def clean_ad_label(value):
    """
    Keep an ad label short and plain: lower-case letters, numbers, dots,
    hyphens and underscores only. Anything else is dropped, so a strange
    web address can never put odd text into beehiiv or the email.
    """
    value = (value or '').strip().lower()
    kept = ''.join(ch for ch in value if ch.isascii() and (ch.isalnum() or ch in '._-'))
    return kept[:AD_LABEL_MAX_LENGTH]


def read_ad_labels(data):
    """
    The ad labels sent with a signup, or None if the visitor did not arrive
    from a labelled link.
    """
    ad = {
        'source': clean_ad_label(data.get('ad_source')),
        'medium': clean_ad_label(data.get('ad_medium')),
        'campaign': clean_ad_label(data.get('ad_campaign')),
        'content': clean_ad_label(data.get('ad_content')),
    }
    if not ad['source']:
        return None
    return ad


def describe_ad(ad):
    """e.g. 'Pinterest ad, campaign guide-us-sept2026, pin step-in'"""
    if not ad:
        return ''
    name = 'Pinterest' if ad['source'] == 'pinterest' else ad['source']
    kind = ' ad' if ad['medium'] in ('cpc', 'paid', 'ppc', 'paid_social') else ''
    parts = [f'{name}{kind}']
    if ad['campaign']:
        parts.append(f"campaign {ad['campaign']}")
    if ad['content']:
        parts.append(f"pin {ad['content']}")
    return ', '.join(parts)


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


def add_to_beehiiv(email, first_name, optin_moment, source='leadtime', ad=None):
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
    stamp = SIGNUP_SOURCES.get(source, SIGNUP_SOURCES['leadtime'])

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
        # Someone who fills in this form and ticks the box is giving fresh,
        # timestamped consent, so a person who unsubscribed in the past is
        # welcomed back rather than blocked. (Changed August 2026; it was
        # previously False, which silently left returners unsubscribed.)
        'reactivate_existing': True,
        # Karen's decision, August 2026: beehiiv sends its welcome email to
        # every new subscriber. (This was False until the welcome email
        # existed; leaving it False would have stopped it firing.)
        'send_welcome_email': True,
        # Karen's decision: no double opt-in. Set explicitly rather than
        # letting beehiiv's publication default decide.
        'double_opt_override': 'off',
        # So Karen can tell website signups apart from recommendation
        # network and referral signups in beehiiv, and leadtime.news
        # signups apart from kintrak.ca signups.
        'utm_source': stamp['utm_source'],
        'utm_medium': stamp['utm_medium'],
        'referring_site': stamp['referring_site'],
        'custom_fields': custom_fields,
    }

    # Came from an ad (September 2026): record the ad instead of the plain
    # website stamp. beehiiv only lets Karen filter subscribers by source,
    # medium and campaign, so the pin's label rides along inside the
    # campaign, e.g. "guide-us-sept2026/step-in". The page they signed up
    # on stays in referring_site.
    if ad:
        payload['utm_source'] = ad['source']
        payload['utm_medium'] = ad['medium'] or 'paid'
        campaign = ad['campaign'] or 'no-campaign'
        if ad['content']:
            campaign = f"{campaign}/{ad['content']}"
        payload['utm_campaign'] = campaign

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
                             moment, beehiiv_status, source='leadtime', ad=None):
    """
    Email Karen about a signup, including how beehiiv responded.

    Wrapped in a try/except so no matter what goes wrong here (Gmail down,
    password revoked, network hiccup), nothing else is affected.
    """
    if not (NOTIFY_GMAIL_ADDRESS and NOTIFY_GMAIL_APP_PASSWORD and NOTIFY_TO_ADDRESS):
        return

    try:
        timestamp = readable_timestamp(moment)
        stamp = SIGNUP_SOURCES.get(source, SIGNUP_SOURCES['leadtime'])
        if source == 'kintrak':
            via = ' (via kintrak.ca)'
        elif source == 'kintrak-popup':
            via = ' (via kintrak.ca pop-up)'
        else:
            via = ''
        if ad:
            ad_name = 'Pinterest' if ad['source'] == 'pinterest' else ad['source']
            via = f'{via} (from {ad_name})'
        ad_line = f'Arrived from: {describe_ad(ad)}\n' if ad else ''

        message = EmailMessage()
        if newsletter_optin:
            message['Subject'] = f'New Lead Time subscriber{via}: {email}'
            intro = 'A new subscriber just joined Lead Time (ticked the newsletter box).'
        else:
            message['Subject'] = f'Guide download (no newsletter){via}: {email}'
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
            f'Signed up from: {stamp["label"]}\n'
            f'{ad_line}'
            '\n'
            f'beehiiv: {beehiiv_status}\n'
            '\n'
            'If the line above says FAILED, that person still received the '
            'guide, but you may want to add them to beehiiv by hand.\n'
            '\n'
            'Sent automatically by leadtime.news.'
        )

        with smtplib.SMTP_SSL('smtp.gmail.com', 465, timeout=15) as server:
            server.login(NOTIFY_GMAIL_ADDRESS, NOTIFY_GMAIL_APP_PASSWORD)
            server.send_message(message)

    except Exception as e:
        # Log it for the Render logs, then move on. Never raise.
        print(f'Signup notification email failed: {e}')


def process_signup(email, first_name, newsletter_optin, source='leadtime', ad=None):
    """
    Everything that happens after the visitor has been sent on their way
    to the guide. Runs in a background thread so nothing here can delay or
    block the download.
    """
    moment = calgary_now()

    if newsletter_optin:
        beehiiv_status = add_to_beehiiv(email, first_name, moment, source, ad)
    else:
        beehiiv_status = 'not sent (guide only, no newsletter box ticked)'

    send_signup_notification(
        email, first_name, newsletter_optin,
        moment, beehiiv_status, source, ad
    )


# Serve the home page. The root URL is the lead-magnet landing page, so the
# home page lives at /home. The logo and the Home link on every interior page
# point here.
@app.route('/home')
@app.route('/home.html')
def home_page():
    return send_from_directory('.', 'home.html')


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


@app.route('/articles/why-downsizing-gets-it-wrong')
def article_why_downsizing_gets_it_wrong():
    return send_from_directory('.', 'why-downsizing-gets-it-wrong.html')


@app.route('/articles/starting-the-conversation-about-where-a-parent-will-live')
def article_starting_the_conversation_about_where_a_parent_will_live():
    return send_from_directory('.', 'starting-the-conversation-about-where-a-parent-will-live.html')


@app.route('/articles/what-a-family-needs-to-know-if-someone-steps-in')
def article_what_a_family_needs_to_know_if_someone_steps_in():
    return send_from_directory('.', 'what-a-family-needs-to-know-if-someone-steps-in.html')


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
    <loc>https://leadtime.news/home</loc>
    <changefreq>weekly</changefreq>
    <priority>0.9</priority>
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
    <loc>https://leadtime.news/articles/why-downsizing-gets-it-wrong</loc>
    <lastmod>2026-07-27</lastmod>
    <changefreq>monthly</changefreq>
    <priority>0.7</priority>
  </url>
  <url>
    <loc>https://leadtime.news/articles/starting-the-conversation-about-where-a-parent-will-live</loc>
    <lastmod>2026-08-07</lastmod>
    <changefreq>monthly</changefreq>
    <priority>0.7</priority>
  </url>
  <url>
    <loc>https://leadtime.news/articles/what-a-family-needs-to-know-if-someone-steps-in</loc>
    <lastmod>2026-09-08</lastmod>
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


# ---------------------------------------------------------------------------
# THE UNSUBSCRIBE LISTENER
#
# beehiiv sends the details of each unsubscribe to the web address at the
# bottom of this section. Everything here is wrapped so that nothing can
# affect the website or the signup form.
# ---------------------------------------------------------------------------

def beehiiv_custom_field(data, wanted_name):
    """
    Pull one custom field value out of a beehiiv payload.

    beehiiv sends custom fields as a list of small objects rather than as
    plain values, so this digs the one we want back out. Returns an empty
    string if it isn't there.
    """
    try:
        for field in (data.get('custom_fields') or []):
            if (field.get('name') or '').strip().lower() == wanted_name.strip().lower():
                raw = field.get('value')
                # A list-type field (like the survey answer) can come back as
                # an array rather than a plain value.
                if isinstance(raw, list):
                    return ', '.join(str(v).strip() for v in raw if str(v).strip())
                return str(raw or '').strip()
    except Exception:
        pass
    return ''


def fetch_unsubscribe_reason(email, subscription_id):
    """
    Ask beehiiv whether this person answered the "Before you go" question.

    Called after the wait, so the answer has had time to arrive. Returns an
    empty string if they didn't answer or if anything goes wrong. Never raises.
    """
    if not (BEEHIIV_API_KEY and BEEHIIV_PUBLICATION_ID):
        return ''

    base = f'https://api.beehiiv.com/v2/publications/{BEEHIIV_PUBLICATION_ID}/subscriptions'
    lookups = []
    if email:
        lookups.append(f'{base}/by_email/{quote(email, safe="")}')
    if subscription_id:
        lookups.append(f'{base}/{subscription_id}')

    for url in lookups:
        try:
            response = requests.get(
                url,
                headers={'Authorization': f'Bearer {BEEHIIV_API_KEY}'},
                params={'expand[]': 'custom_fields'},
                timeout=10
            )
            if response.status_code == 200:
                subscription = (response.json() or {}).get('data') or {}
                reason = beehiiv_custom_field(subscription, BEEHIIV_FIELD_UNSUB_REASON)
                if reason:
                    return reason
            else:
                print(f'Reason lookup returned {response.status_code} for {url}')
        except Exception as e:
            print(f'Reason lookup failed: {e}')

    return ''


def describe_tenure(subscribed_moment, left_moment):
    """
    Turn two dates into a plain phrase like '6 days' or 'about 8 months',
    so the notification email says how long this person stayed.
    """
    try:
        days = (left_moment - subscribed_moment).days
    except Exception:
        return ''

    if days < 0:
        return ''
    if days == 0:
        return 'less than a day'
    if days == 1:
        return '1 day'
    if days < 60:
        return f'{days} days'
    if days < 365:
        return f'about {days // 30} months'
    if days < 730:
        return 'about a year'
    return f'about {days // 365} years'


def describe_source(data):
    """
    Say in plain English where this subscriber originally came from.

    Signups through Karen's own page are stamped by this file when they are
    created, so they are recognised by name. Anything else is reported with
    whatever beehiiv recorded.
    """
    source = (data.get('utm_source') or '').strip()
    medium = (data.get('utm_medium') or '').strip()
    campaign = (data.get('utm_campaign') or '').strip()
    channel = (data.get('utm_channel') or '').strip()
    site = (data.get('referring_site') or '').strip()

    if source == 'leadtime.news':
        return 'the signup page at leadtime.news'
    if source == 'kintrak.ca' and medium == 'kintrak-guide-popup':
        return 'the pop-up guide offer on the Kintrak sales page (kintrak.ca)'
    if source == 'kintrak.ca':
        return 'the Lead Time signup box on the Kintrak sales page (kintrak.ca)'
    if source == 'pinterest':
        where = f' (campaign/pin: {campaign})' if campaign else ''
        return f'a Pinterest ad{where}'

    parts = []
    if source:
        parts.append(source)
    if campaign:
        parts.append(f'campaign "{campaign}"')
    elif medium:
        parts.append(medium)

    if not parts and site:
        parts.append(site)
    if not parts and channel:
        parts.append(channel)

    return ', '.join(parts) if parts else 'not recorded by beehiiv'


def send_unsubscribe_notification(data, left_moment, reason=''):
    """
    Email Karen about one unsubscribe, including the reason they gave if they
    answered the "Before you go" question.

    Wrapped in a try/except so nothing that goes wrong here can affect
    anything else. Never raises.
    """
    if not (NOTIFY_GMAIL_ADDRESS and NOTIFY_GMAIL_APP_PASSWORD and NOTIFY_TO_ADDRESS):
        return

    try:
        email = (data.get('email') or 'unknown address').strip()
        first_name = beehiiv_custom_field(data, BEEHIIV_FIELD_FIRST_NAME)

        # When they originally subscribed. beehiiv sends this as a plain
        # number of seconds, so it has to be turned back into a date.
        subscribed_line = 'Subscribed: not recorded by beehiiv'
        try:
            created_raw = data.get('created')
            if created_raw:
                if CALGARY_TZ:
                    subscribed_moment = datetime.fromtimestamp(int(created_raw), CALGARY_TZ)
                else:
                    subscribed_moment = datetime.utcfromtimestamp(int(created_raw))
                tenure = describe_tenure(subscribed_moment, left_moment)
                subscribed_on = subscribed_moment.strftime('%B %d, %Y')
                if tenure:
                    subscribed_line = f'Subscribed: {subscribed_on} - stayed {tenure}'
                else:
                    subscribed_line = f'Subscribed: {subscribed_on}'
        except Exception as e:
            print(f'Could not read the subscribe date: {e}')

        message = EmailMessage()
        message['Subject'] = f'Unsubscribed from Lead Time: {email}'
        message['From'] = f'Lead Time Signups <{NOTIFY_GMAIL_ADDRESS}>'
        message['To'] = NOTIFY_TO_ADDRESS

        name_line = f'Name: {first_name}\n' if first_name else ''
        tags = data.get('tags') or []
        tags_line = f'Tags: {", ".join(str(t) for t in tags)}\n' if tags else ''

        if reason:
            reason_line = f'Reason given: {reason}\n'
        else:
            reason_line = (
                'Reason given: none - the question is optional and most '
                'people skip it\n'
            )

        message.set_content(
            'Someone unsubscribed from Lead Time.\n'
            '\n'
            f'Email: {email}\n'
            f'{name_line}'
            f'{subscribed_line}\n'
            f'Came from: {describe_source(data)}\n'
            f'{tags_line}'
            f'{reason_line}'
            f'Left: {readable_timestamp(left_moment)}\n'
            '\n'
            'beehiiv has already removed them from the list, so there is '
            'nothing you need to do.\n'
            '\n'
            'Sent automatically by leadtime.news.'
        )

        with smtplib.SMTP_SSL('smtp.gmail.com', 465, timeout=15) as server:
            server.login(NOTIFY_GMAIL_ADDRESS, NOTIFY_GMAIL_APP_PASSWORD)
            server.send_message(message)

    except Exception as e:
        print(f'Unsubscribe notification email failed: {e}')


def process_beehiiv_event(payload):
    """
    Everything that happens after beehiiv has been told its message was
    received. Runs in a background thread so beehiiv is never kept waiting.
    """
    try:
        event_type = (payload.get('event_type') or '').strip().lower()
        data = payload.get('data') or {}

        # Only unsubscribes are of interest. Anything else is noted in the
        # Render logs and otherwise ignored, so switching on a further event
        # type in beehiiv by mistake cannot produce confusing emails.
        if event_type not in ('subscription.deleted',):
            print(f'beehiiv sent an event we do not act on: {event_type}')
            return

        # The moment they left is recorded now, before any waiting, so the
        # email reports when they actually unsubscribed.
        left_moment = calgary_now()

        # Wait so the "Before you go" answer has time to arrive, then ask
        # beehiiv for it.
        if UNSUBSCRIBE_NOTIFICATION_DELAY_SECONDS > 0:
            time.sleep(UNSUBSCRIBE_NOTIFICATION_DELAY_SECONDS)

        reason = fetch_unsubscribe_reason(
            (data.get('email') or '').strip(),
            (data.get('id') or '').strip()
        )

        send_unsubscribe_notification(data, left_moment, reason)

    except Exception as e:
        print(f'Handling the beehiiv event failed: {e}')


@app.route('/hooks/beehiiv/<path_secret>', methods=['POST', 'GET'])
def beehiiv_webhook(path_secret):
    """
    The address beehiiv sends unsubscribe details to.

    Anything with the wrong secret in the address is treated as though the
    page does not exist. Correct messages are acknowledged immediately and
    handled in the background, because beehiiv only wants to know that its
    message arrived.
    """
    if not BEEHIIV_WEBHOOK_SECRET:
        return jsonify({'status': 'not configured'}), 404

    if not hmac.compare_digest(path_secret, BEEHIIV_WEBHOOK_SECRET):
        return jsonify({'status': 'not found'}), 404

    # Opening the address in a browser is a harmless way to check that it
    # is live before pasting it into beehiiv.
    if request.method == 'GET':
        return Response(
            'The Lead Time unsubscribe listener is running.',
            mimetype='text/plain'
        )

    payload = request.get_json(silent=True) or {}

    threading.Thread(
        target=process_beehiiv_event,
        args=(payload,),
        daemon=True
    ).start()

    # beehiiv wants a 200 back to count the delivery as successful.
    return jsonify({'status': 'received'}), 200


def allow_signup_origin(response):
    """
    Let the kintrak.ca signup box read the answer from /subscribe.
    Only the sites named in SIGNUP_ALLOWED_ORIGINS are allowed.
    """
    origin = request.headers.get('Origin', '')
    if origin in SIGNUP_ALLOWED_ORIGINS:
        response.headers['Access-Control-Allow-Origin'] = origin
        response.headers['Vary'] = 'Origin'
        response.headers['Access-Control-Allow-Methods'] = 'POST, OPTIONS'
        response.headers['Access-Control-Allow-Headers'] = 'Content-Type'
    return response


# Handle the signup form submission
@app.route('/subscribe', methods=['POST', 'OPTIONS'])
def subscribe():
    # A browser checks first whether kintrak.ca may send here.
    if request.method == 'OPTIONS':
        return allow_signup_origin(Response(status=204))
    response, code = handle_subscribe()
    return allow_signup_origin(response), code


def handle_subscribe():
    # Get the form data
    data = request.get_json() if request.is_json else request.form
    email = (data.get('email') or '').strip().lower()
    first_name = (data.get('first_name') or '').strip()
    honeypot = (data.get('honeypot') or '').strip()

    # Did the person tick the newsletter box? An unticked checkbox sends
    # nothing at all, so its mere presence (any value) means they opted in.
    newsletter_optin = bool((data.get('newsletter_optin') or '').strip())

    # Which signup box this came from. Only known values count.
    source = (data.get('signup_source') or '').strip().lower()
    if source not in SIGNUP_SOURCES:
        source = 'leadtime'

    # Ad labels, if the visitor arrived from a labelled ad link.
    ad = read_ad_labels(data)

    # Honeypot check: if a bot filled this hidden field, silently fake success.
    # Real humans never see or fill this field. No notification is sent for bots.
    if honeypot:
        return jsonify({'status': 'success'}), 200

    # Basic email validation. This is the only thing that can stop a signup.
    if not email or '@' not in email or '.' not in email:
        return jsonify({'status': 'error', 'message': 'Please enter a valid email address.'}), 400

    # Everything else happens in the background: beehiiv and the notification
    # email. The visitor is sent straight to the guide and never waits on, or
    # is affected by, either of those.
    threading.Thread(
        target=process_signup,
        args=(email, first_name, newsletter_optin, source, ad),
        daemon=True
    ).start()

    return jsonify({'status': 'success'}), 200


if __name__ == '__main__':
    # Render sets PORT automatically; default to 5000 for local testing
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
