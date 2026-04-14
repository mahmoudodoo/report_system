from functools import wraps
import os
import sqlite3
from datetime import datetime
import requests
import tempfile
import whisper
import subprocess

from flask import (
    Flask,
    render_template,
    request,
    redirect,
    session,
    flash,
    url_for,
    jsonify,
)

from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import or_, cast, String
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename

from flask_mail import Mail, Message
from itsdangerous import URLSafeTimedSerializer, SignatureExpired, BadTimeSignature

from twilio.rest import Client
from twilio.twiml.voice_response import VoiceResponse
import assemblyai as aai

from dotenv import load_dotenv
load_dotenv()

# ------------------------------
# App Configuration
# ------------------------------
app = Flask(__name__)
app.secret_key = "secret123"

# Database – use the instance folder
db_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'instance', 'database.db')
app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{db_path}"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["UPLOAD_FOLDER"] = os.path.join("static", "uploads")

# Mail settings
app.config["MAIL_SERVER"] = "smtp.gmail.com"
app.config["MAIL_PORT"] = 587
app.config["MAIL_USE_TLS"] = True
app.config["MAIL_USERNAME"] = "ablgah.official@gmail.com"
app.config["MAIL_PASSWORD"] = "ollgvdgnfkqodscc"
app.config["MAIL_DEFAULT_SENDER"] = "ablgah.official@gmail.com"

# Twilio settings
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_PHONE_NUMBER = os.getenv("TWILIO_PHONE_NUMBER", "+17406658652")
SUPPORT_AGENT_NUMBER = os.getenv("SUPPORT_AGENT_NUMBER")

# AssemblyAI (optional fallback)
AAI_API_KEY = os.getenv("ASSEMBLYAI_API_KEY")
if AAI_API_KEY:
    aai.settings.api_key = AAI_API_KEY

# Initialize extensions
db = SQLAlchemy(app)
mail = Mail(app)
serializer = URLSafeTimedSerializer(app.secret_key)
if TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN:
    twilio_client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
else:
    twilio_client = None

os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)

ADMIN_EMAIL = "reemasaad756@gmail.com"

# Load Whisper model (once at startup)
whisper_model = None
try:
    whisper_model = whisper.load_model("small")  # or "base" for faster/lighter
    print("✅ Whisper model loaded successfully")
except Exception as e:
    print(f"⚠️ Could not load Whisper model: {e}")

# ------------------------------
# Helper function: transcribe using Whisper
# ------------------------------
def transcribe_audio_with_whisper(audio_path):
    """Transcribe audio file (any format) using Whisper."""
    if whisper_model is None:
        raise Exception("Whisper model not available")
    # Whisper can read many formats directly (needs ffmpeg)
    result = whisper_model.transcribe(audio_path, language="ar", fp16=False)
    return result["text"].strip()

# ------------------------------
# Database Models (unchanged)
# ------------------------------
class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    email = db.Column(db.String(100), unique=True, nullable=False)
    password = db.Column(db.String(255), nullable=False)
    phone = db.Column(db.String(20), nullable=True)
    language = db.Column(db.String(20), default="العربية")
    theme = db.Column(db.String(20), default="light")
    avatar = db.Column(db.String(255), nullable=True)
    is_admin = db.Column(db.Boolean, default=False)

