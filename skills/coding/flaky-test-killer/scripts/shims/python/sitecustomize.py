# Clock shim loaded by stress.py through PYTHONPATH. It moves the wall clock
# to FLAKE_FAKE_NOW_EPOCH (the clock keeps ticking from there). time.monotonic
# and perf_counter are left alone so timeouts still behave.
# Covers time.time/time_ns/localtime/gmtime/ctime/strftime and datetime/date
# now/utcnow/today for code that looks them up after startup
# (`import datetime` or `from datetime import datetime` both work).
import os as _os

_target = _os.environ.get("FLAKE_FAKE_NOW_EPOCH")
if _target:
    import time as _time
    import datetime as _dt

    _off = float(_target) - _time.time()
    if _os.environ.get("FLAKE_FAKE_FREEZE") == "1":
        _frozen = float(_target)
        _rt = lambda: _frozen
    else:
        _real_time = _time.time
        _rt = lambda: _real_time() + _off
    _lt, _gt, _ct, _sf = _time.localtime, _time.gmtime, _time.ctime, _time.strftime

    _time.time = _rt
    _time.time_ns = lambda: int(_rt() * 1e9)
    _time.localtime = lambda secs=None: _lt(_rt() if secs is None else secs)
    _time.gmtime = lambda secs=None: _gt(_rt() if secs is None else secs)
    _time.ctime = lambda secs=None: _ct(_rt() if secs is None else secs)
    _time.strftime = lambda fmt, t=None: _sf(fmt, _lt(_rt()) if t is None else t)

    _real_date, _real_datetime = _dt.date, _dt.datetime

    class _DateMeta(type):
        def __instancecheck__(cls, obj):
            return isinstance(obj, _real_date)

        def __subclasscheck__(cls, sub):
            return issubclass(sub, _real_date)

    class _DatetimeMeta(type):
        def __instancecheck__(cls, obj):
            return isinstance(obj, _real_datetime)

        def __subclasscheck__(cls, sub):
            return issubclass(sub, _real_datetime)

    class FakeDate(_real_date, metaclass=_DateMeta):
        @classmethod
        def today(cls):
            return cls.fromtimestamp(_rt())

    class FakeDatetime(_real_datetime, metaclass=_DatetimeMeta):
        @classmethod
        def now(cls, tz=None):
            return cls.fromtimestamp(_rt(), tz)

        @classmethod
        def utcnow(cls):
            return cls.utcfromtimestamp(_rt())

        @classmethod
        def today(cls):
            return cls.fromtimestamp(_rt())

    _dt.date = FakeDate
    _dt.datetime = FakeDatetime
