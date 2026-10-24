"""Offline policy prototype. No live platform adapter or send actuator yet.

Inputs must come from current browser-visible evidence. A platform adapter must
not infer delivery from a click, composer clearing, or an optimistic local bubble.
"""
from dataclasses import dataclass
from typing import Literal
import re
import secrets
import string


def make_marker(platform: str) -> str:
    """Alphabetic IDs avoid the numeric-marker phone-card issue seen on Instagram.

    This does not disable link previews or other transformations of message text.
    """
    if not re.fullmatch('[A-Z]+', platform):
        raise ValueError('Use an uppercase alphabetic platform tag')
    return 'UIAGENT-' + platform + '-' + ''.join(secrets.choice(string.ascii_uppercase) for _ in range(20))


@dataclass(frozen=True)
class Scope:
    platform: str
    sender: str
    conversation: str
    recipients: tuple[str, ...]


@dataclass(frozen=True)
class Contract:
    operation_id: str
    scope: Scope
    body: str
    marker: str

    @property
    def text(self):
        return f"[{self.marker}] {self.body}"


@dataclass(frozen=True)
class SendGrant:
    # Construct only from the human user's explicit test-recipient authorization.
    scope: Scope
    allow_test_sends: bool = False
    self_only: bool = False


@dataclass(frozen=True)
class Receipt:
    text: str
    outgoing: bool
    status: Literal['local', 'sending', 'sent', 'delivered', 'read', 'failed', 'unknown', 'persisted']


@dataclass(frozen=True)
class Observation:
    scope: Scope
    authenticated: bool = True
    challenge: bool = False
    draft: str = ''
    receipts: tuple[Receipt, ...] = ()
    freshly_reopened: bool = False
    identity_unique: bool = True


@dataclass(frozen=True)
class Prior:
    phase: Literal['new', 'composing', 'send_intent', 'uncertain', 'confirmed', 'visible'] = 'new'
    unresolved_peer: bool = False


@dataclass(frozen=True)
class Decision:
    status: str
    reason: str


def decide(c: Contract, o: Observation, prior: Prior, grant: SendGrant | None) -> Decision:
    """Return a bounded next step, never execute a send or grant permission."""
    if not c.operation_id or not c.body or not c.marker.startswith('UIAGENT-'):
        return Decision('blocked', 'Missing immutable operation identity, content, or synthetic marker')
    if not c.scope.recipients or len(set(c.scope.recipients)) != len(c.scope.recipients):
        return Decision('blocked', 'Recipient identity is incomplete or duplicated')
    if grant is None or not grant.allow_test_sends or grant.scope != c.scope:
        return Decision('blocked', 'No matching human authorization for this test scope')
    if grant.self_only and c.scope.recipients != (c.scope.sender,):
        return Decision('blocked', 'Self-only authorization requires exactly the signed-in account as recipient')
    if not o.authenticated or o.challenge:
        return Decision('needs_user', 'Sign-in or an account challenge requires the user')
    if o.scope != c.scope or not o.identity_unique:
        return Decision('blocked', 'Current sender, conversation or recipient identity does not match')

    marked = [r for r in o.receipts if r.outgoing and f'[{c.marker}]' in r.text]
    if len(marked) > 1:
        return Decision('duplicate_detected', 'Multiple outgoing messages carry this operation marker')
    if marked and marked[0].text != c.text:
        return Decision('blocked', 'The marker belongs to different visible content')
    if marked and o.freshly_reopened and marked[0].status in ('sent', 'delivered', 'read'):
        return Decision('confirmed', f'Fresh conversation shows exact outgoing message: {marked[0].status}')
    if marked and o.freshly_reopened and marked[0].status == 'persisted':
        return Decision('visible', 'Exact outgoing message persists after reopening; delivery/read state is not established')
    if prior.phase in ('confirmed', 'visible'):
        return Decision('blocked', 'Previously confirmed message is no longer confirmed; do not resend')
    if marked or prior.phase in ('send_intent', 'uncertain'):
        return Decision('uncertain', 'Submission may exist; inspect again without sending')
    if prior.unresolved_peer:
        return Decision('waiting', 'An earlier unresolved send fences this conversation')
    if o.draft and o.draft != c.text:
        return Decision('blocked', 'Preserve the existing or concurrently changed draft')
    if o.draft == c.text:
        return Decision('ready_to_send', 'Recheck scope and exact text, then persist send intent before one UI send')
    return Decision('ready_to_compose', 'Authorized scope is unique and composer is empty')
