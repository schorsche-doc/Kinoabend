from pathlib import Path
import random
import sqlite3
from datetime import date, datetime

from flask import Flask, flash, redirect, render_template, request, session, url_for


BASE_DIR = Path(__file__).resolve().parent
DATABASE_PATH = BASE_DIR / "kinoabend.sqlite3"

app = Flask(__name__)
app.config["SECRET_KEY"] = "kinoabend-local-secret"
ADMIN_PASSWORD = "admin"
TODAY = date.today().isoformat()


def valid_evening_date(value):
    try:
        return datetime.strptime(value, "%Y-%m-%d").date().isoformat()
    except (TypeError, ValueError):
        return TODAY


def stage_url(stage_number, evening_date):
    return url_for("stage", stage=stage_number, date=evening_date)


def setting_key(name, evening_date):
    return f"{name}:{evening_date}"


def get_latest_evening_date(connection):
    setting = connection.execute(
        "SELECT value FROM settings WHERE key = 'latest_evening_date'"
    ).fetchone()
    return valid_evening_date(setting["value"] if setting else TODAY)


def remember_evening_date(connection, evening_date):
    connection.execute(
        "INSERT INTO settings (key, value) VALUES ('latest_evening_date', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (evening_date,),
    )


def calculate_weight(user, next_round):
    if user["last_won_round"] is None:
        no_win_bonus = 4
    else:
        no_win_bonus = min(4, max(0, next_round - user["last_won_round"] - 1))
    return max(1, 1 + user["attendance_points"] + no_win_bonus)


def get_db():
    connection = sqlite3.connect(DATABASE_PATH)
    connection.row_factory = sqlite3.Row
    return connection


