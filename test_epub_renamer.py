import unittest

from epub_renamer import reduce_batch


class ReduceBatchTests(unittest.TestCase):
    def test_mixed_series_batch_keeps_series_apart(self):
        names = reduce_batch([
            "Mushoku Tensei - Jobless Reincarnation Volume-11.epub",
            "Mushoku Tensei - Jobless Reincarnation Volume-2.epub",
            "Mushoku Tensei - Redundant Reincarnation vol 2.epub",
            "Mushoku Tensei - Redundant Reincarnation vol 2.1 [BOOK☆WALKER Exclusive Short Story].epub",
        ])
        self.assertEqual(sorted(names.values()), [
            "v02 - MTJR.epub",
            "v02 - MTRR.epub",
            "v02.1 - MTRR.epub",
            "v11 - MTJR.epub",
        ])

    def test_lone_volume_gets_same_name_as_in_a_larger_batch(self):
        name = "Mushoku Tensei - Jobless Reincarnation Volume-11.epub"
        self.assertEqual(reduce_batch([name])[name], "v11 - MTJR.epub")

    def test_no_volume_means_no_short_name(self):
        self.assertEqual(reduce_batch(["Some Random Book.epub"]), {"Some Random Book.epub": None})


if __name__ == "__main__":
    unittest.main()
