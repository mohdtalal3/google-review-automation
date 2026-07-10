import csv
import io
import os
import random
from datetime import timedelta
from functools import wraps

from dotenv import load_dotenv
from flask import (
    Flask,
    Response,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from werkzeug.security import check_password_hash, generate_password_hash

import db

load_dotenv()

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", os.urandom(32).hex())
app.permanent_session_lifetime = timedelta(days=7)

# Hash is computed once at startup from the plaintext value in .env
_PASSWORD_HASH = generate_password_hash(
    os.environ.get("APP_PASSWORD", "changeme")
)

# ── Auth helpers ──────────────────────────────────────────────────────────────

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("logged_in"):
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated


# ── Auth routes ───────────────────────────────────────────────────────────────

@app.route("/", methods=["GET", "POST"])
def login():
    if session.get("logged_in"):
        return redirect(url_for("dashboard"))

    error = False
    if request.method == "POST":
        password = request.form.get("password", "")
        if check_password_hash(_PASSWORD_HASH, password):
            session.permanent = True
            session["logged_in"] = True
            return redirect(url_for("dashboard"))
        error = True

    return render_template("login.html", error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ── Dashboard ─────────────────────────────────────────────────────────────────

@app.route("/dashboard")
@login_required
def dashboard():
    businesses = db.get_all_businesses()
    all_emails = db.get_all_emails()
    return render_template("dashboard.html", businesses=businesses, all_emails=all_emails)


@app.route("/business/add", methods=["POST"])
@login_required
def add_business():
    name = request.form.get("name", "").strip()
    url = request.form.get("url", "").strip()
    if name and url:
        db.add_business(name, url)
    return redirect(url_for("dashboard"))


@app.route("/business/<int:business_id>/delete", methods=["POST"])
@login_required
def delete_business(business_id):
    db.delete_business(business_id)
    return redirect(url_for("dashboard"))


# ── Business detail ───────────────────────────────────────────────────────────

@app.route("/business/<int:business_id>")
@login_required
def business(business_id):
    biz = db.get_business(business_id)
    if not biz:
        return redirect(url_for("dashboard"))
    reviews = db.get_reviews_for_business(business_id)
    config = db.get_review_config(business_id)
    is_running = any(r["status"] == "reviewing" for r in reviews)
    all_emails = db.get_all_emails()
    used_email_ids = {r["email_id"] for r in reviews}
    selectable_email_count = sum(1 for e in all_emails if e["id"] not in used_email_ids)
    total_pool = db.count_emails()
    already_queued = db.count_queued_for_business(business_id)
    already_reviewed = db.count_reviewed_for_business(business_id)
    return render_template(
        "business.html",
        business=biz,
        reviews=reviews,
        config=config,
        is_running=is_running,
        all_emails=all_emails,
        used_email_ids=used_email_ids,
        selectable_email_count=selectable_email_count,
        total_pool=total_pool,
        already_queued=already_queued,
        already_reviewed=already_reviewed,
    )


@app.route("/business/<int:business_id>/save-config", methods=["POST"])
@login_required
def save_config(business_id):
    config = {
        "business_description": request.form.get("review_prompt", ""),
        "review_style": "",
        "star_1_count": 0,
        "star_2_count": 0,
        "star_3_count": 0,
        "star_4_count": 0,
        "star_5_count": 0,
        "delay_seconds": max(5, int(request.form.get("delay_seconds") or 60)),
    }
    db.save_review_config(business_id, config)
    return redirect(url_for("business", business_id=business_id, saved=1))


@app.route("/emails/sample-csv")
@login_required
def sample_csv():
    content = (
        "email,password,totp_secret\n"
        "example1@gmail.com,yourpassword1,\n"
        "example2@gmail.com,yourpassword2,JBSWY3DPEHPK3PXP\n"
    )
    return Response(
        content,
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=sample_emails.csv"},
    )


# ── Global email pool ─────────────────────────────────────────────────────────

@app.route("/emails/upload", methods=["POST"])
@login_required
def upload_emails():
    csv_file = request.files.get("csv_file")
    if csv_file and csv_file.filename:
        content = csv_file.read().decode("utf-8-sig")
        first_line = content.strip().split("\n")[0]
        if "\t" in first_line and "User ID" in first_line:
            # New G2G tab-separated format
            reader = csv.DictReader(io.StringIO(content), delimiter="\t")
            for row in reader:
                row = {k.strip(): (v or "").strip() for k, v in row.items()}
                email = row.get("User ID / Email Address", "").lstrip("'")
                password = row.get("Password", "")
                secret_q = row.get("First Secret Question", "").lower()
                totp_secret = row.get("First Secret Answer", "") if secret_q == "2fa" else None
                totp_secret = totp_secret or None
                if email and password:
                    db.add_email(email, password, totp_secret=totp_secret)
        else:
            # Legacy format: email,password[,totp_secret]
            reader = csv.DictReader(io.StringIO(content))
            for row in reader:
                email = (row.get("email") or "").strip()
                password = (row.get("password") or "").strip()
                totp_secret = (row.get("totp_secret") or "").strip() or None
                if email and password:
                    db.add_email(email, password, totp_secret=totp_secret)
    return redirect(url_for("dashboard"))


@app.route("/emails/add", methods=["POST"])
@login_required
def add_email():
    email = request.form.get("email", "").strip()
    password = request.form.get("password", "").strip()
    totp_secret = request.form.get("totp_secret", "").strip() or None
    if email and password:
        db.add_email(email, password, totp_secret=totp_secret)
    return redirect(url_for("dashboard"))


@app.route("/emails/delete/<int:email_id>", methods=["POST"])
@login_required
def delete_email(email_id):
    db.delete_email(email_id)
    return redirect(url_for("dashboard"))


# ── Per-business review management ────────────────────────────────────────────

@app.route("/business/<int:business_id>/reset-review/<int:review_id>", methods=["POST"])
@login_required
def reset_review(business_id, review_id):
    db.reset_review(review_id)
    return redirect(url_for("business", business_id=business_id))


@app.route("/business/<int:business_id>/delete-review/<int:review_id>", methods=["POST"])
@login_required
def delete_review(business_id, review_id):
    db.delete_review(review_id)
    return redirect(url_for("business", business_id=business_id))


# ── Bot control ───────────────────────────────────────────────────────────────

@app.route("/business/<int:business_id>/start-bot", methods=["POST"])
@login_required
def start_bot(business_id):
    # Optional email filter — if none submitted, use all
    selected_email_ids = request.form.getlist("selected_email_ids")
    allowed_email_ids = [int(x) for x in selected_email_ids if x.isdigit()] if selected_email_ids else None

    # Star counts come directly from the form
    star_list = []
    for star in range(1, 6):
        count = int(request.form.get(f"star_{star}_count") or 0)
        star_list.extend([star] * count)

    if not star_list:
        return jsonify({"status": "error", "message": "Select at least one star rating with a count > 0."})

    # Build review type list
    type_list = []
    for rtype in ['short', 'medium', 'long', 'no_text']:
        count = int(request.form.get(f"type_{rtype}_count") or 0)
        type_list.extend([rtype] * count)

    # Build language list
    language_list = []
    for lang in ['English', 'Spanish', 'French', 'German', 'Italian', 'Portuguese', 'Arabic', 'Chinese']:
        count = int(request.form.get(f"lang_{lang.lower()}_count") or 0)
        language_list.extend([lang] * count)

    # Shuffle all three lists independently so assignments are random
    random.shuffle(star_list)
    random.shuffle(type_list)
    random.shuffle(language_list)

    created, already_pending = db.create_review_jobs(business_id, star_list, type_list, language_list, allowed_email_ids=allowed_email_ids)
    if created == 0 and already_pending == 0:
        return jsonify({"status": "error", "message": "No new emails available to assign."})

    return jsonify({"status": "queued", "jobs_created": created, "already_pending": already_pending})


if __name__ == "__main__":
    db.init_db()
    db.migrate_add_delay_seconds()
    db.migrate_add_review_type_language()
    db.migrate_add_totp_secret()
    db.migrate_add_email_fail_tracking()
    #app.run(debug=True, port=5000)
    app.run(host="0.0.0.0", port=5000, debug=False)
