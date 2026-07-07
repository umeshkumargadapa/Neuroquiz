from flask import Flask, jsonify, render_template, request, redirect, session, flash
from flask_bcrypt import Bcrypt
import sqlite3
from datetime import datetime
import os
from dotenv import load_dotenv
from groq import Groq
import json

# -------------------- LOAD ENV --------------------
load_dotenv()
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
SECRET_KEY = os.getenv("SECRET_KEY", os.urandom(24).hex())

# -------------------- APP CONFIG --------------------
app = Flask(__name__)
app.secret_key = SECRET_KEY
bcrypt = Bcrypt(app)
client = Groq(api_key=GROQ_API_KEY)
DB_PATH = os.path.join(os.path.dirname(__file__), "database.db")

# -------------------- DB CONNECTION --------------------
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


# -------------------- HOME --------------------
@app.route("/")
def home():
    return render_template("home.html")


# -------------------- REGISTER --------------------
@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        username = request.form["username"]
        email = request.form["email"]
        password = request.form["password"]
        hashed_pw = bcrypt.generate_password_hash(password).decode("utf-8")

        conn = get_db()
        cursor = conn.cursor()
        try:
            cursor.execute("INSERT INTO users (username, email, password) VALUES (?, ?, ?)",
                           (username, email, hashed_pw))
            conn.commit()
            flash("Registration successful! Please login.", "success")
            return redirect("/login")
        except sqlite3.IntegrityError:
            flash("Username or Email already exists.", "danger")
        finally:
            conn.close()
    return render_template("register.html")


# -------------------- LOGIN --------------------
@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form["username"]
        password = request.form["password"]

        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM users WHERE username = ?", (username,))
        user = cursor.fetchone()
        conn.close()

        if user and bcrypt.check_password_hash(user["password"], password):
            session["user_id"] = user["id"]
            session["username"] = user["username"]
            return redirect("/dashboard")
        else:
            flash("Invalid credentials.", "danger")
    return render_template("login.html")


# -------------------- DASHBOARD --------------------
@app.route("/dashboard")
def dashboard():
    if "user_id" not in session:
        flash("Login first!", "warning")
        return redirect("/login")

    user_id = session["user_id"]
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT topic, score, total_questions, taken_at
        FROM quizzes
        WHERE user_id = ?
        ORDER BY taken_at DESC
    """, (user_id,))
    rows = cursor.fetchall()
    conn.close()

    if not rows:
        stats = {
            "total_quizzes": 0,
            "best_score": 0,
            "avg_score": 0,
            "last_attempt": "No attempts yet"
        }
        recent_activity_list = []
    else:
        total_quizzes = len(rows)
        best_score = max([r["score"] for r in rows])
        avg_score = round(sum([r["score"] for r in rows]) / total_quizzes, 2)
        last_attempt = rows[0]["taken_at"]
        recent_activity_list = [
            {
                "quiz_id": idx + 1,
                "topic": r["topic"],
                "score": r["score"],
                "total_questions": r["total_questions"],
                "taken_at": r["taken_at"]
            }
            for idx, r in enumerate(rows[:5])
        ]
        stats = {
            "total_quizzes": total_quizzes,
            "best_score": best_score,
            "avg_score": avg_score,
            "last_attempt": last_attempt
        }

    return render_template("dashboard.html",
                           username=session["username"],
                           stats=stats,
                           recent_activity=recent_activity_list)


# -------------------- QUIZ GENERATION --------------------
import json, random

def generate_quiz(topic, num_questions, difficulty):
    variation = random.randint(1, 9999)

    prompt = f"""
You are a creative quiz generator.
Generate {num_questions} unique, creative, non-repetitive {difficulty}-level multiple-choice questions 
about {topic}. Each question should be distinct from typical textbook ones.
Use randomness factor {variation} to diversify results.

