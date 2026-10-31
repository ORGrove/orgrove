import unittest

from hello.greet import greet


class GreetTest(unittest.TestCase):
    def test_greets_by_name(self):
        self.assertEqual(greet("Ada"), "Hello, Ada!")

    def test_empty_name_greets_the_world(self):
        self.assertEqual(greet("  "), "Hello, world!")
