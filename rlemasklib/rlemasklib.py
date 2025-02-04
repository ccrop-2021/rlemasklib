import zlib
import numpy as np
import rlemasklib.rlemasklib_cython as rlemasklib_cython

# Interface for manipulating masks stored in RLE format.
#
# RLE is a simple yet efficient format for storing binary masks. RLE
# first divides a vector (or vectorized image) into a series of piecewise
# constant regions and then for each piece simply stores the length of
# that piece. For example, given M=[0 0 1 1 1 0 1] the RLE counts would
# be [2 3 1 1], or for M=[1 1 1 1 1 1 0] the counts would be [0 6 1]
# (note that the odd counts are always the numbers of zeros). Instead of
# storing the counts directly, additional compression is achieved with a
# variable bitrate representation based on a common scheme called LEB128.
#
# Compression is greatest given large piecewise constant regions.
# Specifically, the size of the RLE is proportional to the number of
# *boundaries* in M (or for an image the number of boundaries in the y
# direction). Assuming fairly simple shapes, the RLE representation is
# O(sqrt(n)) where n is number of pixels in the object. Hence space usage
# is substantially lower, especially for large simple objects (large n).
#
# Many common operations on masks can be computed directly using the RLE
# (without need for decoding). This includes computations such as area,
# union, intersection, etc. All of these operations are linear in the
# size of the RLE, in other words they are O(sqrt(n)) where n is the area
# of the object. Computing these operations on the original mask is O(n).
# Thus, using the RLE can result in substantial computational savings.
#
# To compile run "python setup.py build_ext --inplace"
#
# Based on the Microsoft COCO Toolbox version 2.0
# Code written by Piotr Dollar and Tsung-Yi Lin, 2015.
# Modified by Istvan Sarandi, 2023.
# Licensed under the Simplified BSD License [see coco/license.txt]

from enum import Enum

_X = 0b1100
_Y = 0b1010


class BoolFunc(Enum):
    A = _X
    B = _Y
    UNION = OR = _X | _Y
    INTERSECTION = AND = _X & _Y
    DIFFERENCE = _X & ~_Y
    SYMMETRIC_DIFFERENCE = XOR = _X ^ _Y
    EQUIVALENCE = XNOR = ~(_X ^ _Y)
    IMPLICATION = ~_X | _Y
    NOR = ~(_X | _Y)
    NAND = ~(_X & _Y)


