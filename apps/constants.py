from __future__ import annotations

from typing import Final


DEFAULT_BASE_URL: Final = "http://127.0.0.1:8500"
DEFAULT_PROMPT_ROLES: Final = "MANAGER,PLAN,DEV,REVIEW,AUDIT,A,B"
DEFAULT_BROWSER_ROLES: Final = "DEV,REVIEW"
DEFAULT_FINISH_ROLES: Final = "MANAGER"
DEFAULT_MAX_STATE_CHARS: Final = 30000
DEFAULT_HANDOFF_STATE_CHARS: Final = 24000
DEFAULT_HANDOFF_RESPONSE_CHARS: Final = 12000
DEFAULT_RESPONSE_ACTIVE_WAIT_BEFORE_RELOAD_S: Final = 300.0
DEFAULT_RESPONSE_RECOVERY_RELOAD_DELAY_S: Final = DEFAULT_RESPONSE_ACTIVE_WAIT_BEFORE_RELOAD_S
DEFAULT_RESPONSE_RECOVERY_PAGE_WAIT_S: Final = 10.0
DEFAULT_RESPONSE_RECOVERY_POLL_S: Final = 2.0
# Two matching samples must span at least this much real elapsed time before
# being trusted as "settled" -- mirrors the browser-side completion_confirm_ms
# fix: a merely-different observation_seq is not proof of genuine quiet time,
# only that another poll happened to run.
DEFAULT_RESPONSE_STABLE_CONFIRM_S: Final = 2.5
DEFAULT_UPLOAD_METHOD: Final = "drop"
ALLOWED_COMMANDS: Final = {"", "none", "handoff"}
