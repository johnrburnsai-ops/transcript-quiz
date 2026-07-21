from __future__ import annotations

import json
import os
import sqlite3
import sys
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


_SCHEMA_VERSION = 2
_MIN_QUESTION_COUNT = 1
_MAX_QUESTION_COUNT = 50


@dataclass(frozen=True)
class TranscriptRecord:
    id: int
    name: str
    content: str
    created_at: str


@dataclass(frozen=True)
class QuizRecord:
    id: int
    transcript_id: int
    name: str
    questions: list[dict]
    created_at: str


@dataclass(frozen=True)
class AttemptRecord:
    id: int
    quiz_id: int
    user_answers: dict[str, str]
    score: int
    total: int
    started_at: str
    completed_at: str


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _default_data_dir() -> Path:
    if sys.platform == "win32":
        root = os.environ.get("LOCALAPPDATA")
        return (Path(root) if root else Path.home() / "AppData" / "Local") / "TranscriptQuiz"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "TranscriptQuiz"
    root = os.environ.get("XDG_DATA_HOME")
    return (Path(root) if root else Path.home() / ".local" / "share") / "transcript-quiz"


def _validate_questions(value: Any) -> list[dict]:
    if not isinstance(value, list):
        raise ValueError("questions must be a list")
    if not _MIN_QUESTION_COUNT <= len(value) <= _MAX_QUESTION_COUNT:
        raise ValueError("questions must contain between 1 and 50 quiz questions")
    normalized: list[dict] = []
    question_keys = {"question", "options", "answer"}
    option_keys = {"A", "B", "C", "D"}
    for index, item in enumerate(value, start=1):
        if not isinstance(item, dict) or set(item) != question_keys:
            raise ValueError(f"question {index} has an invalid shape")
        question = item["question"]
        options = item["options"]
        answer = item["answer"]
        if not isinstance(question, str) or not question.strip() or len(question) > 2000:
            raise ValueError(f"question {index} text is invalid")
        if not isinstance(options, dict) or set(options) != option_keys:
            raise ValueError(f"question {index} options are invalid")
        clean_options: dict[str, str] = {}
        for letter in ("A", "B", "C", "D"):
            option = options[letter]
            if not isinstance(option, str) or not option.strip() or len(option) > 1000:
                raise ValueError(f"question {index} option {letter} is invalid")
            clean_options[letter] = option.strip()
        if len({" ".join(option.split()).casefold() for option in clean_options.values()}) != 4:
            raise ValueError(f"question {index} option texts must be unique")
        if not isinstance(answer, str) or answer not in option_keys:
            raise ValueError(f"question {index} answer is invalid")
        normalized.append(
            {
                "question": question.strip(),
                "options": clean_options,
                "answer": answer,
            }
        )
    return normalized


