# (C) Copyright 2020 ECMWF.
#
# This software is licensed under the terms of the Apache Licence Version 2.0
# which can be obtained at http://www.apache.org/licenses/LICENSE-2.0.
# In applying this licence, ECMWF does not waive the privileges and immunities
# granted to it by virtue of its status as an intergovernmental organisation
# nor does it submit to any jurisdiction.
#

import copy

# import atexit
import datetime
import json
import logging
import os
import warnings

import eccodes

from climetlab import load_source
from climetlab.core import Base
from climetlab.core.caching import auxiliary_cache_file
from climetlab.profiling import call_counter

# from climetlab.decorators import dict_args
from climetlab.utils.bbox import BoundingBox

from . import Reader

# from collections import defaultdict


LOG = logging.getLogger(__name__)


def missing_is_none(x):
    return None if x == 2147483647 else x


def mix_kwargs(
    user,
    default,
    forced={},
    logging_owner="",
    logging_main_key="",
):
    kwargs = copy.deepcopy(default)

    for k, v in user.items():
        if k in forced and v != forced[k]:
            LOG.warning(
                (
                    f"In {logging_owner} {logging_main_key},"
                    f"ignoring attempt to override {k}={forced[k]} with {k}={v}."
                )
            )
            continue

        if k in default and v != default[k]:
            LOG.warning(
                (
                    f"In {logging_owner} {logging_main_key}, overriding the default value "
                    f"({k}={default[k]}) with {k}={v} is not recommended."
                )
            )

        kwargs[k] = v

    kwargs.update(forced)

    return kwargs


# This does not belong here, should be in the C library
def _get_message_offsets(path):

    fd = os.open(path, os.O_RDONLY)
    try:

        def get(count):
            buf = os.read(fd, count)
            assert len(buf) == count
            n = 0
            for i in buf:
                n = n * 256 + int(i)
            return n

        offset = 0
        while True:
            code = os.read(fd, 4)
            if len(code) < 4:
                break

            if code != b"GRIB":
                offset = os.lseek(fd, offset + 1, os.SEEK_SET)
                continue

            length = get(3)
            edition = get(1)

            if edition == 1:
                if length & 0x800000:
                    sec1len = get(3)
                    os.lseek(fd, 4, os.SEEK_CUR)
                    flags = int.from_bytes(
                        os.read(fd, 1), byteorder="big", signed=False
                    )
                    os.lseek(fd, sec1len - 8, os.SEEK_CUR)

                    if flags & (1 << 7):
                        sec2len = get(3)
                        os.lseek(fd, sec2len - 3, os.SEEK_CUR)

                    if flags & (1 << 6):
                        sec3len = get(3)
                        os.lseek(fd, sec3len - 3, os.SEEK_CUR)

                    sec4len = get(3)

                    if sec4len < 120:
                        length &= 0x7FFFFF
                        length *= 120
                        length -= sec4len
                        length += 4

            if edition == 2:
                length = get(8)

            yield offset, length
            offset = os.lseek(fd, offset + length, os.SEEK_SET)

    finally:
        os.close(fd)


eccodes_codes_release = call_counter(eccodes.codes_release)
eccodes_codes_new_from_file = call_counter(eccodes.codes_new_from_file)


class CodesHandle:
    def __init__(self, handle, path, offset):
        self.handle = handle
        self.path = path
        self.offset = offset

    def __del__(self):
        eccodes_codes_release(self.handle)

    def get(self, name):
        try:
            if name == "values":
                return eccodes.codes_get_values(self.handle)
            if name in ("distinctLatitudes", "distinctLongitudes"):
                return eccodes.codes_get_double_array(self.handle, name)
            return eccodes.codes_get(self.handle, name)
        except eccodes.KeyValueNotFoundError:
            return None


class CodesReader:
    def __init__(self, path):
        self.path = path
        self.file = open(self.path, "rb")

    def __del__(self):
        try:
            self.file.close()
        except Exception:
            pass

    def at_offset(self, offset):
        self.file.seek(offset, 0)
        return next(self)

    def __iter__(self):
        return self

    def __next__(self):
        handle = self._next_handle()
        if handle is None:
            raise StopIteration()
        return handle

    def _next_handle(self):
        offset = self.file.tell()
        handle = eccodes_codes_new_from_file(self.file, eccodes.CODES_PRODUCT_GRIB)
        if not handle:
            return None
        return CodesHandle(handle, self.path, offset)

    @property
    def offset(self):
        return self.file.tell()


