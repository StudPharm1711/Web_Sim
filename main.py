import os
import io
import random
import time
import json  # for parsing JSON responses from the API
import re  # for password complexity validation & optional post-processing
import uuid  # for generating unique session tokens
import statistics  # for computing median weighted cost
from datetime import datetime
from flask import Flask, render_template, redirect, url_for, request, flash, session, send_file, jsonify, Blueprint
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy.exc import IntegrityError
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from dotenv import load_dotenv
import openai
import stripe
from reportlab.lib.pagesizes import letter
from reportlab.platypus import SimpleDocTemplate, Paragraph, Preformatted
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from flask_migrate import Migrate
from itsdangerous import URLSafeTimedSerializer, SignatureExpired, BadSignature
from apscheduler.schedulers.background import BackgroundScheduler
import smtplib
from email.mime.text import MIMEText  # For sending emails via SMTP
from flask_session import Session
from datetime import timedelta  # add this at the top with your other imports

def get_client_ip():
    x_forwarded_for = request.headers.get('X-Forwarded-For', '')
    if x_forwarded_for:
        # Split the header on commas and return the first IP
        return x_forwarded_for.split(',')[0].strip()
    return request.remote_addr

# Load environment variables from .env file
load_dotenv()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
openai.api_key = OPENAI_API_KEY


# --- Password Complexity Validator ---
def validate_password(password):
    """
    Validate that the password meets the following criteria:
      - At least 8 characters
      - Contains at least one letter
      - Contains at least one digit.
    """
    if len(password) < 8:
        return False, "Password must be at least 8 characters long."
    if not re.search(r"[A-Za-z]", password):
        return False, "Password must contain at least one letter."
    if not re.search(r"[0-9]", password):
        return False, "Password must contain at least one digit."
    return True, ""


# Configure Stripe
stripe.api_key = os.getenv("STRIPE_SECRET_KEY")
STRIPE_STUDENT_PRICE_ID = os.getenv("STRIPE_STUDENT_PRICE_ID")  # e.g., for £3.49/month
STRIPE_NONSTUDENT_PRICE_ID = os.getenv("STRIPE_NONSTUDENT_PRICE_ID")  # e.g., for £4.99/month

# Set up model pricing (per 1M tokens) from environment
GPT4_INPUT_COST_PER_1M = float(os.getenv("GPT4_INPUT_COST_PER_1M", "10.00"))
GPT4_OUTPUT_COST_PER_1M = float(os.getenv("GPT4_OUTPUT_COST_PER_1M", "30.00"))
GPT35_INPUT_COST_PER_1M = float(os.getenv("GPT35_INPUT_COST_PER_1M", "0.50"))
GPT35_OUTPUT_COST_PER_1M = float(os.getenv("GPT35_OUTPUT_COST_PER_1M", "1.50"))

# Initialise Flask app and SQLAlchemy
app = Flask(__name__)
app.config['WTF_CSRF_ENABLED'] = False
app.config['SESSION_PERMANENT'] = False
app.config['SESSION_TYPE'] = "filesystem"
app.config['SQLALCHEMY_DATABASE_URI'] = os.getenv(
    "DATABASE_URL", "postgresql://postgres:London22!!@localhost/project_db"
)
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SECRET_KEY'] = os.getenv("SECRET_KEY", os.urandom(24).hex())
Session(app)

from flask_wtf import CSRFProtect

csrf = CSRFProtect()
csrf.init_app(app)

db = SQLAlchemy(app)
migrate = Migrate(app, db)
login_manager = LoginManager(app)
login_manager.login_view = 'login'


def csrf_exempt(view):
    view.csrf_exempt = True
    return view


# Initialise serializer for password reset tokens
s = URLSafeTimedSerializer(app.config['SECRET_KEY'])


# --- SMTP Email Helper using Brevo ---
def send_email_via_brevo(subject, body, to_address, html=False):
    from_address = os.getenv("FROM_EMAIL", "no-reply@email.simul-ai-tor.com")
    smtp_server = "smtp-relay.brevo.com"  # Brevo SMTP host
    smtp_port = 587  # Typical port for TLS
    smtp_login = os.getenv("BREVO_SMTP_LOGIN")
    smtp_password = os.getenv("BREVO_SMTP_PASSWORD")

    # Choose MIME type based on whether HTML is desired
    if html:
        msg = MIMEText(body, 'html')
    else:
        msg = MIMEText(body)  # defaults to plain text

    msg["Subject"] = subject
    msg["From"] = from_address
    msg["To"] = to_address

    try:
        email_string = msg.as_string()
        print("DEBUG: Email message to be sent:\n", email_string)
        with smtplib.SMTP(smtp_server, smtp_port) as server:
            server.starttls()  # Enable TLS
            server.login(smtp_login, smtp_password)
            server.send_message(msg)
            print(f"Email sent to {to_address} via Brevo SMTP.")
    except Exception as e:
        print(f"Error sending email to {to_address}: {e}")
        raise

# --- Models ---
class User(UserMixin, db.Model):
    __tablename__ = 'subscribers'
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(150), unique=True, nullable=False)
    password = db.Column(db.String(255), nullable=False)
    category = db.Column(db.String(50))
    discipline = db.Column(db.String(100))
    stripe_customer_id = db.Column(db.String(100))
    subscription_id = db.Column(db.String(100))
    subscription_status = db.Column(db.String(50))
    token_prompt_usage_gpt35 = db.Column(db.Integer, default=0)
    token_completion_usage_gpt35 = db.Column(db.Integer, default=0)
    token_prompt_usage_gpt4 = db.Column(db.Integer, default=0)
    token_completion_usage_gpt4 = db.Column(db.Integer, default=0)
    is_admin = db.Column(db.Boolean, default=False)
    current_session = db.Column(db.String(255), nullable=True)
    last_device_change = db.Column(db.DateTime)


class Feedback(db.Model):
    __tablename__ = 'feedback'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('subscribers.id'), nullable=False)
    score = db.Column(db.Float, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    user = db.relationship('User', backref=db.backref('feedbacks', lazy=True))


class PendingRegistration(db.Model):
    __tablename__ = 'pending_registration'
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(150), unique=True, nullable=False)
    hashed_password = db.Column(db.String(255), nullable=False)
    category = db.Column(db.String(50))
    discipline = db.Column(db.String(100))
    stripe_customer_id = db.Column(db.String(100))
    subscription_id = db.Column(db.String(100))
    subscription_status = db.Column(db.String(50))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class AlertSignup(db.Model):
    __tablename__ = 'alert_signup'
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(150), unique=True, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

class DeviceUsage(db.Model):
    __tablename__ = 'device_usage'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('subscribers.id'), nullable=False)
    ip_address = db.Column(db.String(100), nullable=False)
    user_agent = db.Column(db.String(256))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    last_used = db.Column(db.DateTime, default=datetime.utcnow)

    # Relationship back to user if desired
    user = db.relationship('User', backref=db.backref('devices', lazy=True))

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))


# --- Before Request: Ensure Single Session per User ---
@app.before_request
def ensure_single_session():
    if current_user.is_authenticated:
        token = session.get('session_token')
        if token != current_user.current_session:
            logout_user()
            flash("You have been logged out because your account was logged in from another location.", "warning")
            return redirect(url_for('login'))


# --- Global Simulation Variables ---
PATIENT_NAMES = [
    {"name": "Aisha Patel", "ethnicity": "South Asian (Indian)", "gender": "female", "age": 55},
    {"name": "John Smith", "ethnicity": "British (White)", "gender": "male", "age": 65},
    {"name": "Li Wei", "ethnicity": "East Asian (Chinese)", "gender": "male", "age": 42},
    # ... (other patient records)
]
random.shuffle(PATIENT_NAMES)

