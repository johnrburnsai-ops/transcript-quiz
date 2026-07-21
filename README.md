# Transcript Quiz

A local Python desktop application that turns saved transcripts into configurable multiple-choice quizzes. The interface is built with CustomTkinter, data is stored in SQLite, and OpenAI sign-in is handled in a browser without asking for an API key.

## Important authentication note

OpenAI's ChatGPT/Codex OAuth credentials are not supported credentials for `gpt-4o-mini` on the standard OpenAI Platform API. OpenAI documents Platform API keys for that API and model.

To preserve the **no API keys** requirement, this application uses the supported `codex app-server` integration instead:

- `codex app-server` starts the OpenAI device-code browser login.
- Codex owns token refresh and stores this app's credentials in its isolated file-backed profile.
- This application never reads, copies, logs, or stores OAuth access or refresh tokens.
- Quiz generation prefers the application default `gpt-5.4-mini` when it is present in the live visible Codex catalog, then falls back to the account default. It does not silently fall back to an API key or claim to use `gpt-4o-mini`.

The OpenAI Python SDK cannot turn a ChatGPT/Codex OAuth token into a supported Platform API session, so it is intentionally not used. Direct token handling and inference are delegated to Codex's supported app-server boundary.

## Requirements

- Python 3.11 or newer
- Node.js/npm, Homebrew, or another supported way to install the OpenAI Codex CLI
- A ChatGPT account with Codex access

Install Codex with one of the official methods:

```powershell
npm install -g @openai/codex@0.144.5
```

If `codex` is not on `PATH`, set `CODEX_CLI_PATH` to the executable before launching the app.

The application currently enforces Codex CLI `0.144.5` because its app-server protocol and security controls are version-specific. Upgrade the app and CLI together when support for a newer protocol is added.

## Setup

```powershell
cd C:\quiz-app
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python main.py
```

For a normal source-tree launch without a PowerShell or Python console,
double-click `LaunchTranscriptQuiz.vbs`. It uses
`.venv\Scripts\pythonw.exe`, sets the working directory beside the launcher,
and can also start a frozen `TranscriptQuiz.exe` placed beside it.
`LaunchTranscriptQuiz.cmd` is the visible-console alternative; its source-tree
path uses `python.exe` so startup tracebacks remain visible. Neither launcher
installs Node.js or Codex. Both honor an existing `CODEX_CLI_PATH`, extend the
child `PATH`, and check common npm shim and standalone Codex locations.

## Building and distributing a Windows build

Run `build_exe.cmd` from `C:\quiz-app`. It creates or reuses `.venv`, installs
the Python requirements and PyInstaller into that environment, and writes an
onedir windowed build to `dist\TranscriptQuiz`. The script does not install
Node.js or Codex. OpenAI Codex CLI `0.144.5` is an external runtime
prerequisite and is not bundled:

```powershell
.\build_exe.cmd
```

Distribute the entire `dist\TranscriptQuiz` folder, not just
`TranscriptQuiz.exe`; PyInstaller's `_internal` files and the included
launchers are required. Double-click `LaunchTranscriptQuiz.vbs` in that folder
on the destination machine. The frozen EXE still needs Codex CLI `0.144.5`
installed and discoverable on `PATH`, or configured with `CODEX_CLI_PATH`. The
PyInstaller spec includes CustomTkinter's themes and images, but does not
bundle Codex, OAuth credentials, or the Codex profile.

On first launch:

1. Select **Sign in with OpenAI**.
2. The app asks Codex to begin a device-code login.
3. Your browser opens automatically.
4. Enter the one-time code displayed by the app.
5. Return to the app after authorization completes.

The isolated application profile does not import credentials from your normal `~/.codex` profile. Sign in once inside this app; Codex then keeps that app-specific OAuth session in the app profile's `auth.json` across launches. Treat that file as sensitive.

**Sign out** removes only this app's local Codex credentials; it does not send a server-side logout to OpenCode or standalone Codex CLI. Closing the application does not revoke or remove the local session.

