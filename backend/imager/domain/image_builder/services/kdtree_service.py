from scipy.spatial import KDTree


class KDTreeService:
    @staticmethod
    def build_trees(data):
        trees = {}
        for group, cell_infos in data.items():
            points = []
            cell_objects = []
            for cell_obj in cell_infos.values():
                rgb = (cell_obj.cell.rgb.r.value, cell_obj.cell.rgb.g.value, cell_obj.cell.rgb.b.value)
                points.append(rgb)
                cell_objects.append(cell_obj)
            if points:
                trees[group] = (KDTree(points), cell_objects)
        return trees

    @staticmethod
    def find_closest(tree: KDTree, target: tuple[int, int, int]) -> tuple:
        distance, index = tree.query(target)
        return distance, index