SYSTEM_COMPLAINTS = {
    "common ailments": [
        "I've had a dry cough for a week.",
        "My heart skips a beat every now and then.",
        # ... (other complaints)
    ],
    # ... (other system categories)
}

FEEDBACK_INSTRUCTION = (
    "You are an examiner, not a patient. Cease all patient role-playing immediately. Your task is to analyse the conversation "
    "history and provide detailed feedback on the user's communication and history-taking skills using the Calgary–Cambridge model. "
    "Also evaluate their clinical reasoning using hypothetical-deductive reasoning, dual-process theory, and Bayesian theory, noting "
    "any biases. Use specific examples from the dialogue provided. Keep feedback constructive, clear, and professional. "
    "Use British English spellings. End with: \"Thank you for the consultation. Goodbye.\" Here is the consultation transcript to review:"
)

PROMPT_INSTRUCTION = (
    "You are a Calgary Cambridge communication expert. Based on the following consultation transcript, provide one single, concise "
    "suggested next question for the user to ask, ensuring that essential patient details (e.g., demographics, personal history, key "
    "symptoms) are addressed. If the transcript does not include questions about the patient's name or date of birth, include such a "
    "question. Provide a brief justification (1-2 sentences) for your suggestion. Format your answer as a single bullet point."
)

# --- Blueprints and Routes ---
account_bp = Blueprint('account', __name__, url_prefix='/account')


@app.route('/account')
@login_required
def account():
    subscription_info = None
    if current_user.subscription_id:
        try:
            subscription = stripe.Subscription.retrieve(current_user.subscription_id)
            print("DEBUG: Stripe subscription retrieved:", subscription)  # Debug statement here
            current_period_end = datetime.utcfromtimestamp(subscription.current_period_end).strftime("%Y-%m-%d %H:%M:%S")
            subscription_info = {
                "cancel_at_period_end": subscription.cancel_at_period_end,
                "current_period_end": current_period_end,
                "status": subscription.status
            }
            print("DEBUG: subscription_info:", subscription_info)  # And here
        except Exception as e:
            flash(f"Error retrieving subscription info: {str(e)}", "danger")
    else:
        print("DEBUG: current_user.subscription_id is not set")
    return render_template('account.html', subscription_info=subscription_info)

app.register_blueprint(account_bp)

@app.route('/terms.html')
def terms():
    return render_template('terms.html')


@app.route('/instructions')
@login_required
def instructions():
    return render_template('instructions.html')


# --- Subscription Cancellation Route ---
@app.route('/cancel_subscription', methods=['POST'])
@login_required
def cancel_subscription():
    user = current_user
    if not user.subscription_id:
        flash("No active subscription to cancel.", "warning")
        return redirect(url_for('account'))
    try:
        subscription = stripe.Subscription.modify(user.subscription_id, cancel_at_period_end=True)
        user.subscription_status = subscription.status
        db.session.commit()
        subject = "Subscription Cancellation Confirmation"
        body = (
            "Hello,\n\nYour subscription has been scheduled for cancellation at the end of your current billing period. "
            "You will retain access until that time.\n\nThank you. Please note: This is an automated email and replies to this address are not monitored."
        )
        send_email_via_brevo(subject, body, user.email)
        flash(
            "Your subscription will be cancelled at the end of the current billing period. "
            "Please note: This is an automated email and replies to this address are not monitored.",
            "success"
        )
    except Exception as e:
        flash(f"Error cancelling subscription or sending email: {str(e)}", "danger")
    return redirect(url_for('account'))


# --- Reactivation Routes ---
@app.route('/reactivate_subscription')
@login_required
def reactivate_subscription():
    price_id = STRIPE_STUDENT_PRICE_ID if current_user.category == 'health_student' else STRIPE_NONSTUDENT_PRICE_ID
    checkout_session = stripe.checkout.Session.create(
        payment_method_types=["card"],
        line_items=[{"price": price_id, "quantity": 1}],
        mode="subscription",
        allow_promotion_codes=True,
        success_url=url_for('reactivate_payment_success', _external=True) + '?session_id={CHECKOUT_SESSION_ID}',
        cancel_url=url_for('account', _external=True)
    )
    return redirect(checkout_session.url)


@app.route('/reactivate_payment_success')
@login_required
def reactivate_payment_success():
    session_id = request.args.get('session_id')
    if not session_id:
        flash("No session ID provided.", "warning")
        return redirect(url_for('account'))
    try:
        checkout_session = stripe.checkout.Session.retrieve(session_id)
        subscription_id = checkout_session.subscription
        if not subscription_id:
            flash("No subscription ID found in session.", "danger")
            return redirect(url_for('account'))
        subscription = stripe.Subscription.retrieve(subscription_id)
        stripe_customer_id = checkout_session.customer

        current_user.stripe_customer_id = stripe_customer_id
        current_user.subscription_id = subscription_id
        current_user.subscription_status = subscription.status
        db.session.commit()

        subject = "Subscription Reactivation Confirmation"
        body = (
            "Hello,\n\nYour subscription has been reactivated. Enjoy using Simul-AI-tor.\n\n"
            "Best regards,\nThe Support Team. Please note: This is an automated email and replies to this address are not monitored."
        )
        send_email_via_brevo(subject, body, current_user.email)
        flash("Subscription reactivated successfully! A confirmation email has been sent.", "success")
    except Exception as e:
        flash(f"Error processing reactivation: {str(e)}", "danger")
    return redirect(url_for('account'))


@app.route('/')
def landing():
    active_count = User.query.filter(User.subscription_status == 'active').count()
    remaining_places = max(100 - active_count, 0)
    return render_template('landing.html', remaining_places=remaining_places)


ADMIN_LOGIN_PASSWORD = os.getenv("ADMIN_LOGIN_PASSWORD")


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        admin_flag = request.form.get('admin')
        if admin_flag == "true":
            admin_password = request.form.get('admin_password')
            if not admin_password or admin_password != ADMIN_LOGIN_PASSWORD:
                flash("Invalid admin password.", "danger")
                return redirect(url_for('login'))
            admin_user = User.query.filter_by(email="admin@example.com").first()
            if not admin_user:
                hashed_admin = generate_password_hash(ADMIN_LOGIN_PASSWORD)
                admin_user = User(email="admin@example.com", password=hashed_admin, is_admin=True)
                db.session.add(admin_user)
                db.session.commit()
            else:
                if not admin_user.is_admin:
                    admin_user.is_admin = True
                    db.session.commit()
            admin_user.current_session = str(uuid.uuid4())
            db.session.commit()
            session['session_token'] = admin_user.current_session
            login_user(admin_user)
            flash("Logged in as admin.", "success")
            return redirect(url_for('simulation'))
        else:
            email = request.form['email'].strip().lower()
            password = request.form['password']
            user = User.query.filter_by(email=email).first()
            if user and check_password_hash(user.password, password):
                if not user.is_admin and (not user.subscription_id or user.subscription_status != "active"):
                    flash("Your account is not activated yet. Please complete your payment to activate your account.",
                          "warning")
                    return redirect(url_for('start_payment'))

                # --------------------- Begin Device Usage Tracking ---------------------
                if not user.is_admin:
                    user_ip = get_client_ip()
                    user_agent = request.headers.get('User-Agent', '')
                    print("DEBUG: User IP:", user_ip)
                    print("DEBUG: User Agent:", user_agent)

                    devices = DeviceUsage.query.filter_by(user_id=user.id).all()
                    # Check if any stored device has the same IP and user agent
                    device_exists = any(
                        device.ip_address == user_ip and device.user_agent == user_agent for device in devices)

                    if not device_exists:
                        if len(devices) >= 2:
                            if user.last_device_change and datetime.utcnow() - user.last_device_change < timedelta(
                                    hours=24):
                                flash("You must wait 24 hours before registering a new device.", "danger")
                                return redirect(url_for('login'))
                            else:
                                token = s.dumps({"user_id": user.id, "ip": user_ip, "ua": user_agent}, salt='device-confirmation-salt')
                                confirmation_link = url_for('confirm_device', token=token, _external=True)
                                send_email_via_brevo("New Device Confirmation",
                                                     f"Click here to confirm your new device: {confirmation_link}",
                                                     user.email, html=False)
                                user.last_device_change = datetime.utcnow()
                                db.session.commit()
                                flash(
                                    "A confirmation email has been sent to add this new device. Please confirm it to continue.",
                                    "warning")
                                return redirect(url_for('login'))
                        else:
                            new_device = DeviceUsage(user_id=user.id, ip_address=user_ip, user_agent=user_agent)
                            db.session.add(new_device)
                            db.session.commit()
                    else:
                        for device in devices:
                            if device.ip_address == user_ip:
                                device.last_used = datetime.utcnow()
                        db.session.commit()
                # --------------------- End Device Usage Tracking ---------------------

                session.pop('conversation', None)
                user.current_session = str(uuid.uuid4())
                db.session.commit()
                session['session_token'] = user.current_session
                login_user(user)
                return redirect(url_for('simulation'))
            else:
                flash("Invalid email or password", "danger")
    return render_template('login.html')