class GribField(Base):
    def __init__(self, *, handle=None, reader=None, offset=None):
        assert reader
        self._handle = handle
        self._reader = reader
        self._offset = offset

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        pass

    @property
    def path(self):
        return self.handle.path

    @property
    def handle(self):
        if self._handle is None:
            assert self._offset is not None
            assert self._reader is not None
            self._handle = self._reader.at_offset(self._offset)
        return self._handle

    @property
    def values(self):
        return self.handle.get("values")

    @property
    def offset(self):
        if self._offset is None:
            self._offset = int(self.handle.get("offset"))
        return self._offset

    @property
    def shape(self):
        return (
            missing_is_none(self.handle.get("Nj")),
            missing_is_none(self.handle.get("Ni")),
        )

    def plot_map(self, backend):
        backend.bounding_box(
            north=self.handle.get("latitudeOfFirstGridPointInDegrees"),
            south=self.handle.get("latitudeOfLastGridPointInDegrees"),
            west=self.handle.get("longitudeOfFirstGridPointInDegrees"),
            east=self.handle.get("longitudeOfLastGridPointInDegrees"),
        )
        backend.plot_grib(self.path, self.handle.get("offset"))

    @call_counter
    def to_numpy(self, normalise=False):
        shape = self.shape
        if shape[0] is None or shape[1] is None:
            return self.values
        if normalise:
            return self.values.reshape(self.shape)
        return self.values.reshape(self.shape)

    def __repr__(self):
        return "GribField(%s,%s,%s,%s,%s,%s)" % (
            self.handle.get("shortName"),
            self.handle.get("levelist"),
            self.handle.get("date"),
            self.handle.get("time"),
            self.handle.get("step"),
            self.handle.get("number"),
        )

    def _grid_definition(self):
        return dict(
            north=self.handle.get("latitudeOfFirstGridPointInDegrees"),
            south=self.handle.get("latitudeOfLastGridPointInDegrees"),
            west=self.handle.get("longitudeOfFirstGridPointInDegrees"),
            east=self.handle.get("longitudeOfLastGridPointInDegrees"),
            south_north_increment=self.handle.get("jDirectionIncrementInDegrees"),
            west_east_increment=self.handle.get("iDirectionIncrementInDegrees"),
        )

    def field_metadata(self):
        m = self._grid_definition()
        for n in ("shortName", "units", "paramId"):
            p = self.handle.get(n)
            if p is not None:
                m[n] = str(p)
        m["shape"] = self.shape
        return m

    def datetime(self):
        date = self.handle.get("date")
        time = self.handle.get("time")
        return datetime.datetime(
            date // 10000, date % 10000 // 100, date % 100, time // 100, time % 100
        )

    def valid_datetime(self):
        step = self.handle.get("endStep")
        return self.datetime() + datetime.timedelta(hours=step)

    def to_datetime_list(self):
        return [self.valid_datetime()]

    def to_bounding_box(self):
        return BoundingBox(
            north=self.handle.get("latitudeOfFirstGridPointInDegrees"),
            south=self.handle.get("latitudeOfLastGridPointInDegrees"),
            west=self.handle.get("longitudeOfFirstGridPointInDegrees"),
            east=self.handle.get("longitudeOfLastGridPointInDegrees"),
        )

    def _attributes(self, names):
        result = {}
        for name in names:
            result[name] = self.handle.get(name)
        return result

    def _get(self, name):
        """Private, for testing only"""
        # paramId is renamed as param to get rid of the
        # additional '.128' (in climetlab/scripts/grib.py)
        if name == "param":
            name = "paramId"
        return self.handle.get(name)


class GRIBIterator:
    def __init__(self, path):
        self.path = path
        self.reader = CodesReader(path)

    def __repr__(self):
        return "GRIBIterator(%s)" % (self.path,)

    def __next__(self):
        offset = self.reader.offset
        handle = next(self.reader)
        return GribField(handle=handle, reader=self.reader, offset=offset)

    def __iter__(self):
        return self


class GRIBFilter:
    def __init__(self, reader, filter):
        self._reader = reader
        self._filter = dict(**filter)

    def __repr__(self):
        return "GRIBFilter(%s, %s)" % (self._reader, self._filter)

    def __iter__(self):
        return GRIBIterator(self.path)


# class MultiGribReaders(GriddedMultiReaders):
#     engine = "cfgrib"
#     backend_kwargs = {"squeeze": True}


