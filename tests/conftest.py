"""A CapCut-authored draft to compile against.

Every draft eyecut writes needs one: `capcut compile`'s bundled template declares
CapCut 6.5.0, and CapCut 9.x refuses to open anything built from it. The fixture
here is a real empty project captured from CapCut 9.1.0 -- device ids, disk id and
MAC address scrubbed, timeline already empty.
"""
import shutil
from pathlib import Path

import pytest

CAPTURED = Path(__file__).parent / "fixtures" / "native_template"


@pytest.fixture
def drafts_dir(tmp_path):
    """A drafts store holding one empty CapCut-authored project, as a real one does."""
    store = tmp_path / "com.lveditor.draft"
    shutil.copytree(CAPTURED, store / "empty_project")
    return store