@app.route('/confirm_device/<token>', methods=['GET'])
def confirm_device(token):
    try:
        data = s.loads(token, salt='device-confirmation-salt', max_age=3600)
        user_id = data.get("user_id")
        token_ip = data.get("ip")
        token_ua = data.get("ua")
    except Exception as e:
        flash("The confirmation link is invalid or has expired.", "danger")
        return redirect(url_for('login'))

    current_ip = get_client_ip()
    current_ua = request.headers.get('User-Agent', '')

    if current_ip != token_ip or current_ua != token_ua:
        flash("The device used to confirm does not match the device used to request registration.", "danger")
        return redirect(url_for('login'))

    user = User.query.get(user_id)
    if user:
        # Add new device record
        new_device = DeviceUsage(user_id=user.id, ip_address=token_ip, user_agent=token_ua)
        db.session.add(new_device)
        user.last_device_change = datetime.utcnow()
        db.session.commit()
        flash("New device confirmed and added.", "success")
    else:
        flash("User not found.", "danger")
    return redirect(url_for('login'))

@app.route('/register', methods=['GET', 'POST'])
def register():
    active_count = User.query.filter(User.subscription_status == 'active').count()
    if active_count >= 100:
        flash("All subscription spaces are currently taken. Please sign up for alerts when a space becomes available.",
              "info")
        return redirect(url_for('alert_signup'))
    if request.method == 'POST':
        print("DEBUG: /register POST route reached", flush=True)
        email = request.form['email'].strip().lower()
        password = request.form['password']
        category = request.form['category']
        discipline = request.form.get('discipline')
        other_discipline = request.form.get('otherDiscipline')
        promo_code = request.form.get('promo_code')

        is_valid, message = validate_password(password)
        if not is_valid:
            flash(message, "danger")
            return redirect(url_for('register'))

        if User.query.filter_by(email=email).first():
            flash("An account with this email already exists. Please use a different email.", "danger")
            return redirect(url_for('register'))

        if discipline == 'other' and other_discipline:
            discipline = other_discipline
        if category == 'health_student' and not email.lower().endswith('.ac.uk'):
            flash("For student registration, please use a valid academic email (ending with .ac.uk).", "danger")
            return redirect(url_for('register'))

        pending_registration = {
            "email": email,
            "hashed_password": generate_password_hash(password),
            "category": category,
            "discipline": discipline,
            "promo_code": promo_code
        }
        session['pending_registration'] = pending_registration

        existing_pending = PendingRegistration.query.filter_by(email=email).first()
        if existing_pending:
            flash(
                "A registration for this email is already pending confirmation. Please check your email or use the resend link.",
                "warning")
            return redirect(url_for('check_email'))

        new_pending = PendingRegistration(
            email=email,
            hashed_password=pending_registration["hashed_password"],
            category=category,
            discipline=discipline
        )
        try:
            db.session.add(new_pending)
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            flash(
                "A registration for this email is already pending confirmation. Please check your email or use the resend link.",
                "warning")
            return redirect(url_for('check_email'))

        token = s.dumps(json.dumps(pending_registration), salt='email-confirmation-salt')

        try:
            confirmation_link = url_for('confirm_email', token=token, _external=True)
            html_body = f"""
<html>
  <body>
    <p>Hello,</p>
    <p>Thank you for subscribing! To complete your registration, please confirm your email address by clicking the link below:</p>
    <p><a href="{confirmation_link}">Confirm Your Email Address</a></p>
    <p>If you did not register, please ignore this email.</p>
  </body>
</html>
"""
            subject = "Please Confirm Your Email Address"
            send_email_via_brevo(subject, html_body, pending_registration["email"], html=True)
            print("DEBUG: Confirmation email sent to:", pending_registration["email"])
        except Exception as e:
            print("DEBUG: Error sending confirmation email:", e)
            flash(f"Error sending confirmation email: {str(e)}", "warning")
            return redirect(url_for('register'))

        session['pending_email'] = pending_registration["email"]

        flash("A confirmation email has been sent. Please check your inbox to complete registration.", "info")
        return redirect(url_for('check_email'))

    # GET branch: render the registration form.
    return render_template('register.html')

@app.route('/check_email', methods=['GET'])
def check_email():
    return render_template('check_email.html')

@app.route('/alert_signup', methods=['GET', 'POST'])
def alert_signup():
    if request.method == 'POST':
        email = request.form.get('email')
        if not email:
            flash("Please provide an email address.", "danger")
            return redirect(url_for('alert_signup'))

        # Check if this email is already signed up
        existing = AlertSignup.query.filter_by(email=email).first()
        if existing:
            flash("This email is already signed up for alerts.", "info")
            return redirect(url_for('landing'))

        # Save the email in the AlertSignup table
        new_alert = AlertSignup(email=email)
        try:
            db.session.add(new_alert)
            db.session.commit()
            print(f"Alert record created for {email}")
        except Exception as e:
            db.session.rollback()
            flash(f"Error saving your email: {str(e)}", "danger")
            return redirect(url_for('alert_signup'))

        # Send confirmation email using SMTP helper
        try:
            subject = "Alert Signup Confirmation"
            body = (
                "Thank you for signing up for alerts! "
                "We will notify you as soon as a subscription space becomes available."
            )
            send_email_via_brevo(subject, body, email)
            print(f"Alert signup email sent to {email}")
            flash("Thank you! You've been signed up for alerts.", "success")
        except Exception as e:
            flash(f"Error sending alert confirmation email: {str(e)}", "danger")

        return redirect(url_for('landing'))

    return render_template('alert_signup.html')


