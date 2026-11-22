.PHONY: smoke clean-install-smoke anonymity-check

# Live smoke suite (System Leverage Plan T1-6): unmocked checks against the
# real installed venv, catching the class of bug a fully-mocked unit suite
# can miss (see scripts/smoke.py's module docstring for the task #104/#110
# motivation). Run after `uv sync` and before a hub deploy (cockpit
# docs/runbooks/DEPLOY-STEWARD.md).
smoke:
	uv run python scripts/smoke.py

# Install this checkout into a throwaway HOME as a stranger would, then require
# a clean doctor and an operator token that resolves to the operator profile.
clean-install-smoke:
	scripts/clean_install_smoke.sh

# Fail on personal identifiers (names, hosts, paths) beyond the tracked baseline.
# The public export drops scripts/public_release/ (it names what it scrubs), so a
# public tree says so instead of failing on a missing file.
anonymity-check:
	@if [ -f scripts/public_release/check_anonymity.py ]; then \
		uv run python scripts/public_release/check_anonymity.py; \
	else \
		echo "anonymity-check: skipped (scripts/public_release/ is not part of this tree)"; \
	fi
