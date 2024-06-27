class KDNode:
    def __init__(
        self, point: tuple[int, int, int], value, left=None, right=None
    ):
        self.point = point
        self.value = value
        self.left = left
        self.right = right
