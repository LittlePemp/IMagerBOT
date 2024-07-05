import unittest
from src.domain.image_builder.value_objects.color_definition import ColorDefinition


class TestColorDefinition(unittest.TestCase):

    def test_color_definition_create_valid(self):
        result = ColorDefinition.create(128)
        self.assertTrue(result.is_success)
        self.assertEqual(result.value.value, 128)

    def test_color_definition_create_invalid_non_integer(self):
        result = ColorDefinition.create('not_an_int')
        self.assertFalse(result.is_success)
        self.assertEqual(result.error, 'RGB must be an integer')

    def test_color_definition_create_invalid_out_of_range(self):
        result = ColorDefinition.create(300)
        self.assertFalse(result.is_success)
        self.assertEqual(result.error, 'RGB must be in [0..255]')

    def test_color_definition_comparison(self):
        color1 = ColorDefinition.create(100).value
        color2 = ColorDefinition.create(150).value
        self.assertTrue(color1 < color2)
        self.assertTrue(color2 > color1)
        self.assertNotEqual(color1, color2)
        self.assertEqual(color1, ColorDefinition.create(100).value)

    def test_color_definition_str(self):
        color = ColorDefinition.create(200).value
        self.assertEqual(str(color), '200')

    def test_color_definition_int(self):
        color = ColorDefinition.create(50).value
        self.assertEqual(int(color), 50)

    def test_color_definition_equality(self):
        color1 = ColorDefinition.create(50).value
        color2 = ColorDefinition.create(50).value
        self.assertEqual(color1, color2)

    def test_color_definition_inequality(self):
        color1 = ColorDefinition.create(50).value
        color2 = ColorDefinition.create(60).value
        self.assertNotEqual(color1, color2)
