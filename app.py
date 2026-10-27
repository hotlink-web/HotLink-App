import os
import sqlite3
import uuid
from PIL import Image, UnidentifiedImageError
from functools import wraps
from datetime import datetime, timezone
from flask import (
    Flask, request, redirect, url_for, session,
    render_template, flash, abort
)
from werkzeug.security import generate_password_hash, check_password_hash

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "hotlink.db")

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get(
    "HOTLINK_SECRET_KEY",
    os.urandom(32).hex()
)
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024


def get_db():
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    return db


def init_db():
    with get_db() as db:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE COLLATE NOCASE,
                email TEXT NOT NULL UNIQUE COLLATE NOCASE,
                password_hash TEXT NOT NULL,
                age INTEGER NOT NULL CHECK(age >= 18),
                bio TEXT NOT NULL DEFAULT '',
                city TEXT NOT NULL DEFAULT '',
                photo TEXT NOT NULL DEFAULT '',
                visible INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS follows (
                follower_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                followed_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (follower_id, followed_id),
                CHECK (follower_id != followed_id)
            );

            CREATE TABLE IF NOT EXISTS friend_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sender_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                receiver_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                status TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'accepted', 'declined')),
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(sender_id, receiver_id),
                CHECK (sender_id != receiver_id)
            );


            CREATE TABLE IF NOT EXISTS stories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                caption TEXT NOT NULL DEFAULT '',
                media_path TEXT NOT NULL,
                media_type TEXT NOT NULL CHECK(media_type IN ('image', 'video')),
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                expires_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS posts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                body TEXT NOT NULL DEFAULT '',
                media_path TEXT NOT NULL DEFAULT '',
                media_type TEXT NOT NULL DEFAULT 'none'
                    CHECK (media_type IN ('none', 'image', 'video')),
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS post_likes (
                post_id INTEGER NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (post_id, user_id)
            );

            CREATE TABLE IF NOT EXISTS post_comments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                post_id INTEGER NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                body TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS likes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                liker_id INTEGER NOT NULL REFERENCES users(id)
                    ON DELETE CASCADE,
                liked_id INTEGER NOT NULL REFERENCES users(id)
                    ON DELETE CASCADE,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(liker_id, liked_id),
                CHECK(liker_id != liked_id)
            );

            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sender_id INTEGER NOT NULL REFERENCES users(id)
                    ON DELETE CASCADE,
                receiver_id INTEGER NOT NULL REFERENCES users(id)
                    ON DELETE CASCADE,
                body TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS blocks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                blocker_id INTEGER NOT NULL REFERENCES users(id)
                    ON DELETE CASCADE,
                blocked_id INTEGER NOT NULL REFERENCES users(id)
                    ON DELETE CASCADE,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(blocker_id, blocked_id),
                CHECK(blocker_id != blocked_id)
            );

            CREATE TABLE IF NOT EXISTS reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                reporter_id INTEGER NOT NULL REFERENCES users(id)
                    ON DELETE CASCADE,
                reported_id INTEGER NOT NULL REFERENCES users(id)
                    ON DELETE CASCADE,
                reason TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                CHECK(reporter_id != reported_id)
            );

            CREATE INDEX IF NOT EXISTS idx_blocks_pair
                ON blocks(blocker_id, blocked_id);

            CREATE INDEX IF NOT EXISTS idx_messages_pair
                ON messages(sender_id, receiver_id, id);

            CREATE INDEX IF NOT EXISTS idx_likes_liked
                ON likes(liked_id, liker_id);
        """)


def current_user():
    user_id = session.get("user_id")
    if not user_id:
        return None

    with get_db() as db:
        return db.execute(
            "SELECT * FROM users WHERE id = ?", (user_id,)
        ).fetchone()


def login_required(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        if not session.get("user_id"):
            flash("Please log in to continue.", "info")
            return redirect(url_for("login"))
        return fn(*args, **kwargs)
    return wrapped


@app.context_processor
def inject_globals():
    return {"me": current_user()}


@app.route("/")
def home():
    if not session.get("user_id"):
        return render_template("landing.html")

    return redirect(url_for("feed"))


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        age_text = request.form.get("age", "")
        consent = request.form.get("adult_consent")

        if not username or len(username) < 3:
            flash("Your username must have at least 3 characters.", "error")
            return redirect(url_for("register"))

        if len(username) > 30 or not all(
            c.isalnum() or c in "_-" for c in username
        ):
            flash("Use only letters, numbers, underscores or hyphens.", "error")
            return redirect(url_for("register"))

        if "@" not in email or len(email) > 254:
            flash("Enter a valid email address.", "error")
            return redirect(url_for("register"))

        if len(password) < 10:
            flash("Your password must contain at least 10 characters.", "error")
            return redirect(url_for("register"))

        try:
            age = int(age_text)
        except (ValueError, TypeError):
            age = 0

        if age < 18 or not consent:
            flash("HotLink is for adults aged 18 and older.", "error")
            return redirect(url_for("register"))

        try:
            with get_db() as db:
                cur = db.execute("""
                    INSERT INTO users
                    (username, email, password_hash, age)
                    VALUES (?, ?, ?, ?)
                """, (
                    username,
                    email,
                    generate_password_hash(password),
                    age
                ))
                user_id = cur.lastrowid

            session.clear()
            session["user_id"] = user_id
            flash("Welcome to HotLink! Complete your profile.", "success")
            return redirect(url_for("profile"))

        except sqlite3.IntegrityError:
            flash("That username or email is already registered.", "error")
            return redirect(url_for("register"))

    return render_template("auth.html", mode="register")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        identity = request.form.get("identity", "").strip()
        password = request.form.get("password", "")

        with get_db() as db:
            user = db.execute("""
                SELECT * FROM users
                WHERE username = ? COLLATE NOCASE
                   OR email = ? COLLATE NOCASE
            """, (identity, identity)).fetchone()

        if user and check_password_hash(user["password_hash"], password):
            session.clear()
            session["user_id"] = user["id"]
            return redirect(url_for("browse"))

        flash("Incorrect username, email or password.", "error")
        return redirect(url_for("login"))

    return render_template("auth.html", mode="login")


@app.route("/logout", methods=["POST"])
@login_required
def logout():
    session.clear()
    flash("You have logged out.", "success")
    return redirect(url_for("home"))


@app.route("/browse")
@login_required
def browse():
    me = current_user()
    search = request.args.get("q", "").strip()[:60]

    with get_db() as db:
        sql = """
            SELECT u.*,
                EXISTS(
                    SELECT 1 FROM likes l
                    WHERE l.liker_id = ? AND l.liked_id = u.id
                ) AS already_liked,
                EXISTS(
                    SELECT 1 FROM likes l
                    WHERE l.liker_id = u.id AND l.liked_id = ?
                ) AS likes_me
            FROM users u
            WHERE u.id != ? AND u.visible = 1

              AND NOT EXISTS (
                SELECT 1 FROM blocks bl
                WHERE (bl.blocker_id = ? AND bl.blocked_id = u.id)
                   OR (bl.blocker_id = u.id AND bl.blocked_id = ?)
              )
        """
        args = [me["id"], me["id"], me["id"], me["id"], me["id"]]

        if search:
            sql += " AND (u.username LIKE ? OR u.city LIKE ?)"
            args.extend([f"%{search}%", f"%{search}%"])

        sql += " ORDER BY u.created_at DESC LIMIT 100"
        users = db.execute(sql, args).fetchall()

    return render_template("dashboard.html", page="Browse", users=users)


@app.route("/like/<int:user_id>", methods=["POST"])
@login_required
def like(user_id):
    me = current_user()

    if user_id == me["id"]:
        abort(400)

    with get_db() as db:
        target = db.execute(
            "SELECT id FROM users WHERE id = ? AND visible = 1",
            (user_id,)
        ).fetchone()

        if not target:
            abort(404)

        blocked = db.execute("""
            SELECT 1 FROM blocks
            WHERE (blocker_id = ? AND blocked_id = ?)
               OR (blocker_id = ? AND blocked_id = ?)
            LIMIT 1
        """, (me["id"], user_id, user_id, me["id"])).fetchone()
        if blocked:
            flash("You cannot like this account.", "error")
            return redirect(request.referrer or url_for("browse"))

        db.execute("""
            INSERT OR IGNORE INTO likes (liker_id, liked_id)
            VALUES (?, ?)
        """, (me["id"], user_id))

        mutual = db.execute("""
            SELECT 1 FROM likes
            WHERE liker_id = ? AND liked_id = ?
        """, (user_id, me["id"])).fetchone()

    if mutual:
        flash("It's a mutual match! You can now message each other.", "success")
    else:
        flash("Like saved.", "success")

    return redirect(request.referrer or url_for("browse"))


@app.route("/unlike/<int:user_id>", methods=["POST"])
@login_required
def unlike(user_id):
    me = current_user()

    with get_db() as db:
        db.execute("""
            DELETE FROM likes WHERE liker_id = ? AND liked_id = ?
        """, (me["id"], user_id))

    flash("Like removed.", "success")
    return redirect(request.referrer or url_for("browse"))


@app.route("/matches")
@login_required
def matches():
    me = current_user()

    with get_db() as db:
        users = db.execute("""
            SELECT u.*
            FROM users u
            JOIN likes a ON a.liked_id = u.id AND a.liker_id = ?
            JOIN likes b ON b.liker_id = u.id AND b.liked_id = ?
            WHERE u.visible = 1

              AND NOT EXISTS (
                SELECT 1 FROM blocks bl
                WHERE (bl.blocker_id = ? AND bl.blocked_id = u.id)
                   OR (bl.blocker_id = u.id AND bl.blocked_id = ?)
              )
            ORDER BY a.created_at DESC
        """, (me["id"], me["id"], me["id"], me["id"])).fetchall()

    return render_template("dashboard.html", page="Matches", users=users)


@app.route("/likes")
@login_required
def likes():
    me = current_user()

    with get_db() as db:
        users = db.execute("""
            SELECT u.*,
                EXISTS(
                    SELECT 1 FROM likes b
                    WHERE b.liker_id = u.id AND b.liked_id = ?
                ) AS mutual
            FROM users u
            JOIN likes a ON a.liker_id = u.id AND a.liked_id = ?
            WHERE u.visible = 1

              AND NOT EXISTS (
                SELECT 1 FROM blocks bl
                WHERE (bl.blocker_id = ? AND bl.blocked_id = u.id)
                   OR (bl.blocker_id = u.id AND bl.blocked_id = ?)
              )
            ORDER BY a.created_at DESC
        """, (me["id"], me["id"], me["id"], me["id"])).fetchall()

    return render_template("dashboard.html", page="Likes", users=users)


@app.route("/messages")
@login_required
def messages():
    me = current_user()

    with get_db() as db:
        conversations = db.execute("""
            SELECT u.id, u.username, u.photo,
                (SELECT body FROM messages m
                 WHERE (m.sender_id = u.id AND m.receiver_id = ?)
                    OR (m.sender_id = ? AND m.receiver_id = u.id)
                 ORDER BY m.id DESC LIMIT 1) AS last_message,
                (SELECT created_at FROM messages m
                 WHERE (m.sender_id = u.id AND m.receiver_id = ?)
                    OR (m.sender_id = ? AND m.receiver_id = u.id)
                 ORDER BY m.id DESC LIMIT 1) AS last_time
            FROM users u
            WHERE u.id != ?

              AND NOT EXISTS (
                SELECT 1 FROM blocks bl
                WHERE (bl.blocker_id = ? AND bl.blocked_id = u.id)
                   OR (bl.blocker_id = u.id AND bl.blocked_id = ?)
              )
              AND EXISTS(
                SELECT 1 FROM messages m
                WHERE (m.sender_id = u.id AND m.receiver_id = ?)
                   OR (m.sender_id = ? AND m.receiver_id = u.id)
              )
            ORDER BY last_time DESC
        """, (
            me["id"], me["id"], me["id"], me["id"],
            me["id"], me["id"], me["id"],
            me["id"], me["id"]
        )).fetchall()

    return render_template(
        "dashboard.html", page="Messages", conversations=conversations
    )


@app.route("/chat/<int:user_id>", methods=["GET", "POST"])
@login_required
def chat(user_id):
    me = current_user()

    if user_id == me["id"]:
        abort(400)

    with get_db() as db:
        other = db.execute("""
            SELECT * FROM users WHERE id = ? AND visible = 1
        """, (user_id,)).fetchone()

        if not other:
            abort(404)

        blocked = db.execute("""
            SELECT 1 FROM blocks
            WHERE (blocker_id = ? AND blocked_id = ?)
               OR (blocker_id = ? AND blocked_id = ?)
            LIMIT 1
        """, (me["id"], user_id, user_id, me["id"])).fetchone()

        if blocked:
            flash("This conversation is unavailable.", "error")
            return redirect(url_for("messages"))

        allowed = db.execute("""
            SELECT 1 FROM likes
            WHERE liker_id = ? AND liked_id = ?
        """, (me["id"], user_id)).fetchone()

        mutual = db.execute("""
            SELECT 1 FROM likes
            WHERE liker_id = ? AND liked_id = ?
        """, (user_id, me["id"])).fetchone()

        if not (allowed and mutual):
            flash("You can message someone after you match.", "error")
            return redirect(url_for("matches"))

        if request.method == "POST":
            body = request.form.get("body", "").strip()

            if not body or len(body) > 2000:
                flash("Messages must contain 1–2000 characters.", "error")
            else:
                db.execute("""
                    INSERT INTO messages (sender_id, receiver_id, body)
                    VALUES (?, ?, ?)
                """, (me["id"], user_id, body))

            return redirect(url_for("chat", user_id=user_id))

        history = db.execute("""
            SELECT * FROM messages
            WHERE (sender_id = ? AND receiver_id = ?)
               OR (sender_id = ? AND receiver_id = ?)
            ORDER BY id ASC LIMIT 500
        """, (me["id"], user_id, user_id, me["id"])).fetchall()

    return render_template(
        "dashboard.html",
        page="Chat",
        other=other,
        history=history
    )



@app.route("/user/<int:user_id>")
@login_required
def public_profile(user_id):
    me = current_user()

    if user_id == me["id"]:
        return redirect(url_for("profile"))

    with get_db() as db:
        other = db.execute("""
            SELECT * FROM users
            WHERE id = ? AND visible = 1
        """, (user_id,)).fetchone()

        if not other:
            abort(404)

        blocked = db.execute("""
            SELECT 1 FROM blocks
            WHERE (blocker_id = ? AND blocked_id = ?)
               OR (blocker_id = ? AND blocked_id = ?)
            LIMIT 1
        """, (me["id"], user_id, user_id, me["id"])).fetchone()

        if blocked:
            abort(404)

        already_liked = db.execute("""
            SELECT 1 FROM likes
            WHERE liker_id = ? AND liked_id = ?
        """, (me["id"], user_id)).fetchone()

        mutual = db.execute("""
            SELECT 1 FROM likes
            WHERE liker_id = ? AND liked_id = ?
        """, (user_id, me["id"])).fetchone()

        following = db.execute("""
            SELECT 1 FROM follows
            WHERE follower_id = ? AND followed_id = ?
        """, (me["id"], user_id)).fetchone()

        friend_request = db.execute("""
            SELECT * FROM friend_requests
            WHERE (sender_id = ? AND receiver_id = ?)
               OR (sender_id = ? AND receiver_id = ?)
        """, (me["id"], user_id, user_id, me["id"])).fetchone()

    return render_template(
        "dashboard.html",
        page="PublicProfile",
        other=other,
        already_liked=bool(already_liked),
        mutual=bool(mutual),
        following=bool(following),
        friend_request=friend_request,
    )


@app.route("/follow/<int:user_id>", methods=["POST"])
@login_required
def follow_user(user_id):
    me = current_user()

    if user_id == me["id"]:
        abort(400)

    with get_db() as db:
        target = db.execute(
            "SELECT id FROM users WHERE id = ? AND visible = 1",
            (user_id,)
        ).fetchone()

        if not target:
            abort(404)

        blocked = db.execute("""
            SELECT 1 FROM blocks
            WHERE (blocker_id = ? AND blocked_id = ?)
               OR (blocker_id = ? AND blocked_id = ?)
            LIMIT 1
        """, (me["id"], user_id, user_id, me["id"])).fetchone()

        if blocked:
            flash("You cannot follow this account.", "error")
        else:
            existing = db.execute("""
                SELECT 1 FROM follows
                WHERE follower_id = ? AND followed_id = ?
            """, (me["id"], user_id)).fetchone()

            if existing:
                db.execute("""
                    DELETE FROM follows
                    WHERE follower_id = ? AND followed_id = ?
                """, (me["id"], user_id))
                flash("You unfollowed this member.", "success")
            else:
                db.execute("""
                    INSERT INTO follows (follower_id, followed_id)
                    VALUES (?, ?)
                """, (me["id"], user_id))
                flash("You are now following this member.", "success")

    return redirect(url_for("public_profile", user_id=user_id))


@app.route("/friend-request/<int:user_id>", methods=["POST"])
@login_required
def send_friend_request(user_id):
    me = current_user()

    if user_id == me["id"]:
        abort(400)

    with get_db() as db:
        target = db.execute(
            "SELECT id FROM users WHERE id = ? AND visible = 1",
            (user_id,)
        ).fetchone()

        if not target:
            abort(404)

        blocked = db.execute("""
            SELECT 1 FROM blocks
            WHERE (blocker_id = ? AND blocked_id = ?)
               OR (blocker_id = ? AND blocked_id = ?)
            LIMIT 1
        """, (me["id"], user_id, user_id, me["id"])).fetchone()

        if blocked:
            flash("You cannot send a friend request to this account.", "error")
        else:
            existing = db.execute("""
                SELECT * FROM friend_requests
                WHERE (sender_id = ? AND receiver_id = ?)
                   OR (sender_id = ? AND receiver_id = ?)
            """, (me["id"], user_id, user_id, me["id"])).fetchone()

            if existing and existing["status"] == "accepted":
                flash("You are already friends.", "success")
            elif existing and existing["status"] == "pending":
                flash("A friend request is already pending.", "error")
            elif existing:
                db.execute("""
                    DELETE FROM friend_requests WHERE id = ?
                """, (existing["id"],))
                db.execute("""
                    INSERT INTO friend_requests (sender_id, receiver_id)
                    VALUES (?, ?)
                """, (me["id"], user_id))
                flash("Friend request sent.", "success")
            else:
                db.execute("""
                    INSERT INTO friend_requests (sender_id, receiver_id)
                    VALUES (?, ?)
                """, (me["id"], user_id))
                flash("Friend request sent.", "success")

    return redirect(url_for("public_profile", user_id=user_id))


@app.route("/friend-request/respond/<int:request_id>", methods=["POST"])
@login_required
def respond_friend_request(request_id):
    me = current_user()
    action = request.form.get("action", "")

    if action not in ("accept", "decline"):
        abort(400)

    with get_db() as db:
        req = db.execute("""
            SELECT * FROM friend_requests
            WHERE id = ? AND receiver_id = ? AND status = 'pending'
        """, (request_id, me["id"])).fetchone()

        if not req:
            abort(404)

        blocked = db.execute("""
            SELECT 1 FROM blocks
            WHERE (blocker_id = ? AND blocked_id = ?)
               OR (blocker_id = ? AND blocked_id = ?)
            LIMIT 1
        """, (me["id"], req["sender_id"], req["sender_id"], me["id"])).fetchone()

        if blocked:
            abort(403)

        status = "accepted" if action == "accept" else "declined"
        db.execute("""
            UPDATE friend_requests SET status = ? WHERE id = ?
        """, (status, request_id))

        flash(
            "Friend request accepted." if action == "accept"
            else "Friend request declined.",
            "success"
        )

    return redirect(url_for("public_profile", user_id=req["sender_id"]))




@app.route("/profile/cover", methods=["POST"])
@login_required
def upload_cover_photo():
    me = current_user()
    uploaded = request.files.get("cover_photo")

    if not uploaded or not uploaded.filename:
        flash("Choose a cover photo first.", "error")
        return redirect(url_for("profile"))

    try:
        image = Image.open(uploaded.stream)
        image.verify()
        uploaded.stream.seek(0)
        image = Image.open(uploaded.stream)

        if image.format not in ("JPEG", "PNG", "WEBP"):
            flash("Use a JPG, PNG or WEBP cover photo.", "error")
            return redirect(url_for("profile"))

        if image.width * image.height > 16000000:
            flash("Cover image is too large.", "error")
            return redirect(url_for("profile"))

        image.thumbnail((1800, 1200))
        if image.mode != "RGB":
            image = image.convert("RGB")

        filename = uuid.uuid4().hex + "_cover.jpg"
        upload_dir = os.path.join(app.root_path, "static", "uploads")
        os.makedirs(upload_dir, exist_ok=True)
        image.save(
            os.path.join(upload_dir, filename),
            format="JPEG", quality=85, optimize=True
        )
        cover = "/static/uploads/" + filename

        with get_db() as db:
            db.execute(
                "UPDATE users SET cover_photo=? WHERE id=?",
                (cover, me["id"])
            )
            db.commit()

        flash("Cover photo updated!", "success")
    except (UnidentifiedImageError, OSError, ValueError,
            Image.DecompressionBombError):
        flash("That image could not be processed.", "error")

    return redirect(url_for("profile"))

@app.route("/profile", methods=["GET", "POST"])
@login_required
def profile():
    me = current_user()

    if request.method == "POST":
        bio = request.form.get("bio", "").strip()[:500]
        city = request.form.get("city", "").strip()[:100]
        uploaded = request.files.get("photo")

        photo = me["photo"] or ""

        if uploaded and uploaded.filename:
            try:
                # Decode and validate the actual image.
                image = Image.open(uploaded.stream)
                image.verify()

                uploaded.stream.seek(0)
                image = Image.open(uploaded.stream)

                if image.format not in ("JPEG", "PNG", "WEBP"):
                    flash("Upload a JPG, PNG or WEBP image.", "error")
                    return redirect(url_for("profile"))

                if image.width < 1 or image.height < 1:
                    flash("Invalid image dimensions.", "error")
                    return redirect(url_for("profile"))

                if image.width * image.height > 16000000:
                    flash("Image is too large in pixel dimensions.", "error")
                    return redirect(url_for("profile"))

                # Re-encode the image to avoid storing arbitrary file content.
                image.thumbnail((1200, 1200))
                if image.mode not in ("RGB",):
                    image = image.convert("RGB")

                filename = uuid.uuid4().hex + ".jpg"
                upload_dir = os.path.join(
                    app.root_path, "static", "uploads"
                )
                os.makedirs(upload_dir, exist_ok=True)

                destination = os.path.join(upload_dir, filename)
                image.save(destination, format="JPEG", quality=85, optimize=True)

                photo = "/static/uploads/" + filename

            except (
                UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError
            ):
                flash("That image could not be processed. Try another photo.", "error")
                return redirect(url_for("profile"))

        with get_db() as db:
            db.execute("""
                UPDATE users
                SET bio = ?, city = ?, photo = ?
                WHERE id = ?
            """, (bio, city, photo, me["id"]))

        flash("Your profile has been updated.", "success")
        return redirect(url_for("profile"))

    return render_template("dashboard.html", page="Profile")


@app.route("/settings", methods=["GET", "POST"])
@login_required
def settings():
    me = current_user()

    if request.method == "POST":
        visible = 1 if request.form.get("visible") == "on" else 0

        with get_db() as db:
            db.execute(
                "UPDATE users SET visible = ? WHERE id = ?",
                (visible, me["id"])
            )

        flash("Privacy settings saved.", "success")
        return redirect(url_for("settings"))

    return render_template("dashboard.html", page="Settings")


@app.route("/nearby")
@login_required
def nearby():
    # This uses the city entered in the user's profile.
    # Exact GPS-based discovery requires explicit location permission.
    me = current_user()

    if not me["city"]:
        flash("Add your city to your profile to discover nearby people.", "info")
        return redirect(url_for("profile"))

    with get_db() as db:
        users = db.execute("""
            SELECT u.* FROM users u
            WHERE u.id != ? AND u.visible = 1

              AND NOT EXISTS (
                SELECT 1 FROM blocks bl
                WHERE (bl.blocker_id = ? AND bl.blocked_id = u.id)
                   OR (bl.blocker_id = u.id AND bl.blocked_id = ?)
              )
              AND u.city != '' AND u.city = ? COLLATE NOCASE
            ORDER BY created_at DESC
            LIMIT 100
        """, (me["id"], me["id"], me["id"], me["city"])).fetchall()

    return render_template("dashboard.html", page="Nearby", users=users)


@app.route("/vip")
@login_required
def vip():
    return render_template("dashboard.html", page="VIP")



@app.route("/block/<int:user_id>", methods=["POST"])
@login_required
def block_user(user_id):
    me = current_user()

    if user_id == me["id"]:
        abort(400)

    with get_db() as db:
        target = db.execute(
            "SELECT id FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        if not target:
            abort(404)

        db.execute("""
            INSERT OR IGNORE INTO blocks (blocker_id, blocked_id)
            VALUES (?, ?)
        """, (me["id"], user_id))

        db.execute("""
            DELETE FROM likes
            WHERE (liker_id = ? AND liked_id = ?)
               OR (liker_id = ? AND liked_id = ?)
        """, (me["id"], user_id, user_id, me["id"]))

    flash("User blocked and match removed.", "success")
    return redirect(url_for("messages"))


@app.route("/report/<int:user_id>", methods=["POST"])
@login_required
def report_user(user_id):
    me = current_user()
    reason = request.form.get("reason", "").strip()

    if user_id == me["id"]:
        abort(400)

    if not 5 <= len(reason) <= 500:
        flash("Report reason must contain 5–500 characters.", "error")
        return redirect(url_for("chat", user_id=user_id))

    with get_db() as db:
        target = db.execute(
            "SELECT id FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        if not target:
            abort(404)

        db.execute("""
            INSERT INTO reports (reporter_id, reported_id, reason)
            VALUES (?, ?, ?)
        """, (me["id"], user_id, reason))

    flash("Report recorded for administrator review.", "success")
    return redirect(url_for("messages"))







@app.route("/stories/create", methods=["GET", "POST"])
@login_required
def create_story():
    if request.method == "GET":
        return render_template("create_story.html")

    import os
    from datetime import timedelta
    from werkzeug.utils import secure_filename

    media = request.files.get("media")
    caption = request.form.get("caption", "").strip()

    if not media or not media.filename:
        flash("Choose a photo or video for your story.", "error")
        return redirect(url_for("create_story"))

    filename = secure_filename(media.filename)
    ext = os.path.splitext(filename)[1].lower()
    image_exts = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
    video_exts = {".mp4", ".webm", ".mov"}

    if ext in image_exts:
        media_type = "image"
        try:
            from PIL import Image
            media.stream.seek(0)
            with Image.open(media.stream) as img:
                img.verify()
            media.stream.seek(0)
        except Exception:
            flash("That image file is invalid.", "error")
            return redirect(url_for("create_story"))
    elif ext in video_exts:
        media_type = "video"
    else:
        flash("Choose a JPG, PNG, GIF, WEBP, MP4, WEBM, or MOV file.", "error")
        return redirect(url_for("create_story"))

    os.makedirs("static/uploads/stories", exist_ok=True)
    stored_name = f"{uuid.uuid4().hex}{ext}"
    media.save(os.path.join("static/uploads/stories", stored_name))
    media_path = f"uploads/stories/{stored_name}"

    now = datetime.now(timezone.utc)
    created_at = now.isoformat(timespec="seconds")
    expires_at = (now + timedelta(hours=24)).isoformat(timespec="seconds")

    with get_db() as db:
        db.execute(
            """INSERT INTO stories
               (user_id, caption, media_path, media_type, created_at, expires_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (session["user_id"], caption, media_path, media_type,
             created_at, expires_at)
        )

    flash("Your story was published!", "success")
    return redirect(url_for("feed"))




