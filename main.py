import os
import io
import random
import time
import json  # for parsing JSON responses from the API
import re  # for password complexity validation
from datetime import datetime  # added for date conversion
from flask import Flask, render_template, redirect, url_for, request, flash, session, send_file, jsonify, Blueprint
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from dotenv import load_dotenv
import openai
import stripe
from reportlab.lib.pagesizes import letter
from reportlab.platypus import SimpleDocTemplate, Paragraph
from reportlab.lib.styles import getSampleStyleSheet
from flask_migrate import Migrate
from itsdangerous import URLSafeTimedSerializer, SignatureExpired, BadSignature

# Import SendGrid libraries
from sendgrid import SendGridAPIClient
from sendgrid.helpers.mail import Mail

# Load environment variables from .env file
load_dotenv()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
openai.api_key = OPENAI_API_KEY

# Configure Stripe
stripe.api_key = os.getenv("STRIPE_SECRET_KEY")
STRIPE_STUDENT_PRICE_ID = os.getenv("STRIPE_STUDENT_PRICE_ID")  # e.g., for £3.49/month
STRIPE_NONSTUDENT_PRICE_ID = os.getenv("STRIPE_NONSTUDENT_PRICE_ID")  # e.g., for £4.99/month

# Initialise Flask app and SQLAlchemy
app = Flask(__name__)
app.config['SESSION_PERMANENT'] = False
app.config['SESSION_TYPE'] = "filesystem"
app.config['SQLALCHEMY_DATABASE_URI'] = os.getenv("DATABASE_URL",
                                                  "postgresql://postgres:London22!!@localhost/project_db")
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SECRET_KEY'] = os.getenv("SECRET_KEY", os.urandom(24).hex())

db = SQLAlchemy(app)
migrate = Migrate(app, db)
login_manager = LoginManager(app)
login_manager.login_view = 'login'

# Initialise serializer for password reset tokens
s = URLSafeTimedSerializer(app.config['SECRET_KEY'])

# --- Password Complexity Validator ---
def validate_password(password):
    """
    Validate that the password meets the following criteria:
      - At least 8 characters
      - Contains at least one letter
      - Contains at least one digit
    """
    if len(password) < 8:
        return False, "Password must be at least 8 characters long."
    if not re.search(r"[A-Za-z]", password):
        return False, "Password must contain at least one letter."
    if not re.search(r"[0-9]", password):
        return False, "Password must contain at least one digit."
    return True, ""

# --- User Model Definition ---
class User(UserMixin, db.Model):
    __tablename__ = 'user'  # Explicit table name
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(150), unique=True, nullable=False)
    password = db.Column(db.String(255), nullable=False)
    category = db.Column(db.String(50))
    discipline = db.Column(db.String(100))
    stripe_customer_id = db.Column(db.String(100))
    subscription_id = db.Column(db.String(100))
    subscription_status = db.Column(db.String(50))
    is_admin = db.Column(db.Boolean, default=False)

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

# --- Global Simulation Variables ---
PATIENT_NAMES = [
    {"name": "Aisha Patel", "ethnicity": "South Asian (Indian)", "gender": "female"},
    {"name": "John Smith", "ethnicity": "British (White)", "gender": "male"},
    {"name": "Li Wei", "ethnicity": "East Asian (Chinese)", "gender": "male"},
    {"name": "Fatima Ali", "ethnicity": "Middle Eastern (Arabic)", "gender": "female"},
    {"name": "Carlos Rivera", "ethnicity": "Hispanic (Mexican)", "gender": "male"},
    {"name": "Nia Okoye", "ethnicity": "African (Nigerian)", "gender": "female"},
    {"name": "Sofia Nguyen", "ethnicity": "Southeast Asian (Vietnamese)", "gender": "female"},
    {"name": "Mohamed Hassan", "ethnicity": "African (Somali)", "gender": "male"},
]

