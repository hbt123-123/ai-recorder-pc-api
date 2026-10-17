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
            # 用 imencode + write_bytes 而非 cv2.imwrite：OpenCV 的 imwrite 在
            # Windows 上走 ANSI 文件 API，路径含非 ASCII 字符（中文用户名 / 中文
            # 安装目录，打包产物极易出现）时会失败或写出乱码文件名（已实测复现）。
            # imencode 只管内存编码，磁盘写入交给 Python 处理 Unicode 路径。
            ok, buf = cv2.imencode(".jpg", frame)
            if not ok:
                raise RuntimeError(f"图片编码失败: {output_path}")
            Path(output_path).write_bytes(buf.tobytes())
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
