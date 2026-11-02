from brain.collectors.calendar import TYPE as CALENDAR
from brain.collectors.capture import TYPE as CAPTURE
from brain.collectors.files import TYPE as FILES
from brain.collectors.github import TYPE as GITHUB
from brain.collectors.messages import GMAIL, SLACK
from brain.collectors.vanta import TYPE as VANTA

REGISTRY = {
    "files": FILES,
    "calendar": CALENDAR,
    "github": GITHUB,
    "vanta": VANTA,
    "gmail": GMAIL,
    "slack": SLACK,
    "capture": CAPTURE,
}