class RLE:
    def __init__(self, masks=None, encoded_mask=None):
        if encoded_mask is not None:
            if isinstance(encoded_mask, (tuple, list)):
                self.rles = encoded_mask
            else:
                self.rles = [encoded_mask]
        elif masks is not None:
            if masks.ndim == 3:
                # masks is (BXHXW) but decode expects (HXWXB)                
                masks = np.moveaxis(masks, 0, -1)
                self.rles = encode(masks)                
            elif masks.ndim == 2:
                self.rles = [encode(masks)]
            else:
                raise ValueError("Invalid mask dimensions. Must be (BXHXW) or (HXW).")
        else:
            raise ValueError("Either masks or encoded_mask must be provided.")

    def __getitem__(self, index):
        if isinstance(index, np.ndarray):
            if index.dtype == bool:
                # Boolean indexing
                selected_rles = [self.rles[i] for i, flag in enumerate(index) if flag]
                return RLE(encoded_mask=selected_rles)
            elif index.dtype == int:
                # Index array
                selected_rles = [self.rles[i] for i in index]
                return RLE(encoded_mask=selected_rles)
            else:
                raise ValueError("Unsupported indexing type.")
        else:
            # Slicing with slice object
            return RLE(encoded_mask=self.rles[index])        

    def __len__(self):
        return len(self.rles)

    def __repr__(self):
        if len(self.rles) == 1:
            return str(self.rles[0])
        return str(self.rles)

    def __and__(self, other):
        return RLE(encoded_mask=[intersection([self.rles[i], other.rles[i]]) for i in range(len(self))])

    def __or__(self, other):
        return RLE(encoded_mask=[union([self.rles[i], other.rles[i]]) for i in range(len(self))])

    def __xor__(self, other):
        return RLE(encoded_mask=[symmetric_difference(self.rles[i], other.rles[i]) for i in range(len(self))])

    def __sub__(self, other):
        return RLE(encoded_mask=[difference(self.rles[i], other.rles[i]) for i in range(len(self))])

    def __invert__(self):
        return RLE(encoded_mask=[complement(self.rles[i]) for i in range(len(self))])

    def todict(self):
        return self.rles if len(self.rles) > 1 else self.rles[0]

    @property
    def area(self):
        return area(self.rles)

    def complement(self):
        return RLE(encoded_mask=complement(self.rles))

    def decode(self):
        masks = decode(self.rles)
        masks = np.moveaxis(masks, -1, 0).astype(bool)
        return masks

    @property
    def masks(self):
        return self.decode()

    def crop(self, bbox):
        return RLE(encoded_mask=crop(self.rles, bbox))

    def pad(self, paddings, value=0):
        return RLE(encoded_mask=pad(self.rles, paddings, value))

    @property
    def xywh(self):
        return to_bbox(self.rles)
    @property
    def xyxy(self):
        xywh = np.array(to_bbox(self.rles))  # Assuming to_bbox returns a numpy array already
        # Efficiently compute xyxy in a vectorized way
        xyxy = np.hstack((xywh[:, :2], xywh[:, :2] + xywh[:, 2:4]))
        return xyxy
    

    def from_bbox(self, bbox, imshape=None, imsize=None):
        return RLE(encoded_mask=[from_bbox(bbox, imshape, imsize)])

    def from_polygon(self, poly, imshape=None, imsize=None):
        return RLE(encoded_mask=[from_polygon(poly, imshape, imsize)])

    def union(self, other):
        return RLE(encoded_mask=[union([self.rles[i], other.rles[i]]) for i in range(len(self))])

    def intersection(self, other):
        return RLE(encoded_mask=[intersection([self.rles[i], other.rles[i]]) for i in range(len(self))])

    def difference(self, other):
        return RLE(encoded_mask=[difference(self.rles[i], other.rles[i]) for i in range(len(self))])

    def symmetric_difference(self, other):
        return RLE(encoded_mask=[symmetric_difference(self.rles[i], other.rles[i]) for i in range(len(self))])

    def merge(self, other, boolfunc: BoolFunc):
        return RLE(encoded_mask=[merge([self.rles[i], other.rles[i]], boolfunc) for i in range(len(self))])

    def iou(self):
        # return iou matrix size BXB
        ioum = np.zeros((len(self), len(self)))
        for i in range(len(self)):
            for j in range(len(self)):
                ioum[i, j] = iou([self.rles[i],self.rles[j]])
        return ioum

    def connected_components(self, connectivity=4, min_size=1):        
        return [RLE(encoded_mask=connected_components(rle, connectivity, min_size)) for rle in self.rles]

    def shift(self, offset, border_value=0):
        return RLE(encoded_mask=[shift(rle, offset, border_value) for rle in self.rles])

    def erode(self, connectivity=4):
        return RLE(encoded_mask=[erode(rle, connectivity) for rle in self.rles])

    def dilate(self, connectivity=4):
        return RLE(encoded_mask=[dilate(rle, connectivity) for rle in self.rles])

    def opening(self, connectivity=4):
        return RLE(encoded_mask=[opening(rle, connectivity) for rle in self.rles])

    def closing(self, connectivity=4):
        return RLE(encoded_mask=[closing(rle, connectivity) for rle in self.rles])

    def remove_small_components(self, connectivity=4, min_size=1):
        return RLE(encoded_mask=[remove_small_components(rle, connectivity, min_size) for rle in self.rles])

    def fill_small_holes(self, connectivity=4, min_size=1):
        return RLE(encoded_mask=[fill_small_holes(rle, connectivity, min_size) for rle in self.rles])

    def largest_connected_component(self, connectivity=4):
        return RLE(encoded_mask=[largest_connected_component(rle, connectivity) for rle in self.rles])

    @property
    def centroid(self):
        return centroid(self.rles)

def area(rleObjs):
    """Compute the foreground area for a mask or multiple masks.

    Args:
        rleObjs: either a single RLE or a list of RLEs

    Returns:
        A scalar if input was a single RLE, otherwise a list of scalars.
    """
    if isinstance(rleObjs, (tuple, list)):
        return rlemasklib_cython.area(rleObjs)
    else:
        return rlemasklib_cython.area([rleObjs])[0]


