# cython: language_level=3
import numpy as np
cimport numpy as np

cdef int _collect_landmarks(float[:, :, :] kpts_xy, float[:, :] kpts_conf,
                            float threshold,
                            float[:, :] out_xy, float[:] out_conf) nogil:
    cdef int num_kpts = kpts_xy.shape[1]
    cdef int count = 0
    cdef int kid
    for kid in range(num_kpts):
        if kpts_conf[0, kid] >= threshold:
            out_xy[count, 0] = kpts_xy[0, kid, 0]
            out_xy[count, 1] = kpts_xy[0, kid, 1]
            out_conf[count] = kpts_conf[0, kid]
            count += 1
    return count


def yolo_result2landmarks_array(np.ndarray[np.float32_t, ndim=3] kpts_xy not None,
                                np.ndarray[np.float32_t, ndim=2] kpts_conf not None,
                                float threshold=0.5):
    cdef int num_kpts = kpts_xy.shape[1]
    cdef np.ndarray[np.float32_t, ndim=2] out_xy = np.empty((num_kpts, 2), dtype=np.float32)
    cdef np.ndarray[np.float32_t, ndim=1] out_conf = np.empty(num_kpts, dtype=np.float32)
    cdef int count

    count = _collect_landmarks(kpts_xy, kpts_conf, threshold, out_xy, out_conf)

    cdef list result = []
    cdef int i
    for i in range(count):
        result.append((<int>out_xy[i, 0], <int>out_xy[i, 1],
                       round(float(out_conf[i]), 3)))
    return result
