import numpy as np

try:
    from utils.fast_postprocess import yolo_result2landmarks_array as _fast_impl
    HAS_FAST = True
except Exception:  # pragma: no cover - fallback when extension not built
    HAS_FAST = False


def yolo_result2landmarks(kpts, threshold: float = 0.5):
    """Convert keypoints to landmark tuples.

    Parameters
    ----------
    kpts : object
        Keypoints object from YOLO result.
    threshold : float, optional
        Confidence threshold, by default 0.5

    Returns
    -------
    list[tuple[int, int, float]]
        Filtered landmark list.
    """
    kpts_xy = kpts[0].xy.cpu().numpy()
    kpts_conf = kpts[0].conf.cpu().numpy()
    if HAS_FAST:
        return _fast_impl(kpts_xy, kpts_conf, threshold)

    num_kpts = kpts_xy.shape[1]
    return [
        (int(kpts_xy[0][kid][0]), int(kpts_xy[0][kid][1]),
         round(float(kpts_conf[0][kid]), 3))
        for kid in range(num_kpts) if kpts_conf[0][kid] >= threshold
    ]