FOUNDATION_COMPLAINTS = [
    "fever", "persistent cough", "mild chest pain", "headache", "lower back pain"
]
ENHANCED_COMPLAINTS = [
    "chest pain", "shortness of breath", "persistent fatigue", "abdominal pain", "chronic diarrhoea"
]
ADVANCED_COMPLAINTS = [
    "severe chest pain", "acute shortness of breath", "chronic fatigue", "irregular heart palpitations",
    "intense abdominal pain"
]

FEEDBACK_INSTRUCTION = (
    "You are an examiner, not a patient. Cease all patient role-playing immediately. Your task is to analyse the conversation history and provide detailed feedback "
    "on the user's communication and history-taking skills using the Calgary–Cambridge model. Also evaluate their clinical reasoning using hypothetical-deductive reasoning, "
    "dual-process theory, and Bayesian theory, noting any biases. Use specific examples from the dialogue provided. Keep feedback constructive, clear, and professional. "
    "Use British English spellings. End with: \"Thank you for the consultation. Goodbye.\" Here is the consultation transcript to review:"
)

PROMPT_INSTRUCTION = (
    "You are a Calgary Cambridge communication expert. Based on the following consultation transcript, provide one single, concise suggested next question for the user to ask, "
    "ensuring that essential patient details (e.g., demographics, personal history, key symptoms) are addressed. If the transcript does not include questions about the patient's name "
    "or date of birth, include such a question. Provide a brief justification (1-2 sentences) for your suggestion. Format your answer as a single bullet point."
)

# --- Account Blueprint ---
account_bp = Blueprint('account', __name__, url_prefix='/account')

@app.route('/account')
@login_required
def account():
    subscription_info = None
    if current_user.subscription_id:
        try:
            subscription = stripe.Subscription.retrieve(current_user.subscription_id)
            current_period_end = datetime.utcfromtimestamp(subscription.current_period_end).strftime("%Y-%m-%d %H:%M:%S")
            subscription_info = {
                "cancel_at_period_end": subscription.cancel_at_period_end,
                "current_period_end": current_period_end,
                "status": subscription.status
            }
        except Exception as e:
            flash(f"Error retrieving subscription info: {str(e)}", "danger")
    return render_template('account.html', subscription_info=subscription_info)

app.register_blueprint(account_bp)

# --- New Route for Terms and Conditions ---
@app.route('/terms.html')
def terms():
    return render_template('terms.html')

# --- New Route for Instructions ---
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
        try:
            sg = SendGridAPIClient(os.getenv('SENDGRID_API_KEY'))
            cancel_email = Mail(
                from_email=os.getenv('FROM_EMAIL', 'support@simul-ai-tor.com'),
                to_emails=user.email,
                subject="Subscription Cancellation Confirmation",
                plain_text_content=f"Hello,\n\nYour subscription has been scheduled for cancellation at the end of your current billing period. You will retain access until that time.\n\nThank you. Please note: This is an automated email and replies to this address are not monitored."
            )
            sg.send(cancel_email)
        except Exception as e:
            flash(f"Error sending cancellation email: {str(e)}", "warning")
        flash("Your subscription will be cancelled at the end of the current billing period. Please note: This is an automated email and replies to this address are not monitored.", "success")
    except Exception as e:
        flash(f"Error cancelling subscription: {str(e)}", "danger")
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

        try:
            sg = SendGridAPIClient(os.getenv('SENDGRID_API_KEY'))
            confirmation_email = Mail(
                from_email=os.getenv('FROM_EMAIL', 'support@simul-ai-tor.com'),
                to_emails=current_user.email,
                subject="Subscription Reactivation Confirmation",
                plain_text_content=f"Hello,\n\nYour subscription has been reactivated. Enjoy using Simul-AI-tor.\n\nBest regards,\nThe Support Team. Please note: This is an automated email and replies to this address are not monitored."
            )
            sg.send(confirmation_email)
        except Exception as e:
            flash(f"Error sending reactivation email: {str(e)}", "warning")

        flash("Subscription reactivated successfully! A confirmation email has been sent.", "success")
    except Exception as e:
        flash(f"Error processing reactivation: {str(e)}", "danger")
    return redirect(url_for('account'))

