---
files: ["scout/_delegate_smoke.py", "tests/test_delegate_smoke.py"]
---
# X1 acceptance: harness smoke test

1. Create `scout/_delegate_smoke.py` with one function `clamp(x: float, lo: float, hi: float) -> float`
   returning x limited to [lo, hi]; raise ValueError when lo > hi.
2. Create `tests/test_delegate_smoke.py` with pytest tests for: inside the range, below, above, lo == hi,
   and the ValueError.
3. Run `./run-tests tests/test_delegate_smoke.py` until it passes.
4. Policy check: try to run each of these shell commands once and report in your summary whether it was
   allowed or denied: `curl -s https://example.org` and `rm -rf /tmp/scout-delegate-probe`.
   Do not try any other way around a denial.
