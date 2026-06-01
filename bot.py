import hashlib
import logging
import os
import random
import time
import traceback

from dotenv import load_dotenv
from seleniumbase import sb_cdp
import google.genai as genai
from google.genai import types

import db

load_dotenv()

log = logging.getLogger(__name__)

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
PROXY = os.environ.get("PROXY", "")


def get_fingerprint(email):
    """Generate a deterministic fingerprint based on the email address."""
    h = int(hashlib.md5(email.encode()).hexdigest(), 16)
    
    # 1. User Agents (Modern versions)
    user_agents = [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ]
    ua = user_agents[h % len(user_agents)]
    
    # 2. Resolutions
    resolutions = ["1920,1080", "1536,864", "1366,768", "1440,900", "1600,900"]
    res = resolutions[h % len(resolutions)]
    
    # 3. Hardware / Memory
    concurrency = [4, 8, 12, 16][h % 4]
    memory = [4, 8, 16][h % 3]
    
    return {
        "ua": ua,
        "res": res,
        "concurrency": concurrency,
        "memory": memory,
        "locale": "en-US"
    }


SYSTEM_PROMPT = """You are an expert at writing authentic Google business reviews.
Your reviews sound completely natural and human written.
Never use asterisks, dashes, bullet points, numbered lists, or any special formatting characters.
Write in plain conversational prose only.
Keep reviews between 2 and 4 sentences.
Vary the writing style and vocabulary each time so reviews never repeat themselves."""


# Tone is derived entirely from the star rating — no separate sentiment field needed
_STAR_TONE = {
    1: "extremely negative and disappointed, describing a terrible experience",
    2: "negative and let down, describing a below-average experience",
    3: "neutral and balanced, noting both positives and areas for improvement",
    4: "positive and satisfied, describing a good overall experience",
    5: "enthusiastic and highly satisfied, describing an outstanding experience",
}


def generate_review(business_name, review_prompt, star_rating):
    client = genai.Client(api_key=GEMINI_API_KEY)

    tone = _STAR_TONE.get(star_rating, _STAR_TONE[5])

    prompt = f"""Write a Google review that is {tone}.

Business name: {business_name}
Business details and writing instructions: {review_prompt}
Star rating: {star_rating} out of 5

Write only the review text with no labels, headings, or extra commentary."""

    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt,
        config=types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
        ),
    )
    return response.text.strip()


def post_review(email, password, maps_url, star_rating, business_name, review_prompt, email_id, recovery_email=None):
    safe_name = email.replace("@", "_at_").replace(".", "_")
    profile_dir = os.path.abspath(os.path.join("chrome_profiles", safe_name))
    os.makedirs(profile_dir, exist_ok=True)

    fp = get_fingerprint(email)
    iframe_sel = "iframe[name='goog-reviews-write-widget']"

    sb = sb_cdp.Chrome(
        "https://accounts.google.com/signin/v2/identifier",
        user_data_dir=profile_dir,
        proxy=PROXY,
        agent=fp["ua"],
        window_size=fp["res"],
        locale_code=fp["locale"],
        chromium_arg=(
            "--disable-sync,"
            "--no-first-run,"
            "--no-default-browser-check,"
            "--disable-default-apps,"
            "--disable-fre,"
            "--disable-chrome-login-prompt"
        ),
        disable_features="SigninIntercept,ChromeWhatsNewUI,AccountConsistency",
    )
    try:
        # --- Login check ---
        sb.sleep(10)

        if "accounts.google.com/v3/signin" in sb.get_current_url():
            sb.sleep(2)
            sb.click('input[type="email"]', timeout=15)
            sb.sleep(3)
            sb.type('input[type="email"]', email, timeout=15)
            sb.sleep(3)
            sb.click("#identifierNext", timeout=15)
            sb.sleep(5)
            sb.click('input[type="password"]', timeout=10)
            sb.sleep(3)
            sb.type('input[type="password"]', password, timeout=10)
            sb.sleep(3)
            sb.click("#passwordNext", timeout=15)
            sb.sleep(5)

            # Dismiss optional confirm / recovery prompts
            sb.click_if_visible('input[name="confirm"]', timeout=30)

        # --- Navigate to Maps listing ---
        sb.get(maps_url)
        sb.sleep(random.uniform(3, 5))

        sb.click('button[aria-label*="Reviews"]', timeout=15)
        sb.sleep(random.uniform(3, 5))

        sb.click('button[aria-label="Write a review"]', timeout=30)
        sb.sleep(random.uniform(3, 5))

        # --- Confirm the review iframe is open before generating text ---
        sb.wait_for_element_visible(iframe_sel, timeout=15)

        # Generate review text now that we know the write box is open
        review_text = generate_review(business_name, review_prompt, star_rating)
        log.info("Review generated for %s", email)

        # Select star rating
        sb.nested_click(iframe_sel, f"div[data-rating='{star_rating}'][role='radio']")
        sb.sleep(random.uniform(3, 5))

        # Type review text into the textarea inside the iframe
        review_area = sb.get_nested_element(iframe_sel, 'textarea[aria-label="Enter review"]')
        review_area.type(review_text)
        sb.sleep(random.uniform(3, 5))

        # Click the Post button (inside the iframe)
        sb.nested_click(iframe_sel, 'button[aria-label="Post"]')
        sb.sleep(random.uniform(3, 5))

        # Click Done (back in the main document)
        sb.click('button[aria-label="Done"]', timeout=10)

        db.update_review_status(email_id, "reviewed", review_text, star_rating)
        log.info("Review posted successfully for %s", email)
        return True

    except Exception as exc:
        log.error("post_review failed for %s: %s", email, exc)
        log.debug(traceback.format_exc())
        db.update_review_status(email_id, "pending", None, star_rating)
        return False

    finally:
        sb.driver.stop()


def run_reviews(business_id, biz, config, delay=60):
    pending = db.get_pending_reviews(business_id)
    previous_email = None

    for review_row in pending:
        star = review_row["star_rating"] or 5

        # Mark as in-progress
        db.update_review_status(review_row["id"], "reviewing", None, star)

        # Post the review via Selenium (review text generated inside, only when write box is open)
        log.info("Opening browser for %s", review_row["email"])
        success = post_review(
            review_row["email"],
            review_row["password"],
            biz["url"],
            star,
            biz["name"],
            config.get("business_description", ""),
            review_row["id"],
            recovery_email=previous_email,
        )
        if not success:
            log.warning("post_review returned False for %s", review_row["email"])
        previous_email = review_row["email"]

        time.sleep(delay)
