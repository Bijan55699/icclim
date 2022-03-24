from __future__ import annotations

import contextlib
import shutil

import dask
import psutil
import xarray as xr
import zarr
from rechunker import rechunk
from xarray.core.dataarray import DataArray
from xarray.core.dataset import Dataset

from icclim.icclim_exceptions import InvalidIcclimArgumentError
from icclim.icclim_logger import IcclimLogger
from icclim.pre_processing.input_parsing import read_dataset

TMP_STORE_1 = "icclim-tmp-store-1.zarr"
TMP_STORE_2 = "icclim-tmp-store-2.zarr"
DEFAULT_DASK_CONF = {
    "distributed.worker.memory.target": False,
    "distributed.worker.memory.spill": False,
    "distributed.worker.memory.pause": "0.95",
    "distributed.worker.memory.terminate": "0.98",
}

logger = IcclimLogger.get_instance()


def _get_mem_limit(factor: float = 0.9) -> int:
    if factor > 1 or factor < 0:
        raise ValueError(f"factor was {factor} but, it must be between 0 and 1.")
    try:
        import distributed

        max_sys_mem = (
            distributed.get_client()
            .submit(lambda: distributed.get_worker().memory_limit)
            .result()
        )
    except (ValueError, ImportError):
        # Assumes default scheduler is used
        max_sys_mem = psutil.virtual_memory().total
    return int(factor * max_sys_mem)


@contextlib.contextmanager
def create_optimized_zarr_store(
    in_files: str | list[str] | Dataset | DataArray,
    var_names: str | list[str],
    target_zarr_store_name: str = "icclim-target-store.zarr",
    keep_target_store: bool = False,
    chunking: dict[str, int] | None = None,
) -> xr.Dataset:
    """
    -- EXPERIMENTAL FEATURE --
    API may changes without deprecation warning!

    Context manager to create an zarr store given an input netcdf or xarray structure.
    The execution may take a long time

    The result is rechunked according to `chunking` schema provided.
    By default, when leaving `chunking` to None, the resulting zarr store is NOT chunked
    on time dimension.
    This kind of chunking will significantly speed up the bootstrapping of
    percentiles for indices such as Tx90p, Tx10p, TN90p...
    But such chunking will most likely result in suboptimal performances for other
    indices.
    Actually, when computing indices where no bootstrap is needed,
    you should first try the computation without using `create_optimized_zarr_store`.
    If there are performance issues, you may want to use `create_optimized_zarr_store`
    with a dictionary of a better chunking schema than your current storage chunking.

    By default, `keep_target_store` being False, the resulting zarr store is destroyed
    when the context manager is exit.
    To keep the zarr store for futur uses set `keep_target_store` to True.

    The output is the resulting zarr store as a xarray Dataset.

    Examples
    --------

    >>> import icclim
    >>> with icclim.create_optimized_zarr_store(
    >>>                             in_files="tasmax.nc",
    >>>                             var_names="tasmax",
    >>>                             target_zarr_store_name="tasmax-store.zarr",
    >>>                             chunking={"time": 42, "lat": 42, "lon": 42},
    >>>                             ) as tasmax_opti:
    >>>      su_out = icclim.index(in_files = tasmax_opti, index_name = "su")

    Parameters
    ----------
    in_files : str | list[str] | Dataset | DataArray
        Absolute path(s) to NetCDF dataset(s), including OPeNDAP URLs,
        or path to zarr store, or xarray.Dataset or xarray.DataArray.
    var_names : str | list[str]
        List of data variable to include in the target zarr store.
        All other data variable are dropped.
        The coordinate variable are untouched and are part of the target zarr store.
    target_zarr_store_name : str
        Name of the target zarr store.
        Used to avoid overriding an existing zarr store.
    dim : str
        The dimension on which is optimization is performed.
        This dimension will be unchunked on target zarr store.
    keep_target_store : bool
        Set to True to keep the target zarr store after the execution of the context
        manager.
        Set to False to remove the target zarr store once execution is finished.
        Default is False.

    Returns
    -------
    returns xr.Dataset opened on the newly created target zarr store.

    """
    # According to
    # https://github.com/pangeo-data/rechunker/issues/54#issuecomment-700748875
    # we should limit rechunk mem usage to around 0.9 and avoid spilling to disk
    try:
        shutil.rmtree(TMP_STORE_1, ignore_errors=True)
        shutil.rmtree(TMP_STORE_2, ignore_errors=True)
        shutil.rmtree(target_zarr_store_name, ignore_errors=True)
        yield _unsafe_create_optimized_zarr_store(
            in_files, var_names, target_zarr_store_name, chunking, _get_mem_limit()
        )
    finally:
        shutil.rmtree(TMP_STORE_1, ignore_errors=True)
        shutil.rmtree(TMP_STORE_2, ignore_errors=True)
        if not keep_target_store:
            shutil.rmtree(target_zarr_store_name, ignore_errors=True)


