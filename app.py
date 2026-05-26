"""
Lead Time signup handler.
Receives form submissions from leadtime.news, adds them to the Mailchimp
Lead Time audience, and serves the static site files.
"""
import os
import hashlib
import requests
from flask import Flask, request, jsonify, send_from_directory

app = Flask(__name__, static_folder='.', static_url_path='')

# Mailchimp configuration - all secrets pulled from Render environment variables
MAILCHIMP_API_KEY = os.environ.get('MAILCHIMP_API_KEY', '')
MAILCHIMP_AUDIENCE_ID = os.environ.get('MAILCHIMP_AUDIENCE_ID', 'de5f89484c')
# The data center is the suffix on the API key (e.g. 'us17' from a key ending in '-us17')
MAILCHIMP_DC = MAILCHIMP_API_KEY.split('-')[-1] if '-' in MAILCHIMP_API_KEY else 'us17'


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


# Handle the signup form submission
@app.route('/subscribe', methods=['POST'])
def subscribe():
    # Get the form data
    data = request.get_json() if request.is_json else request.form
    email = (data.get('email') or '').strip().lower()
    first_name = (data.get('first_name') or '').strip()
    honeypot = (data.get('honeypot') or '').strip()

    # Honeypot check: if a bot filled this hidden field, silently fake success.
    # Real humans never see or fill this field.
    if honeypot:
        return jsonify({'status': 'success'}), 200

    # Basic email validation
    if not email or '@' not in email or '.' not in email:
        return jsonify({'status': 'error', 'message': 'Please enter a valid email address.'}), 400

    # Confirm we have an API key configured
    if not MAILCHIMP_API_KEY:
        return jsonify({'status': 'error', 'message': 'Server is not configured. Please try again later.'}), 500

    # Mailchimp uses an MD5 hash of the lowercased email as the subscriber's ID
    subscriber_hash = hashlib.md5(email.encode('utf-8')).hexdigest()

    # Build the request to Mailchimp
    url = f'https://{MAILCHIMP_DC}.api.mailchimp.com/3.0/lists/{MAILCHIMP_AUDIENCE_ID}/members/{subscriber_hash}'
    payload = {
        'email_address': email,
        'status_if_new': 'subscribed',  # Single opt-in - matches current Lead Time audience setting
        'status': 'subscribed',
    }
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
            return jsonify({'status': 'success'}), 200
        else:
            # Log the error for debugging but don't expose Mailchimp internals to the user
            print(f'Mailchimp error: {response.status_code} - {response.text}')
            return jsonify({
                'status': 'error',
                'message': 'Something went wrong. Please try again in a moment.'
            }), 500

    except requests.RequestException as e:
        print(f'Request to Mailchimp failed: {e}')
        return jsonify({
            'status': 'error',
            'message': 'Could not reach the email service. Please try again in a moment.'
        }), 500


if __name__ == '__main__':
    # Render sets PORT automatically; default to 5000 for local testing
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
