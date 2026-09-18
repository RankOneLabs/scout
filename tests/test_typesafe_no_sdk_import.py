import sys


def test_package_does_not_import_typesafe_sdk() -> None:
    import scout.typesafe  # noqa: F401

    assert not any(
        name == "typesafe_sdk" or name.startswith("typesafe_sdk.") for name in sys.modules
    )
    assert not any(name == "typesafe_ai" or name.startswith("typesafe_ai.") for name in sys.modules)