def _unsafe_create_optimized_zarr_store(
    in_files: str | list[str] | Dataset | DataArray,
    var_names: str | list[str],
    zarr_store_name: str,
    chunking: dict[str, int] | None,
    max_mem: int,
):
    with dask.config.set(DEFAULT_DASK_CONF):
        logger.info("Rechunking in progress, this will take some time.")
        ds, _ = read_dataset(in_files, index=None, var_names=var_names)
        # drop all non essential data variables
        ds = ds.drop_vars(filter(lambda v: v not in var_names, ds.data_vars.keys()))
        if len(ds.data_vars.keys()) == 0:
            raise InvalidIcclimArgumentError(
                f"The variable(s) {var_names} were not found in the dataset."
            )
        ds = ds.chunk("auto")
        if chunking is None:
            if len(ds.chunks["time"]) == 1:
                return ds
            chunking = _build_default_chunking(ds)
        # It seems rechunker performs better when the dataset is first converted
        # to a zarr store, without rechunking anything.
        ds.to_zarr(TMP_STORE_1, mode="w")
        ds_zarr = xr.open_zarr(TMP_STORE_1).chunk(chunking)
        target_chunks = {}
        for data_var in ds_zarr.data_vars:
            ds_zarr[data_var].encoding = {}
            acc = {}
            for dim in ds_zarr[data_var].dims:
                acc.update({dim: _get_chunksizes(ds_zarr)[dim][0]})
            target_chunks.update({data_var: acc})
        for c in ds_zarr.coords:
            ds_zarr[c].encoding = {}
            target_chunks.update({c: None})
        rechunk(
            source=ds_zarr,
            target_chunks=target_chunks,
            max_mem=max_mem,
            target_store=zarr_store_name,
            temp_store=TMP_STORE_2,
        ).execute()
        shutil.rmtree(TMP_STORE_1, ignore_errors=True)
        shutil.rmtree(TMP_STORE_2, ignore_errors=True)
        zarr.consolidate_metadata(zarr_store_name)
        return xr.open_zarr(zarr_store_name)


# FIXME To remove once minimal xarray version is v0.20.0 (use .chunksizes instead)
def _get_chunksizes(ds: Dataset) -> dict:
    # Copied and adapted from xarray
    def _chunksizes(da):
        if hasattr(da.data, "chunks"):
            return {dim: c for dim, c in zip(da.dims, da.data.chunks)}
        else:
            return {}

    chunks = {}
    for v in ds.variables.values():
        if hasattr(v.data, "chunks"):
            for dim, c in _chunksizes(v).items():
                if dim in chunks and c != chunks[dim]:
                    raise ValueError(
                        f"Object has inconsistent chunks along dimension {dim}."
                        " This can be fixed by calling unify_chunks()."
                    )
                chunks[dim] = c
    return chunks


def _build_default_chunking(ds: Dataset) -> dict:
    # Leave dask find the best chunking schema for all dimensions but `dim`
    chunking = {d: "auto" for d in ds.dims}
    chunking["time"] = -1
    return chunking