def complement(rleObjs):
    """Compute the complement of a mask or multiple masks.

    Args:
        rleObjs: either a single RLE or a list of RLEs

    Returns:
        A single RLE or a list of RLEs, depending on input type.
    """
    if isinstance(rleObjs, (tuple, list)):
        return rlemasklib_cython.complement(rleObjs)
    else:
        return rlemasklib_cython.complement([rleObjs])[0]


def encode(mask, compressed=True, zlevel=None):
    """Encode binary mask into a compressed RLE.

    Args:
        mask: a binary mask (numpy 2D array of any type, where zero is background and nonzero is foreground)
        compressed: whether to compress the RLE using the LEB128-like algorithm from COCO (and potentially zlib afterwards).
        zlevel: zlib compression level. None means no zlib compression, numbers up to 9 are increasing zlib compression
            levels and -1 is the default level in zlib. It has no effect if compressed=False.

    Returns:
        An encoded RLE object with a size field:
            size: (height, width) of the mask
        And one of the following fields:
            ucounts: uncompressed run-length counts
            counts: LEB128-like compressed run-length counts
            zcounts: zlib-compressed LEB128-like compressed run-length counts
    """
    encoded = _encode(np.asfortranarray(mask.astype(np.uint8)), compress_leb128=compressed)
    if compressed and zlevel is not None:
        return compress(encoded, zlevel=zlevel)

    return encoded


def decode(encoded_mask):
    """Decode a (potentially compressed) RLE encoded mask.

    Args:
        encoded_mask: encoded RLE object

    Returns:
        A binary mask (numpy 2D array of type uint8, where 0 is background and 1 is foreground)
    """

    if 'zcounts' in encoded_mask:
        encoded_mask = dict(
            size=encoded_mask['size'],
            counts=zlib.decompress(encoded_mask['zcounts']))

    if 'ucounts' in encoded_mask:
        return _decode_uncompressed(encoded_mask)

    return _decode(encoded_mask)


def crop(rleObjs, bbox):
    """Crop a mask or multiple masks (RLEs) by the given bounding box.
    The size of each output RLE is the same as the size of the corresponding bounding box.

    Args:
        rleObjs: either a single RLE or a list of RLEs
        bbox: either a single bounding box or a list of bounding boxes, in the format [x_start, y_start, width, height]

    Returns:
        Either a single RLE or a list of RLEs, depending on input type.
    """
    bbox = np.asanyarray(bbox, dtype=np.uint32)
    if isinstance(rleObjs, (tuple, list)):
        return rlemasklib_cython.crop(rleObjs, bbox)
    else:
        rleObjs_out = rlemasklib_cython.crop([rleObjs], bbox[np.newaxis])
        return rleObjs_out[0]


def _pad(rleObjs, paddings):
    """Pad a mask or multiple masks (RLEs) by the given padding amounts.

    Args:
        rleObjs: either a single RLE or a list of RLEs
        paddings: left,right,top,bottom

    Returns:
        Either a single RLE or a list of RLEs, depending on input type.
    """
    paddings = np.asanyarray(paddings, dtype=np.uint32)
    if isinstance(rleObjs, (tuple, list)):
        return rlemasklib_cython.pad(rleObjs, paddings)
    else:
        rleObjs_out = rlemasklib_cython.pad([rleObjs], paddings)
        return rleObjs_out[0]


def pad(rleObjs, paddings, value=0):
    if value == 0:
        return _pad(rleObjs, paddings)
    else:
        return complement(_pad(complement(rleObjs), paddings))


def to_bbox(rleObjs):
    """Convert an RLE mask or multiple RLE masks to a bounding box or a list of bounding boxes.

    Args:
        rleObjs: either a single RLE or a list of RLEs

    Returns:
        bbox(es): either a single bounding box or a list of bounding boxes, in the format [x_start, y_start, width, height]
    """
    if isinstance(rleObjs, (tuple, list)):
        return rlemasklib_cython.toBbox(rleObjs).astype(np.float32)
    else:
        return rlemasklib_cython.toBbox([rleObjs])[0].astype(np.float32)


def get_imshape(imshape=None, imsize=None):
    assert imshape is not None or imsize is not None
    if imshape is None:
        imshape = [imsize[1], imsize[0]]
    return imshape[:2]


