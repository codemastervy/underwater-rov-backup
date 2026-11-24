"""Camera autopilot: looks for the line, target or drift in the camera picture and tells rov.py."""
import threading
import time

import rov as core

from .camera import HEIGHT, WIDTH


class VisionWorker(threading.Thread):
    def __init__(self, link, camera):
        super().__init__(daemon=True)
        self.link, self.camera = link, camera
        self.result = {}
        self.error = ""

    def run(self):
        try:
            import vision
        except ImportError as ex:         # numpy missing
            self.error = f"Camera autopilot unavailable: {ex}"
            return
        tracker, last = vision.HeadingTracker(), None
        while True:
            time.sleep(0.03)
            status, frame = self.link.status, self.camera.frame
            if status.ap_mode not in core.VISION_MODES:
                continue
            if frame and self.camera.fresh:
                if frame.number == last:
                    continue
                last = frame.number
                try:
                    result = self._look(vision, tracker, frame, status)
                except Exception as ex:   # never let one bad frame end the thread
                    result = {"found": False, "error": str(ex)}
            else:
                result = {"found": False}
                time.sleep(0.07)
            result.update(ts=time.time(), mode=status.ap_mode, epoch=status.ap_epoch)
            self.result = result
            self.link.send_vision(result)

    @staticmethod
    def _look(vision, tracker, frame, status):
        picture, settings = vision.to_array(frame.data, WIDTH, HEIGHT), status.ap_settings
        if status.ap_mode == "line":
            return vision.find_line(picture, settings.get("line_colour", "dark"))
        if status.ap_mode == "target":
            return vision.find_target(picture, settings.get("target_colour", "orange"), settings.get("target_pick"))
        return tracker.update(picture, status.ap_epoch)