def init_db():
    with get_db() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                attendance_points INTEGER NOT NULL DEFAULT 0,
                pending_penalty INTEGER NOT NULL DEFAULT 0,
                last_won_round INTEGER
            );

            CREATE TABLE IF NOT EXISTS suggestions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                film_title TEXT NOT NULL,
                suggested_by TEXT NOT NULL,
                suggested_by_id INTEGER REFERENCES users(id),
                slogan TEXT NOT NULL DEFAULT '',
                evening_date TEXT NOT NULL DEFAULT '2026-09-13',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS draws (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                suggestion_id INTEGER NOT NULL REFERENCES suggestions(id),
                round_number INTEGER NOT NULL,
                drawn_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS movie_nights (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                draw_id INTEGER NOT NULL UNIQUE REFERENCES draws(id),
                owner_id INTEGER NOT NULL REFERENCES users(id),
                confirmed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS attendance (
                movie_night_id INTEGER NOT NULL REFERENCES movie_nights(id),
                user_id INTEGER NOT NULL REFERENCES users(id),
                rating INTEGER NOT NULL CHECK (rating BETWEEN 1 AND 10),
                PRIMARY KEY (movie_night_id, user_id)
            );

            CREATE TABLE IF NOT EXISTS roll_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                round_number INTEGER NOT NULL,
                roll_number INTEGER NOT NULL DEFAULT 0,
                evening_date TEXT NOT NULL DEFAULT '2026-09-13',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS roll_results (
                session_id INTEGER NOT NULL REFERENCES roll_sessions(id),
                roll_number INTEGER NOT NULL,
                suggestion_id INTEGER NOT NULL REFERENCES suggestions(id),
                user_id INTEGER NOT NULL REFERENCES users(id),
                base_points INTEGER NOT NULL,
                weight_points INTEGER NOT NULL,
                total_points INTEGER NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (session_id, roll_number)
            );
            """
        )

        columns = {row["name"] for row in connection.execute("PRAGMA table_info(suggestions)")}
        if "suggested_by_id" not in columns:
            connection.execute("ALTER TABLE suggestions ADD COLUMN suggested_by_id INTEGER")
        if "slogan" not in columns:
            connection.execute("ALTER TABLE suggestions ADD COLUMN slogan TEXT NOT NULL DEFAULT ''")
        if "evening_date" not in columns:
            connection.execute("ALTER TABLE suggestions ADD COLUMN evening_date TEXT NOT NULL DEFAULT '2026-09-13'")
        roll_columns = {row["name"] for row in connection.execute("PRAGMA table_info(roll_sessions)")}
        if "evening_date" not in roll_columns:
            connection.execute("ALTER TABLE roll_sessions ADD COLUMN evening_date TEXT NOT NULL DEFAULT '2026-09-13'")

        old_names = connection.execute(
            "SELECT DISTINCT suggested_by FROM suggestions WHERE suggested_by <> ''"
        ).fetchall()
        for old_name in old_names:
            connection.execute("INSERT OR IGNORE INTO users (name) VALUES (?)", (old_name["suggested_by"],))
        connection.execute(
            "UPDATE suggestions SET suggested_by_id = (SELECT id FROM users WHERE users.name = suggestions.suggested_by) "
            "WHERE suggested_by_id IS NULL"
        )
        connection.execute(
            "INSERT OR IGNORE INTO settings (key, value) VALUES ('latest_evening_date', ?)",
            (TODAY,),
        )

        old_winner = connection.execute("SELECT value FROM settings WHERE key = 'winner'").fetchone()
        current_draw = connection.execute("SELECT value FROM settings WHERE key = 'current_draw_id'").fetchone()
        if old_winner and not current_draw:
            suggestion = connection.execute(
                "SELECT id FROM suggestions WHERE film_title = ? ORDER BY id DESC LIMIT 1", (old_winner["value"],)
            ).fetchone()
            if suggestion:
                connection.execute(
                    "INSERT INTO draws (suggestion_id, round_number) VALUES (?, (SELECT COALESCE(MAX(round_number), 0) + 1 FROM draws))",
                    (suggestion["id"],),
                )
                draw_id = connection.execute("SELECT last_insert_rowid()").fetchone()[0]
                connection.execute(
                    "INSERT INTO settings (key, value) VALUES ('current_draw_id', ?)", (str(draw_id),)
                )
        legacy_draw = connection.execute("SELECT value FROM settings WHERE key = 'current_draw_id'").fetchone()
        if legacy_draw and not connection.execute(
            "SELECT 1 FROM settings WHERE key = ?", (setting_key("current_draw_id", TODAY),)
        ).fetchone():
            connection.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?)",
                (setting_key("current_draw_id", TODAY), legacy_draw["value"]),
            )
        if current_draw or connection.execute(
            "SELECT 1 FROM settings WHERE key = 'current_draw_id'"
        ).fetchone():
            connection.execute("UPDATE users SET pending_penalty = 0")

        if not connection.execute(
            "SELECT 1 FROM settings WHERE key = 'global_rounds_migrated'"
        ).fetchone():
            completed_draws = connection.execute(
                "SELECT id FROM draws ORDER BY drawn_at, id"
            ).fetchall()
            for global_round, draw in enumerate(completed_draws, start=1):
                connection.execute(
                    "UPDATE draws SET round_number = ? WHERE id = ?",
                    (global_round, draw["id"]),
                )
            connection.execute(
                "UPDATE users SET last_won_round = ("
                "SELECT MAX(draws.round_number) FROM movie_nights "
                "JOIN draws ON draws.id = movie_nights.draw_id "
                "WHERE movie_nights.owner_id = users.id"
                ") WHERE EXISTS ("
                "SELECT 1 FROM movie_nights WHERE movie_nights.owner_id = users.id"
                ")"
            )
            connection.execute(
                "INSERT INTO settings (key, value) VALUES ('global_rounds_migrated', '1')"
            )
        connection.commit()


def get_current_draw(connection, evening_date):
    setting = connection.execute(
        "SELECT value FROM settings WHERE key = ?", (setting_key("current_draw_id", evening_date),)
    ).fetchone()
    if not setting:
        return None
    return connection.execute(
        "SELECT draws.id, draws.round_number, suggestions.id AS suggestion_id, suggestions.film_title, "
        "suggestions.suggested_by_id, users.name AS owner_name FROM draws "
        "JOIN suggestions ON suggestions.id = draws.suggestion_id JOIN users ON users.id = suggestions.suggested_by_id "
        "WHERE draws.id = ?", (setting["value"],)
    ).fetchone()


def get_active_roll_session(connection, evening_date):
    setting = connection.execute(
        "SELECT value FROM settings WHERE key = ?",
        (setting_key("active_roll_session_id", evening_date),),
    ).fetchone()
    if not setting:
        return None
    return connection.execute(
        "SELECT id, round_number, roll_number, evening_date FROM roll_sessions WHERE id = ?",
        (setting["value"],),
    ).fetchone()


def get_user_weights(connection, next_round):
    weights = {}
    users = connection.execute(
        "SELECT id, attendance_points, pending_penalty, last_won_round FROM users"
    ).fetchall()
    for user in users:
        weights[user["id"]] = calculate_weight(user, next_round)
    return weights


def page_context(connection, stage, evening_date):
    user_rows = connection.execute(
        "SELECT id, name, attendance_points, pending_penalty, last_won_round FROM users ORDER BY name"
    ).fetchall()
    next_round = connection.execute(
        "SELECT COALESCE(MAX(round_number), 0) + 1 AS next_round FROM draws"
    ).fetchone()["next_round"]
    users = []
    for user in user_rows:
        users.append({
            **dict(user),
            "weight": calculate_weight(user, next_round),
        })
    suggestions = connection.execute(
        "SELECT suggestions.*, users.name AS owner_name FROM suggestions "
        "JOIN users ON users.id = suggestions.suggested_by_id WHERE suggestions.evening_date = ? "
        "ORDER BY suggestions.created_at DESC, suggestions.id DESC",
        (evening_date,),
    ).fetchall()
    current_draw = get_current_draw(connection, evening_date)
    confirmed_night = None
    ratings = []
    if current_draw:
        confirmed_night = connection.execute(
            "SELECT movie_nights.id, owner.name AS owner_name, "
            "AVG(attendance.rating) AS average_rating "
            "FROM movie_nights JOIN users owner ON owner.id = movie_nights.owner_id "
            "LEFT JOIN attendance ON attendance.movie_night_id = movie_nights.id "
            "WHERE movie_nights.draw_id = ? GROUP BY movie_nights.id",
            (current_draw["id"],),
        ).fetchone()
        if confirmed_night:
            ratings = connection.execute(
                "SELECT users.name, attendance.rating FROM attendance "
                "JOIN users ON users.id = attendance.user_id "
                "WHERE attendance.movie_night_id = ? ORDER BY users.name",
                (confirmed_night["id"],),
            ).fetchall()
    active_roll = get_active_roll_session(connection, evening_date)
    roll_results = []
    roll_scores = []
    if active_roll:
        roll_results = connection.execute(
            "SELECT roll_results.roll_number, roll_results.suggestion_id, roll_results.base_points, roll_results.weight_points, "
            "roll_results.total_points, suggestions.film_title, suggestions.slogan, users.name AS user_name "
            "FROM roll_results JOIN suggestions ON suggestions.id = roll_results.suggestion_id "
            "JOIN users ON users.id = roll_results.user_id WHERE session_id = ? "
            "ORDER BY roll_number DESC",
            (active_roll["id"],),
        ).fetchall()
        roll_scores = connection.execute(
            "SELECT suggestions.film_title AS user_name, SUM(roll_results.total_points) AS total_points "
            "FROM roll_results JOIN suggestions ON suggestions.id = roll_results.suggestion_id "
            "WHERE session_id = ? GROUP BY suggestion_id ORDER BY total_points DESC, film_title",
            (active_roll["id"],),
        ).fetchall()
    history = connection.execute(
        "SELECT movie_nights.confirmed_at, suggestions.film_title, owner.name AS owner_name, "
        "COUNT(attendance.user_id) AS attendees FROM movie_nights JOIN draws ON draws.id = movie_nights.draw_id "
        "JOIN suggestions ON suggestions.id = draws.suggestion_id JOIN users owner ON owner.id = movie_nights.owner_id "
        "LEFT JOIN attendance ON attendance.movie_night_id = movie_nights.id GROUP BY movie_nights.id "
        "ORDER BY movie_nights.id DESC LIMIT 5"
    ).fetchall()
    return {
        "stage": stage,
        "users": users,
        "suggestions": suggestions,
        "current_draw": current_draw,
        "confirmed_night": confirmed_night,
        "ratings": ratings,
        "history": history,
        "active_roll": active_roll,
        "roll_results": roll_results,
        "roll_scores": roll_scores,
        "evening_date": evening_date,
    }


@app.route("/")
def index():
    with get_db() as connection:
        evening_date = valid_evening_date(request.args.get("date")) if request.args.get("date") else get_latest_evening_date(connection)
        if request.args.get("date"):
            remember_evening_date(connection, evening_date)
            connection.commit()
        stage = 3 if get_current_draw(connection, evening_date) else 1
    return redirect(stage_url(stage, evening_date))


@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    next_stage = request.args.get("next", "2")
    evening_date = valid_evening_date(request.form.get("evening_date", request.args.get("date", TODAY)))
    if next_stage not in {"2", "3"}:
        next_stage = "2"
    if request.method == "POST":
        if request.form.get("password", "") == ADMIN_PASSWORD:
            session["is_admin"] = True
            return redirect(stage_url(int(next_stage), evening_date))
        flash("Неверный пароль администратора.", "error")
    return render_template("admin_login.html", next_stage=next_stage, evening_date=evening_date)


@app.get("/stage/<int:stage>")
def stage(stage):
    if stage not in (1, 2, 3):
        return redirect(url_for("stage", stage=1))
    with get_db() as connection:
        evening_date = valid_evening_date(request.args.get("date")) if request.args.get("date") else get_latest_evening_date(connection)
        if request.args.get("date"):
            remember_evening_date(connection, evening_date)
            connection.commit()
    session["evening_date"] = evening_date
    if stage in (2, 3) and not session.get("is_admin"):
        return redirect(url_for("admin_login", next=stage, date=evening_date))
    with get_db() as connection:
        context = page_context(connection, stage, evening_date)
    context["show_fireworks"] = session.pop("show_fireworks", False)
    return render_template("index.html", **context)


@app.post("/users")
def add_user():
    evening_date = valid_evening_date(request.form.get("evening_date", session.get("evening_date", TODAY)))
    name = request.form.get("name", "").strip()
    if not name:
        flash("Введи имя пользователя.", "error")
        return redirect(stage_url(1, evening_date))
    try:
        with get_db() as connection:
            connection.execute("INSERT INTO users (name) VALUES (?)", (name,))
            connection.commit()
        flash(f"Пользователь {name} добавлен.", "success")
    except sqlite3.IntegrityError:
        flash("Такой пользователь уже есть.", "error")
    return redirect(stage_url(1, evening_date))


@app.post("/users/<int:user_id>/delete")
def delete_user(user_id):
    with get_db() as connection:
        user = connection.execute("SELECT name FROM users WHERE id = ?", (user_id,)).fetchone()
        if not user:
            flash("Пользователь не найден.", "error")
            return redirect(url_for("stage", stage=1))
        has_suggestions = connection.execute(
            "SELECT 1 FROM suggestions WHERE suggested_by_id = ? LIMIT 1", (user_id,)
        ).fetchone()
        has_history = connection.execute(
            "SELECT 1 FROM movie_nights WHERE owner_id = ? LIMIT 1", (user_id,)
        ).fetchone() or connection.execute(
            "SELECT 1 FROM attendance WHERE user_id = ? LIMIT 1", (user_id,)
        ).fetchone()
        if has_suggestions or has_history:
            flash("Нельзя удалить участника: он связан с фильмами или историей вечеров.", "error")
            return redirect(url_for("stage", stage=1))
        connection.execute("DELETE FROM users WHERE id = ?", (user_id,))
        connection.commit()
    flash(f"Пользователь {user['name']} удалён.", "success")
    return redirect(url_for("stage", stage=1))


@app.post("/suggestions")
def add_suggestion():
    film_title = request.form.get("film_title", "").strip()
    slogan = request.form.get("slogan", "").strip()
    user_id = request.form.get("user_id", "").strip()
    evening_date = valid_evening_date(request.form.get("evening_date", session.get("evening_date", TODAY)))
    with get_db() as connection:
        user = connection.execute("SELECT id FROM users WHERE id = ?", (user_id,)).fetchone()
        if not film_title or not user:
            flash("Выбери себя и укажи название фильма.", "error")
            return redirect(stage_url(1, evening_date))
        existing_suggestion = connection.execute(
            "SELECT film_title FROM suggestions WHERE suggested_by_id = ? AND evening_date = ? LIMIT 1",
            (user_id, evening_date),
        ).fetchone()
        if existing_suggestion:
            flash(
                f"Участник уже предложил фильм «{existing_suggestion['film_title']}». "
                "За один вечер можно предложить только один фильм.",
                "error",
            )
            return redirect(stage_url(1, evening_date))
        connection.execute(
            "INSERT INTO suggestions (film_title, suggested_by, suggested_by_id, slogan) "
            "SELECT ?, name, id, ? FROM users WHERE id = ?",
            (film_title, slogan, user_id),
        )
        connection.execute(
            "UPDATE suggestions SET evening_date = ? WHERE id = last_insert_rowid()",
            (evening_date,),
        )
        connection.commit()
    flash("Предложение добавлено.", "success")
    return redirect(stage_url(1, evening_date))


@app.post("/suggestions/<int:suggestion_id>/delete")
def delete_suggestion(suggestion_id):
    with get_db() as connection:
        suggestion = connection.execute(
            "SELECT film_title FROM suggestions WHERE id = ?", (suggestion_id,)
        ).fetchone()
        if not suggestion:
            flash("Фильм не найден.", "error")
            return redirect(url_for("stage", stage=1))
        has_draw = connection.execute(
            "SELECT 1 FROM draws WHERE suggestion_id = ? LIMIT 1", (suggestion_id,)
        ).fetchone()
        if has_draw:
            flash("Нельзя удалить фильм, который уже участвовал в розыгрыше.", "error")
            return redirect(url_for("stage", stage=1))
        connection.execute("DELETE FROM suggestions WHERE id = ?", (suggestion_id,))
        connection.commit()
    flash(f"Фильм «{suggestion['film_title']}» удалён из списка.", "success")
    return redirect(url_for("stage", stage=1))


@app.post("/reset-evening")
def reset_evening():
    evening_date = valid_evening_date(request.form.get("evening_date", TODAY))
    if not session.get("is_admin"):
        return redirect(url_for("admin_login", next=2))
    with get_db() as connection:
        evening_date = valid_evening_date(request.form.get("evening_date", session.get("evening_date", TODAY)))
        connection.execute("DELETE FROM attendance WHERE movie_night_id IN (SELECT id FROM movie_nights WHERE draw_id IN (SELECT id FROM draws WHERE suggestion_id IN (SELECT id FROM suggestions WHERE evening_date = ?)))", (evening_date,))
        connection.execute("DELETE FROM movie_nights WHERE draw_id IN (SELECT id FROM draws WHERE suggestion_id IN (SELECT id FROM suggestions WHERE evening_date = ?))", (evening_date,))
        connection.execute("DELETE FROM roll_results WHERE session_id IN (SELECT id FROM roll_sessions WHERE evening_date = ?)", (evening_date,))
        connection.execute("DELETE FROM roll_sessions WHERE evening_date = ?", (evening_date,))
        connection.execute("DELETE FROM draws WHERE id IN (SELECT id FROM draws WHERE suggestion_id IN (SELECT id FROM suggestions WHERE evening_date = ?))", (evening_date,))
        connection.execute("DELETE FROM settings WHERE key IN (?, ?)", (setting_key("current_draw_id", evening_date), setting_key("active_roll_session_id", evening_date)))
        connection.commit()
    session.pop("show_fireworks", None)
    flash("Вечер обнулён. Участники и фильмы остались в списке.", "success")
    return redirect(stage_url(1, evening_date))


@app.post("/reset-weights")
def reset_weights():
    if not session.get("is_admin"):
        return redirect(url_for("admin_login", next=1))
    with get_db() as connection:
        connection.execute(
            "UPDATE users SET attendance_points = 0, last_won_round = NULL"
        )
        connection.commit()
    flash("Веса всех игроков обнулены.", "success")
    return redirect(stage_url(1, session.get("evening_date", TODAY)))


@app.post("/roll")
@app.post("/draw")
def roll_winner():
    evening_date = valid_evening_date(request.form.get("evening_date", request.args.get("date", session.get("evening_date", TODAY))))
    if not session.get("is_admin"):
        return redirect(url_for("admin_login", next=2, date=evening_date))
    with get_db() as connection:
        if get_current_draw(connection, evening_date):
            flash("Сначала подтвердите просмотр текущего фильма.", "error")
            return redirect(stage_url(3, evening_date))
        suggestions = connection.execute(
            "SELECT id, film_title, suggested_by_id FROM suggestions WHERE evening_date = ?",
            (evening_date,),
        ).fetchall()
        if not suggestions or not connection.execute("SELECT 1 FROM users LIMIT 1").fetchone():
            flash("Добавьте пользователей и хотя бы один фильм.", "error")
            return redirect(stage_url(1, evening_date))
        active_roll = get_active_roll_session(connection, evening_date)
        if not active_roll:
            round_number = connection.execute(
                "SELECT COALESCE(MAX(round_number), 0) + 1 AS next_round FROM draws"
            ).fetchone()["next_round"]
            connection.execute(
                "INSERT INTO roll_sessions (round_number, evening_date) VALUES (?, ?)", (round_number, evening_date)
            )
            session_id = connection.execute("SELECT last_insert_rowid()").fetchone()[0]
            connection.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?)",
                (setting_key("active_roll_session_id", evening_date), str(session_id)),
            )
            active_roll = connection.execute(
                "SELECT id, round_number, roll_number, evening_date FROM roll_sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
        if active_roll["roll_number"] >= 3:
            flash("Все три ролла уже прокручены.", "error")
            return redirect(url_for("stage", stage=2))
        roll_number = active_roll["roll_number"] + 1
        base_points = {1: 10, 2: 7, 3: 5}[roll_number]
        weights = get_user_weights(connection, active_roll["round_number"])
        already_rolled_users = {
            row["user_id"] for row in connection.execute(
                "SELECT user_id FROM roll_results WHERE session_id = ?",
                (active_roll["id"],),
            ).fetchall()
        }
        weighted_suggestions = [
            suggestion for suggestion in suggestions for _ in range(weights[suggestion["suggested_by_id"]])
        ]
        selected = random.choice(weighted_suggestions)
        weight_points = 0 if selected["suggested_by_id"] in already_rolled_users else weights[selected["suggested_by_id"]]
        connection.execute(
            "INSERT INTO roll_results (session_id, roll_number, suggestion_id, user_id, base_points, weight_points, total_points) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                active_roll["id"], roll_number, selected["id"], selected["suggested_by_id"],
                base_points, weight_points, base_points + weight_points,
            ),
        )
        connection.execute(
            "UPDATE roll_sessions SET roll_number = ? WHERE id = ?",
            (roll_number, active_roll["id"]),
        )
        if roll_number == 3:
            leaders = connection.execute(
                "SELECT user_id, SUM(total_points) AS total_points FROM roll_results "
                "WHERE session_id = ? GROUP BY user_id ORDER BY total_points DESC",
                (active_roll["id"],),
            ).fetchall()
            top_score = leaders[0]["total_points"]
            leader = random.choice([row for row in leaders if row["total_points"] == top_score])
            winning_suggestions = connection.execute(
                "SELECT id, film_title FROM suggestions WHERE suggested_by_id = ? AND evening_date = ?",
                (leader["user_id"], evening_date),
            ).fetchall()
            winning_suggestion = random.choice(winning_suggestions)
            connection.execute("UPDATE users SET pending_penalty = 0")
            connection.execute(
                "INSERT INTO draws (suggestion_id, round_number) VALUES (?, ?)",
                (winning_suggestion["id"], active_roll["round_number"]),
            )
            draw_id = connection.execute("SELECT last_insert_rowid()").fetchone()[0]
            connection.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?)",
                (setting_key("current_draw_id", evening_date), str(draw_id)),
            )
            session["show_fireworks"] = True
        connection.commit()
    if roll_number == 3:
        flash(f"Три ролла завершены. Выбран фильм «{winning_suggestion['film_title']}»!", "success")
        return redirect(stage_url(2, evening_date))
    flash(f"Ролл {roll_number}/3: «{selected['film_title']}» получает {base_points} + {weight_points} очков.", "success")
    return redirect(stage_url(2, evening_date))


@app.post("/confirm")
def confirm_movie_night():
    evening_date = valid_evening_date(request.form.get("evening_date", request.args.get("date", session.get("evening_date", TODAY))))
    if not session.get("is_admin"):
        return redirect(url_for("admin_login", next=3, date=evening_date))
    with get_db() as connection:
        current_draw = get_current_draw(connection, evening_date)
        if not current_draw:
            flash("Сначала проведите розыгрыш.", "error")
            return redirect(stage_url(2, evening_date))
        owner_id = request.form.get("owner_id", "")
        attendee_ids = request.form.getlist("attendee_ids")
        if not attendee_ids or not owner_id:
            flash("Отметь участников и владельца фильма.", "error")
            return redirect(stage_url(3, evening_date))
        valid_users = {str(row["id"]): row["id"] for row in connection.execute("SELECT id FROM users").fetchall()}
        attendee_ids = [valid_users[value] for value in attendee_ids if value in valid_users]
        if not attendee_ids or str(owner_id) not in valid_users:
            flash("Выбери корректных пользователей.", "error")
            return redirect(stage_url(3, evening_date))
        connection.execute("INSERT INTO movie_nights (draw_id, owner_id) VALUES (?, ?)", (current_draw["id"], valid_users[str(owner_id)]))
        night_id = connection.execute("SELECT last_insert_rowid()").fetchone()[0]
        for attendee_id in attendee_ids:
            rating = request.form.get(f"rating_{attendee_id}", "")
            try:
                rating = int(rating)
            except ValueError:
                rating = 0
            if not 1 <= rating <= 10:
                flash("Для каждого присутствующего нужна оценка от 1 до 10.", "error")
                connection.rollback()
                return redirect(stage_url(3, evening_date))
            connection.execute(
                "INSERT INTO attendance (movie_night_id, user_id, rating) VALUES (?, ?, ?)",
                (night_id, attendee_id, rating),
            )
            connection.execute("UPDATE users SET attendance_points = MIN(5, attendance_points + 1) WHERE id = ?", (attendee_id,))
        connection.execute("UPDATE users SET last_won_round = ? WHERE id = ?", (current_draw["round_number"], valid_users[str(owner_id)]))
        connection.execute("DELETE FROM settings WHERE key = ?", (setting_key("active_roll_session_id", evening_date),))
        connection.commit()
    flash("Просмотр подтверждён. Веса пользователей обновлены.", "success")
    return redirect(stage_url(3, evening_date))


if __name__ == "__main__":
    init_db()
    app.run(debug=True)
