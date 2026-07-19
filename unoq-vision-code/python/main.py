import threading
import time
from datetime import datetime
from pathlib import Path

from arduino.app_utils import App, Bridge
from arduino.app_peripherals.camera import Camera
from arduino.app_bricks.video_objectdetection import VideoObjectDetection


FRAME_WIDTH = 320
FRAME_HEIGHT = 240
CENTER_X = FRAME_WIDTH / 2

CAMERA_HORIZONTAL_FOV = 60
IGNORE_ANGLE = 5

LOCK_AFTER_SECONDS = 10
SAVE_EVERY_SECONDS = 2


# Save beside the python/ folder.
python_folder = Path(__file__).resolve().parent

if python_folder.name == "python":
    project_folder = python_folder.parent
else:
    project_folder = python_folder

SAVE_FOLDER = project_folder / "saved_faces"
SAVE_FOLDER.mkdir(parents=True, exist_ok=True)

print(f"SAVE FOLDER: {SAVE_FOLDER.resolve()}", flush=True)


camera = Camera(
    "usb:0",
    resolution=(FRAME_WIDTH, FRAME_HEIGHT),
    fps=10,
)

detector = VideoObjectDetection(
    camera=camera,
    confidence=0.35,
    debounce_sec=0.0,

    # Required to receive JPEG frames.
    camera_preview=True,
)


acquisition_started = None
last_periodic_save = 0

candidate_box = None
candidate_label = None
candidate_frame = None

target_box = None
target_label = None
target_locked = False

last_sent_direction = None


def send_cart_direction(direction):
    global last_sent_direction

    # Avoid repeatedly sending the same command.
    if direction == last_sent_direction:
        return

    try:
        Bridge.call(
            "set_cart_direction",
            direction,
        )

        last_sent_direction = direction

        print(
            f"CART COMMAND SENT: {direction}",
            flush=True,
        )

    except Exception as error:
        print(
            f"CART COMMAND FAILED: {error}",
            flush=True,
        )


def area(box):
    x1, y1, x2, y2 = box

    return (
        max(0, x2 - x1)
        * max(0, y2 - y1)
    )


def iou(box1, box2):
    x1 = max(box1[0], box2[0])
    y1 = max(box1[1], box2[1])
    x2 = min(box1[2], box2[2])
    y2 = min(box1[3], box2[3])

    intersection = (
        max(0, x2 - x1)
        * max(0, y2 - y1)
    )

    union = area(box1) + area(box2) - intersection

    return intersection / union if union > 0 else 0


def save_jpeg(frame, prefix):
    if not frame:
        print(f"SAVE WAITING ({prefix}): no preview yet", flush=True)
        return False

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    filename = SAVE_FOLDER / f"{prefix}_{timestamp}.jpg"

    try:
        # The callback frame is already JPEG-encoded bytes.
        filename.write_bytes(frame)

        print(
            f"IMAGE SAVED: {filename.resolve()} "
            f"({filename.stat().st_size} bytes)",
            flush=True,
        )

        return True

    except Exception as error:
        print(f"SAVE FAILED: {error}", flush=True)
        return False


def find_targets(detections):
    targets = []

    for label, objects in detections.items():
        label = label.lower()

        if label not in ("person", "face"):
            continue

        for obj in objects:
            targets.append(
                (
                    label,
                    obj["bounding_box_xyxy"],
                )
            )

    return targets


def choose_target(targets, old_box=None):
    # Prefer a face whenever one is available.
    faces = [
        target
        for target in targets
        if target[0] == "face"
    ]

    choices = faces if faces else targets

    if old_box is None:
        return max(
            choices,
            key=lambda target: area(target[1]),
        )

    return max(
        choices,
        key=lambda target: iou(
            old_box,
            target[1],
        ),
    )


def print_direction(label, box, overlap):
    center_x = (box[0] + box[2]) / 2
    pixel_error = center_x - CENTER_X

    angle = (
        pixel_error / (FRAME_WIDTH / 2)
    ) * (CAMERA_HORIZONTAL_FOV / 2)

    if angle < -IGNORE_ANGLE:
        direction = "LEFT"
    elif angle > IGNORE_ANGLE:
        direction = "RIGHT"
    else:
        direction = "CENTER"

    print(
        f"{label.upper()} | "
        f"IoU={overlap:.2f} | "
        f"angle={angle:+.1f}° | "
        f"direction={direction}",
        flush=True,
    )
    
    send_cart_direction(direction)


def on_detections(detections, frame=None):
    global acquisition_started

    global candidate_box
    global candidate_label
    global candidate_frame

    global target_box
    global target_label
    global target_locked

    now = time.monotonic()

    if acquisition_started is None:
        acquisition_started = now
        print("10-SECOND ACQUISITION STARTED", flush=True)

    targets = find_targets(detections)

    # During the first 10 seconds, remember the latest target.
    if not target_locked:
        if targets:
            candidate_label, candidate_box = choose_target(
                targets,
                candidate_box,
            )

            if frame:
                candidate_frame = frame

            elapsed = now - acquisition_started

            print(
                f"ACQUIRING {candidate_label.upper()} | "
                f"{elapsed:.1f}/{LOCK_AFTER_SECONDS} seconds",
                flush=True,
            )

        return

    # Track after the target has been locked.
    if targets:
        old_box = target_box

        target_label, target_box = choose_target(
            targets,
            None if target_label == "last_frame" else target_box,
        )

        overlap = iou(old_box, target_box)

        print_direction(
            target_label,
            target_box,
            overlap,
        )


def background_worker():
    global acquisition_started
    global last_periodic_save

    global target_box
    global target_label
    global target_locked

    while True:
        time.sleep(0.1)

        now = time.monotonic()

        # App Lab keeps the latest preview frame internally.
        frame = detector._decode_preview_frame()

        if frame and acquisition_started is None:
            acquisition_started = now
            print("10-SECOND ACQUISITION STARTED", flush=True)

        # Save every two seconds, even without a detection.
        if now - last_periodic_save >= SAVE_EVERY_SECONDS:
            if save_jpeg(frame, "periodic"):
                last_periodic_save = now
            else:
                # Avoid printing failures every 0.1 seconds.
                last_periodic_save = now

        if acquisition_started is None or target_locked:
            continue

        elapsed = now - acquisition_started

        if elapsed < LOCK_AFTER_SECONDS:
            continue

        target_locked = True

        if candidate_box is not None:
            target_box = candidate_box
            target_label = candidate_label

            print(
                f"TARGET LOCKED: {target_label.upper()}",
                flush=True,
            )

            lock_frame = candidate_frame or frame

            save_jpeg(
                lock_frame,
                f"LOCKED_{target_label}",
            )

            print_direction(
                target_label,
                target_box,
                1.0,
            )

        else:
            # No person/face was found during acquisition.
            # Use the entire last frame as a centered fallback target.
            target_box = (
                0,
                0,
                FRAME_WIDTH,
                FRAME_HEIGHT,
            )

            target_label = "last_frame"

            print(
                "NO PERSON/FACE FOUND — LAST FRAME LOCKED",
                flush=True,
            )

            save_jpeg(
                frame,
                "LOCKED_LAST_FRAME",
            )

            print_direction(
                target_label,
                target_box,
                1.0,
            )


detector.on_detect_all(on_detections)

threading.Thread(
    target=background_worker,
    daemon=True,
).start()

print("Waiting for camera preview...", flush=True)

App.run()