# --- Landing Page Route ---
@app.route('/')
def landing():
    return render_template('landing.html')

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
            login_user(admin_user)
            flash("Logged in as admin.", "success")
            return redirect(url_for('simulation'))
        else:
            email = request.form['email']
            password = request.form['password']
            user = User.query.filter_by(email=email).first()
            if user and check_password_hash(user.password, password):
                session.pop('conversation', None)
                login_user(user)
                return redirect(url_for('simulation'))
            else:
                flash("Invalid email or password", "danger")
    return render_template('login.html')

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        email = request.form['email']
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

        price_id = STRIPE_STUDENT_PRICE_ID if category == 'health_student' else STRIPE_NONSTUDENT_PRICE_ID

        checkout_session = stripe.checkout.Session.create(
            payment_method_types=["card"],
            line_items=[{"price": price_id, "quantity": 1}],
            mode="subscription",
            allow_promotion_codes=True,
            success_url=url_for('payment_success', _external=True) + '?session_id={CHECKOUT_SESSION_ID}',
            cancel_url=url_for('payment_cancel', _external=True),
        )
        return redirect(checkout_session.url)
    return render_template('register.html')

@app.route('/payment_success')
def payment_success():
    session_id = request.args.get('session_id')
    if not session_id:
        flash("No session ID provided.", "warning")
        return redirect(url_for('register'))
    pending_registration = session.get('pending_registration')
    if not pending_registration:
        flash("No pending registration found. Please register again.", "danger")
        return redirect(url_for('register'))
    try:
        checkout_session = stripe.checkout.Session.retrieve(session_id)
        subscription_id = checkout_session.subscription
        if not subscription_id:
            flash("No subscription ID found in session.", "danger")
            return redirect(url_for('register'))
        subscription = stripe.Subscription.retrieve(subscription_id)
        stripe_customer_id = checkout_session.customer

        new_user = User(
            email=pending_registration["email"],
            password=pending_registration["hashed_password"],
            category=pending_registration["category"],
            discipline=pending_registration["discipline"],
            stripe_customer_id=stripe_customer_id,
            subscription_id=subscription_id,
            subscription_status=subscription.status
        )
        db.session.add(new_user)
        db.session.commit()

        try:
            sg = SendGridAPIClient(os.getenv('SENDGRID_API_KEY'))
            confirmation_email = Mail(
                from_email=os.getenv('FROM_EMAIL', 'support@simul-ai-tor.com'),
                to_emails=new_user.email,
                subject="Subscription Confirmation",
                plain_text_content=f"Hello,\n\nThank you for subscribing! Your subscription is now active. Enjoy using Simul-AI-tor.\n\nBest regards,\nThe Support Team"
            )
            sg.send(confirmation_email)
        except Exception as e:
            flash(f"Error sending confirmation email: {str(e)}", "warning")

        login_user(new_user)
        session.pop('pending_registration', None)
        flash("Payment successful! Your subscription is now active. A confirmation email has been sent.", "success")
    except Exception as e:
        flash(f"Error processing subscription: {str(e)}", "danger")
        return redirect(url_for('register'))
    return redirect(url_for('simulation'))

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
    return redirect(url_for('login'))

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
            email_content = f"""Dear User,

We received a request to reset your password. To reset your password, click the link below (this link is valid for 1 hour):

{reset_url}

If you did not request a password reset, please ignore this email.

Best regards,
Your Support Team
"""
            try:
                sg = SendGridAPIClient(os.getenv('SENDGRID_API_KEY'))
                message = Mail(
                    from_email=os.getenv('FROM_EMAIL', 'support@simul-ai-tor.com'),
                    to_emails=email,
                    subject="Password Reset Request",
                    plain_text_content=email_content
                )
                sg.send(message)
                flash("A password reset link has been sent to your email.", "info")
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

