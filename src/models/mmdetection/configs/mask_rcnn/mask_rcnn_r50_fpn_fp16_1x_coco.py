_base_ = './mask_rcnn_r50_fpn_1x_coco.py'
# fp16 settings
fp16 = dict(loss_scale=512.0)
