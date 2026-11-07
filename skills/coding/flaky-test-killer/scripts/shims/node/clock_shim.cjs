// Clock shim loaded by stress.py through NODE_OPTIONS=--require.
// Moves Date to FLAKE_FAKE_NOW_EPOCH (seconds); the clock keeps ticking from
// there unless FLAKE_FAKE_FREEZE=1. performance.now and timers are untouched.
'use strict';
const target = process.env.FLAKE_FAKE_NOW_EPOCH;
if (target) {
  const RealDate = Date;
  const realNow = RealDate.now.bind(RealDate);
  const freeze = process.env.FLAKE_FAKE_FREEZE === '1';
  const targetMs = Number(target) * 1000;
  const offset = targetMs - realNow();
  const now = freeze ? () => targetMs : () => realNow() + offset;

  function FakeDate(...args) {
    if (!new.target) return new RealDate(now()).toString();
    if (args.length === 0) return Reflect.construct(RealDate, [now()], new.target);
    return Reflect.construct(RealDate, args, new.target);
  }
  FakeDate.prototype = RealDate.prototype;
  Object.setPrototypeOf(FakeDate, RealDate);
  FakeDate.now = now;
  FakeDate.parse = RealDate.parse;
  FakeDate.UTC = RealDate.UTC;
  globalThis.Date = FakeDate;
}