class Report(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    type = db.Column(db.String(50), nullable=False)
    description = db.Column(db.Text, nullable=False)
    status = db.Column(db.String(20), default="جديد")
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"))
    created_at = db.Column(db.DateTime, default=db.func.current_timestamp())

class SupportMessage(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    email = db.Column(db.String(100), nullable=False)
    issue_type = db.Column(db.String(50), nullable=False)
    message = db.Column(db.Text, nullable=False)
    status = db.Column(db.String(20), default="جديدة")
    reply = db.Column(db.Text, nullable=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)
    is_read = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=db.func.current_timestamp())

class CallReport(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    report_type = db.Column(db.String(50), nullable=False)
    problem_category = db.Column(db.String(50), nullable=True)
    transcript = db.Column(db.Text, nullable=True)
    location_lat = db.Column(db.Float, nullable=True)
    location_lng = db.Column(db.Float, nullable=True)
    status = db.Column(db.String(20), default="pending")
    call_sid = db.Column(db.String(100), nullable=True)
    recording_url = db.Column(db.String(500), nullable=True)
    created_at = db.Column(db.DateTime, default=db.func.current_timestamp())

class EmergencyCall(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)
    problem_category = db.Column(db.String(50), nullable=False)
    transcript = db.Column(db.Text, nullable=False)
    location = db.Column(db.String(200), nullable=True)
    call_sid = db.Column(db.String(100), nullable=True)
    status = db.Column(db.String(20), default="initiated")
    created_at = db.Column(db.DateTime, default=db.func.current_timestamp())

# ------------------------------
# Helper functions (unchanged)
# ------------------------------
def login_required(view_func):
    @wraps(view_func)
    def wrapped_view(*args, **kwargs):
        if "user_id" not in session:
            flash("يجب تسجيل الدخول أولًا", "error")
            return redirect(url_for("login_page"))
        return view_func(*args, **kwargs)
    return wrapped_view

def admin_required(view_func):
    @wraps(view_func)
    def wrapped_view(*args, **kwargs):
        user = get_current_user()
        if not user or not user.is_admin:
            flash("ليس لديك صلاحية للوصول لهذه الصفحة", "error")
            return redirect(url_for("home"))
        return view_func(*args, **kwargs)
    return wrapped_view

def get_current_user():
    if "user_id" in session:
        return User.query.get(session["user_id"])
    return None

def classify_problem(transcript):
    t = transcript.lower()
    if any(word in t for word in ["حريق", "fire", "flame", "burning"]):
        return "حريق"
    if any(word in t for word in ["حادث", "accident", "crash", "collision"]):
        return "حادث"
    if any(word in t for word in ["نزيف", "bleeding", "blood"]):
        return "نزيف"
    if any(word in t for word in ["سرقة", "theft", "robbery", "steal"]):
        return "سرقة"
    if any(word in t for word in ["شجار", "fight", "quarrel"]):
        return "شجار"
    return "عام"

@app.context_processor
def inject_user_preferences():
    user = get_current_user()
    unread_support_count = 0
    if user:
        unread_support_count = SupportMessage.query.filter_by(
            user_id=user.id, status="تم الرد", is_read=False
        ).count()
    return {
        "current_user": user,
        "current_theme": user.theme if user else "light",
        "current_language": user.language if user else "العربية",
        "user": user,
        "unread_support_count": unread_support_count
    }

# ------------------------------
# Automatic database column repair
# ------------------------------
def ensure_columns():
    db_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'instance', 'database.db')
    if not os.path.exists(db_path):
        return
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute("PRAGMA table_info(report)")
        cols = [c[1] for c in cursor.fetchall()]
        if 'created_at' not in cols:
            cursor.execute("ALTER TABLE report ADD COLUMN created_at TIMESTAMP")
            conn.commit()
            print("✅ Added created_at to report")
        cursor.execute("PRAGMA table_info(support_message)")
        cols = [c[1] for c in cursor.fetchall()]
        if 'created_at' not in cols:
            cursor.execute("ALTER TABLE support_message ADD COLUMN created_at TIMESTAMP")
            conn.commit()
            print("✅ Added created_at to support_message")
        cursor.execute("PRAGMA table_info(call_report)")
        cols = [c[1] for c in cursor.fetchall()] if cursor.fetchone() else []
        if 'report_type' not in cols:
            cursor.execute("ALTER TABLE call_report ADD COLUMN report_type VARCHAR(50) NOT NULL DEFAULT ''")
            conn.commit()
            print("✅ Added report_type to call_report")
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS emergency_call (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                problem_category VARCHAR(50) NOT NULL,
                transcript TEXT NOT NULL,
                location VARCHAR(200),
                call_sid VARCHAR(100),
                status VARCHAR(20) DEFAULT 'initiated',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES user(id)
            )
        """)
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"Column ensure warning: {e}")

ensure_columns()

# ------------------------------
# All existing routes (unchanged) – only the transcription logic is replaced
# I'll include them from your previous app.py, but I'll replace the AssemblyAI calls with Whisper.
# For brevity, I'll show only the modified parts; the full file is attached.
# ------------------------------

# (The following routes are identical to your previous app.py except the transcription steps)

@app.route("/")
def home():
    return render_template("home.html", user=get_current_user())

@app.route("/login", methods=["GET"])
def login_page():
    return render_template("login.html")

@app.route("/login", methods=["POST"])
def login():
    email = request.form.get("email", "").strip()
    password = request.form.get("password", "").strip()
    if not email or not password:
        flash("يرجى تعبئة جميع الحقول", "error")
        return redirect(url_for("login_page"))
    user = User.query.filter_by(email=email).first()
    if user is None or not check_password_hash(user.password, password):
        flash("البريد الإلكتروني أو كلمة المرور غير صحيحة", "error")
        return redirect(url_for("login_page"))
    session["user_id"] = user.id
    session["user_name"] = user.name
    flash("تم تسجيل الدخول بنجاح", "success")
    return redirect(url_for("home"))

@app.route("/admin-login", methods=["POST"])
def admin_login():
    email = request.form.get("email", "").strip()
    password = request.form.get("password", "").strip()
    if not email or not password:
        flash("يرجى تعبئة جميع الحقول", "error")
        return redirect(url_for("login_page"))
    user = User.query.filter_by(email=email).first()
    if user is None or not check_password_hash(user.password, password):
        flash("البريد الإلكتروني أو كلمة المرور غير صحيحة", "error")
        return redirect(url_for("login_page"))
    if not user.is_admin:
        flash("ليس لديك صلاحية أدمن", "error")
        return redirect(url_for("login_page"))
    session["user_id"] = user.id
    session["user_name"] = user.name
    flash("مرحباً! تم تسجيل دخولك كأدمن", "success")
    return redirect(url_for("dashboard"))

@app.route("/register", methods=["GET"])
def register_page():
    return render_template("register.html")

@app.route("/register", methods=["POST"])
def register():
    name = request.form.get("name", "").strip()
    email = request.form.get("email", "").strip()
    password = request.form.get("password", "").strip()
    if not name or not email or not password:
        flash("يرجى تعبئة جميع الحقول", "error")
        return redirect(url_for("register_page"))
    existing_user = User.query.filter_by(email=email).first()
    if existing_user:
        flash("هذا البريد مسجل من قبل", "error")
        return redirect(url_for("register_page"))
    hashed_password = generate_password_hash(password)
    is_admin = (email.lower() == ADMIN_EMAIL.lower())
    new_user = User(
        name=name, email=email, password=hashed_password,
        phone="", language="العربية", theme="light", avatar="", is_admin=is_admin
    )
    db.session.add(new_user)
    db.session.commit()
    flash("تم إنشاء الحساب بنجاح، يمكنك تسجيل الدخول الآن", "success")
    return redirect(url_for("login_page"))

@app.route("/forgot-password", methods=["GET"])
def forgot_password_page():
    return render_template("forgot_password.html")

@app.route("/forgot-password", methods=["POST"])
def forgot_password():
    email = request.form.get("email", "").strip()
    user = User.query.filter_by(email=email).first()
    if user is None:
        flash("هذا البريد غير مسجل", "error")
        return redirect(url_for("forgot_password_page"))
    try:
        token = serializer.dumps(user.email, salt="reset-password-salt")
        reset_link = url_for("reset_password_page", token=token, _external=True)
        msg = Message(subject="إعادة تعيين كلمة المرور - منصة أبلغ", recipients=[user.email])
        msg.body = f"""مرحبًا {user.name}،

تلقينا طلبًا لإعادة تعيين كلمة المرور الخاصة بحسابك في منصة أبلغ.

يمكنك إعادة تعيين كلمة المرور من خلال الرابط التالي:
{reset_link}

ملاحظة:
هذا الرابط صالح لمدة ساعة واحدة فقط.

إذا لم تطلب إعادة تعيين كلمة المرور، يمكنك تجاهل هذه الرسالة.

مع التحية،
فريق منصة أبلغ"""
        mail.send(msg)
        flash("تم إرسال رابط إعادة تعيين كلمة المرور إلى بريدك الإلكتروني", "success")
        return redirect(url_for("login_page"))
    except Exception:
        flash("حدث خطأ أثناء إرسال البريد الإلكتروني.", "error")
        return redirect(url_for("forgot_password_page"))

@app.route("/reset-password/<token>", methods=["GET"])
def reset_password_page(token):
    try:
        email = serializer.loads(token, salt="reset-password-salt", max_age=3600)
        return render_template("reset_password.html", token=token, email=email)
    except SignatureExpired:
        flash("انتهت صلاحية رابط إعادة التعيين", "error")
        return redirect(url_for("forgot_password_page"))
    except BadTimeSignature:
        flash("رابط إعادة التعيين غير صالح", "error")
        return redirect(url_for("forgot_password_page"))

@app.route("/reset-password/<token>", methods=["POST"])
def reset_password(token):
    try:
        email = serializer.loads(token, salt="reset-password-salt", max_age=3600)
    except SignatureExpired:
        flash("انتهت صلاحية رابط إعادة التعيين", "error")
        return redirect(url_for("forgot_password_page"))
    except BadTimeSignature:
        flash("رابط إعادة التعيين غير صالح", "error")
        return redirect(url_for("forgot_password_page"))
    password = request.form.get("password", "").strip()
    confirm_password = request.form.get("confirm_password", "").strip()
    if not password or not confirm_password:
        flash("يرجى تعبئة جميع الحقول", "error")
        return render_template("reset_password.html", token=token, email=email)
    if password != confirm_password:
        flash("كلمتا المرور غير متطابقتين", "error")
        return render_template("reset_password.html", token=token, email=email)
    user = User.query.filter_by(email=email).first()
    if user is None:
        flash("المستخدم غير موجود", "error")
        return redirect(url_for("forgot_password_page"))
    user.password = generate_password_hash(password)
    db.session.commit()
    flash("تم تغيير كلمة المرور بنجاح، يمكنك تسجيل الدخول الآن", "success")
    return redirect(url_for("login_page"))

@app.route("/report", methods=["GET"])
@login_required
def report_page():
    return render_template("report.html")

@app.route("/report/<string:report_type>", methods=["GET"])
@login_required
def report_form(report_type):
    return render_template("report_form.html", type=report_type)

@app.route("/submit", methods=["POST"])
@login_required
def submit_report():
    report_type = request.form.get("type", "").strip()
    description = request.form.get("description", "").strip()
    if not description:
        flash("يرجى تعبئة وصف البلاغ", "error")
        return redirect(url_for("report_page"))
    final_type = report_type if report_type else "عام"
    new_report = Report(
        type=final_type,
        description=description,
        status="جديد",
        user_id=session["user_id"]
    )
    db.session.add(new_report)
    db.session.commit()
    flash("تم إرسال البلاغ بنجاح", "success")
    return redirect(url_for("success_page"))

@app.route("/success")
@login_required
def success_page():
    reports = Report.query.filter_by(user_id=session["user_id"]).all()
    return render_template("success.html", reports=reports)

@app.route("/dashboard")
@admin_required
def dashboard():
    reports = Report.query.all()
    users = User.query.all()
    return render_template("dashboard.html", reports=reports, users=users)

@app.route("/my-reports")
@login_required
def my_reports():
    regular_reports = Report.query.filter_by(user_id=session["user_id"]).all()
    call_reports = CallReport.query.filter_by(user_id=session["user_id"]).all()
    combined = []
    for r in regular_reports:
        combined.append({
            'id': r.id,
            'type': r.type,
            'description': r.description,
            'status': r.status,
            'created_at': r.created_at,
            'is_call': False,
            'call_id': None,
            'transcript': None
        })
    for cr in call_reports:
        combined.append({
            'id': cr.id,
            'type': cr.report_type,
            'description': f"[بلاغ هاتفي] {cr.transcript[:150] if cr.transcript else 'جاري المعالجة...'}",
            'status': 'جديد',
            'created_at': cr.created_at,
            'is_call': True,
            'call_id': cr.id,
            'transcript': cr.transcript
        })
    combined.sort(key=lambda x: x['created_at'] or datetime.min, reverse=True)
    return render_template("my_reports.html", reports=combined)

@app.route("/call-details/<int:call_id>")
@login_required
def call_report_details(call_id):
    call_report = CallReport.query.get_or_404(call_id)
    if call_report.user_id != session["user_id"] and not get_current_user().is_admin:
        flash("ليس لديك صلاحية لعرض هذا البلاغ", "error")
        return redirect(url_for("my_reports"))

    if not call_report.transcript and call_report.call_sid and twilio_client:
        try:
            recordings = twilio_client.recordings.list(call_sid=call_report.call_sid)
            if recordings:
                recording = recordings[0]
                recording_url = f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_ACCOUNT_SID}/Recordings/{recording.sid}.mp3"
                auth = (TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
                resp = requests.get(recording_url, auth=auth, stream=True)
                if resp.status_code == 200:
                    with tempfile.NamedTemporaryFile(delete=False, suffix='.mp3') as tmp_file:
                        for chunk in resp.iter_content(chunk_size=8192):
                            tmp_file.write(chunk)
                        tmp_path = tmp_file.name
                    # Use Whisper for transcription
                    try:
                        transcript = transcribe_audio_with_whisper(tmp_path)
                        category = classify_problem(transcript)
                        call_report.transcript = transcript
                        call_report.problem_category = category
                        call_report.status = "transcribed"
                        db.session.commit()
                        # Create normal report entry
                        existing = Report.query.filter_by(
                            user_id=call_report.user_id,
                            description=f"[مكالمة هاتفية] {transcript[:200]}"
                        ).first()
                        if not existing:
                            normal_report = Report(
                                type=call_report.report_type,
                                description=f"[مكالمة هاتفية] {transcript[:200]}",
                                status="جديد",
                                user_id=call_report.user_id
                            )
                            db.session.add(normal_report)
                            db.session.commit()
                    except Exception as e:
                        print(f"Whisper transcription error: {e}")
                        flash("حدث خطأ أثناء تحويل الصوت إلى نص.", "error")
                    finally:
                        os.unlink(tmp_path)
        except Exception as e:
            print(f"On‑demand transcription error: {e}")
            flash("حدث خطأ أثناء محاولة معالجة التسجيل الصوتي.", "error")

    return render_template("call_details.html", call=call_report)

@app.route("/about")
def about():
    return render_template("about.html")

@app.route('/about-us')
def about_us():
    return render_template('about_us.html')

@app.route("/details/<int:report_id>")
@login_required
def details(report_id):
    report = Report.query.get_or_404(report_id)
    return render_template("details.html", report=report)

@app.route("/update/<int:report_id>", methods=["POST"])
@admin_required
def update_report(report_id):
    report = Report.query.get_or_404(report_id)
    new_status = request.form.get("new_status", "")
    if new_status == "processing":
        report.status = "قيد المعالجة"
    elif new_status == "closed":
        report.status = "مغلق"
    elif new_status == "new":
        report.status = "جديد"
    else:
        flash("حالة غير صالحة", "error")
        return redirect(url_for("dashboard"))
    db.session.commit()
    flash("تم تحديث حالة البلاغ بنجاح", "success")
    return redirect(url_for("dashboard"))

@app.route("/delete/<int:report_id>", methods=["POST"])
@admin_required
def delete_report(report_id):
    report = Report.query.get_or_404(report_id)
    db.session.delete(report)
    db.session.commit()
    flash("تم حذف البلاغ بنجاح", "success")
    return redirect(url_for("dashboard"))

@app.route("/promote/<int:user_id>", methods=["POST"])
@admin_required
def promote_to_admin(user_id):
    user = User.query.get_or_404(user_id)
    if user.is_admin:
        flash("هذا المستخدم أدمن بالفعل", "error")
        return redirect(url_for("dashboard"))
    user.is_admin = True
    db.session.commit()
    flash(f"تم ترقية {user.name} لأدمن بنجاح ✅", "success")
    return redirect(url_for("dashboard"))

@app.route("/profile", methods=["GET"])
@login_required
def profile_page():
    user = User.query.get_or_404(session["user_id"])
    return render_template("profile.html", user=user)

@app.route("/profile", methods=["POST"])
@login_required
def update_profile():
    user = User.query.get_or_404(session["user_id"])
    name = request.form.get("name", "").strip()
    email = request.form.get("email", "").strip()
    phone = request.form.get("phone", "").strip()
    if not name or not email:
        flash("يرجى تعبئة الاسم والبريد الإلكتروني", "error")
        return redirect(url_for("profile_page"))
    existing_user = User.query.filter(User.email == email, User.id != user.id).first()
    if existing_user:
        flash("هذا البريد الإلكتروني مستخدم من قبل", "error")
        return redirect(url_for("profile_page"))
    user.name = name
    user.email = email
    user.phone = phone
    avatar_file = request.files.get("avatar")
    if avatar_file and avatar_file.filename:
        filename = secure_filename(avatar_file.filename)
        filepath = os.path.join(app.config["UPLOAD_FOLDER"], filename)
        avatar_file.save(filepath)
        user.avatar = f"uploads/{filename}"
    db.session.commit()
    session["user_name"] = user.name
    flash("تم تحديث الملف الشخصي بنجاح", "success")
    return redirect(url_for("profile_page"))

@app.route("/settings", methods=["GET"])
@login_required
def settings_page():
    user = User.query.get_or_404(session["user_id"])
    return render_template("settings.html", user=user)

@app.route("/settings", methods=["POST"])
@login_required
def update_settings():
    user = User.query.get_or_404(session["user_id"])
    language = request.form.get("language", "").strip()
    theme = request.form.get("theme", "").strip()
    current_password = request.form.get("current_password", "").strip()
    new_password = request.form.get("new_password", "").strip()
    confirm_password = request.form.get("confirm_password", "").strip()
    user.language = language if language else "العربية"
    user.theme = theme if theme else "light"
    if current_password or new_password or confirm_password:
        if not current_password or not new_password or not confirm_password:
            flash("لتغيير كلمة المرور يجب تعبئة جميع حقول كلمة المرور", "error")
            return redirect(url_for("settings_page"))
        if not check_password_hash(user.password, current_password):
            flash("كلمة المرور الحالية غير صحيحة", "error")
            return redirect(url_for("settings_page"))
        if new_password != confirm_password:
            flash("كلمتا المرور الجديدتان غير متطابقتين", "error")
            return redirect(url_for("settings_page"))
        user.password = generate_password_hash(new_password)
    db.session.commit()
    flash("تم تحديث الإعدادات بنجاح", "success")
    return redirect(url_for("settings_page"))

@app.route("/support", methods=["GET", "POST"])
def support_page():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip()
        issue_type = request.form.get("issue_type", "").strip()
        message = request.form.get("message", "").strip()
        if not name or not email or not issue_type or not message:
            flash("يرجى تعبئة جميع الحقول", "error")
            return redirect(url_for("support_page"))
        new_message = SupportMessage(
            name=name, email=email, issue_type=issue_type, message=message,
            user_id=session.get("user_id")
        )
        db.session.add(new_message)
        db.session.commit()
        flash("تم إرسال رسالتك بنجاح، وسيتم الرد عليك من داخل الموقع", "success")
        return redirect(url_for("support_page"))
    user_messages = []
    if "user_id" in session:
        user_messages = SupportMessage.query.filter_by(user_id=session["user_id"]).order_by(SupportMessage.id.desc()).all()
        unread_messages = SupportMessage.query.filter_by(user_id=session["user_id"], status="تم الرد", is_read=False).all()
        for msg in unread_messages:
            msg.is_read = True
        if unread_messages:
            db.session.commit()
    return render_template("support.html", user_messages=user_messages)

@app.route("/admin-support")
@admin_required
def admin_support():
    messages = SupportMessage.query.order_by(SupportMessage.id.desc()).all()
    return render_template("admin_support.html", messages=messages)

@app.route("/support/reply/<int:id>", methods=["POST"])
@admin_required
def reply_support(id):
    msg = SupportMessage.query.get_or_404(id)
    reply_text = request.form.get("reply", "").strip()
    if not reply_text:
        flash("يرجى كتابة الرد أولًا", "error")
        return redirect(url_for("admin_support"))
    msg.reply = reply_text
    msg.status = "تم الرد"
    msg.is_read = False
    db.session.commit()
    flash("تم إرسال الرد داخل الموقع بنجاح", "success")
    return redirect(url_for("admin_support"))

@app.route("/support/update/<int:id>/<string:new_status>")
@admin_required
def update_support_status(id, new_status):
    msg = SupportMessage.query.get_or_404(id)
    if new_status == "replied":
        msg.status = "تم الرد"
    elif new_status == "closed":
        msg.status = "مغلقة"
    elif new_status == "new":
        msg.status = "جديدة"
    else:
        flash("حالة غير صالحة", "error")
        return redirect(url_for("admin_support"))
    db.session.commit()
    return redirect(url_for("admin_support"))

@app.route("/support/delete/<int:id>")
@admin_required
def delete_support(id):
    msg = SupportMessage.query.get_or_404(id)
    db.session.delete(msg)
    db.session.commit()
    flash("تم حذف الرسالة بنجاح", "success")
    return redirect(url_for("admin_support"))

@app.route("/notifications")
@login_required
def notifications_page():
    notifications = SupportMessage.query.filter_by(user_id=session["user_id"], status="تم الرد").order_by(SupportMessage.id.desc()).all()
    unread_messages = SupportMessage.query.filter_by(user_id=session["user_id"], status="تم الرد", is_read=False).all()
    for msg in unread_messages:
        msg.is_read = True
    if unread_messages:
        db.session.commit()
    return render_template("notifications.html", notifications=notifications)

@app.route("/search-suggestions")
def search_suggestions():
    query = request.args.get("q", "").strip()
    if not query:
        return jsonify([])
    reports = (
        db.session.query(Report, User)
        .outerjoin(User, Report.user_id == User.id)
        .filter(
            or_(
                cast(Report.id, String).ilike(f"%{query}%"),
                Report.type.ilike(f"%{query}%"),
                Report.description.ilike(f"%{query}%"),
                Report.status.ilike(f"%{query}%"),
                User.name.ilike(f"%{query}%"),
                User.email.ilike(f"%{query}%")
            )
        )
        .limit(6)
        .all()
    )
    results = []
    for report, user in reports:
        owner_name = user.name if user else "مستخدم"
        results.append({
            "title": f"بلاغ رقم {report.id} - {report.type}",
            "subtitle": f"{report.description[:60]} | مقدم البلاغ: {owner_name}",
            "status": report.status,
            "url": url_for("dashboard")
        })
    return jsonify(results)

@app.route("/logout")
def logout():
    session.clear()
    flash("تم تسجيل الخروج", "success")
    return redirect(url_for("login_page"))

# ------------------------------
# Call Report Routes (Twilio webhooks) – only the transcription part changed
# ------------------------------
@app.route("/save-location", methods=["POST"])
@login_required
def save_location():
    data = request.get_json()
    lat = data.get("lat")
    lng = data.get("lng")
    if lat is not None and lng is not None:
        session["temp_lat"] = lat
        session["temp_lng"] = lng
        return jsonify({"success": True})
    return jsonify({"success": False}), 400

@app.route("/initiate-call-report", methods=["POST"])
@login_required
def initiate_call_report():
    data = request.get_json()
    report_type = data.get("type")
    if not report_type:
        return jsonify({"error": "نوع البلاغ مطلوب"}), 400
    user = get_current_user()
    if not user.phone:
        return jsonify({"error": "رقم الهاتف غير موجود. يرجى إضافته في الملف الشخصي."}), 400
    lat = session.get("temp_lat")
    lng = session.get("temp_lng")
    if not lat or not lng:
        return jsonify({"error": "يرجى تحديد موقعك باستخدام زر 'إرسال الموقع الحالي' أولاً"}), 400
    call_report = CallReport(
        user_id=user.id,
        report_type=report_type,
        location_lat=lat,
        location_lng=lng,
        status="pending"
    )
    db.session.add(call_report)
    db.session.commit()
    try:
        if not twilio_client:
            return jsonify({"error": "Twilio not configured"}), 500
        call = twilio_client.calls.create(
            url=url_for("voice_webhook", report_id=call_report.id, _external=True),
            to=user.phone,
            from_=TWILIO_PHONE_NUMBER,
            timeout=30
        )
        call_report.call_sid = call.sid
        db.session.commit()
        return jsonify({"success": True, "message": "جاري الاتصال بك...", "call_id": call_report.id})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/voice-webhook/<int:report_id>", methods=["POST"])
def voice_webhook(report_id):
    response = VoiceResponse()
    response.say("مرحباً، هذه منصة أبلغ. بعد سماع صوت التنبيه، سجل رسالتك بوضوح.", voice="woman")
    response.record(
        action=url_for("process_recording", report_id=report_id, _external=True),
        method="POST",
        max_length=55,
        timeout=5,
        play_beep=True,
        finish_on_key="",
        trim="trim-silence"
    )
    response.say("لم يتم تسجيل أي رسالة. شكراً لك، مع السلامة.", voice="woman")
    response.hangup()
    return str(response)

@app.route("/process-recording/<int:report_id>", methods=["POST"])
def process_recording(report_id):
    recording_url = request.form.get("RecordingUrl")
    call_report = CallReport.query.get(report_id)
    if not call_report:
        return "Report not found", 404

    call_report.recording_url = recording_url
    call_report.status = "downloading"
    db.session.commit()

    transcript = ""
    category = "عام"

    if recording_url and TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN:
        try:
            audio_url = recording_url + ".mp3"
            auth = (TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
            resp = requests.get(audio_url, auth=auth, stream=True)
            if resp.status_code == 200:
                with tempfile.NamedTemporaryFile(delete=False, suffix='.mp3') as tmp_file:
                    for chunk in resp.iter_content(chunk_size=8192):
                        tmp_file.write(chunk)
                    tmp_path = tmp_file.name
                # Use Whisper for transcription
                try:
                    transcript = transcribe_audio_with_whisper(tmp_path)
                    category = classify_problem(transcript)
                    call_report.status = "transcribed"
                except Exception as e:
                    print(f"Whisper error: {e}")
                    call_report.status = "failed"
                finally:
                    os.unlink(tmp_path)
            else:
                call_report.status = "failed"
        except Exception as e:
            call_report.status = "error"
            print(f"Transcription error: {e}")
    else:
        call_report.status = "no_api_key"

    call_report.transcript = transcript
    call_report.problem_category = category
    db.session.commit()

    normal_report = Report(
        type=call_report.report_type,
        description=f"[مكالمة هاتفية] {transcript[:200]}" if transcript else "[لم يتم التعرف على الصوت]",
        status="جديد",
        user_id=call_report.user_id
    )
    db.session.add(normal_report)
    db.session.commit()

    response = VoiceResponse()
    if SUPPORT_AGENT_NUMBER:
        response.say("شكراً لك. جاري تحويلك إلى أحد المختصين، الرجاء الانتظار.", voice="woman")
        response.dial(SUPPORT_AGENT_NUMBER)
    else:
        response.say("تم استلام بلاغك بنجاح. سيتم الرد عليك لاحقاً. مع السلامة.", voice="woman")
    return str(response)

@app.route("/voice-incoming", methods=["POST"])
def voice_incoming():
    response = VoiceResponse()
    response.say("مرحباً بك في منصة أبلغ. الرجاء تسجيل الدخول إلى الموقع لاستخدام خدمة المكالمات.", voice="woman")
    return str(response)

# ------------------------------
# New: Emergency Voice Report (using Whisper)
# ------------------------------
@app.route("/emergency-voice-report", methods=["POST"])
@login_required
def emergency_voice_report():
    """Receive audio from chatbot, transcribe with Whisper, detect emergency, call support agent."""
    if 'audio' not in request.files:
        return jsonify({"error": "No audio file provided"}), 400

    audio_file = request.files['audio']
    if audio_file.filename == '':
        return jsonify({"error": "Empty audio file"}), 400

    # Save audio temporarily
    temp_audio = tempfile.NamedTemporaryFile(delete=False, suffix='.webm')
    audio_file.save(temp_audio.name)
    temp_audio.close()

    transcript = ""
    category = "عام"
    try:
        # Convert webm to wav using ffmpeg (Whisper can read webm directly but may need ffmpeg)
        # We'll use a temporary wav file for better compatibility
        wav_temp = tempfile.NamedTemporaryFile(delete=False, suffix='.wav')
        wav_temp.close()
        # Use ffmpeg to convert
        subprocess.run(['ffmpeg', '-i', temp_audio.name, '-acodec', 'pcm_s16le', '-ar', '16000', '-ac', '1', wav_temp.name],
                       check=True, capture_output=True)
        # Transcribe with Whisper
        transcript = transcribe_audio_with_whisper(wav_temp.name)
        os.unlink(wav_temp.name)
        category = classify_problem(transcript)
    except Exception as e:
        print(f"Transcription error: {e}")
        os.unlink(temp_audio.name)
        return jsonify({"error": "فشل التعرف على الصوت", "transcript": ""}), 500

    os.unlink(temp_audio.name)

    # Prepare response data
    response_data = {
        "transcript": transcript,
        "category": category,
        "emergency": category != "عام"
    }

    # If emergency, initiate call to support agent
    if category != "عام" and SUPPORT_AGENT_NUMBER and twilio_client:
        user = get_current_user()
        location_str = ""
        lat = session.get("temp_lat")
        lng = session.get("temp_lng")
        if lat and lng:
            location_str = f"الموقع: خط العرض {lat}, خط الطول {lng}. "
        message_body = (
            f"تنبيه طوارئ: تم استلام بلاغ صوتي من المستخدم {user.name if user else 'مجهول'} "
            f"يصف مشكلة من نوع {category}. {location_str}"
            f"النص: {transcript[:150]}..."
        )
        try:
            call = twilio_client.calls.create(
                to=SUPPORT_AGENT_NUMBER,
                from_=TWILIO_PHONE_NUMBER,
                twiml=f'<Response><Say voice="woman" language="ar">{message_body}</Say></Response>'
            )
            emergency = EmergencyCall(
                user_id=user.id if user else None,
                problem_category=category,
                transcript=transcript,
                location=f"lat:{lat},lng:{lng}" if lat and lng else None,
                call_sid=call.sid,
                status="completed"
            )
            db.session.add(emergency)
            db.session.commit()
            response_data["call_initiated"] = True
            response_data["call_sid"] = call.sid
        except Exception as e:
            print(f"Twilio call error: {e}")
            response_data["call_initiated"] = False
            response_data["call_error"] = str(e)
    else:
        response_data["call_initiated"] = False

    return jsonify(response_data)

# ------------------------------
# Run the app
# ------------------------------
if __name__ == "__main__":
    with app.app_context():
        db.create_all()
        admin = User.query.filter_by(email=ADMIN_EMAIL).first()
        if admin and not admin.is_admin:
            admin.is_admin = True
            db.session.commit()
            print(f"✅ تم تحويل {admin.email} لأدمن تلقائياً")
    app.run(debug=True)