@app.route("/admin/verification", methods=["GET", "POST"])
@login_required
def manage_verification():
    me = current_user()
    if not me or me["username"].lower() != "hotlink":
        abort(403)

    with get_db() as db:
        if request.method == "POST":
            user_id = request.form.get("user_id", type=int)
            action = request.form.get("action", "")
            if user_id and action in ("verify", "unverify"):
                db.execute(
                    "UPDATE users SET is_verified=? WHERE id=?",
                    (1 if action == "verify" else 0, user_id)
                )
                db.commit()
                flash("Verification status updated.", "success")
                return redirect(url_for("manage_verification"))

        members = db.execute(
            "SELECT id, username, is_verified FROM users ORDER BY username"
        ).fetchall()

    return render_template(
        "verification_admin.html", members=members
    )

@app.route("/feed")
@login_required
def feed():
    with get_db() as db:
        posts = db.execute(
            """
            SELECT posts.*,
                   users.username,
                   users.is_verified,
                   (SELECT COUNT(*) FROM post_likes
                    WHERE post_likes.post_id = posts.id) AS like_count,
                   (SELECT COUNT(*) FROM post_comments
                    WHERE post_comments.post_id = posts.id) AS comment_count,
                   EXISTS(
                       SELECT 1 FROM post_likes
                       WHERE post_likes.post_id = posts.id
                         AND post_likes.user_id = ?
                   ) AS liked_by_me
            FROM posts
            JOIN users ON users.id = posts.user_id
            ORDER BY posts.created_at DESC, posts.id DESC
            LIMIT 100
            """,
            (session["user_id"],)
        ).fetchall()

        post_comments = {}
        for post in posts:
            post_comments[post["id"]] = db.execute(
                """
                SELECT post_comments.*, users.username, users.is_verified
                FROM post_comments
                JOIN users ON users.id = post_comments.user_id
                WHERE post_comments.post_id = ?
                ORDER BY post_comments.created_at ASC, post_comments.id ASC
                """,
                (post["id"],)
            ).fetchall()

    return render_template(
        "dashboard.html",
        page="Feed",
        posts=posts,
        post_comments=post_comments
    )



