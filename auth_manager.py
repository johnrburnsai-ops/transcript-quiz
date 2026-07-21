from __future__ import annotations

import threading
import time
import webbrowser
from dataclasses import dataclass
from typing import Any, Callable

from api_handler import (
    ApiError,
    ApiHandler,
    AuthRequiredError,
    GenerationCancelledError,
    _sanitize_message,
)


@dataclass(frozen=True)
class DeviceChallenge:
    login_id: str
    verification_url: str
    user_code: str


@dataclass(frozen=True)
class AuthStatus:
    signed_in: bool
    email: str | None
    plan_type: str | None
    requires_openai_auth: bool


def _params(event: dict[str, Any]) -> dict[str, Any]:
    value = event.get("params")
    return value if isinstance(value, dict) else {}


def _login_id(value: dict[str, Any]) -> str | None:
    direct = value.get("loginId")
    if isinstance(direct, str) and direct:
        return direct
    login = value.get("login")
    if isinstance(login, dict):
        nested = login.get("id")
        if isinstance(nested, str) and nested:
            return nested
    return None


class AuthManager:
    LOGIN_TIMEOUT_SECONDS = 15 * 60

    def __init__(self, api: ApiHandler):
        self.api = api

    def check_status(self) -> AuthStatus:
        response = self.api.get_account()
        account = response.get("account")
        account_type = account.get("type") if isinstance(account, dict) else None
        if isinstance(account, dict) and account_type != "chatgpt":
            raise AuthRequiredError("Only ChatGPT OAuth sign-in is allowed by this application")
        signed_in = isinstance(account, dict) and account_type == "chatgpt"
        email: str | None = None
        plan_type: str | None = None
        if isinstance(account, dict):
            raw_email = account.get("email")
            if isinstance(raw_email, str) and raw_email:
                email = raw_email
            raw_plan = account.get("planType")
            if not isinstance(raw_plan, str):
                raw_plan = account.get("plan_type")
            if isinstance(raw_plan, str) and raw_plan:
                plan_type = raw_plan
        return AuthStatus(
            signed_in=signed_in,
            email=email,
            plan_type=plan_type,
            requires_openai_auth=bool(response.get("requiresOpenaiAuth", False)),
        )

    @staticmethod
    def _challenge_from_response(response: Any) -> DeviceChallenge:
        if not isinstance(response, dict):
            raise AuthRequiredError("Codex returned an invalid device-login response")
        login_id = _login_id(response)
        verification_url = response.get("verificationUrl")
        if not isinstance(verification_url, str) or not verification_url:
            verification_url = response.get("authUrl")
        user_code = response.get("userCode")
        if not isinstance(login_id, str) or not login_id:
            raise AuthRequiredError("Codex did not return a login identifier")
        if not isinstance(verification_url, str) or not verification_url:
            raise AuthRequiredError("Codex did not return a verification URL")
        if not isinstance(user_code, str) or not user_code:
            raise AuthRequiredError("Codex did not return a device user code")
        return DeviceChallenge(login_id, verification_url, user_code)

    @staticmethod
    def _completion_matches(event: dict[str, Any], login_id: str) -> bool:
        return _login_id(_params(event)) == login_id

    @staticmethod
    def _update_matches(event: dict[str, Any], login_id: str) -> bool:
        event_login_id = _login_id(_params(event))
        return event_login_id is None or event_login_id == login_id

    def _cancel_login(self, login_id: str) -> None:
        try:
            self.api.request(
                "account/login/cancel",
                {"loginId": login_id},
                timeout=min(self.api.request_timeout, 10.0),
            )
        except ApiError:
            pass

    def sign_in(
        self,
        on_challenge: Callable[[DeviceChallenge], None],
        cancel_event: threading.Event | None = None,
    ) -> AuthStatus:
        status = self.check_status()
        if status.signed_in:
            return status
        if cancel_event is not None and cancel_event.is_set():
            raise GenerationCancelledError("Sign-in cancelled")

        event_mark = self.api.mark_events()
        response = self.api.request(
            "account/login/start",
            {"type": "chatgptDeviceCode"},
        )
        challenge = self._challenge_from_response(response)
        deadline = time.monotonic() + self.LOGIN_TIMEOUT_SECONDS

        try:
            on_challenge(challenge)
            webbrowser.open(challenge.verification_url)
            if cancel_event is not None and cancel_event.is_set():
                raise GenerationCancelledError("Sign-in cancelled")

            completed = self.api.wait_for_event(
                "account/login/completed",
                after=event_mark,
                timeout=max(0.001, deadline - time.monotonic()),
                predicate=lambda event: self._completion_matches(event, challenge.login_id),
                cancel_event=cancel_event,
            )
            completed_params = _params(completed)
            completion_error = completed_params.get("error")
            completion_status = completed_params.get("status")
            if (
                completed_params.get("success") is False
                or completion_error not in (None, "")
                or completion_status in {"failed", "error", "cancelled"}
            ):
                raise AuthRequiredError(
                    _sanitize_message(completion_error, "Codex device sign-in was not completed")
                )

            updated = self.api.wait_for_event(
                "account/updated",
                after=event_mark,
                timeout=max(0.001, deadline - time.monotonic()),
                predicate=lambda event: self._update_matches(event, challenge.login_id),
                cancel_event=cancel_event,
            )
            auth_mode = _params(updated).get("authMode")
            if auth_mode not in (None, "chatgpt"):
                raise AuthRequiredError("Codex completed sign-in with an unsupported auth mode")
            self.api.audit_security()
            final_status = self.check_status()
            if not final_status.signed_in:
                raise AuthRequiredError("Codex sign-in completed, but no account is available")
            return final_status
        except GenerationCancelledError:
            self._cancel_login(challenge.login_id)
            raise GenerationCancelledError("Sign-in cancelled") from None
        except ApiError:
            self._cancel_login(challenge.login_id)
            raise
        except BaseException:
            self._cancel_login(challenge.login_id)
            raise

    def sign_out(self) -> None:
        self.api.request("account/logout", {})