# --- Modified Resend Confirmation Route ---
@app.route('/resend_confirmation', methods=['GET', 'POST'])
def resend_confirmation():
    if request.method == 'POST':
        email = request.form.get('email')
        if not email:
            flash("Please enter your email address.", "danger")
            return redirect(url_for('resend_confirmation'))
        pending = PendingRegistration.query.filter_by(email=email).first()
        if not pending:
            flash("No pending registration record found for that email. Please register again.", "danger")
            return redirect(url_for('register'))

        pending_data = {
            "email": pending.email,
            "hashed_password": pending.hashed_password,
            "category": pending.category,
            "discipline": pending.discipline,
            "stripe_customer_id": pending.stripe_customer_id,
            "subscription_id": pending.subscription_id,
            "subscription_status": pending.subscription_status
        }
        token = s.dumps(json.dumps(pending_data), salt='email-confirmation-salt')

        try:
            confirmation_link = url_for('confirm_email', token=token, _external=True)
            subject = "Please Confirm Your Email Address (New Link)"
            html_body = f"""
            <html>
              <body>
                <p>Hello,</p>
                <p>Your previous confirmation link expired. Please complete your registration by clicking the new link below:</p>
                <p><a href="{confirmation_link}">Confirm Your Email Address</a></p>
                <p>If you did not register, please ignore this email.</p>
              </body>
            </html>
            """
            send_email_via_brevo(subject, html_body, pending.email, html=True)
            flash("A new confirmation email has been sent. Please check your inbox.", "info")
        except Exception as e:
            flash(f"Error sending new confirmation email: {str(e)}", "warning")
        return redirect(url_for('login'))
    return render_template('resend_confirmation.html')

@app.route('/confirm_email/<token>', methods=['GET'])
def confirm_email(token):
    try:
        pending_json = s.loads(token, salt='email-confirmation-salt', max_age=3600)
        registration_data = json.loads(pending_json)
    except Exception as e:
        flash("The confirmation link is invalid or has expired.", "danger")
        return redirect(url_for('resend_confirmation'))

    existing_user = User.query.filter_by(email=registration_data["email"]).first()
    if existing_user:
        if not existing_user.subscription_id or existing_user.subscription_status != "active":
            flash("Please complete your subscription payment before logging in.", "warning")
            return redirect(url_for('start_payment'))
        flash("This email is already confirmed. Please log in.", "info")
        return redirect(url_for('login'))

    pending = PendingRegistration.query.filter_by(email=registration_data["email"]).first()
    if not pending:
        flash("No pending registration record found. Please register again.", "danger")
        return redirect(url_for('register'))

    new_user = User(
        email=pending.email,
        password=pending.hashed_password,
        category=pending.category,
        discipline=pending.discipline,
        stripe_customer_id=pending.stripe_customer_id,
        subscription_id=None,
        subscription_status=None
    )
    db.session.add(new_user)
    db.session.delete(pending)
    db.session.commit()

    new_user.current_session = str(uuid.uuid4())
    db.session.commit()
    session['session_token'] = new_user.current_session
    login_user(new_user)

    flash("Your email has been confirmed. Please complete payment to activate your account.", "success")
    return redirect(url_for('start_payment'))


@app.route('/start_payment')
@login_required
def start_payment():
    price_id = STRIPE_STUDENT_PRICE_ID if current_user.category == 'health_student' else STRIPE_NONSTUDENT_PRICE_ID
    checkout_session = stripe.checkout.Session.create(
        payment_method_types=["card"],
        line_items=[{"price": price_id, "quantity": 1}],
        mode="subscription",
        allow_promotion_codes=True,
        success_url=url_for('payment_success', _external=True) + '?session_id={CHECKOUT_SESSION_ID}',
        cancel_url=url_for('account', _external=True)
    )
    return redirect(checkout_session.url)


@app.route('/payment_success')
@login_required
def payment_success():
    session_id = request.args.get('session_id')
    if not session_id:
        flash("No session ID provided.", "warning")
        return redirect(url_for('start_payment'))
    try:
        checkout_session = stripe.checkout.Session.retrieve(session_id)
        subscription_id = checkout_session.subscription
        if not subscription_id:
            flash("No subscription ID found in session.", "danger")
            return redirect(url_for('start_payment'))
        subscription = stripe.Subscription.retrieve(subscription_id)
        stripe_customer_id = checkout_session.customer

        current_user.stripe_customer_id = stripe_customer_id
        current_user.subscription_id = subscription_id
        current_user.subscription_status = subscription.status
        db.session.commit()

        subject = "Subscription Confirmation"
        body = (
            "Hello,\n\nYour subscription has been successfully updated. Enjoy using Simul-AI-tor.\n\n"
            "Best regards,\nThe Support Team"
        )
        send_email_via_brevo(subject, body, current_user.email)

        flash("Subscription updated successfully!", "success")
        return redirect(url_for('account'))
    except Exception as e:
        flash(f"Error processing subscription: {str(e)}", "danger")
        return redirect(url_for('start_payment'))


@app.route('/payment_cancel')
def payment_cancel():
    flash("Payment was cancelled. Please try again.", "warning")
    return redirect(url_for('register'))


@app.route('/logout')
@login_required
def logout():
    session.pop('conversation', None)
    session.pop('feedback', None)
    session.pop('feedback_json', None)
    session.pop('hint', None)
    logout_user()
    return redirect(url_for('landing'))


@app.route('/about')
@login_required
def about():
    return render_template('about.html')


@app.route('/forgot_password', methods=['GET', 'POST'])
def forgot_password():
    if request.method == 'POST':
        email = request.form.get('email')
        user = User.query.filter_by(email=email).first()
        if user:
            token = s.dumps(email, salt='password-reset-salt')
            reset_url = url_for('reset_password', token=token, _external=True)
            html_body = f"""
<html>
  <body>
    <p>Dear User,</p>
    <p>We received a request to reset your password. To reset your password, please click the link below (this link is valid for 1 hour):</p>
    <p><a href="{reset_url}">Reset Your Password</a></p>
    <p>If you did not request a password reset, please ignore this email.</p>
    <p>Best regards,<br>Your Support Team</p>
  </body>
</html>
"""
            try:
                send_email_via_brevo("Password Reset Request", html_body, email, html=True)
                flash("A password reset link has been sent to your email. **Please check your SPAM folder**", "info")
            except Exception as e:
                flash(f"Error sending email: {str(e)}", "danger")
        else:
            flash("Email address not found.", "danger")
        return redirect(url_for('login'))
    return render_template('forgot_password.html')

@app.route('/reset_password/<token>', methods=['GET', 'POST'])
def reset_password(token):
    try:
        email = s.loads(token, salt='password-reset-salt', max_age=3600)
    except SignatureExpired:
        flash("The password reset link has expired.", "danger")
        return redirect(url_for('forgot_password'))
    except BadSignature:
        flash("Invalid password reset token.", "danger")
        return redirect(url_for('forgot_password'))
    if request.method == 'POST':
        new_password = request.form.get('password')
        confirm_password = request.form.get('confirm_password')
        if new_password != confirm_password:
            flash("Passwords do not match.", "danger")
            return redirect(url_for('reset_password', token=token))
        is_valid, message = validate_password(new_password)
        if not is_valid:
            flash(message, "danger")
            return redirect(url_for('reset_password', token=token))
        user = User.query.filter_by(email=email).first()
        if user:
            user.password = generate_password_hash(new_password)
            db.session.commit()
            flash("Your password has been updated successfully.", "success")
            return redirect(url_for('login'))
        else:
            flash("User not found.", "danger")
            return redirect(url_for('forgot_password'))
    return render_template('reset_password.html', token=token)


