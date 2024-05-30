import warnings
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable, List, Optional, Tuple, Union

import numpy as np

from supervision.detection.core import Detections
from supervision.detection.overlap_filter import OverlapFilter, validate_overlap_filter
from supervision.detection.utils import move_boxes, move_masks
from supervision.utils.image import crop_image
from supervision.utils.internal import SupervisionWarnings
from supervision.utils.iterables import create_batches


def move_detections(
    detections: Detections,
    offset: np.ndarray,
    resolution_wh: Optional[Tuple[int, int]] = None,
) -> Detections:
    """
    Args:
        detections (sv.Detections): Detections object to be moved.
        offset (np.ndarray): An array of shape `(2,)` containing offset values in format
            is `[dx, dy]`.
        resolution_wh (Tuple[int, int]): The width and height of the desired mask
            resolution. Required for segmentation detections.

    Returns:
        (sv.Detections) repositioned Detections object.
    """
    detections.xyxy = move_boxes(xyxy=detections.xyxy, offset=offset)
    if detections.mask is not None:
        if resolution_wh is None:
            raise ValueError(
                "Resolution width and height are required for moving segmentation "
                "detections. This should be the same as (width, height) of image shape."
            )
        detections.mask = move_masks(
            masks=detections.mask, offset=offset, resolution_wh=resolution_wh
        )
    return detections


