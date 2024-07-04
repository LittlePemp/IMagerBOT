import unittest
from scipy.spatial import KDTree
from imager.domain.image_builder.services.kdtree_service import KDTreeService
from imager.domain.image_builder.value_objects.color_definition import ColorDefinition
from imager.domain.image_builder.value_objects.cell_rgb import CellRgb
from imager.domain.image_builder.aggregates.cell import Cell
from imager.domain.image_builder.entities.cell_object import CellObject


class TestKDTreeService(unittest.TestCase):
    def setUp(self):
        r = ColorDefinition(100)
        g = ColorDefinition(150)
        b = ColorDefinition(200)

        rgb = CellRgb(r, g, b)
        cell = Cell(rgb, 'group1', 'path/to/image1.png')
        self.cell_object1 = CellObject(cell, image=None)

        r = ColorDefinition(50)
        g = ColorDefinition(75)
        b = ColorDefinition(100)

        rgb = CellRgb(r, g, b)
        cell = Cell(rgb, 'group1', 'path/to/image2.png')
        self.cell_object2 = CellObject(cell, image=None)

        self.data = {
            'group1': {
                'path/to/image1.png': self.cell_object1,
                'path/to/image2.png': self.cell_object2
            }
        }

    def test_build_trees(self):
        trees = KDTreeService.build_trees(self.data)
        self.assertIn('group1', trees)
        tree, cell_objects = trees['group1']
        self.assertIsInstance(tree, KDTree)
        self.assertEqual(len(cell_objects), 2)
        self.assertIn(self.cell_object1, cell_objects)
        self.assertIn(self.cell_object2, cell_objects)

    def test_find_closest(self):
        trees = KDTreeService.build_trees(self.data)
        tree, cell_objects = trees['group1']

        target_rgb = (100, 150, 200)
        distance, index = KDTreeService.find_closest(tree, target_rgb)
        closest_cell = cell_objects[index]
        self.assertEqual(closest_cell.cell.rgb.r.value, 100)
        self.assertEqual(closest_cell.cell.rgb.g.value, 150)
        self.assertEqual(closest_cell.cell.rgb.b.value, 200)

        target_rgb = (50, 75, 100)
        distance, index = KDTreeService.find_closest(tree, target_rgb)
        closest_cell = cell_objects[index]
        self.assertEqual(closest_cell.cell.rgb.r.value, 50)
        self.assertEqual(closest_cell.cell.rgb.g.value, 75)
        self.assertEqual(closest_cell.cell.rgb.b.value, 100)
