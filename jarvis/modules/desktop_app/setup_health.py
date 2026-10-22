"""Setup-health model for the setup checklist (DESIGN.md §4.7) - pure,
no Qt import, so the state logic is testable without a running
application. `setup_checklist.py` owns the Qt widgets that render this;
`views/connectors_view.py` owns gathering the inputs and deciding when
to recompute.

Deliberately not detected: "Claude Desktop isn't installed." No reliable
primitive exists anywhere in this codebase for it (`claude_desktop_setup.
CASE_MISSING` means "no config file yet," not "the app isn't present"),
and inventing new platform-specific detection (an app-bundle/binary
presence check per OS, never built or tested here) risks a false
"not installed" for someone who simply never configured it yet - worse
than the less-specific "not connected" state this folds into instead.
"""

from dataclasses import dataclass
from typing import Literal, Optional

from jarvis.modules.desktop_app import claude_desktop_setup as cds

ItemState = Literal["done", "to_do", "needs_attention", "checking", "dismissed"]
ClaudeDesktopState = Literal[
    "connected", "needs_repair", "not_connected", "translocated", "dismissed",
]


@dataclass(frozen=True)
class SetupHealth:
    signed_in: bool
    # None = unknown yet ("Checking…") - the only item that ever depends
    # on an async daemon round-trip (get_sync_roots); signed_in and
    # claude_desktop are both resolvable from local, synchronous reads
    # the instant this is computed.
    has_source: Optional[bool]
    claude_desktop: ClaudeDesktopState

    @property
    def total_steps(self) -> int:
        return 3

    @property
    def steps_done(self) -> int:
        # Dismissed counts as done, deliberately: DESIGN.md calls Claude
        # Desktop "(optional)" and a dismissed item is simply hidden - if
        # dismissing never counted toward steps_done, a user who
        # dismisses it could never reach all_done, and the checklist
        # (which doubles as the empty-Sources-page card) would never go
        # away even with nothing left to do.
        done = 0
        if self.signed_in:
            done += 1
        if self.has_source is True:
            done += 1
        if self.claude_desktop in ("connected", "dismissed"):
            done += 1
        return done

    @property
    def all_done(self) -> bool:
        return self.steps_done == self.total_steps


def compute_setup_health(
    *,
    signed_in: bool,
    has_source: Optional[bool],
    claude_plan: cds.ConnectClaudeDesktopPlan,
    claude_dismissed: bool,
) -> SetupHealth:
    """Pure: every input is already-resolved data, not re-fetched here.
    Callers gather `signed_in` (`session_store.load_session() is not
    None`), `has_source` (`len(SyncRootsResult.roots) > 0`, or `None`
    before that's known), `claude_plan` (`claude_desktop_setup.
    build_plan()`), and `claude_dismissed` (a QSettings read owned by
    `setup_checklist.py`, not here)."""
    return SetupHealth(
        signed_in=signed_in,
        has_source=has_source,
        claude_desktop=_claude_desktop_state(claude_plan, claude_dismissed),
    )


def _claude_desktop_state(plan: cds.ConnectClaudeDesktopPlan, dismissed: bool) -> ClaudeDesktopState:
    if dismissed:
        return "dismissed"
    if plan.case == cds.CASE_TRANSLOCATED:
        return "translocated"
    if plan.case == cds.CASE_HAS_MCP_SERVERS and plan.existing_jarvis_entry is not None:
        frozen = cds.frozen_mcp_entry()
        if (
            frozen is not None
            and plan.existing_jarvis_entry.get("command") == frozen.get("command")
            and plan.existing_jarvis_entry.get("args") == frozen.get("args")
        ):
            return "connected"
        return "needs_repair"
    # CASE_MISSING, CASE_NO_MCP_SERVERS, CASE_UNREADABLE, CASE_DEV_MODE,
    # CASE_UNSUPPORTED_PLATFORM, or CASE_HAS_MCP_SERVERS with no existing
    # "jarvis" entry yet all fold into "not_connected" - see module
    # docstring for why "not installed" isn't a separate state here. A
    # user who clicks Connect still sees the real, case-accurate
    # ConnectClaudeDesktopDialog instructions - this mapping only affects
    # the one-line checklist row.
    return "not_connected"


def signed_in_item_state(health: SetupHealth) -> ItemState:
    return "done" if health.signed_in else "to_do"


def source_item_state(health: SetupHealth) -> ItemState:
    if health.has_source is None:
        return "checking"
    return "done" if health.has_source else "to_do"


_CLAUDE_DESKTOP_TO_ITEM_STATE: dict[ClaudeDesktopState, ItemState] = {
    "connected": "done",
    "needs_repair": "needs_attention",
    "translocated": "needs_attention",
    "not_connected": "to_do",
    "dismissed": "dismissed",
}


def claude_desktop_item_state(health: SetupHealth) -> ItemState:
    return _CLAUDE_DESKTOP_TO_ITEM_STATE[health.claude_desktop]