Each question must have 4 options (A, B, C, D).
Return output strictly in **valid JSON** like this:
[
    {{
        "question": "Question text",
        "options": {{
            "A": "Option A text",
            "B": "Option B text",
            "C": "Option C text",
            "D": "Option D text"
        }},
        "correct": "A"
    }}
]
"""


    response = client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        messages = [
            {"role": "system", "content": "Return only raw JSON. No explanations, markdown, or text outside JSON."},
            {"role": "user", "content": prompt}
        ],
        temperature=0.9,  # higher creativity
    )

    content = response.choices[0].message.content.strip()
    if content.startswith("```"):
        content = content.split("```")[1].replace("json", "").strip("` \n")

    try:
        return json.loads(content)
    except json.JSONDecodeError:
        print("⚠️ Error parsing AI output:", content)
        return []



# -------------------- QUIZ SELECTION --------------------
@app.route("/select_quiz")
def select_quiz():
    if "user_id" not in session:
        flash("Login first!", "warning")
        return redirect("/login")
    return render_template("select_quiz.html")


# -------------------- START QUIZ --------------------
@app.route("/start_quiz", methods=["POST"])
def start_quiz():
    if "user_id" not in session:
        flash("Login first!", "warning")
        return redirect("/login")

    topic = request.form["topic"]
    num_questions = int(request.form["num_questions"])
    difficulty = request.form["difficulty"]
    questions = generate_quiz(topic, num_questions, difficulty)

    if not questions:
        flash("Failed to generate quiz. Try again.", "danger")
        return redirect("/dashboard")

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO quizzes (user_id, topic, score, total_questions, taken_at) VALUES (?, ?, ?, ?, datetime('now'))",
        (session["user_id"], topic, 0, num_questions)
    )
    quiz_id = cursor.lastrowid

    for q in questions:
        options = q["options"]
        cursor.execute("""
            INSERT INTO questions (quiz_id, question_text, option_a, option_b, option_c, option_d, correct_answer)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (quiz_id, q["question"], options["A"], options["B"], options["C"], options["D"], q["correct"]))

    conn.commit()
    conn.close()

    return render_template("quiz.html", quiz_id=quiz_id, questions=questions, topic=topic)


# -------------------- QUIZ RESULT --------------------
@app.route("/quiz_result", methods=["POST"])
def quiz_result():
    if "user_id" not in session:
        flash("Please login first!", "warning")
        return redirect("/login")

    quiz_id = request.form.get("quiz_id")
    score = int(request.form.get("score", 0))
    total_questions = int(request.form.get("total_questions", 0))

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("UPDATE quizzes SET score = ? WHERE id = ?", (score, quiz_id))
    conn.commit()
    conn.close()

    return render_template("result.html", score=score, total_questions=total_questions)


@app.route("/save_answer", methods=["POST"])
def save_answer():
    data = request.get_json()
    question_id = data.get("question_id")   # ✅ Make sure question_id is sent
    user_answer = data.get("user_answer")
    correct_answer = data.get("correct_answer")

    is_correct = 1 if user_answer == correct_answer else 0

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        UPDATE questions
        SET user_answer = ?, is_correct = ?
        WHERE id = ?
    """, (user_answer, is_correct, question_id))
    conn.commit()
    conn.close()

    return jsonify({"status": "success"})


# -------------------- HISTORY --------------------
@app.route("/history")
def history():
    if "user_id" not in session:
        flash("Login first!", "warning")
        return redirect("/login")

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT id, topic, score, total_questions, taken_at
        FROM quizzes
        WHERE user_id = ?
        ORDER BY taken_at DESC
    """, (session["user_id"],))
    rows = cursor.fetchall()
    conn.close()

    return render_template("history.html", history=rows)


# -------------------- LEADERBOARD --------------------
@app.route("/leaderboard")
def leaderboard():
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT users.username, quizzes.score, quizzes.total_questions, quizzes.taken_at
        FROM quizzes
        JOIN users ON quizzes.user_id = users.id
        ORDER BY quizzes.score DESC
        LIMIT 10
    """)
    top_scores = cursor.fetchall()
    conn.close()
    return render_template("leaderboard.html", leaderboard=top_scores)


# -------------------- LOGOUT --------------------
@app.route("/logout")
def logout():
    session.clear()
    flash("Logged out successfully.", "success")
    return redirect("/login")

import sqlite3, os

# ✅ Choose database path based on environment
if os.getenv("RENDER"):  # If running on Render
    DB_PATH = "/tmp/quiz.db"
else:
    DB_PATH = "database/quiz.db"

# ✅ Create directory only if using local path
if not os.getenv("RENDER"):
    os.makedirs("database", exist_ok=True)

conn = sqlite3.connect(DB_PATH)
c = conn.cursor()

# Users table
c.execute("""
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    email TEXT UNIQUE NOT NULL,
    password TEXT NOT NULL
)
""")

# Quizzes table
c.execute('''CREATE TABLE IF NOT EXISTS quizzes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    quiz_id INTEGER,
    topic TEXT,
    score INTEGER,
    total_questions INTEGER,
    taken_at TEXT DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES users(id)
)''')


# Questions table
c.execute('''CREATE TABLE IF NOT EXISTS questions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    quiz_id INTEGER,
    question_text TEXT,
    option_a TEXT,
    option_b TEXT,
    option_c TEXT,
    option_d TEXT,
    correct_answer TEXT,
    user_answer TEXT,
    is_correct INTEGER,
    FOREIGN KEY (quiz_id) REFERENCES quizzes(id)
)''')


conn.commit()
conn.close()
print("✅ Database initialized successfully!")

# -------------------- RUN --------------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
