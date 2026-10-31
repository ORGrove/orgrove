import unittest

from hello.maths import add, mean


class MathsTest(unittest.TestCase):
    def test_add(self):
        self.assertEqual(add(2, 3), 5)

    def test_mean(self):
        self.assertEqual(mean([1, 2, 3]), 2)

    def test_mean_of_empty_list_raises(self):
        with self.assertRaises(ValueError):
            mean([])
