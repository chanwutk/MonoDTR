import os
from tqdm import tqdm
from easydict import EasyDict
from typing import Sized
import numpy as np
import torch
import torch.nn as nn
from torch.utils.tensorboard import SummaryWriter
from visualDet3D.networks.utils.registry import PIPELINE_DICT
from visualDet3D.evaluator.kitti.evaluate import evaluate
from visualDet3D.networks.utils.utils import BBox3dProjector, BackProjection
from visualDet3D.data.kitti.utils import write_result_to_file


@PIPELINE_DICT.register_module
@torch.no_grad()
def predict(cfg:EasyDict, 
            model:nn.Module,
            dataset_val:Sized,
            writer:SummaryWriter,
            epoch_num:int,
            ):
    model.eval()
    result_path = os.path.join(cfg.path.preprocessed_path, 'data')
    if os.path.isdir(result_path):
        os.system("rm -r {}".format(result_path))
        print("clean up the recorder directory of {}".format(result_path))
    os.mkdir(result_path)
    print("rebuild {}".format(result_path))
    test_func = PIPELINE_DICT[cfg.trainer.test_func]
    projector = BBox3dProjector().cuda()
    backprojector = BackProjection().cuda()
    for index in tqdm(range(len(dataset_val))):
        test_one(cfg, index, dataset_val, model, test_func, backprojector, projector, result_path)
    if "is_running_test_set" in cfg and cfg["is_running_test_set"]:
        print("Finish evaluation.")
        return
    result_texts = evaluate(
        label_path=os.path.join(cfg.path.data_path, 'label_2'),
        result_path=result_path,
        label_split_file=cfg.data.val_split_file,
        current_classes=[i for i in range(len(cfg.obj_types))],
        gpu=min(cfg.trainer.gpu, torch.cuda.device_count() - 1)
    )
    for class_index, result_text in enumerate(result_texts):
        if writer is not None:
            writer.add_text("validation result {}".format(class_index), result_text.replace(' ', '&nbsp;').replace('\n', '  \n'), epoch_num + 1)
        print(result_text)

def test_one(cfg, index, dataset, model, test_func, backprojector:BackProjection, projector:BBox3dProjector, result_path):
    data = dataset[index]
    if isinstance(data['calib'], list):
        P2 = data['calib'][0]
    else:
        P2 = data['calib']
    original_height = data['original_shape'][0]
    collated_data = dataset.collate_fn([data])
    height = collated_data[0].shape[2]
        
    scores, bbox, obj_names = test_func(collated_data, model, None, cfg=cfg)
    bbox_2d = bbox[:, 0:4]
    if bbox.shape[1] > 4: # run 3D
        bbox_3d_state = bbox[:, 4:] #[cx,cy,z,w,h,l,alpha, bot, top]
        bbox_3d_state_3d = backprojector(bbox_3d_state, P2) #[x, y, z, w,h ,l, alpha, bot, top]

        _, _, thetas = projector(bbox_3d_state_3d, bbox_3d_state_3d.new(P2))

        original_P = data['original_P']
        scale_x = original_P[0, 0] / P2[0, 0]
        scale_y = original_P[1, 1] / P2[1, 1]
        
        shift_left = original_P[0, 2] / scale_x - P2[0, 2]
        shift_top  = original_P[1, 2] / scale_y - P2[1, 2]
        bbox_2d[:, 0:4:2] += shift_left
        bbox_2d[:, 1:4:2] += shift_top

        bbox_2d[:, 0:4:2] *= scale_x
        bbox_2d[:, 1:4:2] *= scale_y

        write_result_to_file(result_path, index, scores, bbox_2d, bbox_3d_state_3d, thetas, obj_names)
    else:
        if "crop_top" in cfg.data.augmentation and cfg.data.augmentation.crop_top is not None:
            crop_top = cfg.data.augmentation.crop_top
        elif "crop_top_height" in cfg.data.augmentation and cfg.data.augmentation.crop_top_height is not None:
            if cfg.data.augmentation.crop_top_height >= original_height:
                crop_top = 0
            else:
                crop_top = original_height - cfg.data.augmentation.crop_top_height

        scale_2d = (original_height - crop_top) / height
        bbox_2d[:, 0:4] *= scale_2d
        bbox_2d[:, 1:4:2] += cfg.data.augmentation.crop_top
        if isinstance(scores, torch.Tensor):
            scores = scores.detach().cpu().numpy()
        write_result_to_file(result_path, index, scores, bbox_2d, obj_types=obj_names)


def write_result_to_image(
    base_result_path:str, 
    index:int,
    scores,
    bbox_2d,
    bbox_3d_state_3d=None,
    thetas=None,
    obj_types=['Car', 'Pedestrian', 'Cyclist'],
    threshold=0.4
):
    """Write Kitti prediction results of one frame to a file 

    Args:
        base_result_path (str): path to the result dictionary 
        index (int): index of the target frame
        scores (List[float]): A list or numpy array or cpu tensor of float for score
        bbox_2d (np.ndarray): numpy array of [N, 4]
        bbox_3d_state_3d (np.ndarray, optional): 3D stats [N, 7] [x_center, y_center, z_center, w, h, l, alpha]. Defaults to None.
        thetas (np.ndarray, optional): [N]. Defaults to None.
        obj_types (List[str], optional): List of string if object type names. Defaults to ['Car', 'Pedestrian', 'Cyclist'].
        threshold (float, optional): Threshold for selection samples. Defaults to 0.4.
    """    
    name = "%06d" % index
    text_to_write = ""
    file = open(os.path.join(base_result_path, name + '.txt'), 'w')
    if bbox_3d_state_3d is None:
        bbox_3d_state_3d = np.ones([bbox_2d.shape[0], 7], dtype=int)
        bbox_3d_state_3d[:, 3:6] = -1
        bbox_3d_state_3d[:, 0:3] = -1000
        bbox_3d_state_3d[:, 6]   = -10
    else:
        for i in range(len(bbox_2d)):
            bbox_3d_state_3d[i][1] = bbox_3d_state_3d[i][1] + 0.5*bbox_3d_state_3d[i][4] # kitti receive bottom center

    if thetas is None:
        thetas = np.ones(bbox_2d.shape[0]) * -10
    if len(scores) > 0:
        for i in range(len(bbox_2d)):
            if scores[i] < threshold:
                continue
            bbox = bbox_2d[i]
            text_to_write += ('{} -1 -1 {:.6f} {:.6f} {:.6f} {:.6f} {:.6f} {:.6f} {:.6f} {:.6f} {:.6f} {:.6f} {:.6f} {:.6f} {} \n').format(
                obj_types[i], bbox_3d_state_3d[i][-1], bbox[0], bbox[1], bbox[2], bbox[3],
                bbox_3d_state_3d[i][4], bbox_3d_state_3d[i][3], bbox_3d_state_3d[i][5],
                bbox_3d_state_3d[i][0], bbox_3d_state_3d[i][1], bbox_3d_state_3d[i][2],
                thetas[i], scores[i])
    file.write(text_to_write)
    file.close()