## Using the app

1. Select **New**, enter a name, and paste a transcript.
2. Save the transcript.
3. Choose a question count and select **Generate quiz**. Generation runs in the background and can be cancelled.
4. Select a generated quiz and choose **Take quiz**.
5. Answer all questions, submit, and review the result. Selecting an answer advances automatically, while the number grid and **Previous** button let you revisit questions.

Saved transcripts are grouped by date and searchable. Each transcript can have multiple quizzes; quizzes can be renamed or deleted, and each quiz keeps its attempt history, scores, answers, and elapsed time. Deleting a quiz also deletes its saved attempts.

Quiz generation uses the transcript's prior quizzes as context. It aims to cover the transcript's topic clusters, mix recall with application and troubleshooting questions, use original CompTIA A+-style practice wording when appropriate, and keep at least half of a later quiz's question stems novel. It does not reproduce official exam questions.

After sign-in, the **Available models** selector is populated from the account's live Codex catalog. `gpt-5.4-mini` is the preferred application default when available; otherwise the account default is selected. You can choose any other visible model, including the account default, for the current session. Models are account- and service-dependent; if an explicitly selected model disappears, generation falls back safely. The selector does not promise a particular cost or usage rate.

Keyboard shortcuts:

- `Ctrl+N`: new transcript
- `Ctrl+S`: save transcript
- `Ctrl+Enter`: generate quiz
- Quiz window: `A`-`D` or `1`-`4` selects an answer; Left/Right changes questions

## Local storage

The SQLite database is created at:

- Windows: `%LOCALAPPDATA%\TranscriptQuiz\transcript_quiz.db`
- macOS: `~/Library/Application Support/TranscriptQuiz/transcript_quiz.db`
- Linux: `$XDG_DATA_HOME/transcript-quiz/transcript_quiz.db` or `~/.local/share/transcript-quiz/transcript_quiz.db`

Deleting a transcript also deletes its quizzes and attempts. SQLite content is not encrypted, so anyone with access to your operating-system account may be able to read locally saved transcripts.

Transcript text is sent to the OpenAI Codex service when a quiz is generated. The app starts each generation in a fresh ephemeral, read-only Codex thread with approvals disabled and network access disabled for model-invoked commands.

The app uses a dedicated profile at `%LOCALAPPDATA%\TranscriptQuiz\codex` on Windows (or the platform-equivalent application data directory). It uses file-backed credentials inside that profile so local sign-out cannot remove another tool's credentials. It writes a strict configuration that permits only ChatGPT OAuth with the official OpenAI provider, disables shell, web, MCP, plugin, app, skill, hook, and multi-agent features, and prevents inherited environment credentials or proxy/routing overrides. Generation fails closed if Codex reports a different provider or attempts a tool item. The transcript generation limit is 1 MB.

These controls limit model-visible capabilities; they are not an operating-system sandbox for a compromised Codex executable. Install Codex only from OpenAI's official distribution.

## Validation

Run the automated backend suite:

```powershell
python -m unittest discover -s tests -v
```

The suite uses a fake app-server and does not require a live OpenAI account. It validates:

- SQLite CRUD and cascading deletion
- strict quiz JSON validation
- app-server initialization and message correlation
- device-code challenge/completion handling
- model discovery and generated-quiz handling

A live browser login cannot be completed automatically because it requires the account holder's approval. To smoke-test it, install Codex, run `python main.py`, select **Sign in with OpenAI**, and complete the displayed device flow.

## Troubleshooting

### Codex CLI was not found

Confirm installation:

```powershell
codex --version
```

If needed:

```powershell
$env:CODEX_CLI_PATH = "C:\path\to\codex.exe"
python main.py
```

### No models are available

The available Codex models depend on the signed-in account and current Codex service catalog. Confirm that the account has Codex access, then sign out and sign in again.

### Sign-in or generation fails

Check the network connection, update the Codex CLI, and retry. The application deliberately removes `OPENAI_API_KEY` from the app-server child process so it cannot silently switch to API-key authentication.
