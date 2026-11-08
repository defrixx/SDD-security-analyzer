import unittest

from spec_enricher.inputs import InputError, parse
from spec_enricher.snapshots import Patch, PatchConflict, apply, sha256


class InputsTests(unittest.TestCase):
    def test_json_rejects_duplicate_nonfinite_but_document_refs_are_data(self):
        for data in (b'{"x":1,"x":2}', b'{"x":NaN}', b'{"x":1e999}'):
            with self.subTest(data=data), self.assertRaises(InputError):
                parse(data, "json")
        self.assertEqual(parse(b'{"a":[1,true,null]}', "json"), {"a": [1, True, None]})
        self.assertEqual(
            parse(b'{"$ref":"file:///etc/passwd","command":"nothing"}', "json")["command"],
            "nothing",
        )

    def test_yaml_rejects_aliases_tags_and_duplicates(self):
        for data in (b"a: &x 1\nb: *x", b"a: !!str 1", b"a: 1\na: 2"):
            with self.subTest(data=data), self.assertRaises(InputError):
                parse(data, "yaml")

    def test_depth_and_size_limits(self):
        with self.assertRaises(InputError):
            parse(("[" * 70 + "0" + "]" * 70).encode(), "json")
        with self.assertRaises(InputError):
            parse(b" " * (10 * 1024 * 1024 + 1), "json")

    def test_selected_patch_and_conflicts(self):
        original = "Привет, мир!".encode()
        patches = [Patch("one", 0, 6, "Привет", "Здравствуйте"), Patch("two", 8, 11, "мир", "все")]
        result = apply(original, sha256(original), patches, {"two"})
        self.assertEqual(result.decode(), "Привет, все!")
        self.assertEqual(original.decode(), "Привет, мир!")
        with self.assertRaises(PatchConflict):
            apply(original + b" ", sha256(original), patches, {"two"})
        with self.assertRaises(PatchConflict):
            apply(original, sha256(original), patches, {"missing"})


if __name__ == "__main__":
    unittest.main()
