import os
import random
import time

from dotenv import load_dotenv
from seleniumbase import SB
import google.genai as genai
from google.genai import types

import db

load_dotenv()

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
PROXY = os.environ.get("PROXY", "")

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


def post_review(email, password, maps_url, star_rating, review_text, email_id, recovery_email=None):
    safe_name = email.replace("@", "_at_").replace(".", "_")
    profile_dir = os.path.abspath(os.path.join("chrome_profiles", safe_name))
    os.makedirs(profile_dir, exist_ok=True)

    try:
        with SB(uc=True, user_data_dir=profile_dir,proxy=PROXY) as sb:
            # Check login state
            sb.open("https://accounts.google.com/signin/v2/identifier")
            time.sleep(5)

            if "accounts.google.com/v3/signin" in sb.get_current_url():
                # Sign in with email
                time.sleep(2)
                sb.type('input[type="email"]', email)
                time.sleep(3)
                sb.click("#identifierNext")
                time.sleep(5)
                sb.type('input[type="password"]', password)
                time.sleep(3)
                sb.click("#passwordNext")
                time.sleep(5)
                try:
                    sb.click('//button[.//span[text()="Not now"]]', timeout=10)
                except Exception:
                    pass
                time.sleep(3)
                try:
                    sb.type('input[type="email"]', recovery_email or email, timeout=10)
                    time.sleep(2)
                    sb.click('button[aria-label="Save"]', timeout=10)
                except Exception:
                    pass
                time.sleep(3)
                try:
                    sb.click('button[aria-label="Skip"]', timeout=10)
                except Exception:
                    pass
                time.sleep(5)


            sb.open(maps_url)
            time.sleep(random.uniform(3, 5))
            sb.click("//div[text()='Reviews']", timeout=10)
            time.sleep(random.uniform(3, 5))
            sb.click("//span[text()='Write a review']", timeout=10)
            time.sleep(random.uniform(3, 5))
            sb.switch_to_frame("iframe[name='goog-reviews-write-widget']")
            sb.click(f"div[data-rating='{star_rating}'][role='radio']", timeout=10)
            time.sleep(random.uniform(3, 5))
            sb.type("//textarea[@aria-label='Enter review']", review_text)
            time.sleep(random.uniform(3, 5))
            sb.click("//button[.//span[text()='Post']]", timeout=10)
            time.sleep(random.uniform(3, 5))

        db.update_review_status(email_id, "reviewed", review_text, star_rating)
        return True

    except Exception:
        db.update_review_status(email_id, "pending", None, star_rating)
        return False


def run_reviews(business_id, biz, config, delay=60):
    pending = db.get_pending_reviews(business_id)
    previous_email = None

    for review_row in pending:
        star = review_row["star_rating"] or 5

        # Mark as in-progress
        db.update_review_status(review_row["id"], "reviewing", None, star)

        # Generate review text via Gemini
        try:
            review_text = generate_review(
                biz["name"],
                config.get("business_description", ""),
                star,
            )
        except Exception as exc:
            db.update_review_status(
                review_row["id"], "pending", None, star
            )
            continue

        # Post the review via Selenium
        post_review(
            review_row["email"],
            review_row["password"],
            biz["url"],
            star,
            review_text,
            review_row["id"],
            recovery_email=previous_email,
        )
        previous_email = review_row["email"]

        time.sleep(delay)
