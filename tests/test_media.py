import pytest

from learner import media, store

PNG = b"\x89PNG\r\n\x1a\n" + b"fake image body"
JPEG = b"\xff\xd8\xff\xe0" + b"jpeg body"
WEBP = b"RIFF\x10\x00\x00\x00WEBPVP8 " + b"body"


@pytest.fixture
def conn(tmp_path):
    c = store.connect(tmp_path / "m.db")
    store.migrate(c)
    yield c
    c.close()


def test_types_are_detected_from_the_bytes_not_the_name():
    assert media.detect_mime(PNG) == "image/png"
    assert media.detect_mime(JPEG) == "image/jpeg"
    assert media.detect_mime(WEBP) == "image/webp"
    assert media.detect_mime(b"<svg xmlns=...>") is None


def test_same_image_is_stored_once(conn):
    a = media.save_image(conn, PNG, "a.png", "chart")
    b = media.save_image(conn, PNG, "copy.png", "same chart")
    assert a.sha256 == b.sha256 and conn.execute("SELECT COUNT(*) FROM media").fetchone()[0] == 1
    assert media.get_image(conn, a.sha256).data == PNG


@pytest.mark.parametrize("data, message", [(b"", "empty"), (b"GIF87a" + b"x" * 100, "limit"), (b"not an image", "Only JPEG")])
def test_rejects_bad_images(conn, data, message):
    with pytest.raises(media.MediaError, match=message):
        media.save_image(conn, data, max_bytes=50)


def test_referenced_images_in_order_without_repeats(conn):
    a, b = media.save_image(conn, PNG), media.save_image(conn, JPEG)
    text = f"See ![x]({a.url}) and ![y]({b.url}) and again ![x]({a.url})"
    assert media.referenced(text) == [a.sha256, b.sha256]
    assert [i.mime for i in media.images_for(conn, text, "no images here")] == ["image/png", "image/jpeg"]


def test_export_round_trip_checks_the_fingerprint(conn):
    image = media.save_image(conn, PNG)
    exported = media.to_export(image)
    assert media.decode_export(image.sha256, exported) == PNG
    with pytest.raises(media.MediaError, match="fingerprint"):
        media.decode_export("0" * 64, exported)


def test_groups_allow_the_same_name_twice(conn):
    a = store.save_collection(conn, "Basics", "mysql", {}, group="SQL")
    b = store.save_collection(conn, "Basics", "mysql", {}, group="Python")
    assert a != b and store.find_collection(conn, "Basics", "Python").id == b
    assert store.list_groups(conn) == ["Python", "SQL"]
    assert store.get_collection(conn, a).display_name == "SQL › Basics"
    import sqlite3
    with pytest.raises(sqlite3.IntegrityError):
        store.save_collection(conn, "Basics", "mysql", {}, group="SQL")
