"""
Example demonstrating the links tool for fraud detection.

This example shows how to use the new link extraction feature to analyze
suspicious emails without downloading the full HTML content.
"""

import json

from courier.models import Email, EmailAddress, EmailContent


def example_fraud_detection():
    """Example: Extract links from a phishing email for analysis."""

    # Simulated phishing email HTML
    phishing_html = """
    <html>
        <body>
            <h1>Urgent: Verify Your Account</h1>
            <p>Your account has been suspended. Click below to verify:</p>
            <a 
                href="https://fake-bank-login.suspicious-domain.com/verify?user=12345"
                class="btn"
            >
                Verify Now
            </a>
            <p>If you have questions, contact us:</p>
            <a href="mailto:support@legitimate-bank.com">Email Support</a>
            <p>
                <a href="https://legitimate-bank.com/privacy">Privacy Policy</a> | 
                <a href="https://legitimate-bank.com/terms">Terms of Service</a>
            </p>
            <!-- Hidden tracking pixel -->
            <a href="https://tracking.malicious-actor.com/pixel?id=abc123">
                <img src="pixel.gif" width="1" height="1">
            </a>
        </body>
    </html>
    """

    # Extract all links
    email_obj = Email(
        message_id="<phish@example.com>",
        subject="Verify",
        from_=EmailAddress(name="", address="noreply@fake.com"),
        to=[EmailAddress(name="", address="victim@example.com")],
        content=EmailContent(html=phishing_html),
    )
    links = email_obj.extract_links()

    print("Extracted Links for Analysis:")
    print("=" * 60)
    print(json.dumps(links, indent=2))

    # Fraud detection analysis
    print("\n\nFraud Detection Analysis:")
    print("=" * 60)

    suspicious_indicators = []

    for link in links:
        url = link["url"]
        anchor = link["anchor"]

        # Check for suspicious patterns
        if "verify" in url.lower() and "suspicious-domain" in url:
            suspicious_indicators.append(f"⚠️  Suspicious verification link: {url}")

        if anchor == "" and "tracking" in url:
            suspicious_indicators.append(
                f"🔍 Hidden tracking link (no anchor text): {url}"
            )

        if "mailto:" not in url and "legitimate-bank.com" in url:
            print(f"✅ Legitimate domain link: {url}")

    if suspicious_indicators:
        print("\n⚠️  POTENTIAL PHISHING INDICATORS DETECTED:")
        for indicator in suspicious_indicators:
            print(f"   {indicator}")

    return links


def example_multiline_links():
    """Example: Handle multi-line anchor tags (as per user requirement)."""

    html_with_multiline = """
    <div>
        <a 
            href="https://example.com/action"
            class="button primary"
            data-tracking="campaign-123"
            style="background-color: blue; color: white;"
        >
            Click here for
            special offer
            limited time only
        </a>
    </div>
    """

    email_obj = Email(
        message_id="<multi@example.com>",
        subject="Offer",
        from_=EmailAddress(name="", address="noreply@example.com"),
        to=[EmailAddress(name="", address="user@example.com")],
        content=EmailContent(html=html_with_multiline),
    )
    links = email_obj.extract_links()

    print("\nMulti-line Link Extraction:")
    print("=" * 60)
    print(json.dumps(links, indent=2))
    print("\nNote: Multi-line whitespace is normalized to single space")


if __name__ == "__main__":
    example_fraud_detection()
    print("\n" + "=" * 60 + "\n")
    example_multiline_links()