class GRIBIndex:

    VERSION = 1

    def __init__(self, path):
        assert isinstance(path, str), path
        self.path = path
        self.offsets = None
        self.lengths = None
        self.cache = auxiliary_cache_file(
            "grib-index",
            path,
            content="null",
            extension=".json",
        )

        if not self._load_cache():
            self._build_index()

    def _build_index(self):

        offsets = []
        lengths = []

        for offset, length in _get_message_offsets(self.path):
            offsets.append(offset)
            lengths.append(length)

        self.offsets = offsets
        self.lengths = lengths

        self._save_cache()

    def _save_cache(self):
        try:
            with open(self.cache, "w") as f:
                json.dump(
                    dict(
                        version=self.VERSION,
                        offsets=self.offsets,
                        lengths=self.lengths,
                    ),
                    f,
                )
        except Exception:
            LOG.exception("Write to cache failed %s", self.cache)

    def _load_cache(self):
        try:
            with open(self.cache) as f:
                c = json.load(f)
                if not isinstance(c, dict):
                    return False

                assert c["version"] == self.VERSION
                self.offsets = c["offsets"]
                self.lengths = c["lengths"]
                return True
        except Exception:
            LOG.exception("Load from cache failed %s", self.cache)

        return False


class FieldSetIterator:
    def __init__(self, fieldset):
        self.fieldset = fieldset
        self.i = -1

    def __next__(self):
        self.i += 1
        try:
            return self.fieldset[self.i]
        except IndexError:
            raise StopIteration()


class Field:
    def __init__(self, field):
        self.field = field
        self.keys = {}

    def __del__(self):
        print(self.keys)

    def __getitem__(self, name):
        self.keys[name] = self.field.handle.get(name)
        return self.field.handle.get(name)


class FieldSet:
    def __init__(self, sources):
        self.indexes = [s._reader for s in sources]

    def __iter__(self):
        return FieldSetIterator(self)

    def __getitem__(self, i):
        j = i
        for idx in self.indexes:
            if j <= len(idx):
                return Field(idx[j])
            j -= len(idx)
        raise IndexError(i)


