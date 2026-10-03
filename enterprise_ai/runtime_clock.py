"""Process-local monotonic leases; persisted wall timestamps are diagnostics only."""
import time


class LeaseClock:
    def __init__(self, clock=None):
        self.clock = clock or time.monotonic
        self.deadlines = {}

    def issue(self, owner, seconds):
        self.deadlines[owner] = self.clock()+seconds

    def live(self, row):
        # Missing owners after a restart are expired, never revived by clock drift.
        return row['lease_until'] > 0 and self.deadlines.get(row['owner'], 0) > self.clock()

    def revoke(self, owner):
        self.deadlines.pop(owner, None)
