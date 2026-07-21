from __future__ import annotations

import queue
import re
import threading
import time
import webbrowser
from datetime import datetime, timezone
from typing import Any, Callable
from tkinter import messagebox, simpledialog

import customtkinter as ctk

from api_handler import (
    ApiError,
    ApiHandler,
    AuthRequiredError,
    CodexNotFoundError,
    GenerationCancelledError,
    QuizValidationError,
    _DEFAULT_PREFERRED_MODEL,
)
from auth_manager import AuthManager, AuthStatus, DeviceChallenge
from database import AttemptRecord, Database, QuizRecord, TranscriptRecord


ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")


# A small, deliberate palette keeps the interface calm while giving actions and
# states a clear visual language.  The old color names are retained because the
# rest of the app uses them as semantic roles (rather than raw colors).
COLORS = {
    "window": "#080A0D",       # application background
    "panel": "#11151A",        # primary surface
    "panel_alt": "#191E25",    # elevated/input surface
    "panel_hover": "#252D36",  # hover surface
    "border": "#2A333D",
    "border_strong": "#3D4956",
    "text": "#F4F6F8",
    "muted": "#9AA5B0",
    "subtle": "#6F7B87",
    "accent": "#2D6BFF",       # restrained electric blue
    "accent_hover": "#3D73E8",
    "accent_soft": "#142A52",
    "on_accent": "#FFFFFF",
    "focus": "#9AB7FF",
    "teal": "#40C995",         # success
    "green": "#40C995",        # success
    "green_dark": "#12392C",   # success surface
    "success_surface": "#12392C",
    "success_surface_hover": "#194936",
    "success_text": "#B6F0D1",
    "success_border": "#40C995",
    "red": "#F0717B",          # error
    "red_dark": "#42232B",     # error surface
    "danger_text": "#FFD9DE",
    "amber": "#D8AB55",        # warning
    "amber_dark": "#3D3018",
    "warning_text": "#F3D58E",
    "disabled_text": "#74808C",
}

FONT_DISPLAY = "Bahnschrift"
FONT_BODY = "Segoe UI"
FONT_MONO = "Consolas"


def _font(size: int, weight: str = "normal", family: str = FONT_BODY) -> ctk.CTkFont:
    return ctk.CTkFont(family=family, size=size, weight=weight)

QUESTION_COUNT_CHOICES = (5, 10, 15, 20, 25, 30, 35, 40, 45, 50)
DEFAULT_QUESTION_COUNT = 10
QUESTION_COUNT_MIN = 1
QUESTION_COUNT_MAX = 50
MAX_PREVIOUS_QUESTIONS_FOR_GENERATION = 100


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_datetime(value: str) -> datetime:
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _format_datetime(value: str) -> str:
    try:
        return _parse_datetime(value).astimezone().strftime("%b %d, %Y · %I:%M %p")
    except (TypeError, ValueError):
        return value


def _date_group(value: str) -> str:
    try:
        date = _parse_datetime(value).astimezone().date()
        today = datetime.now().astimezone().date()
        delta = (today - date).days
        if delta == 0:
            return "Today"
        if delta == 1:
            return "Yesterday"
        return date.strftime("%B %Y")
    except (TypeError, ValueError):
        return "Earlier"


def _elapsed_seconds(started_at: str, completed_at: str) -> int:
    try:
        return max(0, int((_parse_datetime(completed_at) - _parse_datetime(started_at)).total_seconds()))
    except (TypeError, ValueError):
        return 0


def _format_duration(seconds: int) -> str:
    minutes, seconds = divmod(max(0, seconds), 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours:d}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:d}:{seconds:02d}"


def _ellipsize(value: Any, limit: int) -> str:
    text = " ".join(str(value).split())
    if len(text) <= limit:
        return text
    return f"{text[: max(1, limit - 1)].rstrip()}…"


_SECRET_PATTERNS = (
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b"),
    re.compile(r"(?i)(?:access[_ -]?token|refresh[_ -]?token|id[_ -]?token|api[_ -]?key|password|secret)\s*[:=]\s*\S+"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}(?:\.[A-Za-z0-9_-]{8,})?\b"),
)


def _safe_exception_message(value: BaseException | Any, fallback: str = "Unexpected error") -> str:
    """Return useful exception text without putting credentials in the UI."""

    try:
        message = str(value)
    except BaseException:
        message = ""
    message = " ".join(message.split())
    for pattern in _SECRET_PATTERNS:
        message = pattern.sub("[redacted]", message)
    return message[:500] or fallback


def _clear_children(widget: Any) -> None:
    for child in widget.winfo_children():
        child.destroy()


class PreviousQuizContextError(RuntimeError):
    """Raised when the prior-question context cannot be loaded safely."""


class DeviceLoginDialog(ctk.CTkToplevel):
    def __init__(
        self,
        master: "QuizApp",
        challenge: DeviceChallenge,
        on_cancel: Callable[[], None],
    ) -> None:
        super().__init__(master)
        self.challenge = challenge
        self.on_cancel = on_cancel
        self._finished = False

        self.title("OpenAI sign-in")
        self.geometry("580x410")
        self.resizable(False, False)
        self.configure(fg_color=COLORS["window"])
        self.transient(master)
        self.grab_set()
        self.protocol("WM_DELETE_WINDOW", self.cancel)

        shell = ctk.CTkFrame(
            self,
            fg_color=COLORS["panel"],
            border_width=1,
            border_color=COLORS["border"],
            corner_radius=16,
        )
        shell.pack(fill="both", expand=True, padx=22, pady=22)

        ctk.CTkLabel(
            shell,
            text="OPENAI AUTHENTICATION",
            font=_font(10, "bold"),
            text_color=COLORS["accent_hover"],
        ).pack(anchor="w", padx=26, pady=(22, 7))

        ctk.CTkLabel(
            shell,
            text="Finish signing in",
            font=_font(24, "bold", FONT_DISPLAY),
            text_color=COLORS["text"],
        ).pack(anchor="w", padx=26, pady=(0, 6))
        ctk.CTkLabel(
            shell,
            text="Enter this one-time code on the OpenAI page. Your browser should already be open.",
            font=_font(13),
            text_color=COLORS["muted"],
            justify="left",
            wraplength=470,
        ).pack(anchor="w", padx=26, pady=(0, 20))

        code_frame = ctk.CTkFrame(
            shell,
            fg_color=COLORS["panel_alt"],
            border_width=1,
            border_color=COLORS["accent_soft"],
            corner_radius=12,
        )
        code_frame.pack(fill="x", padx=26)
        ctk.CTkLabel(
            code_frame,
            text=challenge.user_code,
            font=_font(30, "bold", FONT_MONO),
            text_color=COLORS["accent_hover"],
        ).pack(pady=(18, 6))
        ctk.CTkLabel(
            code_frame,
            text=challenge.verification_url,
            font=_font(12),
            text_color=COLORS["muted"],
        ).pack(pady=(0, 18))

        button_row = ctk.CTkFrame(shell, fg_color="transparent")
        button_row.pack(fill="x", padx=26, pady=(22, 10))
        button_row.grid_columnconfigure((0, 1, 2), weight=1)
        self.copy_button = ctk.CTkButton(
            button_row,
            text="Copy code",
            height=38,
            fg_color=COLORS["panel_alt"],
            hover_color=COLORS["panel_hover"],
            border_width=1,
            border_color=COLORS["border"],
            corner_radius=9,
            font=_font(12, "bold"),
            text_color=COLORS["text"],
            text_color_disabled=COLORS["disabled_text"],
            command=self.copy_code,
        )
        self.copy_button.grid(row=0, column=0, sticky="ew", padx=(0, 6))
        ctk.CTkButton(
            button_row,
            text="Open browser",
            height=38,
            fg_color=COLORS["accent"],
            hover_color=COLORS["accent_hover"],
            corner_radius=9,
            font=_font(12, "bold"),
            text_color=COLORS["on_accent"],
            text_color_disabled=COLORS["disabled_text"],
            command=lambda: webbrowser.open(challenge.verification_url),
        ).grid(row=0, column=1, sticky="ew", padx=6)
        self.cancel_button = ctk.CTkButton(
            button_row,
            text="Cancel",
            height=38,
            fg_color=COLORS["red_dark"],
            hover_color=COLORS["red"],
            corner_radius=9,
            font=_font(12, "bold"),
            text_color=COLORS["danger_text"],
            text_color_disabled=COLORS["disabled_text"],
            command=self.cancel,
        )
        self.cancel_button.grid(row=0, column=2, sticky="ew", padx=(6, 0))

        self.wait_label = ctk.CTkLabel(
            shell,
            text="Waiting for authorization…",
            text_color=COLORS["muted"],
            font=_font(12),
        )
        self.wait_label.pack(pady=(6, 0))
        self.bind("<Escape>", lambda _event: self.cancel())
        self.after(100, self.focus_force)

    def copy_code(self) -> None:
        if self._finished or not self.winfo_exists():
            return
        self.clipboard_clear()
        self.clipboard_append(self.challenge.user_code)
        self.copy_button.configure(text="Copied")
        self.after(1400, lambda: self.copy_button.configure(text="Copy code") if self.winfo_exists() else None)

    def cancel(self) -> None:
        if self._finished:
            return
        self._finished = True
        try:
            self.on_cancel()
        except BaseException:
            pass
        finally:
            try:
                self.grab_release()
            except BaseException:
                pass
            try:
                self.destroy()
            except BaseException:
                pass

    def complete(self) -> None:
        if self._finished:
            return
        self._finished = True
        try:
            self.grab_release()
        except BaseException:
            pass
        try:
            self.destroy()
        except BaseException:
            pass


