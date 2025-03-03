import os
import io
import random
import time
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

# Additional imports for email and token generation
from flask_mail import Mail, Message
from itsdangerous import URLSafeTimedSerializer, SignatureExpired, BadSignature

# Load environment variables from .env file
load_dotenv()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
openai.api_key = OPENAI_API_KEY

# Configure Stripe
stripe.api_key = os.getenv("STRIPE_SECRET_KEY")
STRIPE_STUDENT_PRICE_ID = os.getenv("STRIPE_STUDENT_PRICE_ID")  # e.g., for £3.49/month
STRIPE_NONSTUDENT_PRICE_ID = os.getenv("STRIPE_NONSTUDENT_PRICE_ID")  # e.g., for £4.99/month

# Initialize Flask app and SQLAlchemy
app = Flask(__name__)
app.config['SESSION_PERMANENT'] = False
app.config['SESSION_TYPE'] = "filesystem"
app.config['SQLALCHEMY_DATABASE_URI'] = os.getenv("DATABASE_URL", "postgresql://postgres:London22!!@localhost/project_db")
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SECRET_KEY'] = os.getenv("SECRET_KEY", os.urandom(24).hex())

# Flask-Mail configuration
app.config['MAIL_SERVER'] = os.getenv("MAIL_SERVER", "smtp.gmail.com")
app.config['MAIL_PORT'] = int(os.getenv("MAIL_PORT", 587))
app.config['MAIL_USE_TLS'] = os.getenv("MAIL_USE_TLS", "True") == "True"
app.config['MAIL_USE_SSL'] = False
app.config['MAIL_USERNAME'] = os.getenv("MAIL_USERNAME")
app.config['MAIL_PASSWORD'] = os.getenv("MAIL_PASSWORD")
app.config['MAIL_DEFAULT_SENDER'] = os.getenv("MAIL_DEFAULT_SENDER", "noreply@example.com")

mail = Mail(app)
db = SQLAlchemy(app)

# Initialize Flask-Migrate and Flask-Login
migrate = Migrate(app, db)
login_manager = LoginManager(app)
login_manager.login_view = 'login'

# Initialize serializer for password reset tokens
s = URLSafeTimedSerializer(app.config['SECRET_KEY'])

# --- User Model Definition ---
class User(UserMixin, db.Model):
    __tablename__ = 'user'  # Explicit table name
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(150), unique=True, nullable=False)
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
    "chest pain", "shortness of breath", "persistent fatigue", "abdominal pain", "chronic diarrhea"
]
ADVANCED_COMPLAINTS = [
    "severe chest pain", "acute shortness of breath", "chronic fatigue", "irregular heart palpitations",
    "intense abdominal pain"
]

FEEDBACK_INSTRUCTION = (
    "You are an examiner, not a patient. Cease all patient role-playing immediately. Your task is to analyse the conversation history and provide detailed feedback "
    "on the user's communication and history-taking skills using the Calgary-Cambridge model. Also evaluate their clinical reasoning using hypothetical-deductive reasoning, "
    "dual-process theory, and Bayesian theory, noting any biases. Use specific examples from the dialogue provided. Keep feedback constructive, clear, and professional. "
    "Use British English spellings. End with: \"Thank you for the consultation. Goodbye.\" Here is the conversation history to review:"
)

PROMPT_INSTRUCTION = (
    "You are a Calgary Cambridge communication expert. Based on the conversation history below, provide one single, concise suggested next question that the user should ask "
    "to advance the history-taking interview. Your suggestion must ensure that essential patient details—such as demographics, personal history, or key symptoms—are addressed. "
    "If the conversation does not include any questions asking for the patient's name and/or date of birth, your suggestion should explicitly include a question to gather them. "
    "Include a brief justification (1-2 sentences) for your suggestion. Format your answer as a single bullet point.\nConversation history:"
)

# --- Account Blueprint ---
account_bp = Blueprint('account', __name__, url_prefix='/account')
@account_bp.route('/')
@login_required
def account():
    return render_template('account.html')
app.register_blueprint(account_bp)

