_base_ = [
    '../_base_/models/faster_rcnn_r50_fpn.py',
    '../_base_/datasets/objects365v2_detection.py',
    '../_base_/schedules/schedule_1x.py',
    '../_base_/default_runtime.py',
]

model = dict(roi_head=dict(bbox_head=dict(num_classes=365)))

data = dict(samples_per_gpu=4)

# Using 32 GPUS while training
optimizer = dict(type='SGD', lr=0.08, momentum=0.9, weight_decay=0.0001)
optimizer_config = dict(
    _delete_=True,
    grad_clip=dict(max_norm=35, norm_type=2),
)
lr_config = dict(
    policy='step',
    warmup='linear',
    warmup_iters=1000,
    warmup_ratio=1.0 / 1000,
    step=[8, 11],
)

# NOTE: `auto_scale_lr` is for automatically scaling LR,
# USER SHOULD NOT CHANGE ITS VALUES.
# base_batch_size = (16 GPUs) x (4 samples per GPU)
auto_scale_lr = dict(base_batch_size=64)
