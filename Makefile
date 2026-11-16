.PHONY: lint test test-debian12 test-debian13 test-ubuntu2004 test-ubuntu2204 test-ubuntu2404 test-all test-forwards test-keep test-reset clean distclean

VENV := .venv
BIN := $(VENV)/bin
export PATH := $(CURDIR)/$(BIN):$(PATH)

$(VENV): requirements-dev.txt
	python3 -m venv $(VENV)
	$(BIN)/pip install --upgrade pip
	$(BIN)/pip install -r requirements-dev.txt
	@touch $(VENV)

# ansible-lint skips molecule/ and .github/ (excluded in .ansible-lint);
# yamllint still covers them, so a malformed scenario play fails here rather
# than at the end of a full matrix run.
lint: $(VENV)
	$(BIN)/yamllint -c .yamllint \
		defaults tasks vars meta handlers templates molecule .github
	$(BIN)/ansible-lint -c .ansible-lint defaults tasks vars meta handlers

test: lint test-all test-forwards

test-debian12: $(VENV)
	MOLECULE_OS=debian MOLECULE_VERSION=12 \
		MOLECULE_IMAGE=geerlingguy/docker-debian12-ansible:latest \
		$(BIN)/molecule test

test-debian13: $(VENV)
	MOLECULE_OS=debian MOLECULE_VERSION=13 \
		MOLECULE_IMAGE=geerlingguy/docker-debian13-ansible:latest \
		$(BIN)/molecule test

test-ubuntu2004: $(VENV)
	MOLECULE_OS=ubuntu MOLECULE_VERSION=2004 \
		MOLECULE_IMAGE=geerlingguy/docker-ubuntu2004-ansible:latest \
		$(BIN)/molecule test

test-ubuntu2204: $(VENV)
	MOLECULE_OS=ubuntu MOLECULE_VERSION=2204 \
		MOLECULE_IMAGE=geerlingguy/docker-ubuntu2204-ansible:latest \
		$(BIN)/molecule test

test-ubuntu2404: $(VENV)
	MOLECULE_OS=ubuntu MOLECULE_VERSION=2404 \
		MOLECULE_IMAGE=geerlingguy/docker-ubuntu2404-ansible:latest \
		$(BIN)/molecule test

# Keep this list identical to the matrix in .github/workflows/ci.yml, or
# `make test-all` passes while CI fails on a target nobody runs locally.
test-all: test-debian12 test-debian13 test-ubuntu2004 test-ubuntu2204 test-ubuntu2404

# Forward-zone lifecycle: add zones, prove they resolve, remove them, prove
# they stop. One image, because it exercises role logic rather than OS
# variance; separate from the default scenario, because removing and
# re-adding a file changes on every run and would fail idempotence.
test-forwards: $(VENV)
	MOLECULE_OS=ubuntu MOLECULE_VERSION=2404 \
		MOLECULE_IMAGE=geerlingguy/docker-ubuntu2404-ansible:latest \
		$(BIN)/molecule test -s forwards

# Converge only, leaving the container up for interactive diagnosis.
# `molecule test` destroys on both success and failure, which is correct
# for CI and useless when a resolver is answering the wrong thing.
test-keep: $(VENV)
	MOLECULE_OS=ubuntu MOLECULE_VERSION=2404 \
		MOLECULE_IMAGE=geerlingguy/docker-ubuntu2404-ansible:latest \
		$(BIN)/molecule converge

# An interrupted run can leave a scenario's ephemeral directory without its
# molecule.yml, and the next `create` then fails on a missing-file lookup
# that reads like a scenario bug.
test-reset: $(VENV)
	$(BIN)/molecule reset

# Every image triple, not just the default: the platform name in
# molecule.yml interpolates MOLECULE_OS/MOLECULE_VERSION, so a bare
# `molecule destroy` matches nothing after a run on another target and
# leaves a privileged container behind.
CLEAN_TRIPLES := \
	debian:12:debian12 \
	debian:13:debian13 \
	ubuntu:2004:ubuntu2004 \
	ubuntu:2204:ubuntu2204 \
	ubuntu:2404:ubuntu2404

clean:
	@for t in $(CLEAN_TRIPLES); do \
		os=$${t%%:*}; rest=$${t#*:}; ver=$${rest%%:*}; img=$${rest#*:}; \
		MOLECULE_OS=$$os MOLECULE_VERSION=$$ver \
			MOLECULE_IMAGE=geerlingguy/docker-$$img-ansible:latest \
			$(BIN)/molecule destroy 2>/dev/null || true; \
		MOLECULE_OS=$$os MOLECULE_VERSION=$$ver \
			MOLECULE_IMAGE=geerlingguy/docker-$$img-ansible:latest \
			$(BIN)/molecule destroy -s forwards 2>/dev/null || true; \
	done

distclean: clean
	rm -rf $(VENV)
