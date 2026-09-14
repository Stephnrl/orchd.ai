"""Process-local operator lease. Never persisted or distributed to workers."""
import hmac
import math
import secrets
import threading
import time

from .contracts import Rejected


class OperatorSession:
    def __init__(self, *, clock=time.monotonic, idle_seconds=3600, lifetime_seconds=28800):
        if (type(idle_seconds) is not int or type(lifetime_seconds) is not int
                or not 1 <= idle_seconds <= lifetime_seconds <= 86400):
            raise Rejected('Invalid operator session policy')
        self._clock = clock
        self._lock = threading.RLock()
        self._token = secrets.token_urlsafe(32)
        self._created = self._last_activity = self._clock()
        self._idle, self._lifetime = idle_seconds, lifetime_seconds
        self._generation = 1
        self._revoked = False

    @property
    def token(self):
        """Bootstrap/rotation delivery only. Do not place in reports or logs."""
        return self._token

    def _valid(self, token, checked):
        # A backwards monotonic clock invalidates the lease rather than extending it.
        if (not math.isfinite(checked) or checked < self._last_activity
                or checked >= self._created+self._lifetime or checked >= self._last_activity+self._idle):
            self._revoked = True
        return (not self._revoked and isinstance(token, str) and len(token) == 43 and token.isascii()
                and hmac.compare_digest(token, self._token))

    def authenticate(self, token, *, touch=True):
        with self._lock:
            checked = self._clock()
            valid = self._valid(token, checked)
            if valid and touch: self._last_activity = checked
            return valid

    def status(self, token):
        with self._lock:
            checked = self._clock()
            if not self._valid(token, checked): raise Rejected('Operator session required')
            return {'kind': 'OperatorSessionStatus', 'generation': self._generation,
                    'idle_timeout_seconds': self._idle, 'absolute_lifetime_seconds': self._lifetime,
                    'idle_remaining_seconds': max(0, int(self._last_activity+self._idle-checked)),
                    'absolute_remaining_seconds': max(0, int(self._created+self._lifetime-checked)),
                    'expires_in_seconds': max(0, int(min(self._last_activity+self._idle, self._created+self._lifetime)-checked)),
                    'persistent': False, 'operator_count': 1}

    def rotate(self, token):
        with self._lock:
            checked = self._clock()
            if not self._valid(token, checked): raise Rejected('Operator session required')
            self._token = secrets.token_urlsafe(32)
            self._generation += 1
            self._last_activity = checked
            # Rotation never extends the absolute process-session lifetime.
            return {'session': self._token, 'status': self.status(self._token)}

    def revoke(self, token):
        with self._lock:
            if not self._valid(token, self._clock()): raise Rejected('Operator session required')
            self._revoked = True
            self._token = ''
            return {'kind': 'OperatorSessionRevocation', 'revoked': True, 'restart_required': True}
