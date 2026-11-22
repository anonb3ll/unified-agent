- **Targeted test runs include the component-map and test-budget gates.**
  A change anywhere can add an undeclared import or an oversized test file
  without touching what those tests import. Every affected-test selection now
  runs `tests/test_component_check.py` and `tests/test_test_budget.py`.