# --- Simulation and Messaging Routes ---
@app.route('/start_simulation', methods=['POST'])
@login_required
def start_simulation():
    problem_complexity = request.form.get('problem_complexity')
    patient_complexity = request.form.get('patient_complexity')
    nomenclature = request.form.get('drug_nomenclature', 'BNF')
    system_choice = request.form.get('system', 'random')
    comorbidities = request.form.get('comorbidities', 'no')
    session['comorbidities'] = comorbidities

    if comorbidities.lower() == "yes+":
        system_conditions = {
            "cardiovascular": [
                "hypertension",
                "atrial fibrillation",
                "hyperlipidaemia",
                "heart failure",
                "coronary artery disease",
                "peripheral vascular disease",
                "cardiomyopathy",
                "arrhythmia",
                "valvular heart disease",
                "congestive heart failure"
            ],
            "gastrointestinal": [
                "gastroesophageal reflux disease (GERD)",
                "peptic ulcer disease",
                "irritable bowel syndrome",
                "chronic pancreatitis",
                "liver cirrhosis",
                "cholelithiasis",
                "diverticulosis",
                "inflammatory bowel disease",
                "celiac disease",
                "gastroenteritis"
            ],
            "respiratory": [
                "mild asthma",
                "chronic obstructive pulmonary disease (COPD)",
                "bronchiectasis",
                "sleep apnoea",
                "interstitial lung disease",
                "pulmonary fibrosis",
                "chronic bronchitis",
                "emphysema",
                "allergic rhinitis",
                "upper respiratory tract infection"
            ],
            "musculoskeletal": [
                "osteoarthritis",
                "osteoporosis",
                "rheumatoid arthritis",
                "fibromyalgia",
                "gout",
                "tendinitis",
                "sciatica",
                "back pain",
                "spondylosis",
                "ligament sprain"
            ],
            "endocrine": [
                "type 2 diabetes",
                "addison's disease",
                "hyperthyroidism",
                "hypothyroidism",
                "Cushing's syndrome",
                "adrenal insufficiency",
                "polycystic ovary syndrome",
                "metabolic syndrome",
                "vitamin D deficiency",
                "hypoglycaemia"
            ],
            "ENT": [
                "allergic rhinitis",
                "chronic sinusitis",
                "otitis media",
                "tinnitus",
                "vertigo",
                "tonsillitis",
                "pharyngitis",
                "laryngitis",
                "nasal polyps",
                "hearing loss"
            ],
            "genitourinary": [
                "urinary tract infection",
                "kidney stones",
                "overactive bladder",
                "interstitial cystitis",
                "nephrolithiasis",
                "chronic kidney disease",
                "urinary incontinence",
                "polycystic kidney disease",
                "bladder cancer",
                "urethritis"
            ],
            "neurological": [
                "migraine",
                "tension headache",
                "epilepsy",
                "transient ischaemic attack",
                "multiple sclerosis",
                "Parkinson's disease",
                "peripheral neuropathy",
                "dizziness of unknown origin",
                "restless legs syndrome",
                "dementia"
                "depression"
                "ADHD"
                "Autism"
                "General Anxiety Disorder"
            ],
            "dermatological": [
                "psoriasis",
                "eczema",
                "acne vulgaris",
                "rosacea",
                "seborrheic dermatitis",
                "contact dermatitis",
                "vitiligo",
                "basal cell carcinoma",
                "melanoma",
                "impetigo"
            ]
        }
        all_conditions = set()
        for cond_list in system_conditions.values():
            all_conditions.update(cond_list)
        if len(all_conditions) < 5:
            raise ValueError("Not enough comorbid conditions defined to select 5 unique conditions.")
        selected_conditions = random.sample(list(all_conditions), k=5)
        comorbidity_details = " This patient has the following comorbid conditions: " + ", ".join(
            selected_conditions) + "."
    elif comorbidities.lower() == "yes":
        comorbidity_details = " This patient has co-morbidities, which may influence their clinical presentation based on their age and ethnicity."
    else:
        comorbidity_details = " This patient does not have any co-morbidities."

    if patient_complexity not in ['Nil', 'Memory Issues', 'Frustrated']:
        flash("Invalid patient complexity selected.", "danger")
        return redirect(url_for('simulation'))
    if not nomenclature:
        flash("Please select a drug naming standard.", "danger")
        return redirect(url_for('simulation'))

    if patient_complexity == "Nil":
        tone = " Always use natural patient friendly language throughout as a common person would. Avoid jargon"
    elif patient_complexity == "Memory Issues":
        tone = "You are very forgetful with significant memory issues. You are not orientated to time and place. Always use natural patient friendly language throughout as a common person would. Avoid jargon or technical words. You answer some questions inaccurately or \"I'm not sure\"."
    elif patient_complexity == "Frustrated":
        tone = " You are short tempered, noticeably frustrated, sarcastic and in a rush. You complain about past experiences with healthcare practitioners and question whether the person asking you questions is even qualified, although you do answer questions. Everything frustrates you. Always use natural patient friendly language throughout as a common person would. Avoid jargon or technical words."
    else:
        tone = ""

    if comorbidities.lower() == "yes+":
        eligible_patients = [p for p in PATIENT_NAMES if p["age"] >= 60]
        patient = random.choice(eligible_patients) if eligible_patients else random.choice(PATIENT_NAMES)
    elif patient_complexity == "Memory Issues":
        eligible_patients = [p for p in PATIENT_NAMES if p["age"] >= 60]
        patient = random.choice(eligible_patients) if eligible_patients else random.choice(PATIENT_NAMES)
    else:
        patient = random.choice(PATIENT_NAMES)

    if system_choice == 'random':
        system_choice = random.choice(list(SYSTEM_COMPLAINTS.keys()))
    complaints = SYSTEM_COMPLAINTS.get(system_choice)
    if complaints and isinstance(complaints, list):
        selected_complaint = random.choice(complaints)
    else:
        selected_complaint = "No complaint available."

    session['system_choice'] = system_choice
    session['selected_complaint'] = selected_complaint
    session['problem_complexity'] = problem_complexity
    session['patient_complexity'] = patient_complexity
    session['nomenclature'] = nomenclature

    instr = (
            f"You are a patient in a history-taking simulation using the {nomenclature} drug naming standard. "
            f"Your problem complexity is {problem_complexity} and your patient complexity is {patient_complexity}. "
            f"Your name is {patient['name']} (age {patient['age']}) and you are a {patient['gender']} patient."
            + comorbidity_details + " " +
            "At the very beginning of the consultation, your initial response is: "
            "\"Can I speak with someone about my symptoms?\". Once you have provided that opener, "
            "continue the conversation naturally without repeating the phrase. Consent to answering questions regardless of the interviewer's profession. "
            f"Present your complaint: {selected_complaint}. "
            "Provide only minimal details until further questions are asked, then gradually add more information. "
            "IMPORTANT: You are a patient and must NEVER provide any clinical advice or act as a clinician. "
            "If asked for advice or for anything beyond discussing your symptoms, steer the conversation back to your complaint. "
            "If you are asked to consent to a physical examination, ALWAYS respond affirmatively with a clear "
            "'Yes, I consent to a physical examination,' regardless of the virtual nature of the consultation."
            + tone
    )

    session['conversation'] = [{'role': 'system', 'content': instr}]
    print("DEBUG: Conversation initialized with prompt:", session['conversation'])

    try:
        response = openai.ChatCompletion.create(
            model="gpt-4-turbo",
            messages=session['conversation'],
            temperature=0.8
        )
        first_reply = response.choices[0].message["content"]
        if response.usage and 'prompt_tokens' in response.usage and 'completion_tokens' in response.usage:
            current_user.token_prompt_usage_gpt4 = (current_user.token_prompt_usage_gpt4 or 0) + response.usage[
                'prompt_tokens']
            current_user.token_completion_usage_gpt4 = (current_user.token_completion_usage_gpt4 or 0) + response.usage[
                'completion_tokens']
            db.session.commit()
    except Exception as e:
        first_reply = f"Error with API: {str(e)}"
    session['conversation'].append({'role': 'assistant', 'content': first_reply})
    print("DEBUG: After first reply, conversation state:", session['conversation'])
    session.pop('feedback', None)
    session.pop('hint', None)
    return redirect(url_for('simulation'))


