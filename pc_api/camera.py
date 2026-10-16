"""摄像头拍照封装（OpenCV）。

MVP 策略：点击拍照时打开摄像头抓一帧保存。不做实时预览以保持简单。
摄像头不可用时抛 RuntimeError，GUI 层降级为"选择图片文件"。
"""

from pathlib import Path


class CameraCapture:
    """OpenCV 摄像头拍照。"""

    def __init__(self, camera_index: int = 0):
        self.camera_index = camera_index

    def capture(self, output_path: str) -> str:
        """打开摄像头，抓一帧保存到 output_path。

        Raises:
            RuntimeError: 摄像头无法打开或读取失败
            ImportError: 未安装 opencv-python
        """
        import cv2  # 延迟导入

        cap = cv2.VideoCapture(self.camera_index)
        if not cap.isOpened():
            raise RuntimeError(f"无法打开摄像头 (index={self.camera_index})")

        try:
            # 预热：丢弃前几帧让自动曝光稳定
            for _ in range(10):
                cap.read()
            ret, frame = cap.read()
            if not ret or frame is None:
                raise RuntimeError("摄像头读取失败")
            Path(output_path).parent.mkdir(parents=True, exist_ok=True)
            ok = cv2.imwrite(output_path, frame)
            if not ok:
                raise RuntimeError(f"图片保存失败: {output_path}")
            return output_path
        finally:
            cap.release()

    @staticmethod
    def available(camera_index: int = 0) -> bool:
        """检查摄像头是否可用（不抛异常）。"""
        try:
            import cv2
            cap = cv2.VideoCapture(camera_index)
            ok = cap.isOpened()
            cap.release()
            return ok
        except Exception:
            return False
