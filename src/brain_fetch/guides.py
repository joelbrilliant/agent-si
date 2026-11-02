"""brain-fetch setup: a printed, step-by-step guide for one service."""
from __future__ import annotations

from brain_fetch import credentials

DEFAULT_NAMES = {"documents": "work-docs", "gmail": "work-mail", "slack": "work-slack", "calendar": "work-calendar",
                 "github": "work-github"}

DOCUMENTS = """brain-fetch setup: the documents folder for {instance}

No sign-in is needed. brain-fetch reads one folder on this machine and never changes it.

1. Add this to {dir}/brain.toml, with your own folder:

[agents.{agent}]
kind = "collector"
operations = ["add_source", "record_collector_run"]

[collectors.{name}]
type = "capture"
agent = "{agent}"
origin = "{name}"
kinds = ["document"]
live_folder = "/Users/example/Documents/Example Co"

2. brain apply-config --instance {dir}
3. brain-fetch check --instance {dir}
4. brain-fetch run --instance {dir} {name}
5. brain-fetch schedule --instance {dir}

It captures Markdown, text, Word and PDF files. PDF text needs pypdf: reinstall the agent-si wheel with uv's --with pypdf==6.19.0.
Meeting transcripts (.vtt, .srt, .transcript.txt, .transcript.json) are captured when kinds includes meeting_transcript. Remove one with brain redact.
"""

_GOOGLE = """1. In the Google Cloud console, create a project, enable the {api}, and create an OAuth client of type
   "Web application" with the authorised redirect URI https://developers.google.com/oauthplayground.
   Make the app internal (OAuth consent screen, User type: Internal) if your account is in a Google Workspace.
   An app left in "Testing" status gets refresh tokens that expire after 7 days: then plan to mint a new token
   each week (step 2 again), or the collector shows as failing on the brief's Intake line.
2. Mint a refresh token as the account itself: open https://developers.google.com/oauthplayground, open the gear,
   tick "Use your own OAuth credentials", paste the client id and secret, authorise only the scope
   {scope}, and exchange the code for tokens.
3. Store the credential as one line of JSON. The command asks for the value, so it never reaches your shell history:

{store}

   The value: {{"client_id": "...", "client_secret": "...", "refresh_token": "..."}}
   On a server, put the same line in /run/secrets/{name} (mode 0600) instead.
"""

_STEPS = """
5. brain apply-config --instance {dir}
6. brain-fetch check --instance {dir} {name}
7. brain-fetch run --instance {dir} {name}
8. brain-fetch schedule --instance {dir}
"""

GMAIL = """brain-fetch setup: Gmail for {instance}

brain-fetch reads one mailbox, read-only. It keeps the text of mail matching live_query, with addresses and phone
numbers replaced, quoted history and signatures dropped, and only the names and sizes of attachments.

""" + _GOOGLE + """
4. Add this to {dir}/brain.toml. The threads collector keeps reply tracking, without subjects:

[agents.{agent}]
kind = "collector"
operations = ["add_source", "record_collector_run"]
author_principal = true

[collectors.{name}]
type = "capture"
agent = "{agent}"
origin = "{name}"
kinds = ["email"]
service = "gmail"
threads = "{name}-threads"
# live_query = "..."  (default: inbox and sent, minus Promotions, Social, Updates and Forums, last 14 days)

[agents.{agent}threads]
kind = "collector"
operations = ["add_source", "upsert_thread", "record_collector_run"]

[collectors.{name}-threads]
type = "gmail"
agent = "{agent}threads"
account = "work"
with_subject = false
""" + _STEPS + """
A message that starts matching the query later without being newly added (say, moved back to the inbox) isn't read.
"""

SLACK = """brain-fetch setup: Slack for {instance}

brain-fetch reads only the channels you list, and your DMs only with include_dms = true. Bot and app posts are
dropped, and addresses and phone numbers are replaced. A thread is captured once it has been quiet for 24 hours.

1. At https://api.slack.com/apps, create an app from scratch in your own workspace. Never distribute it: a
   distributed app outside the Marketplace is limited to 1 call a minute.
2. Under OAuth & Permissions, add these User Token Scopes: channels:history, groups:history and users:read;
   channels:read and groups:read to show channel names; im:history and im:read only with include_dms.
   Install the app and copy the User OAuth Token.
3. Store the token. The command asks for the value, so it never reaches your shell history:

{store}

   On a server, put it in /run/secrets/{name} (mode 0600) instead.
4. Add this to {dir}/brain.toml. A channel's id is at the bottom of its About tab:

[agents.{agent}]
kind = "collector"
operations = ["add_source", "record_collector_run"]
author_principal = true

[collectors.{name}]
type = "capture"
agent = "{agent}"
origin = "{name}"
kinds = ["slack"]
service = "slack"
live_channels = ["C0123ABCD"]
include_dms = false
threads = "{name}-threads"

[agents.{agent}threads]
kind = "collector"
operations = ["add_source", "upsert_thread", "record_collector_run"]

[collectors.{name}-threads]
type = "slack"
agent = "{agent}threads"
account = "work"
""" + _STEPS

CALENDAR = """brain-fetch setup: Google Calendar for {instance}

brain-fetch reads one calendar, read-only, for meeting times and reply tracking. No text is captured.

""" + _GOOGLE + """
4. Add this to {dir}/brain.toml:

[agents.{agent}]
kind = "collector"
operations = ["add_source", "upsert_thread", "upsert_meeting", "record_collector_run"]

[collectors.{name}]
type = "calendar"
agent = "{agent}"
account = "work"
live_calendar_id = "primary"
live_window_days = 14
""" + _STEPS + """
An event counts as yours (updated by you) when you organised it. Google doesn't say who last changed an event.
"""

GITHUB = """brain-fetch setup: GitHub for {instance}

brain-fetch reads the Markdown files on one repository's default branch, and its release notes.

1. On GitHub, open Settings, Developer settings, Fine-grained tokens, and generate a token for only this
   repository, with Repository permissions, Contents: Read-only. Nothing else.
2. Store the token. The command asks for the value, so it never reaches your shell history:

{store}

   On a server, put it in /run/secrets/{name} (mode 0600) instead.
3. Add this to {dir}/brain.toml:

[agents.{agent}]
kind = "collector"
operations = ["add_source", "record_collector_run"]

[collectors.{name}]
type = "capture"
agent = "{agent}"
origin = "{name}"
kinds = ["document"]
service = "github"
repo = "example-co/handbook"
""" + _STEPS.replace("5.", "4.").replace("6.", "5.").replace("7.", "6.").replace("8.", "7.")

_TEXT = {"documents": DOCUMENTS, "gmail": GMAIL, "slack": SLACK, "calendar": CALENDAR, "github": GITHUB}
_SCOPES = {"gmail": ("Gmail API", "https://www.googleapis.com/auth/gmail.readonly"),
           "calendar": ("Google Calendar API", "https://www.googleapis.com/auth/calendar.readonly")}


def guide(service: str, instance_name: str, directory: str, name: str) -> str:
    api, scope = _SCOPES.get(service, ("", ""))
    return _TEXT[service].format(instance=instance_name, dir=directory, name=name, agent=name.replace("-", ""),
                                 api=api, scope=scope, store=credentials.store_command(instance_name, name))
