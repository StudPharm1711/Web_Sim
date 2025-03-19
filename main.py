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
import boto3  # AWS SES via boto3
from flask_session import Session

# Load environment variables from .env file
load_dotenv()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
openai.api_key = OPENAI_API_KEY

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

# --- User Model Definition ---
class User(UserMixin, db.Model):
    __tablename__ = 'subscribers'  # Explicit table name
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(150), unique=True, nullable=False)
    password = db.Column(db.String(255), nullable=False)
    category = db.Column(db.String(50))
    discipline = db.Column(db.String(100))
    stripe_customer_id = db.Column(db.String(100))
    subscription_id = db.Column(db.String(100))
    subscription_status = db.Column(db.String(50))
    # New fields to track prompt (input) and completion (output) token usage separately per model
    token_prompt_usage_gpt35 = db.Column(db.Integer, default=0)
    token_completion_usage_gpt35 = db.Column(db.Integer, default=0)
    token_prompt_usage_gpt4 = db.Column(db.Integer, default=0)
    token_completion_usage_gpt4 = db.Column(db.Integer, default=0)
    is_admin = db.Column(db.Boolean, default=False)
    current_session = db.Column(db.String(255), nullable=True)  # To track the current session token

# --- Feedback Model Definition ---
class Feedback(db.Model):
    __tablename__ = 'feedback'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('subscribers.id'), nullable=False)
    score = db.Column(db.Float, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    # Relationship back to the User
    user = db.relationship('User', backref=db.backref('feedbacks', lazy=True))

# --- Pending Registration Model ---
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

# --- Alert Signup Model ---
class AlertSignup(db.Model):
    __tablename__ = 'alert_signup'
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(150), unique=True, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

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
    {"name": "Fatima Ali", "ethnicity": "Middle Eastern (Arabic)", "gender": "female", "age": 68},
    {"name": "Carlos Rivera", "ethnicity": "Hispanic (Mexican)", "gender": "male", "age": 50},
    {"name": "Nia Okoye", "ethnicity": "African (Nigerian)", "gender": "female", "age": 59},
    {"name": "Sofia Nguyen", "ethnicity": "Southeast Asian (Vietnamese)", "gender": "female", "age": 34},
    {"name": "Mohamed Hassan", "ethnicity": "African (Somali)", "gender": "male", "age": 72},
    {"name": "Emily Johnson", "ethnicity": "American (White)", "gender": "female", "age": 37},
    {"name": "Marcus Brown", "ethnicity": "African American", "gender": "male", "age": 61},
    {"name": "Isabella Garcia", "ethnicity": "Hispanic (Latina)", "gender": "female", "age": 29},
    {"name": "Liam O'Connor", "ethnicity": "Irish (White)", "gender": "male", "age": 47},
    {"name": "Chloe Martin", "ethnicity": "French (White)", "gender": "female", "age": 33},
    {"name": "Noah Kim", "ethnicity": "Korean", "gender": "male", "age": 25},
    {"name": "Zara Ahmed", "ethnicity": "South Asian (Pakistani)", "gender": "female", "age": 62},
    {"name": "Ethan Davis", "ethnicity": "American (White)", "gender": "male", "age": 40},
    {"name": "Grace Lee", "ethnicity": "East Asian (Korean)", "gender": "female", "age": 28},
    {"name": "Alexander Müller", "ethnicity": "German (White)", "gender": "male", "age": 66},
    {"name": "Sophia Rossi", "ethnicity": "Italian", "gender": "female", "age": 31},
    {"name": "David Wilson", "ethnicity": "British (White)", "gender": "male", "age": 54},
    {"name": "Layla Hassan", "ethnicity": "Middle Eastern (Arabic)", "gender": "female", "age": 58},
    {"name": "Oliver Chen", "ethnicity": "East Asian (Chinese)", "gender": "male", "age": 36},
    {"name": "Amelia Patel", "ethnicity": "South Asian (Indian)", "gender": "female", "age": 45},
    {"name": "Benjamin Carter", "ethnicity": "American (White)", "gender": "male", "age": 52}
]

