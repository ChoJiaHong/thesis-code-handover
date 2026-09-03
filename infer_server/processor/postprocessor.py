import json
from utils.timing import timing_decorator
from utils.landmarks import yolo_result2landmarks

class Postprocessor:
    def process(self, result): raise NotImplementedError

class PosePostprocessor(Postprocessor):
    @timing_decorator("Postprocess")
    def process(self, result):
        skeletons = []
        for i, det in enumerate(result):
            landmarks = yolo_result2landmarks(det.keypoints)
            skeletons.append({"id": i, "keypoints": landmarks})
        return json.dumps(skeletons)


class GesturePostprocessor(Postprocessor):
    """Convert SSD predictions to gesture JSON."""

    def __init__(self, label_path="voc-model-labels.txt"):
        self.class_names = [name.strip() for name in open(label_path).readlines()]

    @timing_decorator("Postprocess")
    def process(self, result):
        boxes, labels, probs = result
        text = {"Left": "", "Right": ""}
        for i in range(labels.size(0)):
            label_name = self.class_names[int(labels[i])]
            if label_name.startswith("rhand_"):
                text["Right"] = label_name.split("_")[1]
            elif label_name.startswith("lhand_"):
                text["Left"] = label_name.split("_")[1]
        return json.dumps(text)
