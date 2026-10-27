import os
import sqlite3
from functools import wraps
from flask import Blueprint, request, session, jsonify
from flask_socketio import SocketIO, join_room, leave_room, emit

live_bp = Blueprint("live_api", __name__)
socketio = SocketIO(
    cors_allowed_origins=[],
    async_mode="threading"
)

DB_PATH = os.path.join(os.path.dirname(__file__), "hotlink.db")
MAX_MESSAGE_LENGTH = 300
active_viewers = {}


def db_connect():
    db = sqlite3.connect(DB_PATH, timeout=10)
    db.row_factory = sqlite3.Row
    return db


def init_live_db():
    with db_connect() as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS live_rooms (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            host_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'live',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            ended_at TEXT,
            FOREIGN KEY(host_id) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS live_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            room_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            body TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(room_id) REFERENCES live_rooms(id),
            FOREIGN KEY(user_id) REFERENCES users(id)
        );
        """)


def current_user(db):
    uid = session.get("user_id")
    if not uid:
        return None
    return db.execute(
        "SELECT id, username, photo FROM users WHERE id=?",
        (uid,)
    ).fetchone()


def socket_user():
    uid = session.get("user_id")
    if not uid:
        return None
    with db_connect() as db:
        return db.execute(
            "SELECT id, username, photo FROM users WHERE id=?",
            (uid,)
        ).fetchone()


@live_bp.get("/api/live/rooms")
def list_rooms():
    with db_connect() as db:
        rows = db.execute("""
            SELECT r.id, r.title, r.created_at,
                   u.username, u.photo
            FROM live_rooms r
            JOIN users u ON u.id = r.host_id
            WHERE r.status='live'
            ORDER BY r.id DESC
            LIMIT 100
        """).fetchall()

    return jsonify([
        {
            "id": r["id"],
            "title": r["title"],
            "host": r["username"],
            "photo": r["photo"],
            "created_at": r["created_at"],
            "viewers": len(active_viewers.get(str(r["id"]), set()))
        }
        for r in rows
    ])


@live_bp.post("/api/live/start")
def start_live():
    uid = session.get("user_id")
    if not uid:
        return jsonify(error="Login required"), 401

    data = request.get_json(silent=True) or {}
    title = str(data.get("title", "")).strip()[:100]
    if not title:
        return jsonify(error="Enter a LIVE title"), 400

    with db_connect() as db:
        user = db.execute(
            "SELECT id FROM users WHERE id=?", (uid,)
        ).fetchone()
        if not user:
            return jsonify(error="User not found"), 401

        existing = db.execute("""
            SELECT id FROM live_rooms
            WHERE host_id=? AND status='live'
        """, (uid,)).fetchone()

        if existing:
            room = db.execute(
                "SELECT id, title FROM live_rooms WHERE id=?",
                (existing["id"],)
            ).fetchone()
            return jsonify(
                room_id=room["id"],
                title=room["title"],
                already_live=True
            ), 200

        cur = db.execute("""
            INSERT INTO live_rooms(host_id, title, status)
            VALUES (?, ?, 'live')
        """, (uid, title))
        room_id = cur.lastrowid

    return jsonify(room_id=room_id, title=title), 201


@live_bp.post("/api/live/<int:room_id>/end")
def end_live(room_id):
    uid = session.get("user_id")
    if not uid:
        return jsonify(error="Login required"), 401

    with db_connect() as db:
        cur = db.execute("""
            UPDATE live_rooms
            SET status='ended', ended_at=CURRENT_TIMESTAMP
            WHERE id=? AND host_id=? AND status='live'
        """, (room_id, uid))

        if not cur.rowcount:
            return jsonify(error="Live room not found or not yours"), 404

    socketio.emit("live_ended", {"room_id": room_id},
                  to=str(room_id), namespace="/")
    active_viewers.pop(str(room_id), None)
    return jsonify(ok=True)


@live_bp.get("/api/live/<int:room_id>/messages")
def get_messages(room_id):
    with db_connect() as db:
        room = db.execute(
            "SELECT id FROM live_rooms WHERE id=? AND status='live'",
            (room_id,)
        ).fetchone()
        if not room:
            return jsonify(error="Live room not found"), 404

        rows = db.execute("""
            SELECT m.id, m.body, m.created_at, u.username, u.photo
            FROM live_messages m
            JOIN users u ON u.id=m.user_id
            WHERE m.room_id=?
            ORDER BY m.id DESC LIMIT 50
        """, (room_id,)).fetchall()

    return jsonify([dict(r) for r in reversed(rows)])


@live_bp.get("/api/live/<int:room_id>")
def live_room_details(room_id):
    with db_connect() as db:
        row = db.execute("""
            SELECT r.id, r.title, r.status, r.created_at,
                   u.id AS host_id, u.username, u.photo
            FROM live_rooms r JOIN users u ON u.id=r.host_id
            WHERE r.id=?
        """, (room_id,)).fetchone()

    if not row:
        return jsonify(error="Room not found"), 404

    result = dict(row)
    result["viewers"] = len(active_viewers.get(str(room_id), set()))
    return jsonify(result)


@socketio.on("join_live")
def on_join(data):
    user = socket_user()
    if not user:
        emit("live_error", {"error": "Please log in first"})
        return

    try:
        room_id = int((data or {}).get("room_id"))
    except (TypeError, ValueError):
        emit("live_error", {"error": "Invalid room"})
        return

    with db_connect() as db:
        room = db.execute("""
            SELECT id, title, host_id
            FROM live_rooms WHERE id=? AND status='live'
        """, (room_id,)).fetchone()

    if not room:
        emit("live_error", {"error": "This LIVE has ended"})
        return

    sid = request.sid
    key = str(room_id)
    join_room(key)
    active_viewers.setdefault(key, set()).add(sid)

    emit("joined_live", {
        "room_id": room_id,
        "title": room["title"],
        "host_id": room["host_id"],
        "username": user["username"],
        "viewers": len(active_viewers[key])
    })

    socketio.emit("viewer_joined", {"room_id": room_id, "sid": sid}, to=key, skip_sid=sid)
    emit("viewer_count", {"room_id": room_id, "count": len(active_viewers[key])},
         to=key)


@socketio.on("leave_live")
def on_leave(data):
    try:
        room_id = int((data or {}).get("room_id"))
    except (TypeError, ValueError):
        return

    key = str(room_id)
    leave_room(key)
    viewers = active_viewers.get(key, set())
    viewers.discard(request.sid)
    if not viewers:
        active_viewers.pop(key, None)

    socketio.emit(
        "viewer_count",
        {"room_id": room_id, "count": len(viewers)},
        to=key
    )


@socketio.on("live_message")
def on_live_message(data):
    user = socket_user()
    if not user:
        emit("live_error", {"error": "Please log in first"})
        return

    try:
        room_id = int((data or {}).get("room_id"))
    except (TypeError, ValueError):
        emit("live_error", {"error": "Invalid room"})
        return

    body = str((data or {}).get("body", "")).strip()
    if not body or len(body) > MAX_MESSAGE_LENGTH:
        emit("live_error", {"error": "Message must be 1–300 characters"})
        return

    with db_connect() as db:
        room = db.execute(
            "SELECT id FROM live_rooms WHERE id=? AND status='live'",
            (room_id,)
        ).fetchone()
        if not room:
            emit("live_error", {"error": "LIVE has ended"})
            return

        cur = db.execute("""
            INSERT INTO live_messages(room_id, user_id, body)
            VALUES (?, ?, ?)
        """, (room_id, user["id"], body))
        message_id = cur.lastrowid

    socketio.emit("live_message", {
        "id": message_id,
        "room_id": room_id,
        "username": user["username"],
        "photo": user["photo"],
        "body": body
    }, to=str(room_id))



@socketio.on("live_like")
def on_live_like(data):
    user = socket_user()
    if not user:
        emit("live_error", {"error": "Please log in first"})
        return

    try:
        room_id = int((data or {}).get("room_id"))
    except (TypeError, ValueError):
        emit("live_error", {"error": "Invalid room"})
        return

    with db_connect() as db:
        cols = {r["name"] for r in db.execute(
            "PRAGMA table_info(live_rooms)"
        )}
        if "likes_count" not in cols:
            emit("live_error", {"error": "Likes counter not initialized"})
            return

        cur = db.execute(
            "UPDATE live_rooms SET likes_count = likes_count + 1 "
            "WHERE id = ? AND status = 'live'",
            (room_id,)
        )
        if not cur.rowcount:
            emit("live_error", {"error": "LIVE has ended"})
            return

        row = db.execute(
            "SELECT likes_count FROM live_rooms WHERE id = ?",
            (room_id,)
        ).fetchone()
        count = row["likes_count"]

    socketio.emit("live_like", {
        "room_id": room_id, "username": user["username"]
    }, to=str(room_id))
    socketio.emit("like_count", {
        "room_id": room_id, "count": count
    }, to=str(room_id))



# HOTLINK_WEBRTC_SIGNALING_V1
# Host SID per active room; signaling is relayed only to room members.
active_hosts = {}

@socketio.on("register_live_host")
def register_live_host(data):
    user = socket_user()
    if not user:
        emit("live_error", {"error": "Please log in first"})
        return

    try:
        room_id = int((data or {}).get("room_id"))
    except (TypeError, ValueError):
        emit("live_error", {"error": "Invalid room"})
        return

    with db_connect() as db:
        room = db.execute(
            "SELECT host_id FROM live_rooms WHERE id=? AND status='live'",
            (room_id,)
        ).fetchone()

    if not room or str(room["host_id"]) != str(user["id"]):
        emit("live_error", {"error": "Only the host can register the stream"})
        return

    key = str(room_id)
    if request.sid not in active_viewers.get(key, set()):
        emit("live_error", {"error": "Join the LIVE room first"})
        return

    active_hosts[key] = request.sid
    socketio.emit("live_host_ready", {"room_id": room_id}, to=key,
                  skip_sid=request.sid)


@socketio.on("webrtc_offer")
def on_webrtc_offer(data):
    relay_webrtc_signal("webrtc_offer", data, host_only=True)


@socketio.on("webrtc_answer")
def on_webrtc_answer(data):
    relay_webrtc_signal("webrtc_answer", data, host_only=False)


@socketio.on("webrtc_ice")
def on_webrtc_ice(data):
    relay_webrtc_signal("webrtc_ice", data, host_only=False)


def relay_webrtc_signal(event_name, data, host_only=False):
    user = socket_user()
    if not user:
        emit("live_error", {"error": "Please log in first"})
        return

    data = data or {}
    try:
        room_id = int(data.get("room_id"))
    except (TypeError, ValueError):
        emit("live_error", {"error": "Invalid room"})
        return

    key = str(room_id)
    members = active_viewers.get(key, set())
    sender = request.sid
    target = str(data.get("target_sid", ""))

    if sender not in members or target not in members or target == sender:
        emit("live_error", {"error": "Invalid WebRTC participant"})
        return

    if host_only and active_hosts.get(key) != sender:
        emit("live_error", {"error": "Only the host can send an offer"})
        return

    if not host_only and sender != active_hosts.get(key):
        # Only the registered host and its viewers may exchange signals.
        # Answers and ICE may travel in either direction.
        if target != active_hosts.get(key):
            emit("live_error", {"error": "Invalid WebRTC signaling route"})
            return

    payload = {
        "room_id": room_id,
        "from_sid": sender,
        "target_sid": target,
        "description": data.get("description"),
        "candidate": data.get("candidate"),
    }
    socketio.emit(event_name, payload, to=target)


@socketio.on("disconnect")
def on_disconnect(reason=None):
    sid = request.sid
    for key, host_sid in list(active_hosts.items()):
        if host_sid == sid:
            active_hosts.pop(key, None)
    for key in list(active_viewers):
        viewers = active_viewers[key]
        if sid in viewers:
            viewers.discard(sid)
            if not viewers:
                active_viewers.pop(key, None)
            socketio.emit("viewer_count", {
                "room_id": int(key),
                "count": len(viewers)
            }, to=key)
