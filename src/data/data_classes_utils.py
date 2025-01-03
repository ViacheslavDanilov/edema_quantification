import re
from collections import defaultdict
from typing import Any, DefaultDict, Dict, List, Tuple, Union

import albumentations as A
import cv2
import numpy as np
import pandas as pd
import torch
from PIL import Image, ImageDraw, ImageOps
from torch.utils.data import Dataset, Subset

from src.data.utils_sly import FEATURE_MAP, convert_base64_to_mask

__all__ = [
    'resize_and_create_masks',
    'split_dataset',
    'combine_image_and_masks',
    'parse_coord_string',
]


FINDINGS_DTYPE = torch.float32
IMAGE_DTYPE = torch.float32
MASK_DTYPE = np.float32
TENSOR_DTYPE = torch.float32
# all relevant edema findings for which masks will be prepared subsequently
EDEMA_FINDINGS = [k for k in FEATURE_MAP if k not in ['Heart', 'Lungs']]


def parse_coord_string(coord_string: str) -> np.ndarray:
    coordinates = [int(d) for d in re.findall(r'\d+', coord_string)]
    return np.array(coordinates).reshape(-1, 2)


def extract_annotations(group_df: pd.DataFrame) -> Dict[str, Union[DefaultDict[Any, list], None]]:
    """Extract annotations.

    Args:
        group_df: chunk of DataFrame with annotations for a given image

    Returns:
        annotations: dict with processed annotation data for a given image
    """
    # for "No edema" class there are no findings
    # if group_df['Feature'].isna().all():
    if (
        _is_unique(group_df['Class'])
        and group_df['Class'].iloc[0] == 'No edema'
        and _is_unique(group_df['Feature'])
        and group_df['Feature'].iloc[0] == 'Heart'
    ):
        return {'No_findings': None}

    # for other classes we create dict with finding_name as a key
    # and values are the lists containing
    # coordinates of points for polygon annotations and mask data for mask annotations
    annotations: Dict[str, Union[DefaultDict[Any, list], None]] = {
        k: defaultdict(list) for k in group_df['Feature'].unique()
    }

    # change 'x1', 'y1' columns type from float (due to NANs presence) to int
    group_df = group_df[['Feature', 'x1', 'y1', 'Mask', 'Points']].astype(int, errors='ignore')

    for _, data in group_df.iterrows():
        if pd.notna(data['Mask']):
            annotations[data['Feature']]['bitmaps'].append(data.loc['x1':'Mask'].to_dict())  # type: ignore
        else:
            annotations[data['Feature']]['polygons'].append(parse_coord_string(data['Points']))

    return annotations


def make_masks(
    image: Image.Image,
    annotations: Dict,
    linelike_finding_width: int = 15,
) -> Tuple[List[np.ndarray], List[int], int]:
    """Make masks.

    Args:
        image: image
        annotations: dict with annotation data for a given image
        linelike_finding_width: line width in masks corresponding to findings annotated with lines

    Returns:
        masks: list of masks represented as numpy arrays for each edema finding
        findings: list of 0/1 values showing the absence/presence of a given finding in a given image
        default_mask_value: value with which mask is padded depending on the
            presence of at least one of the edema findings in the image
    """
    masks = []
    findings = []
    width, height = image.size
    default_mask_value = 1 if any(f in EDEMA_FINDINGS for f in annotations.keys()) else 0

    # adding default 'No_findings' finding to the list and preparing masks for each of them
    for finding in ['No_findings'] + EDEMA_FINDINGS:
        # binary mask template
        finding_mask = Image.new(mode='1', size=(width, height), color=default_mask_value)

        if finding in annotations.keys():
            # for "No edema" class without findings we do nothing and proceed with the default mask
            if annotations[finding] is None:
                pass

            # draw finding mask represented by polygons
            elif annotations[finding]['polygons']:
                draw = ImageDraw.Draw(finding_mask)

                for point_array in annotations[finding]['polygons']:
                    # for line-like findings we draw lines with default width of 15px
                    if finding in ('Kerley', 'Cephalization'):
                        draw.line(
                            point_array.flatten().tolist(),
                            fill=0,
                            width=linelike_finding_width,
                        )
                    else:
                        draw.polygon(point_array.flatten().tolist(), fill=0, outline=0)

            # draw finding mask represented by bitmaps
            elif annotations[finding]['bitmaps']:
                for bitmap in annotations[finding]['bitmaps']:
                    bitmap_array = convert_base64_to_mask(bitmap['Mask'])
                    bitmap_mask = Image.fromarray(bitmap_array).convert('1')
                    inverted_bitmap_mask = ImageOps.invert(bitmap_mask)

                    finding_mask.paste(inverted_bitmap_mask, box=(bitmap['x1'], bitmap['y1']))

            else:
                raise RuntimeError('neither polygon nor mask data is present in the metadata')

            masks.append(np.array(finding_mask, dtype=MASK_DTYPE))
            findings.append(1)

        else:
            # Make masks == 1 for non 'No_findings' features
            if list(annotations.keys())[0] == 'No_findings':
                finding_mask = Image.new(mode='1', size=(width, height), color=1)
                masks.append(np.array(finding_mask, dtype=MASK_DTYPE))
                findings.append(0)
            else:
                masks.append(np.array(finding_mask, dtype=MASK_DTYPE))
                findings.append(0)

    return masks, findings, default_mask_value


