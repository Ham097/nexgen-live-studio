"""Durable session evidence and a transactional publication outbox."""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
from uuid import uuid4

from .analysis import ACTIVITIES, LEVELS, OUTCOMES, eligible, report
from .linguistics import extract

SCHEMA = """
CREATE TABLE sessions (
 id TEXT PRIMARY KEY, student_id TEXT NOT NULL, name TEXT NOT NULL,
 level TEXT NOT NULL, reservation_id TEXT, mission_id TEXT, meet_url TEXT,
 consent_at TEXT NOT NULL, created_at TEXT NOT NULL, profile TEXT NOT NULL,
 approval TEXT, source TEXT NOT NULL, adapter_url TEXT
);
CREATE UNIQUE INDEX reservation_unique ON sessions(reservation_id, source, ifnull(adapter_url, ''))
 WHERE reservation_id IS NOT NULL;
CREATE TABLE samples (
 id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions ON DELETE CASCADE,
 position INTEGER NOT NULL, activity TEXT NOT NULL, label TEXT NOT NULL DEFAULT '',
 excluded INTEGER NOT NULL DEFAULT 0, state TEXT NOT NULL,
 UNIQUE(session_id, position)
);
CREATE TABLE turns (
 id TEXT PRIMARY KEY, sample_id TEXT NOT NULL REFERENCES samples ON DELETE CASCADE,
 epoch TEXT NOT NULL, speaker TEXT NOT NULL, mode TEXT NOT NULL, start_ms INTEGER NOT NULL,
 end_ms INTEGER NOT NULL, original TEXT NOT NULL, original_words TEXT NOT NULL,
 verified TEXT, teaching TEXT NOT NULL DEFAULT '', flags TEXT NOT NULL DEFAULT '[]',
 evidence TEXT NOT NULL DEFAULT 'observed', excluded INTEGER NOT NULL DEFAULT 0,
 bookmarked INTEGER NOT NULL DEFAULT 0, error_count INTEGER, features TEXT NOT NULL,
 confirmed_grammar TEXT NOT NULL DEFAULT '[]',
 UNIQUE(sample_id, epoch, speaker, start_ms, end_ms)
);
CREATE TABLE revisions (
 id INTEGER PRIMARY KEY, turn_id TEXT NOT NULL REFERENCES turns ON DELETE CASCADE,
 updated_at TEXT NOT NULL, previous TEXT NOT NULL, revised TEXT NOT NULL
);
CREATE TABLE board (
 id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions ON DELETE CASCADE,
 term TEXT NOT NULL, meaning TEXT NOT NULL DEFAULT '', definition TEXT NOT NULL DEFAULT '',
 example TEXT NOT NULL DEFAULT '', note TEXT NOT NULL DEFAULT '',
 revision INTEGER NOT NULL DEFAULT 0, shared INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE outbox (
 session_id TEXT PRIMARY KEY REFERENCES sessions ON DELETE CASCADE,
 payload TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT,
 delivered_at TEXT, next_at TEXT
);
CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE INDEX sessions_student ON sessions(student_id, created_at);
CREATE INDEX turns_sample ON turns(sample_id, start_ms);
PRAGMA user_version = 1;
"""


def now():
    return datetime.now(timezone.utc).isoformat()