@app.route('/simulation', methods=['GET'])
@login_required
def simulation():
    conversation = session.get('conversation', [])
    display_conv = [m for m in conversation if m['role'] != 'system']
    safe_display_conv = repr(display_conv).encode('utf-8', errors='replace').decode('utf-8')
    try:
        print("DEBUG: Display conversation (without system messages):", safe_display_conv)
    except OSError as e:
        print("DEBUG: Error printing conversation:", e)
    return render_template(
        'simulation.html',
        conversation=display_conv,
        feedback_json=session.get('feedback_json'),
        feedback_raw=session.get('feedback'),
        hint=session.get('hint')
    )


@app.route('/send_message', methods=['POST'])
@csrf.exempt
@login_required
def send_message():
    conversation = session.get('conversation', [])
    msg = request.form.get('message')
    generic_phrases = ["i need help", "help", "assist", "???", "??", "?"]
    forced_context_phrases = ["i am the patient", "i'm the patient", "i am the clinician", "i'm the clinician"]

    if msg:
        stripped_msg = msg.strip()
        lower_msg = stripped_msg.lower()
        if (len(stripped_msg) < 3 or
                lower_msg in generic_phrases or
                not re.search(r"[aeiou]", stripped_msg) or
                any(phrase in lower_msg for phrase in forced_context_phrases)):
            msg = "Sorry, I didn't catch that. Could you please repeat yourself?"

        conversation.append({'role': 'user', 'content': msg})
        session['conversation'] = conversation
        session.pop('hint', None)
        print("DEBUG: After user message added, conversation:", session['conversation'])
        return jsonify({"status": "ok"}), 200

    return jsonify({"status": "error", "message": "No message provided"}), 400


@app.route('/get_reply', methods=['POST'])
@csrf.exempt
@login_required
def get_reply():
    conversation = session.get('conversation', [])
    user_message_count = sum(1 for m in conversation if m.get('role') == 'user')

    if user_message_count > 0 and user_message_count % 3 == 0:
        reinforcement_message = ("REINFORCEMENT: You are a patient in a history-taking simulation. "
                                 "Remember: You must NEVER provide clinical advice or act as a clinician. "
                                 "Remain strictly in character as a patient.")
        if not any("REINFORCEMENT:" in m.get("content", "") for m in conversation if m.get("role") == "system"):
            insert_index = 1 if conversation and conversation[0].get('role') == 'system' else 0
            conversation.insert(insert_index, {'role': 'system', 'content': reinforcement_message})
            print("DEBUG: Reinforcement message inserted.")

    print("DEBUG: Before get_reply, conversation:", conversation)

    try:
        response = openai.ChatCompletion.create(
            model="gpt-3.5-turbo",
            messages=conversation,
            temperature=0.8
        )
        resp_text = response.choices[0].message["content"]
        if response.usage and 'prompt_tokens' in response.usage and 'completion_tokens' in response.usage:
            current_user.token_prompt_usage_gpt35 = (current_user.token_prompt_usage_gpt35 or 0) + response.usage[
                'prompt_tokens']
            current_user.token_completion_usage_gpt35 = (current_user.token_completion_usage_gpt35 or 0) + \
                                                        response.usage['completion_tokens']
            db.session.commit()
    except openai.error.OpenAIError as e:
        resp_text = f"OpenAI API Error: {str(e)}"
    except Exception as e:
        resp_text = f"Unexpected Error: {str(e)}"

    conversation.append({'role': 'assistant', 'content': resp_text})
    session['conversation'] = conversation
    print("DEBUG: After get_reply, conversation:", conversation)
    return jsonify({"reply": resp_text}), 200


@app.route('/hint', methods=['POST'])
@login_required
def hint():
    conversation = session.get('conversation', [])
    if not conversation:
        flash("No conversation available for hint suggestions", "warning")
        return redirect(url_for('simulation'))
    conv_text = "\n".join([f"{'User' if m['role'] == 'user' else 'Patient'}: {m['content']}" for m in conversation if
                           m['role'] != 'system'])
    hint_text = PROMPT_INSTRUCTION + "\n" + conv_text
    hint_conversation = [{'role': 'system', 'content': hint_text}]
    print("DEBUG: Hint prompt constructed:", hint_text)
    try:
        response = openai.ChatCompletion.create(
            model="gpt-3.5-turbo",
            messages=hint_conversation,
            temperature=0.8
        )
        hint_response = response.choices[0].message["content"]
        if response.usage and 'prompt_tokens' in response.usage and 'completion_tokens' in response.usage:
            current_user.token_prompt_usage_gpt35 = (current_user.token_prompt_usage_gpt35 or 0) + response.usage[
                'prompt_tokens']
            current_user.token_completion_usage_gpt35 = (current_user.token_completion_usage_gpt35 or 0) + \
                                                        response.usage['completion_tokens']
            db.session.commit()
    except Exception as e:
        hint_response = f"Error with API: {str(e)}"
    session['hint'] = hint_response
    print("DEBUG: Hint response received:", hint_response)
    return redirect(url_for('simulation'))


@app.route('/feedback', methods=['POST'])
@login_required
def feedback():
    # Check if feedback has already been provided in this session
    if session.get('feedback_given'):
        flash("Feedback has already been provided in this session.", "warning")
        return redirect(url_for('simulation'))

    conversation = session.get('conversation', [])
    if not conversation:
        flash("No conversation available for feedback", "warning")
        return redirect(url_for('simulation'))

    user_conv_text = "\n".join(
        [f"User: {m['content']}" for m in conversation if m.get('role') == 'user']
    )
    feedback_prompt = (
        "IMPORTANT: Output ONLY valid JSON with NO disclaimers or additional commentary. "
        "Your answer MUST start with '{' and end with '}'. Use double quotes for all keys and string values, "
        "and do NOT use single quotes. Evaluate the following consultation transcript using the Calgary–Cambridge model. "
        "Score each category on a scale of 1 to 10, and provide a short comment for each:\n"
        "1. Initiating the session\n"
        "2. Gathering information: When scoring, ensure you assess if the transcript clearly explores the history of the present complaint, past medical and surgical history, medication history including allergies, social history (including living situation, vocation, diet, exercise, alcohol consumption, smoking) and family history\n"
        "3. Physical examination: When scoring consider if consent sought and whether results of the examination were discussed\n"
        "4. Explanation & planning\n"
        "5. Closing the session: When scoring consider if a clear management plan, safety-netting and a plan for follow-up were discussed\n"
        "6. Building a relationship\n"
        "7. Providing structure\n\n"
        "Then, calculate the overall score (max 70) and provide a brief commentary on the user's clinical reasoning considering hypothetico-deductive and bayesian reasoning in particular"
        ' in a key called "clinical_reasoning".\n\n'
        "Format your answer strictly as a single JSON object (do not include any extra text):\n"
        "{\n"
        '  "initiating_session": {"score": X, "comment": "..."},\n'
        '  "gathering_information": {"score": X, "comment": "..."},\n'
        '  "physical_examination": {"score": X, "comment": "..."},\n'
        '  "explanation_planning": {"score": X, "comment": "..."},\n'
        '  "closing_session": {"score": X, "comment": "..."},\n'
        '  "building_relationship": {"score": X, "comment": "..."},\n'
        '  "providing_structure": {"score": X, "comment": "..."},\n'
        '  "overall": Y,\n'
        '  "clinical_reasoning": "..." \n'
        '}\n\n'
        "The consultation transcript is:\n" + user_conv_text
    )

    feedback_conversation = [{'role': 'system', 'content': feedback_prompt}]
    print("DEBUG: Feedback prompt constructed:", feedback_prompt)
    try:
        response = openai.ChatCompletion.create(
            model="gpt-4-turbo",
            messages=feedback_conversation,
            temperature=0.8,
            max_tokens=500
        )
        fb = response.choices[0].message["content"]
        print("DEBUG: Raw GPT-4 feedback:", fb)
        if response.usage and 'prompt_tokens' in response.usage and 'completion_tokens' in response.usage:
            current_user.token_prompt_usage_gpt4 = (current_user.token_prompt_usage_gpt4 or 0) + response.usage['prompt_tokens']
            current_user.token_completion_usage_gpt4 = (current_user.token_completion_usage_gpt4 or 0) + response.usage['completion_tokens']
            db.session.commit()
    except Exception as e:
        fb = f"Error generating feedback: {str(e)}"
    try:
        feedback_json = json.loads(fb)
        pretty_feedback = json.dumps(feedback_json, indent=2)
        session['feedback_json'] = feedback_json
        session['feedback'] = pretty_feedback
        print("DEBUG: Feedback JSON parsed successfully:", pretty_feedback)
    except Exception as e:
        print("DEBUG: JSON parsing error in feedback:", e)
        session['feedback_json'] = None
        session['feedback'] = fb

    # Set flag to mark feedback as provided
    session['feedback_given'] = True
    return redirect(url_for('simulation'))