class Database:
    """SQLite persistence for transcripts, quizzes, and quiz attempts."""

    def __init__(self, db_path: str | Path | None = None):
        if db_path is None:
            data_dir = _default_data_dir()
            data_dir.mkdir(parents=True, exist_ok=True)
            self.db_path = data_dir / "transcript_quiz.db"
        elif str(db_path) == ":memory:":
            self.db_path: Path | str = ":memory:"
        else:
            self.db_path = Path(db_path).expanduser()
            self.db_path.parent.mkdir(parents=True, exist_ok=True)

        self._memory = self.db_path == ":memory:"
        self._memory_lock = threading.RLock()
        self._memory_connection: sqlite3.Connection | None = None
        if self._memory:
            self._memory_connection = self._new_connection(set_wal=False)
        self._migrate()

    def _new_connection(self, *, set_wal: bool = False) -> sqlite3.Connection:
        connection = sqlite3.connect(
            str(self.db_path),
            timeout=5.0,
            check_same_thread=not self._memory,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        if set_wal and not self._memory:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = NORMAL")
        return connection

    @contextmanager
    def _connection(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        if self._memory:
            assert self._memory_connection is not None
            with self._memory_lock:
                try:
                    yield self._memory_connection
                    if write:
                        self._memory_connection.commit()
                except BaseException:
                    if write:
                        self._memory_connection.rollback()
                    raise
            return

        connection = self._new_connection()
        try:
            yield connection
            if write:
                connection.commit()
        except BaseException:
            if write:
                connection.rollback()
            raise
        finally:
            connection.close()

    def _migrate(self) -> None:
        if self._memory:
            assert self._memory_connection is not None
            connection = self._memory_connection
            lock = self._memory_lock
        else:
            connection = self._new_connection(set_wal=True)
            lock = threading.Lock()

        try:
            with lock:
                version = int(connection.execute("PRAGMA user_version").fetchone()[0])
                if version > _SCHEMA_VERSION:
                    raise RuntimeError(
                        f"Database schema version {version} is newer than supported version {_SCHEMA_VERSION}"
                    )
                if version < 1:
                    connection.executescript(
                        """
                        CREATE TABLE IF NOT EXISTS transcripts (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            name TEXT NOT NULL,
                            content TEXT NOT NULL,
                            created_at TEXT NOT NULL
                        );

                        CREATE TABLE IF NOT EXISTS quizzes (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            transcript_id INTEGER NOT NULL,
                            name TEXT NOT NULL,
                            questions_json TEXT NOT NULL,
                            created_at TEXT NOT NULL,
                            FOREIGN KEY (transcript_id) REFERENCES transcripts(id) ON DELETE CASCADE
                        );

                        CREATE TABLE IF NOT EXISTS attempts (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            quiz_id INTEGER NOT NULL,
                            user_answers_json TEXT NOT NULL,
                            score INTEGER NOT NULL,
                            total INTEGER NOT NULL,
                            started_at TEXT NOT NULL,
                            completed_at TEXT NOT NULL,
                            FOREIGN KEY (quiz_id) REFERENCES quizzes(id) ON DELETE CASCADE
                        );

                        CREATE INDEX IF NOT EXISTS idx_quizzes_transcript
                            ON quizzes(transcript_id, created_at DESC);
                        CREATE INDEX IF NOT EXISTS idx_attempts_quiz
                            ON attempts(quiz_id, completed_at DESC);
                        PRAGMA user_version = 2;
                        """
                    )
                    connection.commit()
                elif version == 1:
                    columns = {
                        str(row[1])
                        for row in connection.execute("PRAGMA table_info(quizzes)").fetchall()
                    }
                    if "name" not in columns:
                        connection.execute(
                            "ALTER TABLE quizzes ADD COLUMN name TEXT NOT NULL DEFAULT ''"
                        )
                    connection.execute(
                        "UPDATE quizzes SET name = 'Quiz ' || id WHERE name IS NULL OR trim(name) = ''"
                    )
                    connection.execute("PRAGMA user_version = 2")
                    connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            if not self._memory:
                connection.close()

    @staticmethod
    def _require_nonempty(value: str, field: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{field} must be a nonempty string")
        return value

    @staticmethod
    def _json_round_trip(value: Any, field: str) -> tuple[str, Any]:
        try:
            encoded = json.dumps(
                value,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            )
            decoded = json.loads(encoded)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError(f"{field} must be JSON serializable") from exc
        return encoded, decoded

    @staticmethod
    def _transcript_from_row(row: sqlite3.Row) -> TranscriptRecord:
        return TranscriptRecord(
            id=int(row["id"]),
            name=str(row["name"]),
            content=str(row["content"]),
            created_at=str(row["created_at"]),
        )

    @staticmethod
    def _quiz_from_row(row: sqlite3.Row) -> QuizRecord:
        questions = json.loads(row["questions_json"])
        try:
            questions = _validate_questions(questions)
        except ValueError as exc:
            raise sqlite3.DatabaseError("Stored quiz JSON has an invalid shape") from exc
        return QuizRecord(
            id=int(row["id"]),
            transcript_id=int(row["transcript_id"]),
            name=str(row["name"]),
            questions=questions,
            created_at=str(row["created_at"]),
        )

    @staticmethod
    def _attempt_from_row(row: sqlite3.Row) -> AttemptRecord:
        answers = json.loads(row["user_answers_json"])
        if not isinstance(answers, dict):
            raise sqlite3.DatabaseError("Stored attempt JSON has an invalid shape")
        return AttemptRecord(
            id=int(row["id"]),
            quiz_id=int(row["quiz_id"]),
            user_answers={str(key): str(value) for key, value in answers.items()},
            score=int(row["score"]),
            total=int(row["total"]),
            started_at=str(row["started_at"]),
            completed_at=str(row["completed_at"]),
        )

    def save_transcript(self, name: str, content: str) -> TranscriptRecord:
        name = self._require_nonempty(name, "name")
        content = self._require_nonempty(content, "content")
        created_at = _utc_now()
        with self._connection(write=True) as connection:
            cursor = connection.execute(
                "INSERT INTO transcripts(name, content, created_at) VALUES (?, ?, ?)",
                (name, content, created_at),
            )
            record_id = int(cursor.lastrowid)
        return TranscriptRecord(record_id, name, content, created_at)

    def update_transcript(self, id: int, name: str, content: str) -> TranscriptRecord:
        name = self._require_nonempty(name, "name")
        content = self._require_nonempty(content, "content")
        with self._connection(write=True) as connection:
            cursor = connection.execute(
                "UPDATE transcripts SET name = ?, content = ? WHERE id = ?",
                (name, content, id),
            )
            if cursor.rowcount == 0:
                raise KeyError(f"Transcript {id} does not exist")
            row = connection.execute(
                "SELECT id, name, content, created_at FROM transcripts WHERE id = ?",
                (id,),
            ).fetchone()
        assert row is not None
        return self._transcript_from_row(row)

    def get_transcript(self, id: int) -> TranscriptRecord | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT id, name, content, created_at FROM transcripts WHERE id = ?",
                (id,),
            ).fetchone()
        return None if row is None else self._transcript_from_row(row)

    def list_transcripts(self, search: str = "") -> list[TranscriptRecord]:
        if not isinstance(search, str):
            raise TypeError("search must be a string")
        term = search.strip()
        with self._connection() as connection:
            if term:
                escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                pattern = f"%{escaped}%"
                rows = connection.execute(
                    """
                    SELECT id, name, content, created_at
                    FROM transcripts
                    WHERE name LIKE ? ESCAPE '\\' COLLATE NOCASE
                       OR content LIKE ? ESCAPE '\\' COLLATE NOCASE
                    ORDER BY created_at DESC, id DESC
                    """,
                    (pattern, pattern),
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT id, name, content, created_at
                    FROM transcripts
                    ORDER BY created_at DESC, id DESC
                    """
                ).fetchall()
        return [self._transcript_from_row(row) for row in rows]

    def delete_transcript(self, id: int) -> bool:
        with self._connection(write=True) as connection:
            cursor = connection.execute("DELETE FROM transcripts WHERE id = ?", (id,))
            return cursor.rowcount > 0

    def save_quiz(
        self,
        transcript_id: int,
        questions: list[dict],
        name: str | None = None,
    ) -> QuizRecord:
        normalized = _validate_questions(questions)
        questions_json, normalized = self._json_round_trip(normalized, "questions")
        if name is not None:
            name = self._require_nonempty(name, "name")
        created_at = _utc_now()
        try:
            with self._connection(write=True) as connection:
                cursor = connection.execute(
                    """
                    INSERT INTO quizzes(transcript_id, name, questions_json, created_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (transcript_id, name if name is not None else "", questions_json, created_at),
                )
                record_id = int(cursor.lastrowid)
                if name is None:
                    existing_names = {
                        str(row["name"])
                        for row in connection.execute(
                            "SELECT name FROM quizzes WHERE transcript_id = ?",
                            (transcript_id,),
                        ).fetchall()
                    }
                    number = 1
                    while f"Quiz {number}" in existing_names:
                        number += 1
                    name = f"Quiz {number}"
                    connection.execute(
                        "UPDATE quizzes SET name = ? WHERE id = ?",
                        (name, record_id),
                    )
        except sqlite3.IntegrityError as exc:
            raise ValueError(f"Transcript {transcript_id} does not exist") from exc
        assert name is not None
        return QuizRecord(record_id, transcript_id, name, normalized, created_at)

    def update_quiz_name(self, id: int, name: str) -> QuizRecord:
        name = self._require_nonempty(name, "name")
        with self._connection(write=True) as connection:
            cursor = connection.execute(
                "UPDATE quizzes SET name = ? WHERE id = ?",
                (name, id),
            )
            if cursor.rowcount == 0:
                raise KeyError(f"Quiz {id} does not exist")
            row = connection.execute(
                """
                SELECT id, transcript_id, name, questions_json, created_at
                FROM quizzes WHERE id = ?
                """,
                (id,),
            ).fetchone()
        assert row is not None
        return self._quiz_from_row(row)

    def delete_quiz(self, id: int) -> bool:
        with self._connection(write=True) as connection:
            cursor = connection.execute("DELETE FROM quizzes WHERE id = ?", (id,))
            return cursor.rowcount > 0

    def get_quiz(self, id: int) -> QuizRecord | None:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT id, transcript_id, name, questions_json, created_at
                FROM quizzes WHERE id = ?
                """,
                (id,),
            ).fetchone()
        return None if row is None else self._quiz_from_row(row)

    def list_quizzes(self, transcript_id: int) -> list[QuizRecord]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT id, transcript_id, name, questions_json, created_at
                FROM quizzes WHERE transcript_id = ?
                ORDER BY created_at DESC, id DESC
                """,
                (transcript_id,),
            ).fetchall()
        return [self._quiz_from_row(row) for row in rows]

    def save_attempt(
        self,
        quiz_id: int,
        user_answers: dict[str, str],
        score: int,
        total: int,
        started_at: str,
        completed_at: str | None = None,
    ) -> AttemptRecord:
        if not isinstance(user_answers, dict) or not all(
            isinstance(key, str) and isinstance(value, str) for key, value in user_answers.items()
        ):
            raise ValueError("user_answers must be a dictionary of strings")
        if isinstance(score, bool) or not isinstance(score, int):
            raise ValueError("score must be an integer")
        if isinstance(total, bool) or not isinstance(total, int):
            raise ValueError("total must be an integer")
        started_at = self._require_nonempty(started_at, "started_at")
        if completed_at is None:
            completed_at = _utc_now()
        else:
            completed_at = self._require_nonempty(completed_at, "completed_at")
        answers_json, normalized = self._json_round_trip(user_answers, "user_answers")
        try:
            with self._connection(write=True) as connection:
                quiz_row = connection.execute(
                    "SELECT questions_json FROM quizzes WHERE id = ?",
                    (quiz_id,),
                ).fetchone()
                if quiz_row is None:
                    raise ValueError(f"Quiz {quiz_id} does not exist")
                try:
                    questions = _validate_questions(json.loads(quiz_row["questions_json"]))
                except (json.JSONDecodeError, ValueError) as exc:
                    raise sqlite3.DatabaseError("Stored quiz JSON has an invalid shape") from exc
                expected_keys = {str(index) for index in range(len(questions))}
                if set(normalized) != expected_keys or any(
                    answer not in {"A", "B", "C", "D"} for answer in normalized.values()
                ):
                    raise ValueError("user_answers must answer every quiz question with A, B, C, or D")
                derived_total = len(questions)
                derived_score = sum(
                    1
                    for index, question in enumerate(questions)
                    if normalized[str(index)] == question["answer"]
                )
                if total != derived_total or score != derived_score:
                    raise ValueError("score and total do not match the submitted quiz answers")
                cursor = connection.execute(
                    """
                    INSERT INTO attempts(
                        quiz_id, user_answers_json, score, total, started_at, completed_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (quiz_id, answers_json, score, total, started_at, completed_at),
                )
                record_id = int(cursor.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ValueError(f"Quiz {quiz_id} does not exist") from exc
        return AttemptRecord(
            record_id,
            quiz_id,
            normalized,
            score,
            total,
            started_at,
            completed_at,
        )

    def get_attempt(self, id: int) -> AttemptRecord | None:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT id, quiz_id, user_answers_json, score, total, started_at, completed_at
                FROM attempts WHERE id = ?
                """,
                (id,),
            ).fetchone()
        return None if row is None else self._attempt_from_row(row)

    def list_attempts(self, quiz_id: int) -> list[AttemptRecord]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT id, quiz_id, user_answers_json, score, total, started_at, completed_at
                FROM attempts WHERE quiz_id = ?
                ORDER BY completed_at DESC, id DESC
                """,
                (quiz_id,),
            ).fetchall()
        return [self._attempt_from_row(row) for row in rows]
