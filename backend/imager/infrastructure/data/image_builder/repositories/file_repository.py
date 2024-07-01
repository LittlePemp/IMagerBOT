import os
import cv2
import numpy as np


class FileRepository:
    def read_image_file(self, relative_path: str) -> np.ndarray:
        image = cv2.imread(relative_path, cv2.IMREAD_UNCHANGED)
        if image is None:
            raise FileNotFoundError(f"Image not found at path: {relative_path}")
        return image

    def save_image_file(self, image: np.ndarray, relative_path: str) -> None:
        cv2.imwrite(relative_path, image)

    def list_image_files(self, directory_path: str) -> list[str]:
        supported_extensions = ('.png', '.jpg', '.jpeg', '.bmp', '.tiff')
        return [
            os.path.join(directory_path, f)
            for f in os.listdir(directory_path)
            if f.lower().endswith(supported_extensions)
        ]
