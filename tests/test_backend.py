from __future__ import annotations

import json
import os
import queue
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from api_handler import (
    ApiHandler,
    QuizValidationError,
    _DISABLED_CODEX_FEATURES,
    validate_quiz_output,
)
from auth_manager import AuthManager, DeviceChallenge
from database import Database


def _quiz_data(count: int = 10) -> list[dict]:
    return [
        {
            "question": f"Question {index}?",
            "options": {
                "A": f"Correct {index}",
                "B": f"Distractor B {index}",
                "C": f"Distractor C {index}",
                "D": f"Distractor D {index}",
            },
            "answer": "A",
        }
        for index in range(1, count + 1)
    ]


class DatabaseTests(unittest.TestCase):
    def test_crud_search_and_cascades(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "backend.sqlite3"
            database = Database(db_path)

            first = database.save_transcript("First lecture", "Alpha material")
            second = database.save_transcript("Second lecture", "Beta material")
            self.assertEqual(database.get_transcript(first.id), first)
            self.assertEqual([item.id for item in database.list_transcripts()], [second.id, first.id])
            self.assertEqual([item.id for item in database.list_transcripts("alpha")], [first.id])

            updated = database.update_transcript(first.id, "Updated lecture", "Updated content")
            self.assertEqual(updated.created_at, first.created_at)
            self.assertEqual(database.get_transcript(first.id), updated)

            quiz = database.save_quiz(first.id, _quiz_data())
            self.assertEqual(database.get_quiz(quiz.id), quiz)
            self.assertEqual(database.list_quizzes(first.id), [quiz])
            self.assertEqual(quiz.name, f"Quiz {quiz.id}")
            attempt = database.save_attempt(
                quiz.id,
                {str(index): "A" for index in range(10)},
                10,
                10,
                "2026-07-16T10:00:00Z",
                "2026-07-16T10:01:00Z",
            )
            self.assertEqual(database.get_attempt(attempt.id), attempt)
            self.assertEqual(database.list_attempts(quiz.id), [attempt])

            self.assertTrue(database.delete_transcript(first.id))
            self.assertFalse(database.delete_transcript(first.id))
            self.assertIsNone(database.get_quiz(quiz.id))
            self.assertIsNone(database.get_attempt(attempt.id))

            connection = sqlite3.connect(db_path)
            try:
                self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 2)
                self.assertEqual(connection.execute("PRAGMA journal_mode").fetchone()[0].lower(), "wal")
                table_names = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    ).fetchall()
                }
                self.assertTrue({"transcripts", "quizzes", "attempts"}.issubset(table_names))
            finally:
                connection.close()

    def test_quiz_names_rename_delete_and_attempt_cascade(self) -> None:
        database = Database(":memory:")
        transcript = database.save_transcript("Lecture", "Content")
        quiz = database.save_quiz(transcript.id, _quiz_data(5), name="Five questions")
        self.assertEqual(quiz.name, "Five questions")
        renamed = database.update_quiz_name(quiz.id, "Renamed quiz")
        self.assertEqual(renamed.name, "Renamed quiz")
        self.assertEqual(database.get_quiz(quiz.id), renamed)

        attempt = database.save_attempt(
            quiz.id,
            {str(index): "A" for index in range(5)},
            5,
            5,
            "2026-07-16T10:00:00Z",
        )
        self.assertTrue(database.delete_quiz(quiz.id))
        self.assertFalse(database.delete_quiz(quiz.id))
        self.assertIsNone(database.get_quiz(quiz.id))
        self.assertIsNone(database.get_attempt(attempt.id))

    def test_migrates_v1_quiz_names(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "legacy.sqlite3"
            connection = sqlite3.connect(db_path)
            try:
                connection.executescript(
                    """
                    PRAGMA foreign_keys = ON;
                    CREATE TABLE transcripts (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        name TEXT NOT NULL,
                        content TEXT NOT NULL,
                        created_at TEXT NOT NULL
                    );
                    CREATE TABLE quizzes (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        transcript_id INTEGER NOT NULL,
                        questions_json TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        FOREIGN KEY (transcript_id) REFERENCES transcripts(id) ON DELETE CASCADE
                    );
                    CREATE TABLE attempts (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        quiz_id INTEGER NOT NULL,
                        user_answers_json TEXT NOT NULL,
                        score INTEGER NOT NULL,
                        total INTEGER NOT NULL,
                        started_at TEXT NOT NULL,
                        completed_at TEXT NOT NULL,
                        FOREIGN KEY (quiz_id) REFERENCES quizzes(id) ON DELETE CASCADE
                    );
                    INSERT INTO transcripts(name, content, created_at)
                    VALUES ('Legacy', 'Old content', '2026-07-16T10:00:00Z');
                    PRAGMA user_version = 1;
                    """
                )
                connection.execute(
                    """
                    INSERT INTO quizzes(transcript_id, questions_json, created_at)
                    VALUES (1, ?, '2026-07-16T10:01:00Z')
                    """,
                    (json.dumps(_quiz_data()),),
                )
                connection.commit()
            finally:
                connection.close()

            database = Database(db_path)
            migrated = database.get_quiz(1)
            self.assertIsNotNone(migrated)
            self.assertEqual(migrated.name if migrated else None, "Quiz 1")
            self.assertEqual(migrated.questions if migrated else None, _quiz_data())
            connection = sqlite3.connect(db_path)
            try:
                self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 2)
                self.assertIn(
                    "name",
                    {
                        row[1]
                        for row in connection.execute("PRAGMA table_info(quizzes)").fetchall()
                    },
                )
            finally:
                connection.close()

    def test_dynamic_question_counts_and_attempt_scoring(self) -> None:
        database = Database(":memory:")
        transcript = database.save_transcript("Counts", "Content")
        for count in (5, 15):
            with self.subTest(count=count):
                quiz = database.save_quiz(transcript.id, _quiz_data(count))
                attempt = database.save_attempt(
                    quiz.id,
                    {str(index): "A" for index in range(count)},
                    count,
                    count,
                    "2026-07-16T10:00:00Z",
                )
                self.assertEqual(attempt.score, count)
                self.assertEqual(attempt.total, count)

    def test_rejects_question_counts_outside_safe_range(self) -> None:
        database = Database(":memory:")
        transcript = database.save_transcript("Counts", "Content")
        for questions in ([], _quiz_data(51)):
            with self.subTest(count=len(questions)):
                with self.assertRaises(ValueError):
                    database.save_quiz(transcript.id, questions)

    def test_database_validation(self) -> None:
        database = Database(":memory:")
        with self.assertRaises(ValueError):
            database.save_transcript(" ", "content")
        transcript = database.save_transcript("Name", "Content")
        with self.assertRaises(ValueError):
            database.save_quiz(transcript.id, [{"bad": float("nan")}])
        with self.assertRaises(ValueError):
            database.save_quiz(transcript.id, [{"bad": object()}])
        with self.assertRaises(ValueError):
            database.save_attempt(999, {}, 0, 1, "start")
        quiz = database.save_quiz(transcript.id, _quiz_data())
        with self.assertRaises(ValueError):
            database.save_attempt(
                quiz.id,
                {str(index): "A" for index in range(10)},
                0,
                10,
                "start",
            )