class ReviewWindow(ctk.CTkToplevel):
    def __init__(self, master: "QuizApp", quiz: QuizRecord, attempt: AttemptRecord) -> None:
        super().__init__(master)
        self.app = master
        self._closed = False
        self._copy_reset_after: str | None = None
        self.title("Quiz review")
        self.geometry("960x800")
        self.minsize(760, 620)
        self.configure(fg_color=COLORS["window"])
        self.transient(master)
        self.protocol("WM_DELETE_WINDOW", self.close_window)

        score = attempt.score
        total = len(quiz.questions)
        percentage = round((score / total) * 100) if total else 0
        elapsed = _elapsed_seconds(attempt.started_at, attempt.completed_at)

        header = ctk.CTkFrame(
            self,
            fg_color=COLORS["panel"],
            border_width=1,
            border_color=COLORS["border"],
            corner_radius=0,
        )
        header.pack(fill="x")
        ctk.CTkLabel(
            header,
            text="Review results",
            font=_font(24, "bold", FONT_DISPLAY),
            text_color=COLORS["text"],
        ).pack(side="left", padx=26, pady=20)
        ctk.CTkLabel(
            header,
            text=f"{score}/{total}  ·  {percentage}%  ·  {_format_duration(elapsed)}",
            font=_font(15, "bold", FONT_MONO),
            text_color=COLORS["success_text"] if percentage >= 70 else COLORS["warning_text"],
            fg_color=COLORS["success_surface"] if percentage >= 70 else COLORS["amber_dark"],
            corner_radius=8,
        ).pack(side="right", padx=26, pady=16)

        body = ctk.CTkScrollableFrame(self, fg_color=COLORS["window"], corner_radius=0)
        body.pack(fill="both", expand=True, padx=18, pady=18)
        body.grid_columnconfigure(0, weight=1)

        for index, question in enumerate(quiz.questions):
            answer_key = str(index)
            user_answer = attempt.user_answers.get(answer_key)
            correct_answer = question.get("answer")
            is_correct = user_answer == correct_answer
            options = question.get("options", {})

            card = ctk.CTkFrame(
                body,
                fg_color=COLORS["panel"],
                border_width=1,
                border_color=COLORS["success_border"] if is_correct else COLORS["red"],
                corner_radius=12,
            )
            card.grid(row=index, column=0, sticky="ew", padx=4, pady=6)
            card.grid_columnconfigure(0, weight=1)

            status = "Correct" if is_correct else "Incorrect"
            ctk.CTkLabel(
                card,
                text=f"{index + 1}. {question.get('question', '')}",
                font=_font(15, "bold", FONT_DISPLAY),
                text_color=COLORS["text"],
                justify="left",
                anchor="w",
                wraplength=580,
            ).grid(row=0, column=0, sticky="ew", padx=16, pady=(15, 8))
            ctk.CTkLabel(
                card,
                text=status,
                font=_font(11, "bold"),
                text_color=COLORS["success_text"] if is_correct else COLORS["danger_text"],
                fg_color=COLORS["success_surface"] if is_correct else COLORS["red_dark"],
                corner_radius=7,
            ).grid(row=0, column=1, sticky="ne", padx=16, pady=(14, 8))

            user_text = "Unanswered"
            if user_answer in options:
                user_text = f"{user_answer}. {options[user_answer]}"
            correct_text = f"{correct_answer}. {options.get(correct_answer, '')}"
            ctk.CTkLabel(
                card,
                text=f"Your answer: {user_text}",
                font=_font(13),
                text_color=COLORS["success_text"] if is_correct else COLORS["danger_text"],
                justify="left",
                anchor="w",
                wraplength=580,
            ).grid(row=1, column=0, columnspan=2, sticky="ew", padx=16, pady=2)
            if not is_correct:
                ctk.CTkLabel(
                    card,
                    text=f"Correct answer: {correct_text}",
                    font=_font(13),
                    text_color=COLORS["success_text"],
                    justify="left",
                    anchor="w",
                    wraplength=580,
                ).grid(row=2, column=0, columnspan=2, sticky="ew", padx=16, pady=(2, 15))
            else:
                ctk.CTkLabel(card, text="", height=4).grid(row=2, column=0, pady=(0, 7))

        answer_key = "\n\n".join(
            "\n".join(
                (
                    f"Question {index + 1}: {question.get('question', '')}",
                    f"Answer {index + 1}: {question.get('answer', '')}. "
                    f"{question.get('options', {}).get(question.get('answer'), '')}",
                )
            )
            for index, question in enumerate(quiz.questions)
        )
        ctk.CTkLabel(
            body,
            text="Answer key",
            font=_font(16, "bold", FONT_DISPLAY),
            text_color=COLORS["text"],
            anchor="w",
        ).grid(row=len(quiz.questions), column=0, sticky="w", padx=4, pady=(20, 8))

        answer_key_card = ctk.CTkFrame(
            body,
            fg_color=COLORS["panel"],
            border_width=1,
            border_color=COLORS["border"],
            corner_radius=12,
        )
        answer_key_card.grid(
            row=len(quiz.questions) + 1,
            column=0,
            sticky="ew",
            padx=4,
            pady=(0, 12),
        )
        answer_key_card.grid_columnconfigure(0, weight=1)
        answer_key_toolbar = ctk.CTkFrame(answer_key_card, fg_color="transparent")
        answer_key_toolbar.grid(row=0, column=0, sticky="ew", padx=14, pady=(12, 6))
        answer_key_toolbar.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            answer_key_toolbar,
            text="Select, copy, or edit the answer key below.",
            font=_font(11),
            text_color=COLORS["muted"],
            anchor="w",
        ).grid(row=0, column=0, sticky="w")
        self.copy_answer_key_button = ctk.CTkButton(
            answer_key_toolbar,
            text="Copy answer key",
            width=142,
            height=32,
            fg_color=COLORS["accent"],
            hover_color=COLORS["accent_hover"],
            corner_radius=8,
            font=_font(11, "bold"),
            text_color=COLORS["on_accent"],
            text_color_disabled=COLORS["disabled_text"],
            command=self.copy_answer_key,
        )
        self.copy_answer_key_button.grid(row=0, column=1, padx=(10, 0))
        self.answer_key_textbox = ctk.CTkTextbox(
            answer_key_card,
            height=230,
            fg_color=COLORS["panel_alt"],
            border_width=1,
            border_color=COLORS["border"],
            corner_radius=9,
            wrap="word",
            font=_font(12, family=FONT_MONO),
            state="normal",
        )
        self.answer_key_textbox.grid(row=1, column=0, sticky="ew", padx=14, pady=(0, 14))
        self.answer_key_textbox.insert("1.0", answer_key)

        self.bind("<Escape>", lambda _event: self.close_window())

    def copy_answer_key(self) -> None:
        if self._closed or not self.winfo_exists():
            return
        try:
            answer_key = self.answer_key_textbox.get("1.0", "end-1c")
            self.clipboard_clear()
            self.clipboard_append(answer_key)
            self.copy_answer_key_button.configure(text="Copied")
            if self._copy_reset_after is not None:
                try:
                    self.after_cancel(self._copy_reset_after)
                except BaseException:
                    pass
            self._copy_reset_after = self.after(
                1400,
                lambda: self.copy_answer_key_button.configure(text="Copy answer key")
                if self.winfo_exists() and not self._closed
                else None,
            )
        except BaseException:
            pass

    def close_window(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._copy_reset_after is not None:
            try:
                self.after_cancel(self._copy_reset_after)
            except BaseException:
                pass
            self._copy_reset_after = None
        try:
            self.grab_release()
        except BaseException:
            pass
        try:
            self.destroy()
        except BaseException:
            pass
        try:
            self.app._untrack_review_window(self)
        except BaseException:
            pass


class QuizWindow(ctk.CTkToplevel):
    def __init__(self, master: "QuizApp", quiz: QuizRecord) -> None:
        super().__init__(master)
        self.app = master
        self.quiz = quiz
        self.answers: dict[str, str] = {}
        self.current_index = 0
        self.started_at = _utc_now()
        self.started_clock = time.monotonic()
        self._submitted = False
        self._submitting = False
        self._timer_after: str | None = None

        self.title("Take quiz")
        self.geometry("1040x800")
        self.minsize(820, 680)
        self.configure(fg_color=COLORS["window"])
        self.transient(master)
        self.grab_set()
        self.protocol("WM_DELETE_WINDOW", self.request_close)

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(2, weight=1)

        header = ctk.CTkFrame(
            self,
            fg_color=COLORS["panel"],
            border_width=1,
            border_color=COLORS["border"],
            corner_radius=0,
        )
        header.grid(row=0, column=0, sticky="ew")
        header.grid_columnconfigure(0, weight=1)
        self.heading_label = ctk.CTkLabel(
            header,
            text=f"Question 1 of {len(quiz.questions)}",
            font=_font(19, "bold", FONT_DISPLAY),
            text_color=COLORS["text"],
        )
        self.heading_label.grid(row=0, column=0, sticky="w", padx=26, pady=18)
        self.timer_label = ctk.CTkLabel(
            header,
            text="0:00",
            font=_font(14, "bold", FONT_MONO),
            text_color=COLORS["accent_hover"],
            fg_color=COLORS["accent_soft"],
            corner_radius=8,
        )
        self.timer_label.grid(row=0, column=1, padx=26, pady=14, ipadx=9, ipady=4)

        self.number_frame = ctk.CTkFrame(
            self,
            fg_color=COLORS["panel"],
            border_width=1,
            border_color=COLORS["border"],
            corner_radius=12,
        )
        self.number_frame.grid(row=1, column=0, sticky="ew", padx=26, pady=(16, 2))
        navigation_columns = min(10, max(1, len(quiz.questions)))
        self.number_frame.grid_columnconfigure(tuple(range(navigation_columns)), weight=1)
        self.number_buttons: list[ctk.CTkButton] = []
        for index in range(len(quiz.questions)):
            nav_row, nav_column = divmod(index, navigation_columns)
            button = ctk.CTkButton(
                self.number_frame,
                text=str(index + 1),
                width=42,
                height=32,
                corner_radius=8,
                font=_font(11, "bold", FONT_MONO),
                border_width=1,
                border_color=COLORS["border"],
                text_color=COLORS["text"],
                text_color_disabled=COLORS["disabled_text"],
                command=lambda value=index: self.go_to(value),
            )
            button.grid(row=nav_row, column=nav_column, sticky="ew", padx=3, pady=3)
            self.number_buttons.append(button)

        card = ctk.CTkFrame(
            self,
            fg_color=COLORS["panel"],
            border_width=1,
            border_color=COLORS["border"],
            corner_radius=16,
        )
        card.grid(row=2, column=0, sticky="nsew", padx=26, pady=16)
        card.grid_columnconfigure(0, weight=1)
        card.grid_rowconfigure(1, weight=1)

        self.question_label = ctk.CTkLabel(
            card,
            text="",
            font=_font(21, "bold", FONT_DISPLAY),
            text_color=COLORS["text"],
            justify="left",
            anchor="nw",
            wraplength=710,
        )
        self.question_label.grid(row=0, column=0, sticky="ew", padx=26, pady=(26, 16))

        options_frame = ctk.CTkFrame(card, fg_color="transparent")
        options_frame.grid(row=1, column=0, sticky="nsew", padx=28, pady=(0, 24))
        options_frame.grid_columnconfigure(0, weight=1)
        self.option_buttons: dict[str, ctk.CTkButton] = {}
        for row, letter in enumerate(("A", "B", "C", "D")):
            button = ctk.CTkButton(
                options_frame,
                text="",
                height=64,
                anchor="w",
                border_spacing=16,
                font=_font(14),
                corner_radius=10,
                border_width=1,
                text_color=COLORS["text"],
                text_color_disabled=COLORS["disabled_text"],
                command=lambda value=letter: self.choose(value),
            )
            button.grid(row=row, column=0, sticky="ew", pady=5)
            self.option_buttons[letter] = button

        footer = ctk.CTkFrame(self, fg_color="transparent")
        footer.grid(row=3, column=0, sticky="ew", padx=26, pady=(0, 24))
        footer.grid_columnconfigure(1, weight=1)
        self.previous_button = ctk.CTkButton(
            footer,
            text="Previous",
            width=120,
            height=42,
            corner_radius=9,
            font=_font(12, "bold"),
            fg_color=COLORS["panel_alt"],
            hover_color=COLORS["panel_hover"],
            border_width=1,
            border_color=COLORS["border"],
            text_color=COLORS["text"],
            text_color_disabled=COLORS["disabled_text"],
            command=self.previous,
        )
        self.previous_button.grid(row=0, column=0)
        self.answer_count_label = ctk.CTkLabel(
            footer,
            text=f"0 of {len(quiz.questions)} answered",
            text_color=COLORS["muted"],
            font=_font(12),
        )
        self.answer_count_label.grid(row=0, column=1)
        self.next_button = ctk.CTkButton(
            footer,
            text="Next",
            width=120,
            height=42,
            corner_radius=9,
            font=_font(12, "bold"),
            fg_color=COLORS["panel_alt"],
            hover_color=COLORS["panel_hover"],
            border_width=1,
            border_color=COLORS["border"],
            text_color=COLORS["text"],
            text_color_disabled=COLORS["disabled_text"],
            command=self.next,
        )
        self.next_button.grid(row=0, column=2, padx=(0, 10))
        self.submit_button = ctk.CTkButton(
            footer,
            text="Submit quiz",
            width=140,
            height=42,
            corner_radius=9,
            font=_font(12, "bold"),
            fg_color=COLORS["accent"],
            hover_color=COLORS["accent_hover"],
            text_color=COLORS["on_accent"],
            text_color_disabled=COLORS["disabled_text"],
            state="disabled",
            command=self.submit,
        )
        self.submit_button.grid(row=0, column=3)

        self.bind("<Left>", lambda _event: self.previous())
        self.bind("<Right>", lambda _event: self.next())
        self.bind("<Escape>", lambda _event: self.request_close())
        self.bind("<KeyPress>", self._key_answer)
        self.render()
        self._tick()
        self.after(100, self.focus_force)

    def _key_answer(self, event: Any) -> None:
        character = str(getattr(event, "char", "")).upper()
        mapping = {"1": "A", "2": "B", "3": "C", "4": "D"}
        if character in ("A", "B", "C", "D"):
            self.choose(character)
        elif character in mapping:
            self.choose(mapping[character])

    def _tick(self) -> None:
        if self._submitted or not self.winfo_exists():
            return
        self.timer_label.configure(text=_format_duration(int(time.monotonic() - self.started_clock)))
        self._timer_after = self.after(1000, self._tick)

    def go_to(self, index: int) -> None:
        if not self._submitted and not self._submitting and 0 <= index < len(self.quiz.questions):
            self.current_index = index
            self.render()

    def choose(self, letter: str) -> None:
        if self._submitted or self._submitting or letter not in {"A", "B", "C", "D"}:
            return
        self.answers[str(self.current_index)] = letter
        if self.current_index < len(self.quiz.questions) - 1:
            self.current_index += 1
        self.render()

    def previous(self) -> None:
        if not self._submitted and not self._submitting and self.current_index > 0:
            self.current_index -= 1
            self.render()

    def next(self) -> None:
        if (
            not self._submitted
            and not self._submitting
            and self.current_index < len(self.quiz.questions) - 1
        ):
            self.current_index += 1
            self.render()

    def render(self) -> None:
        total = len(self.quiz.questions)
        question = self.quiz.questions[self.current_index]
        selected = self.answers.get(str(self.current_index))
        self.heading_label.configure(text=f"Question {self.current_index + 1} of {total}")
        self.question_label.configure(text=question.get("question", ""))

        options = question.get("options", {})
        for letter, button in self.option_buttons.items():
            is_selected = selected == letter
            button.configure(
                text=f"{letter}. {options.get(letter, '')}",
                fg_color=COLORS["accent"] if is_selected else COLORS["panel_alt"],
                hover_color=COLORS["accent_hover"] if is_selected else COLORS["panel_hover"],
                border_color=COLORS["focus"] if is_selected else COLORS["border_strong"],
                text_color=COLORS["on_accent"] if is_selected else COLORS["text"],
                text_color_disabled=COLORS["disabled_text"],
            )

        for index, button in enumerate(self.number_buttons):
            if index == self.current_index:
                color = COLORS["accent"]
                hover = COLORS["accent_hover"]
                text = str(index + 1)
                text_color = COLORS["on_accent"]
                border = COLORS["focus"]
            elif str(index) in self.answers:
                color = COLORS["success_surface"]
                hover = COLORS["success_surface_hover"]
                text = f"✓ {index + 1}"
                text_color = COLORS["success_text"]
                border = COLORS["success_border"]
            else:
                color = COLORS["panel_alt"]
                hover = COLORS["panel_hover"]
                text = str(index + 1)
                text_color = COLORS["text"]
                border = COLORS["border"]
            button.configure(
                text=text,
                fg_color=color,
                hover_color=hover,
                border_color=border,
                text_color=text_color,
                text_color_disabled=COLORS["disabled_text"],
            )

        answered = len(self.answers)
        self.answer_count_label.configure(text=f"{answered} of {total} answered")
        self.previous_button.configure(state="normal" if self.current_index > 0 else "disabled")
        self.next_button.configure(state="normal" if self.current_index < total - 1 else "disabled")
        self.submit_button.configure(
            text="Submitting…" if self._submitting else "Submit quiz",
            state="disabled" if self._submitting or answered != total else "normal",
        )

    def _set_submission_controls(self, submitting: bool) -> None:
        self._submitting = submitting
        state = "disabled" if submitting else "normal"
        for button in (*self.number_buttons, *self.option_buttons.values()):
            button.configure(state=state)
        if submitting:
            self.previous_button.configure(state="disabled")
            self.next_button.configure(state="disabled")
            self.submit_button.configure(text="Submitting…", state="disabled")
        else:
            self.render()

    def submit(self) -> None:
        if self.app._closing or not self.winfo_exists() or self._submitted or self._submitting:
            return
        total = len(self.quiz.questions)
        if len(self.answers) != total:
            return

        self._set_submission_controls(True)
        self.app._set_status(f"Submitting {total}-question quiz…", "info")
        try:
            self.update_idletasks()
        except BaseException:
            pass

        completed_at = _utc_now()
        score = sum(
            1
            for index, question in enumerate(self.quiz.questions)
            if self.answers.get(str(index)) == question.get("answer")
        )
        database = self.app._get_database("save quiz attempt")
        if database is None:
            self._set_submission_controls(False)
            self.app._set_status("Could not save attempt: the local database is unavailable.", "error")
            return
        try:
            attempt = database.save_attempt(
                self.quiz.id,
                dict(self.answers),
                score,
                total,
                self.started_at,
                completed_at,
            )
        except BaseException as exc:
            self._set_submission_controls(False)
            detail = _safe_exception_message(exc)
            self.app._set_status(f"Could not save attempt: {detail}", "error")
            try:
                messagebox.showerror("Could not save attempt", detail, parent=self)
            except BaseException:
                pass
            return

        self._submitted = True
        if self._timer_after is not None:
            try:
                self.after_cancel(self._timer_after)
            except Exception:
                pass
        self.grab_release()
        self.destroy()
        self.app._quiz_window = None
        self.app._set_status(f"Quiz submitted: {score}/{total}.", "success")
        self.app._attempt_saved(self.quiz, attempt)

    def request_close(self) -> None:
        if self.app._closing:
            self.force_close()
            return
        if self._submitted or self._submitting:
            return
        if self.answers and not messagebox.askyesno(
            "Leave quiz?", "This unfinished attempt will not be saved.", parent=self
        ):
            return
        if self._timer_after is not None:
            try:
                self.after_cancel(self._timer_after)
            except Exception:
                pass
        self.grab_release()
        self.destroy()
        self.app._quiz_window = None

    def force_close(self) -> None:
        """Close without prompting when the parent application is shutting down."""

        self._submitted = True
        if self._timer_after is not None:
            try:
                self.after_cancel(self._timer_after)
            except BaseException:
                pass
        try:
            self.grab_release()
        except BaseException:
            pass
        try:
            self.destroy()
        except BaseException:
            pass
        self.app._quiz_window = None


class QuizApp(ctk.CTk):
    def __init__(self, db: Database | None = None, api: ApiHandler | None = None) -> None:
        super().__init__()
        self._db_error: BaseException | None = None
        try:
            self.db: Database | None = db if db is not None else Database()
        except BaseException as exc:
            self.db = None
            self._db_error = exc
        self.api = api or ApiHandler()
        self.auth = AuthManager(self.api)

        self.title("Transcript Quiz")
        self.geometry("1440x900")
        self.minsize(1280, 800)
        self.configure(fg_color=COLORS["window"])
        self.protocol("WM_DELETE_WINDOW", self._request_shutdown)

        self._ui_queue: queue.Queue[tuple[Any, ...]] = queue.Queue()
        self._async_handlers: dict[
            int, tuple[Callable[[Any], None], Callable[[BaseException], None]]
        ] = {}
        self._next_async_token = 1
        self._closing = False
        self._destroyed = False
        self._startup_running = False
        self._api_ready = False
        self._api_error: BaseException | None = None
        self._auth_status: AuthStatus | None = None
        self._auth_running = False
        self._login_cancel: threading.Event | None = None
        self._generation_cancel: threading.Event | None = None
        self._generation_token: int | None = None
        self._generation_busy = False
        self._pending_generation: TranscriptRecord | None = None
        self._pending_question_count: int | None = None
        self._device_dialog: DeviceLoginDialog | None = None
        self._quiz_window: QuizWindow | None = None
        self._review_windows: list[ReviewWindow] = []
        self._search_after: str | None = None
        self._startup_check_after: str | None = None
        self._shutdown_after: str | None = None
        self._status_message = ""
        self._status_kind = "info"
        self._model_entries: list[dict[str, Any]] = []
        self._model_display_to_id: dict[str, str] = {}
        self._model_id_to_display: dict[str, str] = {}
        self._model_default_id: str | None = None
        self._selected_model_id: str | None = None
        self._model_loading = False
        self._model_load_generation = 0
        self._model_load_token: int | None = None
        self._transcript_buttons: list[ctk.CTkButton] = []
        self._quiz_buttons: list[ctk.CTkButton] = []
        self._attempt_buttons: list[ctk.CTkButton] = []

        self.selected_transcript: TranscriptRecord | None = None
        self.selected_quiz: QuizRecord | None = None
        self._editor_snapshot = ("", "")

        self._build_header()
        self._build_library()
        self._build_status_bar()
        self._bind_shortcuts()
        self._refresh_transcripts()
        self._new_transcript(confirm=False)
        if self._db_error is not None:
            self._set_status(
                f"Database unavailable: {_safe_exception_message(self._db_error)}",
                "error",
            )
        else:
            self._set_status("Starting application…", "info")
        self._update_control_states()

        self.after(50, self._drain_ui_queue)
        self.after(120, self._start_backend)
        self._startup_check_after = self.after(300, self._startup_state_check)

    def _build_header(self) -> None:
        header = ctk.CTkFrame(
            self,
            height=86,
            fg_color=COLORS["panel"],
            border_width=1,
            border_color=COLORS["border"],
            corner_radius=0,
        )
        header.pack(fill="x")
        header.pack_propagate(False)

        title_group = ctk.CTkFrame(header, fg_color="transparent")
        title_group.pack(side="left", padx=24, pady=13)
        ctk.CTkLabel(
            title_group,
            text="TQ",
            width=38,
            height=38,
            font=_font(12, "bold", FONT_MONO),
            text_color=COLORS["accent_hover"],
            fg_color=COLORS["accent_soft"],
            corner_radius=9,
        ).pack(side="left", padx=(0, 12))
        title_text = ctk.CTkFrame(title_group, fg_color="transparent")
        title_text.pack(side="left")
        ctk.CTkLabel(
            title_text,
            text="Transcript Quiz",
            font=_font(23, "bold", FONT_DISPLAY),
            text_color=COLORS["text"],
        ).pack(anchor="w")
        ctk.CTkLabel(
            title_text,
            text="Build focused practice from saved transcripts.",
            font=_font(12),
            text_color=COLORS["muted"],
        ).pack(anchor="w")

        model_group = ctk.CTkFrame(header, fg_color="transparent")
        model_group.pack(side="left", padx=(28, 10), pady=10)
        ctk.CTkLabel(
            model_group,
            text="Model",
            font=_font(10, "bold"),
            text_color=COLORS["muted"],
        ).pack(anchor="w", pady=(0, 3))
        self.model_selector = ctk.CTkComboBox(
            model_group,
            width=300,
            height=36,
            values=["Sign in to load models"],
            state="disabled",
            fg_color=COLORS["panel_alt"],
            border_color=COLORS["border"],
            button_color=COLORS["accent"],
            button_hover_color=COLORS["accent_hover"],
            text_color=COLORS["text"],
            corner_radius=9,
            font=_font(12),
            command=self._model_selection_changed,
        )
        self.model_selector.pack(anchor="w")

        auth_group = ctk.CTkFrame(header, fg_color="transparent")
        auth_group.pack(side="right", padx=24, pady=14)
        self.auth_text = ctk.CTkLabel(
            auth_group,
            text="Checking sign-in…",
            width=200,
            font=_font(12),
            text_color=COLORS["muted"],
            anchor="e",
        )
        self.auth_text.pack(side="left", padx=(0, 12))
        self.auth_button = ctk.CTkButton(
            auth_group,
            text="Please wait",
            width=116,
            height=38,
            corner_radius=9,
            font=_font(12, "bold"),
            state="disabled",
            fg_color=COLORS["accent"],
            hover_color=COLORS["accent_hover"],
            text_color=COLORS["on_accent"],
            text_color_disabled=COLORS["disabled_text"],
            command=self._auth_action,
        )
        self.auth_button.pack(side="left")

    def _build_status_bar(self) -> None:
        self.status_frame = ctk.CTkFrame(
            self,
            fg_color=COLORS["panel_alt"],
            border_width=1,
            border_color=COLORS["border"],
            corner_radius=9,
            height=38,
        )
        self.status_frame.pack(fill="x", padx=18, pady=(0, 14))
        self.status_frame.pack_propagate(False)
        self.status_label = ctk.CTkLabel(
            self.status_frame,
            text="",
            font=_font(12),
            text_color=COLORS["muted"],
            anchor="w",
        )
        self.status_label.pack(fill="both", expand=True, padx=14)

    def _set_status(self, message: str, kind: str = "info") -> None:
        self._status_message = str(message)
        self._status_kind = kind
        if self._destroyed or not hasattr(self, "status_label"):
            return
        color = {
            "success": COLORS["success_text"],
            "error": COLORS["red"],
            "warning": COLORS["amber"],
            "info": COLORS["muted"],
        }.get(kind, COLORS["muted"])
        try:
            self.status_label.configure(
                text=_ellipsize(self._status_message, 170),
                text_color=color,
            )
            self.status_frame.configure(border_color=color if kind != "info" else COLORS["border"])
        except BaseException:
            pass

    def _get_database(self, operation: str = "database operation") -> Database | Any | None:
        database = getattr(self, "db", None)
        if database is not None:
            return database
        detail = _safe_exception_message(
            self._db_error or RuntimeError("the local database is unavailable")
        )
        self._set_status(f"{operation.capitalize()} unavailable: {detail}", "error")
        return None

    def _report_ui_callback_error(self, exc: BaseException, title: str = "UI update failed") -> None:
        if self._closing or self._destroyed:
            return
        self._set_status(f"{title}: {_safe_exception_message(exc)}", "error")

    def _controls_blocked(self) -> bool:
        return self._closing or self._generation_busy or self._generation_cancel is not None

    def _update_control_states(self) -> None:
        if self._destroyed:
            return
        try:
            blocked = self._controls_blocked()
            name, content = self._editor_values() if hasattr(self, "name_entry") else ("", "")
            has_content = bool(name.strip() and content.strip())
            dirty = self._is_dirty() if hasattr(self, "name_entry") else False
            database_ready = self.db is not None

            if hasattr(self, "new_button"):
                self.new_button.configure(state="disabled" if blocked else "normal")
            if hasattr(self, "search_entry"):
                self.search_entry.configure(state="disabled" if blocked else "normal")
            if hasattr(self, "clear_search_button"):
                has_search = bool(self.search_var.get().strip())
                self.clear_search_button.configure(
                    state="disabled" if blocked or not has_search else "normal"
                )
            if hasattr(self, "name_entry"):
                self.name_entry.configure(state="disabled" if blocked else "normal")
            if hasattr(self, "transcript_text"):
                self.transcript_text.configure(state="disabled" if blocked else "normal")
            if hasattr(self, "save_button"):
                self.save_button.configure(
                    state="normal" if not blocked and database_ready and has_content and dirty else "disabled"
                )
            if hasattr(self, "delete_button"):
                self.delete_button.configure(
                    state="normal"
                    if not blocked and database_ready and self.selected_transcript is not None
                    else "disabled"
                )
            if hasattr(self, "generate_button"):
                self.generate_button.configure(
                    state="normal" if not blocked and database_ready and has_content else "disabled"
                )
            if hasattr(self, "question_count_selector"):
                question_count_blocked = (
                    blocked or self._startup_running or self._auth_running or not self._api_ready
                )
                self.question_count_selector.configure(
                    state="disabled" if question_count_blocked else "readonly"
                )
            if hasattr(self, "take_quiz_button"):
                self.take_quiz_button.configure(
                    state="normal"
                    if not blocked and self.selected_quiz is not None
                    else "disabled"
                )
            for button in (*self._transcript_buttons, *self._quiz_buttons, *self._attempt_buttons):
                if button.winfo_exists():
                    button.configure(state="disabled" if blocked else "normal")
            if hasattr(self, "auth_button"):
                auth_blocked = blocked or self._startup_running or self._auth_running
                self.auth_button.configure(state="disabled" if auth_blocked else "normal")
            if hasattr(self, "model_selector"):
                signed_in = bool(self._auth_status is not None and self._auth_status.signed_in)
                model_ready = bool(self._model_entries) and not self._model_loading
                model_blocked = (
                    blocked
                    or self._startup_running
                    or self._auth_running
                    or not self._api_ready
                    or not signed_in
                    or not model_ready
                )
                self.model_selector.configure(state="disabled" if model_blocked else "readonly")
        except BaseException as exc:
            self._report_ui_callback_error(exc)

    def _build_library(self) -> None:
        content = ctk.CTkFrame(self, fg_color="transparent")
        content.pack(fill="both", expand=True, padx=18, pady=16)
        content.grid_rowconfigure(0, weight=1)
        content.grid_columnconfigure(0, weight=24, minsize=280)
        content.grid_columnconfigure(1, weight=44, minsize=500)
        content.grid_columnconfigure(2, weight=32, minsize=370)

        self.left_panel = ctk.CTkFrame(
            content,
            fg_color=COLORS["panel"],
            border_width=1,
            border_color=COLORS["border"],
            corner_radius=14,
        )
        self.left_panel.grid(row=0, column=0, sticky="nsew", padx=(0, 7))
        self.middle_panel = ctk.CTkFrame(
            content,
            fg_color=COLORS["panel"],
            border_width=1,
            border_color=COLORS["border"],
            corner_radius=14,
        )
        self.middle_panel.grid(row=0, column=1, sticky="nsew", padx=7)
        self.right_panel = ctk.CTkFrame(
            content,
            fg_color=COLORS["panel"],
            border_width=1,
            border_color=COLORS["border"],
            corner_radius=14,
        )
        self.right_panel.grid(row=0, column=2, sticky="nsew", padx=(7, 0))

        self._build_transcript_list()
        self._build_editor()
        self._build_quiz_details()

    def _build_transcript_list(self) -> None:
        self.left_panel.grid_columnconfigure(0, weight=1)
        self.left_panel.grid_rowconfigure(3, weight=1)
        title_row = ctk.CTkFrame(self.left_panel, fg_color="transparent")
        title_row.grid(row=0, column=0, sticky="ew", padx=16, pady=(16, 10))
        ctk.CTkLabel(
            title_row,
            text="Library",
            font=_font(18, "bold", FONT_DISPLAY),
            text_color=COLORS["text"],
        ).pack(side="left")
        self.new_button = ctk.CTkButton(
            title_row,
            text="New transcript",
            width=118,
            height=34,
            corner_radius=8,
            font=_font(11, "bold"),
            fg_color=COLORS["accent"],
            hover_color=COLORS["accent_hover"],
            text_color=COLORS["on_accent"],
            text_color_disabled=COLORS["disabled_text"],
            command=self._new_transcript,
        )
        self.new_button.pack(side="right")

        self.search_var = ctk.StringVar()
        self.search_var.trace_add("write", self._schedule_search)
        search_row = ctk.CTkFrame(self.left_panel, fg_color="transparent")
        search_row.grid(row=1, column=0, sticky="ew", padx=16, pady=(0, 4))
        search_row.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(
            search_row,
            text="Search",
            font=_font(10, "bold"),
            text_color=COLORS["muted"],
        ).grid(row=0, column=0, sticky="w", padx=(0, 8))
        self.search_entry = ctk.CTkEntry(
            search_row,
            textvariable=self.search_var,
            height=38,
            fg_color=COLORS["panel_alt"],
            border_color=COLORS["border"],
            corner_radius=9,
            font=_font(12),
            text_color=COLORS["text"],
        )
        self.search_entry.grid(row=0, column=1, sticky="ew", padx=(0, 6))
        self.clear_search_button = ctk.CTkButton(
            search_row,
            text="Clear",
            width=58,
            height=38,
            corner_radius=9,
            font=_font(11, "bold"),
            fg_color=COLORS["panel_alt"],
            hover_color=COLORS["panel_hover"],
            border_width=1,
            border_color=COLORS["border"],
            text_color=COLORS["text"],
            text_color_disabled=COLORS["disabled_text"],
            state="disabled",
            command=self._clear_search,
        )
        self.clear_search_button.grid(row=0, column=2)
        self.search_count_label = ctk.CTkLabel(
            self.left_panel,
            text="0 transcripts",
            font=_font(10, "bold"),
            text_color=COLORS["muted"],
            anchor="w",
        )
        self.search_count_label.grid(row=2, column=0, sticky="w", padx=18, pady=(0, 5))
        self.transcript_list = ctk.CTkScrollableFrame(
            self.left_panel,
            fg_color="transparent",
            corner_radius=0,
        )
        self.transcript_list.grid(row=3, column=0, sticky="nsew", padx=8, pady=(0, 12))
        self.transcript_list.grid_columnconfigure(0, weight=1)

    def _build_editor(self) -> None:
        self.middle_panel.grid_columnconfigure(0, weight=1)
        self.middle_panel.grid_rowconfigure(3, weight=3)
        self.middle_panel.grid_rowconfigure(8, weight=2)

        ctk.CTkLabel(
            self.middle_panel,
            text="Transcript editor",
            font=_font(18, "bold", FONT_DISPLAY),
            text_color=COLORS["text"],
        ).grid(row=0, column=0, sticky="w", padx=18, pady=(18, 4))
        ctk.CTkLabel(
            self.middle_panel,
            text="Transcript name",
            font=_font(11, "bold"),
            text_color=COLORS["muted"],
        ).grid(row=1, column=0, sticky="w", padx=18, pady=(7, 4))
        self.name_entry = ctk.CTkEntry(
            self.middle_panel,
            placeholder_text="e.g. Week 4 lecture",
            height=38,
            fg_color=COLORS["panel_alt"],
            border_color=COLORS["border"],
            corner_radius=9,
            font=_font(12),
        )
        self.name_entry.grid(row=2, column=0, sticky="ew", padx=18)
        self.name_entry.bind("<KeyRelease>", lambda _event: self._update_editor_state())

        self.transcript_text = ctk.CTkTextbox(
            self.middle_panel,
            fg_color=COLORS["panel_alt"],
            border_width=1,
            border_color=COLORS["border"],
            corner_radius=11,
            wrap="word",
            font=_font(13),
        )
        self.transcript_text.grid(row=3, column=0, sticky="nsew", padx=18, pady=(10, 6))
        self.transcript_text.bind("<KeyRelease>", lambda _event: self._update_editor_state())
        self.editor_meta = ctk.CTkLabel(
            self.middle_panel,
            text="Paste a transcript to start",
            font=_font(11),
            text_color=COLORS["muted"],
        )
        self.editor_meta.grid(row=4, column=0, sticky="w", padx=18)

        action_row = ctk.CTkFrame(self.middle_panel, fg_color="transparent")
        action_row.grid(row=5, column=0, sticky="ew", padx=18, pady=(10, 5))
        action_row.grid_columnconfigure(2, weight=1)
        self.save_button = ctk.CTkButton(
            action_row,
            text="Save",
            width=70,
            height=36,
            corner_radius=9,
            font=_font(11, "bold"),
            fg_color=COLORS["panel_alt"],
            hover_color=COLORS["panel_hover"],
            border_width=1,
            border_color=COLORS["border"],
            text_color=COLORS["text"],
            text_color_disabled=COLORS["disabled_text"],
            command=self._save_current,
        )
        self.save_button.grid(row=0, column=0, padx=(0, 7))
        self.delete_button = ctk.CTkButton(
            action_row,
            text="Delete",
            width=70,
            height=36,
            corner_radius=9,
            font=_font(11, "bold"),
            fg_color=COLORS["red_dark"],
            hover_color=COLORS["red"],
            text_color=COLORS["danger_text"],
            text_color_disabled=COLORS["disabled_text"],
            command=self._delete_current,
        )
        self.delete_button.grid(row=0, column=1)
        question_count_group = ctk.CTkFrame(action_row, fg_color="transparent")
        question_count_group.grid(row=0, column=2, sticky="w", padx=(10, 0))
        ctk.CTkLabel(
            question_count_group,
            text="Questions",
            font=_font(10, "bold"),
            text_color=COLORS["muted"],
        ).pack(side="left", padx=(0, 6))
        self.question_count_selector = ctk.CTkComboBox(
            question_count_group,
            width=70,
            height=34,
            values=[str(value) for value in QUESTION_COUNT_CHOICES],
            state="disabled",
            fg_color=COLORS["panel_alt"],
            border_color=COLORS["border"],
            button_color=COLORS["accent"],
            button_hover_color=COLORS["accent_hover"],
            text_color=COLORS["text"],
            corner_radius=8,
            font=_font(11),
            command=self._question_count_changed,
        )
        self.question_count_selector.pack(side="left")
        self.question_count_selector.configure(state="readonly")
        self.question_count_selector.set(str(DEFAULT_QUESTION_COUNT))
        self.question_count_selector.configure(state="disabled")
        self.generate_button = ctk.CTkButton(
            action_row,
            text="Generate quiz",
            width=112,
            height=36,
            corner_radius=9,
            font=_font(11, "bold"),
            fg_color=COLORS["accent"],
            hover_color=COLORS["accent_hover"],
            text_color=COLORS["on_accent"],
            text_color_disabled=COLORS["disabled_text"],
            command=self._generate_quiz,
        )
        self.generate_button.grid(row=0, column=3, padx=(7, 0))
        self.cancel_generate_button = ctk.CTkButton(
            action_row,
            text="Cancel",
            width=68,
            height=36,
            corner_radius=9,
            font=_font(11, "bold"),
            fg_color=COLORS["red_dark"],
            hover_color=COLORS["red"],
            text_color=COLORS["danger_text"],
            text_color_disabled=COLORS["disabled_text"],
            command=self._cancel_generation,
        )
        self.cancel_generate_button.grid(row=0, column=4, padx=(7, 0))
        self.cancel_generate_button.grid_remove()

        self.progress_frame = ctk.CTkFrame(
            self.middle_panel,
            fg_color=COLORS["panel_alt"],
            border_width=1,
            border_color=COLORS["border"],
            corner_radius=8,
        )
        self.progress_frame.grid(row=6, column=0, sticky="ew", padx=18, pady=(4, 2))
        self.progress_frame.grid_columnconfigure(0, weight=1)
        self.progress_label = ctk.CTkLabel(
            self.progress_frame,
            text="",
            font=_font(11),
            text_color=COLORS["muted"],
        )
        self.progress_label.grid(row=0, column=0, sticky="w", padx=10, pady=(7, 0))
        self.progress_bar = ctk.CTkProgressBar(
            self.progress_frame,
            mode="indeterminate",
            height=5,
            progress_color=COLORS["accent"],
        )
        self.progress_bar.grid(row=1, column=0, sticky="ew", padx=10, pady=(5, 8))
        self.progress_frame.grid_remove()

        ctk.CTkLabel(
            self.middle_panel,
            text="Generated quizzes",
            font=_font(14, "bold", FONT_DISPLAY),
            text_color=COLORS["text"],
        ).grid(row=7, column=0, sticky="w", padx=18, pady=(10, 4))
        self.quiz_list = ctk.CTkScrollableFrame(
            self.middle_panel,
            fg_color="transparent",
            corner_radius=0,
        )
        self.quiz_list.grid(row=8, column=0, sticky="nsew", padx=10, pady=(0, 12))
        self.quiz_list.grid_columnconfigure(0, weight=1)

    def _build_quiz_details(self) -> None:
        self.right_panel.grid_columnconfigure(0, weight=1)
        self.right_panel.grid_rowconfigure(7, weight=1)
        ctk.CTkLabel(
            self.right_panel,
            text="Quiz details",
            font=_font(18, "bold", FONT_DISPLAY),
            text_color=COLORS["text"],
        ).grid(row=0, column=0, sticky="w", padx=18, pady=(18, 4))
        self.quiz_detail_title = ctk.CTkLabel(
            self.right_panel,
            text="Select a quiz",
            font=_font(16, "bold", FONT_DISPLAY),
            text_color=COLORS["text"],
            justify="left",
            anchor="w",
            wraplength=320,
        )
        self.quiz_detail_title.grid(row=1, column=0, sticky="ew", padx=18, pady=(14, 3))
        self.quiz_detail_meta = ctk.CTkLabel(
            self.right_panel,
            text="Choose a generated quiz to take it or review past attempts.",
            font=_font(12),
            text_color=COLORS["muted"],
            justify="left",
            anchor="w",
            wraplength=320,
        )
        self.quiz_detail_meta.grid(row=2, column=0, sticky="ew", padx=18, pady=(0, 12))
        self.take_quiz_button = ctk.CTkButton(
            self.right_panel,
            text="Take quiz",
            height=42,
            corner_radius=9,
            font=_font(12, "bold"),
            fg_color=COLORS["accent"],
            hover_color=COLORS["accent_hover"],
            text_color=COLORS["on_accent"],
            text_color_disabled=COLORS["disabled_text"],
            state="disabled",
            command=self._start_quiz,
        )
        self.take_quiz_button.grid(row=3, column=0, sticky="ew", padx=18)
        ctk.CTkFrame(self.right_panel, height=1, fg_color=COLORS["border"]).grid(
            row=4, column=0, sticky="ew", padx=18, pady=18
        )
        ctk.CTkLabel(
            self.right_panel,
            text="Attempts",
            font=_font(14, "bold", FONT_DISPLAY),
            text_color=COLORS["text"],
        ).grid(row=5, column=0, sticky="w", padx=18)
        self.attempt_summary = ctk.CTkLabel(
            self.right_panel,
            text="No quiz selected",
            font=_font(11),
            text_color=COLORS["muted"],
        )
        self.attempt_summary.grid(row=6, column=0, sticky="w", padx=18, pady=(2, 6))
        self.attempt_list = ctk.CTkScrollableFrame(
            self.right_panel,
            fg_color="transparent",
            corner_radius=0,
        )
        self.attempt_list.grid(row=7, column=0, sticky="nsew", padx=10, pady=(0, 12))
        self.attempt_list.grid_columnconfigure(0, weight=1)

    def _bind_shortcuts(self) -> None:
        self.bind("<Control-n>", self._shortcut_new)
        self.bind("<Control-s>", self._shortcut_save)
        self.bind("<Control-Return>", self._shortcut_generate)

    def _validated_question_count(self, raw_value: Any, *, report: bool = True) -> int | None:
        try:
            if isinstance(raw_value, bool):
                raise ValueError
            value = int(str(raw_value).strip())
            if not QUESTION_COUNT_MIN <= value <= QUESTION_COUNT_MAX:
                raise ValueError
        except (TypeError, ValueError, OverflowError):
            if report:
                self._set_status(
                    f"Question count must be a whole number from {QUESTION_COUNT_MIN} through "
                    f"{QUESTION_COUNT_MAX}.",
                    "warning",
                )
            return None
        return value

    def _get_question_count(self) -> int | None:
        try:
            raw_value = self.question_count_selector.get()
        except BaseException as exc:
            self._set_status(f"Could not read question count: {_safe_exception_message(exc)}", "error")
            return None
        return self._validated_question_count(raw_value)

    def _question_count_changed(self, value: str) -> None:
        if self._closing or self._destroyed:
            return
        count = self._validated_question_count(value)
        if count is not None and not self._startup_running and not self._auth_running:
            self._set_status(f"Question count set to {count}.", "info")
        self._update_control_states()

    def _shortcut_new(self, _event: Any) -> str:
        self._new_transcript()
        return "break"

    def _shortcut_save(self, _event: Any) -> str:
        self._save_current()
        return "break"

    def _shortcut_generate(self, _event: Any) -> str:
        self._generate_quiz()
        return "break"

    def _run_async(
        self,
        work: Callable[[int], Any],
        on_success: Callable[[Any], None],
        on_error: Callable[[BaseException], None] | None = None,
    ) -> int:
        if self._closing or self._destroyed:
            return -1
        token = self._next_async_token
        self._next_async_token += 1
        self._async_handlers[token] = (on_success, on_error or self._show_async_error)

        def runner() -> None:
            try:
                result = work(token)
            except BaseException as exc:
                self._ui_queue.put(("async_error", token, exc))
            else:
                self._ui_queue.put(("async_success", token, result))

        threading.Thread(target=runner, name=f"quiz-app-worker-{token}", daemon=True).start()
        return token

    def _drain_ui_queue(self) -> None:
        try:
            if getattr(self, "_destroyed", False):
                return
            while not getattr(self, "_destroyed", False):
                try:
                    event = self._ui_queue.get_nowait()
                except queue.Empty:
                    break
                try:
                    self._process_ui_event(event)
                except BaseException as exc:
                    # One bad callback must not prevent later events from being
                    # delivered or stop the queue pump from being scheduled.
                    try:
                        self._report_ui_callback_error(exc)
                    except BaseException:
                        pass
        except BaseException as exc:
            # Keep the UI pump alive even if queue inspection or error reporting
            # itself fails unexpectedly.
            try:
                self._report_ui_callback_error(exc)
            except BaseException:
                pass
        finally:
            if not getattr(self, "_destroyed", False):
                try:
                    self.after(50, self._drain_ui_queue)
                except BaseException:
                    pass

    def _process_ui_event(self, event: tuple[Any, ...]) -> None:
        if not event:
            return
        kind = event[0]
        if kind in {"async_success", "async_error"}:
            if len(event) != 3:
                raise ValueError("Malformed asynchronous UI event")
            _, token, value = event
            handlers = self._async_handlers.pop(token, None)
            if handlers is None or self._closing or self._destroyed:
                return
            callback = handlers[0] if kind == "async_success" else handlers[1]
            try:
                callback(value)
            except BaseException as exc:
                try:
                    self._report_ui_callback_error(exc)
                except BaseException:
                    pass
            return
        if kind == "shutdown_done":
            if self._closing:
                self._destroy_now()
            return
        if kind == "device_challenge":
            if len(event) != 2 or self._closing or self._destroyed:
                return
            try:
                self._show_device_challenge(event[1])
            except BaseException as exc:
                try:
                    self._report_ui_callback_error(exc, "Could not show sign-in dialog")
                except BaseException:
                    pass
            return
        if kind == "generation_progress":
            if len(event) != 3 or self._closing or self._destroyed:
                return
            _, token, message = event
            if token != self._generation_token:
                return
            try:
                if self.progress_frame.winfo_exists():
                    display_message = _ellipsize(message, 82)
                    self.progress_label.configure(text=display_message)
                    self._set_status(display_message, "info")
            except BaseException as exc:
                try:
                    self._report_ui_callback_error(exc, "Could not update generation progress")
                except BaseException:
                    pass

    def _start_backend(self) -> None:
        if self._startup_running or self._closing:
            if self._startup_running:
                self._set_status("Starting Codex…", "info")
            return
        self._startup_running = True
        self._auth_running = True
        self._set_model_selector_message("Loading available models…")
        try:
            self.auth_text.configure(text="Starting Codex…", text_color=COLORS["muted"])
            self.auth_button.configure(text="Please wait", state="disabled")
        except BaseException as exc:
            self._report_ui_callback_error(exc)
        self._set_status("Starting Codex…", "info")
        self._update_control_states()

        def work(_token: int) -> AuthStatus:
            self.api.start()
            return self.auth.check_status()

        def success(status: AuthStatus) -> None:
            self._startup_running = False
            self._auth_running = False
            self._api_ready = True
            self._api_error = None
            self._apply_auth_status(status)
            self._resume_pending_generation()

        def error(exc: BaseException) -> None:
            self._startup_running = False
            self._auth_running = False
            self._api_ready = False
            self._api_error = exc
            if isinstance(exc, CodexNotFoundError):
                label = "Codex CLI setup required"
                button_text = "Setup help"
            else:
                label = "OpenAI connection unavailable"
                button_text = "Retry"
            try:
                self.auth_text.configure(text=label, text_color=COLORS["red"] if not isinstance(exc, CodexNotFoundError) else COLORS["amber"])
                self.auth_button.configure(text=button_text, state="normal")
            except BaseException as callback_error:
                self._report_ui_callback_error(callback_error)
            detail = _safe_exception_message(exc)
            pending = " Generation request is queued; retry startup to continue." if self._pending_generation else ""
            self._set_status(f"Codex startup failed: {detail}.{pending}", "error")
            self._set_model_selector_message("Retry startup to load models")
            self._show_startup_error(exc)
            self._update_control_states()

        self._run_async(work, success, error)

    @staticmethod
    def _model_entry_id(entry: dict[str, Any]) -> str | None:
        for key in ("model", "id"):
            value = entry.get(key)
            if isinstance(value, str):
                model_id = value.strip()
                if model_id:
                    return model_id
        return None

    @classmethod
    def _account_default_model_id(cls, entries: list[dict[str, Any]]) -> str | None:
        for entry in entries:
            if bool(entry.get("isDefault", False)) or bool(entry.get("default", False)):
                model_id = cls._model_entry_id(entry)
                if model_id is not None:
                    return model_id
        return cls._model_entry_id(entries[0]) if entries else None

    @classmethod
    def _model_display_name(cls, entry: dict[str, Any], model_id: str) -> str:
        friendly: str | None = None
        for key in ("displayName", "name", "label", "title"):
            value = entry.get(key)
            if isinstance(value, str) and value.strip():
                friendly = " ".join(value.split())[:120]
                break
        if friendly is None:
            friendly = re.sub(r"[-_]+", " ", model_id).strip().title()[:120]
        if friendly.casefold() == model_id.casefold():
            return model_id
        return f"{friendly} — {model_id}"

    def _set_model_selector_message(self, message: str) -> None:
        self._model_entries = []
        self._model_display_to_id.clear()
        self._model_id_to_display.clear()
        self._model_default_id = None
        self._selected_model_id = None
        try:
            self.model_selector.configure(values=[message], state="readonly")
            self.model_selector.set(message)
            self.model_selector.configure(state="disabled")
        except BaseException:
            pass

    def _set_model_selector_value(self, value: str) -> None:
        """Set a CTkComboBox value even when its normal UI state is disabled."""

        try:
            previous_state = self.model_selector.cget("state")
            self.model_selector.configure(state="readonly")
            self.model_selector.set(value)
            self.model_selector.configure(
                state=previous_state if previous_state in {"normal", "readonly", "disabled"} else "disabled"
            )
        except BaseException as exc:
            self._report_ui_callback_error(exc, "Could not update the model selector")

    def _load_models(self) -> None:
        if self._closing or self._destroyed:
            return
        if not self._api_ready or self._auth_status is None or not self._auth_status.signed_in:
            self._set_model_selector_message("Sign in to load models")
            self._update_control_states()
            return

        self._model_load_generation += 1
        generation = self._model_load_generation
        self._model_loading = True
        self._set_model_selector_message("Loading available models…")
        self._update_control_states()

        def work(_token: int) -> list[dict[str, Any]]:
            return self.api.list_available_models()

        def success(entries: Any) -> None:
            if (
                generation != self._model_load_generation
                or self._closing
                or self._destroyed
                or self._auth_status is None
                or not self._auth_status.signed_in
            ):
                return
            self._model_loading = False
            self._model_load_token = None
            self._apply_model_entries(entries)

        def error(exc: BaseException) -> None:
            if generation != self._model_load_generation or self._closing or self._destroyed:
                return
            self._model_loading = False
            self._model_load_token = None
            self._set_model_selector_message("Models unavailable")
            self._set_status(
                f"Available models could not be loaded: {_safe_exception_message(exc)}",
                "error",
            )
            self._update_control_states()

        token = self._run_async(work, success, error)
        self._model_load_token = token if token >= 0 else None

    def _apply_model_entries(self, entries: Any) -> None:
        normalized: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        if isinstance(entries, list):
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                if bool(entry.get("hidden", False)) or bool(entry.get("isHidden", False)):
                    continue
                model_id = self._model_entry_id(entry)
                if model_id is None or model_id in seen_ids:
                    continue
                seen_ids.add(model_id)
                normalized.append(dict(entry))

        if not normalized:
            try:
                self.api.reset_to_application_default()
            except BaseException:
                pass
            self._set_model_selector_message("No available models")
            self._set_status("No visible models are available for this account.", "warning")
            self._update_control_states()
            return

        default_id = self._account_default_model_id(normalized)
        if default_id is None:
            self._set_model_selector_message("No available models")
            self._set_status("No usable models are available for this account.", "warning")
            self._update_control_states()
            return

        preferred = self.api.get_preferred_model()
        application_default = _DEFAULT_PREFERRED_MODEL if preferred is None else preferred
        fallback_message: str | None = None
        if application_default in seen_ids:
            selected_id = application_default
        else:
            selected_id = default_id
            if preferred is not None:
                fallback_message = (
                    f"Selected model {preferred} is no longer available; "
                    f"using account default {default_id}."
                )
                try:
                    self.api.reset_to_application_default()
                except BaseException as exc:
                    self._report_ui_callback_error(exc, "Could not reset the unavailable model")

        display_to_id: dict[str, str] = {}
        id_to_display: dict[str, str] = {}
        for entry in normalized:
            model_id = self._model_entry_id(entry)
            if model_id is None:
                continue
            display = self._model_display_name(entry, model_id)
            if model_id == default_id:
                display = f"{display} · account default"
            display_to_id[display] = model_id
            id_to_display[model_id] = display

        self._model_entries = normalized
        self._model_display_to_id = display_to_id
        self._model_id_to_display = id_to_display
        self._model_default_id = default_id
        self._selected_model_id = selected_id
        values = list(display_to_id)
        try:
            self.model_selector.configure(values=values)
            self._set_model_selector_value(id_to_display[selected_id])
        except BaseException as exc:
            self._report_ui_callback_error(exc, "Could not update the model selector")
        self._update_control_states()
        if fallback_message:
            self._set_status(fallback_message, "warning")
        elif (
            not self._generation_busy
            and self._pending_generation is None
            and self._status_kind not in {"success", "warning", "error"}
        ):
            self._set_status(
                f"Available models loaded — using {selected_id}.",
                "info",
            )

    def _model_selection_changed(self, value: str) -> None:
        if self._closing or self._destroyed:
            return
        if (
            self._startup_running
            or self._auth_running
            or self._generation_busy
            or self._model_loading
            or self._auth_status is None
            or not self._auth_status.signed_in
        ):
            return
        model_id = self._model_display_to_id.get(value)
        if model_id is None:
            if self._selected_model_id is not None:
                try:
                    self._set_model_selector_value(self._model_id_to_display[self._selected_model_id])
                except BaseException:
                    pass
            self._set_status("Choose one of the live available models.", "warning")
            return
        try:
            self.api.set_preferred_model(model_id)
        except BaseException as exc:
            self._set_status(f"Could not select model: {_safe_exception_message(exc)}", "error")
            if self._selected_model_id is not None:
                try:
                    self._set_model_selector_value(self._model_id_to_display[self._selected_model_id])
                except BaseException:
                    pass
            return
        self._selected_model_id = model_id
        self._set_status(f"Model selected: {model_id}.", "info")
        self._update_control_states()

    def _sync_model_after_generation(self, model: Any) -> str | None:
        if not isinstance(model, str) or not model.strip():
            return None
        model_id = model.strip()
        previous = self._selected_model_id
        if model_id == previous:
            return None

        display = self._model_id_to_display.get(model_id)
        if display is not None:
            self._selected_model_id = model_id
            try:
                # A different model returned by generation means the live
                # catalog selected a fallback.  Keep the API on its account
                # default rather than turning that fallback into a stale
                # preference based on an older selector snapshot.
                self.api.reset_to_application_default()
                self._set_model_selector_value(display)
            except BaseException as exc:
                self._report_ui_callback_error(exc, "Could not update the selected model")
            if previous is not None:
                if self._api_ready and self._auth_status is not None and self._auth_status.signed_in:
                    self._load_models()
                return (
                    f"Selected model {previous} is no longer available; "
                    f"using account default {model_id}."
                )
            return None

        try:
            self.api.reset_to_application_default()
        except BaseException:
            pass
        self._selected_model_id = model_id
        self._set_model_selector_message(f"Account default: {model_id}")
        if self._api_ready and self._auth_status is not None and self._auth_status.signed_in:
            self._load_models()
        if previous is not None:
            return (
                f"Selected model {previous} is no longer available; "
                f"using account default {model_id}."
            )
        return None

    def _startup_state_check(self) -> None:
        self._startup_check_after = None
        if self._closing or self._destroyed:
            return
        self._update_control_states()
        if not self._api_ready and not self._startup_running and self._api_error is None:
            self._start_backend()

    def _apply_auth_status(self, status: AuthStatus, *, refresh_models: bool = True) -> None:
        self._auth_status = status
        if status.signed_in:
            identity = status.email or "OpenAI account"
            plan = f" · {status.plan_type}" if status.plan_type else ""
            try:
                self.auth_text.configure(text=f"{identity}{plan}", text_color=COLORS["success_text"])
                self.auth_button.configure(
                    text="Sign out",
                    state="normal",
                    fg_color=COLORS["panel_alt"],
                    hover_color=COLORS["panel_hover"],
                    border_width=1,
                    border_color=COLORS["border"],
                    text_color=COLORS["text"],
                )
            except BaseException as exc:
                self._report_ui_callback_error(exc)
            self._set_status(f"Signed in as {identity} — ready to generate.", "success")
            if refresh_models:
                self._load_models()
        else:
            self._model_load_generation += 1
            self._model_loading = False
            self._model_load_token = None
            try:
                self.api.reset_to_application_default()
            except BaseException as exc:
                self._report_ui_callback_error(exc, "Could not clear the account model selection")
            self._set_model_selector_message("Sign in to load models")
            try:
                self.auth_text.configure(text="Signed out", text_color=COLORS["muted"])
                self.auth_button.configure(
                    text="Sign in",
                    state="normal",
                    fg_color=COLORS["accent"],
                    hover_color=COLORS["accent_hover"],
                    border_width=0,
                    text_color=COLORS["on_accent"],
                )
            except BaseException as exc:
                self._report_ui_callback_error(exc)
            self._set_status("Sign in required before generating a quiz.", "warning")
        self._update_control_states()

    def _auth_action(self) -> None:
        if self._startup_running:
            self._set_status("Starting Codex…", "info")
            return
        if not self._api_ready:
            if isinstance(self._api_error, CodexNotFoundError):
                try:
                    messagebox.showinfo(
                        "Install Codex CLI",
                        "This no-API-key app uses OpenAI's Codex app-server for OAuth.\n\n"
                        "Install it with:\n  npm install -g @openai/codex@0.144.5\n\n"
                        "Then restart this app, or click Retry after installation.\n\n"
                        f"Startup detail: {_safe_exception_message(self._api_error)}",
                        parent=self,
                    )
                except BaseException as exc:
                    self._report_ui_callback_error(exc)
                try:
                    self.auth_button.configure(text="Retry")
                except BaseException:
                    pass
            self._start_backend()
            return
        if self._auth_status is not None and self._auth_status.signed_in:
            self._sign_out()
        else:
            self._sign_in()

    def _sign_in(self) -> None:
        if self._login_cancel is not None or self._closing:
            if self._login_cancel is not None:
                self._set_status("Sign-in is already in progress…", "info")
            return
        cancel_event = threading.Event()
        self._login_cancel = cancel_event
        self._auth_running = True
        try:
            self.auth_text.configure(text="Starting browser sign-in…", text_color=COLORS["muted"])
            self.auth_button.configure(text="Signing in…", state="disabled")
        except BaseException as exc:
            self._report_ui_callback_error(exc)
        self._set_status("Sign in required — opening browser sign-in…", "warning")
        self._update_control_states()

        def work(_token: int) -> AuthStatus:
            return self.auth.sign_in(
                lambda challenge: self._ui_queue.put(("device_challenge", challenge)),
                cancel_event=cancel_event,
            )

        def finish_dialog() -> None:
            try:
                if self._device_dialog is not None and self._device_dialog.winfo_exists():
                    self._device_dialog.complete()
            except BaseException as exc:
                self._report_ui_callback_error(exc, "Could not close sign-in dialog")
            self._device_dialog = None
            self._login_cancel = None
            self._auth_running = False

        def success(status: AuthStatus) -> None:
            finish_dialog()
            self.api.reset_to_application_default()
            self._apply_auth_status(status)
            self._resume_pending_generation()

        def error(exc: BaseException) -> None:
            cancelled = isinstance(exc, GenerationCancelledError)
            finish_dialog()
            self._apply_auth_status(AuthStatus(False, None, None, True))
            if cancelled:
                self._set_status(
                    "Sign-in cancelled. The generation request remains queued; sign in to continue."
                    if self._pending_generation
                    else "Sign-in cancelled.",
                    "warning",
                )
            else:
                self._show_async_error(exc, title="Sign-in failed")
                if self._pending_generation:
                    self._set_status(
                        f"Sign-in failed: {_safe_exception_message(exc)}. Generation remains queued.",
                        "error",
                    )
            self._update_control_states()

        self._run_async(work, success, error)

    def _show_device_challenge(self, challenge: DeviceChallenge) -> None:
        if self._closing or self._login_cancel is None:
            return
        try:
            if self._device_dialog is not None and self._device_dialog.winfo_exists():
                self._device_dialog.complete()
            self._device_dialog = DeviceLoginDialog(self, challenge, self._cancel_sign_in)
            self._set_status("Sign in required — complete the device login or cancel it.", "warning")
        except BaseException as exc:
            self._show_async_error(exc, title="Could not show sign-in dialog")

    def _cancel_sign_in(self) -> None:
        if self._login_cancel is not None:
            self._login_cancel.set()
            try:
                self.auth_text.configure(text="Cancelling sign-in…", text_color=COLORS["muted"])
            except BaseException as exc:
                self._report_ui_callback_error(exc)
            self._set_status("Cancelling sign-in…", "warning")

    def _sign_out(self) -> None:
        if not messagebox.askyesno(
            "Sign out?",
            "This signs the Codex CLI out of the current OpenAI account on this computer.",
            parent=self,
        ):
            return
        self._auth_running = True
        self.auth_button.configure(text="Signing out…", state="disabled")
        self._set_status("Signing out…", "info")
        self._update_control_states()

        def success(_result: Any) -> None:
            self._auth_running = False
            self._apply_auth_status(AuthStatus(False, None, None, True))

        def error(exc: BaseException) -> None:
            self._auth_running = False
            if self._auth_status is not None:
                self._apply_auth_status(self._auth_status)
            self._show_async_error(exc, title="Sign-out failed")
            self._update_control_states()

        self._run_async(lambda _token: self.auth.sign_out(), success, error)

    def _schedule_search(self, *_args: Any) -> None:
        if self._closing or self._destroyed:
            return
        if self._search_after is not None:
            try:
                self.after_cancel(self._search_after)
            except BaseException:
                pass
        self._search_after = self.after(180, self._refresh_transcripts)
        self._set_status("Searching…", "info")
        self._update_control_states()

    def _clear_search(self) -> None:
        if self._closing or self._controls_blocked():
            self._set_status("Search is unavailable while generation is running.", "warning")
            return
        if self.search_var.get():
            self.search_var.set("")
        else:
            self._refresh_transcripts()

    def _refresh_transcripts(self) -> None:
        self._search_after = None
        if self._closing or self._destroyed:
            return
        database = self._get_database("load library")
        if database is None:
            try:
                self.search_count_label.configure(text="Database unavailable")
                _clear_children(self.transcript_list)
            except BaseException:
                pass
            return
        try:
            search = self.search_var.get() if hasattr(self, "search_var") else ""
            records = database.list_transcripts(search)
        except BaseException as exc:
            self._show_async_error(exc, title="Could not load library")
            try:
                self.search_count_label.configure(text="Search failed")
            except BaseException:
                pass
            return
        try:
            _clear_children(self.transcript_list)
            self._transcript_buttons.clear()
            search = self.search_var.get().strip()
            result_word = "transcript" if len(records) == 1 else "transcripts"
            self.search_count_label.configure(text=f"{len(records)} {result_word}")
            if not records:
                ctk.CTkLabel(
                    self.transcript_list,
                    text=(
                        "No transcripts saved yet.\nCreate one to get started."
                        if not search
                        else "No transcripts match this search."
                    ),
                    text_color=COLORS["muted"],
                    font=_font(12),
                    justify="left",
                    anchor="w",
                    wraplength=240,
                ).grid(row=0, column=0, sticky="w", padx=10, pady=18)
                self._set_status(
                    "Search complete — no matching transcripts." if search else "Library loaded — no saved transcripts.",
                    "info",
                )
                self._update_control_states()
                return

            row = 0
            current_group: str | None = None
            selected_id = self.selected_transcript.id if self.selected_transcript else None
            for record in records:
                group = _date_group(record.created_at)
                if group != current_group:
                    current_group = group
                    ctk.CTkLabel(
                        self.transcript_list,
                        text=group.upper(),
                        font=_font(10, "bold", FONT_MONO),
                        text_color=COLORS["subtle"],
                    ).grid(row=row, column=0, sticky="w", padx=10, pady=(12, 4))
                    row += 1
                display_name = _ellipsize(record.name, 32)
                preview = _ellipsize(record.content, 40)
                text = display_name if not preview else f"{display_name}\n{preview}"
                selected = record.id == selected_id
                button = ctk.CTkButton(
                    self.transcript_list,
                    text=text,
                    height=64,
                    anchor="w",
                    font=_font(12, "bold" if selected else "normal"),
                    fg_color=COLORS["accent_soft"] if selected else COLORS["panel_alt"],
                    hover_color=COLORS["panel_hover"],
                    border_width=1,
                    border_color=COLORS["focus"] if selected else COLORS["border"],
                    corner_radius=9,
                    text_color=COLORS["text"],
                    text_color_disabled=COLORS["disabled_text"],
                    state="disabled" if self._controls_blocked() else "normal",
                    command=lambda value=record.id: self._select_transcript(value),
                )
                button.grid(row=row, column=0, sticky="ew", padx=4, pady=2)
                self._transcript_buttons.append(button)
                row += 1
            self._set_status(
                f"Search complete — {len(records)} {result_word}." if search else f"Library loaded — {len(records)} {result_word}.",
                "info",
            )
            self._update_control_states()
        except BaseException as exc:
            self._show_async_error(exc, title="Could not refresh library")

    def _editor_values(self) -> tuple[str, str]:
        return self.name_entry.get(), self.transcript_text.get("1.0", "end-1c")

    def _is_dirty(self) -> bool:
        return self._editor_values() != self._editor_snapshot

    def _update_editor_state(self) -> None:
        try:
            name, content = self._editor_values()
            count = len(content)
            dirty = (name, content) != self._editor_snapshot
            suffix = " · Unsaved changes" if dirty else ""
            self.editor_meta.configure(text=f"{count:,} characters{suffix}" if count else f"Paste a transcript to start{suffix}")
            self._update_control_states()
        except BaseException as exc:
            self._report_ui_callback_error(exc)

    def _confirm_leave_editor(self) -> bool:
        if not self._is_dirty():
            return True
        answer = messagebox.askyesnocancel(
            "Unsaved changes",
            "Save your transcript changes before continuing?",
            parent=self,
        )
        if answer is None:
            return False
        if answer:
            return self._save_current() is not None
        return True

    def _new_transcript(self, confirm: bool = True) -> None:
        if self._closing:
            self._set_status("The application is closing.", "warning")
            return
        if self._generation_cancel is not None or self._generation_busy:
            self._set_status("Generation in progress — New is disabled until it finishes.", "warning")
            return
        if confirm and not self._confirm_leave_editor():
            return
        try:
            self._pending_generation = None
            self._pending_question_count = None
            self.selected_transcript = None
            self.selected_quiz = None
            self.name_entry.delete(0, "end")
            self.transcript_text.delete("1.0", "end")
            self._editor_snapshot = ("", "")
            self._update_editor_state()
            self.delete_button.configure(state="disabled")
            _clear_children(self.quiz_list)
            self._quiz_buttons.clear()
            ctk.CTkLabel(
                self.quiz_list,
                text="Save this transcript before generating a quiz.",
                text_color=COLORS["muted"],
                font=_font(11),
            ).grid(row=0, column=0, sticky="w", padx=10, pady=12)
            self._clear_quiz_details()
            search_was_active = bool(self.search_var.get().strip())
            if search_was_active:
                self.search_var.set("")
            self._refresh_transcripts()
            self._set_status(
                "New transcript ready — search cleared." if search_was_active else "New transcript ready.",
                "info",
            )
            self.name_entry.focus_set()
        except BaseException as exc:
            self._show_async_error(exc, title="Could not create a new transcript")

    def _select_transcript(self, transcript_id: int) -> None:
        if self._closing:
            self._set_status("The application is closing.", "warning")
            return
        if self._generation_cancel is not None or self._generation_busy:
            self._set_status("Generation in progress — transcript selection is disabled.", "warning")
            return
        if self.selected_transcript and self.selected_transcript.id == transcript_id:
            return
        if not self._confirm_leave_editor():
            return
        database = self._get_database("load transcript")
        if database is None:
            return
        try:
            record = database.get_transcript(transcript_id)
        except BaseException as exc:
            self._show_async_error(exc, title="Could not load transcript")
            return
        if record is None:
            self._set_status("That transcript is no longer available.", "warning")
            self._refresh_transcripts()
            return
        try:
            self.selected_transcript = record
            self.selected_quiz = None
            self.name_entry.delete(0, "end")
            self.name_entry.insert(0, record.name)
            self.transcript_text.delete("1.0", "end")
            self.transcript_text.insert("1.0", record.content)
            self._editor_snapshot = (record.name, record.content)
            self._update_editor_state()
            self.delete_button.configure(state="normal")
            self._refresh_transcripts()
            self._refresh_quizzes()
            self._set_status(f"Loaded transcript: {record.name}", "info")
        except BaseException as exc:
            self._show_async_error(exc, title="Could not select transcript")

    def _save_current(self) -> TranscriptRecord | None:
        if self._closing:
            self._set_status("The application is closing; save is unavailable.", "warning")
            return None
        if self._generation_cancel is not None or self._generation_busy:
            self._set_status("Generation in progress — save is disabled until it finishes.", "warning")
            return None
        name, content = self._editor_values()
        if not name.strip() or not content.strip():
            self._set_status("Cannot save: add both a name and transcript content.", "warning")
            self._update_editor_state()
            return None
        if self.selected_transcript is not None and not self._is_dirty():
            self._set_status("Saved — there are no changes to save.", "success")
            return self.selected_transcript
        database = self._get_database("save transcript")
        if database is None:
            return None
        self._set_status("Saving transcript…", "info")
        try:
            self.update_idletasks()
        except BaseException:
            pass
        try:
            if self.selected_transcript is None:
                record = database.save_transcript(name.strip(), content)
            else:
                record = database.update_transcript(self.selected_transcript.id, name.strip(), content)
        except BaseException as exc:
            self._set_status(f"Save failed: {_safe_exception_message(exc)}", "error")
            self._show_async_error(exc, title="Could not save transcript")
            return None
        try:
            self.selected_transcript = record
            self._editor_snapshot = (record.name, record.content)
            self._update_editor_state()
            self.delete_button.configure(state="normal")
            search_was_active = bool(self.search_var.get().strip())
            if search_was_active:
                self.search_var.set("")
            self._refresh_transcripts()
            self._refresh_quizzes()
            self._set_status(
                "Saved transcript — search cleared so it remains visible."
                if search_was_active
                else "Saved transcript.",
                "success",
            )
            return record
        except BaseException as exc:
            self._show_async_error(exc, title="Could not refresh after saving")
            return record

    def _delete_current(self) -> None:
        record = self.selected_transcript
        if self._closing:
            self._set_status("The application is closing.", "warning")
            return
        if record is None:
            self._set_status("Select a saved transcript before deleting it.", "warning")
            return
        if self._generation_cancel is not None or self._generation_busy:
            self._set_status("Generation in progress — delete is disabled until it finishes.", "warning")
            return
        try:
            confirmed = messagebox.askyesno(
                "Delete transcript?",
                f'“{record.name}” and all of its quizzes and attempts will be deleted.',
                icon="warning",
                parent=self,
            )
        except BaseException as exc:
            self._report_ui_callback_error(exc)
            return
        if not confirmed:
            return
        database = self._get_database("delete transcript")
        if database is None:
            return
        self._set_status("Deleting transcript…", "info")
        try:
            deleted = database.delete_transcript(record.id)
        except BaseException as exc:
            self._set_status(f"Delete failed: {_safe_exception_message(exc)}", "error")
            self._show_async_error(exc, title="Could not delete transcript")
            return
        if not deleted:
            self._new_transcript(confirm=False)
            self._set_status(
                "Transcript was already deleted; its quiz list may be stale.",
                "warning",
            )
            return
        self._new_transcript(confirm=False)
        self._set_status(
            "Transcript deleted — its quizzes and attempts were also deleted.",
            "success",
        )

    def _refresh_quizzes(
        self,
        select_id: int | None = None,
    ) -> None:
        if self._closing or self._destroyed:
            return
        record = self.selected_transcript
        try:
            _clear_children(self.quiz_list)
            self._quiz_buttons.clear()
        except BaseException as exc:
            self._show_async_error(exc, title="Could not refresh quizzes")
            return
        if record is None:
            return
        database = self._get_database("load quizzes")
        if database is None:
            return
        try:
            quizzes = database.list_quizzes(record.id)
        except BaseException as exc:
            self._show_async_error(exc, title="Could not load quizzes")
            return
        try:
            if not quizzes:
                ctk.CTkLabel(
                    self.quiz_list,
                    text="No quizzes yet.\nGenerate one from this transcript.",
                    text_color=COLORS["muted"],
                    font=_font(11),
                    justify="left",
                ).grid(row=0, column=0, sticky="w", padx=10, pady=16)
                self._clear_quiz_details()
                return

            available_ids = {quiz.id for quiz in quizzes}
            existing_selected = select_id
            if existing_selected is None and self.selected_quiz is not None:
                existing_selected = self.selected_quiz.id
            if existing_selected is None:
                existing_selected = quizzes[0].id
            elif existing_selected not in available_ids:
                self._clear_quiz_details()
                self._set_status("The selected quiz is no longer available.", "warning")
                return

            for row, quiz in enumerate(quizzes):
                item_frame = ctk.CTkFrame(
                    self.quiz_list,
                    fg_color=COLORS["panel_alt"],
                    border_width=1,
                    border_color=COLORS["border"],
                    corner_radius=10,
                )
                item_frame.grid(row=row, column=0, sticky="ew", padx=2, pady=3)
                item_frame.grid_columnconfigure(0, weight=1)
                selected = quiz.id == existing_selected
                display_name = _ellipsize(quiz.name, 42)
                button = ctk.CTkButton(
                    item_frame,
                    text=f"{display_name}\n{len(quiz.questions)} questions · {_format_datetime(quiz.created_at)}",
                    height=60,
                    anchor="w",
                    font=_font(11, "bold" if selected else "normal"),
                    fg_color=COLORS["accent_soft"] if selected else "transparent",
                    hover_color=COLORS["panel_hover"],
                    border_width=1,
                    border_color=COLORS["focus"] if selected else COLORS["border"],
                    corner_radius=8,
                    text_color=COLORS["text"],
                    text_color_disabled=COLORS["disabled_text"],
                    state="disabled" if self._controls_blocked() else "normal",
                    command=lambda value=quiz.id: self._select_quiz(value),
                )
                button.grid(row=0, column=0, sticky="ew")
                self._quiz_buttons.append(button)
                rename_button = ctk.CTkButton(
                    item_frame,
                    text="Rename",
                    width=62,
                    height=28,
                    corner_radius=8,
                    font=_font(10, "bold"),
                    fg_color=COLORS["panel_hover"],
                    hover_color=COLORS["panel_hover"],
                    border_width=1,
                    border_color=COLORS["border_strong"],
                    text_color=COLORS["text"],
                    text_color_disabled=COLORS["disabled_text"],
                    state="disabled" if self._controls_blocked() else "normal",
                    command=lambda value=quiz.id: self._rename_quiz(value),
                )
                rename_button.grid(row=0, column=1, padx=(7, 4))
                self._quiz_buttons.append(rename_button)
                delete_button = ctk.CTkButton(
                    item_frame,
                    text="Delete",
                    width=58,
                    height=28,
                    corner_radius=8,
                    font=_font(10, "bold"),
                    fg_color=COLORS["red_dark"],
                    hover_color=COLORS["red"],
                    text_color=COLORS["danger_text"],
                    text_color_disabled=COLORS["disabled_text"],
                    state="disabled" if self._controls_blocked() else "normal",
                    command=lambda value=quiz.id: self._delete_quiz(value),
                )
                delete_button.grid(row=0, column=2, padx=(0, 4))
                self._quiz_buttons.append(delete_button)
            self._select_quiz(existing_selected, rerender=False)
        except BaseException as exc:
            self._show_async_error(exc, title="Could not refresh quizzes")

    def _rename_quiz(self, quiz_id: int) -> None:
        if self._closing:
            self._set_status("The application is closing.", "warning")
            return
        if self._generation_cancel is not None or self._generation_busy:
            self._set_status("Generation in progress — quiz management is disabled.", "warning")
            return
        database = self._get_database("rename quiz")
        if database is None:
            return
        try:
            quiz = database.get_quiz(quiz_id)
        except BaseException as exc:
            self._show_async_error(exc, title="Could not load quiz for renaming")
            return
        if quiz is None:
            self._refresh_quizzes()
            self._set_status("That quiz is no longer available; refreshed the quiz list.", "warning")
            return
        try:
            name = simpledialog.askstring(
                "Rename quiz",
                "Quiz name:",
                initialvalue=quiz.name,
                parent=self,
            )
        except BaseException as exc:
            self._report_ui_callback_error(exc, "Could not open rename dialog")
            return
        if name is None:
            return
        name = name.strip()
        if not name:
            self._set_status("Quiz name cannot be empty.", "warning")
            return
        try:
            updated = database.update_quiz_name(quiz_id, name)
        except KeyError:
            self._refresh_quizzes()
            self._set_status("That quiz was deleted before it could be renamed.", "warning")
            return
        except BaseException as exc:
            self._set_status(f"Rename failed: {_safe_exception_message(exc)}", "error")
            self._show_async_error(exc, title="Could not rename quiz")
            return
        self.selected_quiz = updated if self.selected_quiz and self.selected_quiz.id == quiz_id else self.selected_quiz
        self._refresh_quizzes(select_id=updated.id)
        self._set_status(f"Renamed quiz to {updated.name}.", "success")

    def _delete_quiz(self, quiz_id: int) -> None:
        if self._closing:
            self._set_status("The application is closing.", "warning")
            return
        if self._generation_cancel is not None or self._generation_busy:
            self._set_status("Generation in progress — quiz management is disabled.", "warning")
            return
        database = self._get_database("delete quiz")
        if database is None:
            return
        try:
            quiz = database.get_quiz(quiz_id)
        except BaseException as exc:
            self._show_async_error(exc, title="Could not load quiz for deletion")
            return
        if quiz is None:
            if self.selected_quiz and self.selected_quiz.id == quiz_id:
                self._clear_quiz_details()
                self._refresh_quizzes()
            else:
                self._refresh_quizzes()
            self._set_status("That quiz is already deleted; refreshed the quiz list.", "warning")
            return
        try:
            confirmed = messagebox.askyesno(
                "Delete quiz?",
                f'“{quiz.name}” ({len(quiz.questions)} questions) and all saved attempts will be deleted.',
                icon="warning",
                parent=self,
            )
        except BaseException as exc:
            self._report_ui_callback_error(exc)
            return
        if not confirmed:
            return
        try:
            deleted = database.delete_quiz(quiz_id)
        except BaseException as exc:
            self._set_status(f"Delete failed: {_safe_exception_message(exc)}", "error")
            self._show_async_error(exc, title="Could not delete quiz")
            return
        delete_status = (
            "Quiz was already deleted; refreshed the quiz list."
            if not deleted
            else "Quiz deleted — its saved attempts were also deleted."
        )
        was_selected = self.selected_quiz is not None and self.selected_quiz.id == quiz_id
        if was_selected:
            self._clear_quiz_details()
            self._refresh_quizzes()
        else:
            self._refresh_quizzes()
        self._set_status(delete_status, "warning" if not deleted else "success")

    def _select_quiz(self, quiz_id: int, rerender: bool = True) -> None:
        if self._closing:
            self._set_status("The application is closing.", "warning")
            return
        if self._generation_cancel is not None or self._generation_busy:
            self._set_status("Generation in progress — quiz selection is disabled.", "warning")
            return
        database = self._get_database("load quiz")
        if database is None:
            return
        try:
            quiz = database.get_quiz(quiz_id)
        except BaseException as exc:
            self._show_async_error(exc, title="Could not load quiz")
            return
        if quiz is None:
            self._refresh_quizzes()
            self._set_status("That quiz is no longer available; refreshed the quiz list.", "warning")
            return
        try:
            self.selected_quiz = quiz
            if rerender:
                self._refresh_quizzes(select_id=quiz_id)
                return
            self.quiz_detail_title.configure(text=quiz.name)
            self.quiz_detail_meta.configure(
                text=f"{len(quiz.questions)} questions\nGenerated {_format_datetime(quiz.created_at)}"
            )
            self.take_quiz_button.configure(state="normal")
            self._refresh_attempts()
            self._set_status(f"Selected quiz: {quiz.name}.", "info")
        except BaseException as exc:
            self._show_async_error(exc, title="Could not select quiz")

    def _clear_quiz_details(self) -> None:
        try:
            self.selected_quiz = None
            self.quiz_detail_title.configure(text="Select a quiz")
            self.quiz_detail_meta.configure(
                text="Choose a generated quiz to take it or review past attempts."
            )
            self.take_quiz_button.configure(state="disabled")
            self.attempt_summary.configure(text="No quiz selected")
            _clear_children(self.attempt_list)
            self._attempt_buttons.clear()
        except BaseException as exc:
            self._show_async_error(exc, title="Could not clear quiz details")

    def _refresh_attempts(self) -> None:
        if self._closing or self._destroyed:
            return
        try:
            _clear_children(self.attempt_list)
            self._attempt_buttons.clear()
            quiz = self.selected_quiz
            if quiz is None:
                self.attempt_summary.configure(text="No quiz selected")
                return
        except BaseException as exc:
            self._show_async_error(exc, title="Could not refresh attempts")
            return
        database = self._get_database("load attempts")
        if database is None:
            return
        try:
            attempts = database.list_attempts(quiz.id)
        except BaseException as exc:
            self._show_async_error(exc, title="Could not load attempts")
            return
        try:
            if not attempts:
                self.attempt_summary.configure(text="No attempts yet")
                ctk.CTkLabel(
                    self.attempt_list,
                    text="No results yet.\nTake the quiz to create one.",
                    text_color=COLORS["muted"],
                    font=_font(11),
                    wraplength=320,
                    justify="left",
                ).grid(row=0, column=0, sticky="w", padx=10, pady=12)
                return
            best = max(attempt.score for attempt in attempts)
            self.attempt_summary.configure(
                text=f"{len(attempts)} attempt{'s' if len(attempts) != 1 else ''} · Best {best}/{attempts[0].total}"
            )
            for row, attempt in enumerate(attempts):
                percentage = round((attempt.score / attempt.total) * 100) if attempt.total else 0
                color = COLORS["success_surface"] if percentage >= 70 else COLORS["red_dark"]
                status_color = COLORS["success_border"] if percentage >= 70 else COLORS["red"]
                button = ctk.CTkButton(
                    self.attempt_list,
                    text=f"{attempt.score}/{attempt.total}  ·  {percentage}%\n{_format_datetime(attempt.completed_at)}",
                    height=58,
                    anchor="w",
                    font=_font(11, "bold" if percentage >= 70 else "normal"),
                    fg_color=color,
                    hover_color=COLORS["success_surface_hover"] if percentage >= 70 else COLORS["panel_hover"],
                    border_width=1,
                    border_color=status_color,
                    corner_radius=9,
                    text_color=COLORS["success_text"] if percentage >= 70 else COLORS["danger_text"],
                    text_color_disabled=COLORS["disabled_text"],
                    state="disabled" if self._controls_blocked() else "normal",
                    command=lambda value=attempt.id: self._open_saved_review(value),
                )
                button.grid(row=row, column=0, sticky="ew", padx=4, pady=3)
                self._attempt_buttons.append(button)
        except BaseException as exc:
            self._show_async_error(exc, title="Could not refresh attempts")

    def _generate_quiz(self) -> None:
        if self._generation_cancel is not None or self._generation_busy or self._closing:
            if self._closing:
                self._set_status("The application is closing.", "warning")
            else:
                self._set_status("Generation is already in progress.", "info")
            return
        question_count = self._get_question_count()
        if question_count is None:
            return
        record = self._save_current()
        if record is None:
            return
        if not self._api_ready:
            self._pending_generation = record
            self._pending_question_count = question_count
            if self._startup_running:
                self._set_status(
                    f"Generation queued for {question_count} questions — waiting for Codex startup…",
                    "info",
                )
            else:
                self._set_status(
                    f"Generation queued for {question_count} questions — starting Codex…",
                    "info",
                )
                self._auth_action()
            return
        if self._auth_status is None or not self._auth_status.signed_in:
            self._pending_generation = record
            self._pending_question_count = question_count
            if self._auth_running or self._login_cancel is not None:
                self._set_status(
                    f"Generation queued for {question_count} questions — sign-in is already in progress…",
                    "warning",
                )
            else:
                self._set_status(
                    f"Generation queued for {question_count} questions — sign in to continue.",
                    "warning",
                )
                self._sign_in()
            return

        self._pending_generation = None
        self._pending_question_count = None
        self._start_generation(record, question_count)

    def _resume_pending_generation(self) -> None:
        pending = self._pending_generation
        if pending is None or self._closing or self._destroyed:
            return
        if not self._api_ready or self._startup_running:
            count = self._pending_question_count or DEFAULT_QUESTION_COUNT
            self._set_status(
                f"Generation queued for {count} questions — waiting for Codex startup…",
                "info",
            )
            return
        if self._auth_status is None or not self._auth_status.signed_in:
            count = self._pending_question_count or DEFAULT_QUESTION_COUNT
            self._set_status(
                f"Generation queued for {count} questions — sign in to continue.",
                "warning",
            )
            return

        database = self._get_database("resume generation")
        if database is None:
            return
        try:
            record = database.get_transcript(pending.id)
        except BaseException as exc:
            self._show_async_error(exc, title="Could not resume generation")
            return
        if record is None:
            self._pending_generation = None
            self._pending_question_count = None
            self._set_status(
                "Generation cancelled — the queued transcript is no longer available.",
                "warning",
            )
            try:
                messagebox.showwarning(
                    "Transcript unavailable",
                    "The queued transcript was deleted before generation could start.",
                    parent=self,
                )
            except BaseException as exc:
                self._report_ui_callback_error(exc, "Could not show generation warning")
            return

        self._pending_generation = None
        question_count = self._pending_question_count or DEFAULT_QUESTION_COUNT
        self._pending_question_count = None
        self._set_status(f"Resuming queued generation for {question_count} questions…", "info")
        self._start_generation(record, question_count)

    def _load_previous_questions(self, transcript_id: int) -> list[dict]:
        database = self.db
        if database is None:
            raise PreviousQuizContextError(
                "Could not load previous quiz context: the local database is unavailable. "
                "Restore the database and retry generation."
            )
        try:
            quizzes = database.list_quizzes(transcript_id)
        except BaseException as exc:
            raise PreviousQuizContextError(
                "Could not load previous quiz context from the local database. "
                f"Check the database and retry generation. Details: {_safe_exception_message(exc)}"
            ) from exc
        if not isinstance(quizzes, list):
            raise PreviousQuizContextError(
                "Could not load previous quiz context: the database returned an invalid quiz list. "
                "Retry generation after checking the local database."
            )

        previous_questions: list[dict] = []
        for quiz in quizzes:
            questions = getattr(quiz, "questions", None)
            if not isinstance(questions, list):
                raise PreviousQuizContextError(
                    "Could not load previous quiz context: a saved quiz has invalid questions. "
                    "Repair or delete that quiz, then retry generation."
                )
            for question in questions:
                if not isinstance(question, dict):
                    raise PreviousQuizContextError(
                        "Could not load previous quiz context: a saved question has invalid data. "
                        "Repair or delete that quiz, then retry generation."
                    )
                previous_questions.append(question)
                if len(previous_questions) >= MAX_PREVIOUS_QUESTIONS_FOR_GENERATION:
                    return previous_questions
        return previous_questions

    def _start_generation(self, record: TranscriptRecord, question_count: int | None = None) -> None:
        if self._closing or self._destroyed:
            return
        if self._generation_cancel is not None or self._generation_busy:
            return
        if question_count is None:
            question_count = self._get_question_count()
            if question_count is None:
                return
        if not isinstance(question_count, int) or isinstance(question_count, bool):
            self._set_status("Question count must be a valid integer before generation can start.", "error")
            return
        if not QUESTION_COUNT_MIN <= question_count <= QUESTION_COUNT_MAX:
            self._set_status(
                f"Question count must be from {QUESTION_COUNT_MIN} through {QUESTION_COUNT_MAX}.",
                "error",
            )
            return

        cancel_event = threading.Event()
        self._generation_cancel = cancel_event
        self._set_generation_busy(True, f"Preparing {question_count}-question quiz…")

        def work(token: int) -> Any:
            self._ui_queue.put(
                (
                    "generation_progress",
                    token,
                    f"Preparing {question_count}-question quiz — loading previous questions",
                )
            )
            previous_questions = self._load_previous_questions(record.id)
            self._ui_queue.put(
                (
                    "generation_progress",
                    token,
                    f"Preparing {question_count}-question quiz — loaded "
                    f"{len(previous_questions)} previous questions",
                )
            )
            return self.api.generate_quiz(
                record.content,
                on_progress=lambda message: self._ui_queue.put(
                    ("generation_progress", token, f"{question_count} questions — {message}")
                ),
                cancel_event=cancel_event,
                question_count=question_count,
                previous_questions=previous_questions,
            )

        def success(generated: Any) -> None:
            self._generation_token = None
            self._generation_cancel = None
            self._set_generation_busy(False)

            database = self._get_database("save quiz")
            if database is None:
                return
            try:
                current_record = database.get_transcript(record.id)
            except BaseException as exc:
                self._show_async_error(exc, title="Could not verify transcript after generation")
                return
            if current_record is None:
                self._set_status(
                    "Generation finished, but the transcript was removed; no quiz was saved.",
                    "warning",
                )
                try:
                    messagebox.showwarning(
                        "Transcript removed",
                        "The quiz was generated, but its transcript no longer exists.",
                        parent=self,
                    )
                except BaseException as exc:
                    self._report_ui_callback_error(exc, "Could not show generation warning")
                return
            try:
                quiz = database.save_quiz(record.id, generated.questions)
            except BaseException as exc:
                self._show_async_error(exc, title="Could not save quiz")
                return
            if self.selected_transcript and self.selected_transcript.id == record.id:
                self._refresh_quizzes(select_id=quiz.id)
            model_name = getattr(generated, "model", None)
            model_label = model_name if isinstance(model_name, str) and model_name.strip() else "account default"
            fallback_note = self._sync_model_after_generation(model_name)
            generated_count = len(generated.questions) if isinstance(generated.questions, list) else question_count
            saved_message = (
                f"Quiz saved: {generated_count} questions using model {model_label.strip()}."
            )
            self._set_status(
                f"{fallback_note} {saved_message}" if fallback_note else saved_message,
                "success" if fallback_note is None else "warning",
            )

        def error(exc: BaseException) -> None:
            cancelled = isinstance(exc, GenerationCancelledError)
            self._generation_token = None
            self._generation_cancel = None
            self._set_generation_busy(False)
            if cancelled:
                self._pending_generation = None
                self._pending_question_count = None
                self._set_status("Generation cancelled.", "warning")
                return
            if isinstance(exc, AuthRequiredError):
                self._pending_generation = record
                self._pending_question_count = question_count
                self._apply_auth_status(AuthStatus(False, None, None, True))
                self._set_status(
                    f"Generation paused for {question_count} questions — sign in to continue.",
                    "warning",
                )
                if self._login_cancel is None and not self._auth_running:
                    self._sign_in()
                return
            if isinstance(exc, PreviousQuizContextError):
                self._show_async_error(exc, title="Could not load previous quiz context")
                return
            self._show_async_error(exc, title="Quiz generation failed")

        self._generation_token = self._run_async(work, success, error)

    def _set_generation_busy(self, busy: bool, message: str = "") -> None:
        self._generation_busy = busy
        state = "disabled" if busy else "normal"
        try:
            self.save_button.configure(state=state)
            self.delete_button.configure(
                state="disabled" if busy or self.selected_transcript is None else "normal"
            )
            self.generate_button.configure(state=state)
            self.name_entry.configure(state=state)
            self.transcript_text.configure(state=state)
            self.auth_button.configure(state="disabled" if busy else "normal")
            if busy:
                self.cancel_generate_button.configure(state="normal")
                self.progress_label.configure(text=_ellipsize(message, 82))
                self.progress_frame.grid()
                self.progress_bar.start()
                self.cancel_generate_button.grid()
            else:
                self.progress_bar.stop()
                self.progress_frame.grid_remove()
                self.cancel_generate_button.grid_remove()
                if self._api_ready and self._auth_status is not None:
                    self._apply_auth_status(self._auth_status, refresh_models=False)
            self._update_control_states()
        except BaseException as exc:
            self._report_ui_callback_error(exc, "Could not update generation controls")

    def _cancel_generation(self) -> None:
        if self._generation_cancel is not None:
            self._generation_cancel.set()
            try:
                self.progress_label.configure(text="Cancelling generation…")
                self.cancel_generate_button.configure(state="disabled")
            except BaseException as exc:
                self._report_ui_callback_error(exc, "Could not update cancellation progress")
            self._set_status("Cancelling generation…", "warning")

    def _start_quiz(self) -> None:
        quiz = self.selected_quiz
        if quiz is None:
            return
        if self._is_dirty():
            if not messagebox.askyesno(
                "Save transcript first?",
                "Save your transcript changes before starting the quiz.",
                parent=self,
            ):
                return
            if self._save_current() is None:
                return
            quiz = self.selected_quiz
            if quiz is None:
                self._set_status("The selected quiz is no longer available.", "warning")
                return
        database = self._get_database("load quiz")
        if database is None:
            return
        try:
            current_quiz = database.get_quiz(quiz.id)
        except BaseException as exc:
            self._show_async_error(exc, title="Could not verify quiz before starting")
            return
        if current_quiz is None:
            self._clear_quiz_details()
            self._refresh_quizzes()
            self._set_status("That quiz was deleted before it could be started.", "warning")
            return
        quiz = current_quiz
        self.selected_quiz = current_quiz
        if self._quiz_window is not None and self._quiz_window.winfo_exists():
            self._quiz_window.focus_force()
            return
        if not quiz.questions:
            messagebox.showerror(
                "Invalid quiz",
                "This saved quiz does not contain any questions.",
                parent=self,
            )
            return
        self._quiz_window = QuizWindow(self, quiz)

    def _untrack_review_window(self, review: ReviewWindow) -> None:
        self._review_windows = [window for window in self._review_windows if window is not review]

    def _attempt_saved(self, quiz: QuizRecord, attempt: AttemptRecord) -> None:
        if self.selected_quiz and self.selected_quiz.id == quiz.id:
            self._refresh_attempts()
        try:
            review = ReviewWindow(self, quiz, attempt)
            self._review_windows.append(review)
        except BaseException as exc:
            self._show_async_error(exc, title="Could not open quiz review")

    def _open_saved_review(self, attempt_id: int) -> None:
        if self._closing:
            self._set_status("The application is closing.", "warning")
            return
        database = self._get_database("load quiz review")
        if database is None:
            return
        try:
            attempt = database.get_attempt(attempt_id)
        except BaseException as exc:
            self._show_async_error(exc, title="Could not load quiz attempt")
            return
        if attempt is None:
            self._set_status("That quiz attempt is no longer available.", "warning")
            self._refresh_attempts()
            return
        try:
            quiz = database.get_quiz(attempt.quiz_id)
        except BaseException as exc:
            self._show_async_error(exc, title="Could not load quiz for review")
            return
        if quiz is None:
            self._set_status("The saved quiz for this attempt is no longer available.", "warning")
            self._refresh_attempts()
            return
        try:
            review = ReviewWindow(self, quiz, attempt)
            self._review_windows.append(review)
        except BaseException as exc:
            self._show_async_error(exc, title="Could not open quiz review")

    def _show_async_error(self, exc: BaseException, title: str = "Something went wrong") -> None:
        if self._closing or self._destroyed:
            return
        if isinstance(exc, CodexNotFoundError):
            message = (
                "Codex CLI was not found. Install it with:\n\n"
                "npm install -g @openai/codex@0.144.5\n\n"
                "Then restart the app.\n\n"
                f"Details: {_safe_exception_message(exc)}"
            )
        elif isinstance(exc, QuizValidationError):
            message = f"The generated quiz was not valid: {_safe_exception_message(exc)}"
        elif isinstance(exc, ApiError):
            message = _safe_exception_message(exc, "The OpenAI connection failed.")
        else:
            message = _safe_exception_message(exc, exc.__class__.__name__)
        self._set_status(f"{title}: {_safe_exception_message(exc)}", "error")
        try:
            messagebox.showerror(title, message, parent=self)
        except BaseException as dialog_error:
            self._report_ui_callback_error(dialog_error, "Could not show error dialog")

    def _show_startup_error(self, exc: BaseException) -> None:
        if self._closing or self._destroyed:
            return
        detail = _safe_exception_message(exc, "Codex could not be started")
        if isinstance(exc, CodexNotFoundError):
            message = (
                "Codex CLI was not found. Install it with:\n\n"
                "npm install -g @openai/codex@0.144.5\n\n"
                f"Startup detail: {detail}"
            )
        else:
            message = f"Codex could not start.\n\nStartup detail: {detail}"
        pending_note = " Generation request remains queued for retry." if self._pending_generation else ""
        self._set_status(f"Codex startup failed: {detail}.{pending_note}", "error")
        try:
            messagebox.showerror("Codex startup failed", message, parent=self)
        except BaseException as dialog_error:
            self._report_ui_callback_error(dialog_error, "Could not show startup error")

    def _disable_widget_tree(self, widget: Any) -> None:
        try:
            children = tuple(widget.winfo_children())
        except BaseException:
            children = ()
        for child in children:
            self._disable_widget_tree(child)
            try:
                child.configure(state="disabled")
            except BaseException:
                pass

    def _disable_controls_for_shutdown(self) -> None:
        try:
            self._update_control_states()
        except BaseException:
            pass
        try:
            for widget in tuple(self.winfo_children()):
                self._disable_widget_tree(widget)
        except BaseException:
            pass

    def _close_child_windows(self) -> None:
        dialog = self._device_dialog
        self._device_dialog = None
        if dialog is not None:
            try:
                if dialog.winfo_exists():
                    dialog.cancel()
            except BaseException:
                try:
                    dialog.destroy()
                except BaseException:
                    pass

        quiz_window = self._quiz_window
        if quiz_window is not None:
            try:
                if quiz_window.winfo_exists():
                    quiz_window.force_close()
            except BaseException:
                try:
                    quiz_window.destroy()
                except BaseException:
                    pass
            self._quiz_window = None

        for review in tuple(self._review_windows):
            try:
                if review.winfo_exists():
                    review.close_window()
            except BaseException:
                try:
                    review.destroy()
                except BaseException:
                    pass
        self._review_windows.clear()

    def _request_shutdown(self) -> None:
        if self._closing:
            return
        losses: list[str] = []
        if self._quiz_window is not None and self._quiz_window.winfo_exists():
            losses.append("the unfinished quiz")
        if self._is_dirty():
            losses.append("unsaved transcript changes")
        if losses:
            if len(losses) == 1:
                detail = losses[0]
            else:
                detail = f"{losses[0]} and {losses[1]}"
            if not messagebox.askyesno(
                "Quit application?",
                f"You will lose {detail}.",
                parent=self,
            ):
                return
        self._closing = True
        self._pending_generation = None
        self._pending_question_count = None
        if self._login_cancel is not None:
            self._login_cancel.set()
        if self._generation_cancel is not None:
            self._generation_cancel.set()
        for after_id in (self._search_after, self._startup_check_after):
            if after_id is not None:
                try:
                    self.after_cancel(after_id)
                except BaseException:
                    pass
        self._search_after = None
        self._startup_check_after = None
        self._set_status("Closing…", "info")
        self._disable_controls_for_shutdown()
        self._close_child_windows()
        try:
            self.auth_text.configure(text="Closing…", text_color=COLORS["muted"])
            self.auth_button.configure(state="disabled")
        except BaseException:
            pass

        def close_api() -> None:
            try:
                self.api.close()
            except BaseException:
                pass
            finally:
                self._ui_queue.put(("shutdown_done",))

        threading.Thread(target=close_api, name="quiz-app-shutdown", daemon=True).start()
        try:
            self._shutdown_after = self.after(1000, self._destroy_now)
        except BaseException:
            self._destroy_now()

    def _destroy_now(self) -> None:
        if self._destroyed:
            return
        shutdown_after = self._shutdown_after
        self._shutdown_after = None
        if shutdown_after is not None:
            try:
                self.after_cancel(shutdown_after)
            except BaseException:
                pass
        self._closing = True
        self._close_child_windows()
        try:
            self.api.force_terminate()
        except BaseException:
            pass
        self._destroyed = True
        try:
            self.destroy()
        except BaseException:
            pass


__all__ = ["QuizApp"]