@app.route('/download_feedback', methods=['GET'])
@login_required
def download_feedback():
    feedback_dict = session.get('feedback_json')
    raw_feedback = session.get('feedback')
    if not feedback_dict and not raw_feedback:
        flash("No feedback available to download", "warning")
        return redirect(url_for('simulation'))
    import io
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, ListFlowable, ListItem
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.enums import TA_LEFT
    from reportlab.lib import colors
    pdf_buffer = io.BytesIO()
    doc = SimpleDocTemplate(pdf_buffer, pagesize=letter)
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        name='Title',
        parent=styles['Title'],
        fontName='Helvetica-Bold',
        fontSize=14,
        alignment=TA_LEFT,
        textColor=colors.black,
        spaceAfter=12
    )
    bullet_style = ParagraphStyle(
        name='Bullet',
        parent=styles['Normal'],
        fontName='Helvetica',
        fontSize=11,
        leading=14,
        leftIndent=20,
        spaceBefore=4,
        spaceAfter=4
    )
    story = []
    story.append(Paragraph("Consultation Feedback", title_style))
    story.append(Spacer(1, 8))
    if feedback_dict:
        bullet_items = []
        is_score = feedback_dict["initiating_session"]["score"]
        is_comment = feedback_dict["initiating_session"]["comment"]
        bullet_items.append(
            ListItem(
                Paragraph(f"<b>Initiating the session:</b> Score: {is_score}, Comment: {is_comment}", bullet_style),
                bulletSymbol="•"
            )
        )
        gi_score = feedback_dict["gathering_information"]["score"]
        gi_comment = feedback_dict["gathering_information"]["comment"]
        bullet_items.append(
            ListItem(
                Paragraph(f"<b>Gathering information:</b> Score: {gi_score}, Comment: {gi_comment}", bullet_style),
                bulletSymbol="•"
            )
        )
        pe_score = feedback_dict["physical_examination"]["score"]
        pe_comment = feedback_dict["physical_examination"]["comment"]
        bullet_items.append(
            ListItem(
                Paragraph(f"<b>Physical examination:</b> Score: {pe_score}, Comment: {pe_comment}", bullet_style),
                bulletSymbol="•"
            )
        )
        ep_score = feedback_dict["explanation_planning"]["score"]
        ep_comment = feedback_dict["explanation_planning"]["comment"]
        bullet_items.append(
            ListItem(
                Paragraph(f"<b>Explanation &amp; planning:</b> Score: {ep_score}, Comment: {ep_comment}", bullet_style),
                bulletSymbol="•"
            )
        )
        cs_score = feedback_dict["closing_session"]["score"]
        cs_comment = feedback_dict["closing_session"]["comment"]
        bullet_items.append(
            ListItem(
                Paragraph(f"<b>Closing the session:</b> Score: {cs_score}, Comment: {cs_comment}", bullet_style),
                bulletSymbol="•"
            )
        )
        br_score = feedback_dict["building_relationship"]["score"]
        br_comment = feedback_dict["building_relationship"]["comment"]
        bullet_items.append(
            ListItem(
                Paragraph(f"<b>Building a relationship:</b> Score: {br_score}, Comment: {br_comment}", bullet_style),
                bulletSymbol="•"
            )
        )
        ps_score = feedback_dict["providing_structure"]["score"]
        ps_comment = feedback_dict["providing_structure"]["comment"]
        bullet_items.append(
            ListItem(
                Paragraph(f"<b>Providing structure:</b> Score: {ps_score}, Comment: {ps_comment}", bullet_style),
                bulletSymbol="•"
            )
        )
        overall_score = feedback_dict["overall"]
        bullet_items.append(
            ListItem(
                Paragraph(f"<b>Overall Score:</b> {overall_score}/70", bullet_style),
                bulletSymbol="•"
            )
        )
        if "clinical_reasoning" in feedback_dict:
            cr_text = feedback_dict["clinical_reasoning"]
            bullet_items.append(
                ListItem(
                    Paragraph(f"<b>Clinical Reasoning &amp; Bias Analysis:</b> {cr_text}", bullet_style),
                    bulletSymbol="•"
                )
            )
        feedback_list = ListFlowable(bullet_items, bulletType='bullet', start=None)
        story.append(feedback_list)
    else:
        fallback_style = ParagraphStyle(
            name='Fallback',
            parent=styles['Normal'],
            fontName='Courier',
            fontSize=10,
            leading=12
        )
        story.append(Paragraph("Raw Feedback:", title_style))
        story.append(Paragraph(raw_feedback, fallback_style))
    doc.build(story)
    pdf_buffer.seek(0)
    return send_file(
        pdf_buffer,
        as_attachment=True,
        download_name="feedback.pdf",
        mimetype="application/pdf"
    )


@app.route('/clear_simulation')
@login_required
def clear_simulation():
    print("DEBUG: Clearing simulation; previous conversation:", session.get('conversation'))
    session.pop('feedback', None)
    session.pop('feedback_json', None)
    session.pop('hint', None)
    session.pop('feedback_given', None)  # Clear the feedback flag here
    nomenclature = request.form.get('drug_nomenclature', 'BNF')
    import random
    patient = session.get('patient')
    if not patient:
        patient = random.choice(PATIENT_NAMES)
        session['patient'] = patient
    instr = (
        f"You are a patient in a history-taking simulation using the {nomenclature} drug naming standard. "
        f"Your name is {patient['name']} (age {patient['age']}) and you are a {patient['gender']} patient. "
        "Begin every interaction by saying exactly: \"Can I speak with someone about my symptoms?\" "
        "and wait for the user's response before providing further details. "
        "Present your complaint: <your complaint here>. "
        "Provide only minimal details until further questions are asked, then gradually add more information. "
        "IMPORTANT: You are a patient and must NEVER provide any clinical advice or act as a clinician. "
        "If asked for advice or for anything beyond discussing your symptoms, steer the conversation back to your complaint. "
        "If you are asked to consent to a physical examination, ALWAYS respond affirmatively with a clear 'Yes, I consent to a physical examination,' "
        "regardless of the virtual nature of the consultation."
    )
    session['conversation'] = [{'role': 'system', 'content': instr}]
    print("DEBUG: Simulation reinitialized with prompt:", session['conversation'])
    return redirect(url_for('simulation'))


