from scipy.spatial import KDTree

from src.shared_kernel.loggers import exception_logger
from src.shared_kernel.result import Result


class KDTreeService:
    @staticmethod
    def build_trees(data) -> Result:
        try:
            trees = {}
            for group, cell_infos in data.items():
                points = []
                cell_objects = []
                for cell_obj in cell_infos.values():
                    rgb = (cell_obj.cell.rgb.r.value,
                           cell_obj.cell.rgb.g.value,
                           cell_obj.cell.rgb.b.value)
                    points.append(rgb)
                    cell_objects.append(cell_obj)
                if points:
                    trees[group] = (KDTree(points), cell_objects)
            return Result.Success(trees)
        except Exception as e:
            exception_logger.error(f'Build trees error {e}')
            return Result.Error('Build trees error')

    @staticmethod
    def find_closest(tree: KDTree, target: tuple[int, int, int]) -> Result:
        try:
            distance, index = tree.query(target)
            return Result.Success((distance, index))
        except Exception as e:
            exception_logger.error(f'Find closest cell error: {e}')
            return Result.Error('Find closest cell error')