class QuizValidationTests(unittest.TestCase):
    def test_valid_json_and_exact_json_fence(self) -> None:
        encoded = json.dumps(_quiz_data())
        self.assertEqual(validate_quiz_output(encoded), _quiz_data())
        self.assertEqual(
            validate_quiz_output(json.dumps({"questions": _quiz_data()})),
            _quiz_data(),
        )
        self.assertEqual(validate_quiz_output(f"```json\n{encoded}\n```"), _quiz_data())

    def test_variable_question_counts(self) -> None:
        for count in (5, 15):
            with self.subTest(count=count):
                encoded = json.dumps({"questions": _quiz_data(count)})
                self.assertEqual(validate_quiz_output(encoded, count), _quiz_data(count))

    def test_rejects_invalid_question_counts(self) -> None:
        for count in (0, 51, True, 5.0, "5"):
            with self.subTest(count=count):
                with self.assertRaises(ValueError):
                    validate_quiz_output(json.dumps({"questions": _quiz_data()}), count)

    def test_rejects_non_strict_output(self) -> None:
        cases = [
            json.dumps(_quiz_data()[:9]),
            json.dumps(_quiz_data()) + " trailing",
            "```JSON\n" + json.dumps(_quiz_data()) + "\n```",
            json.dumps(
                [dict(item, unexpected=True) if index == 0 else item for index, item in enumerate(_quiz_data())]
            ),
            json.dumps(
                [
                    {
                        **item,
                        "options": {**item["options"], "B": item["options"]["A"]},
                    }
                    if index == 0
                    else item
                    for index, item in enumerate(_quiz_data())
                ]
            ),
        ]
        duplicate_first = (
            '{"question":"One?","question":"Two?","options":'
            '{"A":"a","B":"b","C":"c","D":"d"},"answer":"A"}'
        )
        cases.append("[" + duplicate_first + "," + ",".join(json.dumps(x) for x in _quiz_data()[1:]) + "]")
        for value in cases:
            with self.subTest(value=value[:40]):
                with self.assertRaises(QuizValidationError):
                    validate_quiz_output(value)