# Unified dictionary for system-level complaints (all complaints in one list per system)
SYSTEM_COMPLAINTS = {
    "common ailments": [
        "I've had a dry cough for a week.",
        "My heart skips a beat every now and then.",
        "My skin is so dry.",
        "There's a small lump near my anus.",
        "I get a little out of breath when I go up the stairs.",
        "Sometimes I feel more tired than usual.",
        "I have an itchy feeling between my toes.",
        "My shoulder hurts if I lift things.",
        "My ankles get a little swollen after a long day.",
        "Occasionally, my heartbeat feels a bit uneven.",
        "I've noticed some scaling on my scalp.",
        "Sometimes, my heart feels like it’s racing or skipping beats.",
        "My throat is really sore.",
        "I'm losing my voice.",
        "My inner thigh is red and itchy, and it sometimes feels like there's a burning sensation.",
        "My feet feel damp and itchy after wearing certain shoes, and I'm concerned that it might be an infection.",
        "My heart beats strangely more often than it used to.",
        "My eyes are itchy and watering.",
        "Sometimes, my arm aches along with the chest pain.",
        "My heartbeat has become more noticeable lately.",
        "I've noticed white patches on my tongue.",
        "I have this round patch on my hand",
        "I sometimes get so dizzy and weak that I nearly pass out.",
        "It hurst when I go to the toilet to pee. I'm sorry this is quite embarassing for me",
        "My privates are itching.",
        "My heart keeps beating in a strange way, and it makes me nervous.",
        "My left ear hurts.",
        "I can't hear out of my right ear very well.",
        "My left eye is full of discharge.",
        "Something isn't right. I suddenly feel like I'm having a major heart problem."
    ],
    "ENT": [
        "I've been experiencing a persistent sore throat that feels raw and scratchy.",
        "My throat has been irritated and it's painful to swallow.",
        "There's a constant tickle in my throat that makes me cough frequently.",
        "My voice has become hoarse over the past few days without any clear cause.",
        "I keep getting a dry cough that seems to start in my throat.",
        "I feel as though my ears are blocked and I'm not hearing as clearly as usual.",
        "Occasionally, I hear a ringing sound in my ears that doesn't stop.",
        "My ears feel congested and it's hard to focus on sounds.",
        "I experience a mild ear pain that worsens when I tilt my head.",
        "Sometimes, I get a bit dizzy along with a buzzing in my ears.",
        "My nose is constantly blocked and I struggle to breathe through it.",
        "I've been sneezing frequently with a runny nose that won't subside.",
        "My nasal passages feel inflamed and irritated, almost like I'm having an allergy attack.",
        "When I bend over, I feel a sharp pain in my sinuses.",
        "There's pressure around my cheeks that seems related to my sinuses.",
        "My face feels tender in the sinus areas, particularly near my nose.",
        "I've noticed a clear discharge from my nose that's making me worried.",
        "I experience a bad taste in my mouth from post-nasal drip.",
        "I find myself clearing my throat constantly due to irritation.",
        "My tonsils feel slightly swollen and sore, especially in the mornings.",
        "I have some difficulty swallowing because my throat feels unusually tight.",
        "There's a recurring, sharp pain in one of my ears that comes and goes.",
        "I sometimes feel a popping sensation in my ears as if the pressure is shifting.",
        "My ears occasionally buzz or ring intermittently throughout the day.",
        "I have a mild headache focused around my forehead and sinuses.",
        "My nose feels persistently dry and cracked, which is quite uncomfortable.",
        "After eating, I sometimes feel a burning sensation in my throat.",
        "I have a scratchy throat that makes speaking for long periods difficult.",
        "My ears are overly sensitive to sound, which is making me uneasy.",
        "I experience occasional dizziness that seems to be related to my inner ear."
    ],
    "cardiovascular": [
        "I have a tickly cough that won’t go away.",
        "My throat feels scratchy, and it hurts a little when I swallow.",
        "I keep getting headaches, especially in the afternoon.",
        "My nose is always blocked, even when I don’t have a cold.",
        "I feel really bunged up, and my sinuses ache.",
        "I keep sneezing, and my eyes are itchy.",
        "My ears feel blocked, and I can’t hear properly.",
        "I have a sore in my mouth that won’t heal.",
        "There’s a weird taste in my mouth all the time.",
        "My stomach feels bloated after I eat.",
        "I’ve been getting mild heartburn after meals.",
        "I feel a bit nauseous on and off, but I haven’t been sick.",
        "I have occasional diarrhoea, but I feel fine otherwise.",
        "My stools are harder than usual, and I struggle to go.",
        "I’ve had a dull ache in my lower back for a few days.",
        "My joints feel a bit stiff and achy in the morning.",
        "My skin is really dry and itchy, especially on my hands.",
        "I’ve got this rash that won’t go away.",
        "My eyes feel dry and irritated all the time.",
        "I keep getting a little dizziness when I stand up.",
        "I feel tired all the time, even when I get a good night’s sleep.",
        "I’ve been feeling a bit more anxious than usual.",
        "My lips are really chapped, no matter what I do.",
        "I get pins and needles in my hands every now and then.",
        "I’ve got a patch of flaky skin on my scalp that’s really itchy.",
        "My gums bleed a little when I brush my teeth.",
        "I’ve had a mild sore throat for a few days, but no fever.",
        "My feet feel cold all the time, even when I wear socks.",
        "I wake up with a dry mouth, even though I drink plenty of water.",
        "My hair seems to be falling out more than usual."
    ],
    "respiratory": [
        "mild cough",
        "occasional wheezing",
        "minor shortness of breath",
        "light chest tightness",
        "intermittent throat clearing",
        "slight nasal congestion",
        "mild breathing discomfort",
        "occasional mild sputum production",
        "light shortness of breath on exertion",
        "minor episodes of mild cough",
        "I have a cough that keeps coming back with some mucus",
        "I often hear a wheezing sound when I breathe",
        "I get short of breath during everyday activities",
        "My chest feels tight when I’m active",
        "I sometimes have a little trouble catching my breath when moving",
        "I feel noticeably short of breath even when resting",
        "I produce more mucus than usual",
        "Sometimes I feel wheezy and a bit out of breath",
        "I have a constant mild difficulty breathing",
        "There are times when I feel some discomfort when breathing",
        "I suddenly can’t seem to get enough air, and my chest feels tight.",
        "I sound really wheezy, like I’m breathing through a straw.",
        "I can’t stop coughing, and sometimes I see a little blood.",
        "When I breathe, it makes a strange noise, like a whistle.",
        "Even when I’m resting, I feel like I’m running out of breath.",
        "My breathing is so fast and shallow that I feel exhausted.",
        "I wake up gasping for air, and it’s really scary.",
        "I have to sit up to breathe properly, lying down makes it worse.",
        "My chest feels locked up, like no air is getting in.",
        "It’s like I have to fight for every breath."
    ],
    "gastrointestinal": [
        "mild abdominal discomfort",
        "occasional indigestion",
        "a bit of bloating",
        "nausea thats on and off",
        "occasional heartburn",
        "mild stomach cramping",
        "slight constipation",
        "mild diarrhoea",
        "intermittent discomfort after eating",
        "minor stomach upset",
        "I have ongoing stomach pain",
        "I often feel bloated and experience indigestion",
        "I get regular heartburn that sometimes makes me bring food back up",
        "I occasionally throw up after meals",
        "I suffer from constant stomach cramps and discomfort",
        "I often feel nauseous, and sometimes it leads to vomiting",
        "I have more frequent bouts of loose stools",
        "I sometimes experience sharp stomach pain",
        "I get moderate constipation that is quite uncomfortable",
        "I constantly feel bloated with some pain",
        "My stomach hurts so badly that I have to curl up.",
        "I’ve been throwing up, and nothing stays down.",
        "I can’t stop running to the toilet, and I feel really weak.",
        "The pain in my belly is so sharp it takes my breath away.",
        "I keep getting heartburn, and I’m losing weight without trying.",
        "My upper stomach feels like it’s on fire.",
        "I feel sick all the time, and I keep throwing up.",
        "My belly hurts if I touch it, like something is really wrong inside.",
        "I noticed blood in the toilet, and I’m freaking out.",
        "The pain is unbearable, I think something inside me is torn."
    ],
    "neurological": [
        "mild headache",
        "occasional dizziness",
        "slight tension headache",
        "brief episodes of light headedness",
        "minor scalp tenderness",
        "occasional blurred vision",
        "light difficulty concentrating",
        "minor sensory tingling",
        "mild episodes of fatigue",
        "occasional slight disorientation",
        "I have a constant headache that sometimes makes me feel a little nauseous",
        "I often feel dizzy and occasionally lose my balance",
        "I get regular tension headaches",
        "There are times when my vision gets a bit blurry",
        "Sometimes I feel as if the room is spinning",
        "I often feel a mild numbness in my arms or legs",
        "I occasionally forget things",
        "I have mild migraine episodes that keep coming back",
        "My headache sometimes comes with blurred vision",
        "I notice some changes in how I feel sensations in my arms or legs",
        "I have the worst headache of my life, and my vision gets blurry.",
        "Sometimes, I forget what I was saying mid-sentence.",
        "I feel dizzy all the time, like I might fall over.",
        "I get these splitting headaches that make me want to hide in a dark room.",
        "I woke up feeling confused, and I couldn’t remember where I was.",
        "Half my face feels numb, and I don’t know why.",
        "I keep tripping over things, like my legs aren’t working properly.",
        "I tried to say something, but my words came out all wrong.",
        "I keep getting these weird, brief spells where I feel out of it.",
        "It feels like I have no control over my body sometimes."
    ],
    "musculoskeletal": [
        "mild lower back pain",
        "occasional joint stiffness",
        "minor muscle ache",
        "light shoulder discomfort",
        "minor knee pain",
        "slight neck stiffness",
        "occasional muscle soreness in arms and legs",
        "pain in my right wrist",
        "my body aches all over",
        "mild ankle pain",
        "I experience ongoing lower back pain that feels stiff",
        "I notice joint pain when I move",
        "I have frequent muscle aches and occasional cramps",
        "Sometimes I feel a sharp pain in my shoulder",
        "I have constant discomfort in my knee when I'm active",
        "My neck pain limits my movement sometimes",
        "I often get recurring wrist pain when I use my hand",
        "I feel moderate pain in my elbow when moving it",
        "I have persistent hip pain when I walk",
        "My muscles often feel sore and stiff",
        "My back pain is so bad that I can’t stand up straight.",
        "My joints are swollen and hurt so much that I don’t want to move.",
        "I keep getting these horrible muscle spasms that make me jolt.",
        "I can’t lift my arm because my shoulder hurts so much.",
        "My knee locks up randomly, and I feel like I’m going to fall.",
        "My neck pain is making my arms feel weird and tingly.",
        "My wrist is so painful that I can’t even hold a cup.",
        "I have this deep, stabbing pain in my hip when I walk.",
        "I think I broke something, the pain is unbearable.",
        "My whole body aches, and I feel weak all over."
    ],
    "genitourinary": [
        "mild bladder discomfort",
        "I feel like I need to go pee constantly",
        "a bit of tummy discomfort",
        "slight dysuria (painful urination)",
        "cloudy urine",
        "mild urgency",
        "minor pressure during urination",
        "slight increase in frequency",
        "intermittent burning sensation when I pee",
        "mild discomfort during urination",
        "I feel a constant burning sensation when I pee",
        "I feel like I need to pee very often and urgently",
        "Sometimes my lower belly feels uncomfortable",
        "I have persistent pain in my lower abdomen when I pee",
        "I feel a moderate pressure in my bladder",
        "I often get uncomfortable urges to pee",
        "I experience moderate pain when I urinate",
        "Sometimes I notice a little blood in my urine",
        "I feel constantly urgent when I need to pee",
        "I experience moderate discomfort in my pelvic area",
        "It burns so bad when I pee that I’m afraid to go.",
        "I have unbearable pain in my lower stomach that won’t go away.",
        "I feel like I can’t empty my bladder, and it really hurts.",
        "I saw blood in my pee, and now I’m really worried.",
        "My lower stomach pain is so bad that I feel sick.",
        "I need to pee all the time, and it stings like crazy.",
        "It feels like there’s something blocking my urine from coming out.",
        "I have this horrible pain in my side, like a knife stabbing me.",
        "My bladder feels like it’s on fire.",
        "I feel like I have a really bad infection that’s getting worse."
    ],
    "endocrine": [
        "mild fatigue",
        "I've put on weight recently",
        "my appetite seems up and down",
        "I'm constantly thirsty",
        "mild hair thinning",
        "occasional dry skin",
        "some muscle weakness",
        "mood is up and down",
        "minor sleep disturbances",
        "feel cold all the time",
        "I feel constantly tired and sometimes lose a little weight",
        "I notice I don’t feel as hungry, which sometimes affects my weight",
        "There are times I feel too hot and very tired",
        "My skin is dry and I’m losing some hair",
        "I feel moderately weak and my joints sometimes ache",
        "I experience frequent mood swings and have trouble sleeping",
        "I’m always very thirsty and end up needing to pee more often",
        "Sometimes I feel very cold and tired",
        "I notice I look paler when I’m tired",
        "I often feel sluggish and my weight keeps changing",
        "I feel completely drained all the time, and I keep losing weight.",
        "I barely eat, but I’m still losing a lot of weight fast.",
        "I can’t handle heat at all, it makes me feel weak and shaky.",
        "My muscles feel so weak that even standing is hard.",
        "I’ve lost so much hair, and my skin is dry and flaky.",
        "I drink so much water, but I’m always thirsty.",
        "I feel freezing cold even when the heating is on.",
        "Something isn’t right. My body just feels off in every way.",
        "I have such bad mood swings that I feel like a different person.",
        "I think my whole system is out of balance."
    ],
    "dermatological": [
        "mild skin rash",
        "occasional itching",
        "minor dry skin patches",
        "slight redness on the skin",
        "small areas of mild irritation",
        "occasional mild eczema flare-up",
        "minor skin dryness",
        "slight scaling of the skin",
        "occasional mild hives",
        "light skin irritation",
        "I have a rash that keeps coming back with a moderate itch",
        "My skin is red and sometimes has small bumps",
        "I often experience eczema flare-ups with dry skin",
        "My skin feels irritated and a bit flaky",
        "I have several areas on my skin that are very itchy",
        "I get moderate inflammation on my skin that turns red",
        "I sometimes develop hives that are uncomfortable",
        "I have a rash that sometimes even forms small blisters",
        "My skin stays red and irritated for long periods",
        "I experience symptoms similar to mild psoriasis",
        "I feel completely drained all the time, and I keep losing weight fast.",
        "I barely eat, but I’m still losing a lot of weight fast.",
        "I can’t handle heat at all, it makes me feel weak and shaky.",
        "My muscles feel so weak that even standing is hard.",
        "I’ve lost so much hair, and my skin is dry and flaky.",
        "I drink so much water, but I’m always thirsty.",
        "I feel freezing cold even when the heating is on.",
        "Something isn’t right. My body just feels off in every way.",
        "I have such bad mood swings that I feel like a different person.",
        "I think my whole system is out of balance."
    ]
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

# --- Account Blueprint ---
account_bp = Blueprint('account', __name__, url_prefix='/account')

@app.route('/account')
@login_required
def account():
    subscription_info = None
    if current_user.subscription_id:
        try:
            subscription = stripe.Subscription.retrieve(current_user.subscription_id)
            current_period_end = datetime.utcfromtimestamp(subscription.current_period_end).strftime(
                "%Y-%m-%d %H:%M:%S")
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
        # Trigger alert notifications if free spaces reach threshold
        notify_alert_signups()
        try:
            ses = boto3.client('ses', region_name=os.getenv('AWS_REGION'))
            response = ses.send_email(
                Source=os.getenv('FROM_EMAIL', 'support@simul-ai-tor.com'),
                Destination={'ToAddresses': [user.email]},
                Message={
                    'Subject': {'Data': "Subscription Cancellation Confirmation"},
                    'Body': {'Text': {'Data': (
                        "Hello,\n\nYour subscription has been scheduled for cancellation at the end of your current billing period. "
                        "You will retain access until that time.\n\nThank you. Please note: This is an automated email and replies to this address are not monitored."
                    )}}
                }
            )
        except Exception as e:
            flash(f"Error sending cancellation email: {str(e)}", "warning")
        flash(
            "Your subscription will be cancelled at the end of the current billing period. "
            "Please note: This is an automated email and replies to this address are not monitored.",
            "success"
        )
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
            ses = boto3.client('ses', region_name=os.getenv('AWS_REGION'))
            response = ses.send_email(
                Source=os.getenv('FROM_EMAIL', 'support@simul-ai-tor.com'),
                Destination={'ToAddresses': [current_user.email]},
                Message={
                    'Subject': {'Data': "Subscription Reactivation Confirmation"},
                    'Body': {'Text': {'Data': (
                        "Hello,\n\nYour subscription has been reactivated. Enjoy using Simul-AI-tor.\n\n"
                        "Best regards,\nThe Support Team. Please note: This is an automated email and replies to this address are not monitored."
                    )}}
                }
            )
        except Exception as e:
            flash(f"Error sending reactivation email: {str(e)}", "warning")

        flash("Subscription reactivated successfully! A confirmation email has been sent.", "success")
    except Exception as e:
        flash(f"Error processing reactivation: {str(e)}", "danger")
    return redirect(url_for('account'))

# --- Landing Page Route ---
@app.route('/')
def landing():
    active_count = User.query.filter(User.subscription_status == 'active').count()
    remaining_places = max(100 - active_count, 0)  # ensure it doesn't go negative
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
            email = request.form['email']
            password = request.form['password']
            user = User.query.filter_by(email=email).first()
            if user and check_password_hash(user.password, password):
                # Check if the user has an active subscription
                if not user.is_admin and (not user.subscription_id or user.subscription_status != "active"):
                    flash("Your account is not activated yet. Please complete your payment to activate your account.",
                          "warning")
                    return redirect(url_for('start_payment'))

                session.pop('conversation', None)
                user.current_session = str(uuid.uuid4())
                db.session.commit()
                session['session_token'] = user.current_session
                login_user(user)
                return redirect(url_for('simulation'))
            else:
                flash("Invalid email or password", "danger")
    return render_template('login.html')

# --- Modified Registration Route (Email confirmation before payment) ---
@app.route('/register', methods=['GET', 'POST'])
def register():
    # Check for available subscription space
    active_count = User.query.filter(User.subscription_status == 'active').count()
    if active_count >= 100:
        flash("All subscription spaces are currently taken. Please sign up for alerts when a space becomes available.",
              "info")
        return redirect(url_for('alert_signup'))  # Ensure you have an alert signup route.
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

        # Store pending registration data in session
        pending_registration = {
            "email": email,
            "hashed_password": generate_password_hash(password),
            "category": category,
            "discipline": discipline,
            "promo_code": promo_code
        }
        session['pending_registration'] = pending_registration

        # Check if a pending registration already exists for this email.
        existing_pending = PendingRegistration.query.filter_by(email=email).first()
        if existing_pending:
            flash("A registration for this email is already pending confirmation. Please check your email or use the resend link.", "warning")
            return redirect(url_for('check_email'))

        # Create a new PendingRegistration record.
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
            flash("A registration for this email is already pending confirmation. Please check your email or use the resend link.", "warning")
            return redirect(url_for('check_email'))

        # Generate a confirmation token from the pending registration data.
        token = s.dumps(json.dumps(pending_registration), salt='email-confirmation-salt')

        # Send confirmation email with the token link.
        try:
            confirmation_link = url_for('confirm_email', token=token, _external=True)
            ses = boto3.client('ses', region_name=os.getenv('AWS_REGION'))
            response = ses.send_email(
                Source=os.getenv('FROM_EMAIL', 'support@simul-ai-tor.com'),
                Destination={'ToAddresses': [pending_registration["email"]]},
                Message={
                    'Subject': {'Data': "Please Confirm Your Email Address"},
                    'Body': {'Text': {'Data': (
                        f"Hello,\n\nThank you for subscribing! To complete your registration, please confirm your email address "
                        f"by clicking the link below:\n\n{confirmation_link}\n\nIf you did not register, please ignore this email."
                    )}}
                }
            )
        except Exception as e:
            flash(f"Error sending confirmation email: {str(e)}", "warning")
            return redirect(url_for('register'))

        # Save the email in session for later use in resending if needed.
        session['pending_email'] = pending_registration["email"]

        flash("A confirmation email has been sent. Please check your inbox to complete registration.", "info")
        return redirect(url_for('check_email'))
    return render_template('register.html')

@app.route('/check_email', methods=['GET'])
def check_email():
    return render_template('check_email.html')

@app.route('/alert_signup', methods=['GET', 'POST'])
def alert_signup():
    if request.method == 'POST':
        email = request.form.get('email')
        print(f"Received alert signup request for email: {email}")

        # Check if this email is already in the table
        existing = AlertSignup.query.filter_by(email=email).first()
        if existing:
            print(f"Email {email} is already in the alert signup table.")
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
            print(f"Error saving email {email}: {e}")
            flash(f"Error saving your email: {str(e)}", "danger")
            return redirect(url_for('alert_signup'))

        # Send confirmation email using AWS SES
        try:
            print("Initializing AWS SES client...")
            ses = boto3.client('ses', region_name=os.getenv('AWS_REGION'))
            response = ses.send_email(
                Source=os.getenv('FROM_EMAIL', 'support@simul-ai-tor.com'),
                Destination={'ToAddresses': [email]},
                Message={
                    'Subject': {'Data': "Alert Signup Confirmation"},
                    'Body': {'Text': {'Data': (
                        "Thank you for signing up for alerts! "
                        "We will notify you as soon as a subscription space becomes available."
                    )}}
                }
            )
            print(f"Alert signup email sent to {email} with response: {response}")
            flash("Thank you! You've been signed up for alerts.", "success")
        except Exception as e:
            print(f"Error sending alert signup email for {email}: {e}")
            flash(f"Error sending confirmation email: {str(e)}", "danger")

        return redirect(url_for('landing'))

    return render_template('alert_signup.html')

# --- Modified Email Confirmation Route ---
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
        subscription_id=None,  # No subscription yet
        subscription_status=None  # No payment yet
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

# --- New Route to Start Payment after Email Confirmation ---
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

# --- Resend Confirmation Route ---
@app.route('/resend_confirmation', methods=['GET'])
def resend_confirmation():
    # Retrieve the pending email from session
    pending_email = session.get('pending_email')
    if not pending_email:
        flash("No pending registration found. Please register again.", "danger")
        return redirect(url_for('register'))

    pending = PendingRegistration.query.filter_by(email=pending_email).first()
    if not pending:
        flash("No pending registration record found. Please register again.", "danger")
        return redirect(url_for('register'))

    # Prepare the registration data to generate a new token.
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
        ses = boto3.client('ses', region_name=os.getenv('AWS_REGION'))
        response = ses.send_email(
            Source=os.getenv('FROM_EMAIL', 'support@simul-ai-tor.com'),
            Destination={'ToAddresses': [pending.email]},
            Message={
                'Subject': {'Data': "Please Confirm Your Email Address (New Link)"},
                'Body': {'Text': {'Data': (
                    f"Hello,\n\nYour previous confirmation link expired. "
                    f"Please complete your registration by clicking the new link below:\n\n{confirmation_link}\n\n"
                    "If you did not register, please ignore this email."
                )}}
            }
        )
        flash("A new confirmation email has been sent. Please check your inbox.", "info")
    except Exception as e:
        flash(f"Error sending new confirmation email: {str(e)}", "warning")
    return redirect(url_for('login'))

# --- Modified Payment Success Route ---
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

        try:
            ses = boto3.client('ses', region_name=os.getenv('AWS_REGION'))
            response = ses.send_email(
                Source=os.getenv('FROM_EMAIL', 'support@simul-ai-tor.com'),
                Destination={'ToAddresses': [current_user.email]},
                Message={
                    'Subject': {'Data': "Subscription Confirmation"},
                    'Body': {'Text': {'Data': (
                        "Hello,\n\nYour subscription has been successfully updated. Enjoy using Simul-AI-tor.\n\n"
                        "Best regards,\nThe Support Team"
                    )}}
                }
            )
        except Exception as e:
            flash(f"Error sending final confirmation email: {str(e)}", "warning")

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
            email_content = (
                f"Dear User,\n\nWe received a request to reset your password. "
                f"To reset your password, click the link below (this link is valid for 1 hour):\n\n"
                f"{reset_url}\n\n"
                f"If you did not request a password reset, please ignore this email.\n\n"
                f"Best regards,\nYour Support Team\n"
            )
            try:
                ses = boto3.client('ses', region_name=os.getenv('AWS_REGION'))
                response = ses.send_email(
                    Source=os.getenv('FROM_EMAIL', 'support@simul-ai-tor.com'),
                    Destination={'ToAddresses': [email]},
                    Message={
                        'Subject': {'Data': "Password Reset Request"},
                        'Body': {'Text': {'Data': email_content}}
                    }
                )
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

# --- Start Simulation Route with Debug Logging ---
@app.route('/start_simulation', methods=['POST'])
@login_required
def start_simulation():
    # Get new fields: problem_complexity and patient_complexity
    problem_complexity = request.form.get('problem_complexity')
    patient_complexity = request.form.get('patient_complexity')
    country = request.form.get('country', 'United Kingdom')
    system_choice = request.form.get('system', 'random')
    # Retrieve the co-morbidity status from the form (expects "yes" or "no")
    comorbidities = request.form.get('comorbidities', 'no')
    session['comorbidities'] = comorbidities

    # Always include a statement in the prompt about co-morbidities.
    if comorbidities.lower() == "yes":
        comorbidity_str = " This patient has co-morbidities, which may influence their clinical presentation based on their age and ethnicity."
    else:
        comorbidity_str = " This patient does not have any co-morbidities."

    # Remove level check since we're no longer using levels for complaint selection.
    if patient_complexity not in ['Nil', 'Memory Issues', 'Frustrated']:
        flash("Invalid patient complexity selected.", "danger")
        return redirect(url_for('simulation'))
    if not country:
        flash("Please select a country.", "danger")
        return redirect(url_for('simulation'))

    # Determine tone descriptor based on patient complexity selection
    if patient_complexity == "Nil":
        tone = " Always use natural patient friendly language throughout as a common person would. Avoid jargon"
    elif patient_complexity == "Memory Issues":
        tone = "You are very forgetful with significant memory issues. You are not orientated to time and place. Always use natural patient friendly language throughout as a common person would. Avoid jargon or technical words. You answer some questions inaccurately or \"I'm not sure\"."
    elif patient_complexity == "Frustrated":
        tone = " You are short tempered, noticeably frustrated, sarcastic and in a rush. You complain about past experiences with healthcare practitioners and question whether the person asking you questions is even qualified, although you do answer questions. Everything frustrates you. Always use natural patient friendly language throughout as a common person would. Avoid jargon or technical words."
    else:
        tone = ""

    # Choose a patient and store for later use.
    if patient_complexity == "Memory Issues":
        eligible_patients = [p for p in PATIENT_NAMES if p["age"] >= 60]
        patient = random.choice(eligible_patients) if eligible_patients else random.choice(PATIENT_NAMES)
    else:
        patient = random.choice(PATIENT_NAMES)
    session['patient'] = patient
    print("DEBUG: Patient stored in session:", patient)
    session['patient'] = patient
    print("DEBUG: Patient stored in session:", patient)

    # If system_choice is 'random', select one at random.
    if system_choice == 'random':
        system_choice = random.choice(list(SYSTEM_COMPLAINTS.keys()))
    complaints = SYSTEM_COMPLAINTS.get(system_choice)
    if complaints and isinstance(complaints, list):
        selected_complaint = random.choice(complaints)
    else:
        selected_complaint = "No complaint available."

    # Store simulation parameters in session.
    session['system_choice'] = system_choice
    session['selected_complaint'] = selected_complaint
    session['problem_complexity'] = problem_complexity
    session['patient_complexity'] = patient_complexity
    session['country'] = country

    # Build the initial system prompt.
    instr = (
        f"You are a patient in a history-taking simulation taking place in {country}. "
        f"Your problem complexity is {problem_complexity} and your patient complexity is {patient_complexity}. "
        f"Your name is {patient['name']} (age {patient['age']}) and you are a {patient['gender']} patient."
        + comorbidity_str + " " +
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

    # Initialize conversation with this system prompt.
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
            current_user.token_prompt_usage_gpt4 = (current_user.token_prompt_usage_gpt4 or 0) + response.usage['prompt_tokens']
            current_user.token_completion_usage_gpt4 = (current_user.token_completion_usage_gpt4 or 0) + response.usage['completion_tokens']
            db.session.commit()
    except Exception as e:
        first_reply = f"Error with API: {str(e)}"
    session['conversation'].append({'role': 'assistant', 'content': first_reply})
    print("DEBUG: After first reply, conversation state:", session['conversation'])
    session.pop('feedback', None)
    session.pop('hint', None)
    return redirect(url_for('simulation'))

# --- Simulation Display Route ---
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

# --- Send Message Route with Debug Logging ---
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
        # If the message is too short or meets other conditions indicating insufficient context...
        if (len(stripped_msg) < 3 or
                lower_msg in generic_phrases or
                not re.search(r"[aeiou]", stripped_msg) or
                any(phrase in lower_msg for phrase in forced_context_phrases)):
            msg = "Sorry, I didn't catch that. Could you please repeat yourself?"

        conversation.append({'role': 'user', 'content': msg})
        session['conversation'] = conversation
        print("DEBUG: After user message added, conversation:", session['conversation'])
        return jsonify({"status": "ok"}), 200

    return jsonify({"status": "error", "message": "No message provided"}), 400

# --- Get Reply Route with Debug Logging ---
@app.route('/get_reply', methods=['POST'])
@csrf.exempt
@login_required
def get_reply():
    conversation = session.get('conversation', [])

    # Count the number of user messages
    user_message_count = sum(1 for m in conversation if m.get('role') == 'user')

    # Every 3 user messages, insert a reinforcement message if not already inserted.
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
            current_user.token_prompt_usage_gpt35 = (current_user.token_prompt_usage_gpt35 or 0) + response.usage['prompt_tokens']
            current_user.token_completion_usage_gpt35 = (current_user.token_completion_usage_gpt35 or 0) + response.usage['completion_tokens']
            db.session.commit()
    except openai.error.OpenAIError as e:
        resp_text = f"OpenAI API Error: {str(e)}"
    except Exception as e:
        resp_text = f"Unexpected Error: {str(e)}"

    conversation.append({'role': 'assistant', 'content': resp_text})
    session['conversation'] = conversation
    print("DEBUG: After get_reply, conversation:", conversation)
    return jsonify({"reply": resp_text}), 200

# --- Hint Route with Debug Logging ---
@app.route('/hint', methods=['POST'])
@login_required
def hint():
    conversation = session.get('conversation', [])
    if not conversation:
        flash("No conversation available for hint suggestions", "warning")
        return redirect(url_for('simulation'))
    conv_text = "\n".join([f"{'User' if m['role'] == 'user' else 'Patient'}: {m['content']}" for m in conversation if m['role'] != 'system'])
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
            current_user.token_prompt_usage_gpt35 = (current_user.token_prompt_usage_gpt35 or 0) + response.usage['prompt_tokens']
            current_user.token_completion_usage_gpt35 = (current_user.token_completion_usage_gpt35 or 0) + response.usage['completion_tokens']
            db.session.commit()
    except Exception as e:
        hint_response = f"Error with API: {str(e)}"
    session['hint'] = hint_response
    print("DEBUG: Hint response received:", hint_response)
    return redirect(url_for('simulation'))

# --- Feedback Route with Debug Logging ---
@app.route('/feedback', methods=['POST'])
@login_required
def feedback():
    conversation = session.get('conversation', [])
    if not conversation:
        flash("No conversation available for feedback", "warning")
        return redirect(url_for('simulation'))

    user_conv_text = "\n".join([f"User: {m['content']}" for m in conversation if m.get('role') == 'user'])
    feedback_prompt = (
        "IMPORTANT: Output ONLY valid JSON with NO disclaimers or additional commentary. "
        "Your answer MUST start with '{' and end with '}'. Use double quotes for all keys and string values, "
        "and do NOT use single quotes. Evaluate the following consultation transcript using the Calgary–Cambridge model. "
        "Score each category on a scale of 1 to 10, and provide a short comment for each:\n"
        "1. Initiating the session\n"
        "2. Gathering information: When scoring, ensure you assess if the transcript clearly explores the history of the present complaint (HPC) (including a pain assessment using the SOCRATES approach when pain/discomfort or similar is mentioned), and covers relevant aspects including past medical and surgical history, social history and family history\n"
        "3. Physical examination\n"
        "4. Explanation & planning\n"
        "5. Closing the session\n"
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
    return redirect(url_for('simulation'))

# --- Download Feedback Route ---
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
    country = session.get('country', 'Unknown')
    import random
    patient = session.get('patient')
    if not patient:
        patient = random.choice(PATIENT_NAMES)
        session['patient'] = patient
    instr = (
        f"You are a patient in a history-taking simulation taking place in {country}. "
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
    vitals_prompt = (
        "Include vital signs such as heart rate, blood pressure, respiratory rate, temperature, and oxygen saturation. "
    )
    extra_instructions = {
        "common ailments": (
            "Then, Describe any mild or non-specific findings in plain, clear language that relate to the presenting complaint."
        ),
        "ENT": (
            "Then, focus exclusively on the ENT examination: examine the ears for external deformities, tenderness, and discharge; inspect the tympanic membranes and assess hearing function; evaluate the nasal passages and sinuses for congestion, polyps, or discharge; and inspect the throat and oropharynx for redness, swelling, or exudate."
        ),
        "cardiovascular": (
            "Then, focus exclusively on the cardiovascular examination: describe the heart rate in full words, heart rhythm, the presence or absence of murmurs, and the quality of peripheral pulses."
        ),
        "respiratory": (
            "Then, focus exclusively on the respiratory examination: detail lung sounds in both lungs, note any wheezes or crackles, and comment on accessory muscle use."
        ),
        "gastrointestinal": (
            "Then, focus exclusively on the abdominal examination: describe tenderness, guarding, bowel sounds, and signs of peritonitis."
        ),
        "neurological": (
            "Then, focus exclusively on the neurological examination: assess alertness, cranial nerve function, motor and sensory responses, and any focal deficits."
        ),
        "musculoskeletal": (
            "Then, focus exclusively on the musculoskeletal examination: describe joint range of motion, tenderness, swelling, and muscle strength."
        ),
        "genitourinary": (
            "Then, focus exclusively on the genitourinary examination: examine the lower abdomen for tenderness, assess urinary frequency or retention, and look for signs of infection."
        ),
        "endocrine": (
            "Then, focus exclusively on the endocrine examination: comment on general appearance, skin and hair changes, and any metabolic disturbances."
        ),
        "dermatological": (
            "Then, focus exclusively on the dermatological examination: describe the distribution, texture, color, and signs of inflammation or infection of the rash."
        )
    }.get(system_choice,
          "Then, generate a complete physical examination with both vital signs and system-specific findings relevant to the complaint."
          )
    exam_prompt = (
        f"Generate a concise set of physical examination findings for a patient presenting with '{complaint}'. "
        + vitals_prompt +
        extra_instructions +
        " Ensure that the findings are specific to the likely cause of the complaint and written in full, plain language with no acronyms or abbreviations. "
        "Do not include any introductory phrases or extra text; provide only the exam findings."
    )
    print("DEBUG: Exam prompt:", exam_prompt)
    try:
        response = openai.ChatCompletion.create(
            model="gpt-3.5-turbo",
            messages=[{"role": "system", "content": exam_prompt}],
            temperature=0.7,
            max_tokens=250
        )
        exam_results = response.choices[0].message["content"].strip()
        if response.usage and 'prompt_tokens' in response.usage and 'completion_tokens' in response.usage:
            current_user.token_prompt_usage_gpt35 = (current_user.token_prompt_usage_gpt35 or 0) + response.usage['prompt_tokens']
            current_user.token_completion_usage_gpt35 = (current_user.token_completion_usage_gpt35 or 0) + response.usage['completion_tokens']
            db.session.commit()
    except Exception as e:
        exam_results = f"Error generating exam results: {str(e)}"
    print("DEBUG: Exam results generated:", exam_results)
    return jsonify({"results": exam_results}), 200

# --- New Notification Function ---
def notify_alert_signups():
    MAX_SUBSCRIPTIONS = 100  # Adjust as needed or load from config
    active_count = User.query.filter(User.subscription_status == 'active').count()
    free_spaces = MAX_SUBSCRIPTIONS - active_count
    free_percentage = free_spaces / MAX_SUBSCRIPTIONS

    if free_percentage >= 0.15:
        alert_signups = AlertSignup.query.all()
        for signup in alert_signups:
            try:
                ses = boto3.client('ses', region_name=os.getenv('AWS_REGION'))
                # SES does not support adding a custom "List-Unsubscribe" header directly in send_email.
                response = ses.send_email(
                    Source=os.getenv('FROM_EMAIL', 'support@simul-ai-tor.com'),
                    Destination={'ToAddresses': [signup.email]},
                    Message={
                        'Subject': {'Data': "Subscription Space Now Available!"},
                        'Body': {'Text': {'Data': (
                            "Good news! There are now enough subscription spaces available for you to register. "
                            "Please visit our registration page to sign up."
                        )}}
                    }
                )
                print(f"Notification sent to {signup.email}: {response}")
                # Remove the alert entry so they are not notified again
                db.session.delete(signup)
            except Exception as e:
                print(f"Error sending alert to {signup.email}: {str(e)}")
        db.session.commit()

# --- Daily Update Function ---
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
            ses = boto3.client('ses', region_name=os.getenv('AWS_REGION'))
            response = ses.send_email(
                Source=os.getenv('FROM_EMAIL', 'support@simul-ai-tor.com'),
                Destination={'ToAddresses': ["simulaitor@outlook.com"]},
                Message={
                    'Subject': {'Data': "Daily Subscription & API Cost Report"},
                    'Body': {'Text': {'Data': message}}
                }
            )
            print("AWS SES response for daily update:", response)
        except Exception as e:
            print(f"Error sending daily update: {str(e)}")

@app.route('/test_send_update')
def test_send_update():
    send_daily_update()
    return "Test email sent. Please check your inbox."

# --- New Functions for Dynamic Feedback Scoring ---

def get_last_three_average(user_id):
    # Query the last three feedback entries for the given user, ordered by creation time (most recent first)
    last_three = Feedback.query.filter_by(user_id=user_id)\
                               .order_by(Feedback.created_at.desc())\
                               .limit(3).all()
    if not last_three:
        return 0  # or handle as needed if there are no feedback entries
    # Calculate the average score
    avg_score = sum(f.score for f in last_three) / len(last_three)
    return avg_score

def get_user_ranking(user_id):
    # Compute the average score for each user based on all of their feedback entries
    users_avg = db.session.query(
                    Feedback.user_id,
                    db.func.avg(Feedback.score).label("avg_score")
                ).group_by(Feedback.user_id).all()

    # Sort the list in descending order of the average score (highest first)
    sorted_users = sorted(users_avg, key=lambda x: x.avg_score, reverse=True)

    # Find the ranking of the specified user (1-indexed)
    ranking = next((index + 1 for index, record in enumerate(sorted_users) if record.user_id == user_id), None)
    total_users = len(sorted_users)
    return ranking, total_users


@app.route('/get_scores')
@login_required
def get_scores():
    # Calculate the average of the last three feedback scores for the current user
    avg_score = get_last_three_average(current_user.id)

    # Calculate the user's ranking and the total number of users with feedback
    ranking, total_users = get_user_ranking(current_user.id)

    # Return the values as a JSON object
    return jsonify({
        "avg_score": avg_score,
        "ranking": ranking,
        "total_users": total_users
    })

if __name__ == '__main__':
    app.run(debug=True)


