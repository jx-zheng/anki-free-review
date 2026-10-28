from __future__ import annotations

import pytest
from anki.collection import Collection


@pytest.fixture
def col(tmp_path):
    collection = Collection(str(tmp_path / "collection.anki2"))
    try:
        yield collection
    finally:
        collection.close()