def resize_and_create_masks(
    image: Image.Image,
    annotations: Dict,
    target_size: Tuple[int, int],
    linelike_finding_width: int = 15,
) -> Tuple[np.ndarray, List[np.ndarray], torch.Tensor]:
    """Resize and create masks.

    Args:
        image: input image
        annotations: dict with image annotations
        target_size: tuple with target image size (width, height)
        linelike_finding_width: width in pixels for linelike annotations

    Returns:
        image_resized: numpy array of the resized image
        masks_resized: list of numpy arrays of the resized masks
        findings: list of 0/1 values showing the presence/absence of a given finding in the image
    """
    # image and created image masks with initial size
    # images have to be unnormalized in [0, 1] to perform drawing in the model class
    image_arr = np.array(image) / 255
    masks, findings, default_mask_value = make_masks(
        image,
        annotations,
        linelike_finding_width=linelike_finding_width,
    )

    # further we resize image and masks by padding them while keeping initial aspect ratio
    # this sequence of transformations mimics PIL.ImageOps.pad function
    # therefore we define 'resize_transform' depending on several factors
    width_new, height_new = target_size
    width_0, height_0 = image.size
    aspect_0 = width_0 / height_0
    aspect_new = width_new / height_new

    if aspect_0 < aspect_new:
        if width_0 > height_0:
            resize_transform = A.augmentations.geometric.SmallestMaxSize(max_size=height_new)
        else:
            resize_transform = A.augmentations.geometric.LongestMaxSize(max_size=height_new)
    elif aspect_0 > aspect_new:
        if width_0 > height_0:
            resize_transform = A.augmentations.geometric.LongestMaxSize(max_size=width_new)
        else:
            resize_transform = A.augmentations.geometric.SmallestMaxSize(max_size=width_new)
    else:
        resize_transform = A.augmentations.geometric.Resize(height_new, width_new)

    # make image and masks resize operations
    # image is padded with 0 and masks with default_mask_value (0 or 1)
    transform = A.Compose(
        [
            resize_transform,
            A.augmentations.geometric.PadIfNeeded(
                height_new,
                width_new,
                border_mode=cv2.BORDER_CONSTANT,
                value=0,
                mask_value=default_mask_value,
            ),
        ],
    )

    transformed = transform(image=image_arr, masks=masks)
    image_resized = transformed['image']
    masks_resized = transformed['masks']

    findings = torch.tensor(findings, dtype=FINDINGS_DTYPE)

    return image_resized, masks_resized, findings


def combine_image_and_masks(
    transformed_image: torch.Tensor,
    transformed_masks: List[torch.Tensor],
) -> torch.Tensor:
    transformed_image = [transformed_image.to(dtype=IMAGE_DTYPE)]
    expanded_masks = [m.expand(1, -1, -1) for m in transformed_masks]
    tensor = torch.cat(transformed_image + expanded_masks, dim=0)
    tensor = tensor.to(dtype=TENSOR_DTYPE)

    return tensor