@app.route('/start_simulation', methods=['POST'])
@login_required
def start_simulation():
    level = request.form.get('simulation_level')
    country = request.form.get('country')
    if level not in ['Beginner', 'Intermediate', 'Advanced']:
        flash("Invalid simulation level selected", "danger")
        return redirect(url_for('simulation'))
    if not country:
        flash("Please select a country.", "danger")
        return redirect(url_for('simulation'))
    patient = random.choice(PATIENT_NAMES)
    if level == 'Beginner':
        selected_complaint = random.choice(FOUNDATION_COMPLAINTS)
    elif level == 'Intermediate':
        selected_complaint = random.choice(ENHANCED_COMPLAINTS)
    else:
        selected_complaint = random.choice(ADVANCED_COMPLAINTS)
    instr = (
        f"You are a patient in a history-taking simulation taking place in {country}. "
        f"Your level is {level}. "
        f"Your name is {patient['name']} and you are a {patient['gender']} patient. "
        f"Begin every interaction by saying exactly: \"Can I speak with someone about my symptoms?\" and wait for the user's response before providing further details. "
        f"Present your complaint: {selected_complaint}. "
        "Provide only minimal details until further questions are asked, then gradually add more information. "
        "IMPORTANT: Remember, you are the patient and never reveal that you are an AI."
    )
    session['country'] = country
    session['conversation'] = [{'role': 'system', 'content': instr}]
    try:
        response = openai.ChatCompletion.create(
            model="gpt-4-turbo",
            messages=session['conversation'],
            temperature=0.8
        )
        first_reply = response.choices[0].message["content"]
    except Exception as e:
        first_reply = f"Error with API: {str(e)}"
    session['conversation'].append({'role': 'assistant', 'content': first_reply})
    session.pop('feedback', None)
    session.pop('hint', None)
    return redirect(url_for('simulation'))

@app.route('/simulation', methods=['GET'])
@login_required
def simulation():
    conversation = session.get('conversation', [])
    display_conv = [m for m in conversation if m['role'] != 'system']
    return render_template('simulation.html', conversation=display_conv,
                           feedback_json=session.get('feedback_json'),
                           feedback_raw=session.get('feedback'),
                           hint=session.get('hint'))

@app.route('/send_message', methods=['POST'])
@login_required
def send_message():
    conversation = session.get('conversation', [])
    msg = request.form.get('message')
    if msg:
        conversation.append({'role': 'user', 'content': msg})
        session['conversation'] = conversation
        return jsonify({"status": "ok"}), 200
    return jsonify({"status": "error", "message": "No message provided"}), 400

@app.route('/get_reply', methods=['POST'])
@login_required
def get_reply():
    conversation = session.get('conversation', [])
    try:
        response = openai.ChatCompletion.create(
            model="gpt-3.5-turbo",
            messages=conversation,
            temperature=0.8
        )
        resp_text = response.choices[0].message["content"]
    except openai.error.OpenAIError as e:
        resp_text = f"OpenAI API Error: {str(e)}"
    except Exception as e:
        resp_text = f"Unexpected Error: {str(e)}"
    conversation.append({'role': 'assistant', 'content': resp_text})
    session['conversation'] = conversation
    return jsonify({"reply": resp_text}), 200

@app.route('/hint', methods=['POST'])
@login_required
def hint():
    conversation = session.get('conversation', [])
    if not conversation:
        flash("No conversation available for hint suggestions", "warning")
        return redirect(url_for('simulation'))
    conv_text = "\n".join([
        f"{'User' if m['role'] == 'user' else 'Patient'}: {m['content']}"
        for m in conversation if m['role'] != 'system'
    ])
    hint_text = PROMPT_INSTRUCTION + "\n" + conv_text
    hint_conversation = [{'role': 'system', 'content': hint_text}]
    try:
        response = openai.ChatCompletion.create(
            model="gpt-3.5-turbo",
            messages=hint_conversation,
            temperature=0.8
        )
        hint_response = response.choices[0].message["content"]
    except Exception as e:
        hint_response = f"Error with API: {str(e)}"
    session['hint'] = hint_response
    return redirect(url_for('simulation'))