class InferenceSlicer:
    """
    InferenceSlicer performs slicing-based inference for small target detection. This
    method, often referred to as
    [Slicing Adaptive Inference (SAHI)](https://ieeexplore.ieee.org/document/9897990),
    involves dividing a larger image into smaller slices, performing inference on each
    slice, and then merging the detections.

    Args:
        slice_wh (Tuple[int, int]): Dimensions of each slice in the format
            `(width, height)`.
        overlap_ratio_wh (Tuple[float, float]): Overlap ratio between consecutive
            slices in the format `(width_ratio, height_ratio)`.
        overlap_filter_strategy (Union[OverlapFilter, str]): Strategy for
            filtering or merging overlapping detections in slices.
        iou_threshold (float): Intersection over Union (IoU) threshold
            used for non-max suppression.
        callback (Callable): A function that performs inference on a given image
            slice and returns detections. Should accept `np.ndarray` if
            `batch_size` is `1` (default) and `List[np.ndarray]` otherwise.
            See examples for more details.
        batch_size (int): How many images to pass to the model. Defaults to 1.
            For other values, `callback` should accept a list of images. Higher
            value uses more memory but may be faster.
        thread_workers (int): Number of threads for parallel execution.

    Note:
        The class ensures that slices do not exceed the boundaries of the original
        image. As a result, the final slices in the row and column dimensions might be
        smaller than the specified slice dimensions if the image's width or height is
        not a multiple of the slice's width or height minus the overlap.
    """

    def __init__(
        self,
        callback: Union[
            Callable[[np.ndarray], Detections],
            Callable[[List[np.ndarray]], List[Detections]],
        ],
        slice_wh: Tuple[int, int] = (320, 320),
        overlap_ratio_wh: Tuple[float, float] = (0.2, 0.2),
        overlap_filter_strategy: Union[
            OverlapFilter, str
        ] = OverlapFilter.NON_MAX_SUPPRESSION,
        iou_threshold: float = 0.5,
        batch_size: int = 1,
        thread_workers: int = 1,
    ):
        overlap_filter_strategy = validate_overlap_filter(overlap_filter_strategy)

        self.slice_wh = slice_wh
        self.overlap_ratio_wh = overlap_ratio_wh
        self.iou_threshold = iou_threshold
        self.overlap_filter_strategy = overlap_filter_strategy
        self.callback = callback
        self.batch_size = batch_size
        self.thread_workers = thread_workers

        if self.batch_size < 1:
            raise ValueError("batch_size should be greater than 0")
        if self.thread_workers < 1:
            raise ValueError("thread_workers should be greater than 0.")

    def __call__(self, image: np.ndarray) -> Detections:
        """
        Performs slicing-based inference on the provided image using the specified
            callback.

        Args:
            image (np.ndarray): The input image on which inference needs to be
                performed. The image should be in the format
                `(height, width, channels)`.

        Returns:
            Detections: A collection of detections for the entire image after merging
                results from all slices and applying NMS.

        Example:
            ```python
            import cv2
            import supervision as sv
            from ultralytics import YOLO

            image = cv2.imread(SOURCE_IMAGE_PATH)
            model = YOLO(...)

            # Option 1: Single slice
            def callback(slice: np.ndarray) -> sv.Detections:
                result = model(slice)[0]
                detections = sv.Detections.from_ultralytics(result)
                return detections

            slicer = sv.InferenceSlicer(callback=callback)
            detections = slicer(image)


            # Option 2: Batch slices (Faster, but uses more memory)
            def callback(slices: List[np.ndarray]) -> List[sv.Detections]:
                results = model(slices)
                detections_list = [
                    sv.Detections.from_ultralytics(result) for result in results]
                return detections_list

            slicer = sv.InferenceSlicer(
                callback=callback,
                overlap_filter_strategy=sv.OverlapFilter.NON_MAX_SUPPRESSION,
            )

            detections = slicer(image)
            ```
        """
        detections_list = []
        resolution_wh = (image.shape[1], image.shape[0])
        offsets = self._generate_offset(
            resolution_wh=resolution_wh,
            slice_wh=self.slice_wh,
            overlap_ratio_wh=self.overlap_ratio_wh,
        )
        batched_offsets_generator = create_batches(offsets, self.batch_size)

        if self.thread_workers == 1:
            for offset_batch in batched_offsets_generator:
                if self.batch_size == 1:
                    result = self._callback_image_single(image, offset_batch[0])
                    detections_list.append(result)
                else:
                    results = self._callback_image_batch(image, offset_batch)
                    detections_list.extend(results)

        else:
            with ThreadPoolExecutor(max_workers=self.thread_workers) as executor:
                futures = []
                for offset_batch in batched_offsets_generator:
                    if self.batch_size == 1:
                        future = executor.submit(
                            self._callback_image_single, image, offset_batch[0]
                        )
                    else:
                        future = executor.submit(
                            self._callback_image_batch, image, offset_batch
                        )
                    futures.append(future)

                for future in as_completed(futures):
                    if self.batch_size == 1:
                        detections_list.append(future.result())
                    else:
                        detections_list.extend(future.result())

        merged = Detections.merge(detections_list=detections_list)
        if self.overlap_filter_strategy == OverlapFilter.NONE:
            return merged
        elif self.overlap_filter_strategy == OverlapFilter.NON_MAX_SUPPRESSION:
            return merged.with_nms(threshold=self.iou_threshold)
        elif self.overlap_filter_strategy == OverlapFilter.NON_MAX_MERGE:
            return merged.with_nmm(threshold=self.iou_threshold)
        else:
            warnings.warn(
                f"Invalid overlap filter strategy: {self.overlap_filter_strategy}",
                category=SupervisionWarnings,
            )
            return merged

    def _callback_image_single(
        self, image: np.ndarray, offset: np.ndarray
    ) -> Detections:
        """
        Run the callback on a single image.

        Args:
            image (np.ndarray): The input image on which inference needs to run
        """
        assert isinstance(offset, np.ndarray)

        image_slice = crop_image(image=image, xyxy=offset)
        detections = self.callback(image_slice)
        if not isinstance(detections, Detections):
            raise ValueError(
                f"Callback should return a single Detections object when "
                f"max_batch_size is 1. Instead it returned: {type(detections)}"
            )

        detections = move_detections(detections=detections, offset=offset[:2])
        return detections

    def _callback_image_batch(
        self, image: np.ndarray, offsets_batch: List[np.ndarray]
    ) -> List[Detections]:
        """
        Run the callback on a batch of images.

        Args:
            image (np.ndarray): The input image on which inference needs to run
            offsets_batch (List[np.ndarray]): List of N arrays of shape `(4,)`,
                containing coordinates of the slices.

        Returns:
            List[Detections]: Detections found in each slice
        """
        assert isinstance(offsets_batch, list)

        slices = [crop_image(image=image, xyxy=offset) for offset in offsets_batch]
        detections_in_slices = self.callback(slices)
        if not isinstance(detections_in_slices, list):
            raise ValueError(
                f"Callback should return a list of Detections objects when "
                f"max_batch_size is greater than 1. "
                f"Instead it returned: {type(detections_in_slices)}"
            )

        detections_with_offset = [
            move_detections(detections=detections, offset=offset[:2])
            for detections, offset in zip(detections_in_slices, offsets_batch)
        ]

        return detections_with_offset

    @staticmethod
    def _generate_offset(
        resolution_wh: Tuple[int, int],
        slice_wh: Tuple[int, int],
        overlap_ratio_wh: Tuple[float, float],
    ) -> np.ndarray:
        """
        Generate offset coordinates for slicing an image based on the given resolution,
        slice dimensions, and overlap ratios.

        Args:
            resolution_wh (Tuple[int, int]): A tuple representing the width and height
                of the image to be sliced.
            slice_wh (Tuple[int, int]): A tuple representing the desired width and
                height of each slice.
            overlap_ratio_wh (Tuple[float, float]): A tuple representing the desired
                overlap ratio for width and height between consecutive slices. Each
                value should be in the range [0, 1), where 0 means no overlap and a
                value close to 1 means high overlap.

        Returns:
            np.ndarray: An array of shape `(n, 4)` containing coordinates for each
                slice in the format `[xmin, ymin, xmax, ymax]`.

        Note:
            The function ensures that slices do not exceed the boundaries of the
                original image. As a result, the final slices in the row and column
                dimensions might be smaller than the specified slice dimensions if the
                image's width or height is not a multiple of the slice's width or
                height minus the overlap.
        """
        slice_width, slice_height = slice_wh
        image_width, image_height = resolution_wh
        overlap_ratio_width, overlap_ratio_height = overlap_ratio_wh

        width_stride = slice_width - int(overlap_ratio_width * slice_width)
        height_stride = slice_height - int(overlap_ratio_height * slice_height)

        ws = np.arange(0, image_width, width_stride)
        hs = np.arange(0, image_height, height_stride)

        xmin, ymin = np.meshgrid(ws, hs)
        xmax = np.clip(xmin + slice_width, 0, image_width)
        ymax = np.clip(ymin + slice_height, 0, image_height)

        offsets = np.stack([xmin, ymin, xmax, ymax], axis=-1).reshape(-1, 4)

        return offsets