@app.route("/posts/<int:post_id>/like", methods=["POST"])
@login_required
def like_post(post_id):
    user_id = session["user_id"]

    with get_db() as db:
        post = db.execute(
            "SELECT id FROM posts WHERE id = ?", (post_id,)
        ).fetchone()

        if post is None:
            flash("Post not found.", "error")
            return redirect(request.referrer or url_for("feed"))

        existing = db.execute(
            "SELECT 1 FROM post_likes WHERE post_id = ? AND user_id = ?",
            (post_id, user_id)
        ).fetchone()

        if existing:
            db.execute(
                "DELETE FROM post_likes WHERE post_id = ? AND user_id = ?",
                (post_id, user_id)
            )
        else:
            db.execute(
                "INSERT INTO post_likes (post_id, user_id) VALUES (?, ?)",
                (post_id, user_id)
            )

    return redirect(request.referrer or url_for("feed"))

@app.route("/posts/create", methods=["GET"])
@login_required
def create_post_page():
    return render_template("create_post.html")


@app.route("/posts/create", methods=["POST"])
@login_required
def create_post():
    import os
    from werkzeug.utils import secure_filename

    body = request.form.get("body", "").strip()
    media = request.files.get("media")
    media_path = ""
    media_type = "none"

    if not body and (not media or not media.filename):
        flash("Write something or choose a photo/video.", "error")
        return redirect(request.referrer or url_for("browse"))

    if media and media.filename:
        filename = secure_filename(media.filename)
        ext = os.path.splitext(filename)[1].lower()

        image_exts = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
        video_exts = {".mp4", ".webm", ".mov"}

        if ext in image_exts:
            media_type = "image"
        elif ext in video_exts:
            media_type = "video"
        else:
            flash("Choose a JPG, PNG, GIF, WEBP, MP4, WEBM, or MOV file.", "error")
            return redirect(request.referrer or url_for("browse"))

        if media_type == "image":
            try:
                from PIL import Image
                media.stream.seek(0)
                with Image.open(media.stream) as img:
                    img.verify()
                media.stream.seek(0)
            except Exception:
                flash("That image file is invalid.", "error")
                return redirect(request.referrer or url_for("browse"))

        os.makedirs("static/uploads/posts", exist_ok=True)
        stored_name = f"{uuid.uuid4().hex}{ext}"
        media.save(os.path.join("static/uploads/posts", stored_name))
        media_path = f"uploads/posts/{stored_name}"

    with get_db() as db:
        db.execute(
            """INSERT INTO posts (user_id, body, media_path, media_type)
               VALUES (?, ?, ?, ?)""",
            (session["user_id"], body, media_path, media_type)
        )

    flash("Your post was published!", "success")
    return redirect(url_for("feed"))