class _QueueOutput:
    def __init__(self) -> None:
        self.items: queue.Queue[bytes] = queue.Queue()

    def put_message(self, message: dict) -> None:
        self.items.put((json.dumps(message, separators=(",", ":")) + "\n").encode("utf-8"))

    def readline(self, size: int = -1) -> bytes:
        return self.items.get(timeout=5)

    def close_stream(self) -> None:
        self.items.put(b"")


class _EmptyErrorStream:
    def read(self, size: int = -1) -> bytes:
        return b""


class _FakeInput:
    def __init__(self, process: "_FakeProcess") -> None:
        self.process = process
        self.buffer = b""
        self.closed = False

    def write(self, data: bytes) -> int:
        self.buffer += data
        while b"\n" in self.buffer:
            line, self.buffer = self.buffer.split(b"\n", 1)
            self.process.handle(json.loads(line.decode("utf-8")))
        return len(data)

    def flush(self) -> None:
        return None

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            self.process.returncode = 0
            self.process.stdout.close_stream()


class _FakeProcess:
    instances: list["_FakeProcess"] = []

    def __init__(self, args: list[str], **kwargs: object) -> None:
        self.args = args
        self.kwargs = kwargs
        self.stdout = _QueueOutput()
        self.stderr = _EmptyErrorStream()
        self.stdin = _FakeInput(self)
        self.returncode: int | None = None
        self.messages: list[dict] = []
        self.signed_in = False
        self.cwd_seen: str | None = None
        self.cwd_was_empty = False
        self.emit_duplicate_first_turn = False
        self.emit_overlap_first_turn = False
        self.thread_count = 0
        self.turn_count = 0
        self.__class__.instances.append(self)

    def _respond(self, request: dict, result: object) -> None:
        self.stdout.put_message({"id": request["id"], "result": result})

    def _notify(self, method: str, params: dict) -> None:
        self.stdout.put_message({"method": method, "params": params})

    def handle(self, message: dict) -> None:
        self.messages.append(message)
        method = message.get("method")
        if method == "initialize":
            self._respond(
                message,
                {
                    "userAgent": "codex_cli_rs/0.144.5",
                    "codexHome": self.kwargs["env"]["CODEX_HOME"],
                    "platformFamily": "windows",
                    "platformOs": "windows",
                },
            )
        elif method == "initialized":
            return
        elif method == "config/read":
            self._respond(
                message,
                {
                    "config": {
                        "model_provider": "openai",
                        "openai_base_url": "https://chatgpt.com/backend-api/codex",
                        "chatgpt_base_url": "https://chatgpt.com/backend-api",
                        "forced_login_method": "chatgpt",
                        "cli_auth_credentials_store": "keyring",
                        "mcp_oauth_credentials_store": "keyring",
                        "approval_policy": "never",
                        "sandbox_mode": "read-only",
                        "web_search": "disabled",
                        "project_doc_max_bytes": 0,
                        "allow_login_shell": False,
                        "include_apps_instructions": False,
                        "include_collaboration_mode_instructions": False,
                        "include_environment_context": False,
                        "include_permissions_instructions": False,
                        "model_providers": {},
                        "mcp_servers": {},
                        "plugins": {},
                        "marketplaces": {},
                        "features": {feature: False for feature in _DISABLED_CODEX_FEATURES},
                        "apps": {
                            "_default": {
                                "enabled": False,
                                "destructive_enabled": False,
                                "open_world_enabled": False,
                            }
                        },
                        "shell_environment_policy": {"inherit": "none"},
                        "skills": {
                            "include_instructions": False,
                            "bundled": {"enabled": False},
                        },
                        "orchestrator": {
                            "skills": {"enabled": False},
                            "mcp": {"enabled": False},
                        },
                        "history": {"persistence": "none"},
                        "analytics": {"enabled": False},
                        "feedback": {"enabled": False},
                        "otel": {
                            "exporter": "none",
                            "trace_exporter": "none",
                            "metrics_exporter": "none",
                            "log_user_prompt": False,
                        },
                        "hooks": None,
                        "tools": None,
                    },
                    "origins": {},
                    "layers": [
                        {
                            "name": {
                                "type": "user",
                                "file": str(Path(self.kwargs["env"]["CODEX_HOME"]) / "config.toml"),
                            },
                            "version": "1",
                            "config": {},
                        }
                    ],
                },
            )
        elif method == "configRequirements/read":
            self._respond(message, {"requirements": None})
        elif method == "account/read":
            account = (
                {"type": "chatgpt", "email": "learner@example.test", "planType": "plus"}
                if self.signed_in
                else None
            )
            self._respond(message, {"account": account, "requiresOpenaiAuth": True})
        elif method == "account/login/start":
            self._notify(
                "account/login/completed",
                {"loginId": "unrelated", "success": True},
            )
            self._notify("account/updated", {"loginId": "login-1"})
            self._notify(
                "account/login/completed",
                {"loginId": "login-1", "success": True},
            )
            self.signed_in = True
            self._respond(
                message,
                {
                    "type": "chatgptDeviceCode",
                    "loginId": "login-1",
                    "verificationUrl": "https://example.test/device",
                    "userCode": "ABCD-EFGH",
                },
            )
        elif method == "account/logout":
            self.signed_in = False
            self._respond(message, {})
        elif method == "account/login/cancel":
            self._respond(message, {})
        elif method == "model/list":
            params = message["params"]
            if params["cursor"] is None:
                self._respond(
                    message,
                    {
                        "data": [{"model": "visible-other", "isDefault": False}],
                        "nextCursor": "page-2",
                    },
                )
            else:
                self._respond(
                    message,
                    {
                        "data": [{"model": "account-default", "isDefault": True}],
                        "nextCursor": None,
                    },
                )
        elif method == "thread/start":
            cwd = message["params"]["cwd"]
            self.thread_count += 1
            thread_id = f"thread-good-{self.thread_count}"
            self.cwd_seen = cwd
            self.cwd_was_empty = os.path.isdir(cwd) and not os.listdir(cwd)
            self._respond(
                message,
                {
                    "thread": {
                        "id": thread_id,
                        "modelProvider": "openai",
                        "ephemeral": True,
                        "path": None,
                        "cwd": cwd,
                    },
                    "model": message["params"]["model"],
                    "modelProvider": "openai",
                    "cwd": cwd,
                    "approvalPolicy": "never",
                    "sandbox": {"type": "readOnly", "networkAccess": False},
                    "instructionSources": [],
                },
            )
        elif method == "turn/start":
            self.turn_count += 1
            thread_id = message["params"]["threadId"]
            turn_id = f"turn-good-{self.turn_count}"
            question_count = message["params"]["outputSchema"]["properties"]["questions"]["minItems"]
            if self.emit_duplicate_first_turn and self.turn_count == 1:
                duplicate_data = _quiz_data(question_count)
                duplicate_data[min(4, question_count - 1)]["options"]["B"] = duplicate_data[
                    min(4, question_count - 1)
                ]["options"]["A"]
                output = json.dumps({"questions": duplicate_data})
            elif self.emit_overlap_first_turn and self.turn_count == 1:
                output = json.dumps({"questions": _quiz_data(question_count)})
            elif self.emit_overlap_first_turn and self.turn_count == 2:
                fresh_data = _quiz_data(question_count)
                for index, item in enumerate(fresh_data, start=1):
                    item["question"] = f"Fresh question {index}?"
                output = json.dumps({"questions": fresh_data})
            else:
                output = json.dumps({"questions": _quiz_data(question_count)})
            self._notify(
                "item/completed",
                {
                    "threadId": "thread-other",
                    "turnId": "turn-other",
                    "item": {
                        "type": "agentMessage",
                        "phase": "final_answer",
                        "text": json.dumps(_quiz_data()[:1]),
                    },
                },
            )
            self._notify(
                "item/completed",
                {
                    "threadId": thread_id,
                    "turnId": turn_id,
                    "item": {
                        "type": "agentMessage",
                        "phase": "final_answer",
                        "text": output,
                    },
                },
            )
            self._notify(
                "turn/completed",
                {
                    "threadId": thread_id,
                    "turnId": turn_id,
                    "turn": {"id": turn_id, "status": "completed", "error": None},
                },
            )
            self._respond(message, {"turn": {"id": turn_id, "status": "inProgress"}})
        elif method == "turn/interrupt":
            self._respond(message, {})
        elif "id" in message and "error" in message:
            return
        else:
            raise AssertionError(f"Unexpected request: {message}")

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        if self.returncode is None:
            raise AssertionError("wait called before stdin was closed")
        return self.returncode

    def terminate(self) -> None:
        self.returncode = -15
        self.stdout.close_stream()

    def kill(self) -> None:
        self.returncode = -9
        self.stdout.close_stream()


class AppServerIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        _FakeProcess.instances.clear()
        self.temp_directory = tempfile.TemporaryDirectory()
        self.codex_home = Path(self.temp_directory.name) / "codex"
        self.popen_patch = mock.patch("api_handler.subprocess.Popen", _FakeProcess)
        self.which_patch = mock.patch("api_handler.shutil.which", return_value="C:\\fake\\codex.exe")
        self.popen_patch.start()
        self.which_patch.start()

    def tearDown(self) -> None:
        self.which_patch.stop()
        self.popen_patch.stop()
        self.temp_directory.cleanup()

    def test_initialize_and_device_sign_in(self) -> None:
        challenges: list[DeviceChallenge] = []
        with mock.patch.dict(
            os.environ,
            {
                "OPENAI_API_KEY": "must-not-reach-child",
                "CODEX_ACCESS_TOKEN": "must-not-reach-child",
            },
        ):
            api = ApiHandler(
                request_timeout=2,
                generation_timeout=5,
                codex_home=self.codex_home,
            )
            manager = AuthManager(api)
            self.assertFalse(manager.check_status().signed_in)
            process = _FakeProcess.instances[-1]
            self.assertEqual(
                process.args[1:],
                ["app-server", "--strict-config", "--listen", "stdio://"],
            )
            self.assertNotIn("OPENAI_API_KEY", process.kwargs["env"])
            self.assertNotIn("CODEX_ACCESS_TOKEN", process.kwargs["env"])
            self.assertEqual(process.kwargs["env"]["CODEX_HOME"], str(self.codex_home.resolve()))
            self.assertEqual(process.messages[0]["method"], "initialize")
            self.assertIn("id", process.messages[0])
            self.assertEqual(process.messages[1], {"method": "initialized"})
            self.assertEqual(process.messages[2]["method"], "config/read")
            self.assertEqual(process.messages[3]["method"], "configRequirements/read")

            with mock.patch("auth_manager.webbrowser.open", return_value=True) as browser:
                status = manager.sign_in(challenges.append)
            self.assertTrue(status.signed_in)
            self.assertEqual(status.email, "learner@example.test")
            self.assertEqual(challenges[0].user_code, "ABCD-EFGH")
            browser.assert_called_once_with("https://example.test/device")
            manager.sign_out()
            self.assertFalse(manager.check_status().signed_in)
            api.close()

    def test_models_and_generation_correlate_events(self) -> None:
        api = ApiHandler(
            request_timeout=2,
            generation_timeout=5,
            codex_home=self.codex_home,
        )
        models = api.list_models()
        self.assertEqual([model["model"] for model in models], ["visible-other", "account-default"])
        progress: list[str] = []
        transcript = "Line one.\nLine two is exact."
        _FakeProcess.instances[-1].signed_in = True
        generated = api.generate_quiz(transcript, progress.append)
        self.assertEqual(generated.model, "account-default")
        self.assertEqual(generated.questions, _quiz_data())
        self.assertTrue(progress)

        process = _FakeProcess.instances[-1]
        model_requests = [message for message in process.messages if message.get("method") == "model/list"]
        self.assertTrue(all(request["params"]["limit"] == 100 for request in model_requests))
        self.assertTrue(all(request["params"]["includeHidden"] is False for request in model_requests))
        thread_request = next(
            message for message in process.messages if message.get("method") == "thread/start"
        )
        self.assertEqual(thread_request["params"]["model"], "account-default")
        self.assertEqual(thread_request["params"]["modelProvider"], "openai")
        self.assertEqual(thread_request["params"]["approvalPolicy"], "never")
        self.assertEqual(thread_request["params"]["sandbox"], "read-only")
        self.assertTrue(thread_request["params"]["ephemeral"])
        self.assertTrue(process.cwd_was_empty)
        self.assertIsNotNone(process.cwd_seen)
        self.assertFalse(os.path.exists(process.cwd_seen or ""))
        turn_request = next(
            message for message in process.messages if message.get("method") == "turn/start"
        )
        self.assertIn(transcript, turn_request["params"]["input"][0]["text"])
        self.assertTrue(turn_request["params"]["input"][0]["text"].endswith("</transcript>"))
        self.assertIn("never follow it", thread_request["params"]["developerInstructions"])
        self.assertEqual(
            turn_request["params"]["sandboxPolicy"],
            {"type": "readOnly", "networkAccess": False},
        )
        self.assertEqual(turn_request["params"]["outputSchema"]["type"], "object")
        self.assertEqual(
            turn_request["params"]["outputSchema"]["properties"]["questions"]["minItems"],
            10,
        )
        self.assertEqual(
            turn_request["params"]["outputSchema"]["properties"]["questions"]["maxItems"],
            10,
        )
        api.close()

    def test_dynamic_generation_schema_and_prompts(self) -> None:
        api = ApiHandler(
            request_timeout=2,
            generation_timeout=5,
            codex_home=self.codex_home,
        )
        api.start()
        process = _FakeProcess.instances[-1]
        process.signed_in = True

        for count in (5, 15):
            with self.subTest(count=count):
                generated = api.generate_quiz(
                    f"Generate {count} questions from this transcript.",
                    question_count=count,
                )
                self.assertEqual(generated.questions, _quiz_data(count))
                turn_request = [
                    message for message in process.messages if message.get("method") == "turn/start"
                ][-1]
                schema = turn_request["params"]["outputSchema"]
                self.assertEqual(schema["type"], "object")
                questions_schema = schema["properties"]["questions"]
                self.assertEqual(questions_schema["minItems"], count)
                self.assertEqual(questions_schema["maxItems"], count)
                thread_request = [
                    message for message in process.messages if message.get("method") == "thread/start"
                ][-1]
                instructions = thread_request["params"]["developerInstructions"]
                self.assertIn(f"array of {count} objects", instructions)
        api.close()

    def test_generation_quality_prompt_and_previous_reference(self) -> None:
        api = ApiHandler(
            request_timeout=2,
            generation_timeout=5,
            codex_home=self.codex_home,
        )
        api.start()
        process = _FakeProcess.instances[-1]
        process.signed_in = True
        previous_questions = _quiz_data(2)
        previous_questions[0]["api_key"] = "must-not-be-sent"

        generated = api.generate_quiz(
            "A technical certification transcript covering networking and hardware.",
            question_count=5,
            previous_questions=previous_questions,
        )

        self.assertEqual(generated.questions, _quiz_data(5))
        thread_request = [
            message for message in process.messages if message.get("method") == "thread/start"
        ][-1]
        instructions = thread_request["params"]["developerInstructions"]
        for phrase in (
            "topic clusters",
            "allocate questions as evenly as possible",
            "before revisiting a topic",
            "recall/recognition",
            "application/compare",
            "troubleshooting/next-best-action",
            "original practice content",
            "not an official CompTIA question",
            "exam prediction",
            "same-category distractors",
            "materially new",
            "ceil(requested_count/2)",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, instructions)

        turn_request = [
            message for message in process.messages if message.get("method") == "turn/start"
        ][-1]
        prompt = turn_request["params"]["input"][0]["text"]
        self.assertIn("<prior_questions_reference>", prompt)
        self.assertIn("<previous_questions>", prompt)
        self.assertIn("Question 1?", prompt)
        self.assertIn("Question 2?", prompt)
        self.assertNotIn("must-not-be-sent", prompt)
        self.assertIn("</prior_questions_reference>", prompt)
        api.close()

    def test_prior_exact_overlap_uses_one_repair_attempt(self) -> None:
        api = ApiHandler(
            request_timeout=2,
            generation_timeout=5,
            codex_home=self.codex_home,
        )
        api.start()
        process = _FakeProcess.instances[-1]
        process.signed_in = True
        process.emit_overlap_first_turn = True
        progress: list[str] = []

        generated = api.generate_quiz(
            "A transcript with material for a fresh practice quiz.",
            previous_questions=_quiz_data(),
            on_progress=progress.append,
        )

        self.assertEqual(generated.questions[0]["question"], "Fresh question 1?")
        turn_requests = [message for message in process.messages if message.get("method") == "turn/start"]
        thread_requests = [message for message in process.messages if message.get("method") == "thread/start"]
        self.assertEqual(len(turn_requests), 2)
        self.assertEqual(len(thread_requests), 2)
        self.assertTrue(any("repair" in message.casefold() for message in progress))
        retry_instructions = thread_requests[1]["params"]["developerInstructions"]
        for phrase in (
            "topic clusters",
            "application/compare",
            "troubleshooting/next-best-action",
            "original practice content",
            "materially new",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, retry_instructions)
        api.close()

    def test_generation_rejects_invalid_question_counts(self) -> None:
        api = ApiHandler(
            request_timeout=2,
            generation_timeout=5,
            codex_home=self.codex_home,
        )
        for count in (0, 51, True, 5.0, "5"):
            with self.subTest(count=count):
                with self.assertRaises(ValueError):
                    api.generate_quiz("A valid transcript.", question_count=count)
        self.assertEqual(len(_FakeProcess.instances), 0)

    def test_preferred_model_is_catalog_checked_and_invalid_falls_back(self) -> None:
        api = ApiHandler(
            request_timeout=2,
            generation_timeout=5,
            codex_home=self.codex_home,
        )
        available = api.list_available_models()
        process = _FakeProcess.instances[-1]
        process.signed_in = True
        self.assertEqual([model["model"] for model in available], ["visible-other", "account-default"])
        api.set_preferred_model("visible-other")
        self.assertEqual(api.get_preferred_model(), "visible-other")
        generated = api.generate_quiz("Use the preferred visible model.")
        self.assertEqual(generated.model, "visible-other")
        first_thread = [
            message for message in process.messages if message.get("method") == "thread/start"
        ][0]
        self.assertEqual(first_thread["params"]["model"], "visible-other")

        api.set_preferred_model("not-in-the-live-catalog")
        fallback = api.generate_quiz("Fall back when the selected model disappears.")
        self.assertEqual(fallback.model, "account-default")
        self.assertIsNone(api.get_preferred_model())
        thread_requests = [
            message for message in process.messages if message.get("method") == "thread/start"
        ]
        self.assertEqual(thread_requests[-1]["params"]["model"], "account-default")
        api.close()

    def test_generation_repairs_duplicate_option_text(self) -> None:
        api = ApiHandler(
            request_timeout=2,
            generation_timeout=5,
            codex_home=self.codex_home,
        )
        api.start()
        process = _FakeProcess.instances[-1]
        process.signed_in = True
        process.emit_duplicate_first_turn = True
        progress: list[str] = []

        generated = api.generate_quiz("A transcript with educational content.", progress.append)

        self.assertEqual(generated.questions, _quiz_data())
        turn_requests = [message for message in process.messages if message.get("method") == "turn/start"]
        thread_requests = [message for message in process.messages if message.get("method") == "thread/start"]
        self.assertEqual(len(turn_requests), 2)
        self.assertEqual(len(thread_requests), 2)
        self.assertEqual(
            len({request["params"]["threadId"] for request in turn_requests}),
            2,
        )
        self.assertTrue(any("repair" in message.casefold() or "retry" in message.casefold() for message in progress))
        retry_instructions = thread_requests[1]["params"]["developerInstructions"]
        self.assertIn("meaningfully distinct", retry_instructions)
        self.assertIn("only correct option", retry_instructions)
        self.assertIn("conform exactly", retry_instructions)
        self.assertEqual(turn_requests[0]["params"]["outputSchema"], turn_requests[1]["params"]["outputSchema"])
        api.close()

    def test_poisoned_transport_restarts_on_next_request(self) -> None:
        api = ApiHandler(
            request_timeout=2,
            generation_timeout=5,
            codex_home=self.codex_home,
        )
        api.start()
        first_process = _FakeProcess.instances[-1]
        api._record_transport_failure("synthetic malformed protocol message")
        models = api.list_models()
        self.assertEqual(len(_FakeProcess.instances), 2)
        self.assertIsNot(_FakeProcess.instances[-1], first_process)
        self.assertEqual(models[-1]["model"], "account-default")
        api.close()


if __name__ == "__main__":
    unittest.main()
