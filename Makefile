# Build, test and package helpers for panasonic-posture-manager.
#
# The Python package lives under src/ and is built into a wheel by
# pyproject.toml.  The C relay and the gettext catalog are built here; the
# AUR PKGBUILD calls `make` and then builds the wheel.

PYTHON ?= /usr/bin/python3
CC ?= cc
MSG_FMT ?= msgfmt
PACKAGE ?= $(firstword \
	$(wildcard packaging/aur/panasonic-posture-manager-[0-9]*.pkg.tar.zst) \
	$(wildcard panasonic-posture-manager-[0-9]*.pkg.tar.zst))
PREVIOUS_PACKAGE ?=
PACKAGE_NO_INSTALL ?= 0
PACKAGE_CHECK_ARGS = \
	$(if $(PREVIOUS_PACKAGE),--previous "$(PREVIOUS_PACKAGE)",) \
	$(if $(filter 1,$(PACKAGE_NO_INSTALL)),--no-install,)

# Distribution build flags (makepkg exports CFLAGS/LDFLAGS) are preserved:
# the project only appends its own required flags instead of replacing them.
CFLAGS ?= -O2
CPPFLAGS ?=
LDFLAGS ?=
override CFLAGS += -std=c11 -D_DEFAULT_SOURCE -Wall -Wextra
ifeq ($(WERROR),1)
override CFLAGS += -Werror
endif

RELAY_SOURCES := $(wildcard backend/*.c)
RELAY_HEADERS := $(wildcard backend/*.h)
MO := locale/zh_CN/LC_MESSAGES/panasonic-posture-manager.mo

.PHONY: all check check-c check-compile check-unit check-dbus check-i18n check-package \
	package-deb package-rpm clean

all: backend/relay $(MO)

backend/relay: $(RELAY_SOURCES) $(RELAY_HEADERS)
	$(CC) $(CPPFLAGS) $(CFLAGS) $(RELAY_SOURCES) $(LDFLAGS) -o $@

$(MO): po/zh_CN.po
	mkdir -p $(@D)
	$(MSG_FMT) --check $< -o $@

check: all check-c check-compile check-unit check-dbus check-i18n

# Compile the relay with warnings as errors and run its host-side protocol
# tests.  These never open /dev/uinput or /dev/input; real device behavior is
# covered by tests/manual on the target machine.
check-c:
	mkdir -p build
	$(CC) $(CPPFLAGS) $(CFLAGS) -Werror backend/tests/relay_test.c \
		backend/input.c backend/switch.c backend/uinput.c $(LDFLAGS) \
		-o build/relay-test
	./build/relay-test

check-compile:
	PYTHONPATH=src $(PYTHON) -m compileall -q src

check-unit:
	PYTHONPATH=src $(PYTHON) -m unittest discover -s tests -p 'test_*.py' -v

check-dbus:
	dbus-run-session -- env PYTHONPATH=src $(PYTHON) tests/integration/check_dbus.py
	dbus-run-session -- env PYTHONPATH=src $(PYTHON) tests/integration/check_agent.py

check-i18n:
	PYTHONPATH=src $(PYTHON) tests/check_i18n.py

# Validate a built AUR package without touching the host installation.  Set
# PREVIOUS_PACKAGE to an older package to exercise an actual upgrade.
check-package:
	@test -n "$(PACKAGE)" || { \
		echo "usage: make check-package PACKAGE=/path/to/package.pkg.tar.zst" >&2; \
		echo "       [PREVIOUS_PACKAGE=/path/to/old.pkg.tar.zst] [PACKAGE_NO_INSTALL=1]" >&2; \
		exit 2; \
	}
	PYTHONPATH=src $(PYTHON) tests/package/check_package.py \
		--package "$(PACKAGE)" $(PACKAGE_CHECK_ARGS)

# These targets require the matching distribution toolchain (dpkg-deb or
# rpmbuild).  The release workflow runs them in Ubuntu and Fedora containers.
package-deb:
	bash packaging/scripts/build-deb.sh

package-rpm:
	bash packaging/scripts/build-rpm.sh

clean:
	rm -f backend/relay
	rm -f backend/*.gch
	rm -rf locale dist build src/*.egg-info
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