@app.route("/posts/<int:post_id>/comment", methods=["POST"])
@login_required
def add_post_comment(post_id):
    body = request.form.get("body", "").strip()

    if not body:
        flash("Write a comment first.", "error")
        return redirect(request.referrer or url_for("feed"))

    if len(body) > 2000:
        flash("Comments must be 2,000 characters or fewer.", "error")
        return redirect(request.referrer or url_for("feed"))

    with get_db() as db:
        post = db.execute(
            "SELECT id FROM posts WHERE id = ?", (post_id,)
        ).fetchone()

        if post is None:
            flash("Post not found.", "error")
            return redirect(url_for("feed"))

        db.execute(
            """INSERT INTO post_comments (post_id, user_id, body)
               VALUES (?, ?, ?)""",
            (post_id, session["user_id"], body)
        )

    flash("Comment added.", "success")
    return redirect(request.referrer or url_for("feed"))


@app.route("/health")
def health():
    return {"status": "ok", "database": os.path.exists(DB_PATH)}


with app.app_context():
    init_db()


@app.route("/live")
def hotlink_live_page():
    return render_template("live.html")



# LIVE_BACKEND_INTEGRATED
from live_backend import live_bp, socketio, init_live_db

app.register_blueprint(live_bp)
socketio.init_app(app)
init_live_db()

if __name__ == "__main__":
    socketio.run(
        app,
        host="127.0.0.1",
        port=5000,
        debug=False,
        allow_unsafe_werkzeug=True
    )