class GRIBReader(Reader):
    appendable = True  # GRIB messages can be added to the same file

    def __init__(self, source, path):
        super().__init__(source, path)
        self._index = None
        self._reader = None
        self._statistics = None

    def __repr__(self):
        return "GRIBReader(%s)" % (self.path,)

    def __iter__(self):
        return GRIBIterator(self.path)

    @property
    def reader(self):
        if self._reader is None:
            self._reader = CodesReader(self.path)
        return self._reader

    @property
    def index(self):
        if self._index is None:
            self._index = GRIBIndex(self.path)
        return self._index

    def __getitem__(self, n):
        return GribField(
            reader=self.reader,
            offset=self.index.offsets[n],
        )

    @property
    def first(self):
        return GribField(reader=self.reader, offset=0)

    def __len__(self):
        return len(self.index.offsets)

    def to_xarray(self, **kwargs):
        return type(self).to_xarray_multi_from_sources([self.source], **kwargs)

    def to_tfdataset(
        self, split=None, shuffle=None, normalize=None, batch_size=0, **kwargs
    ):
        # assert "label" in kwargs
        if "offset" in kwargs:
            return self._to_tfdataset_offset(**kwargs)
        if "label" in kwargs:
            return self._to_tfdataset_supervised(**kwargs)
        else:
            return self._to_tfdataset_unsupervised(**kwargs)

    def _to_tfdataset_offset(self, offset, **kwargs):

        # μ = self.statistics()["average"]
        # σ = self.statistics()["stdev"]

        def normalise(a):
            return a
            # return (a - μ) / σ

        def generate():
            fields = []
            for s in self:
                fields.append(normalise(s.to_numpy()))
                if len(fields) >= offset:
                    yield fields[0], fields[-1]
                    fields.pop(0)

        import tensorflow as tf

        shape = self.first.shape

        dtype = kwargs.get("dtype", tf.float32)
        return tf.data.Dataset.from_generator(
            generate,
            output_signature=(
                tf.TensorSpec(shape, dtype=dtype, name="input"),
                tf.TensorSpec(shape, dtype=dtype, name="output"),
            ),
        )

    def _to_tfdataset_unsupervised(self, **kwargs):
        def generate():
            for s in self:
                yield s.to_numpy()

        import tensorflow as tf

        # TODO check the cost of the conversion
        # maybe default to float64
        dtype = kwargs.get("dtype", tf.float32)
        return tf.data.Dataset.from_generator(generate, dtype)

    def _to_tfdataset_supervised(self, label, **kwargs):
        @call_counter
        def generate():
            for s in self:
                yield s.to_numpy(), s.handle.get(label)

        import tensorflow as tf

        # with timer("_to_tfdataset_supervised shape"):
        shape = self.first.shape

        # TODO check the cost of the conversion
        # maybe default to float64
        dtype = kwargs.get("dtype", tf.float32)
        # with timer("tf.data.Dataset.from_generator"):
        return tf.data.Dataset.from_generator(
            generate,
            output_signature=(
                tf.TensorSpec(shape, dtype=dtype, name="data"),
                tf.TensorSpec(tuple(), dtype=tf.int64, name=label),
            ),
        )

    @classmethod
    def to_xarray_multi_from_sources(cls, sources, **kwargs):
        readers = [source._reader for source in sources]
        assert all(r.__class__ is cls for r in readers)

        import xarray as xr

        xarray_open_dataset_kwargs = {}

        if "xarray_open_mfdataset_kwargs" in kwargs:
            warnings.warn(
                "xarray_open_mfdataset_kwargs is deprecated, please use xarray_open_dataset_kwargs instead."
            )
            kwargs["xarray_open_dataset_kwargs"] = kwargs.pop(
                "xarray_open_mfdataset_kwargs"
            )

        user_xarray_open_mfdataset_kwargs = kwargs.get("xarray_open_dataset_kwargs", {})
        for key in ["backend_kwargs"]:
            xarray_open_dataset_kwargs[key] = mix_kwargs(
                user=user_xarray_open_mfdataset_kwargs.pop(key, {}),
                default={"errors": "raise"},
                forced={},
                logging_owner="xarray_open_dataset_kwargs",
                logging_main_key=key,
            )
        xarray_open_dataset_kwargs.update(
            mix_kwargs(
                user=user_xarray_open_mfdataset_kwargs,
                default={},
                forced={
                    "errors": "raise",
                    "engine": "cfgrib",
                },
            )
        )

        return xr.open_dataset(
            FieldSet(sources),
            **xarray_open_dataset_kwargs,
        )

    @classmethod
    def to_xarray_multi_from_paths(cls, paths, **kwargs):
        return cls.to_xarray_multi_from_sources(
            [load_source("file", path) for path in paths]
        )

    def to_metview(self):
        return FieldSet([load_source("file", self.path)])
        from climetlab.metview import mv_read

        return mv_read(self.path)

    def plot_map(self, backend):
        return self.first.plot_map(backend)

    def plot_graph(self, backend):
        import numpy as np

        what = backend._options("what", "global_average")
        what = dict(
            global_average=np.mean,
        )[what]

        # initialize list of lists
        data = [[s.valid_datetime(), what(s.to_numpy())] for s in self]
        import pandas as pd

        df = pd.DataFrame(data, columns=["date", "param"])

        backend.plot_graph_add_timeserie(df)

    # Used by normalisers
    def to_datetime(self):
        times = self.to_datetime_list()
        assert len(times) == 1
        return times[0]

    def to_datetime_list(self):
        # TODO: check if that can be done faster
        result = set()
        for s in self:
            result.add(s.valid_datetime())
        return sorted(result)

    def to_bounding_box(self):
        return BoundingBox.multi_merge([s.to_bounding_box() for s in self])

    def statistics(self):
        import numpy as np

        if self._statistics is not None:
            return self._statistics

        cache = auxiliary_cache_file(
            "grib-statistics--",
            self.path,
            content="null",
            extension=".json",
        )

        with open(cache) as f:
            self._statistics = json.load(f)

        if self._statistics is not None:
            return self._statistics

        stdev = None
        average = None
        maximum = None
        minimum = None
        count = 0

        for s in self:
            v = s.values
            if count:
                stdev = np.add(stdev, np.multiply(v, v))
                average = np.add(average, v)
                maximum = np.maximum(maximum, v)
                minimum = np.minimum(minimum, v)
            else:
                stdev = np.multiply(v, v)
                average = v
                maximum = v
                minimum = v

            count += 1

        nans = np.count_nonzero(np.isnan(average))
        assert nans == 0, "Statistics with missing values not yet implemented"

        maximum = np.amax(maximum)
        minimum = np.amin(minimum)
        average = np.mean(average) / count
        stdev = np.sqrt(np.mean(stdev) / count - average * average)

        self._statistics = dict(
            minimum=minimum,
            maximum=maximum,
            average=average,
            stdev=stdev,
            count=count,
        )

        with open(cache, "w") as f:
            json.dump(self._statistics, f)

        print(self._statistics)

        return self._statistics

    @classmethod
    def merge(cls, readers):
        from climetlab.mergers import merge_by_class

        assert all(isinstance(s, GRIBReader) for s in readers)
        assert False, readers


def reader(source, path, magic=None, deeper_check=False):
    if magic is None or magic[:4] == b"GRIB":
        return GRIBReader(source, path)