# --- Subscription Cancellation Route ---
@app.route('/cancel_subscription', methods=['POST'])
@login_required
def cancel_subscription():
    user = current_user
    if not user.subscription_id:
        flash("No active subscription to cancel.", "warning")
        return redirect(url_for('account.account'))
    try:
        subscription = stripe.Subscription.modify(
            user.subscription_id,
            cancel_at_period_end=True
        )
        user.subscription_status = subscription.status
        db.session.commit()
        flash("Your subscription will be cancelled at the end of the current billing period.", "success")
    except Exception as e:
        flash(f"Error cancelling subscription: {str(e)}", "danger")
    return redirect(url_for('account.account'))

# --- Before Request Hook to Enforce Active Subscription ---
@app.before_request
def require_active_subscription():
    safe_endpoints = {
        'login',
        'register',
        'forgot_password',
        'reset_password',
        'logout',
        'payment_success',
        'payment_cancel',
        'static',
        'landing'
    }
    if current_user.is_authenticated and not current_user.is_admin:
        if current_user.subscription_status != 'active' and request.endpoint:
            if request.endpoint not in safe_endpoints:
                flash("Your subscription is not active. Please register to subscribe.", "warning")
                return redirect(url_for('register'))

# --- Landing Page Route ---
@app.route('/')
def landing():
    return render_template('landing.html')

# --- Login & Registration Routes ---
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
            admin_user = User.query.filter_by(username="admin").first()
            if not admin_user:
                hashed_admin = generate_password_hash(ADMIN_LOGIN_PASSWORD)
                admin_user = User(username="admin", email="admin@example.com",
                                  password=hashed_admin, is_admin=True)
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
            username = request.form['username']
            password = request.form['password']
            user = User.query.filter_by(username=username).first()
            if user and check_password_hash(user.password, password):
                session.pop('conversation', None)
                login_user(user)
                return redirect(url_for('simulation'))
            else:
                flash("Invalid username or password", "danger")
    return render_template('login.html')

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        username = request.form['username']
        email = request.form['email']
        password = request.form['password']
        category = request.form['category']
        discipline = request.form.get('discipline')
        other_discipline = request.form.get('otherDiscipline')
        existing_email = User.query.filter_by(email=email).first()
        if existing_email:
            flash("An account with this email already exists. Please use a different email.", "danger")
            return redirect(url_for('register'))
        if discipline == 'other' and other_discipline:
            discipline = other_discipline
        if category == 'health_student' and not email.lower().endswith('.ac.uk'):
            flash("For student registration, please use a valid academic email (ending with .ac.uk).", "danger")
            return redirect(url_for('register'))
        existing_user = User.query.filter_by(username=username).first()
        if existing_user:
            flash("Username already exists. Please choose another one.", "danger")
            return redirect(url_for('register'))
        STRIPE_STUDENT_LINK = "https://buy.stripe.com/8wMfZf8Tvd5idVueUV"
        STRIPE_NONSTUDENT_LINK = "https://buy.stripe.com/28obIZ5Hj2qE9FedQS"
        stripe_checkout_url = STRIPE_STUDENT_LINK if category == 'health_student' else STRIPE_NONSTUDENT_LINK
        hashed_password = generate_password_hash(password)
        new_user = User(username=username, email=email, password=hashed_password,
                        category=category, discipline=discipline)
        db.session.add(new_user)
        db.session.commit()
        try:
            customer = stripe.Customer.create(email=email)
            new_user.stripe_customer_id = customer.id
            db.session.commit()
        except Exception as e:
            flash(f"Error creating Stripe customer: {str(e)}", "danger")
            return redirect(url_for('register'))
        return redirect(stripe_checkout_url)
    return render_template('register.html')

# --- Payment Routes ---
@app.route('/payment_success')
@login_required
def payment_success():
    session_id = request.args.get('session_id')
    if not session_id:
        flash("No session ID provided.", "warning")
        return redirect(url_for('register'))
    try:
        checkout_session = stripe.checkout.Session.retrieve(session_id)
        subscription_id = checkout_session.subscription
        if not subscription_id:
            flash("No subscription ID found in session.", "danger")
            return redirect(url_for('register'))
        subscription = stripe.Subscription.retrieve(subscription_id)
        stripe_customer_id = checkout_session.customer
        user = User.query.filter_by(stripe_customer_id=stripe_customer_id).first()
        if not user:
            flash("User not found for this transaction. Please contact support.", "danger")
            return redirect(url_for('register'))
        user.subscription_id = subscription_id
        user.subscription_status = subscription.status
        db.session.commit()
        flash("Payment successful! Your subscription is now active.", "success")
    except Exception as e:
        flash(f"Error processing subscription: {str(e)}", "danger")
        return redirect(url_for('register'))
    return redirect(url_for('simulation'))

