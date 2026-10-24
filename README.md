# BrowserPilot

A local Windows app that uses an LLM to discover and operate CRM interfaces through the browser. Connect a website, describe a task, review the proposed fields, and inspect the resulting evidence.

**Version 1.1 is a preview.** Generic creation and duplicate prevention have been tested on local Dolibarr and Odoo instances with platform-specific adapters disabled. Salesforce and HubSpot URLs are supported as connection inputs, but their live workflows have not been validated. API-level reliability and speed have not been established.

## Run the Windows app

1. Download `BrowserPilot_Windows_v1.1.zip` from [Releases](https://github.com/RichardAragon/browserpilot/releases).
2. Extract the archive. Open `BrowserPilot.exe` in its `BrowserPilot` folder, keeping `_internal` beside it.
3. In **Settings**, select your existing Codex sign-in or provide an OpenAI API key and a model that supports structured outputs.
4. Add a website and let the assistant inspect it. Complete sign-in in the separate browser when prompted. Microsoft Edge is required for interactive sessions.
5. Describe a record creation or update, review the actual form fields, and approve the change.

The package includes Python and the browser runtime. It is an unsigned preview build. To quit the local server, use **How it works → Quit BrowserPilot**; closing its dashboard tab alone leaves it running.

## Capabilities

- Generic website setup, sign-in control discovery, navigation and field mapping.
- Exact-name duplicate checks and reviewed creation or updates of one record using text/select fields.
- Persistent per-connection platform notes learned from setup and task outcomes. Notes include evidence references, user corrections and revision history.
- Durable operation IDs, write-intent journals, interrupted-session recovery and readback verification.
- Optional optimized Dolibarr/Odoo paths and limited, synthetic self-messaging workflows for Messenger/Instagram.

All agent-authored CRM and social updates use browser controls. Websites can still make their normal internal network requests. Calls to the chosen LLM are separate from platform updates.

## Notes and safety boundaries

Open **Connections → Platform notes** to inspect learned lessons, add guidance, learn from history or restore a revision. Notes are advisory: the controller still requires fresh visible controls, current-state checks and the applicable field review. AI updates cannot silently overwrite a newer user edit.

Creation records intent before opening a new form because some sites autosave drafts. If a form or submission becomes uncertain, recovery inspects the original outcome instead of blindly repeating creation. This can leave a lost draft flagged for manual review.

Browser verification is not a server transaction. It cannot guarantee atomic isolation against another writer or server-enforced uniqueness. Duplicate checks are limited by the account's visibility and the rendered search scope. General deletion, bulk operations, financial changes and permission updates are outside the supported scope.

## Develop from source

Use Windows and Python 3.12. The app uses Windows DPAPI for saved secrets; the current test suite includes Windows-specific credential tests.

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt pytest==9.1.1
.\.venv\Scripts\python.exe -m playwright install chromium --only-shell
.\.venv\Scripts\python.exe app.py
```

Run tests without connecting to a live CRM:

```powershell
.\.venv\Scripts\python.exe -m pytest tests -q
```

Build a Windows bundle:

```powershell
.\build.ps1 -Python .\.venv\Scripts\python.exe
```

The generated app is under `release/BrowserPilot/`. For isolated manual testing, launch with `--data data/test-workspace --port 8767`. Setting `BROWSERPILOT_GENERIC_ONLY=1` disables platform-specific execution paths for validation.

## Project layout

| Path | Purpose |
| --- | --- |
| `app.py`, `web/` | Local server and dashboard |
| `pilot/` | Task queue, model transport, generic setup/creation/update and connection memory |
| `uiagent/` | Browser observations, grounded control actions and operation journals |
| `tests/` | Controller, recovery, memory, local server and model transport tests |
| `build.ps1` | Windows packaging |

## Validation and data

The 1.1 validation run passed **103 automated tests**. Live tests covered generic creation on both CRMs, duplicate prevention, restart recovery, real-model note generation and packaged-app execution. Two early Odoo form-discovery attempts remained uncertain; recovery preserved their intent fences. These are synthetic local examples, not a performance benchmark.

See [the validation report](REPORT_v1.1.html), [structured results](VALIDATION_v1.1.json) and [user guide](START_HERE.html). HTML documents can be downloaded and opened locally.

Application state lives in `%LOCALAPPDATA%\BrowserPilot` by default. It includes browser profiles, screenshots, journals, notes and history. Saved passwords and API keys use Windows account encryption. The selected model receives relevant task, page and memory context. Known task values are filtered from learning evidence, but that is not complete anonymization.

This repository and its release archives exclude local databases, credentials, signed-in profiles and raw CRM screenshots. No open-source license is granted by this repository at this time; third-party dependencies retain their own licenses.
