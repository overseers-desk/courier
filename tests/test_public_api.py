"""The curated public API: every name in courier.__all__ must import.

__all__ is the supported library surface; anything outside it is internal.
"""

import courier


def test_every_all_name_is_importable():
    missing = [name for name in courier.__all__ if not hasattr(courier, name)]
    assert missing == [], f"declared in __all__ but not importable: {missing}"


def test_star_import_matches_all():
    ns: dict = {}
    exec("from courier import *", ns)
    exported = {k for k in ns if not k.startswith("_")}
    assert exported == set(courier.__all__)