def from_bbox(bbox, imshape=None, imsize=None):
    """Connvert a bounding box to an RLE mask of the given size.

    Args:
        bbox: a bounding box, in the format [x_start, y_start, width, height]
        imshape: [height, width] of the desired mask (either this or imsize must be provided)
        imsize: [width, height] of the desired mask (either this or imshape must be provided)

    Returns:
        An RLE mask.
    """
    imshape = get_imshape(imshape, imsize)
    bbox = np.asanyarray(bbox, dtype=np.float64)

    if len(bbox.shape) == 2:
        return rlemasklib_cython.frBbox(bbox, imshape[0], imshape[1])
    else:
        return rlemasklib_cython.frBbox(bbox[np.newaxis], imshape[0], imshape[1])[0]


def from_polygon(poly, imshape=None, imsize=None):
    """Convert a polygon to an RLE mask of the given size.

    Args:
        poly: a polygon (list of xy coordinates)
        imshape: [height, width] of the desired mask (either this or imsize must be provided)
        imsize: [width, height] of the desired mask (either this or imshape must be provided)

    Returns:
        An RLE mask.
    """
    imshape = get_imshape(imshape, imsize)
    poly = np.asanyarray(poly, dtype=np.float64)
    return rlemasklib_cython.frPoly(poly[np.newaxis], imshape[0], imshape[1])[0]


def zeros(imshape=None, imsize=None):
    """Create an empty (fully background) RLE mask of the given size.

    Args:
        imshape: [height, width] of the desired mask (either this or imsize must be provided)
        imsize: [width, height] of the desired mask (either this or imshape must be provided)

    Returns:
        An empty RLE mask.
    """
    imshape = get_imshape(imshape, imsize)
    return compress({'size': imshape[:2], 'ucounts': [imshape[0] * imshape[1]]})


def ones(imshape=None, imsize=None):
    """Create a full (fully foreground) RLE mask of the given size.

    Args:
        imshape: [height, width] of the desired mask (either this or imsize must be provided)
        imsize: [width, height] of the desired mask (either this or imshape must be provided)

    Returns:
        A full RLE mask.
    """
    imshape = get_imshape(imshape, imsize)
    return compress({'size': imshape[:2], 'ucounts': [0, imshape[0] * imshape[1]]})


def ones_like(mask):
    return ones(mask['size'])


def zeros_like(mask):
    return zeros(mask['size'])


def decompress(encoded_mask, only_gzip=False):
    """Decompress a compressed RLE mask to a decompressed RLE. Note that this does not decode the RLE into a binary mask.

    Args:
        encoded_mask:

    Returns:
        An RLE mask dictionary
           'size': [height, width]
           'ucounts': uint32 array of uncompressed run-lengths.
    """
    if 'zcounts' in encoded_mask:
        encoded_mask = dict(
            size=encoded_mask['size'],
            counts=zlib.decompress(encoded_mask['zcounts']))
    if only_gzip:
        return encoded_mask

    return _decompress(encoded_mask)


def compress(rle, zlevel=None):
    """Compress an RLE mask to a compressed RLE. Note that the input needs to be an RLE, not a decoded binary mask.

    Args:
        rle: a mask in RLE format
        zlevel: optional zlib compression level, None means no zlib compression, -1 is zlib's default compression level
           and 0-9 are zlib's compression levels where 9 is maximum compression.

    Returns:
        A compressed RLE mask.
    """
    if 'ucounts' in rle:
        rle = _compress(rle)

    if 'counts' in rle and zlevel is not None:
        rle = dict(
            size=rle['size'],
            zcounts=zlib.compress(rle['counts'], zlevel))

    return rle


def union(masks):
    """Compute the union of multiple RLE masks."""
    return merge(masks, BoolFunc.UNION)


def intersection(masks):
    """Compute the intersection of multiple RLE masks."""
    return merge(masks, BoolFunc.INTERSECTION)


def difference(mask1, mask2):
    """Compute the difference between two RLE masks, i.e., the mask where mask1 is foreground and mask2 is background."""
    return merge([mask1, mask2], BoolFunc.DIFFERENCE)


def any(mask):
    return len(mask['counts']) > 1


