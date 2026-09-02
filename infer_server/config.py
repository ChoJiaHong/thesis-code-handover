from pydantic.v1 import BaseSettings
import numpy as np


class Settings(BaseSettings):
    source: str = '0'  # 攝影機裝置
    device: str = '0'  # device arugments 使用GPU填裝置索引值'0'  使用CPU填'cpu'
    weights: str = 'yolov8n-pose.pt' # 'yolov8m-pose.pt' 'yolov8n-pose.pt' 'yolov8s-pose.pt'

    gRPC_port: str = '50052'

    # 相機影像初始設定
    cap_img_width: int = 1280  # 從source取得的影像寬度
    cap_img_height: int = 720  # 從source取得的影像高度

    # 影像前處理設定
    img_resize_w: int = 1280  # 預設 1280
    img_resize_h: int = 720  # 預設 720

    # 演篹法參數
    conf_thres: float = 0.80
    iou_thres: float = 0.65
    # classes: int = None

    # 除錯用設定
    is_debug: bool = True
    view_img: bool = True  # display results
    out_raw_video_name: str = 'raw'
    out_keypoint_video_name: str = 'keypoint'
    save_txt: bool = False   # 目前沒有用到
    save_txt_name: str = 'Logs'
    save_conf: bool = True  # save confidence in txt writing (--save-txt labels) 目前沒用到
    no_trace: bool = False   # 目前沒用到

    # bounding box顯示設定
    line_thickness: int = 3  # bounding box thickness (pixels)
    hide_labels: bool = False  # bounding box hide label
    hide_conf: bool = False  # bounding box hide conf

    # class Config:
    #    env_file = "env.txt"
    batch_size: int = 1
    queue_timeout: float = 1
    num_workers: int = 1

    # Service selection: "pose" or "gesture"
    service: str = 'pose'

    # gesture service options
    gesture_weights: str = 'mb1-ssd-best.pth'
    gesture_label_path: str = 'voc-model-labels.txt'

    class Config:
        env_prefix = ""  # 啟用環境變數，無前綴

settings = Settings()