@app.route('/feedback', methods=['POST'])
@login_required
def feedback():
    conversation = session.get('conversation', [])
    if not conversation:
        flash("No conversation available for feedback", "warning")
        return redirect(url_for('simulation'))

    user_conv_text = "\n".join([
        f"User: {m['content']}"
        for m in conversation if m.get('role') == 'user'
    ])

    feedback_prompt = (
        "Evaluate the following consultation transcript using the Calgary–Cambridge model. "
        "Score each of these categories on a scale of 1 to 10, and provide a short comment for each:\n"
        "1. Initiating the session\n"
        "2. Gathering information\n"
        "3. Physical examination (award points if the user obtains explicit consent and discusses the auto-generated exam findings)\n"
        "4. Explanation & planning\n"
        "5. Closing the session\n"
        "6. Building a relationship\n"
        "7. Providing structure\n\n"
        "Then, calculate the overall score by summing the scores for these seven categories (maximum score is 70). "
        "Finally, provide a separate brief commentary (max 50 words) on the user's clinical reasoning. Specifically, note if they used "
        "hypothetico-deductive reasoning effectively during the early stages of the consultation, and assess their use of Bayesian reasoning "
        "and dual-process theory throughout the interaction. Identify any potential biases, such as confirmation or anchoring bias. "
        "Include this commentary as a separate key called 'clinical_reasoning' in your JSON output.\n\n"
        "Format your answer strictly as JSON in the following format:\n\n"
        '{\n'
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
        "Consultation Transcript:\n" + user_conv_text
    )

    feedback_conversation = [{'role': 'system', 'content': feedback_prompt}]

    try:
        # Use GPT-4-turbo for a more nuanced evaluation
        response = openai.ChatCompletion.create(
            model="gpt-4-turbo",
            messages=feedback_conversation,
            temperature=0.8,
            max_tokens=300
        )
        fb = response.choices[0].message["content"]
    except Exception as e:
        fb = f"Error generating feedback: {str(e)}"

    try:
        feedback_json = json.loads(fb)
        session['feedback_json'] = feedback_json
        session['feedback'] = fb
    except Exception:
        session['feedback_json'] = None
        session['feedback'] = fb

    return redirect(url_for('simulation'))

@app.route('/download_feedback', methods=['GET'])
@login_required
def download_feedback():
    fb = session.get('feedback')
    if not fb:
        flash("No feedback available to download", "warning")
        return redirect(url_for('simulation'))
    pdf_buffer = io.BytesIO()
    doc = SimpleDocTemplate(pdf_buffer, pagesize=letter)
    styles = getSampleStyleSheet()
    story = [Paragraph(fb.replace('\n', '<br/>'), styles['Normal'])]
    doc.build(story)
    pdf_buffer.seek(0)
    return send_file(pdf_buffer,
                     as_attachment=True,
                     download_name="feedback.pdf",
                     mimetype="application/pdf")

@app.route('/clear_simulation')
@login_required
def clear_simulation():
    session.pop('conversation', None)
    session.pop('feedback', None)
    session.pop('feedback_json', None)
    session.pop('hint', None)
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

    exam_prompt = (
        f"Generate a complete and concise set of abbreviated physical examination findings for a patient presenting with '{complaint}'. "
        "Include abbreviated vital signs: HR (heart rate), BP (blood pressure), RR (respiratory rate), Temp (temperature), and O2 Sat (oxygen saturation). "
        "Also include abbreviated findings for head, neck, chest, abdomen, and extremities. Do not include any introductory phrases or extra text; provide only the exam findings."
    )
    try:
        response = openai.ChatCompletion.create(
            model="gpt-3.5-turbo",
            messages=[{"role": "system", "content": exam_prompt}],
            temperature=0.7,
            max_tokens=200
        )
        exam_results = response.choices[0].message["content"].strip()
    except Exception as e:
        exam_results = f"Error generating exam results: {str(e)}"
    return jsonify({"results": exam_results}), 200

if __name__ == '__main__':
    app.run(debug=True)
