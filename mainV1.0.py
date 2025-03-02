import os
import io
import random
import time
from flask import Flask, render_template, redirect, url_for, request, flash, session, send_file, jsonify
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
db = SQLAlchemy(app)


# Initialize Flask-Migrate and Flask-Login
migrate = Migrate(app, db)
login_manager = LoginManager(app)
login_manager.login_view = 'login'

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
    is_admin = db.Column(db.Boolean, default=False)  # Admin flag

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

# --- Routes ---
@app.route('/')
def index():
    return redirect(url_for('login'))


import os
from flask import render_template, redirect, url_for, request, flash, session
from flask_login import login_user
from werkzeug.security import check_password_hash

# Ensure you've loaded your .env file before this route is defined.
ADMIN_LOGIN_PASSWORD = os.getenv("ADMIN_LOGIN_PASSWORD")

import os
from flask import render_template, redirect, url_for, request, flash, session
from flask_login import login_user
from werkzeug.security import check_password_hash, generate_password_hash

# Ensure environment variables are loaded (e.g., via load_dotenv())
ADMIN_LOGIN_PASSWORD = os.getenv("ADMIN_LOGIN_PASSWORD")

import os
from flask import render_template, redirect, url_for, request, flash, session
from flask_login import login_user
from werkzeug.security import check_password_hash, generate_password_hash

# Ensure environment variables are loaded (e.g., via load_dotenv())
ADMIN_LOGIN_PASSWORD = os.getenv("ADMIN_LOGIN_PASSWORD")


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        # Check if the admin checkbox is checked
        admin_flag = request.form.get('admin')  # will be "true" if checked

        if admin_flag == "true":
            # Admin login flow: only the admin password is used
            admin_password = request.form.get('admin_password')
            if not admin_password or admin_password != ADMIN_LOGIN_PASSWORD:
                flash("Invalid admin password.", "danger")
                return redirect(url_for('login'))

            # Look for a default admin account (username "admin")
            admin_user = User.query.filter_by(username="admin").first()
            if not admin_user:
                # Create a default admin account if one doesn't exist
                hashed_admin = generate_password_hash(ADMIN_LOGIN_PASSWORD)
                admin_user = User(username="admin", email="admin@example.com",
                                  password=hashed_admin, is_admin=True)
                db.session.add(admin_user)
                db.session.commit()
            else:
                # Ensure the user is marked as admin
                if not admin_user.is_admin:
                    admin_user.is_admin = True
                    db.session.commit()

            login_user(admin_user)
            flash("Logged in as admin.", "success")
            return redirect(url_for('simulation'))
        else:
            # Normal login flow using username and password
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
        # Retrieve form data
        username = request.form['username']
        email = request.form['email']
        password = request.form['password']
        category = request.form['category']
        discipline = request.form.get('discipline')
        other_discipline = request.form.get('otherDiscipline')

        # Check if email already exists
        existing_email = User.query.filter_by(email=email).first()
        if existing_email:
            flash("An account with this email already exists. Please use a different email.", "danger")
            return redirect(url_for('register'))

        # Use "other" discipline if provided
        if discipline == 'other' and other_discipline:
            discipline = other_discipline

        # Verify student email if category is health_student
        if category == 'health_student' and not email.lower().endswith('.ac.uk'):
            flash("For student registration, please use a valid academic email (ending with .ac.uk).", "danger")
            return redirect(url_for('register'))

        # Before creating a new user, check if the username exists
        existing_user = User.query.filter_by(username=username).first()
        if existing_user:
            flash("Username already exists. Please choose another one.", "danger")
            return redirect(url_for('register'))

        # Determine the correct Stripe checkout URL based on category
        STRIPE_STUDENT_LINK = "https://buy.stripe.com/8wMfZf8Tvd5idVueUV"
        STRIPE_NONSTUDENT_LINK = "https://buy.stripe.com/28obIZ5Hj2qE9FedQS"

        stripe_checkout_url = STRIPE_STUDENT_LINK if category == 'health_student' else STRIPE_NONSTUDENT_LINK

        # Create new user record in the database
        hashed_password = generate_password_hash(password)
        new_user = User(username=username, email=email, password=hashed_password,
                        category=category, discipline=discipline)
        db.session.add(new_user)
        db.session.commit()

        # Create a Stripe customer for non-admin users
        try:
            customer = stripe.Customer.create(email=email)
            new_user.stripe_customer_id = customer.id
            db.session.commit()
        except Exception as e:
            flash(f"Error creating Stripe customer: {str(e)}", "danger")
            return redirect(url_for('register'))

        # Redirect the user to the appropriate Stripe checkout page
        return redirect(stripe_checkout_url)

    return render_template('register.html')


@app.route('/payment_success')
@login_required
def payment_success():
    session_id = request.args.get('session_id')

    if not session_id:
        flash("No session ID provided.", "warning")
        return redirect(url_for('register'))

    try:
        # Retrieve the checkout session from Stripe
        checkout_session = stripe.checkout.Session.retrieve(session_id)

        # Ensure we have a subscription ID
        subscription_id = checkout_session.subscription
        if not subscription_id:
            flash("No subscription ID found in session.", "danger")
            return redirect(url_for('register'))

        # Retrieve subscription details
        subscription = stripe.Subscription.retrieve(subscription_id)

        # Extract Stripe customer ID from session
        stripe_customer_id = checkout_session.customer

        # Find the user associated with this Stripe customer ID
        user = User.query.filter_by(stripe_customer_id=stripe_customer_id).first()

        if not user:
            flash("User not found for this transaction. Please contact support.", "danger")
            return redirect(url_for('register'))

        # Update the user's subscription details
        user.subscription_id = subscription_id
        user.subscription_status = subscription.status  # typically 'active'

        # Commit changes to the database
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

@app.route('/start_simulation', methods=['POST'])
@login_required
def start_simulation():
    level = request.form.get('simulation_level')
    if level not in ['Foundation', 'Enhanced', 'Advanced']:
        flash("Invalid simulation level selected", "danger")
        return redirect(url_for('simulation'))
    patient = random.choice(PATIENT_NAMES)
    if level == 'Foundation':
        selected_complaint = random.choice(FOUNDATION_COMPLAINTS)
    elif level == 'Enhanced':
        selected_complaint = random.choice(ENHANCED_COMPLAINTS)
    else:
        selected_complaint = random.choice(ADVANCED_COMPLAINTS)
    instr = (
        f"You are a patient in a history-taking simulation. Your name is {patient['name']} and you are a {patient['gender']} patient. "
        f"Begin every interaction by saying exactly: \"Can I speak with someone about my symptoms?\" and wait for the user's response before providing further details. "
        f"Present your complaint: {selected_complaint}. "
        "Provide only minimal details until further questions are asked, then gradually add more information. "
        "IMPORTANT: Remember, you are the patient and never reveal that you are an AI."
    )
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

if __name__ == '__main__':
    app.run(debug=True)
