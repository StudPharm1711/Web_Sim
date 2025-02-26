import os
import io
import random
import re
from flask import Flask, render_template, redirect, url_for, request, flash, session, send_file
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from dotenv import load_dotenv
import openai
from reportlab.lib.pagesizes import letter
from reportlab.platypus import SimpleDocTemplate, Paragraph
from reportlab.lib.styles import getSampleStyleSheet

# Load environment variables from .env file
load_dotenv()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
openai.api_key = OPENAI_API_KEY

# Initialize Flask app and configuration
app = Flask(__name__)
app.config['SECRET_KEY'] = 'your_secret_key'  # Replace with a strong secret key
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///users.db'
db = SQLAlchemy(app)

# Initialize Flask-Login
login_manager = LoginManager(app)
login_manager.login_view = 'login'

# Define the User model
class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(150), unique=True, nullable=False)
    password = db.Column(db.String(150), nullable=False)

# User loader callback
@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

# Global variables for simulation
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

# Expanded presenting complaints for each level
FOUNDATION_COMPLAINTS = [
    "fever", "persistent cough", "mild chest pain", "headache", "lower back pain",
    "pain on urination", "earache", "reduced hearing", "shortness of breath", "abdominal discomfort",
    "sore throat", "runny nose", "muscle aches", "fatigue", "joint stiffness",
    "skin rash", "nausea", "dizziness", "constipation", "leg swelling",
    "chills", "vomiting", "diarrhea", "loss of appetite", "stomach cramps",
    "body aches", "joint pain", "mild confusion", "slight vision changes", "nasal congestion",
    "sneezing", "watery eyes", "mild rash"
]

ENHANCED_COMPLAINTS = [
    "chest pain", "shortness of breath", "persistent fatigue", "abdominal pain", "chronic diarrhea",
    "joint stiffness", "recurring fever", "severe headache", "nausea", "unintentional weight loss",
    "heart palpitations", "muscle weakness", "skin irritation", "recurrent dizziness", "constipation",
    "leg swelling", "backache", "blurred vision", "frequent urination", "throat soreness",
    "fever", "chills", "vomiting", "loss of appetite", "stomach cramps", "body aches",
    "joint pain", "mild confusion", "sore throat", "nasal congestion", "sneezing", "watery eyes", "mild rash"
]

ADVANCED_COMPLAINTS = [
    "severe chest pain", "acute shortness of breath", "chronic fatigue", "irregular heart palpitations", "intense abdominal pain",
    "high fever", "significant weight loss", "persistent diarrhea", "severe nausea", "debilitating joint pain",
    "migraines", "reduced hearing", "leg swelling", "back pain radiating to the legs", "blurred vision",
    "frequent urination with pain", "throat soreness with difficulty swallowing", "skin rash with itching",
    "recurrent dizziness", "confusion", "severe headache"
]

def get_shuffled_complaints(complaints_list):
    """Return a comma-separated string of randomly shuffled complaints."""
    complaints = complaints_list.copy()
    random.shuffle(complaints)
    return ", ".join(complaints)

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

# Routes

@app.route('/')
def index():
    return redirect(url_for('login'))

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        user = User.query.filter_by(username=username).first()
        if user and check_password_hash(user.password, password):
            session.pop('conversation', None)  # Clear previous conversation on login
            login_user(user)
            return redirect(url_for('simulation'))
        else:
            flash("Invalid username or password", "danger")
    return render_template('login.html')

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        if User.query.filter_by(username=username).first():
            flash("Username already exists", "danger")
        else:
            hashed = generate_password_hash(password)
            new_user = User(username=username, password=hashed)
            db.session.add(new_user)
            db.session.commit()
            flash("Registration successful! Please log in.", "success")
            return redirect(url_for('login'))
    return render_template('register.html')

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
    session['conversation'] = [
        {'role': 'system', 'content': instr},
        {'role': 'assistant', 'content': "Can I speak with someone about my symptoms?"}
    ]
    session.pop('feedback', None)
    session.pop('hint', None)
    return redirect(url_for('simulation'))

@app.route('/simulation', methods=['GET', 'POST'])
@login_required
def simulation():
    conversation = session.get('conversation', [])
    if request.method == 'POST':
        session.pop('hint', None)
        msg = request.form['message']
        conversation.append({'role': 'user', 'content': msg})
        try:
            response = openai.ChatCompletion.create(
                model="gpt-4-turbo",
                messages=conversation,
                temperature=0.8
            )
            resp_text = response.choices[0].message["content"]
        except Exception as e:
            resp_text = f"Error with API: {str(e)}"
        conversation.append({'role': 'assistant', 'content': resp_text})
        session['conversation'] = conversation
    display_conv = [m for m in conversation if m['role'] != 'system']
    return render_template('simulation.html', conversation=display_conv,
                           feedback=session.get('feedback'),
                           hint=session.get('hint'))

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
    with app.app_context():
        db.create_all()
    app.run(debug=True)