def doc(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def profile(level):
    return {"level": level, "pause_ms": 600, "response_gap_ms": 3200,
            "min_timed_words": 15, "min_response_ms": 8000, "required_activities": []}


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.db() as db:
            db.execute("PRAGMA journal_mode=WAL")
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version == 0:
                db.executescript("BEGIN IMMEDIATE;\n" + SCHEMA + "\nCOMMIT;")
            elif version != 1:
                raise RuntimeError("This database needs a newer version of Live Studio.")
            db.execute("UPDATE samples SET state='interrupted' WHERE state='open'")

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA secure_delete=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    def settings(self, key, default=None):
        with self.db() as db:
            row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
            return json.loads(row[0]) if row else default

    def configure(self, key, value):
        with self.db() as db:
            db.execute("INSERT OR REPLACE INTO settings VALUES (?,?)", (key, doc(value)))

    def create(self, identity, consent):
        student_id = identity.get("student_id", "").strip()
        name = identity.get("name", "").strip()
        level = identity["level"]
        if not student_id or not name or not consent or level not in LEVELS:
            raise ValueError("Confirm a named student, stable ID, level and consent first.")
        key, created_at = str(uuid4()), now()
        cfg = self.settings(f"profile.{level}", profile(level))
        with self.db() as db:
            db.execute("INSERT INTO sessions VALUES(?,?,?,?,?,?,?,?,?,?,NULL,?,?)",
                       (key, student_id, name, level, identity.get("reservation_id"),
                        identity.get("mission_id"), identity.get("meet_url"), created_at,
                        created_at, doc(cfg), identity.get("source", "local"), identity.get("adapter_url")))
        return key

    def sessions(self):
        with self.db() as db:
            return [dict(row) for row in db.execute("SELECT * FROM sessions ORDER BY created_at DESC")]

    @staticmethod
    def _draft(db, session_id):
        row = db.execute("SELECT approval FROM sessions WHERE id=?", (session_id,)).fetchone()
        if not row or row[0] is not None:
            raise ValueError("Open a draft lesson to make changes.")

    def sample(self, session_id, activity):
        if activity not in ACTIVITIES:
            raise ValueError("Invalid activity.")
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            self._draft(db, session_id)
            key = str(uuid4())
            position = db.execute("SELECT COALESCE(MAX(position),0)+1 FROM samples WHERE session_id=?",
                                  (session_id,)).fetchone()[0]
            db.execute("INSERT INTO samples(id,session_id,position,activity,state) VALUES(?,?,?,?,'open')",
                       (key, session_id, position, activity))
            return key

    def sample_action(self, sample_id, **values):
        allowed = {"label", "activity", "excluded", "state"}
        if not values.keys() <= allowed or ("activity" in values and values["activity"] not in ACTIVITIES):
            raise ValueError("Invalid sample update.")
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT session_id FROM samples WHERE id=?", (sample_id,)).fetchone()
            if not row:
                raise ValueError("Sample no longer exists.")
            self._draft(db, row[0])
            if not values:
                db.execute("DELETE FROM samples WHERE id=?", (sample_id,))
            else:
                clause = ", ".join(f"{key}=?" for key in values)
                db.execute(f"UPDATE samples SET {clause} WHERE id=?", (*values.values(), sample_id))

    def add_turn(self, turn):
        key = str(uuid4())
        features = extract(turn["original"]) if turn["speaker"] == "student" else {}
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT session_id FROM samples WHERE id=?", (turn["sample_id"],)).fetchone()
            if not row:
                return None
            self._draft(db, row[0])
            cursor = db.execute(
                """INSERT OR IGNORE INTO turns(id,sample_id,epoch,speaker,mode,start_ms,end_ms,
                   original,original_words,flags,features) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (key, *(turn[k] for k in ("sample_id", "epoch", "speaker", "mode", "start_ms",
                                         "end_ms", "original")), doc(turn["words"]), doc(turn["flags"]), doc(features)),
            )
            return key if cursor.rowcount else None

    @staticmethod
    def _turn(row):
        turn = dict(row)
        original_words = json.loads(turn["original_words"])
        turn["original_words"] = original_words
        turn["flags"] = json.loads(turn["flags"])
        turn["features"] = json.loads(turn["features"])
        turn["confirmed_grammar"] = json.loads(turn["confirmed_grammar"])
        turn["text"] = turn["verified"] if turn["verified"] is not None else turn["original"]
        turn["words"] = original_words if turn["text"].strip() == turn["original"].strip() else []
        return turn

    def revise(self, turn_id, *, undo=False, **values):
        allowed = {"verified", "teaching", "evidence", "excluded", "bookmarked", "error_count", "flags", "confirmed_grammar"}
        if not values.keys() <= allowed:
            raise ValueError("Invalid transcript change.")
        if ("evidence" in values and values["evidence"] not in ("observed", "independent", "assisted")
                or values.get("error_count") is not None and values["error_count"] < 0):
            raise ValueError("Invalid evidence review.")
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("""SELECT turns.*, samples.session_id FROM turns
                             JOIN samples ON samples.id=turns.sample_id WHERE turns.id=?""",
                             (turn_id,)).fetchone()
            if not row:
                raise ValueError("Turn no longer exists.")
            self._draft(db, row["session_id"])
            if undo:
                revision = db.execute("SELECT id,previous FROM revisions WHERE turn_id=? ORDER BY id DESC LIMIT 1",
                                      (turn_id,)).fetchone()
                if not revision:
                    return False
                values = json.loads(revision["previous"])
                db.execute("DELETE FROM revisions WHERE id=?", (revision["id"],))
            else:
                if "flags" in values:
                    values["flags"] = doc(values["flags"])
                if "verified" in values and values["verified"] != row["verified"]:
                    values.setdefault("error_count", None)
                    values.setdefault("confirmed_grammar", [])
                    if row["speaker"] == "student":
                        text = values["verified"] if values["verified"] is not None else row["original"]
                        values["features"] = doc(extract(text))
                if "confirmed_grammar" in values:
                    candidates = json.loads(values.get("features", row["features"])).get("grammar", {})
                    values["confirmed_grammar"] = doc(sorted(set(values["confirmed_grammar"]) & set(candidates)))
                values = {key: value for key, value in values.items() if row[key] != value}
                if not values:
                    return False
                db.execute("INSERT INTO revisions(turn_id,updated_at,previous,revised) VALUES (?,?,?,?)",
                           (turn_id, now(), doc({key: row[key] for key in values}), doc(values)))
            clause = ", ".join(f"{key}=?" for key in values)
            db.execute(f"UPDATE turns SET {clause} WHERE id=?", (*values.values(), turn_id))
            return True

    def board_item(self, session_id, item_id=None, **values):
        fields = ("term", "meaning", "definition", "example", "note", "revision", "shared")
        if not set(values) <= set(fields):
            raise ValueError("Unknown teaching field.")
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            self._draft(db, session_id)
            if not values and item_id:
                db.execute("DELETE FROM board WHERE id=? AND session_id=?", (item_id, session_id))
                return None
            if not str(values.get("term", "")).strip():
                raise ValueError("Enter a word, phrase or correction first.")
            if item_id:
                clause = ", ".join(f"{key}=?" for key in values)
                db.execute(f"UPDATE board SET {clause} WHERE id=? AND session_id=?",
                           (*values.values(), item_id, session_id))
                return item_id
            item_id = str(uuid4())
            db.execute("""INSERT INTO board(id,session_id,term,meaning,definition,example,note,revision,shared)
                          VALUES (?,?,?,?,?,?,?,?,?)""",
                       (item_id, session_id, *(values.get(key, 0 if key in ("revision", "shared") else "")
                                               for key in fields)))
            return item_id

    def load(self, session_id, db=None):
        if db is None:
            with self.db() as connection:
                return self.load(session_id, connection)
        row = db.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
        if not row:
            raise ValueError("Lesson no longer exists.")
        lesson = dict(row)
        lesson["profile"] = json.loads(lesson["profile"])
        lesson["approval"] = json.loads(lesson["approval"]) if lesson["approval"] else None
        lesson["samples"] = []
        for sample_row in db.execute("SELECT * FROM samples WHERE session_id=? ORDER BY position", (session_id,)):
            sample = dict(sample_row)
            sample["turns"] = [self._turn(turn) for turn in db.execute(
                "SELECT * FROM turns WHERE sample_id=? ORDER BY start_ms,speaker", (sample["id"],))]
            lesson["samples"].append(sample)
        lesson["board"] = [dict(row) for row in db.execute("SELECT * FROM board WHERE session_id=? ORDER BY rowid",
                                                        (session_id,))]
        return lesson

    def preview_metrics(self, lesson):
        return report(lesson["samples"], lesson["profile"])

    def approve(self, session_id, outcome, teacher, note):
        if outcome not in OUTCOMES or not teacher.strip():
            raise ValueError("Choose an assessment decision and enter your name.")
        from .integrations import bundle
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            self._draft(db, session_id)
            lesson = self.load(session_id, db)
            if any(sample["state"] == "open" for sample in lesson["samples"]):
                raise ValueError("Finish the current sample before approving.")
            metrics = self.preview_metrics(lesson)
            if outcome == "Passed":
                if not metrics["words"]:
                    raise ValueError("A pass needs eligible student-language evidence.")
                if set(metrics["required_activities"]) - set(metrics["activities"]):
                    raise ValueError("This profile requires another activity.")
            approval = {"outcome": outcome, "teacher": teacher.strip(), "note": note, "at": now()}
            lesson["approval"] = approval
            payload = bundle(lesson, metrics)
            db.execute("UPDATE sessions SET approval=? WHERE id=?", (doc(approval), session_id))
            db.execute("INSERT INTO outbox(session_id,payload) VALUES (?,?)", (session_id, doc(payload)))
            return payload

    def outbox(self, session_id=None, due=False):
        with self.db() as db:
            if session_id:
                row = db.execute("SELECT * FROM outbox WHERE session_id=?", (session_id,)).fetchone()
                return dict(row) if row else None
            condition = "WHERE delivered_at IS NULL AND (next_at IS NULL OR next_at<=?)" if due else ""
            return [dict(row) for row in db.execute(
                f"SELECT outbox.*,sessions.adapter_url FROM outbox JOIN sessions ON sessions.id=outbox.session_id {condition}",
                (now(),) if due else ())]

    def sync_result(self, session_id, error=None):
        with self.db() as db:
            if error is None:
                db.execute("UPDATE outbox SET delivered_at=?,last_error=NULL,next_at=NULL WHERE session_id=?",
                           (now(), session_id))
            else:
                row = db.execute("SELECT attempts FROM outbox WHERE session_id=?", (session_id,)).fetchone()
                if row:
                    retry = datetime.now(timezone.utc) + timedelta(seconds=min(3600, 30 * 2 ** min(row[0], 7)))
                    db.execute("UPDATE outbox SET attempts=attempts+1,last_error=?,next_at=? WHERE session_id=?",
                               (error, retry.isoformat(), session_id))

    def delete(self, session_id):
        with self.db() as db:
            db.execute("DELETE FROM sessions WHERE id=?", (session_id,))
        with self.db() as db:
            db.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    def prune(self, days):
        if not days:
            return
        before = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        for session in self.sessions():
            # Pending deliveries are held for the teacher to export/delete explicitly.
            queued = self.outbox(session["id"])
            if session["created_at"] < before and not (queued and not queued["delivered_at"]):
                self.delete(session["id"])

    def passport(self, student_id, omit=None):
        history = {"vocabulary": {}, "grammar": {}, "lessons": []}
        active = self.load(omit)["profile"] if omit else None
        for row in reversed(self.sessions()):
            if row["student_id"] != student_id or row["id"] == omit or not row["approval"]:
                continue
            lesson = self.load(row["id"])
            metrics = self.preview_metrics(lesson)
            history["lessons"].append({"id": row["id"], "date": row["created_at"],
                                       "profile_match": active == lesson["profile"], **metrics})
            for field in ("vocabulary", "grammar"):
                introduced = ({item["term"].strip().lower() for item in lesson["board"]}
                              if field == "vocabulary" else set())
                for word in set(metrics[field]) | introduced:
                    record = history[field].setdefault(word, {"first": row["created_at"], "last": None,
                        "frequency": 0, "lessons": 0, "independent_lessons": 0, "confirmed": 0,
                        "independent_dates": [], "introduced": False})
                    frequency = metrics[field].get(word, 0)
                    if frequency:
                        record["last"] = row["created_at"]
                        record["lessons"] += 1
                        record["frequency"] += frequency
                    if word in metrics[f"independent_{field}"]:
                        record["independent_lessons"] += 1
                        date = datetime.fromisoformat(row["created_at"]).astimezone().date().isoformat()
                        if date not in record["independent_dates"]:
                            record["independent_dates"].append(date)
                    if field == "grammar":
                        record["confirmed"] += metrics["confirmed_grammar"].get(word, 0)
                    record["introduced"] |= word in introduced
        for group in ("vocabulary", "grammar"):
            for value in history[group].values():
                value["state"] = ("retained" if len(value["independent_dates"]) >= 2 else
                                  "independently demonstrated" if value["independent_lessons"] else
                                  "repeated" if value["lessons"] >= 2 else
                                  "observed" if value["lessons"] else "introduced")
        return history