@app.route('/payment_cancel')
def payment_cancel():
    flash("Payment was cancelled. Please try again.", "warning")
    return redirect(url_for('register'))

# --- Logout & About Routes ---
@app.route('/logout')
@login_required
def logout():
    session.pop('conversation', None)
    logout_user()
    return redirect(url_for('login'))

@app.route('/about')
@login_required
def about():
    return render_template('about.html')

# --- Forgot Password & Reset Password Routes ---
@app.route('/forgot_password', methods=['GET', 'POST'])
def forgot_password():
    if request.method == 'POST':
        email = request.form.get('email')
        user = User.query.filter_by(email=email).first()
        if user:
            token = s.dumps(email, salt='password-reset-salt')
            reset_url = url_for('reset_password', token=token, _external=True)
            msg = Message("Password Reset Request", recipients=[email])
            msg.body = f"""Dear {user.username},

We received a request to reset your password. To reset your password, click the link below (this link is valid for 1 hour):

{reset_url}

If you did not request a password reset, please ignore this email.

Best regards,
Your Support Team
"""
            try:
                mail.send(msg)
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

# --- Simulation & Chat Routes ---
@app.route('/start_simulation', methods=['POST'])
@login_required
def start_simulation():
    level = request.form.get('simulation_level')
    country = request.form.get('country')
    # Expecting "Beginner", "Intermediate", or "Advanced"
    if level not in ['Beginner', 'Intermediate', 'Advanced']:
        flash("Invalid simulation level selected", "danger")
        return redirect(url_for('simulation'))
    if not country:
        flash("Please select a country.", "danger")
        return redirect(url_for('simulation'))
    patient = random.choice(PATIENT_NAMES)
    # Map levels to complaint sets
    if level == 'Beginner':
        selected_complaint = random.choice(FOUNDATION_COMPLAINTS)
    elif level == 'Intermediate':
        selected_complaint = random.choice(ENHANCED_COMPLAINTS)
    else:  # Advanced
        selected_complaint = random.choice(ADVANCED_COMPLAINTS)
    # Build simulation instructions with country and level details
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
                           feedback=session.get('feedback'),
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
            model="gpt-4-turbo",
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
            model="gpt-4-turbo",
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
    conv_text = "\n".join([
        f"{'User' if m['role'] == 'user' else 'Patient'}: {m['content']}"
        for m in conversation if m['role'] != 'system'
    ])
    feedback_prompt = FEEDBACK_INSTRUCTION + "\n" + conv_text
    feedback_conversation = [{'role': 'system', 'content': feedback_prompt}]
    try:
        res = openai.ChatCompletion.create(
            model="gpt-4-turbo",
            messages=feedback_conversation,
            temperature=0.8
        )
        fb = res.choices[0].message["content"]
    except Exception as e:
        fb = f"Error generating feedback: {str(e)}"
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
    session.pop('hint', None)
    return redirect(url_for('simulation'))

# --- New Generate Exam Route (Updated) ---
@app.route('/generate_exam', methods=['POST'])
@login_required
def generate_exam():
    # Check if at least two user messages exist
    conversation = session.get('conversation', [])
    user_messages = [msg for msg in conversation if msg.get('role') == 'user']
    if len(user_messages) < 2:
        return jsonify({"error": "Please ask at least two questions before accessing exam results."}), 403

    data = request.get_json()
    complaint = data.get('complaint')
    if not complaint:
        return jsonify({"error": "No complaint provided"}), 400

    exam_prompt = (
        f"Generate a complete and concise set of physical examination findings for a patient presenting with '{complaint}'. "
        "Include the following sections: General Appearance, Vital Signs (heart rate, blood pressure, respiratory rate, temperature, oxygen saturation), "
        "and relevant findings for head, neck, chest, abdomen, and extremities. Do not include any introductory phrases or extra text; provide only the exam findings."
    )
    try:
        response = openai.ChatCompletion.create(
            model="gpt-4-turbo",
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
