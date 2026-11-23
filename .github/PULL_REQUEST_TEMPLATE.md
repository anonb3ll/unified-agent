## Summary

<!-- What changes, and why. Link the issue it closes. -->

## Verification

<!-- What you ran and what it returned. Targeted tests are enough locally;
     CI runs the full suite. -->

- [ ] `uv run pytest tests/test_<area>.py -q`
- [ ] `uv run ruff check . && uv run ruff format --check .`
- [ ] A fix for a safety-boundary or security defect carries the regression
      test that fails without it.

## Notes for the reviewer

<!-- Migration, rollout, approval-gate, or evidence implications. -->