def all(mask):
    h, w = mask['size']
    if h * w == 0:
        return True

    return len(mask['counts']) == 2 and mask['counts'][0] == b'\x00'


def symmetric_difference(mask1, mask2):
    """Compute the symmetric difference between two RLE masks, i.e., the mask where either mask1 or mask2 is foreground but not both."""
    return merge([mask1, mask2], BoolFunc.SYMMETRIC_DIFFERENCE)


def merge(masks, boolfunc: BoolFunc):
    return rlemasklib_cython.merge(masks, boolfunc.value)


def _compress(uncompressed_rle):
    if isinstance(uncompressed_rle, (tuple, list)):
        return rlemasklib_cython.frUncompressedRLE(uncompressed_rle)
    return rlemasklib_cython.frUncompressedRLE([uncompressed_rle])[0]


def _decompress(compressed_rle):
    if isinstance(compressed_rle, (tuple, list)):
        return rlemasklib_cython.decompress(compressed_rle)
    return rlemasklib_cython.decompress([compressed_rle])[0]


def _encode(bimask, compress_leb128=True):
    if len(bimask.shape) == 3:
        return rlemasklib_cython.encode(bimask, compress_leb128)
    elif len(bimask.shape) == 2:
        h, w = bimask.shape
        return rlemasklib_cython.encode(bimask.reshape((h, w, 1), order='F'), compress_leb128)[0]


def _decode(rleObjs):
    if isinstance(rleObjs, (tuple, list)):
        return rlemasklib_cython.decode(rleObjs)
    else:
        return rlemasklib_cython.decode([rleObjs])[:, :, 0]


def _decode_uncompressed(rleObjs):
    if isinstance(rleObjs, (tuple, list)):
        return rlemasklib_cython.decodeUncompressed(rleObjs)
    else:
        return rlemasklib_cython.decodeUncompressed([rleObjs])[:, :, 0]


def iou(masks):
    return rlemasklib_cython.iouMulti(masks)


def connected_components(rle, connectivity=4, min_size=1):
    return rlemasklib_cython.connectedComponents(rle, connectivity, min_size)


def shift(rle, offset, border_value=0):
    if offset == (0, 0):
        return rle
    h, w = rle['size']
    paddings = np.maximum(0, np.array([offset[0], -offset[0], offset[1], -offset[1]]))
    cropbox = np.maximum(0, np.array([-offset[0], -offset[1], w, h]))
    return crop(pad(rle, paddings, border_value), cropbox)


def erode(rle, connectivity=4):
    return complement(dilate(complement(rle), connectivity))


def dilate(rle, connectivity=4):
    if connectivity == 4:
        neighbor_offsets = [(0, 1), (0, -1), (1, 0), (-1, 0)]
    else:
        neighbor_offsets = [(0, 1), (0, -1), (1, 0), (-1, 0), (1, 1), (-1, -1), (1, -1), (-1, 1)]
    return union([rle] + [shift(rle, offset) for offset in neighbor_offsets])


def opening(rle, connectivity=4):
    return dilate(erode(rle, connectivity), connectivity)


def closing(rle, connectivity=4):
    return erode(dilate(rle, connectivity), connectivity)


def erode2(rle):
    return complement(dilate2(complement(rle)))


def dilate2(rle):
    return dilate(dilate(rle, 4), 8)


def opening2(rle):
    return dilate2(erode2(rle))


def closing2(rle):
    return erode2(dilate2(rle))


def remove_small_components(rle, connectivity=4, min_size=1):
    components = connected_components(rle, connectivity, min_size)
    return union(components)


def fill_small_holes(rle, connectivity=4, min_size=1):
    return complement(remove_small_components(complement(rle), connectivity, min_size))


def largest_connected_component(rle, connectivity=4):
    components = connected_components(rle, connectivity)
    if not components:
        return None
    areas = area(components)
    return components[np.argmax(areas)]


def centroid(rleObjs):
    """Compute the foreground centroid for a mask or multiple masks.

    Args:
        rleObjs: either a single RLE or a list of RLEs

    Returns:
        A scalar if input was a single RLE, otherwise a list of scalars.
    """
    if isinstance(rleObjs, (tuple, list)):
        return rlemasklib_cython.centroid(rleObjs).astype(np.float32)
    else:
        return rlemasklib_cython.centroid([rleObjs])[0].astype(np.float32)