@app.route('/generate_exam', methods=['POST'])
@login_required
def generate_exam():
    conversation = session.get('conversation', [])
    user_messages = [msg for msg in conversation if msg.get('role') == 'user']

    if len(user_messages) < 2:
        return jsonify({"error": "Please ask at least two questions before accessing exam results."}), 403

    data = request.get_json()
    complaint = data.get('complaint')
    if not complaint:
        return jsonify({"error": "No complaint provided"}), 400

    system_choice = session.get('system_choice', 'random')

    recent_patient_history = " ".join(msg['content'] for msg in user_messages[-3:])

    vitals_prompt = (
        "Include vital signs such as heart rate, blood pressure, respiratory rate, temperature, and oxygen saturation. "
    )

    extra_instructions = {
        "common ailments": (
            "Then, describe any mild or non-specific findings directly related to the patient's complaint."
        ),
        "ENT": (
            "Then, focus exclusively on the ENT examination: examine ears for external deformities, tenderness, wax, and discharge; "
            "inspect tympanic membranes; assess hearing; evaluate nasal passages and sinuses; inspect throat and oropharynx."
        ),
        "cardiovascular": (
            "Then, focus exclusively on cardiovascular examination: describe heart rate, rhythm, murmurs, and peripheral pulses."
        ),
        "respiratory": (
            "Then, focus exclusively on respiratory examination: describe lung sounds, wheezes, crackles, and breathing effort."
        ),
        "gastrointestinal": (
            "Then, focus exclusively on abdominal examination: describe tenderness, guarding, bowel sounds, or peritonitis signs."
        ),
        "neurological": (
            "Then, focus exclusively on neurological examination: assess alertness, cranial nerves, motor and sensory responses, focal deficits."
        ),
        "musculoskeletal": (
            "Then, conduct a musculoskeletal exam specifically matching the patient's complaint: describe joint motion, tenderness, swelling, and muscle strength."
        ),
        "genitourinary": (
            "Then, focus exclusively on genitourinary examination: check lower abdomen tenderness, urinary symptoms, and infection signs."
        ),
        "endocrine": (
            "Then, focus exclusively on endocrine examination: note general appearance, skin/hair changes, or metabolic disturbances."
        ),
        "dermatological": (
            "Then, focus exclusively on dermatological examination: describe distribution, texture, color, inflammation, or infection."
        )
    }.get(system_choice,
          "Then, generate a complete physical examination relevant to the complaint."
          )

    exam_prompt = (
            f"A patient presents with '{complaint}'. Recent patient statements include: {recent_patient_history}. "
            + vitals_prompt
            + extra_instructions +
            " Ensure examination findings explicitly match and are consistent with the patient's complaint and recent statements including vital signs. "
            "Use plain language without acronyms or abbreviations. Provide ONLY the physical exam findings and vitals without introductory phrases or extra text."
    )

    print("DEBUG: Exam prompt:", exam_prompt)

    try:
        response = openai.ChatCompletion.create(
            model="gpt-3.5-turbo",
            messages=[{"role": "system", "content": exam_prompt}],
            temperature=0.4,
            max_tokens=250
        )

        exam_results = response.choices[0].message["content"].strip()

        if response.usage and 'prompt_tokens' in response.usage and 'completion_tokens' in response.usage:
            current_user.token_prompt_usage_gpt35 = (current_user.token_prompt_usage_gpt35 or 0) + response.usage[
                'prompt_tokens']
            current_user.token_completion_usage_gpt35 = (current_user.token_completion_usage_gpt35 or 0) + \
                                                        response.usage['completion_tokens']
            db.session.commit()

    except Exception as e:
        exam_results = f"Error generating exam results: {str(e)}"

    print("DEBUG: Exam results generated:", exam_results)
    return jsonify({"results": exam_results}), 200


def notify_alert_signups():
    MAX_SUBSCRIPTIONS = 100
    active_count = User.query.filter(User.subscription_status == 'active').count()
    free_spaces = MAX_SUBSCRIPTIONS - active_count
    free_percentage = free_spaces / MAX_SUBSCRIPTIONS

    if free_percentage >= 0.15:
        alert_signups = AlertSignup.query.all()
        for signup in alert_signups:
            try:
                subject = "Subscription Space Now Available!"
                body = (
                    "Good news! There are now enough subscription spaces available for you to register. "
                    "Please visit our registration page to sign up."
                )
                send_email_via_brevo(subject, body, signup.email)
                print(f"Notification sent to {signup.email}")
                db.session.delete(signup)
            except Exception as e:
                print(f"Error sending alert to {signup.email}: {str(e)}")
        db.session.commit()


def send_daily_update():
    with app.app_context():
        try:
            active_students = User.query.filter(
                User.subscription_status == 'active',
                User.category == 'health_student'
            ).count()
            active_non_students = User.query.filter(
                User.subscription_status == 'active',
                User.category != 'health_student'
            ).count()
            active_users = User.query.filter(User.subscription_status == 'active').all()

            weighted_costs = []
            total_cost = 0.0
            for user in active_users:
                cost_gpt35 = ((user.token_prompt_usage_gpt35 or 0) / 1_000_000 * GPT35_INPUT_COST_PER_1M) + (
                        (user.token_completion_usage_gpt35 or 0) / 1_000_000 * GPT35_OUTPUT_COST_PER_1M)
                cost_gpt4 = ((user.token_prompt_usage_gpt4 or 0) / 1_000_000 * GPT4_INPUT_COST_PER_1M) + (
                        (user.token_completion_usage_gpt4 or 0) / 1_000_000 * GPT4_OUTPUT_COST_PER_1M)
                user_cost = cost_gpt35 + cost_gpt4
                weighted_costs.append(user_cost)
                total_cost += user_cost

            median_weighted_cost = statistics.median(weighted_costs) if weighted_costs else 0.0

            message = (
                f"Daily Update:\n"
                f"Active Student Subscriptions: {active_students}\n"
                f"Active Non-Student Subscriptions: {active_non_students}\n"
                f"Total Active Subscriptions: {len(active_users)}\n\n"
                f"Token Cost Analysis (weighted average per subscription):\n"
                f"Median Cost per Subscription: ${median_weighted_cost:.4f}\n"
                f"Total Estimated API Cost (cumulative): ${total_cost:.4f}\n\n"
                f"Subscription Prices:\n"
                f" - Health Student: $4.99/month\n"
                f" - Non-Student: $7.99/month\n\n"
                f"Monthly API Budget: $120.00\n"
                f"Current Headroom: ${120 - total_cost if total_cost < 120 else 0:.2f}\n"
            )
            subject = "Daily Subscription & API Cost Report"
            send_email_via_brevo(subject, message, "simulaitor@outlook.com")
            print("Daily update email sent.")
        except Exception as e:
            print(f"Error sending daily update: {str(e)}")


@app.route('/test_send_update')
def test_send_update():
    send_daily_update()
    return "Test email sent. Please check your inbox."


def get_last_three_average(user_id):
    last_three = Feedback.query.filter_by(user_id=user_id).order_by(Feedback.created_at.desc()).limit(3).all()
    if not last_three:
        return 0
    avg_score = sum(f.score for f in last_three) / len(last_three)
    return avg_score


def get_user_ranking(user_id):
    users_avg = db.session.query(
        Feedback.user_id,
        db.func.avg(Feedback.score).label("avg_score")
    ).group_by(Feedback.user_id).all()

    sorted_users = sorted(users_avg, key=lambda x: x.avg_score, reverse=True)
    ranking = next((index + 1 for index, record in enumerate(sorted_users) if record.user_id == user_id), None)
    total_users = len(sorted_users)
    return ranking, total_users


@app.route('/get_scores')
@login_required
def get_scores():
    avg_score = get_last_three_average(current_user.id)
    ranking, total_users = get_user_ranking(current_user.id)
    return jsonify({
        "avg_score": avg_score,
        "ranking": ranking,
        "total_users": total_users
    })


if __name__ == '__main__':
    app.run(debug=True)
