import os
import shutil
from pathlib import Path
from uuid import uuid4

import pytest
from PySide6.QtWidgets import QApplication, QWidget

from tests.fakes import FakeMediaStore, FakePlayerWindow

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# Install lightweight global replacements for expensive runtime/UI dependencies
# used by older service tests.
from tests.support.global_stubs import install_global_stubs  # noqa: E402

install_global_stubs()


def pytest_configure():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def pytest_sessionstart(session):
    QApplication.instance() or QApplication([])


def pytest_runtest_teardown(item, nextitem):
    QApplication.processEvents()


def pytest_unconfigure(config):
    QApplication.processEvents()


def pytest_generate_tests(metafunc):
    return None


def pytest_report_header(config):
    return "Qt offscreen test harness enabled"


@pytest.fixture
def qt_parent():
    return QWidget()


@pytest.fixture
def fake_player_window():
    return FakePlayerWindow()


@pytest.fixture
def fake_media_store():
    return FakeMediaStore()


@pytest.fixture
def workspace_tmp_path():
    root = Path(__file__).resolve().parent / "_tmp"
    root.mkdir(exist_ok=True)
    case_dir = root / uuid4().hex
    case_dir.mkdir()
    try:
        yield case_dir
    finally:
        shutil.rmtree(case_dir, ignore_errors=True)
