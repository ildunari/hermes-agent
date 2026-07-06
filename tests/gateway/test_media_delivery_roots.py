from pathlib import Path

from gateway.platforms import base


def test_media_delivery_roots_include_new_and_legacy_image_cache():
    roots = {Path(root) for root in base.MEDIA_DELIVERY_SAFE_ROOTS}

    assert base._HERMES_HOME / "cache" / "images" in roots
    assert base._HERMES_HOME / "image_cache" in roots


def test_generated_cache_images_remain_allowed_after_recency_window(tmp_path, monkeypatch):
    hermes_home = tmp_path / "hermes-home"
    image_path = hermes_home / "cache" / "images" / "generated.png"
    image_path.parent.mkdir(parents=True)
    image_path.write_bytes(b"png")

    monkeypatch.setattr(base, "MEDIA_DELIVERY_SAFE_ROOTS", (hermes_home / "cache" / "images",))
    monkeypatch.setenv(base.MEDIA_DELIVERY_TRUST_RECENT_ENV, "0")

    assert base.validate_media_delivery_path(str(image_path)) == str(image_path.resolve())