def split_dataset(
    dataset: Dataset,
    train_share: float,
    metadata_df: pd.DataFrame,
    n_split_trials: int = 500,
    ensure_all_classes_in_splits: bool = True,
    verbose: bool = False,
) -> Tuple[Subset, Subset]:
    """Split dataset.

    Args:
        dataset: complete Dataset
        train_share: share of the train part
        metadata_df: dataframe with image data used for proper split
        n_split_trials: number of dataset split trials, basically number of data shuffles
        ensure_all_classes_in_splits: check whether objects of all classes are present in both test and train part
        verbose: show info about the best obtained train/test split

    Returns:
        train_subset: train subset
        test_subset: test subset

    """
    rmsd_min = np.inf
    best_train_subjects_set = None
    best_test_subjects_set = None
    best_class_distribution = None

    for i in range(n_split_trials):
        # shuffle df and split into two subsets based on Subject IDs
        # since we avoid putting images from the same patient both in train and test sets
        grouped = (
            metadata_df.sample(frac=1.0, random_state=i)
            .reset_index(drop=True)
            .groupby('Subject ID', sort=False)['Image path']
            .nunique()
            .cumsum()
            .transform(lambda s: s / s.max())
        )

        train_subjects = grouped[grouped < train_share].index
        test_subjects = grouped[grouped >= train_share].index

        dfc = metadata_df.copy()
        dfc.loc[dfc['Subject ID'].isin(train_subjects), 'train_test_set'] = 'train'
        dfc.loc[dfc['Subject ID'].isin(test_subjects), 'train_test_set'] = 'test'

        dfc_percentages = (
            dfc.groupby('train_test_set')['Class ID'].value_counts(normalize=True).unstack()
        )

        if ensure_all_classes_in_splits:
            # if there is any NAN, it means that the two splits do not contain all classes
            if dfc_percentages.isna().any().any():
                continue

        # RMSD between class shares in train/test splits
        rmsd_classes = np.sum((dfc_percentages.loc['train'] - dfc_percentages.loc['test']) ** 2)

        if rmsd_classes < rmsd_min:
            rmsd_min = rmsd_classes
            best_train_subjects_set, best_test_subjects_set = train_subjects, test_subjects
            best_class_distribution = dfc_percentages

    if best_class_distribution is None:
        raise RuntimeError(
            (
                f'{n_split_trials} split trials were not enough to split dataset. '
                'Consider increasing n_split_trials parameter '
                'or set ensure_all_classes_in_splits to False'
            ),
        )

    # we need image indices but we have split the dataset using patients (through Subject IDs)
    # so here we obtain indices of images belonging to patients in train/test splits
    unique_image_paths = metadata_df['Image path'].unique()
    image_path2index_dict = dict(zip(unique_image_paths, range(unique_image_paths.size)))
    train_indices = (
        metadata_df.loc[metadata_df['Subject ID'].isin(best_train_subjects_set), 'Image path']
        .map(image_path2index_dict)
        .unique()
    )
    test_indices = (
        metadata_df.loc[metadata_df['Subject ID'].isin(best_test_subjects_set), 'Image path']
        .map(image_path2index_dict)
        .unique()
    )

    if verbose:
        print('#' * 80)
        print('Best train/test split class distributions:')
        print('#' * 80)
        print(best_class_distribution)
        print('#' * 80)
        print(
            (
                f'{best_train_subjects_set.size} patients in train set, '
                f'{best_test_subjects_set.size} patients in test set'
            ),
        )
        print(
            (
                f'{train_indices.size} images in train set, '
                f'{test_indices.size} images in test set'
            ),
        )
        print('#' * 80)

    train_subset = Subset(dataset, train_indices)
    test_subset = Subset(dataset, test_indices)

    return train_subset, test_subset


def _is_unique(s: pd.Series) -> bool:
    a = s.to_numpy()
    return (a[0] == a).all()


# if __name__ == '__main__':

#     import os

#     metadata_df = pd.read_excel(
#         os.path.join('C:/Users/makov/Desktop/data_edema', 'metadata.xlsx')
#     ).fillna({'Class ID': -1})
#     for img_idx, (img_path, img_objects) in enumerate(metadata_df.groupby('Image path')):
#         annotations = {k: defaultdict(list) for k in img_objects['Figure'].unique()}
#         print(annotations)

#         for _, data in img_objects[['Figure', 'x1', 'y1', 'Mask', 'Points']].iterrows():
#             if pd.notna(data['Mask']):
#                 annotations[data['Figure']]['bitmaps'].append(data.loc['x1':'Mask'].to_dict())
#             else:
#                 annotations[data['Figure']]['polygons'].append(parse_coord_string(data['Points']))
# print(type(data['Points']))
# print(annotations[data['Figure']]['polygons'])
# annotations[data['Figure']]['polygons'].append(parse_coord_string(data['Points']))
