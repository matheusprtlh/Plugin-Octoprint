"""Stub de ``octoprint.util.RepeatedTimer`` baseado em ``threading.Timer``."""

import threading


class RepeatedTimer:
    def __init__(self, interval, function, run_first=False):
        self._interval = interval
        self._function = function
        self._run_first = run_first
        self._timer = None
        self._stopped = False

    def start(self):
        if self._run_first:
            try:
                self._function()
            except Exception:
                pass
        self._schedule()

    def _schedule(self):
        if self._stopped:
            return
        self._timer = threading.Timer(self._interval, self._tick)
        self._timer.daemon = True
        self._timer.start()

    def _tick(self):
        if self._stopped:
            return
        try:
            self._function()
        except Exception:
            pass
        self._schedule()

    def cancel(self):
        self._stopped = True
        if self._timer is not None:
            self._timer.cancel()
